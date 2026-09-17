from __future__ import annotations

import base64
from datetime import datetime, timezone
from urllib.parse import unquote

from lifeos.core.runtime import RunContext
from lifeos.integrations.gmail import GmailMailboxTransport
from lifeos.integrations.mailbox import MailboxTransportError
from lifeos.newsletter.processor import NewsletterExecutionState, NewsletterProcessor


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


class BacklogFakeHttp(FakeHttp):
    def request_json(self, context, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
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
            if '-label:"J+Newsletters/Processed"' in decoded or '-label:"J Newsletters/Processed"' in decoded:
                if "pageToken=page-2" in decoded:
                    return {"messages": [{"id": "msg-new"}]}
                return {"messages": [{"id": "msg-old"}, {"id": "msg-recent"}], "nextPageToken": "page-2"}
            return {
                "messages": [
                    {"id": "msg-old"},
                    {"id": "msg-recent"},
                    {"id": "msg-new"},
                    {"id": "msg-processed"},
                ]
            }
        for message_id, internal_date in {
            "msg-old": "1609459200000",
            "msg-recent": "1789488000000",
            "msg-new": "1789574400000",
            "msg-processed": "1789574400000",
        }.items():
            if method == "GET" and f"/messages/{message_id}?format=full" in url:
                body = (
                    "[Synthetic Labs\n90%\nProgram Manager\nRemote]"
                    f"(https://jobright.ai/jobs/info/{message_id})\n"
                    "View More Opportunities"
                )
                encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii").rstrip("=")
                return {
                    "id": message_id,
                    "internalDate": internal_date,
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [
                            {"name": "From", "value": "alerts@jobright.example.invalid"},
                            {"name": "Subject", "value": "Jobright jobs"},
                        ],
                        "body": {"data": encoded},
                    },
                }
        raise AssertionError((method, url, kwargs))


class DetailRetryFakeHttp(FakeHttp):
    def __init__(self, *, fail_message_id: str, permanent: bool = False) -> None:
        super().__init__()
        self.fail_message_id = fail_message_id
        self.permanent = permanent
        self.detail_attempts: dict[str, int] = {}

    def request_json(self, context, method, url, **kwargs):
        if method == "GET" and "/messages?" in url:
            return {"messages": [{"id": "msg-ok"}, {"id": self.fail_message_id}]}
        for message_id in ("msg-ok", self.fail_message_id):
            if method == "GET" and f"/messages/{message_id}?format=full" in url:
                self.detail_attempts[message_id] = self.detail_attempts.get(message_id, 0) + 1
                if message_id == self.fail_message_id and (self.permanent or self.detail_attempts[message_id] == 1):
                    raise TimeoutError(f"synthetic detail timeout for {message_id}")
                body = base64.urlsafe_b64encode(f"body {message_id}".encode("utf-8")).decode("ascii").rstrip("=")
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
        return super().request_json(context, method, url, **kwargs)


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


def test_newsletter_processor_consumes_age_independent_unprocessed_gmail_backlog() -> None:
    http = BacklogFakeHttp()
    mailbox = _mailbox(http)
    start = datetime(2026, 9, 15, tzinfo=timezone.utc)
    end = datetime(2026, 9, 16, tzinfo=timezone.utc)

    result = NewsletterProcessor().process_window([mailbox], start, end)

    assert result.state is NewsletterExecutionState.PASS
    assert {message.message_ref for message in result.messages} == {
        "gmail:msg-old",
        "gmail:msg-recent",
        "gmail:msg-new",
    }
    assert {obs.source_message_id for obs in result.observations} == {
        "msg-old",
        "msg-recent",
        "msg-new",
    }
    assert "msg-processed" not in {obs.source_message_id for obs in result.observations}
    list_call = next(call for call in http.calls if call[0] == "GET" and "/messages?" in call[1])
    decoded = unquote(list_call[1])
    assert "after:" not in decoded
    assert "before:" not in decoded
    assert '-label:"J+Newsletters/Processed"' in decoded or '-label:"J Newsletters/Processed"' in decoded


def test_unprocessed_queue_retries_transient_detail_failure_and_returns_complete_queue() -> None:
    http = DetailRetryFakeHttp(fail_message_id="msg-flaky")
    http.created_processed = True
    mailbox = _mailbox(http)
    start = datetime(2026, 9, 15, tzinfo=timezone.utc)
    end = datetime(2026, 9, 16, tzinfo=timezone.utc)

    messages = mailbox.fetch_unprocessed(start, end, "J Newsletters")

    assert [message.message_id for message in messages] == ["msg-flaky", "msg-ok"]
    assert http.detail_attempts == {"msg-ok": 1, "msg-flaky": 2}


def test_unprocessed_queue_fails_closed_with_message_id_after_retry_failure() -> None:
    http = DetailRetryFakeHttp(fail_message_id="msg-stuck", permanent=True)
    http.created_processed = True
    mailbox = _mailbox(http)
    start = datetime(2026, 9, 15, tzinfo=timezone.utc)
    end = datetime(2026, 9, 16, tzinfo=timezone.utc)

    try:
        mailbox.fetch_unprocessed(start, end, "J Newsletters")
    except MailboxTransportError as exc:
        detail = str(exc)
    else:
        raise AssertionError("expected fetch_unprocessed to fail closed")

    assert "mailbox=gmail" in detail
    assert "operation=fetch_unprocessed" in detail
    assert "msg-stuck:TimeoutError:synthetic detail timeout for msg-stuck" in detail
    assert "msg-ok" not in detail
    assert http.detail_attempts == {"msg-ok": 1, "msg-stuck": 2}
