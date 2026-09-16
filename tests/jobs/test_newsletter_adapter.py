from __future__ import annotations

from lifeos.jobs.fit_scoring import FitProfile, RoleFamily
from lifeos.jobs.newsletter_adapter import NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.terminal_evidence import FetchResponse
from lifeos.newsletter.models import SourceVacancyObservation

FAKE_PROFILE = FitProfile(
    model_version="test-1",
    role_families=(RoleFamily(patterns=(r"\bsynthetic engineer\b",), base_score=70, label="Synthetic Engineer"),),
    default_role_base=10,
    default_role_label="weak",
)

JOBPOSTING_HTML = """
<html><script type="application/ld+json">
{"@type": "JobPosting", "description": "Synthetic engineer role. widget-alpha.", "datePosted": "2026-01-10"}
</script></html>
"""


class FakeFetcher:
    def __init__(self, responses: dict[str, FetchResponse]):
        self._responses = responses

    def get(self, url: str) -> FetchResponse:
        if url not in self._responses:
            raise RuntimeError(f"no fixture for {url}")
        return self._responses[url]


def _observation(**overrides) -> SourceVacancyObservation:
    base = dict(
        evidence_ref="ev:1",
        source_provider="synthetic-provider",
        source_mailbox="INBOX",
        source_message_id="msg-1",
        source_subject="Synthetic jobs",
        company="Acme Synthetic Co",
        role="Synthetic Engineer",
        location_text="Remote - Synthetic Country",
        compensation_text="$100,000 - $120,000",
        source_apply_url="https://greenhouse.io/acme/jobs/42",
        provider_job_id="prov-1",
        provider_score=88,
    )
    base.update(overrides)
    return SourceVacancyObservation(**base)


def _adapter(fetcher) -> NewsletterJobsAdapter:
    config = NewsletterAdapterConfig(fetcher=fetcher, fit_profile=FAKE_PROFILE, market="Synthetic-US", source_lane="Newsletter")
    return NewsletterJobsAdapter(config)


def test_resolved_observation_produces_populated_candidate():
    fetcher = FakeFetcher({"https://greenhouse.io/acme/jobs/42": FetchResponse(final_url="https://greenhouse.io/acme/jobs/42", body=JOBPOSTING_HTML)})
    candidate = _adapter(fetcher).to_jobs_candidate(_observation())
    assert candidate.unresolved_reason is None
    assert candidate.job.apply_url == "https://greenhouse.io/acme/jobs/42"
    assert candidate.job.posting_date is not None and candidate.job.posting_date.isoformat() == "2026-01-10"
    assert candidate.job.description_text == "Synthetic engineer role. widget-alpha."
    assert candidate.fit is not None and candidate.fit >= 70
    assert candidate.job.provider_score == 88


def test_missing_source_url_is_unresolved():
    candidate = _adapter(FakeFetcher({})).to_jobs_candidate(_observation(source_apply_url=None))
    assert candidate.unresolved_reason is not None
    assert candidate.fit is None
    assert candidate.job.apply_url is None


def test_failed_resolution_is_unresolved_not_crash():
    fetcher = FakeFetcher({})  # no fixture -> get() raises
    candidate = _adapter(fetcher).to_jobs_candidate(_observation(source_apply_url="https://linkedin.com/jobs/view/1"))
    assert candidate.unresolved_reason is not None
    assert candidate.fit is None


def test_intermediary_only_resolution_is_unresolved():
    fetcher = FakeFetcher({"https://linkedin.com/jobs/view/1": FetchResponse(final_url="https://linkedin.com/jobs/view/1", body="<html>no links here</html>")})
    candidate = _adapter(fetcher).to_jobs_candidate(_observation(source_apply_url="https://linkedin.com/jobs/view/1"))
    assert candidate.unresolved_reason is not None


def test_work_mode_inferred_from_location_text():
    fetcher = FakeFetcher({"https://greenhouse.io/acme/jobs/42": FetchResponse(final_url="https://greenhouse.io/acme/jobs/42", body=JOBPOSTING_HTML)})
    from lifeos.jobs.models import WorkMode

    candidate = _adapter(fetcher).to_jobs_candidate(_observation(location_text="Remote - Synthetic Country"))
    assert candidate.job.work_mode == WorkMode.REMOTE


def test_compensation_minimum_parsed_from_text():
    fetcher = FakeFetcher({"https://greenhouse.io/acme/jobs/42": FetchResponse(final_url="https://greenhouse.io/acme/jobs/42", body=JOBPOSTING_HTML)})
    candidate = _adapter(fetcher).to_jobs_candidate(_observation(compensation_text="$95k - $110k"))
    assert candidate.job.compensation_minimum == 95_000


def test_provider_score_carried_as_evidence_never_used_for_fit():
    fetcher = FakeFetcher({"https://greenhouse.io/acme/jobs/42": FetchResponse(final_url="https://greenhouse.io/acme/jobs/42", body=JOBPOSTING_HTML)})
    high_provider_score = _adapter(fetcher).to_jobs_candidate(_observation(provider_score=100))
    low_provider_score = _adapter(fetcher).to_jobs_candidate(_observation(provider_score=1))
    # Same role/description -> identical computed Fit regardless of provider_score.
    assert high_provider_score.fit == low_provider_score.fit
