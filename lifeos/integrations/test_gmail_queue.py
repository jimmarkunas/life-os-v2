from __future__ import annotations

import base64
from datetime import datetime, timezone
from urllib.parse import unquote

from lifeos.core.runtime import RunContext
from lifeos.integrations.gmail import GmailMailboxTransport


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.created_processed = False

    def request_json(self, context, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET" and url.endswith("/labels"):
            labels = [{"id": "label-news", "name": "J Newsletters"}]
            if self.created_processed:
                labels.append({"id": "label-processed", "name": "J Newsletters/Processed"})
            return {"labels": labels}
        if method == "POST" and url.endswith("/labels"):
            assert kwargs["json_body"]["name"] == "J Newsletters/Processed"
            self.created_processed = True
            return {"id": "label-processed", "name": "J Newsletters/Processed"}
        if method == "GET" and "/messages?" in url:
            decoded = unquote(url)
            assert "labelIds=label-news" in decoded
            if self.created_processed:
                assert '-label:"J+Newsletters/Processed"' in decoded or '-label:"J Newsletters/Processed"' in decoded
            return {"messages": [{"id": "msg-1"}]}
        if method == "GET" and "/messages/msg-1?format=full" in url:
            body = base64.urlsafe_b64encode(b"synthetic job alert").decode("ascii").rstrip("=")
            return {
                "id": "msg-1",
                "internalDate": "1789574400000",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": "alerts@example.invalid"},
                        {"name": "Subject", "value": "Synthetic"},
                    ],
                    "body": {"data": body},
                },
            }
        if method == "POST" and url.endswith("/messages/msg-1/modify"):
            return {"id": "msg-1"}
        raise AssertionError((method, url, kwargs))


def _mailbox(http: FakeHttp) -> GmailMailboxTransport:
    return GmailMailboxTransport(
        context=RunContext.start(timeout_seconds=45),
        http=http,
        access_token="synthetic-token",
        message_factory=lambda **kwargs: kwargs,
    )


def test_staging_and_processed_state_are_distinct() -> None:
    http = FakeHttp()
    mailbox = _mailbox(http)
    mailbox.route_to_newsletters("msg-1", "J Newsletters")
    route_call = next(call for call in http.calls if call[0] == "POST" and call[1].endswith("/messages/msg-1/modify"))
    assert route_call[2]["json_body"] == {"addLabelIds": ["label-news"], "removeLabelIds": ["INBOX"]}

    mailbox.mark_newsletter_processed("msg-1", "J Newsletters")
    modify_calls = [call for call in http.calls if call[0] == "POST" and call[1].endswith("/messages/msg-1/modify")]
    assert modify_calls[-1][2]["json_body"] == {"addLabelIds": ["label-processed"], "removeLabelIds": ["UNREAD"]}


def test_unprocessed_queue_excludes_processed_and_retries_within_retention_window() -> None:
    http = FakeHttp()
    http.created_processed = True
    mailbox = _mailbox(http)
    start = datetime(2026, 9, 15, tzinfo=timezone.utc)
    end = datetime(2026, 9, 16, tzinfo=timezone.utc)
    messages = mailbox.fetch_unprocessed(start, end, "J Newsletters")
    assert [message.message_id for message in messages] == ["msg-1"]
    list_call = next(call for call in http.calls if call[0] == "GET" and "/messages?" in call[1])
    decoded = unquote(list_call[1])
    assert f"after:{int(end.timestamp() - 60 * 24 * 3600)}" in decoded
    assert '-label:"J+Newsletters/Processed"' in decoded or '-label:"J Newsletters/Processed"' in decoded
