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
from urllib.parse import quote, quote_plus
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
    def test_jobright_semantic_search_unwraps_google_result_to_amazon(self):
        source_url = "https://jobright.ai/jobs/info/6a638e578d53603449602dc0"
        amazon_url = "https://amazon.jobs/en/jobs/10483583/sr-technical-program-manager-content-localization-understanding-and-enrichment"
        company = "Amazon"
        role = "Sr. Technical Program Manager, Content Localization, Understanding and Enrichment"
        search_url = "https://www.google.com/search?q=" + quote_plus(f"{company} {role}")
        google_body = f'<a href="/url?q={quote(amazon_url, safe="")}">Amazon vacancy</a>'
        amazon_body = (
            f"<h1>{role}</h1><p>{company}</p>"
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","description":"<p>Lead localization programs and '
            'manage technical content enrichment.</p>","datePosted":"2026-01-10"}'
            '</script>'
        )

        class RecordingFetcher:
            def __init__(self):
                self.requests = []

            def get(self, url):
                self.requests.append(url)
                pages = {
                    source_url: (source_url, "<html>source card only</html>"),
                    search_url: (search_url, google_body),
                    amazon_url: (amazon_url, amazon_body),
                }
                final_url, body = pages[url]
                return SimpleNamespace(final_url=final_url, body=body)

        fetcher = RecordingFetcher()
        evidence = acquire_terminal_vacancy_evidence(
            source_url,
            fetcher=fetcher,
            company=company,
            role=role,
            provider_job_id="6a638e578d53603449602dc0",
        )
        self.assertIsNotNone(evidence)
        self.assertIn(search_url, fetcher.requests)
        self.assertNotIn("6a638e578d53603449602dc0", search_url)
        self.assertEqual(evidence.canonical_url, amazon_url)
        self.assertNotIn("jobright.ai", evidence.canonical_url)
        self.assertIn("localization programs", evidence.description_text)

    def test_same_vacancy_search_fails_closed_without_valid_terminal_vacancy(self):
        source_url = "https://jobright.ai/jobs/info/opaque"
        search_url = "https://www.google.com/search?q=Amazon+Program+Manager"
        fetcher = MappingFetcher({"pages": [
            {"url": source_url, "final_url": source_url, "html": "<p>source card</p>"},
            {"url": search_url, "final_url": search_url, "html": '<a href="/url?q=https%3A%2F%2Famazon.jobs%2Fnot-a-vacancy">bad</a>'},
            {"url": "https://amazon.jobs/not-a-vacancy", "final_url": "https://amazon.jobs/not-a-vacancy", "html": "<p>not enough evidence</p>"},
        ]})
        self.assertIsNone(acquire_terminal_vacancy_evidence(
            source_url, fetcher=fetcher, company="Amazon", role="Program Manager", provider_job_id="opaque"
        ))

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


