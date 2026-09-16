#!/usr/bin/env python3
"""Production Newsletter entry point: the one composition script that wires
already-built components into one bounded execution.

whole Gmail mailbox -> classify -> move confirmed automated Newsletter mail
to J Newsletters -> fetch J Newsletters -> parse vacancies ->
run_newsletter_feature() -> canonical Job Ledger -> authoritative
read-back -> cleanup gated on cleanup_safe.

This module contains no parsing, classification, qualification, Fit, or
Notion-property-mapping logic of its own -- it only constructs and calls
already-landed domain components under one RunContext/deadline. No second
persistence layer, no workflow engine, no scheduler, no recovery
subsystem: run it once, get one terminal summary, exit.

Private runtime configuration is read only from the environment (secret
values) and from a private policy JSON file whose path is itself given by
an environment variable (LaneConfig/FitProfile content is Jim-specific
career policy -- see D-011 -- and must never be hardcoded in this public
script). No secret value, mail body, or token is ever printed; only a
structured, safe summary.

"cleanup" here means the Mail-domain routing mutation (moving confirmed
automated Newsletter mail into J Newsletters) -- this script performs it
only when NOT in --dry-run, and only calls it once per run regardless of
downstream Jobs outcome, matching MailRouter's own existing semantics.
Jobs-side cleanup_safe still gates whether the run is reported PASS.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from lifeos.core.http import HttpClient, HttpError, RetryPolicy
from lifeos.core.runtime import DeadlineExceeded, RunContext
from lifeos.integrations.gmail import GmailMailboxTransport
from lifeos.integrations.notion import NotionTransport
from lifeos.jobs.fit_scoring import FitProfile, PenaltyRule, RoleFamily, ScopeCategory
from lifeos.jobs.newsletter_adapter import HttpClientFetcher, NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition
from lifeos.jobs.newsletter_feature import run_newsletter_feature
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig
from lifeos.jobs.qualification import LaneConfig
from lifeos.mail.classifier import DeterministicMailClassifier
from lifeos.mail.models import MailClass, MailMessage
from lifeos.mail.router import MailRouter
from lifeos.newsletter.processor import NewsletterProcessor

DEFAULT_TIMEOUT_SECONDS = 90.0
MAX_TIMEOUT_SECONDS = 180.0  # this script's own ceiling; RunContext itself
# hard-stops at 300s (lifeos.core.runtime.MAX_RUNTIME_SECONDS) regardless.
DEFAULT_WINDOW_HOURS = 24.0
NEWSLETTER_BOUNDARY = "J Newsletters"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

REQUIRED_ENV = (
    "GMAIL_OAUTH_CLIENT_ID",
    "GMAIL_OAUTH_CLIENT_SECRET",
    "GMAIL_OAUTH_REFRESH_TOKEN",
    "NOTION_API_TOKEN",
    "NOTION_JOB_LEDGER_DATA_SOURCE_ID",
    "NEWSLETTER_PRIVATE_POLICY_PATH",
)


class ProductionConfigError(RuntimeError):
    """Fail-closed configuration problem. Never carries a secret value."""


def _require_env() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        raise ProductionConfigError("missing required runtime configuration: " + ", ".join(missing))
    return {name: os.environ[name] for name in REQUIRED_ENV}


def _load_private_policy(path: str) -> tuple[LaneConfig, dict[str, int], FitProfile, str, str]:
    """Load Jim-specific career/lane policy from a private local file. Only
    the file *path* is an env var; its content (thresholds, role families,
    scope terms) is never hardcoded in this public script -- see D-011."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        raise ProductionConfigError(f"could not read NEWSLETTER_PRIVATE_POLICY_PATH: {type(exc).__name__}") from exc
    except json.JSONDecodeError as exc:
        raise ProductionConfigError(f"NEWSLETTER_PRIVATE_POLICY_PATH is not valid JSON: {exc}") from exc

    try:
        lane_raw = raw["lane"]
        lane = LaneConfig(
            name=lane_raw["name"],
            market=lane_raw["market"],
            fit_floor=lane_raw["fit_floor"],
            target_review_floor=lane_raw.get("target_review_floor"),
            work_mode_policy=lane_raw["work_mode_policy"],
            compensation_floor=lane_raw.get("compensation_floor"),
            freshness_gate=lane_raw["freshness_gate"],
            freshness_max_days=lane_raw.get("freshness_max_days"),
            is_target_bucket=lane_raw.get("is_target_bucket", False),
        )
        lane_priority = {str(k): int(v) for k, v in raw["lane_priority"].items()}

        fit_raw = raw["fit_profile"]
        fit_profile = FitProfile(
            model_version=fit_raw["model_version"],
            role_families=tuple(
                RoleFamily(patterns=tuple(rf["patterns"]), base_score=rf["base_score"], label=rf["label"])
                for rf in fit_raw.get("role_families", [])
            ),
            default_role_base=fit_raw["default_role_base"],
            default_role_label=fit_raw["default_role_label"],
            scope_categories=tuple(
                ScopeCategory(
                    name=sc["name"],
                    term_groups=tuple((tuple(group[0]), group[1], group[2]) for group in sc["term_groups"]),
                    cap=sc["cap"],
                )
                for sc in fit_raw.get("scope_categories", [])
            ),
            penalties=tuple(
                PenaltyRule(
                    terms=tuple(p["terms"]), penalty=p["penalty"], reason=p["reason"], min_hits=p.get("min_hits", 1)
                )
                for p in fit_raw.get("penalties", [])
            ),
        )
        market = str(raw["market"])
        source_lane = str(raw["source_lane"])
    except KeyError as exc:
        raise ProductionConfigError(f"private policy file is missing required key: {exc}") from exc

    return lane, lane_priority, fit_profile, market, source_lane


