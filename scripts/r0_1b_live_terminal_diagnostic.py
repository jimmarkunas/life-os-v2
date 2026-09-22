"""Temporary bounded live trace for exactly one Jobright and one Lensa card."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from lifeos.core.config import ConfigField, RuntimeConfig
from lifeos.core.http import HttpClient
from lifeos.core.runtime import RunContext
from lifeos.integrations.gmail import GmailMailboxTransport
from scripts.run_us_remote_production import _exchange_gmail_access_token
from lifeos.jobs.terminal_evidence import (
    FetchResponse,
    _collect_hrefs,
    _extract_terminal_description,
    _downstream_score,
    acquire_terminal_vacancy_evidence,
    fallback_fetcher,
    is_provider_intermediary_source,
)
from lifeos.jobs.newsletter_adapter import HttpClientFetcher
from lifeos.newsletter import parse_message
from lifeos.mail.models import MailMessage


_FIELDS = tuple(ConfigField(name) for name in (
    "GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET", "GMAIL_OAUTH_REFRESH_TOKEN",
))


def _safe_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value)
    path = re.sub(r"\d{4,}", "<id>", parts.path)
    path = re.sub(r"/[A-Za-z0-9_-]{12,}(?=/|$)", "/<id>", path)
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, "", ""))


def _provider_hint(message: MailMessage) -> str | None:
    text = f"{message.sender} {message.subject}".casefold()
    if "jobright" in text:
        return "Jobright"
    if "lensa" in text:
        return "Lensa"
    return None


class _TraceFetcher:
    def __init__(self, delegate, events: list[dict[str, object]], label: str):
        self._delegate = delegate
        self._events = events
        self._label = label

    def get(self, url: str) -> FetchResponse:
        started = datetime.now(timezone.utc)
        try:
            response = self._delegate.get(url)
        except Exception as exc:
            self._events.append({"fetcher": self._label, "requested": _safe_url(url), "result": type(exc).__name__, "elapsed_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000)})
            raise
        body = response.body or ""
        self._events.append({
            "fetcher": self._label,
            "requested": _safe_url(url),
            "result": "response",
            "final": _safe_url(response.final_url),
            "body": "html" if "<" in body else "non_html",
            "body_length": len(body),
            "elapsed_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        })
        return response


def _trace_observation(observation, *, context: RunContext, http: HttpClient) -> dict[str, object]:
    events: list[dict[str, object]] = []
    primary = _TraceFetcher(HttpClientFetcher(http=http, context=context), events, "primary")
    fallback = fallback_fetcher(context, None)
    traced_fallback = _TraceFetcher(fallback, events, "fallback") if fallback else None
    started = datetime.now(timezone.utc)
    evidence = acquire_terminal_vacancy_evidence(
        observation.source_apply_url,
        fetcher=primary,
        fallback_fetcher=traced_fallback,
        company=observation.company,
        role=observation.role,
        provider_job_id=observation.provider_job_id,
    )
    elapsed_ms = round((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    final_url = evidence.canonical_url if evidence else None
    jd = evidence.description_text if evidence else ""
    apply_candidates: list[str] = []
    if evidence:
        apply_candidates = [_safe_url(item) for item in _collect_hrefs(jd, final_url or "") if _downstream_score(item)[0] >= 2]
    return {
        "provider": observation.source_provider,
        "source_url": _safe_url(observation.source_apply_url),
        "requested_hops": events,
        "redirect_chain": [_safe_url(item) for item in (evidence.resolution_chain if evidence else ())],
        "final_url": _safe_url(final_url),
        "terminal_classification": "intermediary" if is_provider_intermediary_source(final_url or "") else ("terminal" if final_url else "unresolved"),
        "body_classification": "usable_html" if jd else "missing_or_unusable",
        "apply_candidate_present": bool(final_url),
        "apply_actionable_vacancy_specific": bool(final_url and not is_provider_intermediary_source(final_url)),
        "apply_candidates_in_terminal_body": apply_candidates,
        "jd_present": bool(jd),
        "jd_length": len(jd),
        "jd_authority": evidence.evidence_source if evidence else None,
        "accepted": evidence is not None and bool(final_url and jd),
        "rejection_reason": None if evidence else "resolver returned no acceptable terminal evidence",
        "elapsed_ms": elapsed_ms,
    }


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        assert _safe_url("https://jobright.ai/jobs/info/123456789?trackingToken=secret") == "https://jobright.ai/jobs/info/<id>"
        assert _provider_hint(MailMessage("gmail", "x", datetime.now(timezone.utc), "alerts@jobright.example", "Jobs")) == "Jobright"
        assert _provider_hint(MailMessage("gmail", "x", datetime.now(timezone.utc), "alerts@lensa.example", "Jobs")) == "Lensa"
        print("R0_1B_SELF_TEST: PASS")
        return 0

    config = RuntimeConfig.load(_FIELDS)
    context = RunContext.start(timeout_seconds=285)
    http = HttpClient()
    token = _exchange_gmail_access_token(context, http, client_id=config.require("GMAIL_OAUTH_CLIENT_ID"), client_secret=config.require("GMAIL_OAUTH_CLIENT_SECRET"), refresh_token=config.require("GMAIL_OAUTH_REFRESH_TOKEN"))
    gmail = GmailMailboxTransport(context=context, http=http, access_token=token, message_factory=MailMessage)
    end = datetime.now(timezone.utc)
    metadata = gmail.scan_inbox_metadata_window(end - timedelta(hours=24), end)
    selected: dict[str, MailMessage] = {}
    for message in metadata:
        provider = _provider_hint(message)
        if provider and provider not in selected:
            selected[provider] = message
    if set(selected) != {"Jobright", "Lensa"}:
        raise RuntimeError(f"required provider observations unavailable: {sorted(selected)}")
    hydrated = gmail.hydrate_messages(tuple(selected[name].message_id for name in ("Jobright", "Lensa")))
    observations = {}
    for message in hydrated:
        parsed = parse_message(message)
        for observation in parsed.observations:
            if observation.source_provider in {"Jobright", "Lensa"} and observation.source_provider not in observations:
                observations[observation.source_provider] = observation
    if set(observations) != {"Jobright", "Lensa"}:
        raise RuntimeError(f"selected messages did not yield both providers: {sorted(observations)}")
    result = {provider: _trace_observation(observations[provider], context=context, http=http) for provider in ("Jobright", "Lensa")}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
