#!/usr/bin/env python3
"""One bounded US Remote production execution.

Newsletter + US Web acquire independently, then reconcile together through one
shared Jobs ingest batch and one canonical Job Ledger. No second scheduler,
workflow engine, handoff artifact, or lane-specific persistence path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lifeos.core.http import HttpClient, HttpError
from lifeos.core.runtime import DeadlineExceeded, RunContext
from lifeos.integrations.gmail import GmailMailboxTransport
from lifeos.integrations.notion import NotionTransport, NotionTransportError
from lifeos.jobs.newsletter_adapter import HttpClientFetcher, NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition, ingest
from lifeos.jobs.newsletter_feature import _adapt_all
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig
from lifeos.jobs.terminal_evidence import FetchResponse, Fetcher
from lifeos.jobs.us_remote_acquisition import USRemoteAcquirer
from lifeos.mail.models import MailMessage
from lifeos.mail.router import MailRouter
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
_SOURCE_REGISTRY = Path(__file__).resolve().parents[1] / "contracts" / "us_remote_sources.json"


class MappingFetcher(Fetcher):
    """Optional orchestrator/browser-rendered fallback pages, kept in memory."""
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
            raise RuntimeError("browser evidence unavailable")
        return self._pages[url]

    @property
    def available(self) -> bool:
        return bool(self._pages)


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="US Remote: Newsletter + US Web -> one Job Ledger reconciliation")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--dry-run", action="store_true")
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


def _load_registry() -> dict:
    try:
        data = json.loads(_SOURCE_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionConfigError("US Remote source registry unavailable") from exc
    if not isinstance(data, dict):
        raise ProductionConfigError("US Remote source registry invalid")
    return data


def _counts(results) -> dict[str, int]:
    return {disposition.value: sum(item.disposition == disposition for item in results) for disposition in Disposition}


def main(argv: list[str] | None = None) -> int:
    args = _args(sys.argv[1:] if argv is None else argv)
    if not (0 < args.timeout_seconds <= MAX_TIMEOUT_SECONDS):
        print(f"BLOCKED: timeout must be in (0, {MAX_TIMEOUT_SECONDS}]", file=sys.stderr); return 2
    if args.window_hours <= 0:
        print("BLOCKED: window-hours must be positive", file=sys.stderr); return 2

    try:
        env = _require_env(); registry = _load_registry(); browser_evidence = _browser_evidence()
    except ProductionConfigError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr); return 2

    context = RunContext.start(timeout_seconds=args.timeout_seconds)
    http = HttpClient()
    notion = NotionTransport(context=context, http=http, access_token=env["NOTION_API_TOKEN"])
    try:
        fixture = os.getenv("NEWSLETTER_PRIVATE_POLICY_PATH")
        if fixture:
            lane, lane_priority, fit_profile, market, newsletter_source_lane = _load_private_policy(fixture)
        else:
            lane, lane_priority, fit_profile, market, newsletter_source_lane = _load_private_policy_from_notion(
                context, http, notion, notion_token=env["NOTION_API_TOKEN"]
            )
        gmail_token = _exchange_gmail_access_token(
            context, http,
            client_id=env["GMAIL_OAUTH_CLIENT_ID"],
            client_secret=env["GMAIL_OAUTH_CLIENT_SECRET"],
            refresh_token=env["GMAIL_OAUTH_REFRESH_TOKEN"],
        )
    except (ProductionConfigError, NotionTransportError, HttpError, DeadlineExceeded) as exc:
        print(f"BLOCKED: production configuration failed: {type(exc).__name__}", file=sys.stderr); return 2

    gmail = GmailMailboxTransport(context=context, http=http, access_token=gmail_token, message_factory=MailMessage)
    end = datetime.now(timezone.utc); start = end - timedelta(hours=args.window_hours)
    fallback = MappingFetcher(browser_evidence)
    fallback_fetcher = fallback if fallback.available else None

    try:
        mail_result = MailRouter(newsletter_boundary=NEWSLETTER_BOUNDARY).route_window([gmail], start, end) if not args.dry_run else None
        newsletter_result = NewsletterProcessor(boundary_name=NEWSLETTER_BOUNDARY).process_window([gmail], start, end)
        web_result = USRemoteAcquirer(context=context, http=http).acquire(registry, browser_evidence=browser_evidence)

        if args.dry_run:
            print(json.dumps({
                "status": "PASS" if newsletter_result.state is NewsletterExecutionState.PASS and web_result.complete else "DEGRADED",
                "dry_run": True,
                "elapsed_seconds": round(context.elapsed_seconds(), 3),
                "newsletter_observations": len(newsletter_result.observations),
                "web_observations": len(web_result.observations),
                "web_sources_complete": sum(item.state == "COMPLETE" for item in web_result.sources),
                "web_sources_total": len(web_result.sources),
            }, sort_keys=True))
            return 0

        repository = NotionCareerRepository(
            transport=notion,
            config=NotionCareerRepositoryConfig(data_source_id=env["NOTION_JOB_LEDGER_DATA_SOURCE_ID"]),
        )
        http_fetcher = HttpClientFetcher(http=http, context=context)
        newsletter_adapter = NewsletterJobsAdapter(NewsletterAdapterConfig(
            fetcher=http_fetcher, fallback_fetcher=fallback_fetcher,
            fit_profile=fit_profile, market=market, source_lane=newsletter_source_lane,
        ))
        web_adapter = NewsletterJobsAdapter(NewsletterAdapterConfig(
            fetcher=http_fetcher, fallback_fetcher=fallback_fetcher,
            fit_profile=fit_profile, market=market, source_lane="US Web",
        ))
        newsletter_candidates = _adapt_all(newsletter_result.observations, adapter=newsletter_adapter, context=context, max_workers=8)
        web_candidates = _adapt_all(web_result.observations, adapter=web_adapter, context=context, max_workers=8)
        candidates = newsletter_candidates + web_candidates
        results = ingest(
            candidates,
            lane=lane,
            lane_priority=lane_priority,
            repository=repository,
            run_date=end.date(),
            context=context,
        )

        fully_accounted = len(results) == len(candidates)
        unresolved = any(item.disposition is Disposition.REVIEW_DEGRADED for item in results)
        newsletter_ok = newsletter_result.state is NewsletterExecutionState.PASS
        mail_ok = bool(mail_result and mail_result.checkpoint_safe)
        pass_run = fully_accounted and not unresolved and newsletter_ok and mail_ok and web_result.complete
        summary = {
            "status": "PASS" if pass_run else "DEGRADED",
            "elapsed_seconds": round(context.elapsed_seconds(), 3),
            "mail": {"scanned": mail_result.scanned_count if mail_result else 0, "routed": sum(1 for row in mail_result.records if row.routed) if mail_result else 0, "safe": mail_ok},
            "newsletter": {"observations": len(newsletter_result.observations), "state": newsletter_result.state.value},
            "web": {"observations": len(web_result.observations), "complete_sources": sum(item.state == "COMPLETE" for item in web_result.sources), "sources": len(web_result.sources), "degraded_sources": [item.source_id for item in web_result.sources if item.state != "COMPLETE"]},
            "jobs": {"observations": len(candidates), "fully_accounted": fully_accounted, "dispositions": _counts(results)},
            "within_45s_benchmark": context.elapsed_seconds() <= 45.0,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if pass_run else 1
    except DeadlineExceeded:
        print(json.dumps({"status": "DEGRADED", "reason": "execution-deadline-exhausted", "elapsed_seconds": round(context.elapsed_seconds(), 3)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
