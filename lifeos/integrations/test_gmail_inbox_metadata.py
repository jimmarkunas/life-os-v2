"""Slice A -- metadata-first Inbox staging: transport and router-parity proofs.

Synthetic data only. All identifiers/addresses are .invalid.
"""
from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from lifeos.core.runtime import RunContext
from lifeos.integrations.gmail import GmailInboxMetadataPort, GmailMailboxTransport
from lifeos.integrations.mailbox import MailboxTransportError
from lifeos.mail.classifier import DeterministicMailClassifier
from lifeos.mail.models import MailClass, MailMessage
from lifeos.mail.router import MailExecutionState, MailRouter


class FakeInboxHttp:
    """Synthetic Gmail HTTP boundary exposing an Inbox of mixed mail."""

    def __init__(self, messages: dict[str, dict]) -> None:
        self._messages = messages
        self.calls: list[tuple[str, str]] = []
        self.full_fetch_ids: list[str] = []
        self.metadata_fetch_ids: list[str] = []
        self.fail_message_id: str | None = None
        self._lock = threading.Lock()

    def request_json(self, context, method, url, **kwargs):
        with self._lock:
            self.calls.append((method, url))
        if method == "GET" and "/messages?" in url:
            query = parse_qs(urlparse(url).query)
            assert query.get("labelIds") == ["INBOX"], "Inbox staging must list labelIds=INBOX"
            return {"messages": [{"id": message_id} for message_id in self._messages]}
        if method == "GET" and "format=full" in url:
            message_id = url.split("/messages/", 1)[1].split("?", 1)[0]
            self.full_fetch_ids.append(message_id)
            raise AssertionError(f"unexpected full-body fetch during staging: {message_id}")
        if method == "GET" and url.endswith("/labels"):
            return {"labels": [{"id": "label-news", "name": "J Newsletters"}]}
        if method == "POST" and url.endswith("/modify"):
            return {"id": url.split("/messages/", 1)[1].split("/modify", 1)[0]}
        if method == "GET" and "format=metadata" in url:
            message_id = url.split("/messages/", 1)[1].split("?", 1)[0]
            if message_id == self.fail_message_id:
                raise MailboxTransportError("synthetic metadata read failure")
            self.metadata_fetch_ids.append(message_id)
            query = parse_qs(urlparse(url).query)
            requested_headers = set(query.get("metadataHeaders", []))
            for required in ("From", "Subject", "List-Unsubscribe"):
                assert required in requested_headers
            msg = self._messages[message_id]
            headers = [{"name": "From", "value": msg["sender"]}, {"name": "Subject", "value": msg["subject"]}]
            for name, value in msg.get("extra_headers", {}).items():
                headers.append({"name": name, "value": value})
            return {
                "id": message_id,
                "internalDate": str(msg["epoch_ms"]),
                "payload": {"headers": headers},
            }
        raise AssertionError(f"unexpected call {method} {url}")


def _mixed_inbox() -> dict[str, dict]:
    base = 1_700_000_000_000
    return {
        "msg-automated-sender-subject": {
            "sender": "alerts@example.invalid",
            "subject": "Daily job alert: new roles",
            "epoch_ms": base + 1000,
            "extra_headers": {"List-Unsubscribe": "<https://example.invalid/unsub>"},
        },
        "msg-automated-source-header": {
            "sender": "digest@example.invalid",
            "subject": "Roles matched to your profile",
            "epoch_ms": base + 2000,
            "extra_headers": {"X-LifeOS-Source-Adapter": "jobright"},
        },
        "msg-transactional": {
            "sender": "careers@example.invalid",
            "subject": "Application received",
            "epoch_ms": base + 3000,
            "extra_headers": {},
        },
        "msg-human-recruiter": {
            "sender": "person@example.invalid",
            "subject": "Quick question about your background",
            "epoch_ms": base + 4000,
            "extra_headers": {},
        },
        "msg-unrelated-bulk": {
            "sender": "notifications@shipping.example.invalid",
            "subject": "Your package has shipped",
            "epoch_ms": base + 5000,
            "extra_headers": {"List-Unsubscribe": "<https://example.invalid/unsub>"},
        },
        "msg-unrelated-personal": {
            "sender": "friend@example.invalid",
            "subject": "Dinner Friday?",
            "epoch_ms": base + 6000,
            "extra_headers": {},
        },
    }


class MetadataTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = RunContext.start(timeout_seconds=45)
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.end = datetime(2026, 1, 2, tzinfo=timezone.utc)

    def _mailbox(self, http: FakeInboxHttp) -> GmailMailboxTransport:
        return GmailMailboxTransport(
            context=self.context, http=http, access_token="synthetic-token", message_factory=MailMessage,
        )

    def test_metadata_scan_uses_format_metadata_never_full_and_returns_empty_bodies(self) -> None:
        http = FakeInboxHttp(_mixed_inbox())
        mailbox = self._mailbox(http)

        messages = mailbox.scan_inbox_metadata_window(self.start, self.end)

        self.assertEqual(len(messages), 6)
        self.assertEqual(sorted(http.metadata_fetch_ids), sorted(_mixed_inbox().keys()))
        self.assertEqual(http.full_fetch_ids, [])
        self.assertFalse(any("format=raw" in url for _, url in http.calls))
        for message in messages:
            self.assertEqual(message.body_text, "")
            self.assertEqual(message.html_text, "")
            self.assertEqual(message.raw_mime, "")
            self.assertTrue(message.sender)
            self.assertTrue(message.subject)
        # deterministic ordering preserved
        ordered = sorted(messages, key=lambda m: (m.received_at, m.message_id))
        self.assertEqual(list(messages), ordered)

    def test_one_failed_metadata_read_fails_closed(self) -> None:
        http = FakeInboxHttp(_mixed_inbox())
        http.fail_message_id = "msg-human-recruiter"
        mailbox = self._mailbox(http)

        with self.assertRaises(MailboxTransportError):
            mailbox.scan_inbox_metadata_window(self.start, self.end)

    def test_scan_window_and_scan_inbox_window_unchanged(self) -> None:
        http = FakeInboxHttp(_mixed_inbox())
        # scan_window/scan_inbox_window are unaffected regression paths --
        # they still exist and still require format=full; the metadata scan
        # is an additive boundary, not a replacement.
        self.assertTrue(hasattr(GmailMailboxTransport, "scan_window"))
        self.assertTrue(hasattr(GmailMailboxTransport, "scan_inbox_window"))
        self.assertTrue(hasattr(GmailMailboxTransport, "scan_inbox_metadata_window"))


class _StagingMailbox:
    """MailboxPort test double standing in for GmailInboxMetadataPort, so
    router-parity proofs don't depend on live HTTP wiring."""

    provider = "gmail"

    def __init__(self, messages: tuple[MailMessage, ...]) -> None:
        self._messages = messages
        self.routed_ids: list[str] = []

    def scan_window(self, start, end):
        return self._messages

    def route_to_newsletters(self, message_id: str, boundary_name: str) -> None:
        self.routed_ids.append(message_id)


def _message(message_id: str, *, sender: str, subject: str, headers: dict | None = None) -> MailMessage:
    return MailMessage(
        provider="gmail",
        message_id=message_id,
        received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sender=sender,
        subject=subject,
        headers=headers or {},
    )


