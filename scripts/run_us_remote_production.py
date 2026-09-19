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
from urllib.parse import urlencode

from lifeos.core.config import ConfigField, ConfigurationError, RuntimeConfig
from lifeos.core.http import HttpClient, HttpError, RetryPolicy
from lifeos.core.runtime import DeadlineExceeded, RunContext
from lifeos.integrations.gmail import GmailMailboxTransport
from lifeos.integrations.notion import NotionIdentityQuery, NotionTransport, NotionTransportError
from lifeos.jobs.fit_scoring import FitProfile, PenaltyRule, RoleFamily, ScopeCategory
from lifeos.jobs.newsletter_runtime import execute_newsletter
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.us_remote_acquisition import USRemoteAcquirer
from lifeos.jobs.us_remote_runtime import browser_evidence, execute_us_remote, load_registry
from lifeos.mail.models import MailMessage

DEFAULT_TIMEOUT_SECONDS = 45.0
MAX_TIMEOUT_SECONDS = 300.0
DEFAULT_WINDOW_HOURS = 24.0
DEFAULT_WEB_LOOKBACK_HOURS = 24.0
MAX_INBOX_STAGING_HOURS = 24.0
MAX_HISTORICAL_INBOX_RECOVERY_HOURS = 24.0 * 90
_RUNTIME_FIELDS = tuple(ConfigField(name) for name in (
    "GMAIL_OAUTH_CLIENT_ID",
    "GMAIL_OAUTH_CLIENT_SECRET",
    "GMAIL_OAUTH_REFRESH_TOKEN",
    "NOTION_API_TOKEN",
    "NOTION_JOB_LEDGER_DATA_SOURCE_ID",
))
ProductionConfigError = ConfigurationError


def _require_env() -> dict[str, str]:
    config = RuntimeConfig.load(_RUNTIME_FIELDS)
    return {name: config.require(name) for name in config.declared_names()}


_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_NOTION_SEARCH_URL, _NOTION_VERSION = "https://api.notion.com/v1/search", "2026-03-11"
_POLICY_DATABASE_TITLE, _POLICY_LANE, _POLICY_FILE_PROPERTY = "Job Lane Configuration", "US Remote", "Private Policy File"

