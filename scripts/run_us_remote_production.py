#!/usr/bin/env python3
"""One bounded US Remote production execution.

Newsletter + US Web acquire independently, then reconcile together through one
shared Jobs ingest batch and one canonical Job Ledger. Confirmed automated mail
is staged out of Inbox immediately; a second Gmail label marks accepted
processing only after canonical persistence/read-back. No second scheduler,
workflow engine, handoff artifact, or lane-specific persistence path.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Semaphore
from time import perf_counter

from lifeos.core.http import HttpClient, HttpError
from lifeos.core.runtime import DeadlineExceeded, RunContext
from lifeos.integrations.gmail import GmailMailboxTransport
from lifeos.integrations.notion import NotionTransport, NotionTransportError
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.newsletter_adapter import HttpClientFetcher, NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition, IngestResult, ingest
from lifeos.jobs.newsletter_feature import _adapt_all
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.terminal_evidence import FetchResponse, Fetcher
from lifeos.jobs.us_remote_acquisition import USRemoteAcquirer
from lifeos.mail.models import MailMessage
from lifeos.mail.router import MailRouter
from lifeos.newsletter.models import ParseState, SourceVacancyObservation
from lifeos.newsletter.processor import NewsletterExecutionState, NewsletterProcessor

from scripts.run_newsletter_production import (
    NEWSLETTER_BOUNDARY,
    ProductionConfigError,
    _exchange_gmail_access_token,
    _load_private_policy,
    _load_private_policy_from_notion,
    _require_env,
)

DEFAULT_TIMEOUT_SECONDS = 45.0
MAX_TIMEOUT_SECONDS = 300.0
DEFAULT_WINDOW_HOURS = 24.0
DEFAULT_WEB_LOOKBACK_HOURS = 24.0
MAX_INBOX_STAGING_HOURS = 24.0
# Explicit, opt-in, bounded historical Inbox recovery mode only -- never the
# default. Normal scheduled production always uses MAX_INBOX_STAGING_HOURS;
# this only widens the Mail Router's Inbox scan window when a caller
# explicitly asks for a one-off bounded recovery execution. 90 days is a
# concrete finite cap, not an "arbitrary historical scan" allowance.
MAX_HISTORICAL_INBOX_RECOVERY_HOURS = 24.0 * 90
_SOURCE_REGISTRY = Path(__file__).resolve().parents[1] / "contracts" / "us_remote_sources.json"


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


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="US Remote: Newsletter + US Web -> one Job Ledger reconciliation")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--web-lookback-hours", type=float, default=DEFAULT_WEB_LOOKBACK_HOURS)
    parser.add_argument("--full-web-sweep", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--historical-inbox-recovery-hours",
        type=float,
        default=None,
        help=(
            "Explicit bounded historical Inbox recovery mode: widen the Mail "
            "Router's Inbox scan to this many hours instead of the normal "
            f"{MAX_INBOX_STAGING_HOURS:.0f}-hour staging window. Never the "
            "default; omit for normal production behavior."
        ),
    )
    return parser.parse_args(argv)


def _browser_evidence() -> dict | None:
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


def _fallback_fetcher(context: RunContext, evidence: dict | None) -> Fetcher | None:
    fetchers: list[Fetcher] = []
    injected = MappingFetcher(evidence)
    if injected.available:
        fetchers.append(injected)
    chrome = ChromeFetcher(context)
    if chrome.available:
        fetchers.append(chrome)
    return CompositeFetcher(fetchers) if fetchers else None


def _load_registry() -> dict:
    try:
        data = json.loads(_SOURCE_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionConfigError("US Remote source registry unavailable") from exc
    if not isinstance(data, dict):
        raise ProductionConfigError("US Remote source registry invalid")
    return data


def _counts(results: list[IngestResult]) -> dict[str, int]:
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


def _partition_observations(
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


def main(argv: list[str] | None = None) -> int:
    args = _args(sys.argv[1:] if argv is None else argv)
    if not (0 < args.timeout_seconds <= MAX_TIMEOUT_SECONDS):
        print(f"BLOCKED: timeout must be in (0, {MAX_TIMEOUT_SECONDS}]", file=sys.stderr)
        return 2
    if args.window_hours <= 0 or args.web_lookback_hours <= 0:
        print("BLOCKED: window-hours and web-lookback-hours must be positive", file=sys.stderr)
        return 2
    if args.historical_inbox_recovery_hours is not None and not (
        0 < args.historical_inbox_recovery_hours <= MAX_HISTORICAL_INBOX_RECOVERY_HOURS
    ):
        print(
            "BLOCKED: --historical-inbox-recovery-hours must be in "
            f"(0, {MAX_HISTORICAL_INBOX_RECOVERY_HOURS:.0f}]",
            file=sys.stderr,
        )
        return 2

    try:
        env = _require_env()
        registry = _load_registry()
        browser_evidence = _browser_evidence()
    except ProductionConfigError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2

    context = RunContext.start(timeout_seconds=args.timeout_seconds)
    http = HttpClient()
    notion = NotionTransport(context=context, http=http, access_token=env["NOTION_API_TOKEN"])
    try:
        fixture = os.getenv("NEWSLETTER_PRIVATE_POLICY_PATH")
        if fixture:
            lane, lane_priority, fit_profile, market, newsletter_source_lane = _load_private_policy(fixture)
        else:
            lane, lane_priority, fit_profile, market, newsletter_source_lane = _load_private_policy_from_notion(
                context,
                http,
                notion,
                notion_token=env["NOTION_API_TOKEN"],
            )
        gmail_token = _exchange_gmail_access_token(
            context,
            http,
            client_id=env["GMAIL_OAUTH_CLIENT_ID"],
            client_secret=env["GMAIL_OAUTH_CLIENT_SECRET"],
            refresh_token=env["GMAIL_OAUTH_REFRESH_TOKEN"],
        )
    except (ProductionConfigError, NotionTransportError, HttpError, DeadlineExceeded) as exc:
        print(f"BLOCKED: production configuration failed: {type(exc).__name__}", file=sys.stderr)
        return 2

    gmail = GmailMailboxTransport(
        context=context,
        http=http,
        access_token=gmail_token,
        message_factory=MailMessage,
    )
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.window_hours)
    if args.historical_inbox_recovery_hours is not None:
        # Explicit bounded historical recovery: widen only the Inbox
        # staging scan. Everything downstream (classification, routing,
        # Newsletter parse, Jobs reconciliation, cleanup) is the exact
        # existing production path -- unchanged.
        inbox_start = end - timedelta(hours=args.historical_inbox_recovery_hours)
        inbox_mode = "historical_recovery"
    else:
        inbox_start = max(start, end - timedelta(hours=MAX_INBOX_STAGING_HOURS))
        inbox_mode = "normal"
    web_since = end - timedelta(hours=args.web_lookback_hours)
    fallback_fetcher = _fallback_fetcher(context, browser_evidence)
    timings: dict[str, float] = {}

    try:
        stage_started = perf_counter()
        mail_result = (
            MailRouter(newsletter_boundary=NEWSLETTER_BOUNDARY).route_window([gmail], inbox_start, end)
            if not args.dry_run
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
            fallback_fetcher=fallback_fetcher,
        ).acquire(
            registry,
            browser_evidence=browser_evidence,
            since=web_since,
            full_sweep=args.full_web_sweep,
            now=end,
        )
        timings["web_acquire"] = round(perf_counter() - stage_started, 3)

        if args.dry_run:
            print(
                json.dumps(
                    {
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
                        "browser_fallback_available": fallback_fetcher is not None,
                        "timings": timings,
                    },
                    sort_keys=True,
                )
            )
            return 0

        stage_started = perf_counter()
        newsletter_to_resolve, newsletter_preexcluded = _partition_observations(
            newsletter_result.observations,
            lane=lane,
            fit_profile=fit_profile,
        )
        web_to_resolve, web_preexcluded = _partition_observations(
            web_result.observations,
            lane=lane,
            fit_profile=fit_profile,
        )
        timings["cheap_prefilter"] = round(perf_counter() - stage_started, 3)

        repository = NotionCareerRepository(
            transport=notion,
            config=NotionCareerRepositoryConfig(
                data_source_id=env["NOTION_JOB_LEDGER_DATA_SOURCE_ID"]
            ),
        )
        http_fetcher = HttpClientFetcher(http=http, context=context)
        newsletter_adapter = NewsletterJobsAdapter(
            NewsletterAdapterConfig(
                fetcher=http_fetcher,
                fallback_fetcher=fallback_fetcher,
                fit_profile=fit_profile,
                market=market,
                source_lane=newsletter_source_lane,
            )
        )
        web_adapter = NewsletterJobsAdapter(
            NewsletterAdapterConfig(
                fetcher=http_fetcher,
                fallback_fetcher=fallback_fetcher,
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

        fully_accounted = len(results) == len(newsletter_result.observations) + len(web_result.observations)
        unresolved = any(item.disposition is Disposition.REVIEW_DEGRADED for item in results)
        newsletter_ok = newsletter_result.state is NewsletterExecutionState.PASS
        staging_ok = bool(mail_result and mail_result.checkpoint_safe)

        processed_errors: list[str] = []
        processed_count = 0
        stage_started = perf_counter()
        for message_id in _accepted_newsletter_message_ids(newsletter_result, newsletter_results):
            try:
                gmail.mark_newsletter_processed(message_id, NEWSLETTER_BOUNDARY)
            except Exception as exc:
                processed_errors.append(type(exc).__name__)
            else:
                processed_count += 1
        timings["newsletter_mark_processed"] = round(perf_counter() - stage_started, 3)

        mail_ok = staging_ok and not processed_errors
        pass_run = (
            fully_accounted
            and not unresolved
            and newsletter_ok
            and mail_ok
            and web_result.complete
        )
        summary = {
            "status": "PASS" if pass_run else "DEGRADED",
            "elapsed_seconds": round(context.elapsed_seconds(), 3),
            "mail": {
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
                "state": newsletter_result.state.value,
                "error_codes": _error_codes(newsletter_result.errors),
            },
            "web": {
                "observations": len(web_result.observations),
                "preexcluded": len(web_preexcluded),
                "terminal_resolution_required": len(web_to_resolve),
                "complete_sources": sum(item.state == "COMPLETE" for item in web_result.sources),
                "not_due_sources": sum(item.state == "NOT_DUE" for item in web_result.sources),
                "sources": len(web_result.sources),
                "degraded_sources": [
                    item.source_id for item in web_result.sources if item.state == "DEGRADED"
                ],
                "full_sweep": args.full_web_sweep,
            },
            "jobs": {
                "observations": len(newsletter_result.observations) + len(web_result.observations),
                "fully_accounted": fully_accounted,
                "dispositions": _counts(results),
            },
            "timings": timings,
            "browser_fallback_available": fallback_fetcher is not None,
            "within_45s_benchmark": context.elapsed_seconds() <= 45.0,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if pass_run else 1
    except DeadlineExceeded:
        print(
            json.dumps(
                {
                    "status": "DEGRADED",
                    "reason": "execution-deadline-exhausted",
                    "elapsed_seconds": round(context.elapsed_seconds(), 3),
                    "timings": timings,
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())