def _exchange_gmail_access_token(
    context: RunContext, http: HttpClient, *, client_id: str, client_secret: str, refresh_token: str
) -> str:
    """One bounded token-refresh call via the existing HttpClient -- no
    second HTTP implementation, no retry beyond HttpClient's own policy."""
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
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Execution deadline in seconds (default {DEFAULT_TIMEOUT_SECONDS}, max {MAX_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=DEFAULT_WINDOW_HOURS,
        help=f"How far back to scan the mailbox, in hours (default {DEFAULT_WINDOW_HOURS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Scan and classify the mailbox but never move mail to J Newsletters and "
            "never persist to the Job Ledger -- read-only preview using the same "
            "scan/classify components, none of MailRouter's or Jobs's mutation paths."
        ),
    )
    return parser.parse_args(argv)


def _dry_run_preview(gmail: GmailMailboxTransport, start: datetime, end: datetime) -> dict:
    """Read-only preview: scan and classify only, via the same public
    components MailRouter itself uses internally. Never calls
    route_to_newsletters (the mutation) and never touches the Job Ledger."""
    classifier = DeterministicMailClassifier()
    messages = gmail.scan_window(start, end)
    classifications = [classifier.classify(m) for m in messages]
    confirmed = sum(1 for c in classifications if c.mail_class is MailClass.AUTOMATED_JOB_SOURCE)
    return {"scanned": len(messages), "would_route": confirmed}


