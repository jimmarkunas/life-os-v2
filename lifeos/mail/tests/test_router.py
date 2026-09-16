from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from lifeos.mail import MailClass, MailExecutionState, MailMessage, MailRouter


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
    def setUp(self) -> None:
        self.start = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        self.end = self.start + timedelta(hours=1)

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
                    "Opportunity at Synthetic Co",
                    sender="person@example.invalid",
                    body="I am a recruiter and would like to speak about next steps.",
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

        result = router.route_window([gmail, outlook], self.start, self.end)

        self.assertEqual(result.state, MailExecutionState.PASS)
        self.assertTrue(result.checkpoint_safe)
        self.assertEqual(result.scanned_count, 6)
        self.assertEqual(result.count(MailClass.AUTOMATED_JOB_SOURCE), 2)
        self.assertEqual(result.count(MailClass.HUMAN_HIRING), 3)
        self.assertEqual(result.count(MailClass.UNRELATED), 1)
        self.assertEqual(result.errors, ())
        self.assertEqual(gmail.routed, [("synthetic-g-001", "J Newsletters")])
        self.assertEqual(outlook.routed, [("synthetic-o-001", "J Newsletters")])
        self.assertLess(result.timings.total_seconds, 45.0)

    def test_newsletter_body_hiring_language_does_not_override_source_evidence(self) -> None:
        gmail = FakeMailbox(
            "gmail",
            [
                message(
                    "gmail",
                    "synthetic-news-001",
                    "Daily job alert: 4 new roles",
                    sender="alerts@example.invalid",
                    body=(
                        "Senior Program Manager. Partner with recruiter teams, improve "
                        "interview loops, and define next steps."
                    ),
                    headers={
                        "List-Unsubscribe": "<https://example.invalid/unsub>",
                        "Precedence": "list",
                    },
                )
            ],
        )

        result = MailRouter().route_window([gmail], self.start, self.end)

        self.assertEqual(
            result.records[0].classification.mail_class,
            MailClass.AUTOMATED_JOB_SOURCE,
        )
        self.assertTrue(result.records[0].routed)

    def test_scan_failure_is_degraded_and_checkpoint_cannot_advance(self) -> None:
        class FailingScanMailbox(FakeMailbox):
            def scan_window(self, start: datetime, end: datetime):
                raise TimeoutError("synthetic timeout")

        gmail = FailingScanMailbox("gmail", [])
        outlook = FakeMailbox(
            "outlook",
            [
                message(
                    "outlook",
                    "synthetic-o-ok",
                    "Job alert: new jobs",
                    sender="alerts@example.invalid",
                    headers={"List-ID": "jobs.example.invalid"},
                )
            ],
        )

        result = MailRouter().route_window([gmail, outlook], self.start, self.end)

        self.assertEqual(result.state, MailExecutionState.DEGRADED)
        self.assertFalse(result.checkpoint_safe)
        self.assertTrue(
            any(scan.provider == "gmail" and not scan.complete for scan in result.provider_scans)
        )
        self.assertEqual(outlook.routed, [("synthetic-o-ok", "J Newsletters")])

    def test_no_required_mailbox_ports_is_degraded(self) -> None:
        result = MailRouter().route_window([], self.start, self.end)

        self.assertEqual(result.state, MailExecutionState.DEGRADED)
        self.assertFalse(result.checkpoint_safe)
        self.assertEqual(result.errors[0].detail, "no-mailbox-providers")

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

        result = MailRouter().route_window([mailbox], self.start, self.end)

        self.assertEqual(result.state, MailExecutionState.DEGRADED)
        self.assertFalse(result.checkpoint_safe)
        self.assertEqual(result.scanned_count, 1)
        self.assertFalse(result.records[0].routed)
        self.assertEqual(result.errors[0].operation, "route")
        self.assertEqual(result.errors[0].detail, "TimeoutError")


    def test_worker_pools_are_clamped_to_supported_ceilings(self) -> None:
        router = MailRouter(max_workers=1000)
        self.assertLessEqual(router._scan_workers, 2)
        self.assertLessEqual(router._message_workers, 8)

    def test_1000_confirmed_messages_use_bounded_in_flight_futures(self) -> None:
        lock = threading.Lock()
        state = {"active": 0, "max_active": 0}

        class TrackingMailbox(FakeMailbox):
            def route_to_newsletters(self, message_id: str, boundary_name: str) -> None:
                with lock:
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                time.sleep(0.0005)
                with lock:
                    state["active"] -= 1
                super().route_to_newsletters(message_id, boundary_name)

        messages = [
            message(
                "gmail",
                f"synthetic-g-{i:04d}",
                "Daily job alert: new roles for you",
                sender="alerts@example.invalid",
                headers={"List-Unsubscribe": "<https://example.invalid/unsub>"},
                minute=0,
            )
            for i in range(1000)
        ]
        gmail = TrackingMailbox("gmail", messages)
        router = MailRouter()

        result = router.route_window([gmail], self.start, self.end)

        self.assertEqual(result.scanned_count, 1000)
        self.assertEqual(result.count(MailClass.AUTOMATED_JOB_SOURCE), 1000)
        self.assertEqual(len(gmail.routed), 1000)
        self.assertEqual(result.errors, ())
        # Bounded: never more in flight than the clamped worker ceiling.
        self.assertLessEqual(state["max_active"], 8)
        self.assertGreater(state["max_active"], 1)
        # Deterministic final ordering, independent of completion order.
        self.assertEqual(
            [record.ref.message_id for record in result.records],
            [f"synthetic-g-{i:04d}" for i in range(1000)],
        )


if __name__ == "__main__":
    unittest.main()
