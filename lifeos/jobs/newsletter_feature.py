"""Thin Newsletter feature integration.

One RunContext, one bounded execution: Newsletter's already-parsed
observations -> Jobs adapter (parallel terminal-evidence resolution,
bounded by the same RunContext deadline every other stage respects) ->
newsletter_contract.ingest() (qualify/dedupe/persist) -> a single
cleanup-safe signal.

No workflow engine, manifest, handoff file, event bus, trigger file,
Continuity integration, recovery subsystem, or second persistence layer.
This module is pure composition of already-landed components; it contains
no parsing, no identity/qualification policy, and no Notion property
mapping of its own.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date

from lifeos.core.runtime import DeadlineExceeded, ExecutionResult, RunContext
from lifeos.jobs.models import Company, FreshnessStatus, Job, NormalizedCandidate, WorkMode
from lifeos.jobs.newsletter_adapter import NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition, IngestResult, ingest
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.repository import CareerRepository
from lifeos.newsletter.models import SourceVacancyObservation
from lifeos.newsletter.processor import NewsletterExecutionState, NewsletterProcessResult

DEFAULT_MAX_WORKERS = 8


@dataclass(frozen=True)
class NewsletterFeatureResult:
    execution: ExecutionResult
    ingest_results: tuple[IngestResult, ...]
    cleanup_safe: bool
    """True only when: every source observation was accounted for with
    exactly one terminal disposition, none of them is REVIEW_DEGRADED, the
    parse stage itself reported PASS, and (transitively, via ingest()'s own
    ReadBackMismatch handling) every required canonical write was read-back
    verified. Mail/Newsletter must treat this as the sole cleanup-safe
    signal and never recompute it."""


def _unresolved_candidate(observation: SourceVacancyObservation, reason: str) -> NormalizedCandidate:
    """Safety net only: adapter.to_jobs_candidate is designed to never raise,
    but a defensive fallback keeps one bad observation from crashing the
    whole batch and silently dropping every other candidate's result."""
    return NormalizedCandidate(
        job=Job(
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
    results: list[NormalizedCandidate | None] = [None] * len(observations)

    def _resolve_one(index: int, observation: SourceVacancyObservation) -> None:
        context.require_time()  # fail closed rather than start work past the deadline
        try:
            results[index] = adapter.to_jobs_candidate(observation)
        except Exception as exc:  # never let one observation crash the batch
            results[index] = _unresolved_candidate(observation, f"adapter raised {type(exc).__name__}")

    with ThreadPoolExecutor(max_workers=min(max_workers, len(observations))) as pool:
        futures = {pool.submit(_resolve_one, i, obs): i for i, obs in enumerate(observations)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                future.result()
            except DeadlineExceeded:
                if results[index] is None:
                    results[index] = _unresolved_candidate(observations[index], "run deadline exhausted")

    return [c if c is not None else _unresolved_candidate(observations[i], "adapter did not complete") for i, c in enumerate(results)]


def run_newsletter_feature(
    process_result: NewsletterProcessResult,
    *,
    adapter: NewsletterJobsAdapter,
    lane: LaneConfig,
    lane_priority: dict[str, int],
    repository: CareerRepository,
    run_date: date,
    context: RunContext,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> NewsletterFeatureResult:
    observations = process_result.observations

    if process_result.state is NewsletterExecutionState.DEGRADED and not observations:
        return NewsletterFeatureResult(
            execution=ExecutionResult.degraded(code="newsletter-parse-degraded", observations=0),
            ingest_results=(),
            cleanup_safe=False,
        )

    try:
        candidates = _adapt_all(observations, adapter=adapter, context=context, max_workers=max_workers)
    except DeadlineExceeded:
        return NewsletterFeatureResult(
            execution=ExecutionResult.degraded(code="deadline-exceeded", observations=len(observations)),
            ingest_results=(),
            cleanup_safe=False,
        )

    results = ingest(candidates, lane=lane, lane_priority=lane_priority, repository=repository, run_date=run_date)

    fully_accounted = len(results) == len(observations)
    no_review_degraded = all(r.disposition != Disposition.REVIEW_DEGRADED for r in results)
    parser_passed = process_result.state is NewsletterExecutionState.PASS
    cleanup_safe = fully_accounted and no_review_degraded and parser_passed

    counts = {d.value: sum(1 for r in results if r.disposition == d) for d in Disposition}
    if cleanup_safe:
        execution = ExecutionResult.passed(code="cleanup-safe", observations=len(observations), **counts)
    else:
        execution = ExecutionResult.degraded(
            code="not-cleanup-safe",
            observations=len(observations),
            fully_accounted=fully_accounted,
            no_review_degraded=no_review_degraded,
            parser_passed=parser_passed,
            **counts,
        )

    return NewsletterFeatureResult(execution=execution, ingest_results=tuple(results), cleanup_safe=cleanup_safe)
