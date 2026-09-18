#!/usr/bin/env python3
"""One bounded US Remote production execution -- composition root.

CLI/config -> construct dependencies -> invoke the US Remote runtime
boundary (lifeos.jobs.us_remote_runtime) -> print result -> return exit
code. Mail/Newsletter/Web/Jobs execution mechanics live in that runtime
module, not here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from lifeos.core.http import HttpClient, HttpError
from lifeos.core.runtime import DeadlineExceeded, RunContext
from lifeos.integrations.gmail import GmailMailboxTransport
from lifeos.integrations.notion import NotionTransport, NotionTransportError
from lifeos.jobs.us_remote_acquisition import USRemoteAcquirer
from lifeos.jobs.us_remote_runtime import browser_evidence, execute_us_remote, load_registry
from lifeos.mail.models import MailMessage

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
        registry = load_registry()
        browser_evidence_payload = browser_evidence()
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

    result = execute_us_remote(
        context=context,
        http=http,
        notion=notion,
        gmail=gmail,
        registry=registry,
        browser_evidence=browser_evidence_payload,
        lane=lane,
        lane_priority=lane_priority,
        fit_profile=fit_profile,
        market=market,
        newsletter_source_lane=newsletter_source_lane,
        notion_job_ledger_data_source_id=env["NOTION_JOB_LEDGER_DATA_SOURCE_ID"],
        inbox_start=inbox_start,
        inbox_mode=inbox_mode,
        start=start,
        end=end,
        web_since=web_since,
        dry_run=args.dry_run,
        full_web_sweep=args.full_web_sweep,
    )
    print(json.dumps(result.body, indent=result.indent, sort_keys=True))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
