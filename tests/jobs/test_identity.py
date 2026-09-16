from __future__ import annotations

import pytest

from lifeos.jobs.identity import canonical_url, stable_job_key
from tests.jobs.fixtures import make_job


def test_company_and_provider_id_is_strongest_identity():
    job = make_job(provider_job_id="prov-123", apply_url="https://boards.example/x?utm_source=t")
    key = stable_job_key(job)
    assert key == "Acme Synthetic Co::prov-123"


def test_canonical_url_strips_tracking_params():
    url = "https://boards.example/jobs/42?utm_source=lensa&ref=abc&keep=yes"
    assert canonical_url(url) == "https://boards.example/jobs/42?keep=yes"


def test_url_is_second_priority_when_no_provider_id():
    job = make_job(provider_job_id=None, apply_url="https://boards.example/jobs/42?utm_source=t")
    key = stable_job_key(job)
    assert key == "url:https://boards.example/jobs/42"


def test_company_role_location_fallback_when_no_id_or_url():
    job = make_job(provider_job_id=None, apply_url=None, company_name="  Acme  Co ", role="Eng", location="NYC")
    key = stable_job_key(job)
    assert key == "acme co|eng|nyc"


def test_missing_all_identity_evidence_raises():
    job = make_job(provider_job_id=None, apply_url=None, company_name="", role="", location=None)
    with pytest.raises(ValueError):
        stable_job_key(job)


def test_same_url_different_tracking_params_converge():
    job_a = make_job(provider_job_id=None, apply_url="https://boards.example/jobs/1?utm_source=a")
    job_b = make_job(provider_job_id=None, apply_url="https://boards.example/jobs/1?utm_source=b&src=c")
    assert stable_job_key(job_a) == stable_job_key(job_b)
