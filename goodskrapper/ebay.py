"""eBay price research: official Browse API (active listings) or sold-listings page parsing."""
from __future__ import annotations

import base64
import logging
import re
import statistics
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from .db import Database
from .goodwill import make_session

log = logging.getLogger(__name__)

STOPWORDS = {
    "a", "an", "and", "the", "of", "for", "with", "w", "in", "on", "to", "by", "from", "or",
    "lot", "lots", "bundle", "set", "pc", "pcs", "piece", "pieces", "assorted", "misc", "mixed",
    "untested", "tested", "working", "works", "work", "as", "is", "asis", "see", "pics", "photos",
    "new", "used", "preowned", "pre-owned", "nwt", "nwot", "nib", "nos", "great", "good", "excellent",
    "condition", "free", "ship", "shipping", "authentic", "genuine", "rare", "htf", "l@@k", "look",
    "sz", "size", "only", "includes", "included", "inc", "w/", "&", "+", "-", "|", "/",
}

SOLD_URL = "https://www.ebay.com/sch/i.html"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
CONDITION_IDS = {"used": "3000", "new": "1000"}


class ProviderError(Exception):
    pass


def tokenize(text: str) -> List[str]:
    text = (text or "").lower().replace("'", "")
    return [t for t in re.split(r"[^a-z0-9.\-]+", text) if t and t.strip(".-")]


def build_query(title: str, max_words: int = 7) -> str:
    words = []
    for tok in tokenize(title):
        tok = tok.strip(".-")
        if tok in STOPWORDS or len(tok) < 2 and not tok.isdigit():
            continue
        if tok not in words:
            words.append(tok)
        if len(words) >= max_words:
            break
    return " ".join(words)


def sold_search_url(query: str, condition: str = "any") -> str:
    params = {"_nkw": query, "LH_Sold": "1", "LH_Complete": "1", "_ipg": "60"}
    if condition in CONDITION_IDS:
        params["LH_ItemCondition"] = CONDITION_IDS[condition]
    return f"{SOLD_URL}?{urlencode(params)}"


def title_match(query: str, title: str) -> float:
    q = set(query.split())
    if not q:
        return 0.0
    t = set(tok.strip(".-") for tok in tokenize(title))
    return len(q & t) / len(q)


def summarize(prices: List[float]) -> Optional[Tuple[float, float, float, int]]:
    """Median with IQR outlier trimming -> (median, p25, p75, n)."""
    prices = sorted(p for p in prices if p and p > 0)
    if not prices:
        return None
    if len(prices) >= 4:
        q = statistics.quantiles(prices, n=4)
        iqr = q[2] - q[0]
        lo, hi = q[0] - 1.5 * iqr, q[2] + 1.5 * iqr
        trimmed = [p for p in prices if lo <= p <= hi]
        prices = trimmed or prices
    med = statistics.median(prices)
    if len(prices) >= 4:
        q = statistics.quantiles(prices, n=4)
        return med, q[0], q[2], len(prices)
    return med, prices[0], prices[-1], len(prices)


def _parse_price(text: str) -> Optional[float]:
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text or "")
    return float(m.group(1).replace(",", "")) if m else None


