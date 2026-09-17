from __future__ import annotations

import base64
from datetime import datetime, timezone
from urllib.parse import unquote

import lifeos.integrations.gmail as gmail_module
from lifeos.core.backlog import consume_bounded_backlog
from lifeos.core.runtime import RunContext
from lifeos.integrations.gmail import BACKLOG_BATCH_SIZE, GmailMailboxTransport


class BoundedBacklogHttp:
    def __init__(self) -> None:
        self.detail_ids: list[str] = []
        self.list_calls = 0

    def request_json(self, context, method, url, **kwargs):
        if method == "GET" and url.endswith("/labels"):
            return {
                "labels": [
                    {"id": "label-news", "name": "J Newsletters"},
                    {"id": "label-processed", "name": "J Newsletters/Processed"},
                ]
            }
        if method == "GET" and "/messages?" in url:
            decoded = unquote(url)
            assert "labelIds=label-news" in decoded
            assert "after:" not in decoded
            assert "before:" not in decoded
            self.list_calls += 1
            # Gmail list order is newest-first. Use more than one bounded batch
            # so the proof catches accidental whole-backlog hydration.
            return {"messages": [{"id": f"msg-{index:02d}"} for index in range(25, 0, -1)]}
        if method == "GET" and "?format=full" in url:
            message_id = url.split("/messages/", 1)[1].split("?", 1)[0]
            self.detail_ids.append(message_id)
            index = int(message_id.split("-")[1])
            body = base64.urlsafe_b64encode(f"body {message_id}".encode()).decode().rstrip("=")
            return {
                "id": message_id,
                "internalDate": str((1_700_000_000 + index) * 1000),
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": "alerts@example.invalid"},
                        {"name": "Subject", "value": message_id},
                    ],
                    "body": {"data": body},
                },
            }
        raise AssertionError((method, url, kwargs))


def test_fetch_unprocessed_enumerates_complete_backlog_but_hydrates_only_oldest_batch(monkeypatch) -> None:
    monkeypatch.setattr(gmail_module, "sleep", lambda _seconds: None)
    http = BoundedBacklogHttp()
    mailbox = GmailMailboxTransport(
        context=RunContext.start(timeout_seconds=45),
        http=http,
        access_token="synthetic-token",
        message_factory=lambda **kwargs: kwargs,
        max_workers=8,
    )
    start = datetime(2026, 9, 15, tzinfo=timezone.utc)
    end = datetime(2026, 9, 16, tzinfo=timezone.utc)

    messages = mailbox.fetch_unprocessed(start, end, "J Newsletters")

    assert http.list_calls == 1
    assert len(messages) == BACKLOG_BATCH_SIZE
    assert http.detail_ids == [f"msg-{index:02d}" for index in range(1, BACKLOG_BATCH_SIZE + 1)]
    assert [message.message_id for message in messages] == [
        f"msg-{index:02d}" for index in range(1, BACKLOG_BATCH_SIZE + 1)
    ]


class ABCDEBacklogHttp:
    """Canonical source A B C D E, oldest-first once reversed, with the same
    label-minus-processed-label shape real Gmail uses. No platform-owned
    cursor: subsequent list calls simply reflect which messages currently
    carry the processed label."""

    def __init__(self) -> None:
        self.detail_ids: list[str] = []
        self.list_calls = 0
        self.processed: set[str] = set()
        self._newest_first_order = ["E", "D", "C", "B", "A"]

    def request_json(self, context, method, url, **kwargs):
        if method == "GET" and url.endswith("/labels"):
            return {
                "labels": [
                    {"id": "label-news", "name": "J Newsletters"},
                    {"id": "label-processed", "name": "J Newsletters/Processed"},
                ]
            }
        if method == "GET" and "/messages?" in url:
            decoded = unquote(url)
            assert "labelIds=label-news" in decoded
            self.list_calls += 1
            remaining = [mid for mid in self._newest_first_order if mid not in self.processed]
            return {"messages": [{"id": mid} for mid in remaining]}
        if method == "GET" and "?format=full" in url:
            message_id = url.split("/messages/", 1)[1].split("?", 1)[0]
            self.detail_ids.append(message_id)
            index = ord(message_id) - ord("A")
            body = base64.urlsafe_b64encode(f"body {message_id}".encode()).decode().rstrip("=")
            return {
                "id": message_id,
                "internalDate": str((1_700_000_000 + index) * 1000),
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": "alerts@example.invalid"},
                        {"name": "Subject", "value": message_id},
                    ],
                    "body": {"data": body},
                },
            }
        if method == "POST" and url.endswith("/modify"):
            message_id = url.split("/messages/", 1)[1].split("/modify", 1)[0]
            payload = kwargs.get("json_body") or {}
            if "label-processed" in (payload.get("addLabelIds") or []):
                self.processed.add(message_id)
            return {"id": message_id}
        raise AssertionError((method, url, kwargs))


