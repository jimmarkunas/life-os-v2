from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace

from lifeos.jobs.identity import IdentityCollision, derive_identity_evidence, resolve_existing_identity
from lifeos.jobs.models import Company, FitAuthority, FreshnessStatus, JobObservation, NormalizedCandidate, WorkMode
from lifeos.jobs.newsletter_contract import Disposition, ingest
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.repository import InMemoryCareerRepository


LANE = LaneConfig("US Remote", "US", 72, None, "remote_only", None, False, None)

# This proof package is executed explicitly because the repository's CI guard
# forbids increasing the already-over-100 collected-test baseline.
__test__ = False


def observation(
    *,
    company: str = "Synthetic Employer",
    role: str = "Technical Program Manager",
    location: str = "United States",
    url: str | None = "https://boards.greenhouse.io/synthetic/jobs/123?utm_source=newsletter",
    provider: str = "Jobright",
    provider_id: str | None = "jobright-123",
    canonical_identity: str | None = None,
) -> JobObservation:
    return JobObservation(
        company=Company(company),
        role=role,
        location=location,
        work_mode=WorkMode.REMOTE,
        compensation_text=None,
        compensation_minimum=None,
        posting_date=None,
        apply_url=url,
        source_lane="US Remote",
        provider_job_id=provider_id,
        canonical_identity=canonical_identity,
        source_provider=provider,
    )


def candidate(ref: str, job: JobObservation) -> NormalizedCandidate:
    return NormalizedCandidate(job, 80, "US", FreshnessStatus.FRESH, ref, fit_authority=FitAuthority.AUTHORITATIVE)


class IdentityDedupeTests(unittest.TestCase):
    def test_two_providers_converge_and_merge_aliases_in_one_canonical_row(self):
        repo = InMemoryCareerRepository()
        results = ingest(
            [
                candidate("jobright", observation(provider="Jobright", provider_id="jr-123")),
                candidate("linkedin", observation(provider="LinkedIn Jobs", provider_id="li-456")),
            ],
            lane=LANE,
            lane_priority={"US Remote": 0},
            repository=repo,
            run_date=date(2026, 1, 15),
        )

        self.assertEqual(len(repo._store), 1)
        self.assertEqual(results[1].disposition, Disposition.DUPLICATE)
        row = next(iter(repo._store.values()))
        self.assertEqual(row.job.stable_job_key, "url:https://boards.greenhouse.io/synthetic/jobs/123")
        self.assertEqual(row.job.aliases, ("Synthetic Employer::jr-123", "Synthetic Employer::li-456"))

    def test_canonical_url_outranks_provider_native_ids(self):
        first = observation(provider="Jobright", provider_id="same-provider-id")
        second = observation(provider="LinkedIn Jobs", provider_id="different-provider-id")
        self.assertNotEqual(first.provider_job_id, second.provider_job_id)
        self.assertEqual(
            derive_identity_evidence(first).stable_job_keys[0],
            "url:https://boards.greenhouse.io/synthetic/jobs/123",
        )
        self.assertEqual(
            derive_identity_evidence(second).stable_job_keys[0],
            "url:https://boards.greenhouse.io/synthetic/jobs/123",
        )

    def test_legacy_provider_alias_reconciles_fallback_key_to_existing_row(self):
        incoming = observation(url=None, provider_id="legacy-123")
        evidence = derive_identity_evidence(incoming)
        legacy = SimpleNamespace(job=SimpleNamespace(stable_job_key="Synthetic Employer::legacy-123"))
        fallback = "synthetic employer|technical program manager|united states"
        current = SimpleNamespace(job=SimpleNamespace(stable_job_key=fallback))

        self.assertEqual(
            resolve_existing_identity(
                evidence,
                records_by_stable_key={"Synthetic Employer::legacy-123": legacy, fallback: current},
                records_by_apply_url={},
            ),
            "Synthetic Employer::legacy-123",
        )

    def test_competing_provider_aliases_fail_closed_as_identity_collision(self):
        incoming = observation(url=None, provider_id="legacy-123")
        evidence = derive_identity_evidence(incoming)
        with self.assertRaises(IdentityCollision):
            resolve_existing_identity(
                evidence,
                records_by_stable_key={
                    "Synthetic Employer::legacy-123": SimpleNamespace(job=SimpleNamespace(stable_job_key="legacy-row")),
                    "Synthetic Employer::other-123": SimpleNamespace(job=SimpleNamespace(stable_job_key="other-row")),
                    "synthetic employer|technical program manager|united states": SimpleNamespace(job=SimpleNamespace(stable_job_key="fallback-row")),
                },
                records_by_apply_url={},
            )

    def test_stronger_url_evidence_preserves_fallback_row(self):
        repo = InMemoryCareerRepository()
        first = ingest(
            [candidate("fallback", observation(url=None, provider_id="fallback-123"))],
            lane=LANE,
            lane_priority={"US Remote": 0},
            repository=repo,
            run_date=date(2026, 1, 15),
        )
        key = first[0].stable_job_key
        second = ingest(
            [candidate("stronger", observation(url="https://boards.greenhouse.io/synthetic/jobs/123", provider_id="ats-123"))],
            lane=LANE,
            lane_priority={"US Remote": 0},
            repository=repo,
            run_date=date(2026, 1, 16),
        )

        self.assertEqual(len(repo._store), 1)
        self.assertEqual(second[0].stable_job_key, key)
        self.assertEqual(next(iter(repo._store.values())).job.job.apply_url, "https://boards.greenhouse.io/synthetic/jobs/123")

    def test_replay_is_idempotent_and_unresolved_identity_is_reviewable(self):
        repo = InMemoryCareerRepository()
        valid = candidate("replay", observation())
        first = ingest([valid], lane=LANE, lane_priority={"US Remote": 0}, repository=repo, run_date=date(2026, 1, 15))
        second = ingest([valid], lane=LANE, lane_priority={"US Remote": 0}, repository=repo, run_date=date(2026, 1, 16))
        unresolved = ingest(
            [candidate("unresolved", observation(url=None, provider_id=None, role="", location=None))],
            lane=LANE,
            lane_priority={"US Remote": 0},
            repository=repo,
            run_date=date(2026, 1, 16),
        )

        self.assertEqual(len(repo._store), 1)
        self.assertEqual(first[0].stable_job_key, second[0].stable_job_key)
        self.assertEqual(second[0].disposition, Disposition.UPDATED)
        self.assertEqual(unresolved[0].disposition, Disposition.REVIEW_DEGRADED)
        self.assertIsNone(unresolved[0].stable_job_key)


if __name__ == "__main__":
    unittest.main()
