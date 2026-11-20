"""SQLite storage. One short-lived connection per operation keeps it thread/process safe."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

STATUSES = ["new", "watching", "bid_placed", "won", "lost", "sold", "ignored"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id            INTEGER PRIMARY KEY,
    title              TEXT NOT NULL,
    url                TEXT,
    image_url          TEXT,
    category_id        INTEGER,
    category_name      TEXT,
    target_name        TEXT,
    listing_type       TEXT DEFAULT 'auction',
    current_price      REAL DEFAULT 0,
    minimum_bid        REAL DEFAULT 0,
    buy_now_price      REAL DEFAULT 0,
    num_bids           INTEGER DEFAULT 0,
    end_time           TEXT,
    goodwill_shipping  REAL,
    shipping_estimated INTEGER DEFAULT 1,
    ebay_query         TEXT,
    ebay_price         REAL,
    ebay_price_manual  REAL,
    ebay_comps         INTEGER DEFAULT 0,
    ebay_low           REAL,
    ebay_high          REAL,
    ebay_provider      TEXT,
    ebay_url           TEXT,
    buy_price          REAL,
    total_cost         REAL,
    est_fees           REAL,
    est_profit         REAL,
    profit_pct         REAL,
    max_bid            REAL,
    is_deal            INTEGER DEFAULT 0,
    status             TEXT DEFAULT 'new',
    notes              TEXT DEFAULT '',
    source             TEXT DEFAULT 'scraper',
    first_seen         TEXT,
    last_updated       TEXT,
    notified_at        TEXT,
    urgent_notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_end ON items(end_time);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_first_seen ON items(first_seen);

CREATE TABLE IF NOT EXISTS seen (
    item_id      INTEGER PRIMARY KEY,
    price        REAL,
    profit_pct   REAL,
    evaluated_at TEXT
);

CREATE TABLE IF NOT EXISTS ebay_cache (
    query      TEXT PRIMARY KEY,
    provider   TEXT,
    median     REAL,
    low        REAL,
    high       REAL,
    count      INTEGER,
    url        TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns the scraper is allowed to overwrite on an existing row
MARKET_FIELDS = [
    "title", "url", "image_url", "category_id", "category_name", "listing_type",
    "current_price", "minimum_bid", "buy_now_price", "num_bids", "end_time",
    "goodwill_shipping", "shipping_estimated", "ebay_query", "ebay_price", "ebay_comps",
    "ebay_low", "ebay_high", "ebay_provider", "ebay_url", "buy_price", "total_cost",
    "est_fees", "est_profit", "profit_pct", "max_bid", "is_deal",
]
EDITABLE_FIELDS = MARKET_FIELDS + ["target_name", "ebay_price_manual", "status", "notes", "source",
                                   "notified_at", "urgent_notified_at"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Database:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    # ------------------------------------------------------------------ items
    def get_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def insert_item(self, data: Dict[str, Any]) -> None:
        now = iso(utcnow())
        data = dict(data)
        data.setdefault("first_seen", now)
        data["last_updated"] = now
        data.setdefault("status", "new")
        cols = [k for k in data if k in EDITABLE_FIELDS + ["item_id", "first_seen", "last_updated"]]
        sql = f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        with self.conn() as c:
            c.execute(sql, [data[k] for k in cols])

    def update_item(self, item_id: int, data: Dict[str, Any], allowed: Iterable[str] = EDITABLE_FIELDS) -> None:
        allowed = set(allowed)
        fields = {k: v for k, v in data.items() if k in allowed}
        if not fields:
            return
        fields["last_updated"] = iso(utcnow())
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE items SET {sets} WHERE item_id=?", list(fields.values()) + [item_id])

    def upsert_market(self, data: Dict[str, Any]) -> bool:
        """Insert a scraped item or refresh its market data. Returns True if new."""
        existing = self.get_item(data["item_id"])
        if existing is None:
            self.insert_item(data)
            return True
        self.update_item(data["item_id"], data, allowed=MARKET_FIELDS)
        return False

    def delete_items(self, item_ids: Iterable[int]) -> int:
        ids = [int(i) for i in item_ids]
        if not ids:
            return 0
        with self.conn() as c:
            cur = c.execute(f"DELETE FROM items WHERE item_id IN ({','.join('?' for _ in ids)})", ids)
            c.execute(f"DELETE FROM seen WHERE item_id IN ({','.join('?' for _ in ids)})", ids)
            return cur.rowcount

    def set_status(self, item_ids: Iterable[int], status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status}")
        for i in item_ids:
            self.update_item(int(i), {"status": status})

    def list_items(
        self,
        q: str = "",
        status: str = "",
        active_only: bool = False,
        deals_only: bool = False,
        sort: str = "end_time",
        desc: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        where, args = [], []
        if q:
            where.append("(title LIKE ? OR notes LIKE ? OR CAST(item_id AS TEXT)=? OR target_name LIKE ?)")
            args += [f"%{q}%", f"%{q}%", q, f"%{q}%"]
        if status:
            where.append("status=?")
            args.append(status)
        if active_only:
            where.append("(end_time IS NULL OR end_time > ?)")
            args.append(iso(utcnow()))
        if deals_only:
            where.append("is_deal=1")
        sortable = {"end_time", "profit_pct", "est_profit", "first_seen", "current_price", "ebay_price",
                    "title", "status", "max_bid"}
        sort = sort if sort in sortable else "end_time"
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        order = f"ORDER BY {sort} IS NULL, {sort} {'DESC' if desc else 'ASC'}"
        with self.conn() as c:
            total = c.execute(f"SELECT COUNT(*) FROM items {clause}", args).fetchone()[0]
            rows = c.execute(f"SELECT * FROM items {clause} {order} LIMIT ? OFFSET ?",
                             args + [limit, offset]).fetchall()
        return [dict(r) for r in rows], total

    def stats(self) -> Dict[str, Any]:
        now = iso(utcnow())
        with self.conn() as c:
            row = c.execute(
                """SELECT COUNT(*) total,
                          SUM(is_deal=1 AND (end_time IS NULL OR end_time > ?)) active_deals,
                          SUM(end_time IS NOT NULL AND end_time <= ?) ended,
                          SUM(status='watching') watching,
                          SUM(notified_at IS NULL AND is_deal=1) pending_notify
                   FROM items""", (now, now)).fetchone()
        return {k: (row[k] or 0) for k in row.keys()}

    def items_to_notify(self) -> List[Dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute(
                """SELECT * FROM items
                   WHERE is_deal=1 AND notified_at IS NULL AND status NOT IN ('ignored','lost','won','sold')
                     AND (end_time IS NULL OR end_time > ?)
                   ORDER BY end_time ASC""", (iso(utcnow()),)).fetchall()
        return [dict(r) for r in rows]

    def items_ending_within(self, hours: float, deals_only: bool = False) -> List[Dict[str, Any]]:
        now = utcnow()
        sql = """SELECT * FROM items WHERE end_time > ? AND end_time <= ?
                 AND status NOT IN ('ignored','lost','won','sold')"""
        if deals_only:
            sql += " AND is_deal=1"
        with self.conn() as c:
            rows = c.execute(sql + " ORDER BY end_time ASC",
                             (iso(now), iso(now + timedelta(hours=hours)))).fetchall()
        return [dict(r) for r in rows]

    def mark_notified(self, item_ids: Iterable[int], urgent: bool = False) -> None:
        now = iso(utcnow())
        ids = [int(i) for i in item_ids]
        if not ids:
            return
        cols = "notified_at=?, urgent_notified_at=?" if urgent else "notified_at=?"
        vals = [now, now] if urgent else [now]
        with self.conn() as c:
            c.execute(f"UPDATE items SET {cols} WHERE item_id IN ({','.join('?' for _ in ids)})", vals + ids)

    # ------------------------------------------------------------------ seen
    def get_seen(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM seen WHERE item_id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def mark_seen(self, item_id: int, price: float, profit_pct: Optional[float]) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO seen(item_id, price, profit_pct, evaluated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(item_id) DO UPDATE SET price=excluded.price, profit_pct=excluded.profit_pct, "
                "evaluated_at=excluded.evaluated_at",
                (item_id, price, profit_pct, iso(utcnow())))

    # ------------------------------------------------------------------ ebay cache
    def get_cache(self, query: str, max_age_hours: float) -> Optional[Dict[str, Any]]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM ebay_cache WHERE query=?", (query,)).fetchone()
        if not row:
            return None
        fetched = parse_iso(row["fetched_at"])
        if not fetched or utcnow() - fetched > timedelta(hours=max_age_hours):
            return None
        return dict(row)

    def put_cache(self, query: str, provider: str, median: Optional[float], low: Optional[float],
                  high: Optional[float], count: int, url: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO ebay_cache(query, provider, median, low, high, count, url, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (query, provider, median, low, high, count, url, iso(utcnow())))

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: Optional[str]) -> None:
        with self.conn() as c:
            if value is None:
                c.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value))

    # ------------------------------------------------------------------ retention
    def cleanup(self, retention_days: float, keep_statuses: List[str], cache_hours: float) -> Dict[str, int]:
        cutoff = iso(utcnow() - timedelta(days=retention_days))
        cache_cutoff = iso(utcnow() - timedelta(hours=max(cache_hours, 1) * 2))
        keep = list(keep_statuses) or ["__none__"]
        with self.conn() as c:
            items = c.execute(
                f"DELETE FROM items WHERE first_seen < ? AND status NOT IN ({','.join('?' for _ in keep)})",
                [cutoff] + keep).rowcount
            seen = c.execute("DELETE FROM seen WHERE evaluated_at < ?", (cutoff,)).rowcount
            cache = c.execute("DELETE FROM ebay_cache WHERE fetched_at < ?", (cache_cutoff,)).rowcount
        with self.conn() as c:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"items": items, "seen": seen, "cache": cache}
