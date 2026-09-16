from __future__ import annotations

import pytest

from lifeos.jobs.identity import canonical_url, provider_alias, stable_job_key
from tests.jobs.fixtures import make_job


def test_canonical_url_is_stronger_than_provider_id():
    """BLOCKER 1 regression: a bare provider_job_id must never dominate a
    proven canonical URL match -- otherwise two providers observing the
    same vacancy under different IDs would fork identity."""
    job = make_job(provider_job_id="prov-123", apply_url="https://boards.example/x?utm_source=t")
    key = stable_job_key(job)
    assert key == "url:https://boards.example/x"


def test_canonical_url_strips_tracking_params():
    url = "https://boards.example/jobs/42?utm_source=lensa&ref=abc&keep=yes"
    assert canonical_url(url) == "https://boards.example/jobs/42?keep=yes"


def test_company_role_location_fallback_when_no_url():
    job = make_job(provider_job_id=None, apply_url=None, company_name="  Acme  Co ", role="Eng", location="NYC")
    key = stable_job_key(job)
    assert key == "acme co|eng|nyc"


def test_provider_id_alone_does_not_derive_identity():
    """A provider_job_id with no URL and no company+role+location must not
    silently produce an identity from the ID alone -- it is not primary
    identity evidence."""
    job = make_job(
        provider_job_id="prov-123", apply_url=None, company_name="", role="", location=None
    )
    with pytest.raises(ValueError):
        stable_job_key(job)


def test_missing_all_identity_evidence_raises():
    job = make_job(provider_job_id=None, apply_url=None, company_name="", role="", location=None)
    with pytest.raises(ValueError):
        stable_job_key(job)


def test_same_url_different_tracking_params_converge():
    job_a = make_job(provider_job_id=None, apply_url="https://boards.example/jobs/1?utm_source=a")
    job_b = make_job(provider_job_id=None, apply_url="https://boards.example/jobs/1?utm_source=b&src=c")
    assert stable_job_key(job_a) == stable_job_key(job_b)


def test_canonical_identity_override_is_authoritative():
    job = make_job(canonical_identity="  explicit-merged-key  ", apply_url="https://boards.example/x")
    assert stable_job_key(job) == "explicit-merged-key"


# --- Cross-provider same-vacancy convergence (BLOCKER 1) --------------------


def test_two_providers_same_apply_url_converge_despite_different_provider_ids():
    linkedin_job = make_job(
        provider_job_id="linkedin-98765",
        apply_url="https://employer.example/careers/senior-eng?utm_source=linkedin",
    )
    lensa_job = make_job(
        provider_job_id="lensa-11223",
        apply_url="https://employer.example/careers/senior-eng?utm_source=lensa&ref=x",
    )
    assert stable_job_key(linkedin_job) == stable_job_key(lensa_job)


def test_two_providers_same_company_role_location_converge_when_no_url():
    provider_a = make_job(
        provider_job_id="provA-1",
        apply_url=None,
        company_name="Acme Synthetic Co",
        role="Synthetic Engineer",
        location="Remote - Synthetic Country",
    )
    provider_b = make_job(
        provider_job_id="provB-999",
        apply_url=None,
        company_name="Acme Synthetic Co",
        role="Synthetic Engineer",
        location="Remote - Synthetic Country",
    )
    assert stable_job_key(provider_a) == stable_job_key(provider_b)


def test_different_provider_ids_for_genuinely_different_vacancies_do_not_converge():
    job_a = make_job(provider_job_id="provA-1", apply_url="https://employer.example/roles/1")
    job_b = make_job(provider_job_id="provB-2", apply_url="https://employer.example/roles/2")
    assert stable_job_key(job_a) != stable_job_key(job_b)


# --- Provider alias preservation ---------------------------------------------


def test_provider_alias_captures_company_and_id():
    job = make_job(provider_job_id="prov-123", company_name="Acme Synthetic Co")
    assert provider_alias(job) == "Acme Synthetic Co::prov-123"


def test_provider_alias_none_when_no_provider_id():
    job = make_job(provider_job_id=None)
    assert provider_alias(job) is None