# ---------------------------------------------------------------------- providers
class BrowseApiProvider:
    name = "browse_api"

    def __init__(self, cfg: Dict[str, Any], session: requests.Session):
        self.cfg = cfg
        self.session = session
        self._token: Optional[str] = None
        self._token_exp = 0.0

    def available(self) -> bool:
        return bool(self.cfg.get("client_id") and self.cfg.get("client_secret"))

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        creds = f"{self.cfg['client_id']}:{self.cfg['client_secret']}".encode()
        r = self.session.post(
            TOKEN_URL,
            headers={"Authorization": "Basic " + base64.b64encode(creds).decode(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
            timeout=30)
        if r.status_code != 200:
            raise ProviderError(f"eBay OAuth failed ({r.status_code}): {r.text[:200]}")
        data = r.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 7200))
        return self._token

    def search(self, query: str) -> List[Tuple[str, float]]:
        filters = ["buyingOptions:{FIXED_PRICE}", "priceCurrency:USD"]
        cond = self.cfg.get("condition", "used")
        if cond == "used":
            filters.append("conditions:{USED}")
        elif cond == "new":
            filters.append("conditions:{NEW}")
        params = {"q": query, "limit": str(min(int(self.cfg.get("max_results", 50)), 200)),
                  "filter": ",".join(filters)}
        headers = {"Authorization": f"Bearer {self._get_token()}",
                   "X-EBAY-C-MARKETPLACE-ID": self.cfg.get("marketplace_id", "EBAY_US")}
        r = self.session.get(BROWSE_URL, params=params, headers=headers, timeout=30)
        if r.status_code == 401:
            self._token = None
            raise ProviderError("eBay token rejected")
        if r.status_code != 200:
            raise ProviderError(f"eBay Browse API {r.status_code}: {r.text[:200]}")
        out = []
        for it in r.json().get("itemSummaries", []) or []:
            try:
                out.append((it.get("title", ""), float(it["price"]["value"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def adjust(self, price: float) -> float:
        return price * float(self.cfg.get("price_adjustment_factor", 0.85))


class SoldScrapeProvider:
    """Reads eBay's public 'sold items' results page. Stops (does not retry/bypass) when eBay
    serves a bot challenge, and backs off for an hour."""
    name = "sold_scrape"
    BLOCK_MARKERS = ("pardon our interruption", "splashui/challenge", "checking your browser",
                     "captcha", "access denied")

    def __init__(self, cfg: Dict[str, Any], session: requests.Session):
        self.cfg = cfg
        self.session = session
        self._blocked_until = 0.0

    def available(self) -> bool:
        return time.time() >= self._blocked_until

    def search(self, query: str) -> List[Tuple[str, float]]:
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("beautifulsoup4 not installed") from exc
        url = sold_search_url(query, self.cfg.get("condition", "any"))
        r = self.session.get(url, timeout=30, headers={"Accept": "text/html,application/xhtml+xml",
                                                       "Accept-Language": "en-US,en;q=0.9"})
        body_start = r.text[:5000].lower()
        if r.status_code != 200 or any(m in body_start for m in self.BLOCK_MARKERS):
            self._blocked_until = time.time() + 3600
            raise ProviderError(f"eBay sold search unavailable (HTTP {r.status_code}); pausing 1h")
        soup = BeautifulSoup(r.text, "html.parser")
        out: List[Tuple[str, float]] = []
        for li in soup.select("li.s-item, li.s-card"):
            title_el = li.select_one(".s-item__title, .s-card__title")
            price_el = li.select_one(".s-item__price, .s-card__price")
            if not title_el or not price_el:
                continue
            title = title_el.get_text(" ", strip=True).replace("New Listing", "").strip()
            if not title or title.lower().startswith("shop on ebay"):
                continue
            price = _parse_price(price_el.get_text(" ", strip=True))
            if price:
                out.append((title, price))
        return out[: int(self.cfg.get("max_results", 50))]

    def adjust(self, price: float) -> float:
        return price


# ---------------------------------------------------------------------- facade
class EbayPricer:
    def __init__(self, ebay_cfg: Dict[str, Any], db: Optional[Database] = None,
                 session: Optional[requests.Session] = None):
        self.cfg = ebay_cfg
        self.db = db
        self.session = session or make_session()
        self.delay = float(ebay_cfg.get("request_delay_seconds", 1.0))
        self._last = 0.0
        providers = {"browse_api": BrowseApiProvider, "sold_scrape": SoldScrapeProvider}
        order = [ebay_cfg["provider"]]
        if ebay_cfg.get("fallback_provider") and ebay_cfg["fallback_provider"] not in order:
            order.append(ebay_cfg["fallback_provider"])
        self.providers = [providers[n](ebay_cfg, self.session) for n in order]
        if ebay_cfg["provider"] == "browse_api" and not self.providers[0].available():
            log.warning("eBay Browse API keys missing - using fallback provider only")

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def estimate(self, title: str, query: Optional[str] = None, use_cache: bool = True) -> Optional[Dict[str, Any]]:
        query = query or build_query(title, int(self.cfg.get("query_max_words", 7)))
        if not query:
            return None
        url = sold_search_url(query, self.cfg.get("condition", "any"))
        if use_cache and self.db:
            cached = self.db.get_cache(query, float(self.cfg.get("cache_hours", 12)))
            if cached:
                if cached["median"] is None:
                    return None
                return {"query": query, "median": cached["median"], "low": cached["low"],
                        "high": cached["high"], "count": cached["count"], "provider": cached["provider"],
                        "url": url}

        bad = self.cfg.get("exclude_comp_keywords", [])
        min_match = float(self.cfg.get("min_title_match", 0.6))
        for provider in self.providers:
            if not provider.available():
                continue
            try:
                self._throttle()
                comps = provider.search(query)
            except (ProviderError, requests.RequestException, ValueError) as exc:
                log.warning("eBay provider %s failed for %r: %s", provider.name, query, exc)
                continue
            prices = [provider.adjust(p) for t, p in comps
                      if title_match(query, t) >= min_match and not any(b in t.lower() for b in bad)]
            stats = summarize(prices)
            if self.db:
                self.db.put_cache(query, provider.name, stats[0] if stats else None,
                                  stats[1] if stats else None, stats[2] if stats else None,
                                  stats[3] if stats else 0, url)
            log.debug("eBay %s %r: %d raw, %d matching -> %s", provider.name, query, len(comps),
                      len(prices), stats)
            if not stats:
                return None
            return {"query": query, "median": round(stats[0], 2), "low": round(stats[1], 2),
                    "high": round(stats[2], 2), "count": stats[3], "provider": provider.name, "url": url}
        return None
