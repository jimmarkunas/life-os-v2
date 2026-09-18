"""Newsletter high-volume reliability mechanics for the US Remote runtime.

Gmail remains the only durable source queue. This module owns bounded admission,
run-local terminal-fetch convergence, and conservative reuse of already-complete
canonical Jobs. It stores no cursor/checkpoint and creates no second queue.
"""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, replace
from time import perf_counter
from threading import Lock
from typing import Sequence

from lifeos.core.runtime import RunContext
from lifeos.integrations.gmail import GmailMailboxTransport
from lifeos.jobs.identity import canonical_url, derive_identity_evidence
from lifeos.jobs.lifecycle import LifecycleRecord
from lifeos.jobs.models import Company, FitAuthority, FreshnessStatus, Job, NormalizedCandidate, WorkMode
from lifeos.jobs.newsletter_adapter import NewsletterJobsAdapter
from lifeos.jobs.newsletter_feature import _adapt_all
from lifeos.jobs.repository import CareerRepository
from lifeos.jobs.terminal_evidence import FetchResponse, Fetcher, is_provider_intermediary_source
from lifeos.newsletter.models import MessageParseResult, ParseState, SourceVacancyObservation
from lifeos.newsletter.processor import (
    NewsletterError,
    NewsletterExecutionState,
    NewsletterProcessResult,
    NewsletterProcessor,
    NewsletterTimings,
)

# Leave deterministic room for Web acquisition + terminal resolution + one shared
# Jobs ingest/cleanup. Observation reserve grows with admitted workload rather
# than admitting a fixed message count.
FINALIZATION_BASE_RESERVE_SECONDS = 12.0
PER_OBSERVATION_RESERVE_SECONDS = 0.20
MAX_OBSERVATION_RESERVE_SECONDS = 90.0
NEXT_MESSAGE_ADMISSION_SECONDS = 2.0


@dataclass(frozen=True)
class NewsletterDrainResult:
    process_result: NewsletterProcessResult
    pending_before: tuple[str, ...]
    admitted_message_ids: tuple[str, ...]
    admitted_observations: int


@dataclass(frozen=True)
class CandidatePreparationResult:
    candidates: tuple[NormalizedCandidate, ...]
    canonical_reuse_count: int


class MemoizingFetcher(Fetcher):
    """Run-local exact-URL fetch convergence; never persists evidence."""

    def __init__(self, wrapped: Fetcher) -> None:
        self._wrapped = wrapped
        self._lock = Lock()
        self._futures: dict[str, Future[FetchResponse]] = {}

    def get(self, url: str) -> FetchResponse:
        with self._lock:
            future = self._futures.get(url)
            owner = future is None
            if future is None:
                future = Future()
                self._futures[url] = future
        if owner:
            try:
                future.set_result(self._wrapped.get(url))
            except BaseException as exc:
                future.set_exception(exc)
        return future.result()


def _finalization_reserve(observation_count: int) -> float:
    observation_reserve = min(
        MAX_OBSERVATION_RESERVE_SECONDS,
        max(0, observation_count) * PER_OBSERVATION_RESERVE_SECONDS,
    )
    return FINALIZATION_BASE_RESERVE_SECONDS + observation_reserve


def _safe_error_detail(exc: Exception) -> str:
    return type(exc).__name__


