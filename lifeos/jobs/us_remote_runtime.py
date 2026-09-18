"""US Remote product runtime.

Coordinates the existing Mail/Newsletter/Web/Jobs domain primitives
(MailRouter, NewsletterProcessor, USRemoteAcquirer, NewsletterJobsAdapter,
ingest, NotionCareerRepository) for one bounded US Remote execution. This is
product-specific to US Remote, not a general orchestration framework --
scripts/run_us_remote_production.py stays the executable composition root
(CLI, config bootstrap, dependency construction, print, exit code); this
module owns the execution mechanics so that boundary can be read/changed
without pulling in Mail/Newsletter/Web/Jobs mechanics.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Semaphore
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
from lifeos.jobs.newsletter_reliability import (
    MemoizingFetcher,
    drain_newsletter_backlog,
    oldest_pending_age_seconds,
    prepare_newsletter_candidates,
)
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.terminal_evidence import FetchResponse, Fetcher
from lifeos.jobs.us_remote_acquisition import USRemoteAcquirer
from lifeos.mail.router import MailRouter
from lifeos.newsletter.models import ParseState, SourceVacancyObservation
from lifeos.newsletter.processor import NewsletterExecutionState, NewsletterProcessor

from scripts.run_newsletter_production import NEWSLETTER_BOUNDARY, ProductionConfigError

_SOURCE_REGISTRY = Path(__file__).resolve().parents[2] / "contracts" / "us_remote_sources.json"


class MappingFetcher(Fetcher):
    """Optional explicitly supplied browser evidence, kept in memory only."""

    def __init__(self, evidence: dict | None) -> None:
        self._pages = {
            str(item.get("url")): FetchResponse(
                final_url=str(item.get("final_url") or item.get("url") or ""),
                body=str(item.get("html") or ""),
            )
            for item in (evidence or {}).get("pages", [])
            if isinstance(item, dict) and item.get("url") and item.get("html")
        }

    def get(self, url: str) -> FetchResponse:
        if url not in self._pages:
            raise RuntimeError("injected browser evidence unavailable")
        return self._pages[url]

    @property
    def available(self) -> bool:
        return bool(self._pages)


class ChromeFetcher(Fetcher):
    """Bounded headless-browser fallback using the GitHub runner's Chrome."""

    def __init__(self, context: RunContext, *, max_concurrency: int = 2) -> None:
        self._context = context
        self._binary = (
            shutil.which("google-chrome")
            or shutil.which("google-chrome-stable")
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
        )
        self._permits = Semaphore(max(1, min(int(max_concurrency), 2)))

    @property
    def available(self) -> bool:
        return bool(self._binary)

    def get(self, url: str) -> FetchResponse:
        if not self._binary:
            raise RuntimeError("headless browser unavailable")
        wait = self._context.require_time(0.5)
        acquired = self._permits.acquire(timeout=wait)
        if not acquired:
            raise DeadlineExceeded("browser fallback concurrency wait exhausted deadline")
        try:
            with self._context.http_permit():
                remaining = self._context.require_time(0.5)
                timeout = max(0.5, min(12.0, remaining - 0.25))
                completed = subprocess.run(
                    [
                        self._binary,
                        "--headless=new",
                        "--disable-gpu",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--dump-dom",
                        url,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("browser fallback timed out") from exc
        finally:
            self._permits.release()
        body = completed.stdout or ""
        if completed.returncode != 0 or len(body.strip()) < 20:
            raise RuntimeError("browser fallback did not return usable DOM")
        return FetchResponse(final_url=url, body=body)


class CompositeFetcher(Fetcher):
    def __init__(self, fetchers: list[Fetcher]) -> None:
        self._fetchers = tuple(fetchers)

    def get(self, url: str) -> FetchResponse:
        for fetcher in self._fetchers:
            try:
                return fetcher.get(url)
            except DeadlineExceeded:
                raise
            except Exception:
                continue
        raise RuntimeError("all browser fallback transports failed")


def browser_evidence() -> dict | None:
    raw = os.getenv("US_REMOTE_BROWSER_EVIDENCE_JSON")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProductionConfigError("US_REMOTE_BROWSER_EVIDENCE_JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ProductionConfigError("US_REMOTE_BROWSER_EVIDENCE_JSON must be an object")
    return value


def fallback_fetcher(context: RunContext, evidence: dict | None) -> Fetcher | None:
    fetchers: list[Fetcher] = []
    injected = MappingFetcher(evidence)
    if injected.available:
        fetchers.append(injected)
    chrome = ChromeFetcher(context)
    if chrome.available:
        fetchers.append(chrome)
    return CompositeFetcher(fetchers) if fetchers else None


def load_registry() -> dict:
    try:
        data = json.loads(_SOURCE_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionConfigError("US Remote source registry unavailable") from exc
    if not isinstance(data, dict):
        raise ProductionConfigError("US Remote source registry invalid")
    return data


def counts(results: list[IngestResult]) -> dict[str, int]:
    return {
        disposition.value: sum(item.disposition == disposition for item in results)
        for disposition in Disposition
    }


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


def partition_observations(
    observations: tuple[SourceVacancyObservation, ...],
    *,
    lane: LaneConfig,
    fit_profile: FitProfile,
) -> tuple[list[SourceVacancyObservation], list[IngestResult]]:
    resolve: list[SourceVacancyObservation] = []
    excluded: list[IngestResult] = []
    for observation in observations:
        disposition = _preexclude(observation, lane=lane, fit_profile=fit_profile)
        if disposition is None:
            resolve.append(observation)
        else:
            excluded.append(disposition)
    return resolve, excluded


def _error_codes(items) -> list[str]:
    return [f"{item.operation}:{item.detail}" for item in items[:10]]


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
        newsletter_processor = NewsletterProcessor(boundary_name=NEWSLETTER_BOUNDARY)
        newsletter_drain = drain_newsletter_backlog(
            gmail,
            processor=newsletter_processor,
            boundary_name=NEWSLETTER_BOUNDARY,
            context=context,
        )
        newsletter_result = newsletter_drain.process_result
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
        newsletter_to_resolve, newsletter_preexcluded = partition_observations(
            newsletter_result.observations,
            lane=lane,
            fit_profile=fit_profile,
        )
        web_to_resolve, web_preexcluded = partition_observations(
            web_result.observations,
            lane=lane,
            fit_profile=fit_profile,
        )
        timings["cheap_prefilter"] = round(perf_counter() - stage_started, 3)

        repository = NotionCareerRepository(
            transport=notion,
            config=NotionCareerRepositoryConfig(data_source_id=notion_job_ledger_data_source_id),
        )
        http_fetcher = MemoizingFetcher(HttpClientFetcher(http=http, context=context))
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
        newsletter_preparation = prepare_newsletter_candidates(
            tuple(newsletter_to_resolve),
            adapter=newsletter_adapter,
            repository=repository,
            context=context,
            market=market,
            source_lane=newsletter_source_lane,
            max_workers=8,
        )
        newsletter_candidates = list(newsletter_preparation.candidates)
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

        backlog_errors: list[str] = []
        pending_ids_after: tuple[str, ...] = ()
        oldest_pending_age: float | None = None
        try:
            pending_ids_after = tuple(gmail.enumerate_unprocessed_ids(NEWSLETTER_BOUNDARY))
            if pending_ids_after:
                oldest_pending_age = oldest_pending_age_seconds(
                    gmail,
                    pending_ids_after,
                    known_messages=newsletter_result.messages,
                    now=end,
                )
        except Exception as exc:
            backlog_errors.append(type(exc).__name__)

        progress_made = not pending_ids_after or processed_count > 0
        cleanup_safe = (
            newsletter_ok
            and newsletter_fully_accounted
            and not newsletter_unresolved
            and not processed_errors
        )
        backlog_healthy = not backlog_errors and progress_made
        mail_lane_pass = staging_ok and cleanup_safe and backlog_healthy
        web_lane_pass = web_result.complete and web_fully_accounted and not web_unresolved
        pass_run = mail_lane_pass and web_lane_pass

        newsletter_work_seconds = max(
            0.001,
            timings.get("newsletter_fetch_parse", 0.0)
            + timings.get("terminal_resolution", 0.0)
            + timings.get("reconcile_persist", 0.0)
            + timings.get("newsletter_mark_processed", 0.0),
        )
        newsletter_throughput = round(len(newsletter_results) / newsletter_work_seconds, 3)

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
                "error_codes": _error_codes(mail_result.errors) if mail_result else [],
            },
            "newsletter": {
                "messages": len(newsletter_result.messages),
                "observations": len(newsletter_result.observations),
                "preexcluded": len(newsletter_preexcluded),
                "terminal_resolution_required": len(newsletter_to_resolve),
                "canonical_reuse": newsletter_preparation.canonical_reuse_count,
                "state": newsletter_result.state.value,
                "error_codes": _error_codes(newsletter_result.errors),
                "pending_source_messages": len(pending_ids_after),
                "oldest_pending_age_seconds": (
                    round(oldest_pending_age, 3) if oldest_pending_age is not None else None
                ),
                "messages_admitted_this_run": len(newsletter_drain.admitted_message_ids),
                "messages_processed_this_run": processed_count,
                "observations_processed_this_run": len(newsletter_results),
                "throughput_observations_per_second": newsletter_throughput,
                "progress_made": progress_made,
                "cleanup_safe": cleanup_safe,
                "backlog_error_codes": backlog_errors,
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
                "dispositions": counts(results),
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
