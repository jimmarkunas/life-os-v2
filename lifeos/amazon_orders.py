"""Deterministic Amazon Orders owner; consumes an accepted mail census."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parseaddr
from typing import Any

from lifeos.core.runtime import ExecutionResult
from lifeos.integrations.notion import NotionIdentityQuery

ORDER_RE = re.compile(r"^\d{3}-\d{7}-\d{7}$")
CANONICAL_URL = "https://www.amazon.com/your-orders/order-details?orderID={}"
SENDERS = {"auto-confirm": "ORDERED", "shipment-tracking": "SHIPPED", "order-update": "DELIVERED"}
RANK = {"ORDERED": 1, "SHIPPED": 2, "DELIVERED": 3, "REVIEW": 99}


class AmazonError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AmazonEvent:
    message_id: str
    sender: str
    subject: str
    order_id: str
    status: str
    event_at: datetime
    total: str | None
    item_summary: str
    item_count: int | None
    url: str | None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sender_status(value: Any) -> str | None:
    sender = parseaddr(_text(value))[1].lower()
    local, domain = sender.rsplit("@", 1) if "@" in sender else ("", "")
    return SENDERS.get(local) if domain == "amazon.com" else None


def _event(message: Any) -> AmazonEvent:
    sender = parseaddr(_text(getattr(message, "sender", "")))[1].lower()
    status = _sender_status(sender)
    body = _text(getattr(message, "body_text", ""))
    ids = set(re.findall(r"\b\d{3}-\d{7}-\d{7}\b", body))
    if not status or len(ids) != 1:
        raise AmazonError("unsupported or ambiguous Amazon source evidence")
    order_id = next(iter(ids))
    received = getattr(message, "received_at", None)
    if not isinstance(received, datetime) or received.tzinfo is None:
        raise AmazonError("Amazon source timestamp is incomplete")
    total_match = re.search(r"(?:Grand Total|Order Total)\s*:?\s*\$?\s*([0-9]+(?:\.[0-9]{2})?)", body, re.I)
    total = Decimal(total_match.group(1)) if total_match and status == "ORDERED" else None
    url_match = re.search(r"https?://[^\s>]+orderID=" + re.escape(order_id), body)
    url = url_match.group(0) if url_match and url_match.group(0) == CANONICAL_URL.format(order_id) else None
    subject = _text(getattr(message, "subject", ""))
    summary = re.sub(r"^\s*(?:ordered|shipped|delivered)\s*:\s*", "", subject, flags=re.I)[:200]
    count_match = re.match(r"(\d+)\s+", summary)
    return AmazonEvent(str(getattr(message, "message_id", "")), sender, subject, order_id, status, received, total, summary, int(count_match.group(1)) if count_match else None, url)


def _value(props: dict[str, Any], name: str) -> Any:
    prop = props.get(name) or {}
    kind = prop.get("type")
    value = prop.get(kind) if kind else None
    if isinstance(value, list):
        return "\n".join(_text(x.get("plain_text") or x.get("text", {}).get("content")) for x in value if isinstance(x, dict))
    if isinstance(value, dict):
        return value.get("name") or value.get("start") or value.get("number") or value.get("url") or value.get("content")
    return value


def _props(row: dict[str, Any]) -> dict[str, Any]:
    def rt(v: Any) -> dict[str, Any]: return {"rich_text": [{"text": {"content": _text(v)}}]} if _text(v) else {"rich_text": []}
    def title(v: Any) -> dict[str, Any]: return {"title": [{"text": {"content": _text(v)}}]}
    def sel(v: Any) -> dict[str, Any]: return {"select": {"name": v}}
    def date(v: Any) -> dict[str, Any]: return {"date": {"start": v}} if v else {"date": None}
    def num(v: Any) -> dict[str, Any]: return {"number": float(v) if v is not None else None}
    return {"Order ID": title(row["Order ID"]), "Status": sel(row["Status"]), "Ordered At": date(row.get("Ordered At")), "Latest Event At": date(row.get("Latest Event At")), "Grand Total": num(row.get("Grand Total")), "Item Summary": rt(row.get("Item Summary")), "Item Count": num(row.get("Item Count")), "Amazon Order URL": {"url": row.get("Amazon Order URL")}, "Source Message IDs": rt(row.get("Source Message IDs")), "Last Source Subject": rt(row.get("Last Source Subject")), "Last Reconciled At": date(row.get("Last Reconciled At")), "Needs Review": {"checkbox": bool(row.get("Needs Review"))}}


def _row(events: list[AmazonEvent], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    statuses = [e.status for e in events]
    ordered_events = sorted(events, key=lambda e: (e.event_at, e.message_id))
    status = max(statuses, key=RANK.get)
    old = _text(existing.get("Status"))
    review = old == "REVIEW" or (old in RANK and any(RANK[e.status] < RANK[old] for e in events))
    highest = 0
    for event in ordered_events:
        if RANK[event.status] < highest: review = True
        highest = max(highest, RANK[event.status])
    by_key: dict[tuple[str, str, str], AmazonEvent] = {}
    for event in events:
        key = (event.order_id, event.status, event.message_id)
        if key in by_key and by_key[key] != event: raise AmazonError("conflicting duplicate event identity")
        by_key[key] = event
    events = list(by_key.values())
    totals = {e.total for e in events if e.total}
    if existing.get("Grand Total") and totals and Decimal(str(existing["Grand Total"])) not in totals: review = True
    if len(totals) > 1: review = True
    status = max([old, status], key=RANK.get) if old in RANK and not review else status
    latest = max(events, key=lambda e: e.event_at)
    ordered = [e for e in events if e.status == "ORDERED"]
    ids = sorted(set(_text(existing.get("Source Message IDs")).splitlines()) | {e.message_id for e in events})
    existing_url = existing.get("Amazon Order URL")
    urls = {e.url for e in events if e.url}
    if existing_url: urls.add(existing_url)
    if len(urls) > 1: review = True
    if review: status = "REVIEW"
    latest_existing = _parse(existing["Latest Event At"]) if existing.get("Latest Event At") else None
    newer = latest_existing and latest_existing > latest.event_at
    safe_url = existing_url if len(urls) != 1 else next(iter(urls))
    return {"Order ID": events[0].order_id, "Status": status, "Ordered At": min((e.event_at for e in ordered), default=None).isoformat() if ordered else existing.get("Ordered At"), "Latest Event At": max(latest.event_at, latest_existing).isoformat() if latest_existing else latest.event_at.isoformat(), "Grand Total": next(iter(totals), existing.get("Grand Total")), "Item Summary": latest.item_summary or existing.get("Item Summary"), "Item Count": latest.item_count or existing.get("Item Count"), "Amazon Order URL": safe_url, "Source Message IDs": "\n".join(x for x in ids if x), "Last Source Subject": existing.get("Last Source Subject") if newer else latest.subject, "Last Reconciled At": datetime.now(timezone.utc).isoformat(), "Needs Review": review}


def _parse(value: Any) -> datetime:
    return datetime.fromisoformat(_text(value).replace("Z", "+00:00"))


def _existing(page: dict[str, Any]) -> dict[str, Any]:
    return {name: _value(page.get("properties") or {}, name) for name in ("Order ID", "Status", "Ordered At", "Latest Event At", "Grand Total", "Item Summary", "Item Count", "Amazon Order URL", "Source Message IDs", "Last Source Subject", "Needs Review")}


def _same(page: dict[str, Any], row: dict[str, Any]) -> bool:
    old = _existing(page)
    def equal(a: Any, b: Any) -> bool:
        if a in (None, "") and b in (None, ""): return True
        if (isinstance(a, (int, float, Decimal)) and not isinstance(a, bool)) or (isinstance(b, (int, float, Decimal)) and not isinstance(b, bool)):
            try: return Decimal(str(a)) == Decimal(str(b))
            except Exception: return False
        return _text(a) == _text(b)
    return all(equal(old.get(key), value) for key, value in row.items() if key != "Last Reconciled At")


def run(census: Any, gmail: Any, notion: Any, *, data_source_id: str) -> ExecutionResult[dict[str, Any]]:
    if not getattr(census, "checkpoint_safe", False):
        return ExecutionResult.degraded(code="mail-census-not-accepted")
    results: dict[str, Any] = {}
    candidates = []
    try:
        for record in census.records:
            if record.ref.provider != "gmail":
                continue
            message_id = record.ref.message_id
            meta = gmail.fetch_message_metadata(message_id)
            if _sender_status(getattr(meta, "sender", "")):
                candidates.append(gmail.fetch_message(message_id))
        grouped: dict[str, list[AmazonEvent]] = {}
        for message in candidates:
            event = _event(message); grouped.setdefault(event.order_id, []).append(event)
        for order_id, messages in grouped.items():
            found = notion.query_data_source(data_source_id, NotionIdentityQuery("Order ID", "title", (order_id,)))
            if len(found) > 1: raise AmazonError("duplicate Amazon Order rows")
            existing = _existing(found[0]) if found else None
            desired = _row(messages, existing)
            if not existing or not _same(found[0], desired):
                page = notion.update_page(found[0]["id"], _props(desired)) if found else notion.create_page(data_source_id, _props(desired))
                persisted = notion.get_page(page["id"])
            else: persisted = found[0]
            if not _same(persisted, desired): raise AmazonError("Amazon Notion read-back mismatch")
            for message in messages:
                gmail.apply_amazon(message.message_id)
                labels = gmail.read_labels(message.message_id)
                if "Amazon" not in labels or "INBOX" in labels or "TRASH" in labels: raise AmazonError("Amazon Gmail read-back mismatch")
            results[order_id] = desired
        return ExecutionResult.passed(results)
    except Exception as exc:
        return ExecutionResult.degraded(code="amazon-mail-degraded", error=type(exc).__name__)