def drain_newsletter_backlog(
    gmail: GmailMailboxTransport,
    *,
    processor: NewsletterProcessor,
    boundary_name: str,
    context: RunContext,
) -> NewsletterDrainResult:
    """Enumerate the complete Gmail backlog, then hydrate/parse only the
    oldest deterministic prefix that fits the current runtime reserve.

    Selection state is never persisted. A later run simply re-enumerates Gmail
    and starts with the oldest message that still lacks Processed.
    """
    total_started = perf_counter()
    fetch_seconds = 0.0
    parse_seconds = 0.0
    admitted_ids: list[str] = []
    parsed_messages: list[MessageParseResult] = []
    errors: list[NewsletterError] = []

    try:
        pending = tuple(gmail.enumerate_unprocessed_ids(boundary_name))
    except Exception as exc:
        errors.append(NewsletterError(gmail.mailbox, "enumerate", _safe_error_detail(exc)))
        result = NewsletterProcessResult(
            NewsletterExecutionState.DEGRADED,
            (),
            tuple(errors),
            NewsletterTimings(0.0, 0.0, perf_counter() - total_started),
        )
        return NewsletterDrainResult(result, (), (), 0)

    observation_count = 0
    for message_id in pending:
        required = _finalization_reserve(observation_count) + NEXT_MESSAGE_ADMISSION_SECONDS
        if context.remaining_seconds() <= required:
            break

        fetch_started = perf_counter()
        try:
            hydrated = tuple(gmail.hydrate_messages((message_id,)))
        except Exception as exc:
            fetch_seconds += perf_counter() - fetch_started
            errors.append(NewsletterError(gmail.mailbox, "fetch", _safe_error_detail(exc)))
            break
        fetch_seconds += perf_counter() - fetch_started
        if len(hydrated) != 1:
            errors.append(NewsletterError(gmail.mailbox, "fetch", "message-hydration-incomplete"))
            break

        parse_started = perf_counter()
        parsed = processor.process_messages(hydrated)
        parse_seconds += perf_counter() - parse_started
        admitted_ids.append(message_id)
        parsed_messages.extend(parsed.messages)
        errors.extend(parsed.errors)
        observation_count += len(parsed.observations)
        if parsed.state is NewsletterExecutionState.DEGRADED:
            break

    state = (
        NewsletterExecutionState.PASS
        if not errors and all(message.state is ParseState.PASS for message in parsed_messages)
        else NewsletterExecutionState.DEGRADED
    )
    result = NewsletterProcessResult(
        state,
        tuple(sorted(parsed_messages, key=lambda item: item.message_ref)),
        tuple(errors),
        NewsletterTimings(fetch_seconds, parse_seconds, perf_counter() - total_started),
    )
    return NewsletterDrainResult(result, pending, tuple(admitted_ids), observation_count)


def oldest_pending_age_seconds(
    gmail: GmailMailboxTransport,
    pending_ids: Sequence[str],
    *,
    known_messages: Sequence[MessageParseResult],
    now,
) -> float | None:
    """Return authoritative Gmail age for the oldest still-pending source.

    Admitted messages already carry their source timestamp only inside parser
    evidence, so for an unadmitted oldest item use one bounded Gmail hydration.
    This adds no state and runs only while backlog remains.
    """
    if not pending_ids:
        return None
    oldest_id = pending_ids[0]
    hydrated = tuple(gmail.hydrate_messages((oldest_id,)))
    if len(hydrated) != 1:
        raise RuntimeError("oldest pending Gmail message unavailable")
    received_at = hydrated[0].received_at
    return max(0.0, (now - received_at).total_seconds())


def _probe_job(observation: SourceVacancyObservation) -> Job:
    return Job(
        company=Company(name=(observation.company or "").strip()),
        role=(observation.role or "").strip(),
        location=observation.location_text,
        work_mode=WorkMode.UNKNOWN,
        compensation_text=observation.compensation_text,
        compensation_minimum=None,
        posting_date=None,
        apply_url=None,
        source_lane="",
        provider_job_id=observation.provider_job_id,
        source_provider=observation.source_provider,
    )


def _complete_canonical(record: LifecycleRecord) -> bool:
    opportunity = record.opportunity
    job = opportunity.job
    return bool(
        job.apply_url
        and job.description_text
        and job.posting_date
        and opportunity.fit is not None
        and opportunity.fit_authority is FitAuthority.AUTHORITATIVE
    )


def _source_types(observation: SourceVacancyObservation) -> tuple[str, ...]:
    values: list[str] = []
    provider = observation.source_provider.strip()
    if provider:
        values.append(provider)
    mailbox = observation.source_mailbox.casefold()
    values.append("Outlook Alert" if "outlook" in mailbox else "Gmail Alert")
    return tuple(dict.fromkeys(values))


