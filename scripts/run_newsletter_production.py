#!/usr/bin/env python3
"""Production Newsletter entry point: one bounded execution.

whole Gmail mailbox -> classify -> route confirmed automated Newsletter mail
-> parse vacancies -> Jobs terminal evidence/Fit/qualification -> canonical
Job Ledger -> authoritative read-back.

Personal career policy is never stored in this public repository. The live
policy is loaded at runtime from the private Notion Job Lane Configuration
record for US Remote. GitHub remains source/CI/manual-UAT only; this script
creates no scheduler or orchestration layer.
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
from lifeos.integrations.notion import NotionIdentityQuery, NotionTransport, NotionTransportError
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
MAX_TIMEOUT_SECONDS = 180.0
DEFAULT_WINDOW_HOURS = 24.0
NEWSLETTER_BOUNDARY = "J Newsletters"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_NOTION_SEARCH_URL = "https://api.notion.com/v1/search"
_NOTION_VERSION = "2026-03-11"
_POLICY_DATABASE_TITLE = "Job Lane Configuration"
_POLICY_LANE = "US Remote"
_POLICY_FILE_PROPERTY = "Private Policy File"

REQUIRED_ENV = (
    "GMAIL_OAUTH_CLIENT_ID",
    "GMAIL_OAUTH_CLIENT_SECRET",
    "GMAIL_OAUTH_REFRESH_TOKEN",
    "NOTION_API_TOKEN",
    "NOTION_JOB_LEDGER_DATA_SOURCE_ID",
)


class ProductionConfigError(RuntimeError):
    """Fail-closed configuration problem. Never carries a secret value."""


def _require_env() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        raise ProductionConfigError("missing required runtime configuration: " + ", ".join(missing))
    return {name: os.environ[name] for name in REQUIRED_ENV}


def _parse_private_policy(raw: dict) -> tuple[LaneConfig, dict[str, int], FitProfile, str, str]:
    try:
        lane_raw = raw["lane"]
        lane = LaneConfig(name=lane_raw["name"], market=lane_raw["market"], fit_floor=lane_raw["fit_floor"], target_review_floor=lane_raw.get("target_review_floor"), work_mode_policy=lane_raw["work_mode_policy"], compensation_floor=lane_raw.get("compensation_floor"), freshness_gate=lane_raw["freshness_gate"], freshness_max_days=lane_raw.get("freshness_max_days"), is_target_bucket=lane_raw.get("is_target_bucket", False))
        lane_priority = {str(k): int(v) for k, v in raw["lane_priority"].items()}
        fit_raw = raw["fit_profile"]
        fit_profile = FitProfile(model_version=fit_raw["model_version"], role_families=tuple(RoleFamily(patterns=tuple(rf["patterns"]), base_score=rf["base_score"], label=rf["label"]) for rf in fit_raw.get("role_families", [])), default_role_base=fit_raw["default_role_base"], default_role_label=fit_raw["default_role_label"], scope_categories=tuple(ScopeCategory(name=sc["name"], term_groups=tuple((tuple(group[0]), group[1], group[2]) for group in sc["term_groups"]), cap=sc["cap"]) for sc in fit_raw.get("scope_categories", [])), penalties=tuple(PenaltyRule(terms=tuple(p["terms"]), penalty=p["penalty"], reason=p["reason"], min_hits=p.get("min_hits", 1)) for p in fit_raw.get("penalties", [])))
        market = str(raw["market"]); source_lane = str(raw["source_lane"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductionConfigError(f"private policy is invalid: {type(exc).__name__}") from exc
    return lane, lane_priority, fit_profile, market, source_lane


def _load_private_policy(path: str) -> tuple[LaneConfig, dict[str, int], FitProfile, str, str]:
    """Compatibility helper for synthetic tests; not used by the live path."""
    try:
        with open(path, "r", encoding="utf-8") as handle: raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionConfigError(f"could not read private policy fixture: {type(exc).__name__}") from exc
    if not isinstance(raw, dict): raise ProductionConfigError("private policy fixture was not an object")
    return _parse_private_policy(raw)


def _plain_title(item: dict) -> str:
    return "".join(str(part.get("plain_text") or "") for part in (item.get("title") or []) if isinstance(part, dict)).strip()


def _policy_file_url(page: dict) -> str:
    prop = (page.get("properties") or {}).get(_POLICY_FILE_PROPERTY) or {}; files = prop.get("files") or []
    if len(files) != 1 or not isinstance(files[0], dict): raise ProductionConfigError("US Remote private policy file is missing or ambiguous")
    entry = files[0]; payload = entry.get("file") if entry.get("type") == "file" else entry.get("external"); url = payload.get("url") if isinstance(payload, dict) else None
    if not url: raise ProductionConfigError("US Remote private policy file has no readable URL")
    return str(url)


def _load_private_policy_from_notion(context: RunContext, http: HttpClient, notion: NotionTransport, *, notion_token: str) -> tuple[LaneConfig, dict[str, int], FitProfile, str, str]:
    """Resolve private policy from the canonical Notion US Remote lane."""
    search = http.request_json(context, "POST", _NOTION_SEARCH_URL, headers={"Authorization": f"Bearer {notion_token}", "Notion-Version": _NOTION_VERSION, "Accept": "application/json", "Content-Type": "application/json"}, json_body={"query": _POLICY_DATABASE_TITLE, "filter": {"property": "object", "value": "data_source"}, "page_size": 20}, timeout_seconds=10.0, retry=RetryPolicy(max_attempts=2, backoff_seconds=0.1, max_backoff_seconds=1.0))
    if not isinstance(search, dict): raise ProductionConfigError("Notion configuration search returned an invalid response")
    matches = [item for item in (search.get("results") or []) if isinstance(item, dict) and _plain_title(item) == _POLICY_DATABASE_TITLE and item.get("id")]
    if len(matches) != 1: raise ProductionConfigError("canonical Job Lane Configuration data source was not uniquely resolvable")
    rows = notion.query_data_source(str(matches[0]["id"]), NotionIdentityQuery(property_name="Lane", property_type="title", values=(_POLICY_LANE,)), page_size=10)
    if len(rows) != 1: raise ProductionConfigError("canonical US Remote lane was not uniquely resolvable")
    raw = http.request_json(context, "GET", _policy_file_url(rows[0]), timeout_seconds=10.0, retry=RetryPolicy(max_attempts=2, backoff_seconds=0.1, max_backoff_seconds=1.0))
    if not isinstance(raw, dict): raise ProductionConfigError("canonical private policy was not a JSON object")
    return _parse_private_policy(raw)


def _exchange_gmail_access_token(context: RunContext, http: HttpClient, *, client_id: str, client_secret: str, refresh_token: str) -> str:
    body = urlencode({"client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token, "grant_type": "refresh_token"}).encode("utf-8")
    payload = http.request_json(context, "POST", _GOOGLE_TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body, timeout_seconds=10.0, retry=RetryPolicy(max_attempts=1))
    if not isinstance(payload, dict) or not payload.get("access_token"): raise ProductionConfigError("Gmail token refresh did not return an access token")
    return str(payload["access_token"])


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Production Newsletter entry point"); parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS); parser.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS); parser.add_argument("--dry-run", action="store_true"); return parser.parse_args(argv)


def _dry_run_preview(gmail: GmailMailboxTransport, start: datetime, end: datetime) -> dict:
    classifier = DeterministicMailClassifier(); messages = gmail.scan_window(start, end); classifications = [classifier.classify(m) for m in messages]; return {"scanned": len(messages), "would_route": sum(1 for c in classifications if c.mail_class is MailClass.AUTOMATED_JOB_SOURCE)}


def _safe_summary(*, dry_run: bool, elapsed_seconds: float, mail_preview, mail_result, process_result, feature_result) -> dict:
    summary: dict = {"dry_run": dry_run, "elapsed_seconds": round(elapsed_seconds, 3)}
    if mail_preview is not None: summary["mail_preview"] = mail_preview
    if mail_result is not None: summary["mail"] = {"state": mail_result.state.value, "scanned": mail_result.scanned_count, "routed": sum(1 for r in mail_result.records if r.routed), "errors": len(mail_result.errors), "checkpoint_safe": mail_result.checkpoint_safe}
    if process_result is not None: summary["newsletter_parse"] = {"state": process_result.state.value, "messages": len(process_result.messages), "observations": len(process_result.observations), "errors": len(process_result.errors)}
    if feature_result is not None:
        disposition_counts = {d.value: 0 for d in Disposition}
        for result in feature_result.ingest_results: disposition_counts[result.disposition.value] += 1
        summary["jobs"] = {"execution_status": feature_result.execution.status.value, "execution_code": feature_result.execution.code, "dispositions": disposition_counts, "cleanup_safe": feature_result.cleanup_safe}
    return summary


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if not (0 < args.timeout_seconds <= MAX_TIMEOUT_SECONDS): print(f"BLOCKED: --timeout-seconds must be in (0, {MAX_TIMEOUT_SECONDS}]", file=sys.stderr); return 2
    if args.window_hours <= 0: print("BLOCKED: --window-hours must be positive", file=sys.stderr); return 2
    try: env = _require_env()
    except ProductionConfigError as exc: print(f"BLOCKED: {exc}", file=sys.stderr); return 2
    context = RunContext.start(timeout_seconds=args.timeout_seconds); http = HttpClient(); notion_transport = NotionTransport(context=context, http=http, access_token=env["NOTION_API_TOKEN"])
    try:
        lane, lane_priority, fit_profile, market, source_lane = _load_private_policy_from_notion(context, http, notion_transport, notion_token=env["NOTION_API_TOKEN"])
        access_token = _exchange_gmail_access_token(context, http, client_id=env["GMAIL_OAUTH_CLIENT_ID"], client_secret=env["GMAIL_OAUTH_CLIENT_SECRET"], refresh_token=env["GMAIL_OAUTH_REFRESH_TOKEN"])
    except (ProductionConfigError, NotionTransportError, HttpError, DeadlineExceeded) as exc: print(f"BLOCKED: production configuration failed: {type(exc).__name__}", file=sys.stderr); return 2
    gmail = GmailMailboxTransport(context=context, http=http, access_token=access_token, message_factory=MailMessage); end = datetime.now(timezone.utc); start = end - timedelta(hours=args.window_hours)
    execution_status = "PASS"; mail_preview = None; mail_result = None; process_result = None; feature_result = None
    try:
        if args.dry_run: mail_preview = _dry_run_preview(gmail, start, end)
        else:
            mail_result = MailRouter(newsletter_boundary=NEWSLETTER_BOUNDARY).route_window([gmail], start, end)
            if not mail_result.checkpoint_safe: execution_status = "DEGRADED"
        process_result = NewsletterProcessor(boundary_name=NEWSLETTER_BOUNDARY).process_window([gmail], start, end)
        if not args.dry_run:
            repository = NotionCareerRepository(transport=notion_transport, config=NotionCareerRepositoryConfig(data_source_id=env["NOTION_JOB_LEDGER_DATA_SOURCE_ID"]))
            adapter = NewsletterJobsAdapter(NewsletterAdapterConfig(fetcher=HttpClientFetcher(http=http, context=context), fit_profile=fit_profile, market=market, source_lane=source_lane))
            feature_result = run_newsletter_feature(process_result, adapter=adapter, lane=lane, lane_priority=lane_priority, repository=repository, run_date=end.date(), context=context)
            if feature_result.execution.status.value != "PASS": execution_status = feature_result.execution.status.value
    except DeadlineExceeded: execution_status = "DEGRADED"; print("BLOCKED: execution deadline exhausted", file=sys.stderr)
    print(json.dumps(_safe_summary(dry_run=args.dry_run, elapsed_seconds=context.elapsed_seconds(), mail_preview=mail_preview, mail_result=mail_result, process_result=process_result, feature_result=feature_result), indent=2, sort_keys=True)); return 0 if execution_status == "PASS" else 1


if __name__ == "__main__": raise SystemExit(main())
