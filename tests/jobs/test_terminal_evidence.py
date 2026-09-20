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
    def __init__(self, responses: dict[str, FetchResponse]):
        self._responses = responses
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResponse:
        self.calls.append(url)
        if url not in self._responses:
            raise RuntimeError(f"no fixture response for {url}")
        return self._responses[url]


REFERENCE_TIME = datetime(2026, 1, 15, 12, 0, 0)


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
    fetcher = FakeFetcher({"https://linkedin.com/jobs/view/1": FetchResponse(final_url="https://linkedin.com/jobs/view/1", body=body)})
    result = resolve_final_vacancy_url("https://linkedin.com/jobs/view/1", fetcher=fetcher)
    assert result.final_url == "https://greenhouse.io/employer/jobs/42"


def test_jobright_lensa_chain_resolves_to_employer_ats():
    fetcher = FakeFetcher({
        "https://jobright.ai/jobs/info/1": FetchResponse(final_url="https://jobright.ai/jobs/info/1", body='<a href="https://lensa.com/job/redirect-2">continue</a>'),
        "https://lensa.com/job/redirect-2": FetchResponse(final_url="https://lensa.com/job/redirect-2", body='<a href="https://greenhouse.io/employer/jobs/42">Apply</a>'),
    })
    result = resolve_final_vacancy_url("https://jobright.ai/jobs/info/1", fetcher=fetcher)
    assert result.final_url == "https://greenhouse.io/employer/jobs/42"
    assert result.chain == (
        "https://jobright.ai/jobs/info/1",
        "https://lensa.com/job/redirect-2",
        "https://greenhouse.io/employer/jobs/42",
    )


def test_linkedin_easy_apply_with_terminal_evidence_can_remain_linkedin():
    body = """
    <html><body>Easy Apply<div id="job-details" class="jobs-description-content__text" data-test-id="job-description"><h2>About the job</h2>Meaningful role details and responsibilities followed by a private suffix that must not be emitted.</div></body><script type="application/ld+json">
    {"@type": "JobPosting", "description": "Build platform programs.", "datePosted": "2026-01-10"}
    </script></html>
    """
    fetcher = FakeFetcher({"https://linkedin.com/jobs/view/1": FetchResponse(final_url="https://linkedin.com/jobs/view/1", body=body)})
    result = resolve_final_vacancy_url("https://linkedin.com/jobs/view/1", fetcher=fetcher)
    assert result.final_url == "https://linkedin.com/jobs/view/1"
    assert result.verified_body == body


def test_linkedin_external_apply_resolves_downstream_not_linkedin():
    body = """
    <html><body>Easy Apply is not available. Apply on company site.</body>
    <a href="https://greenhouse.io/employer/jobs/42">Apply externally</a>
    <script type="application/ld+json">
    {"@type": "JobPosting", "description": "Build platform programs.", "datePosted": "2026-01-10"}
    </script></html>
    """
    fetcher = FakeFetcher({"https://linkedin.com/jobs/view/1": FetchResponse(final_url="https://linkedin.com/jobs/view/1", body=body)})
    result = resolve_final_vacancy_url("https://linkedin.com/jobs/view/1", fetcher=fetcher)
    assert result.final_url == "https://greenhouse.io/employer/jobs/42"


def test_ambiguous_linkedin_returns_no_fabricated_terminal_url():
    body = """
    <html><script type="application/ld+json">
    {"@type": "JobPosting", "description": "Build platform programs.", "datePosted": "2026-01-10"}
    </script></html>
    """
    fetcher = FakeFetcher({"https://linkedin.com/jobs/view/1": FetchResponse(final_url="https://linkedin.com/jobs/view/1", body=body)})
    result = resolve_final_vacancy_url("https://linkedin.com/jobs/view/1", fetcher=fetcher)
    assert result.final_url is None
    assert result.chain == ("https://linkedin.com/jobs/view/1",)


def test_multi_hop_intermediary_chain_bounded_and_resolves():
    fetcher = FakeFetcher({
        "https://linkedin.com/jobs/view/1": FetchResponse(final_url="https://linkedin.com/jobs/view/1", body='<a href="https://lensa.com/job/redirect-2">continue</a>'),
        "https://lensa.com/job/redirect-2": FetchResponse(final_url="https://lensa.com/job/redirect-2", body='<a href="https://greenhouse.io/employer/jobs/42">Apply</a>'),
    })
    result = resolve_final_vacancy_url("https://linkedin.com/jobs/view/1", fetcher=fetcher)
    assert result.final_url == "https://greenhouse.io/employer/jobs/42"
    assert len(fetcher.calls) == 2


def test_unknown_host_job_looking_urls_converge_via_verified_redirect():
    class TrackingRedirectFetcher:
        def __init__(self):
            self.calls: list[str] = []

        def get(self, url: str) -> FetchResponse:
            self.calls.append(url)
            return FetchResponse(final_url="https://greenhouse.io/acme/jobs/99", body=JOBPOSTING_HTML)

    fetcher = TrackingRedirectFetcher()
    first = resolve_final_vacancy_url("https://track.example/apply?id=aaa", fetcher=fetcher)
    second = resolve_final_vacancy_url("https://track.example/apply?id=bbb", fetcher=fetcher)

    assert first.final_url == "https://greenhouse.io/acme/jobs/99"
    assert second.final_url == first.final_url
    assert "https://track.example/apply?id=aaa" in fetcher.calls
    assert "https://track.example/apply?id=bbb" in fetcher.calls


