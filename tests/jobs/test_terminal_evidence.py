from __future__ import annotations

from datetime import datetime

from lifeos.jobs.terminal_evidence import (
    FetchResponse,
    acquire_terminal_vacancy_evidence,
    extract_job_posting_jsonld,
    is_provider_intermediary_source,
    is_source_message_url,
    parse_posting_date,
    resolve_final_vacancy_url,
)


class FakeFetcher:
    """Synthetic-only in-memory transport double. No network anywhere in
    this test module -- proves the pure resolver/parser logic in isolation
    from Platform Core's eventual real HTTP client."""

    def __init__(self, responses: dict[str, FetchResponse]):
        self._responses = responses
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResponse:
        self.calls.append(url)
        if url not in self._responses:
            raise RuntimeError(f"no fixture response for {url}")
        return self._responses[url]


REFERENCE_TIME = datetime(2026, 1, 15, 12, 0, 0)


# --- parse_posting_date ------------------------------------------------------


def test_parse_iso_date():
    assert parse_posting_date("Posted 2026-01-10") == "2026-01-10"


def test_parse_us_date():
    assert parse_posting_date("1/10/2026") == "2026-01-10"


def test_parse_relative_days_ago_requires_reference_time():
    assert parse_posting_date("3 days ago", reference_time=REFERENCE_TIME) == "2026-01-12"
    assert parse_posting_date("3 days ago", reference_time=None) is None


def test_parse_today_and_yesterday():
    assert parse_posting_date("Posted today", reference_time=REFERENCE_TIME) == "2026-01-15"
    assert parse_posting_date("Posted yesterday", reference_time=REFERENCE_TIME) == "2026-01-14"


def test_parse_no_evidence_returns_none_never_reference_time():
    assert parse_posting_date("no date information here", reference_time=REFERENCE_TIME) is None
    assert parse_posting_date("", reference_time=REFERENCE_TIME) is None


# --- resolve_final_vacancy_url -----------------------------------------------


def test_direct_employer_url_resolves_with_no_fetch():
    fetcher = FakeFetcher({})
    result = resolve_final_vacancy_url("https://greenhouse.io/employer/jobs/42?utm_source=x", fetcher=fetcher)
    assert result.final_url == "https://greenhouse.io/employer/jobs/42"
    assert fetcher.calls == []


def test_mailbox_url_never_resolves():
    fetcher = FakeFetcher({})
    result = resolve_final_vacancy_url("https://mail.google.com/mail/u/0/#inbox/abc", fetcher=fetcher)
    assert result.final_url is None
    assert fetcher.calls == []


def test_intermediary_resolves_through_downstream_link():
    body = '<a href="https://greenhouse.io/employer/jobs/42">Apply</a>'
    fetcher = FakeFetcher(
        {"https://linkedin.com/jobs/view/1": FetchResponse(final_url="https://linkedin.com/jobs/view/1", body=body)}
    )
    result = resolve_final_vacancy_url("https://linkedin.com/jobs/view/1", fetcher=fetcher)
    assert result.final_url == "https://greenhouse.io/employer/jobs/42"


def test_multi_hop_intermediary_chain_bounded_and_resolves():
    fetcher = FakeFetcher(
        {
            "https://linkedin.com/jobs/view/1": FetchResponse(
                final_url="https://linkedin.com/jobs/view/1",
                body='<a href="https://lensa.com/job/redirect-2">continue</a>',
            ),
            "https://lensa.com/job/redirect-2": FetchResponse(
                final_url="https://lensa.com/job/redirect-2",
                body='<a href="https://greenhouse.io/employer/jobs/42">Apply</a>',
            ),
        }
    )
    result = resolve_final_vacancy_url("https://linkedin.com/jobs/view/1", fetcher=fetcher)
    assert result.final_url == "https://greenhouse.io/employer/jobs/42"
    assert len(fetcher.calls) == 2


def test_loop_detection_fails_closed():
    fetcher = FakeFetcher(
        {
            "https://linkedin.com/jobs/view/1": FetchResponse(
                final_url="https://linkedin.com/jobs/view/1",
                body='<a href="https://lensa.com/job/2">next</a>',
            ),
            "https://lensa.com/job/2": FetchResponse(
                final_url="https://lensa.com/job/2",
                body='<a href="https://linkedin.com/jobs/view/1">back</a>',
            ),
        }
    )
    result = resolve_final_vacancy_url("https://linkedin.com/jobs/view/1", fetcher=fetcher)
    assert result.final_url is None