def test_shared_primitive_receives_complete_backlog_and_performs_bounded_selection(monkeypatch) -> None:
    """The required acceptance scenario, through the real Gmail boundary:
    canonical source A B C D E, batch size 3. Proves the shared primitive
    itself -- not Gmail pre-truncation -- performs the bounded selection,
    that only the selected batch is hydrated, that a failed item is not
    completed, and that a later call naturally resumes with no checkpoint."""
    monkeypatch.setattr(gmail_module, "sleep", lambda _seconds: None)
    http = ABCDEBacklogHttp()
    mailbox = GmailMailboxTransport(
        context=RunContext.start(timeout_seconds=45),
        http=http,
        access_token="synthetic-token",
        message_factory=lambda **kwargs: kwargs,
        max_workers=8,
    )
    boundary = "J Newsletters"

    # The real Gmail enumeration boundary returns the COMPLETE backlog,
    # unbounded, oldest-first -- not just msg 1..batch_size.
    all_ids = mailbox.enumerate_unprocessed_ids(boundary)
    assert all_ids == ("A", "B", "C", "D", "E")
    assert http.detail_ids == []  # enumeration alone hydrates nothing

    hydrated_ids: list[str] = []
    completed: list[str] = []

    def process_batch(batch):
        for message in mailbox.hydrate_messages(batch):
            hydrated_ids.append(message.message_id)
        # Domain accounting: A and C are safely accounted; B fails/degrades.
        return {"A": True, "B": False, "C": True}

    def mark_complete(message_id):
        mailbox.mark_newsletter_processed(message_id, boundary)
        completed.append(message_id)

    batch = consume_bounded_backlog(
        enumerate_backlog=lambda: mailbox.enumerate_unprocessed_ids(boundary),
        batch_size=3,
        process_batch=process_batch,
        mark_complete=mark_complete,
    )

    # The primitive itself selected exactly the leading three of the
    # complete five-item backlog -- Gmail never pre-truncated to three.
    assert batch == ("A", "B", "C")
    assert http.detail_ids == ["A", "B", "C"]  # D and E were never hydrated
    assert hydrated_ids == ["A", "B", "C"]
    # A and C were safely accounted -> completed. B failed -> not completed.
    assert completed == ["A", "C"]
    assert http.processed == {"A", "C"}

    # Natural resume: rereading canonical Gmail state (no platform
    # checkpoint) now shows B still present, plus the two items that were
    # never in the first bounded batch.
    remaining = mailbox.enumerate_unprocessed_ids(boundary)
    assert remaining == ("B", "D", "E")

    http.detail_ids.clear()

    def second_process_batch(batch):
        for message in mailbox.hydrate_messages(batch):
            hydrated_ids.append(message.message_id)
        return {message_id: True for message_id in batch}

    second_batch = consume_bounded_backlog(
        enumerate_backlog=lambda: mailbox.enumerate_unprocessed_ids(boundary),
        batch_size=3,
        process_batch=second_process_batch,
        mark_complete=mark_complete,
    )

    assert second_batch == ("B", "D", "E")
    assert http.detail_ids == ["B", "D", "E"]
    assert completed == ["A", "C", "B", "D", "E"]
    assert mailbox.enumerate_unprocessed_ids(boundary) == ()
