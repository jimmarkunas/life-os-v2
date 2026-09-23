"""Explicit V3-3 production-shaped terminal-evidence proofs.

This module is intentionally not pytest-collected because the repository's
test ratchet is already at its limit. Run it explicitly with
``PYTHONPATH=. .venv/bin/python -m unittest lifeos.jobs.test_terminal_evidence``.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from lifeos.jobs.identity import stable_job_key
from lifeos.jobs.lifecycle import JobLedgerRecord
from lifeos.jobs.newsletter_adapter import NewsletterJobsAdapter, NewsletterAdapterConfig
from lifeos.jobs.newsletter_contract import Disposition, ingest
from lifeos.jobs.models import FreshnessStatus, FitAuthority, JobObservation, NormalizedCandidate, Company, WorkMode
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.repository import InMemoryCareerRepository
from lifeos.jobs.terminal_evidence import MappingFetcher, TerminalVacancyEvidence, acquire_terminal_vacancy_evidence

__test__ = False

NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
LANE = LaneConfig("US Remote", "US", 72, None, "remote_only", None, False, None)


def _terminal_html(*, date_posted: str = "2026-01-10") -> str:
    return (
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","datePosted":"' + date_posted + '",'
        '"description":"<p>Lead program delivery and manage complex cross-functional initiatives.</p>"}'
        '</script>'
    )


def _observation(*, source_url: str, provider: str = "Jobright", provider_id: str = "p-1", source_description: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        evidence_ref=f"{provider}-{provider_id}", source_provider=provider, source_mailbox="gmail",
        source_message_id="msg-1", source_subject="Jobs", company="Synthetic Co",
        role="Program Manager", location_text="Remote", compensation_text=None,
        source_apply_url=source_url, provider_job_id=provider_id, provider_score=92,
        source_description_text=source_description, source_received_at=NOW, issues=(),
    )


def _adapter(fetcher) -> NewsletterJobsAdapter:
    profile = SimpleNamespace(
        title_patterns={}, direct_specialization_patterns={}, dimension_patterns={},
        evidence_patterns={}, material_patterns=(), ignore_patterns=(), hard_family_patterns=(),
    )
    return NewsletterJobsAdapter(NewsletterAdapterConfig(
        fetcher=fetcher, fit_profile=profile, market="US", source_lane="US Remote",
    ))


class TerminalEvidenceProofs(unittest.TestCase):
    def test_intermediary_urls_require_bounded_terminal_proof(self):
        source_urls = (
            "https://www.linkedin.com/jobs/view/123456789/",
            "https://jobright.ai/jobs/info/synthetic-1",
            "https://jobs.lensa.com/synthetic-1",
        )
        fetcher = MappingFetcher({"pages": [
            {"url": url, "final_url": url, "html": "<html><p>source card text only</p></html>"}
            for url in source_urls
        ]})
        for source_url in source_urls:
            with self.subTest(source_url=source_url):
                self.assertIsNone(acquire_terminal_vacancy_evidence(source_url, fetcher=fetcher))

    def test_employer_ats_jd_and_posting_date_are_accepted_only_from_terminal_page(self):
        source_url = "https://jobright.ai/jobs/info/synthetic-1"
        terminal_url = "https://boards.greenhouse.io/synthetic/jobs/1"
        evidence = acquire_terminal_vacancy_evidence(
            source_url,
            fetcher=MappingFetcher({"pages": [
                {"url": source_url, "final_url": terminal_url, "html": _terminal_html()},
            ]}),
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.canonical_url, terminal_url)
        self.assertIn("Lead program delivery", evidence.description_text)
        self.assertEqual(evidence.posting_date_raw, "2026-01-10")
        self.assertEqual(evidence.evidence_source, "schema.org/JobPosting")

    def test_source_text_only_is_unresolved_without_fabricated_url_jd_or_fit(self):
        source_url = "https://www.linkedin.com/jobs/view/123456789/"
        candidate = _adapter(MappingFetcher({"pages": []})).to_jobs_candidate(
            _observation(source_url=source_url, provider="LinkedIn Jobs", provider_id="123456789", source_description="Lead program delivery from the alert card.")
        )
        self.assertEqual(candidate.unresolved_reason, "mandatory enrichment unresolved: actionable Apply URL and employer/ATS JD required")
        self.assertIsNone(candidate.job.apply_url)
        self.assertIsNone(candidate.job.description_text)
        self.assertIsNone(candidate.fit)

    def test_terminal_unresolved_persists_and_stronger_evidence_reconciles_same_row(self):
        source_url = "https://jobright.ai/jobs/info/synthetic-1"
        terminal_url = "https://boards.greenhouse.io/synthetic/jobs/1"
        observation = _observation(source_url=source_url)
        adapter = _adapter(MappingFetcher({"pages": [
            {"url": source_url, "final_url": terminal_url, "html": _terminal_html()},
        ]}))
        repository = InMemoryCareerRepository()
        identity = adapter.to_identity_candidate(observation)
        first = ingest([identity], lane=LANE, lane_priority={"US Remote": 0}, repository=repository, run_date=date(2026, 1, 15))
        self.assertTrue(first[0].persistence_verified)
        stable_key = first[0].stable_job_key

        unresolved = _adapter(MappingFetcher({"pages": []})).to_jobs_candidate(_observation(source_url=source_url))
        unresolved = replace(
            unresolved,
            job=replace(unresolved.job, canonical_identity=stable_key),
            unresolved_reason=None,
            fit_reason="terminal_unresolved",
        )
        unresolved_result = ingest([unresolved], lane=LANE, lane_priority={"US Remote": 0}, repository=repository, run_date=date(2026, 1, 15))
        self.assertEqual(unresolved_result[0].disposition, Disposition.REVIEW_DEGRADED)
        unresolved_row = repository.get_many([stable_key])[stable_key]
        self.assertIsNone(unresolved_row.job.job.apply_url)
        self.assertIsNone(unresolved_row.job.job.description_text)
        self.assertIsNone(unresolved_row.job.fit)

        evidence = TerminalVacancyEvidence(terminal_url, "Lead program delivery and manage complex cross-functional initiatives.", "2026-01-10", "vacancy_page", (source_url, terminal_url))
        with patch("lifeos.jobs.newsletter_adapter.acquire_terminal_vacancy_evidence", return_value=evidence):
            enriched = adapter.to_jobs_candidate(observation)
        # The adapter proof above is deliberately configured without a fit
        # profile. Supply the already-established resolved outcome so this
        # test isolates terminal evidence + same-row persistence, not V3-4.
        enriched = replace(enriched, unresolved_reason=None, fit=80, fit_authority=FitAuthority.AUTHORITATIVE)
        enriched_result = ingest([enriched], lane=LANE, lane_priority={"US Remote": 0}, repository=repository, run_date=date(2026, 1, 15))
        self.assertTrue(enriched_result[0].persistence_verified)
        self.assertEqual(enriched_result[0].stable_job_key, stable_key)
        self.assertEqual(len(repository._store), 1)
        final = repository.get_many([stable_key])[stable_key]
        self.assertEqual(final.job.job.apply_url, terminal_url)
        self.assertIn("Lead program delivery", final.job.job.description_text or "")

    def test_repeated_provider_vacancy_uses_existing_bounded_cache(self):
        source_url = "https://jobright.ai/jobs/info/synthetic-1"
        adapter = _adapter(None)
        evidence = TerminalVacancyEvidence("https://boards.greenhouse.io/synthetic/jobs/1", "A trustworthy JD", "", "vacancy_page", (source_url,))
        with patch("lifeos.jobs.newsletter_adapter.acquire_terminal_vacancy_evidence", return_value=evidence) as acquire:
            first = adapter._terminal_evidence_for(source_url, company="Synthetic Co", role="Program Manager", provider_job_id="p-1")
            second = adapter._terminal_evidence_for(source_url, company="Synthetic Co", role="Program Manager", provider_job_id="p-1")
        self.assertIs(first, second)
        acquire.assert_called_once()


if __name__ == "__main__":
    unittest.main()
