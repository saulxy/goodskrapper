"""Configuration loading, defaults and validation."""
from __future__ import annotations

import copy
import os
from typing import Any, Dict, Optional

import yaml

DEFAULTS: Dict[str, Any] = {
    "general": {
        "timezone": "America/New_York",
        "db_path": "data/goodskrapper.db",
        "log_level": "INFO",
        "log_file": "",
    },
    "scraper": {
        "update_frequency_minutes": 30,
        "max_pages_per_search": 3,
        "request_delay_seconds": 2.0,
        "only_ending_within_hours": 72,
        "include_buy_now": True,
        "exclude_pickup_only": True,
        "zip_code": "10001",
        "fetch_real_shipping": True,
        "default_shipping_estimate": 15.0,
        "reevaluate_hours": 6,
        "global_exclude_keywords": [],
    },
    "targets": [],
    "profit": {
        "target_profit_pct": 40.0,
        "min_profit_usd": 10.0,
        "sales_tax_pct": 0.0,
        "expected_final_price_multiplier": 1.0,
        "ebay_final_value_fee_pct": 13.6,
        "ebay_fixed_fee": 0.40,
        "ebay_promoted_listing_pct": 0.0,
        "outbound_shipping_cost": 0.0,
    },
    "ebay": {
        "provider": "browse_api",
        "fallback_provider": "sold_scrape",
        "client_id": "",
        "client_secret": "",
        "marketplace_id": "EBAY_US",
        "condition": "used",
        "price_adjustment_factor": 0.85,
        "max_results": 50,
        "min_comparables": 3,
        "min_title_match": 0.6,
        "query_max_words": 7,
        "cache_hours": 12,
        "request_delay_seconds": 1.0,
        "exclude_comp_keywords": [],
    },
    "database": {
        "retention_days": 14,
        "keep_statuses": ["watching", "bid_placed", "won", "sold"],
    },
    "telegram": {
        "enabled": True,
        "bot_token": "",
        "chat_id": "",
        "digest_interval_minutes": 60,
        "urgent_threshold_hours": 8,
        "urgent_check_minutes": 10,
        "max_items_per_digest": 25,
        "send_empty_digest": False,
        "urgent_with_photo": True,
    },
    "dashboard": {
        "host": "0.0.0.0",
        "port": 8080,
        "username": "admin",
        "password": "change-me",
        "secret_key": "",
        "page_size": 50,
    },
}

TARGET_DEFAULTS: Dict[str, Any] = {
    "name": "target",
    "enabled": True,
    "keywords": [],
    "categories": [],
    "exclude_keywords": [],
    "min_price": 0,
    "max_price": 999999,
    "target_profit_pct": None,
}

# Environment variables that override secrets in the YAML file.
ENV_OVERRIDES = {
    "GOODSKRAPPER_TELEGRAM_TOKEN": ("telegram", "bot_token"),
    "GOODSKRAPPER_TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
    "GOODSKRAPPER_EBAY_CLIENT_ID": ("ebay", "client_id"),
    "GOODSKRAPPER_EBAY_CLIENT_SECRET": ("ebay", "client_secret"),
    "GOODSKRAPPER_DASHBOARD_PASSWORD": ("dashboard", "password"),
    "GOODSKRAPPER_SECRET_KEY": ("dashboard", "secret_key"),
}

VALID_PROVIDERS = ("browse_api", "sold_scrape")


