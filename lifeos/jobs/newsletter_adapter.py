"""Jobs-owned adapter: SourceVacancyObservation -> NormalizedCandidate.

Implements `lifeos.newsletter.jobs_seam.JobsCandidateAdapter[NormalizedCandidate]`
structurally (the seam is a Protocol; this class satisfies it by shape, with
no import of Newsletter's internal parsing/routing logic -- only its public
model dataclass).

Jobs owns everything downstream of a raw source observation: final
employer/ATS resolution (terminal_evidence.py), Posting Date interpretation
(terminal_evidence.parse_posting_date), work-mode/compensation
normalization needed for qualification, canonical Job construction, and the
authoritative Fit score (fit_scoring.py, generic mechanics + an injected
private FitProfile per D-011). Newsletter never computes any of this.

`to_jobs_candidate` never raises and never drops an observation -- when
required evidence (final employer/ATS resolution) cannot be established, it
returns a NormalizedCandidate with `unresolved_reason` set, which
newsletter_contract.ingest() routes straight to REVIEW_DEGRADED. This keeps
the adapter safe to call from Newsletter's `adapt_for_jobs()` (a plain,
non-exception-handling map) while still guaranteeing terminal, non-silent
degraded behavior for every input.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from lifeos.core.http import HttpClient, RetryPolicy
from lifeos.core.runtime import RunContext
from lifeos.jobs.fit_scoring import FitEvidence, FitProfile
from lifeos.jobs.fit_scoring import score as score_fit
from lifeos.jobs.models import Company, FreshnessStatus, Job, NormalizedCandidate, WorkMode
from lifeos.jobs.terminal_evidence import (
    Fetcher,
    FetchResponse,
    acquire_terminal_vacancy_evidence,
    parse_posting_date,
)
from lifeos.newsletter.models import SourceVacancyObservation

_COMPENSATION_NUMBER = re.compile(r"\$?\s*([\d][\d,]*)(\s*[kK])?")


class HttpClientFetcher:
    """Adapts Platform Core's HttpClient/RunContext to terminal_evidence's
    narrow Fetcher Protocol. No retry/backoff layer is added here -- a
    single bounded attempt per call, since terminal_evidence.py's own hop
    budget is the only retry-shaped behavior this resolution path uses.

    Uses Platform Core's HttpResponse.final_url (the actual post-redirect
    URL) so canonical identity resolves against the real employer/ATS
    destination rather than the original tracking URL, even for a direct
    non-intermediary 30x redirect.
    """

    def __init__(self, *, http: HttpClient, context: RunContext, timeout_seconds: float = 10.0) -> None:
        self._http = http
        self._context = context
        self._timeout_seconds = timeout_seconds

    def get(self, url: str) -> FetchResponse:
        response = self._http.request(
            self._context,
            "GET",
            url,
            timeout_seconds=self._timeout_seconds,
            retry=RetryPolicy(max_attempts=1),
        )
        body = response.body.decode("utf-8", errors="replace")
        return FetchResponse(final_url=response.final_url or url, body=body)


def _infer_work_mode(location_text: str | None) -> WorkMode:
    if not location_text:
        return WorkMode.UNKNOWN
    low = location_text.lower()
    if "remote" in low:
        return WorkMode.REMOTE
    if "hybrid" in low:
        return WorkMode.HYBRID
    if any(term in low for term in ("on-site", "onsite", "on site")):
        return WorkMode.ONSITE
    return WorkMode.UNKNOWN


def _parse_compensation_minimum(text: str | None) -> int | None:
    """Smallest deterministic reading of an explicit compensation floor.
    Never fabricates a number when the text is ambiguous/absent -- returns
    None, which qualification.py already treats as "missing compensation
    allowed," never as zero."""
    if not text:
        return None
    match = _COMPENSATION_NUMBER.search(text)
    if not match:
        return None
    digits = match.group(1).replace(",", "")
    try:
        value = int(digits)
    except ValueError:
        return None
    if match.group(2):  # a "k"/"K" suffix means thousands
        value *= 1_000
    return value


@dataclass(frozen=True)
class NewsletterAdapterConfig:
    fetcher: Fetcher
    fit_profile: FitProfile
    market: str
    source_lane: str


class NewsletterJobsAdapter:
    def __init__(self, config: NewsletterAdapterConfig) -> None:
        self._config = config

    def to_jobs_candidate(self, observation: SourceVacancyObservation) -> NormalizedCandidate:
        cfg = self._config
        company = (observation.company or "").strip()
        role = (observation.role or "").strip()
        location = observation.location_text

        if observation.issues:
            # Unresolved source-side parse/evidence issues are never
            # silently accepted -- this observation can never become
            # CREATED/UPDATED, only REVIEW_DEGRADED. Terminal evidence
            # resolution is skipped entirely; the source itself is not
            # trustworthy enough to spend the resolution budget on.
            return NormalizedCandidate(
                job=Job(
                    company=Company(name=company),
                    role=role,
                    location=location,
                    work_mode=_infer_work_mode(location),
                    compensation_text=observation.compensation_text,
                    compensation_minimum=None,
                    posting_date=None,
                    apply_url=None,
                    source_lane=cfg.source_lane,
                    provider_job_id=observation.provider_job_id,
                ),
                fit=None,
                market=cfg.market,
                freshness_status=FreshnessStatus.UNRESOLVED,
                evidence_ref=observation.evidence_ref,
                unresolved_reason=f"source observation has unresolved issues: {', '.join(observation.issues)}",
            )

        unresolved_reason: str | None = None
        apply_url: str | None = None
        description_text: str | None = None
        posting_date: date | None = None

        if not observation.source_apply_url:
            unresolved_reason = "no source apply URL to resolve"
        else:
            try:
                evidence = acquire_terminal_vacancy_evidence(observation.source_apply_url, fetcher=cfg.fetcher)
            except Exception as exc:  # a resolution failure is REVIEW_DEGRADED, never a crash
                evidence = None
                unresolved_reason = f"final employer/ATS resolution raised {type(exc).__name__}"
            if evidence is None:
                unresolved_reason = unresolved_reason or "final employer/ATS evidence could not be resolved"
            else:
                apply_url = evidence.canonical_url
                description_text = evidence.description_text
                posting_iso = parse_posting_date(evidence.posting_date_raw, reference_time=observation.source_received_at)
                posting_date = date.fromisoformat(posting_iso) if posting_iso else None

        job = Job(
            company=Company(name=company),
            role=role,
            location=location,
            work_mode=_infer_work_mode(location),
            compensation_text=observation.compensation_text,
            compensation_minimum=_parse_compensation_minimum(observation.compensation_text),
            posting_date=posting_date,
            apply_url=apply_url,
            source_lane=cfg.source_lane,
            provider_job_id=observation.provider_job_id,
            description_text=description_text,
            provider_score=observation.provider_score,
        )

        fit: int | None = None
        if not unresolved_reason:
            fit_evidence = FitEvidence(role=role, description_text=description_text or "", location_text=location or "")
            fit = score_fit(fit_evidence, profile=cfg.fit_profile).score

        return NormalizedCandidate(
            job=job,
            fit=fit,
            market=cfg.market,
            freshness_status=FreshnessStatus.UNRESOLVED,
            evidence_ref=observation.evidence_ref,
            unresolved_reason=unresolved_reason,
        )
