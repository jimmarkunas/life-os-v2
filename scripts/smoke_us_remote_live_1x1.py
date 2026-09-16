#!/usr/bin/env python3
"""One-shot live US Remote proof: one staged Newsletter message + one Web candidate."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from time import perf_counter

from lifeos.core.http import HttpClient
from lifeos.core.runtime import DeadlineExceeded, RunContext
from lifeos.integrations.gmail import GmailMailboxTransport
from lifeos.integrations.notion import NotionTransport
from lifeos.jobs.newsletter_adapter import HttpClientFetcher, NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition, ingest
from lifeos.jobs.newsletter_feature import _adapt_all
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig
from lifeos.mail.models import MailMessage
from lifeos.newsletter.models import ParseState
from lifeos.newsletter.parsers import parse_message
from scripts import run_us_remote_production as prod


def main() -> int:
    context = RunContext.start(timeout_seconds=120)
    http = HttpClient()
    timings: dict[str, float] = {}
    try:
        env = prod._require_env()
        registry = prod._load_registry()
        notion = NotionTransport(context=context, http=http, access_token=env["NOTION_API_TOKEN"])
        fixture = os.getenv("NEWSLETTER_PRIVATE_POLICY_PATH")
        if fixture:
            lane, lane_priority, fit_profile, market, newsletter_source_lane = prod._load_private_policy(fixture)
        else:
            lane, lane_priority, fit_profile, market, newsletter_source_lane = prod._load_private_policy_from_notion(
                context, http, notion, notion_token=env["NOTION_API_TOKEN"]
            )
        gmail_token = prod._exchange_gmail_access_token(
            context, http,
            client_id=env["GMAIL_OAUTH_CLIENT_ID"],
            client_secret=env["GMAIL_OAUTH_CLIENT_SECRET"],
            refresh_token=env["GMAIL_OAUTH_REFRESH_TOKEN"],
        )
        gmail = GmailMailboxTransport(context=context, http=http, access_token=gmail_token, message_factory=MailMessage)
        end = datetime.now(timezone.utc)
        browser_evidence = prod._browser_evidence()
        fallback_fetcher = prod._fallback_fetcher(context, browser_evidence)

        started = perf_counter()
        label_id = gmail._resolve_label_id(prod.NEWSLETTER_BOUNDARY)
        processed_name = f"{prod.NEWSLETTER_BOUNDARY}/Processed"
        try:
            gmail._resolve_label_id(processed_name)
        except Exception:
            processed_name = ""
        ids = gmail._list_message_ids(
            end - timedelta(days=60), end,
            label_id=label_id,
            exclude_label_name=processed_name or None,
        )
        selected_message = None
        for message_id in ids[:10]:
            parsed = parse_message(gmail._fetch_routed_message(message_id))
            if parsed.state is ParseState.PASS and parsed.observations:
                selected_message = parsed
                break
        if selected_message is None:
            raise RuntimeError("no parseable staged Newsletter message with vacancies in bounded selection")
        newsletter_observations = tuple(selected_message.observations)
        timings["newsletter_select_parse"] = round(perf_counter() - started, 3)

        started = perf_counter()
        web_result = prod.USRemoteAcquirer(context=context, http=http, fallback_fetcher=fallback_fetcher).acquire(
            registry,
            browser_evidence=browser_evidence,
            since=end - timedelta(hours=24),
            full_sweep=False,
            now=end,
        )
        timings["web_acquire"] = round(perf_counter() - started, 3)

        newsletter_to_resolve, newsletter_preexcluded = prod._partition_observations(
            newsletter_observations, lane=lane, fit_profile=fit_profile
        )
        web_to_resolve, _ = prod._partition_observations(web_result.observations, lane=lane, fit_profile=fit_profile)
        if not web_to_resolve:
            raise RuntimeError("no eligible Web candidate available for 1x1 proof")
        web_to_resolve = web_to_resolve[:1]

        repository = NotionCareerRepository(
            transport=notion,
            config=NotionCareerRepositoryConfig(data_source_id=env["NOTION_JOB_LEDGER_DATA_SOURCE_ID"]),
        )
        http_fetcher = HttpClientFetcher(http=http, context=context)
        newsletter_adapter = NewsletterJobsAdapter(NewsletterAdapterConfig(
            fetcher=http_fetcher, fallback_fetcher=fallback_fetcher, fit_profile=fit_profile,
            market=market, source_lane=newsletter_source_lane,
        ))
        web_adapter = NewsletterJobsAdapter(NewsletterAdapterConfig(
            fetcher=http_fetcher, fallback_fetcher=fallback_fetcher, fit_profile=fit_profile,
            market=market, source_lane="US Web",
        ))

        started = perf_counter()
        newsletter_candidates = _adapt_all(tuple(newsletter_to_resolve), adapter=newsletter_adapter, context=context, max_workers=8)
        web_candidates = _adapt_all(tuple(web_to_resolve), adapter=web_adapter, context=context, max_workers=1)
        timings["terminal_resolution"] = round(perf_counter() - started, 3)

        started = perf_counter()
        ingest_results = ingest(
            newsletter_candidates + web_candidates,
            lane=lane,
            lane_priority=lane_priority,
            repository=repository,
            run_date=end.date(),
            context=context,
        )
        timings["persist_readback"] = round(perf_counter() - started, 3)

        newsletter_ingest_count = len(newsletter_candidates)
        newsletter_results = list(newsletter_preexcluded) + ingest_results[:newsletter_ingest_count]
        web_results = ingest_results[newsletter_ingest_count:]
        newsletter_accounted = len(newsletter_results) == len(newsletter_observations)
        newsletter_degraded = any(r.disposition is Disposition.REVIEW_DEGRADED for r in newsletter_results)
        web_accounted = len(web_results) == 1
        web_degraded = any(r.disposition is Disposition.REVIEW_DEGRADED for r in web_results)

        processed = False
        if newsletter_accounted and not newsletter_degraded:
            mailbox, message_id = selected_message.message_ref.split(":", 1)
            if mailbox != "gmail" or not message_id:
                raise RuntimeError("selected Newsletter message is not Gmail-backed")
            gmail.mark_newsletter_processed(message_id, prod.NEWSLETTER_BOUNDARY)
            processed = True

        durable = sum(r.disposition in {Disposition.CREATED, Disposition.UPDATED} for r in newsletter_results + web_results)
        status = "PASS" if newsletter_accounted and web_accounted and not newsletter_degraded and not web_degraded and processed else "DEGRADED"
        print(json.dumps({
            "status": status,
            "elapsed_seconds": round(context.elapsed_seconds(), 3),
            "newsletter_messages": 1,
            "newsletter_observations": len(newsletter_observations),
            "newsletter_processed": processed,
            "web_candidates": 1,
            "durable_job_writes_read_back": durable,
            "dispositions": prod._counts(newsletter_results + web_results),
            "timings": timings,
        }, sort_keys=True))
        return 0 if status == "PASS" else 1
    except DeadlineExceeded:
        print(json.dumps({"status": "DEGRADED", "reason": "execution-deadline-exhausted", "elapsed_seconds": round(context.elapsed_seconds(), 3), "timings": timings}, sort_keys=True))
        return 1
    except Exception as exc:
        print(json.dumps({"status": "DEGRADED", "reason": type(exc).__name__, "elapsed_seconds": round(context.elapsed_seconds(), 3), "timings": timings}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
