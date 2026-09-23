"""Production-shaped proof for the V3-5 source-observation boundary.

This module is intentionally explicit-unittest-only so the repository's normal
pytest budget does not treat the contract proof as a second test suite.
"""
from __future__ import annotations

__test__ = False

import ast
import inspect
import re
import unittest
from datetime import datetime, timezone

from lifeos.newsletter.models import ParseState, RoutedNewsletterMessage, SourceVacancyObservation
from lifeos.newsletter.parsers import parse_message


def _message(message_id: str, subject: str, body: str, *, sender: str, raw_mime: str = ""):
    return RoutedNewsletterMessage(
        mailbox="gmail",
        message_id=message_id,
        received_at=datetime(2026, 1, 15, 12, tzinfo=timezone.utc),
        sender=sender,
        subject=subject,
        body_text=body,
        raw_mime=raw_mime,
    )


class AcquisitionAdapterContractProof(unittest.TestCase):
    def assert_observation_boundary(self, result, count: int | None = None):
        if count is not None:
            self.assertEqual(len(result.observations), count)
        self.assertTrue(all(isinstance(item, SourceVacancyObservation) for item in result.observations))
        self.assertTrue(all(item.evidence_ref for item in result.observations))
        self.assertEqual(len({item.evidence_ref for item in result.observations}), len(result.observations))

    def test_jobright_and_lensa_cards_preserve_source_facts_and_accounting(self):
        jobright = parse_message(_message(
            "jobright-v35", "Jobright daily jobs",
            "[Synthetic Labs\n92%\nSenior Program Manager\nRemote\n$120K-$150K/yr](https://jobright.ai/jobs/info/synthetic-1)\n[Malformed card](https://jobright.ai/jobs/info/synthetic-2)\nUnsubscribe",
            sender="alerts@jobright.example.invalid",
        ))
        self.assert_observation_boundary(jobright, 2)
        self.assertEqual(jobright.state, ParseState.DEGRADED)
        self.assertEqual(jobright.observations[0].provider_score, 92)
        self.assertIn("unresolved-card-shape", jobright.observations[1].issues)

        lensa = parse_message(_message(
            "lensa-v35", "Lensa job alert",
            "[Synthetic Works Senior Project Manager Remote $110K-$130K](https://jobs.lensa.com/synthetic-role-1)\n[Example Co Program Manager Remote $100K-$120K](https://jobs.lensa.com/synthetic-role-2)\nMore jobs",
            sender="alerts@lensa.example.invalid",
        ))
        self.assert_observation_boundary(lensa, 2)
        self.assertEqual(lensa.state, ParseState.PASS)
        self.assertEqual([item.source_provider for item in lensa.observations], ["Lensa", "Lensa"])
        self.assertEqual(lensa.observations[0].source_apply_url, "https://jobs.lensa.com/synthetic-role-1")

        malformed = parse_message(_message(
            "lensa-v35-bad", "Lensa job alert", "[Malformed card](https://jobs.lensa.com/synthetic-bad)\nMore jobs",
            sender="alerts@lensa.example.invalid",
        ))
        self.assert_observation_boundary(malformed, 0)
        self.assertEqual(malformed.state, ParseState.DEGRADED)
        self.assertTrue(malformed.issues)

    def test_linkedin_and_generic_controls_are_source_only(self):
        linkedin = parse_message(_message(
            "linkedin-v35", "LinkedIn job alert",
            "[Senior Product Manager\nSynthetic Systems · Remote](https://www.linkedin.com/jobs/view/123456789/)",
            sender="Synthetic LinkedIn notification via linkedin.com",
        ))
        self.assert_observation_boundary(linkedin, 1)
        item = linkedin.observations[0]
        self.assertEqual(item.source_provider, "LinkedIn Jobs")
        self.assertEqual(item.provider_job_id, "123456789")
        self.assertEqual(item.source_apply_url, "https://www.linkedin.com/jobs/view/123456789/")
        self.assertIsNone(getattr(item, "canonical_apply_url", None))
        self.assertIsNone(getattr(item, "fit", None))

        generic = parse_message(_message(
            "generic-v35", "Latest jobs",
            '<div>Technical Program Manager</div><div>Denver, CO</div><div>$80 - $90 Hourly</div><a href="https://l1.boostie.jobs.invalid/et/click/job">View This Job</a><a href="https://l1.boostie.jobs.invalid/et/click/unsub">Unsubscribe</a><a href="https://l1.boostie.jobs.invalid/et/click/all">View All Jobs</a>',
            sender="synthetic-alert@match.boostie.jobs.invalid",
        ))
        self.assert_observation_boundary(generic, 1)
        self.assertEqual(generic.observations[0].source_apply_url, "https://l1.boostie.jobs.invalid/et/click/job")
        self.assertNotIn("unsub", generic.observations[0].source_apply_url)

    def test_raw_mime_is_transient_and_provider_score_never_becomes_fit(self):
        raw = """From: Jobright <alerts@jobright.example.invalid>
Subject: Jobright daily jobs
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

[Synthetic Labs\n91%\nProgram Manager\nRemote](https://jobright.ai/jobs/info/synthetic-raw)
"""
        result = parse_message(_message("raw-v35", "Jobright daily jobs", "", sender="alerts@jobright.example.invalid", raw_mime=raw))
        self.assert_observation_boundary(result, 1)
        item = result.observations[0]
        self.assertEqual(item.provider_score, 91)
        self.assertFalse(hasattr(item, "raw_mime"))
        self.assertFalse(hasattr(item, "stable_job_key"))
        self.assertFalse(hasattr(item, "qualification"))
        self.assertFalse(hasattr(item, "fit"))
        self.assertNotIn(raw, repr(item))

    def test_source_boundary_has_no_identity_persistence_or_qualification_policy(self):
        fields = set(SourceVacancyObservation.__dataclass_fields__)
        self.assertFalse(fields & {"stable_job_key", "canonical_apply_url", "fit", "qualification", "lifecycle"})
        parser_source = inspect.getsource(__import__("lifeos.newsletter.parsers", fromlist=["parse_message"]))
        tree = ast.parse(parser_source)
        calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertFalse(calls & {"stable_job_key", "qualify", "create_page", "update_page"})

    def test_quarantined_adapters_share_observation_interface_without_activation(self):
        from lifeos.jobs import scale_up_acquisition, us_remote_acquisition

        for module in (us_remote_acquisition, scale_up_acquisition):
            source = inspect.getsource(module)
            self.assertIn("SourceVacancyObservation", source)
            self.assertIn("observations", source)
        # These modules remain quarantined; this proof only checks interface compatibility.


if __name__ == "__main__":
    unittest.main()
