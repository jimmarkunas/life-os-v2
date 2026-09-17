"""BridgeView/Boostie is the production-shaped acceptance fixture proving the
generic preceding-context linked-card capability: role/location/compensation
appear as plain lines immediately before a job-specific "View This Job" CTA
link, unlike other generic providers whose card content lives inside the
anchor itself. All identifiers below are synthetic."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from lifeos.newsletter.models import ParseState, RoutedNewsletterMessage
from lifeos.newsletter.parsers import detect_source, parse_message

SENDER = "BridgeView <synthetic-alert@match.boostie.jobs.invalid>"


def _message(body: str, *, message_id: str = "synthetic-bridgeview-1") -> RoutedNewsletterMessage:
    return RoutedNewsletterMessage(
        mailbox="gmail",
        message_id=message_id,
        received_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
        sender=SENDER,
        subject="The latest jobs you'll want to see first",
        body_text=body,
    )


_TWO_CARD_BODY = """
<html><body>
<div>Synthetic Technical Program Manager</div><div>Denver, CO</div><div>$80 - $90 Hourly</div>
<a href="https://l1.boostie.jobs.invalid/et/click/synthetic-job-1">View This Job</a>
<div>Synthetic Senior Project Manager</div><div>Gallatin, TN</div><div>$65 - $72 Hourly</div>
<a href="https://l1.boostie.jobs.invalid/et/click/synthetic-job-2">View This Job</a>
<a href="https://l1.boostie.jobs.invalid/et/click/all">View All Jobs</a>
<a href="https://l1.boostie.jobs.invalid/et/click/dash">Go To Dashboard</a>
<a href="https://l1.boostie.jobs.invalid/et/click/prefs">Manage Preferences</a>
<a href="https://l1.boostie.jobs.invalid/et/click/unsub">Unsubscribe</a>
</body></html>
"""


class BridgeViewLinkedCardTests(unittest.TestCase):
    def test_sender_is_recognized_as_a_known_source(self) -> None:
        self.assertEqual(detect_source(_message(_TWO_CARD_BODY)), "Other")

    def test_every_card_parses_with_correct_fields_and_no_fabricated_company(self) -> None:
        result = parse_message(_message(_TWO_CARD_BODY))
        self.assertEqual(result.source_provider, "Other")
        self.assertEqual(len(result.observations), 2)

        first, second = result.observations
        self.assertEqual(first.role, "Synthetic Technical Program Manager")
        self.assertEqual(first.location_text, "Denver, CO")
        self.assertEqual(first.compensation_text, "$80 - $90 Hourly")
        self.assertEqual(first.source_apply_url, "https://l1.boostie.jobs.invalid/et/click/synthetic-job-1")
        self.assertIsNone(first.company)

        self.assertEqual(second.role, "Synthetic Senior Project Manager")
        self.assertEqual(second.location_text, "Gallatin, TN")
        self.assertEqual(second.compensation_text, "$65 - $72 Hourly")
        self.assertEqual(second.source_apply_url, "https://l1.boostie.jobs.invalid/et/click/synthetic-job-2")
        self.assertIsNone(second.company)

        self.assertNotEqual(first.source_apply_url, second.source_apply_url)

    def test_missing_company_is_enrichment_gap_not_ingestion_failure(self) -> None:
        result = parse_message(_message(_TWO_CARD_BODY))
        for observation in result.observations:
            self.assertIn("source-company-missing", observation.issues)
            self.assertNotIn("unresolved-card-shape", observation.issues)
        self.assertEqual(result.state, ParseState.PASS)

    def test_footer_and_control_links_never_become_vacancies(self) -> None:
        result = parse_message(_message(_TWO_CARD_BODY))
        urls = {obs.source_apply_url for obs in result.observations}
        for control_url in (
            "https://l1.boostie.jobs.invalid/et/click/all",
            "https://l1.boostie.jobs.invalid/et/click/dash",
            "https://l1.boostie.jobs.invalid/et/click/prefs",
            "https://l1.boostie.jobs.invalid/et/click/unsub",
        ):
            self.assertNotIn(control_url, urls)

    def test_malformed_linked_card_content_still_fails_closed(self) -> None:
        body = """
        <html><body>
        <div>Synthetic Role Only, No Location Or Compensation</div>
        <a href="https://l1.boostie.jobs.invalid/et/click/malformed">View This Job</a>
        <a href="https://l1.boostie.jobs.invalid/et/click/unsub">Unsubscribe</a>
        </body></html>
        """
        result = parse_message(_message(body, message_id="synthetic-bridgeview-malformed"))
        self.assertEqual(result.state, ParseState.DEGRADED)
        self.assertEqual(result.observations, ())


if __name__ == "__main__":
    unittest.main()
