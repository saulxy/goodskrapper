"""Telegram Bot API client + message formatting."""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from .db import parse_iso

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

log = logging.getLogger(__name__)
MAX_TEXT = 4000      # Telegram limit is 4096
MAX_CAPTION = 1000   # Telegram limit is 1024


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, tg_cfg: Dict[str, Any], session: Optional[requests.Session] = None):
        self.cfg = tg_cfg
        self.token = str(tg_cfg.get("bot_token") or "").strip()
        self.chat_id = str(tg_cfg.get("chat_id") or "").strip()
        self.session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.cfg.get("enabled", True) and self.token and self.chat_id)

    def _call(self, method: str, payload: Dict[str, Any], attempts: int = 4) -> Dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for attempt in range(attempts):
            try:
                r = self.session.post(url, json=payload, timeout=30)
            except requests.RequestException as exc:
                log.warning("Telegram %s network error: %s", method, exc)
                time.sleep(5 * (attempt + 1))
                continue
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 429:
                wait = int((data.get("parameters") or {}).get("retry_after", 5))
                log.info("Telegram rate limited, sleeping %ss", wait)
                time.sleep(wait + 1)
                continue
            if not data.get("ok"):
                raise TelegramError(f"{method} failed ({r.status_code}): {data.get('description', r.text[:200])}")
            return data
        raise TelegramError(f"{method} failed after {attempts} attempts")

    def send_message(self, text: str) -> None:
        for chunk in split_message(text):
            self._call("sendMessage", {"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                                       "disable_web_page_preview": True})

    def send_photo(self, photo_url: str, caption: str) -> None:
        if len(caption) > MAX_CAPTION or not photo_url:
            self.send_message(caption)
            return
        try:
            self._call("sendPhoto", {"chat_id": self.chat_id, "photo": photo_url, "caption": caption,
                                     "parse_mode": "HTML"})
        except TelegramError as exc:
            log.info("sendPhoto failed (%s), falling back to text", exc)
            self.send_message(caption)

    def get_me(self) -> Dict[str, Any]:
        return self._call("getMe", {})["result"]

    def get_updates(self) -> List[Dict[str, Any]]:
        return self._call("getUpdates", {"allowed_updates": ["channel_post", "message", "my_chat_member"]})["result"]


def split_message(text: str, limit: int = MAX_TEXT) -> List[str]:
    """Split on blank lines (item blocks) so HTML tags never get cut."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current[:limit])
    return chunks


# ---------------------------------------------------------------------- formatting
def money(v: Any) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def time_left(end_iso: Optional[str], now: Optional[datetime] = None) -> str:
    end = parse_iso(end_iso)
    if not end:
        return "?"
    secs = int((end - (now or datetime.now(timezone.utc))).total_seconds())
    if secs <= 0:
        return "ended"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def local_time(end_iso: Optional[str], tz_name: str) -> str:
    end = parse_iso(end_iso)
    if not end:
        return "?"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return end.astimezone(tz).strftime("%a %b %d %I:%M %p").replace(" 0", " ")


def format_item(item: Dict[str, Any], tz_name: str, index: Optional[int] = None) -> str:
    title = html.escape(item.get("title") or "")[:120]
    prefix = f"{index}. " if index is not None else ""
    sale = item.get("ebay_price_manual") or item.get("ebay_price")
    ship_note = "" if not item.get("shipping_estimated") else " est."
    price_label = "Buy now" if item.get("listing_type") == "buy_now" else f"Bid ({item.get('num_bids', 0)} bids)"
    lines = [
        f"{prefix}<b><a href=\"{html.escape(item.get('url') or '')}\">{title}</a></b>",
        f"💵 {price_label}: {money(item.get('buy_price'))} + S&amp;H {money(item.get('goodwill_shipping'))}{ship_note}",
        f"🏷 eBay ≈ {money(sale)} ({item.get('ebay_comps') or 0} comps"
        + (", manual" if item.get("ebay_price_manual") else "") + ")"
        + (f" · <a href=\"{html.escape(item['ebay_url'])}\">sold comps</a>" if item.get("ebay_url") else ""),
        f"📈 Profit {money(item.get('est_profit'))} (<b>{float(item.get('profit_pct') or 0):.0f}%</b>)"
        f" · Max bid {money(item.get('max_bid'))}",
        f"⏰ {local_time(item.get('end_time'), tz_name)} ({time_left(item.get('end_time'))} left)",
    ]
    return "\n".join(lines)


def format_digest(items: List[Dict[str, Any]], tz_name: str, total: int) -> str:
    header = f"🛒 <b>GoodSkrapper</b> — {total} new deal{'s' if total != 1 else ''}"
    if total > len(items):
        header += f" (showing top {len(items)})"
    blocks = [header] + [format_item(it, tz_name, i + 1) for i, it in enumerate(items)]
    return "\n\n".join(blocks)


def format_urgent(item: Dict[str, Any], tz_name: str) -> str:
    return f"🚨 <b>ENDING SOON — {time_left(item.get('end_time'))} left</b>\n" + format_item(item, tz_name)
