from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from lifeos.mail import MailClass, MailMessage, MailRouter


class FakeMailbox:
    def __init__(self, provider: str, messages: list[MailMessage]) -> None:
        self.provider = provider
        self.messages = messages
        self.routed: list[tuple[str, str]] = []

    def scan_window(self, start: datetime, end: datetime):
        return [message for message in self.messages if start <= message.received_at < end]

    def route_to_newsletters(self, message_id: str, boundary_name: str) -> None:
        self.routed.append((message_id, boundary_name))


def message(
    provider: str,
    message_id: str,
    subject: str,
    *,
    sender: str,
    body: str = "",
    headers: dict[str, str] | None = None,
    minute: int = 0,
) -> MailMessage:
    return MailMessage(
        provider=provider,
        message_id=message_id,
        received_at=datetime(2026, 1, 15, 12, minute, tzinfo=timezone.utc),
        sender=sender,
        subject=subject,
        body_text=body,
        headers=headers or {},
    )


class WholeMailboxRoutingTests(unittest.TestCase):
    def test_scans_both_providers_and_routes_only_confirmed_job_alerts(self) -> None:
        gmail = FakeMailbox(
            "gmail",
            [
                message(
                    "gmail",
                    "synthetic-g-001",
                    "Daily job alert: new roles for you",
                    sender="alerts@example.invalid",
                    headers={"List-Unsubscribe": "<https://example.invalid/unsub>"},
                    minute=1,
                ),
                message(
                    "gmail",
                    "synthetic-g-002",
                    "A recruiter would like to speak",
                    sender="person@example.invalid",
                    body="I am a recruiter. Would you have time to discuss the role?",
                    minute=2,
                ),
                message(
                    "gmail",
                    "synthetic-g-003",
                    "Application received",
                    sender="no-reply@example.invalid",
                    body="Thank you for applying. We will share next steps.",
                    headers={"Precedence": "bulk"},
                    minute=3,
                ),
                message(
                    "gmail",
                    "synthetic-g-004",
                    "Your synthetic receipt",
                    sender="billing@example.invalid",
                    minute=4,
                ),
            ],
        )
        outlook = FakeMailbox(
            "outlook",
            [
                message(
                    "outlook",
                    "synthetic-o-001",
                    "Recommended jobs for you",
                    sender="jobs@example.invalid",
                    headers={"Precedence": "list"},
                    minute=5,
                ),
                message(
                    "outlook",
                    "synthetic-o-002",
                    "Interview scheduling",
                    sender="coordinator@example.invalid",
                    body="Please schedule time for your interview.",
                    minute=6,
                ),
            ],
        )
        router = MailRouter(max_workers=4)
        start = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)

        result = router.route_window([gmail, outlook], start, end)

        self.assertEqual(result.scanned_count, 6)
        self.assertEqual(result.count(MailClass.AUTOMATED_JOB_SOURCE), 2)
        self.assertEqual(result.count(MailClass.HUMAN_HIRING), 3)
        self.assertEqual(result.count(MailClass.UNRELATED), 1)
        self.assertEqual(result.errors, ())
        self.assertEqual(gmail.routed, [("synthetic-g-001", "J Newsletters")])
        self.assertEqual(outlook.routed, [("synthetic-o-001", "J Newsletters")])
        self.assertGreaterEqual(result.timings.scan_seconds, 0)
        self.assertGreaterEqual(result.timings.classify_seconds, 0)
        self.assertGreaterEqual(result.timings.route_seconds, 0)
        self.assertLess(result.timings.total_seconds, 45.0)

    def test_route_failure_is_degraded_and_never_marks_message_routed(self) -> None:
        class FailingMailbox(FakeMailbox):
            def route_to_newsletters(self, message_id: str, boundary_name: str) -> None:
                raise TimeoutError("synthetic timeout")

        mailbox = FailingMailbox(
            "gmail",
            [
                message(
                    "gmail",
                    "synthetic-g-fail",
                    "Job alert: new jobs",
                    sender="alerts@example.invalid",
                    headers={"List-Unsubscribe": "<https://example.invalid/unsub>"},
                )
            ],
        )
        router = MailRouter()
        start = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

        result = router.route_window([mailbox], start, start + timedelta(hours=1))

        self.assertEqual(result.scanned_count, 1)
        self.assertFalse(result.records[0].routed)
        self.assertEqual(result.errors[0].operation, "route")
        self.assertEqual(result.errors[0].detail, "TimeoutError")


if __name__ == "__main__":
    unittest.main()
