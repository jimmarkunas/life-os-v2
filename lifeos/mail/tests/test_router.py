from __future__ import annotations
import unittest
from datetime import datetime, timedelta, timezone
from lifeos.mail import MailClass, MailExecutionState, MailMessage, MailRouter

class FakeMailbox:
    def __init__(self, provider, messages): self.provider, self.messages, self.routed = provider, messages, []
    def scan_window(self, start, end): return [m for m in self.messages if start <= m.received_at < end]
    def route_to_newsletters(self, message_id, boundary_name): self.routed.append((message_id, boundary_name))

def message(provider, message_id, subject, *, sender, body="", headers=None, minute=0):
    return MailMessage(provider, message_id, datetime(2026,1,15,12,minute,tzinfo=timezone.utc), sender, subject, body, headers or {})

class WholeMailboxRoutingTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026,1,15,12,0,tzinfo=timezone.utc); self.end = self.start + timedelta(hours=1)

    def test_scans_both_providers_and_routes_only_confirmed_job_alerts(self):
        gmail = FakeMailbox("gmail", [
            message("gmail","synthetic-g-001","Daily job alert: new roles for you",sender="alerts@example.invalid",headers={"List-Unsubscribe":"<https://example.invalid/unsub>"},minute=1),
            message("gmail","synthetic-g-002","Opportunity at Synthetic Co",sender="person@example.invalid",body="I am a recruiter and would like to speak about next steps.",minute=2),
            message("gmail","synthetic-g-003","Application received",sender="no-reply@example.invalid",body="Thank you for applying. We will share next steps.",headers={"Precedence":"bulk"},minute=3),
            message("gmail","synthetic-g-004","Your synthetic receipt",sender="billing@example.invalid",minute=4),
        ])
        outlook = FakeMailbox("outlook", [
            message("outlook","synthetic-o-001","Recommended jobs for you",sender="jobs@example.invalid",headers={"Precedence":"list"},minute=5),
            message("outlook","synthetic-o-002","Interview scheduling",sender="coordinator@example.invalid",body="Please schedule time for your interview.",minute=6),
        ])
        result = MailRouter(max_workers=4).route_window([gmail,outlook], self.start, self.end)
        self.assertEqual(result.state, MailExecutionState.PASS); self.assertTrue(result.checkpoint_safe)
        self.assertEqual(result.scanned_count,6); self.assertEqual(result.count(MailClass.AUTOMATED_JOB_SOURCE),2)
        self.assertEqual(result.count(MailClass.HUMAN_HIRING),3); self.assertEqual(result.count(MailClass.UNRELATED),1)
        self.assertEqual(gmail.routed, [("synthetic-g-001","J Newsletters")]); self.assertEqual(outlook.routed, [("synthetic-o-001","J Newsletters")])
        self.assertLess(result.timings.total_seconds,45.0)

    def test_newsletter_body_hiring_language_does_not_override_source_evidence(self):
        gmail = FakeMailbox("gmail", [message("gmail","synthetic-news-001","Daily job alert: 4 new roles",sender="alerts@example.invalid",body="Senior Program Manager. Partner with recruiter teams, improve interview loops, and define next steps.",headers={"List-Unsubscribe":"<https://example.invalid/unsub>","Precedence":"list"})])
        result = MailRouter().route_window([gmail], self.start, self.end)
        self.assertEqual(result.records[0].classification.mail_class, MailClass.AUTOMATED_JOB_SOURCE)
        self.assertTrue(result.records[0].routed)

    def test_scan_failure_is_degraded_and_checkpoint_cannot_advance(self):
        class FailingScan(FakeMailbox):
            def scan_window(self, start, end): raise TimeoutError("synthetic")
        gmail = FailingScan("gmail", [])
        outlook = FakeMailbox("outlook", [message("outlook","synthetic-o-ok","Job alert: new jobs",sender="alerts@example.invalid",headers={"List-ID":"jobs.example.invalid"})])
        result = MailRouter().route_window([gmail,outlook], self.start, self.end)
        self.assertEqual(result.state, MailExecutionState.DEGRADED); self.assertFalse(result.checkpoint_safe)
        self.assertTrue(any(s.provider=="gmail" and not s.complete for s in result.provider_scans))
        self.assertEqual(outlook.routed, [("synthetic-o-ok","J Newsletters")])

    def test_no_required_mailbox_ports_is_degraded(self):
        result = MailRouter().route_window([], self.start, self.end)
        self.assertEqual(result.state, MailExecutionState.DEGRADED); self.assertFalse(result.checkpoint_safe)
        self.assertEqual(result.errors[0].detail, "no-mailbox-providers")

    def test_route_failure_is_degraded_and_never_marks_message_routed(self):
        class FailingRoute(FakeMailbox):
            def route_to_newsletters(self, message_id, boundary_name): raise TimeoutError("synthetic")
        box = FailingRoute("gmail", [message("gmail","synthetic-g-fail","Job alert: new jobs",sender="alerts@example.invalid",headers={"List-Unsubscribe":"<https://example.invalid/unsub>"})])
        result = MailRouter().route_window([box], self.start, self.end)
        self.assertEqual(result.state, MailExecutionState.DEGRADED); self.assertFalse(result.checkpoint_safe); self.assertFalse(result.records[0].routed)

if __name__ == "__main__": unittest.main()