def test_hop_limit_exhausted_fails_closed():
    # Five distinct intermediary-only pages, each linking only to the next --
    # exceeds MAX_INTERMEDIARY_HOPS (4) with no ATS/job-path destination ever found.
    responses = {}
    for i in range(6):
        current = f"https://linkedin.com/jobs/view/{i}"
        nxt = f"https://linkedin.com/jobs/view/{i + 1}"
        responses[current] = FetchResponse(final_url=current, body=f'<a href="{nxt}">next</a>')
    fetcher = FakeFetcher(responses)
    result = resolve_final_vacancy_url("https://linkedin.com/jobs/view/0", fetcher=fetcher)
    assert result.final_url is None
    assert len(fetcher.calls) <= 4


def test_resolution_never_retries_on_fetch_failure():
    class FailingFetcher:
        def __init__(self):
            self.calls = 0

        def get(self, url: str) -> FetchResponse:
            self.calls += 1
            raise RuntimeError("simulated transport failure")

    fetcher = FailingFetcher()
    result = resolve_final_vacancy_url("https://linkedin.com/jobs/view/1", fetcher=fetcher)
    assert result.final_url is None
    assert fetcher.calls == 1  # no internal retry -- that is transport's job


def test_intermediary_host_classification():
    assert is_provider_intermediary_source("https://www.linkedin.com/jobs/view/1") is True
    assert is_provider_intermediary_source("https://greenhouse.io/employer/jobs/1") is False


def test_source_message_url_detection():
    assert is_source_message_url("https://mail.google.com/mail/u/0/#inbox/x") is True
    assert is_source_message_url("https://outlook.office.com/mail/inbox/id/x") is True
    assert is_source_message_url("https://greenhouse.io/employer/jobs/1") is False


# --- schema.org extraction + full acquisition --------------------------------


JOBPOSTING_HTML = """
<html><script type="application/ld+json">
{"@type": "JobPosting", "description": "<p>Build things.</p>", "datePosted": "2026-01-10"}
</script></html>
"""


def test_extract_job_posting_jsonld():
    posting = extract_job_posting_jsonld(JOBPOSTING_HTML)
    assert posting is not None
    assert posting["datePosted"] == "2026-01-10"


def test_extract_job_posting_jsonld_missing_returns_none():
    assert extract_job_posting_jsonld("<html>no jsonld here</html>") is None


def test_acquire_terminal_vacancy_evidence_full_flow():
    fetcher = FakeFetcher(
        {
            "https://greenhouse.io/employer/jobs/42": FetchResponse(
                final_url="https://greenhouse.io/employer/jobs/42", body=JOBPOSTING_HTML
            )
        }
    )
    evidence = acquire_terminal_vacancy_evidence("https://greenhouse.io/employer/jobs/42", fetcher=fetcher)
    assert evidence is not None
    assert evidence.canonical_url == "https://greenhouse.io/employer/jobs/42"
    assert evidence.description_text == "Build things."
    assert evidence.posting_date_raw == "2026-01-10"
    assert evidence.evidence_source == "schema.org/JobPosting"


def test_acquire_terminal_vacancy_evidence_fails_closed_when_still_on_intermediary_host():
    fetcher = FakeFetcher(
        {"https://linkedin.com/jobs/view/1": FetchResponse(final_url="https://linkedin.com/jobs/view/1", body="<html></html>")}
    )
    evidence = acquire_terminal_vacancy_evidence("https://linkedin.com/jobs/view/1", fetcher=fetcher)
    assert evidence is None


def test_acquire_terminal_vacancy_evidence_fails_closed_without_complete_evidence():
    fetcher = FakeFetcher(
        {
            "https://greenhouse.io/employer/jobs/42": FetchResponse(
                final_url="https://greenhouse.io/employer/jobs/42", body="<html>no jsonld</html>"
            )
        }
    )
    evidence = acquire_terminal_vacancy_evidence("https://greenhouse.io/employer/jobs/42", fetcher=fetcher)
    assert evidence is None
