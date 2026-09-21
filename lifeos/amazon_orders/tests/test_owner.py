from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from lifeos.amazon_orders import _event, _props, _row, run

AMAZON = "amazon" + ".com"
ORDER_ID = "123-4567890-1234567"


def message(sender=None, subject="Ordered: 1 Garden item", body=None, at=1):
    sender = sender or "Amazon <auto-confirm@" + AMAZON + ">"
    body = body or f"Order {ORDER_ID}\nGrand Total: $19.99"
    return SimpleNamespace(sender=sender, subject=subject, body_text=body, message_id=f"m{at}", received_at=datetime(2026, 1, at, tzinfo=timezone.utc))


def census(message_id="m1", provider="gmail", safe=True):
    return SimpleNamespace(checkpoint_safe=safe, records=(SimpleNamespace(ref=SimpleNamespace(provider=provider, message_id=message_id)),))


def page(row):
    props = _props(row)
    return {"id": "page-1", "properties": {name: {"type": kind, kind: value} for name, wire in props.items() for kind, value in wire.items()}}


class GmailFake:
    def __init__(self, calls, labels=None): self.calls, self.labels = calls, labels or {"Amazon"}
    def fetch_message_metadata(self, message_id): self.calls.append(("metadata", message_id)); return message()
    def fetch_message(self, message_id): self.calls.append(("full", message_id)); return message()
    def apply_amazon(self, message_id): self.calls.append(("apply", message_id))
    def read_labels(self, message_id): self.calls.append(("labels", message_id)); return self.labels


class NotionFake:
    def __init__(self, calls, rows=(), readback=None): self.calls, self.rows, self.readback = calls, tuple(rows), readback
    def query_data_source(self, data_source_id, identity): self.calls.append(("query", data_source_id, identity)); return self.rows
    def create_page(self, data_source_id, properties): self.calls.append(("create", data_source_id, properties)); return {"id": "page-1"}
    def update_page(self, page_id, properties): self.calls.append(("update", page_id, properties)); return {"id": page_id}
    def get_page(self, page_id): self.calls.append(("readback", page_id)); return self.readback


class AmazonOwnerTests(unittest.TestCase):
    def test_census_gate_has_no_side_effects(self):
        calls = []
        result = run(census(safe=False), GmailFake(calls), NotionFake(calls), data_source_id="synthetic")
        self.assertEqual(result.code, "mail-census-not-accepted"); self.assertEqual(calls, [])

    def test_source_lifecycle_and_privacy(self):
        ordered = _event(message()); shipped = _event(message("shipment-tracking@" + AMAZON, "Shipped: 1 Garden item", at=2)); delivered = _event(message("order-update@" + AMAZON, "Delivered: 1 Garden item", at=3))
        with self.subTest("valid progression"):
            for events, status in (([ordered, shipped], "SHIPPED"), ([ordered, shipped, delivered], "DELIVERED")):
                row = _row(events); self.assertEqual(row["Status"], status); self.assertFalse(row["Needs Review"])
        with self.subTest("chronological regression"):
            row = _row([shipped, _event(message(at=3))]); self.assertEqual(row["Status"], "REVIEW"); self.assertTrue(row["Needs Review"])
        with self.subTest("conflicting duplicate event identity"):
            conflicting = message(); conflicting.subject = "Ordered: 9 Other item"
            with self.assertRaises(ValueError): _row([ordered, _event(conflicting)])
        with self.subTest("noncanonical URL and privacy"):
            noncanonical = _event(message(body=f"Order {ORDER_ID} https://other.invalid/orderID={ORDER_ID}")); self.assertIsNone(noncanonical.url); self.assertNotIn(ORDER_ID, ordered.item_summary)
            with self.assertRaises(ValueError): _event(message(body="no trustworthy order id"))
        with self.subTest("canonical URL conflict"):
            existing = {"Amazon Order URL": f"https://www.amazon.com/your-orders/order-details?orderID={ORDER_ID}", "Status": "ORDERED"}
            conflicting = ordered.__class__(ordered.message_id, ordered.sender, ordered.subject, ordered.order_id, ordered.status, ordered.event_at, ordered.total, ordered.item_summary, ordered.item_count, "https://www.amazon.com/your-orders/order-details?orderID=999-9999999-9999999")
            row = _row([conflicting], existing); self.assertEqual(row["Status"], "REVIEW"); self.assertTrue(row["Needs Review"])

    def test_notion_number_and_idempotence_shape(self):
        desired = _row([_event(message())]); canonical = page(desired)
        with self.subTest("exact identity and zero-write replay"):
            calls = []; notion = NotionFake(calls, rows=(canonical,), readback=canonical); result = run(census(), GmailFake(calls), notion, data_source_id="synthetic")
            query = next(call for call in calls if call[0] == "query"); self.assertEqual((query[2].property_name, query[2].property_type, query[2].values), ("Order ID", "title", (ORDER_ID,))); self.assertEqual(result.status.value, "PASS"); self.assertFalse(any(call[0] in {"create", "update"} for call in calls))
        with self.subTest("duplicate rows"):
            calls = []; notion = NotionFake(calls, rows=(canonical, canonical)); self.assertEqual(run(census(), GmailFake(calls), notion, data_source_id="synthetic").status.value, "DEGRADED"); self.assertFalse(any(call[0] in {"create", "update", "apply"} for call in calls))
        with self.subTest("authoritative read-back"):
            calls = []; notion = NotionFake(calls, readback={"id": "page-1", "properties": {}}); self.assertEqual(run(census(), GmailFake(calls), notion, data_source_id="synthetic").status.value, "DEGRADED"); self.assertTrue(any(call[0] == "readback" for call in calls)); self.assertFalse(any(call[0] == "apply" for call in calls))
        self.assertEqual(_props(desired)["Grand Total"]["number"], 19.99)

    def test_gmail_mutation_is_after_persistence(self):
        desired = _row([_event(message())]); canonical = page(desired)
        with self.subTest("successful persistence and routing"):
            calls = []; notion = NotionFake(calls, readback=canonical); gmail = GmailFake(calls); result = run(census(), gmail, notion, data_source_id="synthetic")
            self.assertEqual(result.status.value, "PASS"); self.assertEqual([call[0] for call in calls], ["metadata", "full", "query", "create", "readback", "apply", "labels"]); self.assertIn("Amazon", gmail.labels); self.assertNotIn("INBOX", gmail.labels); self.assertNotIn("TRASH", gmail.labels)
        with self.subTest("Gmail read-back failure"):
            calls = []; notion = NotionFake(calls, readback=canonical); gmail = GmailFake(calls, labels={"INBOX"}); self.assertEqual(run(census(), gmail, notion, data_source_id="synthetic").status.value, "DEGRADED")
