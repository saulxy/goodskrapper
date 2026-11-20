"""Dashboard integration and CRUD tests."""
import os
import tempfile
import unittest

from goodskrapper.config import DEFAULTS, _deep_merge
from goodskrapper.dashboard import create_app
from goodskrapper.db import Database, iso, utcnow


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        self.db = Database(self.db_path)

        c = _deep_merge(DEFAULTS, {})
        c["general"]["db_path"] = self.db_path
        c["dashboard"]["username"] = ""  # disable auth for test
        c["dashboard"]["password"] = ""
        c["dashboard"]["secret_key"] = "test-secret"
        self.cfg = c

        self.app = create_app(c)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _get_csrf(self):
        with self.client.session_transaction() as sess:
            sess["_csrf"] = "test-csrf-token"
        return "test-csrf-token"

    def test_index_empty(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"GoodSkrapper", r.data)
        self.assertIn(b"No items yet", r.data)

    def test_crud_item(self):
        csrf = self._get_csrf()

        # CREATE
        r = self.client.post("/item/new", data={
            "_csrf": csrf,
            "item_ref": "12345678",
            "fetch": "0",
            "title": "Vintage Walkman",
            "current_price": "25.00",
            "goodwill_shipping": "10.00",
            "ebay_price_manual": "80.00",
            "status": "watching",
            "notes": "Testing create",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Vintage Walkman", r.data)

        item = self.db.get_item(12345678)
        self.assertIsNotNone(item)
        self.assertEqual(item["title"], "Vintage Walkman")
        self.assertEqual(item["status"], "watching")
        self.assertEqual(item["ebay_price_manual"], 80.0)
        self.assertEqual(item["is_deal"], 1)

        # READ / EDIT VIEW
        r = self.client.get("/item/12345678")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Vintage Walkman", r.data)

        # UPDATE
        csrf = self._get_csrf()
        r = self.client.post("/item/12345678", data={
            "_csrf": csrf,
            "title": "Vintage Walkman WM-2",
            "status": "bid_placed",
            "current_price": "30.00",
            "goodwill_shipping": "12.00",
            "ebay_price_manual": "120.00",
            "notes": "Updated note",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        updated = self.db.get_item(12345678)
        self.assertEqual(updated["title"], "Vintage Walkman WM-2")
        self.assertEqual(updated["status"], "bid_placed")
        self.assertEqual(updated["notes"], "Updated note")

        # DELETE
        csrf = self._get_csrf()
        r = self.client.post("/item/12345678/delete", data={"_csrf": csrf}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self.db.get_item(12345678))

    def test_bulk_actions(self):
        csrf = self._get_csrf()
        self.db.insert_item({"item_id": 101, "title": "Item 1", "status": "new"})
        self.db.insert_item({"item_id": 102, "title": "Item 2", "status": "new"})

        r = self.client.post("/bulk", data={
            "_csrf": csrf,
            "ids": ["101", "102"],
            "action": "status:watching",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db.get_item(101)["status"], "watching")
        self.assertEqual(self.db.get_item(102)["status"], "watching")

        r = self.client.post("/bulk", data={
            "_csrf": csrf,
            "ids": ["101"],
            "action": "delete",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self.db.get_item(101))
        self.assertIsNotNone(self.db.get_item(102))

    def test_settings_and_api(self):
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Active configuration", r.data)

        r = self.client.get("/api/items")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("items", data)
        self.assertIn("total", data)


if __name__ == "__main__":
    unittest.main()
