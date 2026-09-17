"""Synthetic fixtures only. No real company names, URLs, or provider IDs."""
from __future__ import annotations

from datetime import date

from lifeos.jobs.models import Company, FitAuthority, FreshnessStatus, Job, NormalizedCandidate, WorkMode
from lifeos.jobs.qualification import LaneConfig

RUN_DATE = date(2026, 1, 15)

REMOTE_LANE = LaneConfig(
    name="Synthetic-Remote",
    market="Synthetic-US",
    fit_floor=78,
    target_review_floor=None,
    work_mode_policy="remote_only",
    compensation_floor=80_000,
    freshness_gate=True,
    freshness_max_days=7,
)

ANY_MODE_LANE = LaneConfig(
    name="Synthetic-Any",
    market="Synthetic-Other",
    fit_floor=78,
    target_review_floor=68,
    work_mode_policy="any",
    compensation_floor=None,
    freshness_gate=False,
    freshness_max_days=None,
    is_target_bucket=True,
)


def make_job(
    *,
    company_name: str = "Acme Synthetic Co",
    role: str = "Synthetic Engineer",
    location: str | None = "Remote - Synthetic Country",
    work_mode: WorkMode = WorkMode.REMOTE,
    compensation_text: str | None = "$100,000 - $120,000",
    compensation_minimum: int | None = 100_000,
    posting_date: date | None = RUN_DATE,
    apply_url: str | None = "https://synthetic-boards.example/jobs/12345?utm_source=test",
    source_lane: str = "Synthetic-Remote",
    provider_job_id: str | None = None,
    canonical_identity: str | None = None,
    source_provider: str | None = None,
) -> Job:
    return Job(
        company=Company(name=company_name),
        role=role,
        location=location,
        work_mode=work_mode,
        compensation_text=compensation_text,
        compensation_minimum=compensation_minimum,
        posting_date=posting_date,
        apply_url=apply_url,
        source_lane=source_lane,
        provider_job_id=provider_job_id,
        canonical_identity=canonical_identity,
        source_provider=source_provider,
    )


def make_candidate(
    *,
    job: Job | None = None,
    fit: int | None = 85,
    market: str = "Synthetic-US",
    freshness_status: FreshnessStatus = FreshnessStatus.FRESH,
    evidence_ref: str = "synthetic:evidence:1",
    fit_authority: FitAuthority = FitAuthority.NON_AUTHORITATIVE,
) -> NormalizedCandidate:
    return NormalizedCandidate(
        job=job or make_job(),
        fit=fit,
        market=market,
        freshness_status=freshness_status,
        evidence_ref=evidence_ref,
        fit_authority=fit_authority,
    )
