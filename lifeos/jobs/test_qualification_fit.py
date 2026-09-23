"""Explicit V3-4 production-shaped qualification/Fit proofs.

This module is intentionally not pytest-collected because the repository's
test ratchet is already at its limit. Run it explicitly with
``PYTHONPATH=. .venv/bin/python -m unittest lifeos.jobs.test_qualification_fit``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from lifeos.jobs.fit_requirement_extraction import extract_requirements
from lifeos.jobs.fit_scoreability import compile_fit
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.fit_title_semantics import classify_title
from lifeos.jobs.models import (
    AdmissionStatus, Company, FitAuthority, FitEvidenceKind, FreshnessStatus,
    JobObservation, NormalizedCandidate, WorkMode,
)
from lifeos.jobs.newsletter_adapter import NewsletterJobsAdapter, NewsletterAdapterConfig
from lifeos.jobs.newsletter_contract import Disposition, ingest
from lifeos.jobs.qualification import LaneConfig, qualify, UNIVERSAL_FIT_FLOOR
from lifeos.jobs.repository import InMemoryCareerRepository
from lifeos.jobs.terminal_evidence import TerminalVacancyEvidence

__test__ = False

NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
JD = "Required: lead program delivery and cloud strategy. Must manage complex cross-functional programs with 8 years experience."
PROFILE = FitProfile(
    "V3-4-proof",
    {"DIRECT": ("program", "technical program", "product"), "ADJACENT": ("architect",), "METHOD_EQUIVALENT": ("delivery",), "UNSUPPORTED": ("software engineer",)},
    ("automation", "AI"),
    {"role_seniority": ("years? experience", "senior", "lead"), "functional": ("program", "delivery", "management"), "technical_platform": ("cloud", "platform", "data"), "delivery_complexity": ("complex", "cross-functional"), "competitive_advantage": ("strategy", "automation", "AI")},
    {"DIRECT": ("required", "must", "experience"), "ADJACENT": ("preferred",), "METHOD_EQUIVALENT": ("plus",), "UNSUPPORTED": ("software engineer",)},
    ("required", "must", "experience", "lead", "manage", "preferred", "plus"),
    (),
    ("hands-on coding", "software development"),
)
LANE = LaneConfig("US Remote", "US", UNIVERSAL_FIT_FLOOR, None, "remote_only", None, False, None)


def _candidate(*, fit: int | None, work_mode: WorkMode = WorkMode.REMOTE, comp: int | None = None, posting: date | None = None, freshness: FreshnessStatus = FreshnessStatus.FRESH) -> NormalizedCandidate:
    job = JobObservation(
        company=Company("Synthetic Co"), role="Program Manager", location="United States",
        work_mode=work_mode, compensation_text=None, compensation_minimum=comp,
        posting_date=posting, apply_url="https://boards.greenhouse.io/synthetic/jobs/1",
        source_lane="US Remote", provider_score=99, canonical_identity="job:synthetic",
        description_text=JD, source_provider="Jobright",
    )
    return NormalizedCandidate(
        job=job, fit=fit, market="US", freshness_status=freshness,
        evidence_ref=f"fit-{fit}", fit_authority=FitAuthority.AUTHORITATIVE if fit is not None else FitAuthority.NON_AUTHORITATIVE,
        fit_evidence_kind=FitEvidenceKind.EMPLOYER_ATS_JD if fit is not None else FitEvidenceKind.NONE,
    )


def _newsletter_observation(provider_score: int) -> SimpleNamespace:
    return SimpleNamespace(
        evidence_ref=f"rubrik-{provider_score}", source_provider="Jobright", source_mailbox="gmail",
        source_message_id="msg-fit", source_subject="Jobs", company="Synthetic Co",
        role="Program Manager", location_text="Remote", compensation_text=None,
        source_apply_url="https://jobright.ai/jobs/info/synthetic-1", provider_job_id=f"p-{provider_score}",
        provider_score=provider_score, source_description_text="Lead program delivery from the alert card.",
        source_received_at=NOW, issues=(),
    )


class QualificationFitProofs(unittest.TestCase):
    def test_trustworthy_jd_is_structured_and_authoritative_fit_is_non_null(self):
        extracted = extract_requirements(JD, PROFILE.dimension_patterns, PROFILE.evidence_patterns, PROFILE.material_patterns)
        self.assertTrue(extracted.requirements)
        title = classify_title("Program Manager", PROFILE.title_patterns, PROFILE.direct_specialization_patterns)
        compiled = compile_fit(title="Program Manager", title_semantics=title, requirements=list(extracted.requirements), evidence_kind=FitEvidenceKind.EMPLOYER_ATS_JD)
        self.assertIsNotNone(compiled.fit_result)
        self.assertIsNotNone(compiled.fit_result.final_score)
        self.assertEqual(compiled.authority, FitAuthority.AUTHORITATIVE)

    def test_provider_match_percentage_is_not_fit_input_and_source_only_is_not_authoritative(self):
        evidence = TerminalVacancyEvidence("https://boards.greenhouse.io/synthetic/jobs/1", JD, "", "vacancy_page", ("https://jobright.ai/jobs/info/synthetic-1",))
        candidates = []
        for provider_score in (1, 99):
            adapter = NewsletterJobsAdapter(NewsletterAdapterConfig(fetcher=None, fit_profile=PROFILE, market="US", source_lane="US Remote"))
            with patch("lifeos.jobs.newsletter_adapter.acquire_terminal_vacancy_evidence", return_value=evidence):
                candidates.append(adapter.to_jobs_candidate(_newsletter_observation(provider_score)))
        self.assertEqual(candidates[0].fit, candidates[1].fit)
        self.assertEqual(candidates[0].fit_authority, FitAuthority.AUTHORITATIVE)

        source_only_observation = _newsletter_observation(99)
        source_only_observation.source_apply_url = "https://jobright.ai/jobs/info/source-only"
        source_only = NewsletterJobsAdapter(NewsletterAdapterConfig(fetcher=None, fit_profile=PROFILE, market="US", source_lane="US Remote")).to_jobs_candidate(source_only_observation)
        self.assertIsNone(source_only.fit)
        self.assertIsNone(source_only.job.apply_url)
        self.assertIsNone(source_only.job.description_text)
        self.assertIsNotNone(source_only.unresolved_reason)

    def test_unscoreable_evidence_is_fail_closed_review(self):
        title = classify_title("Program Manager", PROFILE.title_patterns, PROFILE.direct_specialization_patterns)
        compiled = compile_fit(title="Program Manager", title_semantics=title, requirements=[], evidence_kind=FitEvidenceKind.EMPLOYER_ATS_JD)
        self.assertIsNone(compiled.fit_result)
        self.assertEqual(compiled.reason, "missing_scoreable_jd_requirements")
        result = qualify(_candidate(fit=None), lane=LANE, run_date=date(2026, 1, 15))
        self.assertEqual(result.admission_status, AdmissionStatus.PASSED_REVIEW)

    def test_below_floor_persists_but_is_not_admitted(self):
        repository = InMemoryCareerRepository()
        result = ingest([_candidate(fit=UNIVERSAL_FIT_FLOOR - 1)], lane=LANE, lane_priority={"US Remote": 0}, repository=repository, run_date=date(2026, 1, 15))
        self.assertEqual(result[0].disposition, Disposition.EXCLUDED)
        self.assertTrue(result[0].persistence_verified)
        row = repository.get_many(["job:synthetic"])["job:synthetic"]
        self.assertEqual(row.job.fit, UNIVERSAL_FIT_FLOOR - 1)
        self.assertEqual(row.job.eligible_lanes, ())

    def test_at_floor_is_admitted_when_shared_lane_gates_pass(self):
        repository = InMemoryCareerRepository()
        result = ingest([_candidate(fit=UNIVERSAL_FIT_FLOOR)], lane=LANE, lane_priority={"US Remote": 0}, repository=repository, run_date=date(2026, 1, 15))
        self.assertEqual(result[0].disposition, Disposition.CREATED)
        self.assertEqual(repository.get_many(["job:synthetic"])["job:synthetic"].job.admission_status, AdmissionStatus.ADMITTED)

    def test_shared_remote_compensation_and_freshness_policies(self):
        remote_only = LANE
        self.assertEqual(qualify(_candidate(fit=80, work_mode=WorkMode.ONSITE), lane=remote_only, run_date=date(2026, 1, 15)).admission_status, AdmissionStatus.EXCLUDED)
        self.assertEqual(qualify(_candidate(fit=80, work_mode=WorkMode.UNKNOWN), lane=remote_only, run_date=date(2026, 1, 15)).admission_status, AdmissionStatus.PASSED_REVIEW)

        compensation_lane = LaneConfig("US Remote", "US", 72, None, "remote_only", 80_000, False, None)
        self.assertEqual(qualify(_candidate(fit=80, comp=79_999), lane=compensation_lane, run_date=date(2026, 1, 15)).admission_status, AdmissionStatus.EXCLUDED)
        self.assertEqual(qualify(_candidate(fit=80, comp=None), lane=compensation_lane, run_date=date(2026, 1, 15)).admission_status, AdmissionStatus.ADMITTED)

        freshness_lane = LaneConfig("US Remote", "US", 72, None, "remote_only", None, True, 7)
        self.assertEqual(qualify(_candidate(fit=80, posting=date(2026, 1, 1)), lane=freshness_lane, run_date=date(2026, 1, 15)).admission_status, AdmissionStatus.EXCLUDED)
        self.assertEqual(qualify(_candidate(fit=80, posting=None, freshness=FreshnessStatus.UNRESOLVED), lane=freshness_lane, run_date=date(2026, 1, 15)).admission_status, AdmissionStatus.PASSED_REVIEW)
        self.assertEqual(qualify(_candidate(fit=80, posting=date(2026, 1, 12)), lane=freshness_lane, run_date=date(2026, 1, 15)).admission_status, AdmissionStatus.ADMITTED)


if __name__ == "__main__":
    unittest.main()