def _exchange_gmail_access_token(context: RunContext, http: HttpClient, *, client_id: str, client_secret: str, refresh_token: str) -> str:
    body = urlencode({"client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token, "grant_type": "refresh_token"}).encode("utf-8")
    payload = http.request_json(
        context, "POST", _GOOGLE_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body,
        timeout_seconds=10.0, retry=RetryPolicy(max_attempts=1),
    )
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ConfigurationError("Gmail token refresh did not return an access token")
    return str(payload["access_token"])


def _parse_private_policy(raw: dict) -> tuple[LaneConfig, dict[str, int], FitProfile, str, str]:
    try:
        lane_raw = raw["lane"]
        lane = LaneConfig(name=lane_raw["name"], market=lane_raw["market"], fit_floor=lane_raw["fit_floor"], target_review_floor=lane_raw.get("target_review_floor"), work_mode_policy=lane_raw["work_mode_policy"], compensation_floor=lane_raw.get("compensation_floor"), freshness_gate=lane_raw["freshness_gate"], freshness_max_days=lane_raw.get("freshness_max_days"), is_target_bucket=lane_raw.get("is_target_bucket", False))
        lane_priority = {str(k): int(v) for k, v in raw["lane_priority"].items()}
        fit_raw = raw["fit_profile"]
        fit_profile = FitProfile(model_version=fit_raw["model_version"], role_families=tuple(RoleFamily(patterns=tuple(rf["patterns"]), base_score=rf["base_score"], label=rf["label"]) for rf in fit_raw.get("role_families", [])), default_role_base=fit_raw["default_role_base"], default_role_label=fit_raw["default_role_label"], scope_categories=tuple(ScopeCategory(name=sc["name"], term_groups=tuple((tuple(group[0]), group[1], group[2]) for group in sc["term_groups"]), cap=sc["cap"]) for sc in fit_raw.get("scope_categories", [])), penalties=tuple(PenaltyRule(terms=tuple(p["terms"]), penalty=p["penalty"], reason=p["reason"], min_hits=p.get("min_hits", 1)) for p in fit_raw.get("penalties", [])))
        return lane, lane_priority, fit_profile, str(raw["market"]), str(raw["source_lane"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"private policy is invalid: {type(exc).__name__}") from exc

def _load_private_policy(path: str) -> tuple[LaneConfig, dict[str, int], FitProfile, str, str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc: raise ConfigurationError(f"could not read private policy fixture: {type(exc).__name__}") from exc
    if not isinstance(raw, dict): raise ConfigurationError("private policy fixture was not an object")
    return _parse_private_policy(raw)
def _plain_title(item: dict) -> str:
    return "".join(str(part.get("plain_text") or "") for part in (item.get("title") or []) if isinstance(part, dict)).strip()

def _policy_file_url(page: dict) -> str:
    files = (((page.get("properties") or {}).get(_POLICY_FILE_PROPERTY) or {}).get("files") or [])
    if len(files) != 1 or not isinstance(files[0], dict): raise ConfigurationError("US Remote private policy file is missing or ambiguous")
    entry = files[0]
    payload = entry.get("file") if entry.get("type") == "file" else entry.get("external")
    url = payload.get("url") if isinstance(payload, dict) else None
    if not url: raise ConfigurationError("US Remote private policy file has no readable URL")
    return str(url)


def _load_private_policy_from_notion(context, http, notion, *, notion_token: str):
    search = http.request_json(context, "POST", _NOTION_SEARCH_URL, headers={"Authorization": f"Bearer {notion_token}", "Notion-Version": _NOTION_VERSION, "Accept": "application/json", "Content-Type": "application/json"}, json_body={"query": _POLICY_DATABASE_TITLE, "filter": {"property": "object", "value": "data_source"}, "page_size": 20}, timeout_seconds=10.0, retry=RetryPolicy(max_attempts=2, backoff_seconds=0.1, max_backoff_seconds=1.0))
    if not isinstance(search, dict): raise ConfigurationError("Notion configuration search returned an invalid response")
    matches = [item for item in (search.get("results") or []) if isinstance(item, dict) and _plain_title(item) == _POLICY_DATABASE_TITLE and item.get("id")]
    if len(matches) != 1: raise ConfigurationError("canonical Job Lane Configuration data source was not uniquely resolvable")
    rows = notion.query_data_source(str(matches[0]["id"]), NotionIdentityQuery(property_name="Lane", property_type="title", values=(_POLICY_LANE,)), page_size=10)
    if len(rows) != 1: raise ConfigurationError("canonical US Remote lane was not uniquely resolvable")
    raw = http.request_json(context, "GET", _policy_file_url(rows[0]), timeout_seconds=10.0, retry=RetryPolicy(max_attempts=2, backoff_seconds=0.1, max_backoff_seconds=1.0))
    if not isinstance(raw, dict): raise ConfigurationError("canonical private policy was not a JSON object")
    return _parse_private_policy(raw)


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="US Remote: Newsletter + US Web -> one Job Ledger reconciliation")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--web-lookback-hours", type=float, default=DEFAULT_WEB_LOOKBACK_HOURS)
    parser.add_argument("--full-web-sweep", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--newsletter-only",
        action="store_true",
        help="Run only the extracted Newsletter runtime; intended for controlled manual UAT before production cutover.",
    )
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
    except ConfigurationError as exc:
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
    except (RuntimeError, NotionTransportError, HttpError, DeadlineExceeded) as exc:
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
        inbox_start = end - timedelta(hours=args.historical_inbox_recovery_hours)
        inbox_mode = "historical_recovery"
    else:
        inbox_start = max(start, end - timedelta(hours=MAX_INBOX_STAGING_HOURS))
        inbox_mode = "normal"

    if args.newsletter_only:
        result = execute_newsletter(
            context=context,
            http=http,
            notion=notion,
            gmail=gmail,
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
            dry_run=args.dry_run,
        )
    else:
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
