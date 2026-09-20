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
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResponse:
        self.calls.append(url)
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
    from lifeos.jobs.models import FitAuthority, FitEvidenceKind

    assert candidate.fit_evidence_kind == FitEvidenceKind.EMPLOYER_ATS_JD
    assert candidate.fit_authority == FitAuthority.AUTHORITATIVE
    assert candidate.job.provider_score == 88


def test_missing_source_url_does_not_block_identifiable_candidate():
    candidate = _adapter(FakeFetcher({})).to_jobs_candidate(
        _observation(source_apply_url=None, source_description_text="Synthetic engineer role.")
    )
    # Enrichment could not even be attempted, but company/role/location are
    # still sufficient for identity -- this is not an accounting failure.
    assert candidate.unresolved_reason is None
    from lifeos.jobs.models import FitAuthority, FitEvidenceKind

    assert candidate.fit is not None
    assert candidate.fit_evidence_kind == FitEvidenceKind.SOURCE_DESCRIPTION
    assert candidate.fit_authority == FitAuthority.NON_AUTHORITATIVE
    assert candidate.job.apply_url is None


def test_newsletter_source_types_are_acquisition_provenance_not_lane_names():
    candidate = _adapter(FakeFetcher({})).to_jobs_candidate(
        _observation(source_apply_url=None, source_provider="LinkedIn Jobs", source_mailbox="gmail-primary")
    )
    assert candidate.source_types == ("LinkedIn Jobs", "Gmail Alert")
    assert "Newsletter" not in candidate.source_types

    outlook = _adapter(FakeFetcher({})).to_jobs_candidate(
        _observation(source_apply_url=None, source_provider="Lensa", source_mailbox="Outlook Jobs")
    )
    assert outlook.source_types == ("Lensa", "Outlook Alert")


def test_failed_resolution_does_not_block_identifiable_candidate():
    fetcher = FakeFetcher({})  # no fixture -> get() raises
    candidate = _adapter(fetcher).to_jobs_candidate(_observation(source_apply_url="https://linkedin.com/jobs/view/1"))
    assert candidate.unresolved_reason is None
    assert candidate.fit is None
    from lifeos.jobs.models import FitAuthority, FitEvidenceKind

    assert candidate.fit_evidence_kind == FitEvidenceKind.NONE
    assert candidate.fit_authority == FitAuthority.NON_AUTHORITATIVE
    assert candidate.job.apply_url is None


def test_intermediary_only_resolution_does_not_block_identifiable_candidate():
    fetcher = FakeFetcher({"https://linkedin.com/jobs/view/1": FetchResponse(final_url="https://linkedin.com/jobs/view/1", body="<html>no links here</html>")})
    candidate = _adapter(fetcher).to_jobs_candidate(_observation(source_apply_url="https://linkedin.com/jobs/view/1"))
    assert candidate.unresolved_reason is None
    assert candidate.fit is None
    assert candidate.job.apply_url is None


def test_missing_identity_evidence_is_still_unresolved_via_enrichment_failure():
    # Enrichment failure alone is never fatal, but if company/role/location
    # are ALSO insufficient for identity, the candidate is still accounted
    # for as REVIEW_DEGRADED -- just downstream, by identity.stable_job_key()
    # raising in newsletter_contract.ingest(), not by this adapter.
    candidate = _adapter(FakeFetcher({})).to_jobs_candidate(
        _observation(source_apply_url=None, company="", role="", location_text=None)
    )
    assert candidate.unresolved_reason is None
    assert candidate.fit is None
    from lifeos.jobs.identity import stable_job_key

    try:
        stable_job_key(candidate.job)
        assert False, "expected identity derivation to fail without company/role/location"
    except ValueError:
        pass


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
    from lifeos.jobs.models import FitEvidenceKind

    assert high_provider_score.fit_evidence_kind == FitEvidenceKind.EMPLOYER_ATS_JD


def test_same_source_url_terminal_resolution_is_reused_with_one_fetch():
    fetcher = FakeFetcher({"https://greenhouse.io/acme/jobs/42": FetchResponse(final_url="https://greenhouse.io/acme/jobs/42", body=JOBPOSTING_HTML)})
    adapter = _adapter(fetcher)

    first = adapter.to_jobs_candidate(_observation(evidence_ref="ev:1"))
    second = adapter.to_jobs_candidate(_observation(evidence_ref="ev:2"))

    assert fetcher.calls == ["https://greenhouse.io/acme/jobs/42"]
    assert first.evidence_ref == "ev:1"
    assert second.evidence_ref == "ev:2"
    assert first.job.apply_url == second.job.apply_url == "https://greenhouse.io/acme/jobs/42"
    assert first.fit == second.fit


def test_ambiguous_intermediary_resolution_still_fails_closed_and_is_reused():
    body = '<html><script type="application/ld+json">{"@type":"JobPosting","description":"Build platform programs for enterprise customers."}</script></html>'
    fetcher = FakeFetcher({"https://linkedin.com/jobs/view/1": FetchResponse(final_url="https://linkedin.com/jobs/view/1", body=body)})
    adapter = _adapter(fetcher)

    first = adapter.to_jobs_candidate(_observation(evidence_ref="ev:1", source_apply_url="https://linkedin.com/jobs/view/1"))
    second = adapter.to_jobs_candidate(_observation(evidence_ref="ev:2", source_apply_url="https://linkedin.com/jobs/view/1"))

    assert fetcher.calls == ["https://linkedin.com/jobs/view/1"]
    assert first.evidence_ref == "ev:1"
    assert second.evidence_ref == "ev:2"
    assert first.unresolved_reason is None
    assert second.unresolved_reason is None
    assert first.job.apply_url is None
    assert second.job.apply_url is None
    from lifeos.jobs.models import FitAuthority, FitEvidenceKind

    assert first.fit is None
    assert first.fit_evidence_kind == FitEvidenceKind.NONE
    assert first.fit_authority == FitAuthority.NON_AUTHORITATIVE
    assert second.fit is None