def test_unknown_host_job_looking_url_fails_closed_without_verified_redirect():
    class SameHostFetcher:
        def get(self, url: str) -> FetchResponse:
            return FetchResponse(final_url=url, body="<html></html>")

    result = resolve_final_vacancy_url("https://track.example/apply?id=aaa", fetcher=SameHostFetcher())

    assert result.final_url is None


def test_loop_detection_fails_closed():
    fetcher = FakeFetcher({
        "https://linkedin.com/jobs/view/1": FetchResponse(final_url="https://linkedin.com/jobs/view/1", body='<a href="https://lensa.com/job/2">next</a>'),
        "https://lensa.com/job/2": FetchResponse(final_url="https://lensa.com/job/2", body='<a href="https://linkedin.com/jobs/view/1">back</a>'),
    })
    assert resolve_final_vacancy_url("https://linkedin.com/jobs/view/1", fetcher=fetcher).final_url is None


def test_hop_limit_exhausted_fails_closed():
    responses = {}
    for i in range(6):
        current = f"https://linkedin.com/jobs/view/{i}"
        nxt = f"https://linkedin.com/jobs/view/{i + 1}"
        responses[current] = FetchResponse(final_url=current, body=f'<a href="{nxt}">next</a>')
    fetcher = FakeFetcher(responses)
    assert resolve_final_vacancy_url("https://linkedin.com/jobs/view/0", fetcher=fetcher).final_url is None
    assert len(fetcher.calls) <= 4


def test_resolution_never_retries_on_fetch_failure():
    class FailingFetcher:
        def __init__(self): self.calls = 0
        def get(self, url: str) -> FetchResponse:
            self.calls += 1
            raise RuntimeError("simulated transport failure")
    fetcher = FailingFetcher()
    assert resolve_final_vacancy_url("https://linkedin.com/jobs/view/1", fetcher=fetcher).final_url is None
    assert fetcher.calls == 1


def test_intermediary_host_classification():
    assert is_provider_intermediary_source("https://www.linkedin.com/jobs/view/1") is True
    assert is_provider_intermediary_source("https://greenhouse.io/employer/jobs/1") is False


def test_source_message_url_detection():
    assert is_source_message_url("https://mail.google.com/mail/u/0/#inbox/x") is True
    assert is_source_message_url("https://outlook.office.com/mail/inbox/id/x") is True
    assert is_source_message_url("https://greenhouse.io/employer/jobs/1") is False


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
    fetcher = FakeFetcher({"https://greenhouse.io/employer/jobs/42": FetchResponse(final_url="https://greenhouse.io/employer/jobs/42", body=JOBPOSTING_HTML)})
    evidence = acquire_terminal_vacancy_evidence("https://greenhouse.io/employer/jobs/42", fetcher=fetcher)
    assert evidence is not None
    assert evidence.canonical_url == "https://greenhouse.io/employer/jobs/42"
    assert evidence.description_text == "Build things."
    assert evidence.posting_date_raw == "2026-01-10"
    assert evidence.evidence_source == "schema.org/JobPosting"


def test_employer_owned_job_page_is_valid_terminal_evidence_without_known_ats_host():
    url = "https://careers.example.org/openings/senior-program-manager"
    fetcher = FakeFetcher({url: FetchResponse(final_url=url, body=JOBPOSTING_HTML)})
    evidence = acquire_terminal_vacancy_evidence(url, fetcher=fetcher)
    assert evidence is not None
    assert evidence.canonical_url == url
    assert fetcher.calls == [url]


def test_browser_fallback_gets_one_bounded_second_transport_attempt():
    class FailingFetcher:
        def get(self, url: str) -> FetchResponse:
            raise RuntimeError("blocked")
    url = "https://careers.example.org/jobs/42"
    fallback = FakeFetcher({url: FetchResponse(final_url=url, body=JOBPOSTING_HTML)})
    evidence = acquire_terminal_vacancy_evidence(url, fetcher=FailingFetcher(), fallback_fetcher=fallback)
    assert evidence is not None
    assert evidence.canonical_url == url
    assert fallback.calls == [url]


def test_acquire_terminal_vacancy_evidence_fails_closed_when_still_on_intermediary_host():
    fetcher = FakeFetcher({"https://linkedin.com/jobs/view/1": FetchResponse(final_url="https://linkedin.com/jobs/view/1", body="<html></html>")})
    assert acquire_terminal_vacancy_evidence("https://linkedin.com/jobs/view/1", fetcher=fetcher) is None


def test_acquire_terminal_vacancy_evidence_fails_closed_without_complete_evidence():
    fetcher = FakeFetcher({"https://greenhouse.io/employer/jobs/42": FetchResponse(final_url="https://greenhouse.io/employer/jobs/42", body="<html>no jsonld</html>")})
    assert acquire_terminal_vacancy_evidence("https://greenhouse.io/employer/jobs/42", fetcher=fetcher) is None
