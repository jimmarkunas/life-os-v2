"""Jobs-owned adapter: source vacancy observation -> NormalizedCandidate.

Newsletter extracts source facts; Jobs owns terminal vacancy resolution,
normalization, authoritative Fit, and fail-closed unresolved behavior.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from threading import Lock

from lifeos.core.http import HttpClient, RetryPolicy
from lifeos.core.runtime import RunContext
from lifeos.jobs.fit_scoring import FitEvidence, FitProfile
from lifeos.jobs.fit_scoring import score as score_fit
from lifeos.jobs.models import Company, FitAuthority, FreshnessStatus, Job, NormalizedCandidate, WorkMode
from lifeos.jobs.terminal_evidence import (
    Fetcher,
    FetchResponse,
    TerminalVacancyEvidence,
    acquire_terminal_vacancy_evidence,
    parse_posting_date,
)
from lifeos.newsletter.models import SourceVacancyObservation, fatal_issue_codes

_COMPENSATION_NUMBER = re.compile(r"\$?\s*([\d][\d,]*)(\s*[kK])?")


class HttpClientFetcher:
    """Bounded HTTP Fetcher backed by Platform Core."""
    def __init__(self, *, http: HttpClient, context: RunContext, timeout_seconds: float = 10.0) -> None:
        self._http = http; self._context = context; self._timeout_seconds = timeout_seconds

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
    return value * 1_000 if match.group(2) else value


def _newsletter_source_types(*, provider: str, mailbox: str) -> tuple[str, ...]:
    source_types: list[str] = []
    provider = provider.strip()
    if provider:
        source_types.append(provider)

    mailbox_normalized = mailbox.casefold()
    if "outlook" in mailbox_normalized:
        source_types.append("Outlook Alert")
    else:
        source_types.append("Gmail Alert")

    return tuple(dict.fromkeys(source_types))


@dataclass(frozen=True)
class NewsletterAdapterConfig:
    fetcher: Fetcher
    fit_profile: FitProfile
    market: str
    source_lane: str
    fallback_fetcher: Fetcher | None = None


class NewsletterJobsAdapter:
    def __init__(self, config: NewsletterAdapterConfig) -> None:
        self._config = config
        self._terminal_evidence_cache: dict[str, TerminalVacancyEvidence | None] = {}
        self._terminal_evidence_lock = Lock()

    def to_jobs_candidate(self, observation: SourceVacancyObservation) -> NormalizedCandidate:
        cfg = self._config
        company = (observation.company or "").strip()
        role = (observation.role or "").strip()
        location = observation.location_text
        source_types = _newsletter_source_types(provider=observation.source_provider, mailbox=observation.source_mailbox)

        fatal_issues = fatal_issue_codes(observation.issues)
        if fatal_issues:
            return NormalizedCandidate(
                job=Job(
                    company=Company(name=company), role=role, location=location,
                    work_mode=_infer_work_mode(location), compensation_text=observation.compensation_text,
                    compensation_minimum=None, posting_date=None, apply_url=None,
                    source_lane=cfg.source_lane, provider_job_id=observation.provider_job_id,
                    source_provider=observation.source_provider,
                ),
                fit=None, market=cfg.market, freshness_status=FreshnessStatus.UNRESOLVED,
                evidence_ref=observation.evidence_ref,
                unresolved_reason=f"source observation has unresolved issues: {', '.join(fatal_issues)}",
                source_types=source_types,
            )

        # Enrichment failure is not identity failure: a missing source apply
        # URL, an unresolved final employer/ATS destination, or a raised
        # resolver exception all leave apply_url/description_text/
        # posting_date unset below, but never set unresolved_reason. Whether
        # this candidate can still be safely identified is decided later, by
        # identity.stable_job_key()'s own company+role+location fallback --
        # not here. unresolved_reason is reserved for observation.issues
        # above, which signals a parser-level identity problem.
        apply_url: str | None = None
        description_text: str | None = None
        posting_date: date | None = None

        if observation.source_apply_url:
            evidence = self._terminal_evidence_for(observation.source_apply_url)
            if evidence is not None:
                apply_url = evidence.canonical_url
                description_text = evidence.description_text
                posting_iso = parse_posting_date(evidence.posting_date_raw, reference_time=observation.source_received_at)
                posting_date = date.fromisoformat(posting_iso) if posting_iso else None

        job = Job(
            company=Company(name=company), role=role, location=location,
            work_mode=_infer_work_mode(location), compensation_text=observation.compensation_text,
            compensation_minimum=_parse_compensation_minimum(observation.compensation_text),
            posting_date=posting_date, apply_url=apply_url, source_lane=cfg.source_lane,
            provider_job_id=observation.provider_job_id, description_text=description_text,
            provider_score=observation.provider_score,
            source_provider=observation.source_provider,
        )

        # LIFE OS Fit is authoritative evidence only: score it from terminal
        # employer/ATS description text, never from weak source-card/title
        # text alone. When enrichment did not resolve, fit stays None and
        # qualify() routes the candidate to PASSED_REVIEW, never a silent
        # admission and never a fabricated score.
        fit: int | None = None
        if description_text:
            fit = score_fit(
                FitEvidence(role=role, description_text=description_text, location_text=location or ""),
                profile=cfg.fit_profile,
            ).score
        fit_authority = FitAuthority.AUTHORITATIVE if fit is not None else FitAuthority.NON_AUTHORITATIVE

        return NormalizedCandidate(
            job=job, fit=fit, market=cfg.market,
            freshness_status=FreshnessStatus.UNRESOLVED,
            evidence_ref=observation.evidence_ref,
            unresolved_reason=None,
            fit_authority=fit_authority,
            source_types=source_types,
        )

    def _terminal_evidence_for(self, source_apply_url: str) -> TerminalVacancyEvidence | None:
        # Protect only cache access. Holding this lock across network/browser
        # resolution serialized every distinct job URL and defeated _adapt_all's
        # worker pool under large Newsletter batches.
        with self._terminal_evidence_lock:
            if source_apply_url in self._terminal_evidence_cache:
                return self._terminal_evidence_cache[source_apply_url]

        try:
            evidence = acquire_terminal_vacancy_evidence(
                source_apply_url,
                fetcher=self._config.fetcher,
                fallback_fetcher=self._config.fallback_fetcher,
            )
        except Exception:
            evidence = None

        with self._terminal_evidence_lock:
            # A concurrent duplicate URL may have completed first. Preserve the
            # first cached result while allowing distinct URLs to resolve in parallel.
            return self._terminal_evidence_cache.setdefault(source_apply_url, evidence)
