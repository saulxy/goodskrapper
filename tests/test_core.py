"""Offline tests:  python -m unittest discover tests"""
import os
import tempfile
import unittest
from datetime import timedelta

from goodskrapper.config import DEFAULTS, _deep_merge
from goodskrapper.db import Database, iso, utcnow
from goodskrapper.ebay import build_query, summarize, title_match
from goodskrapper.goodwill import GoodwillClient, parse_goodwill_time, parse_item_id
from goodskrapper.profit import compute, evaluate_row
from goodskrapper.telegram import format_digest, split_message


def cfg():
    c = _deep_merge(DEFAULTS, {})
    c["targets"] = []
    return c


class ProfitTests(unittest.TestCase):
    def test_compute_and_max_bid(self):
        p = cfg()["profit"]
        r = compute(20, 10, 100, p, 40)
        fees = 100 * 0.136 + 0.40
        self.assertAlmostEqual(r.fees, fees)
        self.assertAlmostEqual(r.total_cost, 30)
        self.assertAlmostEqual(r.profit, 100 - fees - 30)
        # At max_bid, ROI should be exactly the target
        r2 = compute(r.max_bid, 10, 100, p, 40)
        self.assertAlmostEqual(r2.profit_pct, 40, places=6)

    def test_manual_override(self):
        c = cfg()
        row = {"current_price": 10, "minimum_bid": 11, "goodwill_shipping": 5, "ebay_price": 20,
               "ebay_comps": 10, "ebay_price_manual": 200}
        out = evaluate_row(row, c, 40)
        self.assertEqual(out["is_deal"], 1)
        row["ebay_price_manual"] = None
        self.assertEqual(evaluate_row(row, c, 40)["is_deal"], 0)


class ParsingTests(unittest.TestCase):
    def test_time(self):
        dt = parse_goodwill_time("2026-11-20T13:00:13.84")
        self.assertEqual(dt.hour, 21)  # 13:00 PST == 21:00 UTC

    def test_item_id(self):
        self.assertEqual(parse_item_id("https://shopgoodwill.com/item/284242097"), 284242097)
        self.assertEqual(parse_item_id("284242097"), 284242097)
        self.assertIsNone(parse_item_id("hello"))

    def test_normalize(self):
        raw = {"itemId": 1, "title": "X", "currentPrice": 5, "minimumBid": 6, "endTime": "2026-11-20T13:00:13.84",
               "imageURL": "https://a\\b\\c.jpg", "numBids": 2, "buyNowPrice": 0, "listingType": 0}
        n = GoodwillClient.normalize(raw)
        self.assertEqual(n["image_url"], "https://a/b/c.jpg")
        self.assertEqual(n["listing_type"], "auction")

    def test_query(self):
        q = build_query("Nintendo Switch Console Bundle with Dock, Case and Games - Untested", 7)
        self.assertEqual(q, "nintendo switch console dock case games")
        self.assertEqual(title_match("nintendo switch", "Nintendo Switch OLED"), 1.0)

    def test_summarize(self):
        med, lo, hi, n = summarize([10, 11, 12, 13, 14, 500])
        self.assertEqual(n, 5)
        self.assertEqual(med, 12)


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "t.db"))

    def test_crud_and_retention(self):
        end = iso(utcnow() + timedelta(hours=3))
        self.db.insert_item({"item_id": 1, "title": "A", "end_time": end, "is_deal": 1})
        self.db.insert_item({"item_id": 2, "title": "B", "end_time": end, "is_deal": 1, "status": "watching",
                             "first_seen": iso(utcnow() - timedelta(days=30))})
        self.db.insert_item({"item_id": 3, "title": "C", "first_seen": iso(utcnow() - timedelta(days=30))})
        self.assertEqual(len(self.db.items_ending_within(8, deals_only=True)), 2)
        self.db.update_item(1, {"notes": "hi", "status": "ignored"})
        self.assertEqual(self.db.get_item(1)["notes"], "hi")
        # upsert must not overwrite user fields
        self.db.upsert_market({"item_id": 1, "title": "A2", "status": "new", "notes": ""})
        it = self.db.get_item(1)
        self.assertEqual((it["title"], it["status"], it["notes"]), ("A2", "ignored", "hi"))
        res = self.db.cleanup(14, ["watching"], 12)
        self.assertEqual(res["items"], 1)  # item 3 removed, item 2 kept (watching)
        self.assertIsNotNone(self.db.get_item(2))
        self.assertEqual(self.db.delete_items([1, 2]), 2)


class TelegramTests(unittest.TestCase):
    def test_digest_split(self):
        item = {"title": "Thing <b>", "url": "https://x", "buy_price": 5, "goodwill_shipping": 9,
                "ebay_price": 50, "ebay_comps": 4, "est_profit": 20, "profit_pct": 100, "max_bid": 15,
                "end_time": iso(utcnow() + timedelta(hours=2))}
        text = format_digest([item] * 60, "America/New_York", 60)
        self.assertIn("&lt;b&gt;", text)
        chunks = split_message(text)
        self.assertTrue(all(len(c) <= 4000 for c in chunks))
        self.assertGreater(len(chunks), 1)


if __name__ == "__main__":
    unittest.main()
