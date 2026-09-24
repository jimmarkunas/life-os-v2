"""US Remote production runtime: coordinate Mail, Newsletter, Web, and Jobs."""
from __future__ import annotations

import json
from dataclasses import replace
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta
from time import perf_counter
from typing import Any

from lifeos.core.http import HttpClient
from lifeos.core.config import ConfigurationError
from lifeos.core.runtime import DeadlineExceeded, RunContext
from lifeos.integrations.gmail import GmailInboxMetadataPort, GmailMailboxTransport
from lifeos.integrations.notion import NotionTransport
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.newsletter_adapter import HttpClientFetcher, NewsletterAdapterConfig, NewsletterJobsAdapter, TerminalEvidenceCache
from lifeos.jobs.newsletter_contract import Disposition, IngestResult, derive_review_these_jobs, ingest, result_is_accounted
from lifeos.jobs.newsletter_adapter import _adapt_all
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.terminal_evidence import browser_evidence, fallback_fetcher
from lifeos.jobs.us_remote_acquisition import USRemoteAcquirer
from lifeos.mail.router import MailRouter
from lifeos.mail import DeterministicMailClassifier, MailMessage
from lifeos.newsletter.models import ParseState, SourceVacancyObservation
from lifeos.newsletter.processor import NewsletterExecutionState, NewsletterProcessor

NEWSLETTER_BOUNDARY = "J Newsletters"

_SOURCE_REGISTRY = Path(__file__).resolve().parents[2] / "contracts" / "us_remote_sources.json"
US_REMOTE_HTTP_CONCURRENCY = 18
_TERMINAL_RESOLUTION_WORKERS = US_REMOTE_HTTP_CONCURRENCY