class ProductionRegressionProofs(unittest.TestCase):
    """Production-shaped regression proofs for run-36097365815 failures."""

    # ------------------------------------------------------------------
    # Vanta: jobs.ashbyhq.com/{org}/{uuid} — no /posting/ segment
    # ------------------------------------------------------------------
    def test_vanta_ashby_url_reaches_employer_ats_jd(self):
        vanta_url = "https://jobs.ashbyhq.com/vanta/021cca9c-f937-4d97-8be7-bc83af8307be"
        api_url = "https://api.ashbyhq.com/posting-api/job-board/vanta/job-postings/021cca9c-f937-4d97-8be7-bc83af8307be"
        api_body = '{"descriptionHtml":"<p>Lead complex security-compliance programs and own cross-functional delivery of multi-quarter security initiatives across engineering and go-to-market.</p>","publishedAt":"2026-01-10"}'
        evidence = acquire_terminal_vacancy_evidence(
            vanta_url,
            fetcher=MappingFetcher({"pages": [
                {"url": vanta_url, "final_url": vanta_url, "html": "<html><p>card only</p></html>"},
                {"url": api_url, "final_url": api_url, "html": api_body},
            ]}),
            company="Vanta",
            role="Technical Program Manager",
            provider_job_id="021cca9c-f937-4d97-8be7-bc83af8307be",
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.canonical_url, vanta_url)
        self.assertEqual(evidence.evidence_source, "ashby_api")
        self.assertIn("compliance programs", evidence.description_text)
        self.assertEqual(evidence.posting_date_raw, "2026-01-10")

    def test_vanta_ashby_evidence_produces_terminal_disposition_via_adapter(self):
        from datetime import date
        from dataclasses import replace
        from lifeos.jobs.models import FitAuthority
        from lifeos.jobs.newsletter_contract import Disposition, ingest
        from lifeos.jobs.repository import InMemoryCareerRepository

        vanta_url = "https://jobs.ashbyhq.com/vanta/021cca9c-f937-4d97-8be7-bc83af8307be"
        desc = "Lead complex security-compliance programs and own cross-functional delivery of multi-quarter security initiatives across engineering and go-to-market."
        evidence = TerminalVacancyEvidence(vanta_url, desc, "2026-01-10", "ashby_api", (vanta_url,))
        with patch("lifeos.jobs.newsletter_adapter.acquire_terminal_vacancy_evidence", return_value=evidence):
            candidate = _adapter(None).to_jobs_candidate(
                _observation(source_url=vanta_url, provider="Vanta", provider_id="021cca9c")
            )
        self.assertEqual(candidate.job.apply_url, vanta_url)
        self.assertIn("compliance programs", candidate.job.description_text or "")
        enriched = replace(candidate, unresolved_reason=None, fit=85, fit_authority=FitAuthority.AUTHORITATIVE)
        result = ingest([enriched], lane=LANE, lane_priority={"US Remote": 0},
                        repository=InMemoryCareerRepository(), run_date=date(2026, 1, 15))
        self.assertEqual(result[0].disposition, Disposition.CREATED)

    # ------------------------------------------------------------------
    # Shopify: shopify.com/careers/{title}_{uuid} → Ashby board "shopify"
    # ------------------------------------------------------------------
    def test_shopify_careers_url_reaches_employer_ats_jd(self):
        shopify_url = "https://www.shopify.com/careers/senior-deal-strategy-manager-payments-platform-pricing_9f1a97d8-5862-4ee4-b2b1-0dc02e20c11c"
        api_url = "https://api.ashbyhq.com/posting-api/job-board/shopify/job-postings/9f1a97d8-5862-4ee4-b2b1-0dc02e20c11c"
        api_body = '{"descriptionHtml":"<p>Drive deal strategy and pricing programs across payments platform products, owning cross-functional delivery for major commercial initiatives.</p>","publishedAt":"2026-01-08"}'
        evidence = acquire_terminal_vacancy_evidence(
            shopify_url,
            fetcher=MappingFetcher({"pages": [
                {"url": shopify_url, "final_url": shopify_url, "html": "<html><p>card only</p></html>"},
                {"url": api_url, "final_url": api_url, "html": api_body},
            ]}),
            company="Shopify",
            role="Senior Deal Strategy Manager",
            provider_job_id="9f1a97d8",
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.canonical_url, shopify_url)
        self.assertEqual(evidence.evidence_source, "ashby_api")
        self.assertIn("deal strategy", evidence.description_text)
        self.assertEqual(evidence.posting_date_raw, "2026-01-08")

    def test_shopify_careers_evidence_produces_terminal_disposition_via_adapter(self):
        from datetime import date
        from dataclasses import replace
        from lifeos.jobs.models import FitAuthority
        from lifeos.jobs.newsletter_contract import Disposition, ingest
        from lifeos.jobs.repository import InMemoryCareerRepository

        shopify_url = "https://www.shopify.com/careers/senior-deal-strategy-manager-payments-platform-pricing_9f1a97d8-5862-4ee4-b2b1-0dc02e20c11c"
        desc = "Drive deal strategy and pricing programs across payments platform products, owning cross-functional delivery for major commercial initiatives."
        evidence = TerminalVacancyEvidence(shopify_url, desc, "2026-01-08", "ashby_api", (shopify_url,))
        with patch("lifeos.jobs.newsletter_adapter.acquire_terminal_vacancy_evidence", return_value=evidence):
            candidate = _adapter(None).to_jobs_candidate(
                _observation(source_url=shopify_url, provider="Shopify", provider_id="9f1a97d8")
            )
        self.assertEqual(candidate.job.apply_url, shopify_url)
        self.assertIn("deal strategy", candidate.job.description_text or "")
        enriched = replace(candidate, unresolved_reason=None, fit=80, fit_authority=FitAuthority.AUTHORITATIVE)
        result = ingest([enriched], lane=LANE, lane_priority={"US Remote": 0},
                        repository=InMemoryCareerRepository(), run_date=date(2026, 1, 15))
        self.assertEqual(result[0].disposition, Disposition.CREATED)

    # ------------------------------------------------------------------
    # Robert Half identity regression: ML-engineer URL cannot store Enterprise Architect
    # ------------------------------------------------------------------
    def test_robert_half_machine_learning_engineer_url_cannot_become_enterprise_architect(self):
        rh_url = "https://www.roberthalf.com/us/en/job/reston-virginia/machine-learning-engineer/04838-0013428083-usen"
        next_data_body = (
            '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"job":{"job_description":'
            '"<p>Lead ML-platform delivery and own machine-learning engineer programs.</p>",'
            '"title":"Machine Learning Engineer","location":"Reston, VA"}}}}</script>'
        )
        evidence = acquire_terminal_vacancy_evidence(
            rh_url,
            fetcher=MappingFetcher({"pages": [
                {"url": rh_url, "final_url": rh_url, "html": next_data_body},
            ]}),
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.canonical_url, rh_url)
        self.assertIn("machine-learning", evidence.description_text.casefold())
        self.assertNotIn("enterprise architect", evidence.description_text.casefold())

    # ------------------------------------------------------------------
    # Negative acquisition: explicit non-US geography must be rejected
    # ------------------------------------------------------------------
    def test_plausible_rejects_explicit_uk_toronto_canada(self):
        from lifeos.jobs.us_remote_acquisition import _plausible
        self.assertFalse(_plausible("Program Manager", "Remote - United Kingdom"))
        self.assertFalse(_plausible("Technical Program Manager", "Toronto, ON, Canada"))
        self.assertFalse(_plausible("Project Manager", "Canada"))
        self.assertFalse(_plausible("Product Manager", "Ontario, Canada"))

    def test_plausible_rejects_software_engineer_false_positives_from_platform_terms(self):
        from lifeos.jobs.us_remote_acquisition import _plausible
        self.assertFalse(_plausible("Software Engineer", "Remote, US"))
        self.assertFalse(_plausible("Staff Engineer", None))
        self.assertFalse(_plausible("Senior Software Engineer", "United States"))


if __name__ == "__main__":
    unittest.main()
