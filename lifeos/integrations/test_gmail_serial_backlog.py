from __future__ import annotations

import base64
import threading
import time
from datetime import datetime, timezone

from lifeos.core.runtime import RunContext
from lifeos.integrations.gmail import GmailMailboxTransport


class SerialDetailProbeHttp:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active_detail_requests = 0
        self.max_active_detail_requests = 0

    def request_json(self, context, method, url, **kwargs):
        if method == "GET" and url.endswith("/labels"):
            return {
                "labels": [
                    {"id": "label-news", "name": "J Newsletters"},
                    {"id": "label-processed", "name": "J Newsletters/Processed"},
                ]
            }
        if method == "GET" and "/messages?" in url:
            return {
                "messages": [
                    {"id": "msg-4"},
                    {"id": "msg-3"},
                    {"id": "msg-2"},
                    {"id": "msg-1"},
                ]
            }
        if method == "GET" and "?format=full" in url:
            message_id = url.rsplit("/messages/", 1)[1].split("?", 1)[0]
            with self._lock:
                self.active_detail_requests += 1
                self.max_active_detail_requests = max(
                    self.max_active_detail_requests,
                    self.active_detail_requests,
                )
            try:
                # Long enough for the previous thread-pool implementation to overlap.
                time.sleep(0.02)
                body = base64.urlsafe_b64encode(
                    f"synthetic body {message_id}".encode("utf-8")
                ).decode("ascii").rstrip("=")
                return {
                    "id": message_id,
                    "internalDate": "1789574400000",
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [
                            {"name": "From", "value": "alerts@example.invalid"},
                            {"name": "Subject", "value": f"Synthetic {message_id}"},
                        ],
                        "body": {"data": body},
                    },
                }
            finally:
                with self._lock:
                    self.active_detail_requests -= 1
        raise AssertionError((method, url, kwargs))


def test_fetch_unprocessed_hydrates_gmail_details_serially() -> None:
    http = SerialDetailProbeHttp()
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

    assert [message.message_id for message in messages] == [
        "msg-1",
        "msg-2",
        "msg-3",
        "msg-4",
    ]
    assert http.max_active_detail_requests == 1
