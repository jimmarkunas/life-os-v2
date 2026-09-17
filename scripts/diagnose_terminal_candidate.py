#!/usr/bin/env python3
"""Read-only diagnostic for one US Web terminal vacancy candidate.

Selects the same first post-prefilter Web candidate shape used by the live 1x1
proof, then traces primary HTTP and browser-fallback terminal evidence fetches.
No Gmail or Job Ledger mutations occur.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from lifeos.core.http import HttpClient
from lifeos.core.runtime import RunContext
from lifeos.integrations.notion import NotionTransport
from lifeos.jobs.newsletter_adapter import HttpClientFetcher
from lifeos.jobs.terminal_evidence import (
    FetchResponse,
    Fetcher,
    _downstream_candidates,
    _extract_posting_date_raw,
    _extract_terminal_description,
    acquire_terminal_vacancy_evidence,
    is_provider_intermediary_source,
)
from scripts import run_us_remote_production as prod


def _host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlsplit(url).netloc.casefold()
    except ValueError:
        return None


class TracingFetcher(Fetcher):
    def __init__(self, name: str, inner: Fetcher) -> None:
        self.name = name
        self.inner = inner
        self.events: list[dict[str, object]] = []

    def get(self, url: str) -> FetchResponse:
        event: dict[str, object] = {
            "transport": self.name,
            "requested_url": url,
            "requested_host": _host(url),
        }
        try:
            response = self.inner.get(url)
        except Exception as exc:
            event["error"] = type(exc).__name__
            self.events.append(event)
            raise
        event.update(
            {
                "final_url": response.final_url,
                "final_host": _host(response.final_url),
                "body_bytes": len(response.body.encode("utf-8", errors="ignore")),
                "intermediary_final": is_provider_intermediary_source(response.final_url),
                "has_description": bool(_extract_terminal_description(response.body)),
                "has_posting_date": bool(_extract_posting_date_raw(response.body)),
                "downstream_candidates": len(_downstream_candidates(response.body, response.final_url)),
            }
        )
        self.events.append(event)
        return response


def main() -> int:
    context = RunContext.start(timeout_seconds=120)
    http = HttpClient()
    env = prod._require_env()
    registry = prod._load_registry()
    browser_evidence = prod._browser_evidence()
    notion = NotionTransport(context=context, http=http, access_token=env["NOTION_API_TOKEN"])

    fixture = os.getenv("NEWSLETTER_PRIVATE_POLICY_PATH")
    if fixture:
        lane, _lane_priority, fit_profile, _market, _newsletter_source_lane = prod._load_private_policy(fixture)
    else:
        lane, _lane_priority, fit_profile, _market, _newsletter_source_lane = prod._load_private_policy_from_notion(
            context, http, notion, notion_token=env["NOTION_API_TOKEN"]
        )

    end = datetime.now(timezone.utc)
    fallback = prod._fallback_fetcher(context, browser_evidence)
    web_result = prod.USRemoteAcquirer(
        context=context,
        http=http,
        fallback_fetcher=fallback,
    ).acquire(
        registry,
        browser_evidence=browser_evidence,
        since=end - timedelta(hours=24),
        full_sweep=False,
        now=end,
    )

    web_to_resolve, _ = prod._partition_observations(
        web_result.observations,
        lane=lane,
        fit_profile=fit_profile,
    )
    if not web_to_resolve:
        print(json.dumps({"status": "BLOCKED", "reason": "no post-prefilter Web candidate"}, sort_keys=True))
        return 2

    observation = web_to_resolve[0]
    if not observation.source_apply_url:
        print(json.dumps({"status": "BLOCKED", "reason": "selected candidate has no source_apply_url"}, sort_keys=True))
        return 2

    primary = TracingFetcher("http", HttpClientFetcher(http=http, context=context))
    fallback_trace = TracingFetcher("browser", fallback) if fallback is not None else None
    evidence = acquire_terminal_vacancy_evidence(
        observation.source_apply_url,
        fetcher=primary,
        fallback_fetcher=fallback_trace,
    )

    events = primary.events + (fallback_trace.events if fallback_trace else [])
    summary = {
        "status": "PASS" if evidence is not None else "DEGRADED",
        "candidate": {
            "evidence_ref": observation.evidence_ref,
            "company": observation.company,
            "role": observation.role,
            "source_url": observation.source_apply_url,
            "source_host": _host(observation.source_apply_url),
        },
        "terminal_evidence": None if evidence is None else {
            "canonical_url": evidence.canonical_url,
            "canonical_host": _host(evidence.canonical_url),
            "posting_date_raw": evidence.posting_date_raw,
            "evidence_source": evidence.evidence_source,
            "resolution_chain": evidence.resolution_chain,
        },
        "fetch_events": events,
        "elapsed_seconds": round(context.elapsed_seconds(), 3),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if evidence is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
