"""US Remote production runtime: coordinate Mail, Newsletter, Web, and Jobs."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

from lifeos.core.http import HttpClient
from lifeos.core.runtime import DeadlineExceeded, RunContext
from lifeos.integrations.gmail import GmailInboxMetadataPort, GmailMailboxTransport
from lifeos.integrations.notion import NotionTransport
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.newsletter_adapter import HttpClientFetcher, NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition, IngestResult, ingest
from lifeos.jobs.newsletter_feature import _adapt_all
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.terminal_evidence import browser_evidence, fallback_fetcher
from lifeos.jobs.us_remote_acquisition import USRemoteAcquirer
from lifeos.mail.router import MailRouter
from lifeos.newsletter.models import ParseState, SourceVacancyObservation
from lifeos.newsletter.processor import NewsletterExecutionState, NewsletterProcessor

from scripts.run_newsletter_production import NEWSLETTER_BOUNDARY, ProductionConfigError

_SOURCE_REGISTRY = Path(__file__).resolve().parents[2] / "contracts" / "us_remote_sources.json"


def load_registry() -> dict:
    try:
        data = json.loads(_SOURCE_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionConfigError("US Remote source registry unavailable") from exc
    if not isinstance(data, dict):
        raise ProductionConfigError("US Remote source registry invalid")
    return data


def _accepted_newsletter_message_ids(process_result, results: list[IngestResult]) -> list[str]:
    by_ref = {result.evidence_ref: result for result in results}
    accepted: list[str] = []
    for message in process_result.messages:
        if message.state is not ParseState.PASS:
            continue
        message_results = [by_ref.get(observation.evidence_ref) for observation in message.observations]
        if any(result is None or result.disposition is Disposition.REVIEW_DEGRADED for result in message_results):
            continue
        if ":" not in message.message_ref:
            continue
        mailbox, message_id = message.message_ref.split(":", 1)
        if mailbox == "gmail" and message_id:
            accepted.append(message_id)
    return accepted


def _role_base(role: str, profile: FitProfile) -> int:
    padded = f" {role.casefold()} "
    for family in profile.role_families:
        if any(re.search(pattern, padded, re.I) for pattern in family.patterns):
            return family.base_score
    return profile.default_role_base


def _fit_ceiling(observation: SourceVacancyObservation, profile: FitProfile) -> int | None:
    role = (observation.role or "").strip()
    if not role:
        return None
    return min(100, _role_base(role, profile) + sum(category.cap for category in profile.scope_categories))


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

    ceiling = _fit_ceiling(observation, fit_profile)
    review_floor = (
        lane.target_review_floor
        if lane.is_target_bucket and lane.target_review_floor is not None
        else lane.fit_floor
    )
    if ceiling is not None and ceiling < review_floor:
        return IngestResult(
            observation.evidence_ref,
            Disposition.EXCLUDED,
            None,
            f"maximum possible fit {ceiling} is below configured review floor {review_floor}",
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
        timings["cheap_prefilter"] = round(perf_counter() - stage_started, 3)

        repository = NotionCareerRepository(
            transport=notion,
            config=NotionCareerRepositoryConfig(data_source_id=notion_job_ledger_data_source_id),
        )
        http_fetcher = HttpClientFetcher(http=http, context=context)
        newsletter_adapter = NewsletterJobsAdapter(
            NewsletterAdapterConfig(
                fetcher=http_fetcher,
                fallback_fetcher=fallback,
                fit_profile=fit_profile,
                market=market,
                source_lane=newsletter_source_lane,
            )
        )
        web_adapter = NewsletterJobsAdapter(
            NewsletterAdapterConfig(
                fetcher=http_fetcher,
                fallback_fetcher=fallback,
                fit_profile=fit_profile,
                market=market,
                source_lane="US Web",
            )
        )

        stage_started = perf_counter()
        newsletter_candidates = _adapt_all(
            tuple(newsletter_to_resolve),
            adapter=newsletter_adapter,
            context=context,
            max_workers=8,
        )
        web_candidates = _adapt_all(
            tuple(web_to_resolve),
            adapter=web_adapter,
            context=context,
            max_workers=8,
        )
        timings["terminal_resolution"] = round(perf_counter() - stage_started, 3)
        stage_started = perf_counter()
        ingest_results = ingest(
            newsletter_candidates + web_candidates,
            lane=lane,
            lane_priority=lane_priority,
            repository=repository,
            run_date=end.date(),
            context=context,
        )
        timings["reconcile_persist"] = round(perf_counter() - stage_started, 3)

        newsletter_ingest_count = len(newsletter_candidates)
        newsletter_results = list(newsletter_preexcluded) + ingest_results[:newsletter_ingest_count]
        web_results = list(web_preexcluded) + ingest_results[newsletter_ingest_count:]
        results = newsletter_results + web_results

        newsletter_fully_accounted = len(newsletter_results) == len(newsletter_result.observations)
        newsletter_unresolved = any(item.disposition is Disposition.REVIEW_DEGRADED for item in newsletter_results)
        web_fully_accounted = len(web_results) == len(web_result.observations)
        web_unresolved = any(item.disposition is Disposition.REVIEW_DEGRADED for item in web_results)
        fully_accounted = len(results) == len(newsletter_result.observations) + len(web_result.observations)
        newsletter_ok = newsletter_result.state is NewsletterExecutionState.PASS
        staging_ok = bool(mail_result and mail_result.checkpoint_safe)

        processed_errors: list[str] = []
        processed_count = 0
        stage_started = perf_counter()
        accepted_message_ids = _accepted_newsletter_message_ids(newsletter_result, newsletter_results)
        for message_id in accepted_message_ids:
            try:
                gmail.mark_newsletter_processed(message_id, NEWSLETTER_BOUNDARY)
            except Exception as exc:
                processed_errors.append(type(exc).__name__)
            else:
                processed_count += 1
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
            and newsletter_fully_accounted
            and not newsletter_unresolved
            and not processed_errors
            and not backlog_errors
            and (pending_source_messages == 0 or processed_count > 0)
        )
        web_lane_pass = web_result.complete and web_fully_accounted and not web_unresolved
        pass_run = mail_lane_pass and web_lane_pass
        body = {
            "status": "PASS" if pass_run else "DEGRADED",
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
                "processed_observations": len(newsletter_result.observations),
                "progress_messages": processed_count,
                "cleanup_safe": newsletter_ok and newsletter_fully_accounted and not newsletter_unresolved and not processed_errors,
                "error_codes": [f"{item.operation}:{item.detail}" for item in mail_result.errors[:10]] if mail_result else [],
                "backlog_error_codes": backlog_errors,
            },
            "newsletter": {
                "messages": len(newsletter_result.messages),
                "observations": len(newsletter_result.observations),
                "preexcluded": len(newsletter_preexcluded),
                "terminal_resolution_required": len(newsletter_to_resolve),
                "state": newsletter_result.state.value,
                "error_codes": [f"{item.operation}:{item.detail}" for item in newsletter_result.errors[:10]],
            },
            "web": {
                "status": "PASS" if web_lane_pass else "DEGRADED",
                "observations": len(web_result.observations),
                "preexcluded": len(web_preexcluded),
                "terminal_resolution_required": len(web_to_resolve),
                "complete_sources": sum(item.state == "COMPLETE" for item in web_result.sources),
                "not_due_sources": sum(item.state == "NOT_DUE" for item in web_result.sources),
                "sources": len(web_result.sources),
                "degraded_sources": [
                    item.source_id for item in web_result.sources if item.state == "DEGRADED"
                ],
                "full_sweep": full_web_sweep,
            },
            "jobs": {
                "observations": len(newsletter_result.observations) + len(web_result.observations),
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
