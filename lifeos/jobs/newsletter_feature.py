"""Thin Newsletter feature integration."""
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
MAX_ADAPT_WORKERS = 8


@dataclass(frozen=True)
class NewsletterFeatureResult:
    execution: ExecutionResult
    ingest_results: tuple[IngestResult, ...]
    cleanup_safe: bool


def _unresolved_candidate(observation: SourceVacancyObservation, reason: str) -> NormalizedCandidate:
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
    workers = max(1, min(int(max_workers), MAX_ADAPT_WORKERS))
    results: list[NormalizedCandidate | None] = [None] * len(observations)

    def _resolve_one(index: int, observation: SourceVacancyObservation) -> None:
        context.require_time()
        try:
            results[index] = adapter.to_jobs_candidate(observation)
        except Exception as exc:
            results[index] = _unresolved_candidate(observation, f"adapter raised {type(exc).__name__}")

    with ThreadPoolExecutor(max_workers=min(workers, len(observations))) as pool:
        for chunk_start in range(0, len(observations), workers):
            chunk = observations[chunk_start : chunk_start + workers]
            futures = {
                pool.submit(_resolve_one, chunk_start + offset, observation): chunk_start + offset
                for offset, observation in enumerate(chunk)
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

    candidates = _adapt_all(observations, adapter=adapter, context=context, max_workers=max_workers)
    results = ingest(
        candidates,
        lane=lane,
        lane_priority=lane_priority,
        repository=repository,
        run_date=run_date,
        context=context,
    )

    fully_accounted = len(results) == len(observations)
    no_review_degraded = all(result.disposition != Disposition.REVIEW_DEGRADED for result in results)
    parser_passed = process_result.state is NewsletterExecutionState.PASS
    cleanup_safe = fully_accounted and no_review_degraded and parser_passed

    counts = {disposition.value: sum(1 for result in results if result.disposition == disposition) for disposition in Disposition}
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
