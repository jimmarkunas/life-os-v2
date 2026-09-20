"""Newsletter-only production runtime extracted from the canonical US Remote runtime.

This module intentionally reuses the existing Gmail, Newsletter, Jobs, Notion,
and cleanup contracts.
"""
from __future__ import annotations

from time import perf_counter
from typing import Any

from lifeos.core.http import HttpClient
from lifeos.core.runtime import DeadlineExceeded, RunContext
from lifeos.integrations.gmail import GmailInboxMetadataPort, GmailMailboxTransport
from lifeos.integrations.notion import NotionTransport
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.newsletter_adapter import HttpClientFetcher, NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition, IngestResult, ingest
from lifeos.jobs.newsletter_adapter import _adapt_all
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.terminal_evidence import fallback_fetcher
from lifeos.jobs.us_remote_runtime import UsRemoteResult, _accepted_newsletter_message_ids, _preexclude
from lifeos.mail.router import MailRouter
from lifeos.newsletter.models import SourceVacancyObservation
from lifeos.newsletter.processor import NewsletterExecutionState, NewsletterProcessor
NEWSLETTER_BOUNDARY = "J Newsletters"


def execute_newsletter(
    *,
    context: RunContext,
    http: HttpClient,
    notion: NotionTransport,
    gmail: GmailMailboxTransport,
    browser_evidence: dict | None,
    lane: LaneConfig,
    lane_priority: dict[str, int],
    fit_profile: FitProfile,
    market: str,
    newsletter_source_lane: str,
    notion_job_ledger_data_source_id: str,
    inbox_start,
    inbox_mode: str,
    start,
    end,
    dry_run: bool,
) -> UsRemoteResult:
    """Process the complete routed Newsletter backlog through canonical Jobs."""
    fallback = fallback_fetcher(context, browser_evidence)
    timings: dict[str, float] = {}

    try:
        stage_started = perf_counter()
        mail_result = (
            MailRouter(newsletter_boundary=NEWSLETTER_BOUNDARY).route_window(
                [GmailInboxMetadataPort(gmail)], inbox_start, end
            )
            if not dry_run
            else None
        )
        timings["mail_stage"] = round(perf_counter() - stage_started, 3)

        stage_started = perf_counter()
        backlog_ids = gmail.enumerate_unprocessed_ids(NEWSLETTER_BOUNDARY)
        hydrated = gmail.hydrate_messages(backlog_ids)
        newsletter_result = NewsletterProcessor(boundary_name=NEWSLETTER_BOUNDARY).process_messages(hydrated)
        timings["newsletter_fetch_parse"] = round(perf_counter() - stage_started, 3)

        if dry_run:
            body = {
                "status": "PASS" if newsletter_result.state is NewsletterExecutionState.PASS else "DEGRADED",
                "dry_run": True,
                "elapsed_seconds": round(context.elapsed_seconds(), 3),
                "newsletter_observations": len(newsletter_result.observations),
                "browser_fallback_available": fallback is not None,
                "timings": timings,
            }
            return UsRemoteResult(body=body, indent=None, exit_code=0)

        stage_started = perf_counter()
        newsletter_to_resolve: list[SourceVacancyObservation] = []
        newsletter_preexcluded: list[IngestResult] = []
        for observation in newsletter_result.observations:
            disposition = _preexclude(observation, lane=lane, fit_profile=fit_profile)
            if disposition is None:
                newsletter_to_resolve.append(observation)
            else:
                newsletter_preexcluded.append(disposition)
        newsletter_terminal_required_total = len(newsletter_to_resolve)
        timings["cheap_prefilter"] = round(perf_counter() - stage_started, 3)

        repository = NotionCareerRepository(
            transport=notion,
            config=NotionCareerRepositoryConfig(data_source_id=notion_job_ledger_data_source_id),
        )
        adapter = NewsletterJobsAdapter(
            NewsletterAdapterConfig(
                fetcher=HttpClientFetcher(http=http, context=context),
                fallback_fetcher=fallback,
                fit_profile=fit_profile,
                market=market,
                source_lane=newsletter_source_lane,
            )
        )

        stage_started = perf_counter()
        candidates = _adapt_all(
            tuple(newsletter_to_resolve),
            adapter=adapter,
            context=context,
            max_workers=8,
        )
        timings["terminal_resolution"] = round(perf_counter() - stage_started, 3)

        stage_started = perf_counter()
        ingest_results = ingest(
            candidates,
            lane=lane,
            lane_priority=lane_priority,
            repository=repository,
            run_date=end.date(),
            context=context,
        )
        timings["reconcile_persist"] = round(perf_counter() - stage_started, 3)
        newsletter_results = newsletter_preexcluded + ingest_results
        completed_message_ids = {
            message.message_ref.split(":", 1)[1]
            for message in newsletter_result.messages
            if ":" in message.message_ref
        }
        attempted_observations = len(newsletter_result.observations)
        attempted_messages = len(completed_message_ids)
        deferred_observations = 0
        deferred_messages = 0
        terminal_admitted = len(newsletter_to_resolve)
        fully_accounted = len(newsletter_results) == attempted_observations
        unresolved = any(item.disposition is Disposition.REVIEW_DEGRADED for item in newsletter_results)
        observations_by_ref = {observation.evidence_ref: observation for observation in newsletter_result.observations}
        candidates_by_ref = {candidate.evidence_ref: candidate for candidate in candidates}
        results_by_ref = {result.evidence_ref: result for result in newsletter_results}
        diagnostic_observations = []
        for observation in newsletter_result.observations:
            candidate, result = candidates_by_ref.get(observation.evidence_ref), results_by_ref.get(observation.evidence_ref)
            terminal_evidence = adapter._terminal_evidence_cache.get(observation.source_apply_url) if observation.source_apply_url else None
            diagnostic_observations.append(
                {
                    "company": observation.company,
                    "role": observation.role,
                    "provider_job_id": observation.provider_job_id,
                    "source_description_present": bool(observation.source_description_text),
                    "source_description_length": len(observation.source_description_text or ""),
                    "fit": candidate.fit if candidate else None,
                    "fit_evidence_kind": candidate.fit_evidence_kind.value if candidate else None,
                    "fit_authority": candidate.fit_authority.value if candidate else None,
                    "unresolved_reason": candidate.unresolved_reason if candidate else None,
                    "disposition": result.disposition.value if result else None,
                    "disposition_detail": result.detail if result else None,
                    "stable_job_key": result.stable_job_key if result else None,
                    "linkedin_dom_candidates": [{"evidence_ref": observation.evidence_ref, "provider_job_id": observation.provider_job_id, **metadata} for metadata in (getattr(terminal_evidence, "linkedin_dom_candidates", ()) if observation.source_provider == "LinkedIn Jobs" else ())],
                    "linkedin_page_fingerprint": getattr(terminal_evidence, "linkedin_page_fingerprint", None) if observation.source_provider == "LinkedIn Jobs" else None,
                }
            )
        excluded = [
            {
                "company": getattr(observations_by_ref.get(result.evidence_ref), "company", None),
                "title": getattr(observations_by_ref.get(result.evidence_ref), "role", None),
                "evidence_ref": result.evidence_ref,
                "detail": result.detail,
            }
            for result in newsletter_results
            if result.disposition is Disposition.EXCLUDED
        ]
        newsletter_ok = newsletter_result.state is NewsletterExecutionState.PASS
        staging_ok = bool(mail_result and mail_result.checkpoint_safe)

        processed_errors: list[str] = []
        processed_count = 0
        stage_started = perf_counter()
        accepted_message_ids = _accepted_newsletter_message_ids(
            newsletter_result,
            newsletter_results,
            allowed_message_ids=completed_message_ids,
        )
        pending_cleanup = set(accepted_message_ids)
        for _attempt in range(2):
            if not pending_cleanup:
                break
            failed: set[str] = set()
            for message_id in pending_cleanup:
                try:
                    gmail.mark_newsletter_processed(message_id, NEWSLETTER_BOUNDARY)
                except Exception:
                    failed.add(message_id)
                else:
                    processed_count += 1
            pending_cleanup = failed
        processed_errors = ["cleanup-retry-exhausted"] * len(pending_cleanup)
        timings["newsletter_mark_processed"] = round(perf_counter() - stage_started, 3)

        stage_started = perf_counter()
        backlog_errors: list[str] = []
        try:
            backlog = gmail.newsletter_backlog_snapshot(NEWSLETTER_BOUNDARY, now=end)
            pending_source_messages = backlog.pending_source_messages
            oldest_pending_age_seconds = backlog.oldest_pending_age_seconds
        except Exception as exc:
            pending_source_messages = None
            oldest_pending_age_seconds = None
            backlog_errors.append(type(exc).__name__)
        timings["newsletter_backlog_health"] = round(perf_counter() - stage_started, 3)

        mail_lane_pass = (
            staging_ok
            and newsletter_ok
            and fully_accounted
            and not unresolved
            and not processed_errors
            and not backlog_errors
            and pending_source_messages == 0
        )
        body: dict[str, Any] = {
            "status": "PASS" if mail_lane_pass else "DEGRADED",
            "elapsed_seconds": round(context.elapsed_seconds(), 3),
            "mail": {
                "status": "PASS" if mail_lane_pass else "DEGRADED",
                "mode": inbox_mode,
                "scan_window_hours": round((end - inbox_start).total_seconds() / 3600.0, 3),
                "scanned": mail_result.scanned_count if mail_result else 0,
                "staged": sum(1 for row in mail_result.records if row.routed) if mail_result else 0,
                "staging_safe": staging_ok,
                "processed": processed_count,
                "processed_errors": len(processed_errors),
                "pending_source_messages": pending_source_messages,
                "oldest_pending_age_seconds": oldest_pending_age_seconds,
                "processed_observations": attempted_observations,
                "progress_messages": processed_count,
                "cleanup_safe": newsletter_ok and fully_accounted and not unresolved and not processed_errors,
                "error_codes": [f"{item.operation}:{item.detail}" for item in mail_result.errors[:10]] if mail_result else [],
                "backlog_error_codes": backlog_errors,
            },
            "newsletter": {
                "messages": len(newsletter_result.messages),
                "observations": len(newsletter_result.observations),
                "preexcluded": len(newsletter_preexcluded),
                "attempted_messages": attempted_messages,
                "attempted_observations": attempted_observations,
                "deferred_messages": deferred_messages,
                "deferred_observations": deferred_observations,
                "terminal_resolution_required": newsletter_terminal_required_total,
                "terminal_resolution_budget": None,
                "terminal_resolution_admitted": terminal_admitted,
                "state": newsletter_result.state.value,
                "error_codes": [f"{item.operation}:{item.detail}" for item in newsletter_result.errors[:10]],
            },
            "jobs": {
                "observations": attempted_observations,
                "fully_accounted": fully_accounted,
                "diagnostic_observations": diagnostic_observations,
                "dispositions": {
                    disposition.value: sum(item.disposition == disposition for item in newsletter_results)
                    for disposition in Disposition
                },
                "excluded": excluded,
            },
            "timings": timings,
            "browser_fallback_available": fallback is not None,
            "within_45s_benchmark": context.elapsed_seconds() <= 45.0,
        }
        return UsRemoteResult(body=body, indent=2, exit_code=0 if mail_lane_pass else 1)
    except DeadlineExceeded:
        return UsRemoteResult(
            body={
                "status": "DEGRADED",
                "reason": "execution-deadline-exhausted",
                "elapsed_seconds": round(context.elapsed_seconds(), 3),
                "timings": timings,
            },
            indent=None,
            exit_code=1,
        )
    except RuntimeError as exc:
        return UsRemoteResult(
            body={
                "status": "DEGRADED",
                "reason": type(exc).__name__,
                "elapsed_seconds": round(context.elapsed_seconds(), 3),
                "timings": timings,
            },
            indent=None,
            exit_code=1,
        )
