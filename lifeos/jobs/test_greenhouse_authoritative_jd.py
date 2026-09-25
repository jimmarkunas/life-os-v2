"""Production-shaped Greenhouse authoritative-JD proof."""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone
from dataclasses import replace
from pathlib import Path

from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.newsletter_adapter import NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition, ingest
from lifeos.jobs.models import FitAuthority, FitEvidenceKind
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.repository import InMemoryCareerRepository
from lifeos.jobs.us_remote_acquisition import USRemoteAcquirer
from lifeos.core.runtime import RunContext


HERE = Path(__file__).resolve().parents[2] / "tests" / "poc" / "greenhouse_authoritative_jd"
NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
LANE = LaneConfig("US Remote", "US", 72, None, "remote_only", None, False, None)
PROFILE = FitProfile(
    "greenhouse-authoritative-jd-proof",
    {"DIRECT": (r"technical program manager",), "ADJACENT": (r"program manager",), "METHOD_EQUIVALENT": (r"delivery",), "UNSUPPORTED": (r"recruiter",)},
    (r"infrastructure",),
    {"role_seniority": (r"\b\d+\+ years\b",), "functional": (r"program management", r"stakeholder management", r"execution"), "technical_platform": (r"infrastructure", r"distributed systems", r"cloud-native", r"AWS", r"GCP"), "delivery_complexity": (r"large-scale", r"multi-team", r"reliability"), "competitive_advantage": (r"security", r"compliance", r"FedRAMP", r"SOC 2")},
    {"DIRECT": (r"program management", r"infrastructure", r"cloud-native", r"distributed systems", r"security"), "ADJACENT": (r"execution", r"reliability"), "METHOD_EQUIVALENT": (r"coordination",), "UNSUPPORTED": (r"site reliability engineering",)},
    (r"years", r"experience", r"skills", r"expertise"),
    (r"benefits", r"equal opportunity"),
    (r"account executive", r"recruiter"),
)


class _GreenhouseHttp:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def request_json(self, _context, _method, url, **_kwargs):
        self.urls.append(url)
        return self.payload


class _FailingFetcher:
    def __init__(self):
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        raise AssertionError("terminal/browser recovery must not run")


def _payload():
    return json.loads((HERE / "greenhouse_real_job.json").read_text(encoding="utf-8"))


class GreenhouseAuthoritativeJDProof(unittest.TestCase):
    def test_greenhouse_content_reaches_jobs_without_terminal_recovery(self):
        http = _GreenhouseHttp(_payload())
        acquirer = USRemoteAcquirer(context=RunContext.start(timeout_seconds=30), http=http)
        observations = acquirer._greenhouse({"id": "figma", "slug": "figma", "company": "Figma"}, NOW, None)
        self.assertEqual(http.urls, ["https://boards-api.greenhouse.io/v1/boards/figma/jobs?content=true"])
        observation = observations[0]
        self.assertEqual(observation.provider_job_id, "6020719004")
        self.assertEqual(observation.company, "Figma")
        self.assertEqual(observation.role, "Technical Program Manager - Infrastructure")
        self.assertEqual(observation.location_text, "San Francisco, CA • New York, NY • United States")
        self.assertTrue(observation.source_apply_url and observation.source_apply_url.endswith("gh_jid=6020719004"))
        self.assertIn("distributed systems", observation.source_description_text or "")
        self.assertGreater(len(observation.source_description_text or ""), 500)
        self.assertEqual(observation.source_evidence_authority, "authoritative_provider_api")

        fetcher = _FailingFetcher()
        candidate = NewsletterJobsAdapter(
            NewsletterAdapterConfig(fetcher=fetcher, fit_profile=PROFILE, market="US", source_lane="US Remote")
        ).to_jobs_candidate(observation)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(candidate.fit_authority, FitAuthority.AUTHORITATIVE)
        self.assertEqual(candidate.fit_evidence_kind, FitEvidenceKind.EMPLOYER_ATS_JD)
        self.assertTrue(candidate.fit is not None and candidate.fit >= 72)
        self.assertTrue(candidate.job.apply_url and candidate.job.description_text)

        result = ingest([candidate], lane=LANE, lane_priority={"US Remote": 0}, repository=InMemoryCareerRepository(), run_date=date(2026, 9, 25))
        self.assertEqual(result[0].disposition, Disposition.CREATED)

    def test_missing_or_generic_content_does_not_take_authority_fast_path(self):
        payload = _payload()
        payload["jobs"][0]["content"] = ""
        http = _GreenhouseHttp(payload)
        observation = USRemoteAcquirer(context=RunContext.start(timeout_seconds=30), http=http)._greenhouse(
            {"id": "figma", "slug": "figma", "company": "Figma"}, NOW, None
        )[0]
        self.assertIsNone(observation.source_evidence_authority)
        self.assertIsNone(observation.source_description_text)

        fetcher = _FailingFetcher()
        NewsletterJobsAdapter(NewsletterAdapterConfig(fetcher=fetcher, fit_profile=PROFILE, market="US", source_lane="US Remote")).to_jobs_candidate(observation)
        self.assertEqual(len(fetcher.calls), 1)

        observation = replace(observation, source_description_text="Program manager with infrastructure experience.")
        fetcher = _FailingFetcher()
        NewsletterJobsAdapter(NewsletterAdapterConfig(fetcher=fetcher, fit_profile=PROFILE, market="US", source_lane="US Remote")).to_jobs_candidate(observation)
        self.assertEqual(len(fetcher.calls), 1)


if __name__ == "__main__":
    unittest.main()
