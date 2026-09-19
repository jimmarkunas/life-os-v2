from __future__ import annotations

from tests.testkit.boundaries import (
    BacklogFakeHttp,
    DetailRetryFakeHttp,
    FakeHttp,
    GoogleErrorBackend,
)

from datetime import datetime, timezone
from urllib.parse import unquote

from lifeos.integrations.mailbox import MailboxTransportError
from lifeos.integrations.gmail import GmailMailboxTransport
from lifeos.core.http import HttpClient
from lifeos.newsletter.processor import NewsletterExecutionState, NewsletterProcessor
from tests.testkit.builders import gmail_mailbox


def _mailbox(http: FakeHttp) -> GmailMailboxTransport:
    return gmail_mailbox(http)


def _mailbox_with_client(http: HttpClient) -> GmailMailboxTransport:
    return gmail_mailbox(http)


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


def test_unprocessed_queue_preserves_safe_google_error_reason() -> None:
    mailbox = _mailbox_with_client(HttpClient(backend=GoogleErrorBackend()))
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
    assert "msg-stuck:HttpError" in detail
    assert "status=403" in detail
    assert "reason=rateLimitExceeded" in detail
    assert "message=User rate limit exceeded for <redacted>" in detail
    assert "synthetic-secret-token" not in detail
    assert "https://gmail.googleapis.com" not in detail