def _safe_summary(
    *,
    dry_run: bool,
    elapsed_seconds: float,
    mail_preview: dict | None,
    mail_result,
    process_result,
    feature_result,
) -> dict:
    """Structured, safe terminal summary: counts and dispositions only --
    never a mail body, subject, sender, token, or raw production payload."""
    summary: dict = {"dry_run": dry_run, "elapsed_seconds": round(elapsed_seconds, 3)}

    if mail_preview is not None:
        summary["mail_preview"] = mail_preview
    if mail_result is not None:
        summary["mail"] = {
            "state": mail_result.state.value,
            "scanned": mail_result.scanned_count,
            "routed": sum(1 for r in mail_result.records if r.routed),
            "errors": len(mail_result.errors),
            "checkpoint_safe": mail_result.checkpoint_safe,
        }
    if process_result is not None:
        summary["newsletter_parse"] = {
            "state": process_result.state.value,
            "messages": len(process_result.messages),
            "observations": len(process_result.observations),
            "errors": len(process_result.errors),
        }
    if feature_result is not None:
        disposition_counts = {d.value: 0 for d in Disposition}
        for r in feature_result.ingest_results:
            disposition_counts[r.disposition.value] += 1
        summary["jobs"] = {
            "execution_status": feature_result.execution.status.value,
            "execution_code": feature_result.execution.code,
            "dispositions": disposition_counts,
            "cleanup_safe": feature_result.cleanup_safe,
        }
    return summary


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if not (0 < args.timeout_seconds <= MAX_TIMEOUT_SECONDS):
        print(f"BLOCKED: --timeout-seconds must be in (0, {MAX_TIMEOUT_SECONDS}]", file=sys.stderr)
        return 2
    if args.window_hours <= 0:
        print("BLOCKED: --window-hours must be positive", file=sys.stderr)
        return 2

    try:
        env = _require_env()
        lane, lane_priority, fit_profile, market, source_lane = _load_private_policy(
            env["NEWSLETTER_PRIVATE_POLICY_PATH"]
        )
    except ProductionConfigError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2

    context = RunContext.start(timeout_seconds=args.timeout_seconds)
    http = HttpClient()

    try:
        access_token = _exchange_gmail_access_token(
            context,
            http,
            client_id=env["GMAIL_OAUTH_CLIENT_ID"],
            client_secret=env["GMAIL_OAUTH_CLIENT_SECRET"],
            refresh_token=env["GMAIL_OAUTH_REFRESH_TOKEN"],
        )
    except (ProductionConfigError, HttpError, DeadlineExceeded) as exc:
        print(f"BLOCKED: Gmail authentication failed: {type(exc).__name__}", file=sys.stderr)
        return 2

    gmail = GmailMailboxTransport(context=context, http=http, access_token=access_token, message_factory=MailMessage)

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.window_hours)

    execution_status = "PASS"
    mail_preview = None
    mail_result = None
    process_result = None
    feature_result = None

    try:
        if args.dry_run:
            # Read-only: scan + classify only, no routing mutation.
            mail_preview = _dry_run_preview(gmail, start, end)
        else:
            # whole Gmail mailbox -> classify -> move confirmed automated
            # Newsletter mail to J Newsletters.
            mail_result = MailRouter(newsletter_boundary=NEWSLETTER_BOUNDARY).route_window([gmail], start, end)
            if not mail_result.checkpoint_safe:
                execution_status = "DEGRADED"

        # fetch J Newsletters -> parse vacancies. Safe to run in dry-run
        # too: read-only fetch, no persistence follows in that branch.
        process_result = NewsletterProcessor(boundary_name=NEWSLETTER_BOUNDARY).process_window([gmail], start, end)

        if not args.dry_run:
            # run_newsletter_feature() -> canonical Job Ledger ->
            # authoritative read-back.
            notion_transport = NotionTransport(context=context, http=http, access_token=env["NOTION_API_TOKEN"])
            repository = NotionCareerRepository(
                transport=notion_transport,
                config=NotionCareerRepositoryConfig(data_source_id=env["NOTION_JOB_LEDGER_DATA_SOURCE_ID"]),
            )
            adapter = NewsletterJobsAdapter(
                NewsletterAdapterConfig(
                    fetcher=HttpClientFetcher(http=http, context=context),
                    fit_profile=fit_profile,
                    market=market,
                    source_lane=source_lane,
                )
            )
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
    except DeadlineExceeded:
        execution_status = "DEGRADED"
        print("BLOCKED: execution deadline exhausted", file=sys.stderr)

    summary = _safe_summary(
        dry_run=args.dry_run,
        elapsed_seconds=context.elapsed_seconds(),
        mail_preview=mail_preview,
        mail_result=mail_result,
        process_result=process_result,
        feature_result=feature_result,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))

    return 0 if execution_status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
