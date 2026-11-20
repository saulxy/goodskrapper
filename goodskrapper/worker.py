"""Scrape / evaluate / notify engine and the scheduler loop."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .config import target_profit_for
from .db import Database, iso, parse_iso, utcnow
from .ebay import EbayPricer
from .goodwill import GoodwillClient, make_session
from .profit import evaluate_row, projected_buy_price
from .telegram import TelegramClient, TelegramError, format_digest, format_urgent

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: Dict[str, Any], db: Optional[Database] = None):
        self.cfg = cfg
        self.db = db or Database(cfg["general"]["db_path"])
        session = make_session()
        self.gw = GoodwillClient(cfg["scraper"], session)
        self.pricer = EbayPricer(cfg["ebay"], self.db)
        self.tg = TelegramClient(cfg["telegram"])
        self.tz = cfg["general"]["timezone"]
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ evaluation
    def _excluded(self, title: str, target: Dict[str, Any]) -> bool:
        t = title.lower()
        words = self.cfg["scraper"]["global_exclude_keywords"] + target.get("exclude_keywords", [])
        return any(w and w in t for w in words)

    def evaluate(self, listing: Dict[str, Any], target_name: Optional[str],
                 existing: Optional[Dict[str, Any]] = None, force: bool = False) -> Optional[Dict[str, Any]]:
        """Price a Goodwill listing on eBay and compute profit. Returns the row to store or None."""
        scfg = self.cfg["scraper"]
        target_pct = target_profit_for(self.cfg, target_name)
        buy_price = projected_buy_price(listing, self.cfg["profit"])

        if existing is None and not force:
            seen = self.db.get_seen(listing["item_id"])
            if seen:
                fresh = utcnow() - (parse_iso(seen["evaluated_at"]) or utcnow()) < timedelta(
                    hours=float(scfg["reevaluate_hours"]))
                if fresh and abs((seen["price"] or 0) - buy_price) < 0.01:
                    return None

        query = existing.get("ebay_query") if existing else None
        est = self.pricer.estimate(listing["title"], query=query or None)

        row: Dict[str, Any] = dict(listing)
        row.pop("listing_shipping", None)
        row.pop("pickup_only", None)
        row.pop("ended", None)
        row["target_name"] = existing.get("target_name") if existing else target_name
        if existing and not existing.get("shipping_estimated") and existing.get("goodwill_shipping") is not None:
            row["goodwill_shipping"], row["shipping_estimated"] = existing["goodwill_shipping"], 0
        else:
            row["goodwill_shipping"], row["shipping_estimated"] = float(scfg["default_shipping_estimate"]), 1
        if est:
            row.update({"ebay_query": est["query"], "ebay_price": est["median"], "ebay_comps": est["count"],
                        "ebay_low": est["low"], "ebay_high": est["high"], "ebay_provider": est["provider"],
                        "ebay_url": est["url"]})
        elif existing:
            for k in ("ebay_query", "ebay_price", "ebay_comps", "ebay_low", "ebay_high", "ebay_provider", "ebay_url"):
                row[k] = existing.get(k)
        if existing:
            row["ebay_price_manual"] = existing.get("ebay_price_manual")

        if row.get("ebay_price") is None and row.get("ebay_price_manual") is None:
            if existing is None and not force:
                self.db.mark_seen(listing["item_id"], buy_price, None)
            row["is_deal"] = 0
            return row if (existing or force) else None

        calc = evaluate_row(row, self.cfg, target_pct)
        # Near a deal with estimated shipping -> ask Goodwill for the real quote
        if (row["shipping_estimated"] and scfg.get("fetch_real_shipping")
                and calc.get("profit_pct", -999) >= target_pct * 0.75):
            quote = self.gw.shipping_quote(listing["item_id"])
            if quote is not None:
                row["goodwill_shipping"], row["shipping_estimated"] = quote, 0
                calc = evaluate_row(row, self.cfg, target_pct)
        row.update(calc)

        if not row["is_deal"] and existing is None and not force:
            self.db.mark_seen(listing["item_id"], buy_price, row.get("profit_pct"))
            return None
        return row

    # ------------------------------------------------------------------ scrape
    def scrape_cycle(self) -> Dict[str, int]:
        with self._lock:
            return self._scrape_cycle()

    def _scrape_cycle(self) -> Dict[str, int]:
        scfg = self.cfg["scraper"]
        horizon_h = float(scfg["only_ending_within_hours"])
        horizon = utcnow() + timedelta(hours=horizon_h) if horizon_h > 0 else None
        stats = {"listings": 0, "evaluated": 0, "new_deals": 0, "updated": 0}
        processed = set()
        started = time.monotonic()

        for target in self.cfg["targets"]:
            if not target.get("enabled", True):
                continue
            for keyword in (target["keywords"] or [""]):
                log.info("Searching target=%r keyword=%r categories=%s", target["name"], keyword,
                         target["categories"])
                for listing in self.gw.search(keyword, target["categories"], target["min_price"],
                                              target["max_price"], int(scfg["max_pages_per_search"])):
                    stats["listings"] += 1
                    end = parse_iso(listing.get("end_time"))
                    if end and horizon and end > horizon:
                        break  # results are sorted by ending soonest
                    if end and end <= utcnow():
                        continue
                    if listing["item_id"] in processed:
                        continue
                    processed.add(listing["item_id"])
                    if listing["listing_type"] == "buy_now" and not scfg.get("include_buy_now", True):
                        continue
                    if self._excluded(listing["title"], target):
                        continue
                    existing = self.db.get_item(listing["item_id"])
                    try:
                        row = self.evaluate(listing, target["name"], existing)
                    except Exception:  # keep the cycle alive on unexpected data
                        log.exception("Evaluation failed for item %s", listing["item_id"])
                        continue
                    if row is None:
                        continue
                    stats["evaluated"] += 1
                    if self.db.upsert_market(row):
                        stats["new_deals"] += 1
                        log.info("DEAL %s %.0f%% %s", row["item_id"], row.get("profit_pct") or 0, row["title"])
                    else:
                        stats["updated"] += 1

        stats["seconds"] = int(time.monotonic() - started)
        self.db.set_meta("last_scrape", iso(utcnow()))
        self.db.set_meta("last_scrape_stats", json.dumps(stats))
        log.info("Scrape finished: %s", stats)
        return stats

    # ------------------------------------------------------------------ refresh
    def refresh_item(self, item_id: int, reprice: bool = False) -> Optional[Dict[str, Any]]:
        """Re-fetch an item from Goodwill (and optionally eBay) and recompute profit."""
        existing = self.db.get_item(item_id)
        detail = self.gw.item_detail(item_id)
        if detail is None:
            return existing
        if existing is None:
            row = self.evaluate(detail, None, None, force=True) or {}
            row["source"] = "manual"
            row["status"] = "watching"
            self.db.insert_item(row)
            return self.db.get_item(item_id)
        if reprice:
            row = self.evaluate(detail, existing.get("target_name"), existing, force=True) or {}
        else:
            row = dict(existing)
            for k in ("title", "url", "image_url", "category_id", "category_name", "listing_type",
                      "current_price", "minimum_bid", "buy_now_price", "num_bids", "end_time"):
                if detail.get(k) not in (None, ""):
                    row[k] = detail[k]
            row.update(evaluate_row(row, self.cfg, target_profit_for(self.cfg, existing.get("target_name"))))
        self.db.update_item(item_id, row)
        return self.db.get_item(item_id)

    def recompute(self, item_id: int) -> None:
        row = self.db.get_item(item_id)
        if row:
            self.db.update_item(item_id, evaluate_row(row, self.cfg,
                                                      target_profit_for(self.cfg, row.get("target_name"))))

    def refresh_ending_soon(self) -> int:
        hours = float(self.cfg["telegram"]["urgent_threshold_hours"])
        items = self.db.items_ending_within(hours)
        for it in items:
            try:
                self.refresh_item(it["item_id"])
            except Exception:
                log.exception("Refresh failed for %s", it["item_id"])
        return len(items)

    # ------------------------------------------------------------------ notify
    def send_urgent(self) -> int:
        if not self.tg.configured:
            return 0
        hours = float(self.cfg["telegram"]["urgent_threshold_hours"])
        sent = 0
        for it in self.db.items_ending_within(hours, deals_only=True):
            if it.get("urgent_notified_at"):
                continue
            text = format_urgent(it, self.tz)
            try:
                if self.cfg["telegram"].get("urgent_with_photo") and it.get("image_url"):
                    self.tg.send_photo(it["image_url"], text)
                else:
                    self.tg.send_message(text)
            except TelegramError as exc:
                log.error("Urgent alert failed: %s", exc)
                break
            self.db.mark_notified([it["item_id"]], urgent=True)
            sent += 1
        if sent:
            log.info("Sent %d urgent alerts", sent)
        return sent

    def send_digest(self) -> int:
        self.db.set_meta("last_digest", iso(utcnow()))
        if not self.tg.configured:
            log.info("Telegram not configured - skipping digest")
            return 0
        items = self.db.items_to_notify()
        limit = int(self.cfg["telegram"]["max_items_per_digest"])
        if not items:
            if self.cfg["telegram"].get("send_empty_digest"):
                self.tg.send_message("🛒 <b>GoodSkrapper</b> — no new deals this hour.")
            return 0
        top = sorted(items, key=lambda r: r.get("profit_pct") or 0, reverse=True)[:limit]
        try:
            self.tg.send_message(format_digest(top, self.tz, len(items)))
        except TelegramError as exc:
            log.error("Digest failed: %s", exc)
            return 0
        self.db.mark_notified([r["item_id"] for r in top])
        log.info("Digest sent with %d items", len(top))
        return len(top)

    def cleanup(self) -> Dict[str, int]:
        d = self.cfg["database"]
        res = self.db.cleanup(float(d["retention_days"]), d["keep_statuses"],
                              float(self.cfg["ebay"]["cache_hours"]))
        log.info("Cleanup removed %s", res)
        return res


class Worker:
    """Single-threaded scheduler loop (light on a Raspberry Pi)."""
    TICK = 15

    def __init__(self, cfg: Dict[str, Any], engine: Optional[Engine] = None):
        self.cfg = cfg
        self.engine = engine or Engine(cfg)
        self.stop_event = threading.Event()

    def _job(self, name: str, fn) -> None:
        try:
            fn()
        except Exception:
            log.exception("Job %s crashed", name)

    def run_forever(self) -> None:
        db = self.engine.db
        scfg, tcfg = self.cfg["scraper"], self.cfg["telegram"]
        scrape_every = float(scfg["update_frequency_minutes"]) * 60
        digest_every = float(tcfg["digest_interval_minutes"]) * 60
        urgent_every = float(tcfg["urgent_check_minutes"]) * 60

        now = time.time()
        last_digest = parse_iso(db.get_meta("last_digest"))
        next_digest = (last_digest.timestamp() + digest_every) if last_digest else now + digest_every
        next_scrape, next_urgent, next_cleanup = now, now + urgent_every, now
        log.info("Worker started: scrape every %.0f min, digest every %.0f min, urgent window %sh",
                 scrape_every / 60, digest_every / 60, tcfg["urgent_threshold_hours"])

        while not self.stop_event.is_set():
            now = time.time()
            if db.get_meta("run_requested"):
                db.set_meta("run_requested", None)
                next_scrape = now
            if now >= next_cleanup:
                self._job("cleanup", self.engine.cleanup)
                next_cleanup = now + 6 * 3600
            if now >= next_scrape:
                self._job("scrape", self.engine.scrape_cycle)
                self._job("urgent", self.engine.send_urgent)
                next_scrape = time.time() + scrape_every
            if now >= next_urgent:
                self._job("refresh", self.engine.refresh_ending_soon)
                self._job("urgent", self.engine.send_urgent)
                next_urgent = time.time() + urgent_every
            if now >= next_digest:
                self._job("digest", self.engine.send_digest)
                next_digest = time.time() + digest_every
            self.stop_event.wait(self.TICK)
        log.info("Worker stopped")

    def stop(self) -> None:
        self.stop_event.set()