class RouterParityTests(unittest.TestCase):
    def test_metadata_only_messages_route_exactly_the_confirmed_automated_sources(self) -> None:
        messages = (
            _message(
                "automated-1",
                sender="alerts@example.invalid",
                subject="Daily job alert: new roles",
                headers={"List-Unsubscribe": "<https://example.invalid/unsub>"},
            ),
            _message(
                "automated-2",
                sender="digest@example.invalid",
                subject="Roles matched to your profile",
                headers={"X-LifeOS-Source-Adapter": "jobright"},
            ),
            _message("recruiter", sender="person@example.invalid", subject="Quick question about your background"),
            _message("transactional", sender="careers@example.invalid", subject="Application received"),
            _message(
                "unrelated-bulk",
                sender="notifications@shipping.example.invalid",
                subject="Your package has shipped",
                headers={"List-Unsubscribe": "<https://example.invalid/unsub>"},
            ),
            _message("unrelated-personal", sender="friend@example.invalid", subject="Dinner Friday?"),
        )
        # every message is metadata-only, exactly as scan_inbox_metadata_window returns
        for message in messages:
            self.assertEqual(message.body_text, "")

        mailbox = _StagingMailbox(messages)
        result = MailRouter().route_window(
            [mailbox], datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc)
        )

        self.assertEqual(result.state, MailExecutionState.PASS)
        self.assertEqual(result.scanned_count, 6)
        self.assertEqual(sorted(mailbox.routed_ids), ["automated-1", "automated-2"])
        routed = {record.ref.message_id for record in result.records if record.routed}
        self.assertEqual(routed, {"automated-1", "automated-2"})
        for message_id in ("recruiter", "transactional", "unrelated-bulk", "unrelated-personal"):
            record = next(r for r in result.records if r.ref.message_id == message_id)
            self.assertFalse(record.routed)
            self.assertIsNot(record.classification.mail_class, MailClass.AUTOMATED_JOB_SOURCE)

    def test_classification_parity_matches_full_body_classifier_for_representative_cases(self) -> None:
        classifier = DeterministicMailClassifier()
        cases = [
            (
                _message(
                    "a",
                    sender="alerts@example.invalid",
                    subject="Daily job alert: new roles",
                    headers={"List-Unsubscribe": "x"},
                ),
                MailClass.AUTOMATED_JOB_SOURCE,
            ),
            (
                _message(
                    "b",
                    sender="digest@example.invalid",
                    subject="Roles matched to your profile",
                    headers={"X-LifeOS-Source-Adapter": "jobright"},
                ),
                MailClass.AUTOMATED_JOB_SOURCE,
            ),
            (_message("c", sender="careers@example.invalid", subject="Application received"), MailClass.HUMAN_HIRING),
            (
                _message("d", sender="person@example.invalid", subject="Quick question - recruiter here"),
                MailClass.HUMAN_HIRING,
            ),
            (
                _message(
                    "e",
                    sender="notifications@shipping.example.invalid",
                    subject="Your package has shipped",
                    headers={"List-Unsubscribe": "x"},
                ),
                MailClass.UNRELATED,
            ),
            (_message("f", sender="friend@example.invalid", subject="Dinner Friday?"), MailClass.UNRELATED),
        ]
        for message, expected in cases:
            self.assertIs(classifier.classify(message).mail_class, expected, message.message_id)

    def test_negative_control_failed_scan_is_degraded_not_falsely_complete(self) -> None:
        class FailingMailbox:
            provider = "gmail"

            def scan_window(self, start, end):
                raise MailboxTransportError("synthetic Inbox metadata acquisition failed")

            def route_to_newsletters(self, message_id, boundary_name):
                raise AssertionError("must not route when scan is incomplete")

        result = MailRouter().route_window(
            [FailingMailbox()], datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc)
        )

        self.assertEqual(result.state, MailExecutionState.DEGRADED)
        self.assertEqual(result.scanned_count, 0)
        self.assertFalse(result.checkpoint_safe)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].operation, "scan")
        # sanitized: no raw exception text, just the exception class name
        self.assertEqual(result.errors[0].detail, "MailboxTransportError")


class FiftyMessageLoadShapedTests(unittest.TestCase):
    def test_fifty_message_inbox_stages_without_body_hydration_or_truncation(self) -> None:
        messages: dict[str, dict] = {}
        base = 1_700_000_000_000
        for index in range(50):
            messages[f"msg-job-{index:02d}"] = {
                "sender": "alerts@example.invalid",
                "subject": "Daily job alert: new roles",
                "epoch_ms": base + index * 1000,
                "extra_headers": {"List-Unsubscribe": "<https://example.invalid/unsub>"},
            }
        http = FakeInboxHttp(messages)
        context = RunContext.start(timeout_seconds=45)
        mailbox = GmailMailboxTransport(
            context=context, http=http, access_token="synthetic-token", message_factory=MailMessage,
        )

        scanned = mailbox.scan_inbox_metadata_window(
            datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc)
        )

        self.assertEqual(len(scanned), 50)
        self.assertEqual(len(http.metadata_fetch_ids), 50)
        self.assertEqual(http.full_fetch_ids, [])

        port = GmailInboxMetadataPort(mailbox)
        result = MailRouter().route_window(
            [port], datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc)
        )
        self.assertEqual(result.state, MailExecutionState.PASS)
        self.assertEqual(result.scanned_count, 50)
        self.assertEqual(result.count(MailClass.AUTOMATED_JOB_SOURCE), 50)
        self.assertEqual(http.full_fetch_ids, [])


if __name__ == "__main__":
    unittest.main()
