from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest

from lifeos.core.http import HttpClient, HttpResponse
from lifeos.core.runtime import RunContext
from lifeos.integrations.gmail import GmailMailboxTransport
from lifeos.integrations.mailbox import MailboxTransportError


class GmailRateLimitBackend:
    def __init__(self, *, failures_before_success: int | None) -> None:
        self.failures_before_success = failures_before_success
        self.detail_attempts = 0
        self.secret = "".join(("synthetic", "-credential"))

    def request(self, method, url, *, headers, body, timeout_seconds):
        if method == "GET" and url.endswith("/labels"):
            return _response(
                {
                    "labels": [
                        {"id": "label-news", "name": "J Newsletters"},
                        {"id": "label-processed", "name": "J Newsletters/Processed"},
                    ]
                }
            )
        if method == "GET" and "/messages?" in url:
            return _response({"messages": [{"id": "msg-rate"}]})
        if method == "GET" and "/messages/msg-rate?format=full" in url:
            self.detail_attempts += 1
            if self.failures_before_success is None or self.detail_attempts <= self.failures_before_success:
                return _response(
                    {
                        "error": {
                            "code": 403,
                            "message": (
                                "User rate limit exceeded for https://gmail.googleapis.com "
                                f"Authorization: Bearer {self.secret}"
                            ),
                            "errors": [
                                {
                                    "domain": "usageLimits",
                                    "reason": "rateLimitExceeded",
                                    "message": "User rate limit exceeded",
                                }
                            ],
                            "status": "PERMISSION_DENIED",
                        }
                    },
                    status=403,
                )
            encoded = base64.urlsafe_b64encode(b"synthetic job alert").decode("ascii").rstrip("=")
            return _response(
                {
                    "id": "msg-rate",
                    "internalDate": "1789574400000",
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [
                            {"name": "From", "value": "alerts@example.invalid"},
                            {"name": "Subject", "value": "Synthetic"},
                        ],
                        "body": {"data": encoded},
                    },
                }
            )
        raise AssertionError((method, url))


def _response(payload: dict, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status, {}, json.dumps(payload).encode("utf-8"))


def _mailbox(backend: GmailRateLimitBackend) -> GmailMailboxTransport:
    credential = "".join(("synthetic", "-credential"))
    return GmailMailboxTransport(
        context=RunContext.start(timeout_seconds=45),
        http=HttpClient(backend=backend),
        access_token=credential,
        message_factory=lambda **kwargs: kwargs,
    )


def _window() -> tuple[datetime, datetime]:
    return (
        datetime(2026, 9, 15, tzinfo=timezone.utc),
        datetime(2026, 9, 16, tzinfo=timezone.utc),
    )


def test_rate_limit_exceeded_retries_with_existing_policy_then_succeeds() -> None:
    backend = GmailRateLimitBackend(failures_before_success=1)
    mailbox = _mailbox(backend)
    start, end = _window()

    messages = mailbox.fetch_unprocessed(start, end, "J Newsletters")

    assert [message.message_id for message in messages] == ["msg-rate"]
    assert backend.detail_attempts == 2


def test_rate_limit_exhaustion_preserves_safe_error_and_remains_bounded() -> None:
    backend = GmailRateLimitBackend(failures_before_success=None)
    mailbox = _mailbox(backend)
    start, end = _window()

    with pytest.raises(MailboxTransportError) as caught:
        mailbox.fetch_unprocessed(start, end, "J Newsletters")

    detail = str(caught.value)
    assert backend.detail_attempts == 4
    assert "mailbox=gmail" in detail
    assert "operation=fetch_unprocessed" in detail
    assert "msg-rate:HttpError" in detail
    assert "rate_limit" in detail
    assert "status=403" in detail
    assert "reason=rateLimitExceeded" in detail
    assert "attempts=2" in detail
    assert backend.secret not in detail
    assert "https://gmail.googleapis.com" not in detail