class ConfigError(Exception):
    pass


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def default_config_path() -> str:
    env = os.environ.get("GOODSKRAPPER_CONFIG")
    if env:
        return env
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "config.yaml")


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    path = os.path.abspath(path or default_config_path())
    if not os.path.exists(path):
        raise ConfigError(
            f"Config file not found: {path}\n"
            "Copy config.example.yaml to config.yaml and edit it."
        )
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")

    cfg = _deep_merge(DEFAULTS, raw)
    cfg["_path"] = path
    base_dir = os.path.dirname(path)

    for env, (section, key) in ENV_OVERRIDES.items():
        if os.environ.get(env):
            cfg[section][key] = os.environ[env]

    # Resolve paths relative to the config file
    for key in ("db_path", "log_file"):
        value = cfg["general"].get(key)
        if value and not os.path.isabs(value):
            cfg["general"][key] = os.path.join(base_dir, value)

    # Normalise targets
    targets = []
    for i, t in enumerate(_as_list(cfg.get("targets"))):
        if not isinstance(t, dict):
            raise ConfigError(f"targets[{i}] must be a mapping")
        t = _deep_merge(TARGET_DEFAULTS, t)
        t["keywords"] = [str(k).strip() for k in _as_list(t["keywords"]) if str(k).strip()]
        try:
            t["categories"] = [int(c) for c in _as_list(t["categories"])]
        except (TypeError, ValueError):
            raise ConfigError(f"targets[{i}].categories must be numeric IDs")
        t["exclude_keywords"] = [str(k).lower() for k in _as_list(t["exclude_keywords"])]
        if not t["keywords"] and not t["categories"]:
            raise ConfigError(f"Target '{t['name']}' needs keywords and/or categories")
        targets.append(t)
    cfg["targets"] = targets

    _validate(cfg)
    return cfg


def _validate(cfg: Dict[str, Any]) -> None:
    def positive(section: str, key: str, allow_zero: bool = False) -> None:
        value = cfg[section][key]
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{section}.{key} must be a number")
        if value < 0 or (value == 0 and not allow_zero):
            raise ConfigError(f"{section}.{key} must be {'>= 0' if allow_zero else '> 0'}")

    positive("scraper", "update_frequency_minutes")
    positive("scraper", "max_pages_per_search")
    positive("scraper", "request_delay_seconds", allow_zero=True)
    positive("scraper", "only_ending_within_hours", allow_zero=True)
    positive("profit", "target_profit_pct", allow_zero=True)
    positive("profit", "expected_final_price_multiplier")
    positive("database", "retention_days")
    positive("telegram", "digest_interval_minutes")
    positive("telegram", "urgent_threshold_hours", allow_zero=True)
    positive("telegram", "urgent_check_minutes")

    ebay = cfg["ebay"]
    if ebay["provider"] not in VALID_PROVIDERS:
        raise ConfigError(f"ebay.provider must be one of {VALID_PROVIDERS}")
    if ebay.get("fallback_provider") and ebay["fallback_provider"] not in VALID_PROVIDERS:
        raise ConfigError(f"ebay.fallback_provider must be empty or one of {VALID_PROVIDERS}")
    if ebay["condition"] not in ("used", "new", "any"):
        raise ConfigError("ebay.condition must be used | new | any")

    cfg["scraper"]["global_exclude_keywords"] = [
        str(k).lower() for k in _as_list(cfg["scraper"]["global_exclude_keywords"])
    ]
    cfg["ebay"]["exclude_comp_keywords"] = [
        str(k).lower() for k in _as_list(cfg["ebay"]["exclude_comp_keywords"])
    ]
    cfg["database"]["keep_statuses"] = [str(s) for s in _as_list(cfg["database"]["keep_statuses"])]
    cfg["telegram"]["chat_id"] = str(cfg["telegram"]["chat_id"]).strip()


def target_profit_for(cfg: Dict[str, Any], target_name: Optional[str]) -> float:
    for t in cfg["targets"]:
        if t["name"] == target_name and t.get("target_profit_pct") is not None:
            return float(t["target_profit_pct"])
    return float(cfg["profit"]["target_profit_pct"])


def masked(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of the config with secrets hidden (for the dashboard)."""
    out = copy.deepcopy(cfg)
    secrets = [("telegram", "bot_token"), ("ebay", "client_secret"), ("ebay", "client_id"),
               ("dashboard", "password"), ("dashboard", "secret_key")]
    for section, key in secrets:
        value = out.get(section, {}).get(key)
        if value:
            out[section][key] = value[:4] + "…" if len(str(value)) > 8 else "****"
    return out
