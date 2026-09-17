from __future__ import annotations

import base64
from datetime import datetime, timezone
from urllib.parse import unquote

import lifeos.integrations.gmail as gmail_module
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