def load_registry() -> dict:
    try:
        data = json.loads(_SOURCE_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("US Remote source registry unavailable") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("US Remote source registry invalid")
    return data


def _accepted_newsletter_message_ids(
    process_result,
    results: list[IngestResult],
    *,
    allowed_message_ids: set[str] | None = None,
) -> list[str]:
    by_ref = {result.evidence_ref: result for result in results}
    accepted: list[str] = []
    for message in process_result.messages:
        if message.state is not ParseState.PASS:
            continue
        message_results = [by_ref.get(observation.evidence_ref) for observation in message.observations]
        if any(not result_is_accounted(result) for result in message_results):
            continue
        if ":" not in message.message_ref:
            continue
        mailbox, message_id = message.message_ref.split(":", 1)
        if allowed_message_ids is not None and message_id not in allowed_message_ids:
            continue
        if mailbox == "gmail" and message_id:
            accepted.append(message_id)
    return accepted


@dataclass(frozen=True)
class RetentionResult:
    errors: tuple[str, ...] = ()
    candidate_count: int = 0
    automated_count: int = 0
    trashed_count: int = 0
    human_excluded_count: int = 0
    unresolved_count: int = 0


def _retain_processed_newsletters(gmail: GmailMailboxTransport, *, now: datetime) -> RetentionResult:
    failures: list[str] = []
    candidate_count = automated = trashed = human = unresolved = 0
    try:
        candidates = gmail.newsletter_retention_candidates(NEWSLETTER_BOUNDARY)
    except Exception as exc:
        return RetentionResult(errors=(f"enumeration:{type(exc).__name__}",))
    for fields in candidates:
        if now - fields["received_at"] < timedelta(days=60): continue
        candidate_count += 1
        message = MailMessage(provider="gmail", body_text="", **{key: fields[key] for key in ("message_id", "received_at", "sender", "subject", "headers")})
        classification = DeterministicMailClassifier().classify(message)
        if classification.mail_class.value == "human_hiring":
            human += 1; continue
        headers = {str(key).casefold(): str(value).casefold() for key, value in message.headers.items()}
        sender = message.sender.casefold()
        linkedin_automation = "linkedin.com" in sender and any(token in sender for token in ("job", "alert", "noreply", "no-reply"))
        has_automation = linkedin_automation and ("list-unsubscribe" in headers or "list-id" in headers or "auto-submitted" in headers or "precedence" in headers)
        if classification.mail_class.value != "automated_job_source" and not has_automation:
            unresolved += 1; failures.append("retention_unresolved"); continue
        automated += 1
        for _attempt in range(2):
            try:
                if gmail.trash_newsletter_message(message.message_id):
                    trashed += 1
                    break
            except Exception:
                pass
        else:
            failures.append("trash_or_readback")
    return RetentionResult(tuple(failures), candidate_count, automated, trashed, human, unresolved)


def _preexclude(
    observation: SourceVacancyObservation,
    *,
    lane: LaneConfig,
    fit_profile: FitProfile,
) -> IngestResult | None:
    """Return only exclusions that remain true under all possible enrichment.

    Terminal URL/description resolution is expensive. We may skip it only when
    source facts already prove the candidate can never enter the lane or review
    band. Ambiguous evidence still proceeds to canonical resolution.
    """
    location = (observation.location_text or "").casefold()
    if lane.work_mode_policy == "remote_only" and any(
        marker in location for marker in ("hybrid", "on-site", "onsite", "on site")
    ):
        return IngestResult(
            observation.evidence_ref,
            Disposition.EXCLUDED,
            None,
            "source location explicitly conflicts with remote-only lane",
        )

    return None


@dataclass(frozen=True)
class UsRemoteResult:
    """Serialized result of one execute_us_remote() run, plus its exit code."""

    body: dict[str, Any]
    indent: int | None
    exit_code: int


def execute_us_remote(
    *,
    context: RunContext,
    http: HttpClient,
    notion: NotionTransport,
    gmail: GmailMailboxTransport,
    registry: dict,
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
    web_since,
    dry_run: bool,
    full_web_sweep: bool,
) -> UsRemoteResult:
    """Newsletter + US Web acquire independently, then reconcile together
    through one shared Jobs ingest batch and one canonical Job Ledger.
    Confirmed automated mail is staged out of Inbox immediately; a second
    Gmail label marks accepted processing only after canonical
    persistence/read-back.
    """
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
        newsletter_result = NewsletterProcessor(boundary_name=NEWSLETTER_BOUNDARY).process_window(
            [gmail], start, end
        )
        timings["newsletter_fetch_parse"] = round(perf_counter() - stage_started, 3)

        stage_started = perf_counter()
        web_result = USRemoteAcquirer(
            context=context,
            http=http,
            fallback_fetcher=fallback,
        ).acquire(
            registry,
            browser_evidence=browser_evidence,
            since=web_since,
            full_sweep=full_web_sweep,
            now=end,
        )
        timings["web_acquire"] = round(perf_counter() - stage_started, 3)

        if dry_run:
            body = {
                "status": "PASS"
                if newsletter_result.state is NewsletterExecutionState.PASS and web_result.complete
                else "DEGRADED",
                "dry_run": True,
                "elapsed_seconds": round(context.elapsed_seconds(), 3),
                "newsletter_observations": len(newsletter_result.observations),
                "web_observations": len(web_result.observations),
                "web_sources_complete": sum(item.state == "COMPLETE" for item in web_result.sources),
                "web_sources_not_due": sum(item.state == "NOT_DUE" for item in web_result.sources),
                "web_sources_total": len(web_result.sources),
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
        web_to_resolve: list[SourceVacancyObservation] = []
        web_preexcluded: list[IngestResult] = []
        for observation in web_result.observations:
            disposition = _preexclude(observation, lane=lane, fit_profile=fit_profile)
            if disposition is None:
                web_to_resolve.append(observation)
            else:
                web_preexcluded.append(disposition)
        selected_newsletter_message_ids = {
            message.message_ref.split(":", 1)[1]
            for message in newsletter_result.messages
            if ":" in message.message_ref and message.message_ref.split(":", 1)[0] == "gmail"
        }
        web_deferred_observations = 0
        attempted_newsletter_observations = len(newsletter_result.observations)
        attempted_newsletter_messages = len(selected_newsletter_message_ids)
        deferred_newsletter_observations = 0
        deferred_newsletter_messages = 0
        timings["cheap_prefilter"] = round(perf_counter() - stage_started, 3)

        repository = NotionCareerRepository(
            transport=notion,
            config=NotionCareerRepositoryConfig(data_source_id=notion_job_ledger_data_source_id),
        )
        http_fetcher = HttpClientFetcher(http=http, context=context)
        terminal_cache = TerminalEvidenceCache()
        newsletter_adapter = NewsletterJobsAdapter(
            NewsletterAdapterConfig(
                fetcher=http_fetcher,
                fallback_fetcher=fallback,
                fit_profile=fit_profile,
                market=market,
                source_lane=newsletter_source_lane,
            ),
            terminal_cache=terminal_cache,
        )
        web_adapter = NewsletterJobsAdapter(
            NewsletterAdapterConfig(
                fetcher=http_fetcher,
                fallback_fetcher=fallback,
                fit_profile=fit_profile,
                market=market,
                source_lane="US Web",
            ),
            terminal_cache=terminal_cache,
        )

        initial_candidates = [
            newsletter_adapter.to_identity_candidate(observation)
            for observation in newsletter_to_resolve
        ] + [
            web_adapter.to_identity_candidate(observation)
            for observation in web_to_resolve
        ]
        stage_started = perf_counter()
        initial_ingest_results = ingest(
            initial_candidates,
            lane=lane,
            lane_priority=lane_priority,
            repository=repository,
            run_date=end.date(),
            context=context,
        )
        initial_by_ref = {
            candidate.evidence_ref: result
            for candidate, result in zip(initial_candidates, initial_ingest_results)
        }
        persistence_verified = {
            observation.evidence_ref
            for observation in newsletter_to_resolve + web_to_resolve
            if initial_by_ref.get(observation.evidence_ref)
            and initial_by_ref[observation.evidence_ref].persistence_verified
        }
        newsletter_enrichment_observations = tuple(
            observation for observation in newsletter_to_resolve
            if observation.evidence_ref in persistence_verified
            and not initial_by_ref[observation.evidence_ref].terminal_evidence_satisfied
        )
        web_enrichment_observations = tuple(
            observation for observation in web_to_resolve
            if observation.evidence_ref in persistence_verified
            and not initial_by_ref[observation.evidence_ref].terminal_evidence_satisfied
        )
        newsletter_terminal_required_total = len(newsletter_enrichment_observations)
        web_terminal_required_total = len(web_enrichment_observations)
        timings["initial_persist"] = round(perf_counter() - stage_started, 3)

        stage_started = perf_counter()
        newsletter_candidates = _adapt_all(
            newsletter_enrichment_observations,
            adapter=newsletter_adapter,
            context=context,
            max_workers=_TERMINAL_RESOLUTION_WORKERS,
        )
        web_candidates = _adapt_all(
            web_enrichment_observations,
            adapter=web_adapter,
            context=context,
            max_workers=_TERMINAL_RESOLUTION_WORKERS,
        )
        newsletter_candidates = [
            replace(
                candidate,
                job=replace(candidate.job, canonical_identity=initial_by_ref[candidate.evidence_ref].stable_job_key),
                unresolved_reason=None,
                fit_reason="terminal_unresolved",
            )
            if candidate.unresolved_reason and initial_by_ref[candidate.evidence_ref].stable_job_key
            else candidate
            for candidate in newsletter_candidates
        ]
        web_candidates = [
            replace(
                candidate,
                job=replace(candidate.job, canonical_identity=initial_by_ref[candidate.evidence_ref].stable_job_key),
                unresolved_reason=None,
                fit_reason="terminal_unresolved",
            )
            if candidate.unresolved_reason and initial_by_ref[candidate.evidence_ref].stable_job_key
            else candidate
            for candidate in web_candidates
        ]
        timings["terminal_resolution"] = round(perf_counter() - stage_started, 3)
        stage_started = perf_counter()
        enrichment_ingest_results = ingest(
            newsletter_candidates + web_candidates,
            lane=lane,
            lane_priority=lane_priority,
            repository=repository,
            run_date=end.date(),
            context=context,
        )
        timings["reconcile_persist"] = round(perf_counter() - stage_started, 3)

        final_by_ref = dict(initial_by_ref)
        final_by_ref.update({candidate.evidence_ref: result for candidate, result in zip(newsletter_candidates + web_candidates, enrichment_ingest_results)})
        enriched_refs = {candidate.evidence_ref for candidate in newsletter_candidates + web_candidates}
        for observation in list(newsletter_to_resolve) + list(web_to_resolve):
            ref = observation.evidence_ref
            initial = initial_by_ref.get(ref)
            if initial and initial.terminal_evidence_satisfied and ref not in enriched_refs:
                final_by_ref[ref] = replace(initial, disposition=Disposition.UPDATED)
        newsletter_ingest_count = len(newsletter_to_resolve)
        newsletter_results = list(newsletter_preexcluded) + [final_by_ref[item.evidence_ref] for item in newsletter_to_resolve]
        web_results = list(web_preexcluded) + [final_by_ref[item.evidence_ref] for item in web_to_resolve]
        results = newsletter_results + web_results

        newsletter_fully_accounted = len(newsletter_results) == attempted_newsletter_observations
        newsletter_unresolved = any(not result_is_accounted(item) for item in newsletter_results)
        web_fully_accounted = len(web_results) == len(web_result.observations)
        web_unresolved = any(item.disposition is Disposition.REVIEW_DEGRADED for item in web_results)
        fully_accounted = len(results) == attempted_newsletter_observations + len(web_result.observations)
        newsletter_ok = newsletter_result.state is NewsletterExecutionState.PASS
        staging_ok = bool(mail_result and mail_result.checkpoint_safe)

        processed_errors: list[str] = []
        processed_count = 0
        stage_started = perf_counter()
        accepted_message_ids = _accepted_newsletter_message_ids(
            newsletter_result,
            newsletter_results,
            allowed_message_ids=selected_newsletter_message_ids,
        )
        for message_id in accepted_message_ids:
            try:
                gmail.mark_newsletter_processed(message_id, NEWSLETTER_BOUNDARY)
            except Exception as exc:
                processed_errors.append(type(exc).__name__)
            else:
                processed_count += 1
        timings["newsletter_mark_processed"] = round(perf_counter() - stage_started, 3); retention = _retain_processed_newsletters(gmail, now=end); retention_errors = list(retention.errors)

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
            and newsletter_fully_accounted
            and not newsletter_unresolved
            and not processed_errors and not retention_errors
            and not backlog_errors
            and (pending_source_messages == 0 or processed_count > 0)
        )
        web_lane_pass = web_result.complete and web_fully_accounted and not web_unresolved
        pass_run = mail_lane_pass and web_lane_pass
        body = {
            "status": "PASS" if pass_run else "DEGRADED",
            "review_these_jobs": derive_review_these_jobs(list(newsletter_result.observations) + list(web_result.observations), newsletter_candidates + web_candidates, newsletter_results + web_results),
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
                "retention_error_count": len(retention_errors), "retention_error_codes": sorted(set(retention_errors)),
                "retention_candidate_count": retention.candidate_count,
                "retention_automated_count": retention.automated_count,
                "retention_trashed_count": retention.trashed_count,
                "retention_human_excluded_count": retention.human_excluded_count,
                "retention_unresolved_count": retention.unresolved_count,
                "pending_source_messages": pending_source_messages,
                "oldest_pending_age_seconds": oldest_pending_age_seconds,
                "processed_observations": attempted_newsletter_observations,
                "progress_messages": processed_count,
                "cleanup_safe": newsletter_ok and newsletter_fully_accounted and not newsletter_unresolved and not processed_errors,
                "error_codes": [f"{item.operation}:{item.detail}" for item in mail_result.errors[:10]] if mail_result else [],
                "backlog_error_codes": backlog_errors,
            },
            "newsletter": {
                "messages": len(newsletter_result.messages),
                "observations": len(newsletter_result.observations),
                "preexcluded": len(newsletter_preexcluded),
                "attempted_messages": attempted_newsletter_messages,
                "attempted_observations": attempted_newsletter_observations,
                "deferred_messages": deferred_newsletter_messages,
                "deferred_observations": deferred_newsletter_observations,
                "terminal_resolution_required": newsletter_terminal_required_total,
                "terminal_resolution_budget": None,
                "terminal_resolution_admitted": attempted_newsletter_observations,
                "state": newsletter_result.state.value,
                "error_codes": [f"{item.operation}:{item.detail}" for item in newsletter_result.errors[:10]],
            },
            "web": {
                "status": "PASS" if web_lane_pass else "DEGRADED",
                "observations": len(web_result.observations),
                "preexcluded": len(web_preexcluded),
                "terminal_resolution_required": web_terminal_required_total,
                "terminal_resolution_admitted": len(web_to_resolve),
                "deferred_observations": web_deferred_observations,
                "complete_sources": sum(item.state == "COMPLETE" for item in web_result.sources),
                "not_due_sources": sum(item.state == "NOT_DUE" for item in web_result.sources),
                "sources": len(web_result.sources),
                "degraded_sources": [
                    {"source_id": item.source_id, "detail": item.detail}
                    for item in web_result.sources
                    if item.state == "DEGRADED"
                ],
                "full_sweep": full_web_sweep,
            },
            "jobs": {
                "observations": attempted_newsletter_observations + len(web_result.observations),
                "fully_accounted": fully_accounted,
                "dispositions": {
                    disposition.value: sum(item.disposition == disposition for item in results)
                    for disposition in Disposition
                },
            },
            "timings": timings,
            "browser_fallback_available": fallback is not None,
            "within_45s_benchmark": context.elapsed_seconds() <= 45.0,
        }
        return UsRemoteResult(body=body, indent=2, exit_code=0 if pass_run else 1)
    except DeadlineExceeded:
        body = {
            "status": "DEGRADED",
            "reason": "execution-deadline-exhausted",
            "elapsed_seconds": round(context.elapsed_seconds(), 3),
            "timings": timings,
        }
        return UsRemoteResult(body=body, indent=None, exit_code=1)
    except RuntimeError as exc:
        body = {
            "status": "DEGRADED",
            "reason": type(exc).__name__,
            "elapsed_seconds": round(context.elapsed_seconds(), 3),
            "timings": timings,
        }
        return UsRemoteResult(body=body, indent=None, exit_code=1)
