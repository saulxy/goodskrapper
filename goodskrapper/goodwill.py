"""ShopGoodwill client using the public JSON API used by shopgoodwill.com (no browser needed)."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

log = logging.getLogger(__name__)

API = "https://buyerapi.shopgoodwill.com/api"
ITEM_URL = "https://shopgoodwill.com/item/{}"
GOODWILL_TZ = ZoneInfo("America/Los_Angeles")  # ShopGoodwill timestamps are Pacific time
USER_AGENT = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
PAGE_SIZE = 40


def make_session(user_agent: str = USER_AGENT) -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "POST"]))
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=4))
    s.headers.update({
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://shopgoodwill.com",
        "Referer": "https://shopgoodwill.com/",
    })
    return s


def parse_goodwill_time(value: Optional[str]) -> Optional[datetime]:
    """'2026-11-20T13:00:13.84' (Pacific, naive) -> aware UTC datetime."""
    if not value:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?", str(value))
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    return dt.replace(tzinfo=GOODWILL_TZ).astimezone(timezone.utc)


def parse_item_id(text: str) -> Optional[int]:
    """Accept a numeric ID or a shopgoodwill.com item URL."""
    text = (text or "").strip()
    m = re.search(r"/item/(\d+)", text) or re.fullmatch(r"(\d{5,})", text)
    return int(m.group(1)) if m else None


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _image(url: Optional[str]) -> str:
    return (url or "").replace("\\", "/")


class GoodwillClient:
    def __init__(self, scraper_cfg: Dict[str, Any], session: Optional[requests.Session] = None):
        self.cfg = scraper_cfg
        self.session = session or make_session()
        self.delay = float(scraper_cfg.get("request_delay_seconds", 2.0))
        self._last = 0.0

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _post(self, path: str, payload: Dict[str, Any]) -> Any:
        self._throttle()
        r = self.session.post(f"{API}{path}", json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def _get(self, path: str) -> Any:
        self._throttle()
        r = self.session.get(f"{API}{path}", timeout=30)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ search
    def _payload(self, keyword: str, category_ids: List[int], page: int,
                 low: float, high: float) -> Dict[str, Any]:
        cats = ",".join(str(c) for c in category_ids)
        return {
            "isSize": False, "isWeddingCatagory": "false",
            "isMultipleCategoryIds": len(category_ids) > 1,
            "isFromHeaderMenuTab": False, "layout": "", "isFromHomePage": False,
            "searchText": keyword.replace('"', ""),  # quotes make the API return 403
            "selectedGroup": "", "selectedCategoryIds": cats, "selectedSellerIds": "",
            "lowPrice": str(int(low)), "highPrice": str(int(high)),
            "searchBuyNowOnly": "", "searchPickupOnly": "false",
            "searchNoPickupOnly": "true" if self.cfg.get("exclude_pickup_only", True) else "false",
            "searchOneCentShippingOnly": "false", "searchDescriptions": "false",
            "searchClosedAuctions": "false", "closedAuctionEndingDate": "1/1/2024",
            "closedAuctionDaysBack": "7", "searchCanadaShipping": "false",
            "searchInternationalShippingOnly": "false",
            "sortColumn": "1", "sortDescending": "false",  # ending soonest first
            "page": str(page), "pageSize": str(PAGE_SIZE),
            "savedSearchId": 0, "useBuyerPrefs": "true", "searchUSOnlyShipping": "true",
            "categoryLevelNo": "1", "categoryLevel": 1, "categoryId": 0,
            "partNumber": "", "catIds": "",
        }

    @staticmethod
    def normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
        end = parse_goodwill_time(raw.get("endTime"))
        buy_now = _f(raw.get("buyNowPrice"))
        is_auction = raw.get("listingType", 0) == 0 and not (buy_now > 0 and raw.get("isStock"))
        return {
            "item_id": int(raw["itemId"]),
            "title": (raw.get("title") or "").strip(),
            "url": ITEM_URL.format(raw["itemId"]),
            "image_url": _image(raw.get("imageURL")),
            "category_id": raw.get("categoryId"),
            "category_name": raw.get("catFullName") or raw.get("categoryName") or "",
            "listing_type": "auction" if is_auction else "buy_now",
            "current_price": _f(raw.get("currentPrice")),
            "minimum_bid": _f(raw.get("minimumBid")),
            "buy_now_price": buy_now,
            "num_bids": int(raw.get("numBids") or 0),
            "end_time": end.isoformat(timespec="seconds") if end else None,
            "listing_shipping": _f(raw.get("shippingPrice")),
        }

    def search(self, keyword: str = "", category_ids: Optional[List[int]] = None,
               min_price: float = 0, max_price: float = 999999,
               max_pages: int = 3) -> Iterator[Dict[str, Any]]:
        category_ids = category_ids or []
        seen = set()
        for page in range(1, int(max_pages) + 1):
            try:
                data = self._post("/Search/ItemListing",
                                  self._payload(keyword, category_ids, page, min_price, max_price))
            except (requests.RequestException, ValueError) as exc:
                log.warning("Goodwill search failed (kw=%r cats=%s page=%s): %s",
                            keyword, category_ids, page, exc)
                return
            results = (data or {}).get("searchResults") or {}
            items = results.get("items") or []
            total = int(results.get("itemCount") or 0)
            log.debug("Goodwill kw=%r cats=%s page=%d -> %d items (total %d)",
                      keyword, category_ids, page, len(items), total)
            for raw in items:
                try:
                    item = self.normalize(raw)
                except (KeyError, TypeError, ValueError):
                    continue
                if item["item_id"] in seen:
                    continue
                seen.add(item["item_id"])
                yield item
            if len(items) < PAGE_SIZE or page * PAGE_SIZE >= total:
                return

    # ------------------------------------------------------------------ details
    def item_detail(self, item_id: int) -> Optional[Dict[str, Any]]:
        try:
            d = self._get(f"/ItemDetail/GetItemDetailModelByItemId/{int(item_id)}")
        except (requests.RequestException, ValueError) as exc:
            log.warning("Goodwill detail %s failed: %s", item_id, exc)
            return None
        if not d or not d.get("itemId"):
            return None
        images = (d.get("imageUrlString") or "").split(";")
        server = d.get("imageServer") or ""
        image = _image(server + images[0]) if images and images[0] else ""
        end = parse_goodwill_time(d.get("endTime"))
        cat = " > ".join(filter(None, [d.get("category"), d.get("subcategory")]))
        return {
            "item_id": int(d["itemId"]),
            "title": (d.get("title") or "").strip(),
            "url": ITEM_URL.format(d["itemId"]),
            "image_url": image,
            "category_id": d.get("categoryId"),
            "category_name": cat,
            "listing_type": "auction" if d.get("isAuction", True) else "buy_now",
            "current_price": _f(d.get("currentPrice")),
            "minimum_bid": _f(d.get("minimumBid")),
            "buy_now_price": _f(d.get("buyNowPrice")),
            "num_bids": int(d.get("numberOfBids") or 0),
            "end_time": end.isoformat(timespec="seconds") if end else None,
            "pickup_only": bool(d.get("pickupOnly")),
            "ended": bool(d.get("isItemEndTimeExpire")),
        }

    def shipping_quote(self, item_id: int, zip_code: Optional[str] = None) -> Optional[float]:
        """Total shipping + handling for the configured ZIP code, or None."""
        payload = {"itemId": int(item_id), "zipCode": str(zip_code or self.cfg.get("zip_code", "10001")),
                   "country": "US", "province": None, "quantity": 1, "clientIP": ""}
        try:
            text = self._post("/ItemDetail/CalculateShipping", payload)
        except (requests.RequestException, ValueError) as exc:
            log.debug("Shipping quote %s failed: %s", item_id, exc)
            return None
        m = re.search(r"Total Shipping and Handling:\s*\$([\d,]+\.?\d*)", str(text))
        if not m:
            return None
        return float(m.group(1).replace(",", ""))

    # ------------------------------------------------------------------ categories
    def categories(self) -> List[Dict[str, Any]]:
        data = self._post("/Search/ItemListing", self._payload("", [], 1, 0, 999999))
        tree = ((data or {}).get("categoryListModel") or {}).get("categoryModel") or []
        out: List[Dict[str, Any]] = []

        def walk(nodes: List[Dict[str, Any]], path: List[str]) -> None:
            for n in nodes:
                name = n.get("name") or ""
                if name.startswith("All ") and n.get("parentId") == 0:
                    continue  # "All X" alias of the parent
                full = path + [name]
                out.append({"id": n.get("categoryId"), "level": n.get("levelNumber"),
                            "name": " > ".join(full)})
                walk(n.get("children") or [], full)

        walk(tree, [])
        return out
