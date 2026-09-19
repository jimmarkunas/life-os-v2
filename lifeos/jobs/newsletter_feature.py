"""Thin Newsletter feature integration.

One RunContext, one bounded execution: Newsletter's already-parsed
observations -> Jobs adapter -> newsletter_contract.ingest() -> one
cleanup-safe signal. No second orchestration or persistence layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from lifeos.core.runtime import ExecutionResult, RunContext
from lifeos.jobs.models import NormalizedCandidate
from lifeos.jobs.newsletter_adapter import NewsletterJobsAdapter, _adapt_all, _unresolved_candidate
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

    counts = {
        disposition.value: sum(1 for result in results if result.disposition == disposition)
        for disposition in Disposition
    }
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
