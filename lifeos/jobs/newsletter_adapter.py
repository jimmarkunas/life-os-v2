"""Jobs-owned adapter: SourceVacancyObservation -> NormalizedCandidate.

Newsletter owns source extraction. Jobs owns terminal employer/ATS resolution,
Posting Date interpretation, canonical Job construction, LIFE OS Fit, and
qualification.

A structurally valid supported Newsletter vacancy is never degraded merely
because terminal employer/ATS evidence cannot be acquired. This preserves the
proven v1 production behavior: keep the source observation, leave terminal URL
and Posting Date unresolved, score from the evidence that is actually present,
and let Jobs qualification route unresolved freshness/work-mode evidence to
Passed / Review. Source-side parse ambiguity still fails closed via
`unresolved_reason` and becomes REVIEW_DEGRADED.
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
from lifeos.jobs.terminal_evidence import Fetcher, FetchResponse, acquire_terminal_vacancy_evidence, parse_posting_date
from lifeos.newsletter.models import SourceVacancyObservation

_COMPENSATION_NUMBER = re.compile(r"\$?\s*([\d][\d,]*)(\s*[kK])?")


class HttpClientFetcher:
    """Adapt Platform Core HTTP/RunContext to terminal evidence's Fetcher."""

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
        return FetchResponse(
            final_url=response.final_url or url,
            body=response.body.decode("utf-8", errors="replace"),
        )


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
    if not text:
        return None
    match = _COMPENSATION_NUMBER.search(text)
    if not match:
        return None
    try:
        value = int(match.group(1).replace(",", ""))
    except ValueError:
        return None
    if match.group(2):
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

        # Source ambiguity is a genuine ingestion failure. Do not invent or
        # normalize through an observation the source parser itself could not prove.
        if observation.issues:
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

        apply_url: str | None = None
        description_text: str | None = None
        posting_date: date | None = None

        # Proven v1 behavior: terminal evidence enriches a valid source card,
        # but inability to acquire it does not invalidate the source vacancy.
        # Keep canonical URL/date unresolved and continue to qualification.
        if observation.source_apply_url:
            try:
                evidence = acquire_terminal_vacancy_evidence(observation.source_apply_url, fetcher=cfg.fetcher)
            except Exception:
                evidence = None
            if evidence is not None:
                apply_url = evidence.canonical_url
                description_text = evidence.description_text
                posting_iso = parse_posting_date(
                    evidence.posting_date_raw,
                    reference_time=observation.source_received_at,
                )
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

        # Fit is deterministic over whatever trustworthy evidence exists. When
        # the full employer JD is unavailable, role/location source evidence is
        # still valid input; freshness remains UNRESOLVED and qualification owns
        # the Passed / Review decision.
        fit = score_fit(
            FitEvidence(
                role=role,
                description_text=description_text or "",
                location_text=location or "",
            ),
            profile=cfg.fit_profile,
        ).score

        return NormalizedCandidate(
            job=job,
            fit=fit,
            market=cfg.market,
            freshness_status=FreshnessStatus.UNRESOLVED,
            evidence_ref=observation.evidence_ref,
            unresolved_reason=None,
        )
