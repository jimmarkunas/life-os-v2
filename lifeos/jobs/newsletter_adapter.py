"""Jobs-owned adapter: source vacancy observation -> NormalizedCandidate.

Newsletter extracts source facts; Jobs owns terminal vacancy resolution,
normalization, authoritative Fit, and fail-closed unresolved behavior.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from threading import Event, Lock

from lifeos.core.http import HttpClient, RetryPolicy
from lifeos.core.runtime import DeadlineExceeded, RunContext
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.identity import canonical_url
from lifeos.jobs.fit_title_semantics import classify_title
from lifeos.jobs.fit_requirement_extraction import extract_requirements
from lifeos.jobs.fit_scoreability import compile_fit
from lifeos.jobs.models import (
    Company,
    FitAuthority,
    FitEvidenceKind,
    FreshnessStatus,
    JobObservation,
    NormalizedCandidate,
    WorkMode,
)
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
        source_types.append({"greenhouse": "Greenhouse"}.get(provider.casefold(), provider))

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


MAX_ADAPT_WORKERS = 18


class TerminalEvidenceCache:
    """Execution-scoped single-flight terminal evidence cache."""

    def __init__(self) -> None:
        self.values: dict[str, TerminalVacancyEvidence | None] = {}
        self.in_flight: dict[str, Event] = {}
        self.lock = Lock()


def _unresolved_candidate(
    observation: SourceVacancyObservation,
    reason: str,
) -> NormalizedCandidate:
    return NormalizedCandidate(
        job=JobObservation(
            company=Company(name=observation.company or ""),
            role=observation.role or "",
            location=observation.location_text,
            work_mode=WorkMode.UNKNOWN,
            compensation_text=observation.compensation_text,
            compensation_minimum=None,
            posting_date=None,
            apply_url=None,
            source_lane="",
            provider_job_id=observation.provider_job_id,
        ),
        fit=None,
        market="",
        freshness_status=FreshnessStatus.UNRESOLVED,
        evidence_ref=observation.evidence_ref,
        unresolved_reason=reason,
    )


def _adapt_all(
    observations: tuple[SourceVacancyObservation, ...],
    *,
    adapter: NewsletterJobsAdapter,
    context: RunContext,
    max_workers: int,
) -> list[NormalizedCandidate]:
    if not observations:
        return []
    workers = max(1, min(int(max_workers), MAX_ADAPT_WORKERS))
    results: list[NormalizedCandidate | None] = [None] * len(observations)

    def _resolve_one(index: int, observation: SourceVacancyObservation) -> None:
        context.require_time()
        try:
            results[index] = adapter.to_jobs_candidate(observation)
        except Exception as exc:
            results[index] = _unresolved_candidate(observation, f"adapter raised {type(exc).__name__}")

    with ThreadPoolExecutor(max_workers=min(workers, len(observations))) as pool:
        futures = {
            pool.submit(_resolve_one, index, observation): index
            for index, observation in enumerate(observations)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                future.result()
            except DeadlineExceeded:
                if results[index] is None:
                    results[index] = _unresolved_candidate(observations[index], "run deadline exhausted")

    return [
        candidate if candidate is not None else _unresolved_candidate(observations[i], "adapter did not complete")
        for i, candidate in enumerate(results)
    ]


class NewsletterJobsAdapter:
    def __init__(self, config: NewsletterAdapterConfig, *, terminal_cache: TerminalEvidenceCache | None = None) -> None:
        self._config = config
        cache = terminal_cache or TerminalEvidenceCache()
        self._terminal_evidence_cache = cache.values
        self._terminal_evidence_in_flight = cache.in_flight
        self._terminal_evidence_lock = cache.lock

    def to_identity_candidate(self, observation: SourceVacancyObservation) -> NormalizedCandidate:
        cfg = self._config
        company = (observation.company or "").strip()
        role = (observation.role or "").strip()
        fatal_issues = fatal_issue_codes(observation.issues)
        job = JobObservation(
            company=Company(name=company), role=role, location=observation.location_text,
            work_mode=_infer_work_mode(observation.location_text), compensation_text=observation.compensation_text,
            compensation_minimum=_parse_compensation_minimum(observation.compensation_text), posting_date=None,
            apply_url=None, source_lane=cfg.source_lane, provider_job_id=observation.provider_job_id,
            source_provider=observation.source_provider,
        )
        return NormalizedCandidate(
            job=job, fit=None, market=cfg.market, freshness_status=FreshnessStatus.UNRESOLVED,
            evidence_ref=observation.evidence_ref,
            unresolved_reason=(f"source observation has unresolved issues: {', '.join(fatal_issues)}" if fatal_issues else None),
            source_types=_newsletter_source_types(provider=observation.source_provider, mailbox=observation.source_mailbox),
        )

    def to_jobs_candidate(self, observation: SourceVacancyObservation) -> NormalizedCandidate:
        identity_candidate = self.to_identity_candidate(observation)
        if identity_candidate.unresolved_reason:
            return identity_candidate
        cfg = self._config
        company = (observation.company or "").strip()
        role = (observation.role or "").strip()
        location = observation.location_text
        source_types = _newsletter_source_types(provider=observation.source_provider, mailbox=observation.source_mailbox)

        fatal_issues = fatal_issue_codes(observation.issues)
        if fatal_issues:
            return NormalizedCandidate(
                job=JobObservation(
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

        apply_url: str | None = None
        description_text: str | None = None
        source_description_text: str | None = observation.source_description_text
        posting_date: date | None = None
        unresolved_reason = None

        if observation.source_apply_url:
            evidence = self._terminal_evidence_for(
                observation.source_apply_url,
                company=observation.company,
                role=observation.role,
                provider_job_id=observation.provider_job_id,
            )
            if evidence is not None and evidence.canonical_url and (evidence.description_text or evidence.provider_source_description):
                apply_url = evidence.canonical_url
                description_text = evidence.description_text or evidence.provider_source_description
                posting_iso = parse_posting_date(evidence.posting_date_raw, reference_time=observation.source_received_at)
                posting_date = date.fromisoformat(posting_iso) if posting_iso else None

        if not apply_url or not description_text:
            unresolved_reason = "mandatory enrichment unresolved: actionable Apply URL and employer/ATS JD required"

        job = JobObservation(
            company=Company(name=company), role=role, location=location,
            work_mode=_infer_work_mode(location), compensation_text=observation.compensation_text,
            compensation_minimum=_parse_compensation_minimum(observation.compensation_text),
            posting_date=posting_date, apply_url=apply_url, source_lane=cfg.source_lane,
            provider_job_id=observation.provider_job_id, description_text=description_text,
            provider_score=observation.provider_score,
            source_provider=observation.source_provider,
        )

        fit: int | None = None
        fit_evidence_kind = FitEvidenceKind.NONE
        evidence_text = description_text if description_text and description_text.strip() else source_description_text
        fit_reason = None
        fit_authority = None
        if evidence_text and evidence_text.strip() and apply_url and description_text:
            fit_evidence_kind = (FitEvidenceKind.EMPLOYER_ATS_JD if description_text and description_text.strip() else FitEvidenceKind.SOURCE_DESCRIPTION)
            try:
                title = classify_title(role, cfg.fit_profile.title_patterns, cfg.fit_profile.direct_specialization_patterns)
                extracted = extract_requirements(evidence_text, cfg.fit_profile.dimension_patterns, cfg.fit_profile.evidence_patterns, cfg.fit_profile.material_patterns, cfg.fit_profile.ignore_patterns, cfg.fit_profile.hard_family_patterns)
                hard_family = title.evidence_class == "UNSUPPORTED" and any(
                    re.search(pattern, evidence_text, re.I)
                    for pattern in cfg.fit_profile.hard_family_patterns
                )
                compiled = compile_fit(title=role, title_semantics=title, requirements=list(extracted.requirements), evidence_kind=fit_evidence_kind, hard_family_mismatch=hard_family)
                fit = compiled.fit_result.final_score if compiled.fit_result else None
                fit_authority = compiled.authority
                if fit is None and compiled.reason:
                    fit_reason = compiled.reason
            except ValueError as exc:
                fit = None
                unresolved_reason = str(exc)
        return NormalizedCandidate(
            job=job, fit=fit, market=cfg.market,
            freshness_status=FreshnessStatus.UNRESOLVED,
            evidence_ref=observation.evidence_ref,
            unresolved_reason=unresolved_reason,
            **({"fit_authority": fit_authority} if fit_authority is not None else {}),
            fit_evidence_kind=fit_evidence_kind,
            fit_reason=fit_reason,
            source_types=source_types,
        )

    def _terminal_evidence_for(
        self,
        source_apply_url: str,
        *,
        company: str | None,
        role: str | None,
        provider_job_id: str | None,
    ) -> TerminalVacancyEvidence | None:
        # Protect only cache access. Holding this lock across network/browser
        # resolution serialized every distinct job URL and defeated _adapt_all's
        # worker pool under large Newsletter batches.
        cache_key = canonical_url(source_apply_url) or source_apply_url
        with self._terminal_evidence_lock:
            if cache_key in self._terminal_evidence_cache:
                return self._terminal_evidence_cache[cache_key]
            waiter = self._terminal_evidence_in_flight.get(cache_key)
            if waiter is None:
                waiter = Event()
                self._terminal_evidence_in_flight[cache_key] = waiter
                owner = True
            else:
                owner = False

        if not owner:
            waiter.wait()
            with self._terminal_evidence_lock:
                return self._terminal_evidence_cache.get(cache_key)

        evidence = None
        try:
            evidence = acquire_terminal_vacancy_evidence(
                source_apply_url,
                fetcher=self._config.fetcher,
                fallback_fetcher=self._config.fallback_fetcher,
                company=company,
                role=role,
                provider_job_id=provider_job_id,
            )
        except Exception:
            evidence = None
        finally:
            with self._terminal_evidence_lock:
                self._terminal_evidence_cache[cache_key] = evidence
                waiter = self._terminal_evidence_in_flight.pop(cache_key)
                waiter.set()
        return evidence
