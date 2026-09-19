#!/usr/bin/env python3
"""Production Newsletter entry point: one bounded execution.

whole Gmail mailbox -> classify -> stage confirmed automated Newsletter mail
-> parse vacancies -> Jobs terminal evidence/Fit/qualification -> canonical
Job Ledger -> authoritative read-back -> processed Gmail marker.

Personal career policy is never stored in this public repository. The live
policy is loaded at runtime from the private Notion Job Lane Configuration
record for US Remote. Standard public GitHub-hosted Actions may execute this
script from protected main; this script creates no scheduler or orchestration
layer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from lifeos.core.backlog import consume_bounded_backlog
from lifeos.core.http import HttpClient, HttpError, RetryPolicy
from lifeos.core.runtime import DeadlineExceeded, RunContext
from lifeos.integrations.gmail import BACKLOG_BATCH_SIZE, GmailMailboxTransport
from lifeos.integrations.mailbox import MailboxTransportError
from lifeos.integrations.notion import NotionIdentityQuery, NotionTransport, NotionTransportError
from lifeos.jobs.newsletter_adapter import HttpClientFetcher, NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition
from lifeos.jobs.newsletter_feature import run_newsletter_feature
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig
from lifeos.mail.classifier import DeterministicMailClassifier
from lifeos.mail.models import MailClass, MailMessage
from lifeos.mail.router import MailRouter
from lifeos.newsletter.processor import (
    NewsletterError,
    NewsletterExecutionState,
    NewsletterProcessor,
    NewsletterProcessResult,
    NewsletterTimings,
    _safe_error_detail,
)
from scripts.run_us_remote_production import ProductionConfigError, _load_private_policy, _load_private_policy_from_notion
DEFAULT_TIMEOUT_SECONDS = 45.0
MAX_TIMEOUT_SECONDS = 300.0
DEFAULT_WINDOW_HOURS = 24.0
NEWSLETTER_BOUNDARY = "J Newsletters"
UAT_WORKFLOW_NAME = "Newsletter Production UAT (Manual)"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
REQUIRED_ENV = (
    "GMAIL_OAUTH_CLIENT_ID",
    "GMAIL_OAUTH_CLIENT_SECRET",
    "GMAIL_OAUTH_REFRESH_TOKEN",
    "NOTION_API_TOKEN",
    "NOTION_JOB_LEDGER_DATA_SOURCE_ID",
)


def _require_env() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        raise ProductionConfigError("missing required runtime configuration: " + ", ".join(missing))
    return {name: os.environ[name] for name in REQUIRED_ENV}


def _exchange_gmail_access_token(
    context: RunContext,
    http: HttpClient,
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> str:
    body = urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    payload = http.request_json(
        context,
        "POST",
        _GOOGLE_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
        timeout_seconds=10.0,
        retry=RetryPolicy(max_attempts=1),
    )
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ProductionConfigError("Gmail token refresh did not return an access token")
    return str(payload["access_token"])


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Production Newsletter entry point")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-mail-router",
        action="store_true",
        help="Consume the already-staged J Newsletters backlog without scanning/routing Inbox first.",
    )
    return parser.parse_args(argv)


def _should_skip_mail_router(args: argparse.Namespace) -> bool:
    return bool(
        args.skip_mail_router
        or os.getenv("GITHUB_WORKFLOW") == UAT_WORKFLOW_NAME
    )


def _dry_run_preview(gmail: GmailMailboxTransport, start: datetime, end: datetime) -> dict:
    classifier = DeterministicMailClassifier()
    messages = gmail.scan_window(start, end)
    classifications = [classifier.classify(m) for m in messages]
    return {
        "scanned": len(messages),
        "would_stage": sum(
            1 for c in classifications if c.mail_class is MailClass.AUTOMATED_JOB_SOURCE
        ),
    }


def _safe_summary(
    *,
    dry_run: bool,
    elapsed_seconds: float,
    mail_preview,
    mail_result,
    process_result,
    feature_result,
    processed_count: int,
    processed_errors: int,
    mail_router_skipped: bool = False,
) -> dict:
    summary: dict = {
        "dry_run": dry_run,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "processed_messages": processed_count,
        "processed_errors": processed_errors,
    }
    if mail_router_skipped:
        summary["mail_router_skipped"] = True
    if mail_preview is not None:
        summary["mail_preview"] = mail_preview
    if mail_result is not None:
        summary["mail"] = {
            "state": mail_result.state.value,
            "scanned": mail_result.scanned_count,
            "staged": sum(1 for r in mail_result.records if r.routed),
            "errors": len(mail_result.errors),
            "staging_safe": mail_result.checkpoint_safe,
        }
    if process_result is not None:
        summary["newsletter_parse"] = {
            "state": process_result.state.value,
            "messages": len(process_result.messages),
            "observations": len(process_result.observations),
            "errors": len(process_result.errors),
        }
        if process_result.errors:
            summary["newsletter_parse"]["error_details"] = [
                {
                    "mailbox": error.mailbox,
                    "operation": error.operation,
                    "detail": error.detail,
                }
                for error in process_result.errors
            ]
    if feature_result is not None:
        disposition_counts = {d.value: 0 for d in Disposition}
        for result in feature_result.ingest_results:
            disposition_counts[result.disposition.value] += 1
        summary["jobs"] = {
            "execution_status": feature_result.execution.status.value,
            "execution_code": feature_result.execution.code,
            "dispositions": disposition_counts,
            "cleanup_safe": feature_result.cleanup_safe,
        }
        review_degraded = [
            {
                "evidence_ref": result.evidence_ref,
                "detail": result.detail,
            }
            for result in feature_result.ingest_results
            if result.disposition is Disposition.REVIEW_DEGRADED
        ]
        if review_degraded:
            summary["jobs"]["review_degraded"] = review_degraded
    return summary


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if not (0 < args.timeout_seconds <= MAX_TIMEOUT_SECONDS):
        print(
            f"BLOCKED: --timeout-seconds must be in (0, {MAX_TIMEOUT_SECONDS}]",
            file=sys.stderr,
        )
        return 2
    if args.window_hours <= 0:
        print("BLOCKED: --window-hours must be positive", file=sys.stderr)
        return 2
    try:
        env = _require_env()
    except ProductionConfigError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2

    context = RunContext.start(timeout_seconds=args.timeout_seconds)
    http = HttpClient()
    notion_transport = NotionTransport(
        context=context,
        http=http,
        access_token=env["NOTION_API_TOKEN"],
    )
    try:
        fixture_path = os.getenv("NEWSLETTER_PRIVATE_POLICY_PATH")
        if fixture_path:
            lane, lane_priority, fit_profile, market, source_lane = _load_private_policy(fixture_path)
        else:
            lane, lane_priority, fit_profile, market, source_lane = _load_private_policy_from_notion(
                context,
                http,
                notion_transport,
                notion_token=env["NOTION_API_TOKEN"],
            )
        access_token = _exchange_gmail_access_token(
            context,
            http,
            client_id=env["GMAIL_OAUTH_CLIENT_ID"],
            client_secret=env["GMAIL_OAUTH_CLIENT_SECRET"],
            refresh_token=env["GMAIL_OAUTH_REFRESH_TOKEN"],
        )
    except (ProductionConfigError, NotionTransportError, HttpError, DeadlineExceeded) as exc:
        print(
            f"BLOCKED: production configuration failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 2

    gmail = GmailMailboxTransport(
        context=context,
        http=http,
        access_token=access_token,
        message_factory=MailMessage,
    )
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.window_hours)
    execution_status = "PASS"
    mail_preview = None
    mail_result = None
    process_result = None
    feature_result = None
    processed_count = 0
    processed_errors = 0
    mail_router_skipped = (not args.dry_run) and _should_skip_mail_router(args)

    try:
        if args.dry_run:
            mail_preview = _dry_run_preview(gmail, start, end)
            process_result = NewsletterProcessor(
                boundary_name=NEWSLETTER_BOUNDARY
            ).process_window([gmail], start, end)
        else:
            if not mail_router_skipped:
                mail_result = MailRouter(newsletter_boundary=NEWSLETTER_BOUNDARY).route_window(
                    [gmail], start, end
                )
                if not mail_result.checkpoint_safe:
                    execution_status = "DEGRADED"

            repository = NotionCareerRepository(
                transport=notion_transport,
                config=NotionCareerRepositoryConfig(
                    data_source_id=env["NOTION_JOB_LEDGER_DATA_SOURCE_ID"]
                ),
            )
            adapter = NewsletterJobsAdapter(
                NewsletterAdapterConfig(
                    fetcher=HttpClientFetcher(http=http, context=context),
                    fit_profile=fit_profile,
                    market=market,
                    source_lane=source_lane,
                )
            )

            def _process_selected_batch(message_ids):
                nonlocal process_result, feature_result, execution_status
                hydrated = gmail.hydrate_messages(message_ids)
                process_result = NewsletterProcessor(
                    boundary_name=NEWSLETTER_BOUNDARY
                ).process_messages(hydrated)
                feature_result = run_newsletter_feature(
                    process_result,
                    adapter=adapter,
                    lane=lane,
                    lane_priority=lane_priority,
                    repository=repository,
                    run_date=end.date(),
                    context=context,
                )
                if feature_result.execution.status.value != "PASS":
                    execution_status = feature_result.execution.status.value
                # Newsletter's existing all-or-nothing batch cleanup policy,
                # mapped onto the shared primitive's per-item completion
                # contract: every selected message shares the same
                # cleanup_safe outcome.
                return {message_id: feature_result.cleanup_safe for message_id in message_ids}

            def _mark_message_processed(message_id: str) -> None:
                nonlocal processed_count, processed_errors, execution_status
                try:
                    gmail.mark_newsletter_processed(message_id, NEWSLETTER_BOUNDARY)
                except Exception:
                    processed_errors += 1
                    execution_status = "DEGRADED"
                else:
                    processed_count += 1

            # The one real production invocation of the shared platform
            # mechanic (lifeos.core.backlog): complete canonical Gmail
            # backlog enumeration -> bounded selection -> hydrate only the
            # selected references -> existing Newsletter parse -> existing
            # Jobs processing/accounting -> Gmail Processed marking only for
            # a safely-accounted batch. An empty backlog calls process_batch
            # zero times, so no Jobs/Gmail mutation happens -- a clean
            # PASS/no-op with no platform-owned cursor or checkpoint.
            try:
                consume_bounded_backlog(
                    enumerate_backlog=lambda: gmail.enumerate_unprocessed_ids(NEWSLETTER_BOUNDARY),
                    batch_size=BACKLOG_BATCH_SIZE,
                    process_batch=_process_selected_batch,
                    mark_complete=_mark_message_processed,
                )
            except (MailboxTransportError, HttpError) as exc:
                # Gmail enumeration or selected-message hydration failed.
                # Restore the fail-closed acquisition-error behavior
                # NewsletterProcessor.process_window used to provide before
                # this path called Gmail directly: no message in the
                # attempted batch was marked processed (mark_complete is
                # only ever reached after process_batch returns, which
                # requires hydration to have already succeeded), so canonical
                # Gmail state remains fully resumable on the next execution.
                execution_status = "DEGRADED"
                process_result = NewsletterProcessResult(
                    NewsletterExecutionState.DEGRADED,
                    (),
                    (NewsletterError("gmail", "fetch", _safe_error_detail(exc)),),
                    NewsletterTimings(0.0, 0.0, context.elapsed_seconds()),
                )
    except DeadlineExceeded:
        execution_status = "DEGRADED"
        print("BLOCKED: execution deadline exhausted", file=sys.stderr)

    print(
        json.dumps(
            _safe_summary(
                dry_run=args.dry_run,
                elapsed_seconds=context.elapsed_seconds(),
                mail_preview=mail_preview,
                mail_result=mail_result,
                process_result=process_result,
                feature_result=feature_result,
                processed_count=processed_count,
                processed_errors=processed_errors,
                mail_router_skipped=mail_router_skipped,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if execution_status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
