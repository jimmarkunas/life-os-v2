#!/usr/bin/env python3
"""Read-only live smoke for a bounded US Remote batch.

Exercises the real Gmail, Web acquisition, cheap prefilter, HTTP/browser terminal
resolution, and private policy boundaries without staging mail, writing the Job
Ledger, or marking Newsletter messages processed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from time import perf_counter

from lifeos.core.http import HttpClient
from lifeos.core.runtime import DeadlineExceeded, RunContext
from lifeos.integrations.gmail import GmailMailboxTransport
from lifeos.integrations.notion import NotionTransport
from lifeos.jobs.newsletter_adapter import HttpClientFetcher, NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_feature import _adapt_all
from lifeos.jobs.us_remote_runtime import browser_evidence, fallback_fetcher, load_registry, partition_observations
from lifeos.mail.models import MailMessage
from lifeos.newsletter.processor import NewsletterProcessor
from scripts import run_us_remote_production as prod
from scripts.run_newsletter_production import (
    _exchange_gmail_access_token,
    _load_private_policy,
    _load_private_policy_from_notion,
    _require_env,
)


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only 10x10 US Remote batch smoke")
    parser.add_argument("--newsletter-messages", type=int, default=10)
    parser.add_argument("--web-candidates", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(sys.argv[1:] if argv is None else argv)
    if args.newsletter_messages != 10 or args.web_candidates != 10:
        print("BLOCKED: smoke is fixed at 10 Newsletter messages + 10 Web candidates", file=sys.stderr)
        return 2
    if not (0 < args.timeout_seconds <= 45.0):
        print("BLOCKED: smoke timeout must be in (0, 45]", file=sys.stderr)
        return 2

    context = RunContext.start(timeout_seconds=args.timeout_seconds)
    http = HttpClient()
    timings: dict[str, float] = {}
    try:
        env = _require_env()
        registry = load_registry()
        notion = NotionTransport(context=context, http=http, access_token=env["NOTION_API_TOKEN"])
        fixture = os.getenv("NEWSLETTER_PRIVATE_POLICY_PATH")
        if fixture:
            lane, _lane_priority, fit_profile, market, newsletter_source_lane = _load_private_policy(fixture)
        else:
            lane, _lane_priority, fit_profile, market, newsletter_source_lane = _load_private_policy_from_notion(
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
        gmail = GmailMailboxTransport(
            context=context,
            http=http,
            access_token=gmail_token,
            message_factory=MailMessage,
        )
        browser_evidence_payload = browser_evidence()
        fallback = fallback_fetcher(context, browser_evidence_payload)
        end = datetime.now(timezone.utc)

        started = perf_counter()
        newsletter_result = NewsletterProcessor(boundary_name=prod.NEWSLETTER_BOUNDARY).process_window(
            [gmail], end - timedelta(days=60), end
        )
        selected_messages = newsletter_result.messages[: args.newsletter_messages]
        newsletter_observations = tuple(
            observation
            for message in selected_messages
            for observation in message.observations
        )
        timings["newsletter_fetch_parse"] = round(perf_counter() - started, 3)

        started = perf_counter()
        web_result = prod.USRemoteAcquirer(
            context=context,
            http=http,
            fallback_fetcher=fallback,
        ).acquire(
            registry,
            browser_evidence=browser_evidence_payload,
            since=end - timedelta(hours=24),
            full_sweep=False,
            now=end,
        )
        timings["web_acquire"] = round(perf_counter() - started, 3)

        started = perf_counter()
        newsletter_to_resolve, newsletter_preexcluded = partition_observations(
            newsletter_observations,
            lane=lane,
            fit_profile=fit_profile,
        )
        web_to_resolve, web_preexcluded = partition_observations(
            web_result.observations,
            lane=lane,
            fit_profile=fit_profile,
        )
        web_to_resolve = web_to_resolve[: args.web_candidates]
        timings["cheap_prefilter"] = round(perf_counter() - started, 3)

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

        started = perf_counter()
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
        timings["terminal_resolution"] = round(perf_counter() - started, 3)

        print(json.dumps({
            "status": "PASS",
            "dry_run": True,
            "elapsed_seconds": round(context.elapsed_seconds(), 3),
            "newsletter_messages_selected": len(selected_messages),
            "newsletter_observations_selected": len(newsletter_observations),
            "newsletter_preexcluded": len(newsletter_preexcluded),
            "newsletter_terminal_attempts": len(newsletter_to_resolve),
            "newsletter_candidates": len(newsletter_candidates),
            "web_observations": len(web_result.observations),
            "web_preexcluded": len(web_preexcluded),
            "web_terminal_attempts": len(web_to_resolve),
            "web_candidates": len(web_candidates),
            "browser_fallback_available": fallback is not None,
            "timings": timings,
            "writes": 0,
        }, sort_keys=True))
        return 0
    except DeadlineExceeded:
        print(json.dumps({
            "status": "DEGRADED",
            "reason": "execution-deadline-exhausted",
            "elapsed_seconds": round(context.elapsed_seconds(), 3),
            "timings": timings,
            "writes": 0,
        }, sort_keys=True))
        return 1
    except Exception as exc:
        print(json.dumps({
            "status": "DEGRADED",
            "reason": type(exc).__name__,
            "elapsed_seconds": round(context.elapsed_seconds(), 3),
            "timings": timings,
            "writes": 0,
        }, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