def _candidate_from_existing(
    observation: SourceVacancyObservation,
    record: LifecycleRecord,
    *,
    market: str,
    source_lane: str,
) -> NormalizedCandidate:
    opportunity = record.opportunity
    existing_job = opportunity.job
    job = replace(
        existing_job,
        canonical_identity=opportunity.stable_job_key,
        source_lane=source_lane,
        provider_job_id=observation.provider_job_id or existing_job.provider_job_id,
        provider_score=(
            observation.provider_score
            if observation.provider_score is not None
            else existing_job.provider_score
        ),
        source_provider=observation.source_provider or existing_job.source_provider,
    )
    return NormalizedCandidate(
        job=job,
        fit=opportunity.fit,
        market=market,
        freshness_status=FreshnessStatus.UNRESOLVED,
        evidence_ref=observation.evidence_ref,
        unresolved_reason=None,
        fit_authority=opportunity.fit_authority,
        source_types=_source_types(observation),
    )


def prepare_newsletter_candidates(
    observations: Sequence[SourceVacancyObservation],
    *,
    adapter: NewsletterJobsAdapter,
    repository: CareerRepository,
    context: RunContext,
    market: str,
    source_lane: str,
    max_workers: int = 8,
) -> CandidatePreparationResult:
    """Reuse only already-complete canonical Jobs that can be matched without
    weakening terminal URL trust; adapt every other observation normally.

    Safe reuse is limited to an exact existing canonical Apply URL match, or a
    fallback identity match when the new observation has no source URL at all.
    Discovery/intermediary URLs always continue through normal terminal
    resolution even if legacy persisted state happens to contain the same URL.
    """
    ordered = tuple(observations)
    if not ordered:
        return CandidatePreparationResult((), 0)

    fallback_key_by_index: dict[int, str] = {}
    direct_url_by_index: dict[int, str] = {}
    for index, observation in enumerate(ordered):
        if observation.source_apply_url:
            if is_provider_intermediary_source(observation.source_apply_url):
                continue
            url = canonical_url(observation.source_apply_url)
            if url:
                direct_url_by_index[index] = url
            continue
        evidence = derive_identity_evidence(_probe_job(observation))
        if evidence.stable_job_keys:
            fallback_key_by_index[index] = evidence.stable_job_keys[-1]

    records_by_key: dict[str, LifecycleRecord] = {}
    records_by_url: dict[str, LifecycleRecord] = {}
    try:
        if fallback_key_by_index:
            records_by_key = repository.get_many(list(dict.fromkeys(fallback_key_by_index.values())))
        if direct_url_by_index:
            records_by_url = repository.get_by_apply_urls(list(dict.fromkeys(direct_url_by_index.values())))
    except Exception:
        records_by_key = {}
        records_by_url = {}

    candidates: list[NormalizedCandidate | None] = [None] * len(ordered)
    unresolved_observations: list[SourceVacancyObservation] = []
    unresolved_indices: list[int] = []
    reused = 0

    for index, observation in enumerate(ordered):
        matches: dict[str, LifecycleRecord] = {}
        key = fallback_key_by_index.get(index)
        if key and key in records_by_key:
            record = records_by_key[key]
            matches[record.opportunity.stable_job_key] = record
        url = direct_url_by_index.get(index)
        if url and url in records_by_url:
            record = records_by_url[url]
            matches[record.opportunity.stable_job_key] = record
        if len(matches) == 1:
            record = next(iter(matches.values()))
            if _complete_canonical(record):
                candidates[index] = _candidate_from_existing(
                    observation,
                    record,
                    market=market,
                    source_lane=source_lane,
                )
                reused += 1
                continue
        unresolved_indices.append(index)
        unresolved_observations.append(observation)

    adapted = _adapt_all(
        tuple(unresolved_observations),
        adapter=adapter,
        context=context,
        max_workers=max_workers,
    )
    for index, candidate in zip(unresolved_indices, adapted):
        candidates[index] = candidate

    assert all(candidate is not None for candidate in candidates)
    return CandidatePreparationResult(tuple(candidates), reused)
