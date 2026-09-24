"""Focused production-shaped proof for the six US Remote HTML sources."""
from __future__ import annotations

__test__ = False

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from lifeos.core.http import HttpError, HttpErrorKind
from lifeos.core.runtime import RunContext
from lifeos.jobs.us_remote_acquisition import USRemoteAcquirer
from lifeos.jobs.us_remote_runtime import load_registry
from lifeos.newsletter.models import SourceVacancyObservation

NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)

SOURCES = (
    ("akkodis", "Akkodis", "https://www.akkodis.example.invalid/en/job-search"),
    ("experis", "Experis", "https://www.experis.example.invalid/en/search"),
    ("kforce", "Kforce", "https://myhiring.kforce.example.invalid/careersection/ex/moresearch.ftl"),
    ("linkedin-jobs", "LinkedIn Jobs", "https://www.linkedin.example.invalid/jobs/search/"),
    ("motion-recruitment", "Motion Recruitment", "https://motion.example.invalid/tech-jobs"),
    ("teksystems", "TEKsystems", "https://teksystems.example.invalid/us/en/c/project-manager-jobs"),
)

FIXTURES = {
    "akkodis": '<section class="search-result-card" data-job-title="Technical Program Manager"><a href="/en-us/careers/jobs/technical-program-manager/us_en_6_973631_1624091">View Job</a><span>Remote</span></section><a href="/en-us/careers">Technical Program Manager</a>',
    "experis": '<article class="search-result" data-title="Senior Project Manager"><a href="/en/job/123456/senior-project-manager">View Details</a><span>United States</span></article>',
    "kforce": '<li class="job-card" data-job-id="kf-789"><h2>Program Manager</h2><a href="/careersection/ex/jobdetail.ftl?job=kf-789">Apply</a></li><a href="/careersection/ex/moresearch.ftl">Search jobs</a>',
    "linkedin-jobs": '<div class="base-card" data-job-title="Technical Program Manager"><a href="/jobs/view/987654">View Job</a><span>Remote</span></div><a href="/jobs/search/">Search</a>',
    "motion-recruitment": '<div class="job-card"><h2>IT Project Manager / Duck Creek</h2><a href="/tech-jobs/boston/direct-hire/it-project-manager-duck-creek/888237">View Job</a><span>Open to Remote</span></div><a href="/tech-jobs/project-management">Project Management Jobs</a>',
    "teksystems": '<article data-requisition-id="ts-321"><h3>Technical Project Manager</h3><a href="/us/en/job/JP-006282425/technical-project-manager">View Job</a><span>United States</span></article><a href="/us/en/c/project-manager-jobs">Project Manager category</a>',
}


class _Response:
    def __init__(self, body: str, final_url: str):
        self.body = body.encode("utf-8")
        self.final_url = final_url


class _Http:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    def request(self, context, method, url, **kwargs):
        if url not in self.pages:
            raise RuntimeError("synthetic source unavailable")
        return _Response(self.pages[url], url)


def _registry():
    return {
        "tier1_employers": [],
        "staffing_agencies": [
            {"id": source_id, "company": company, "kind": "html", "url": url, "enabled": True}
            for source_id, company, url in SOURCES
        ],
        "discovery_helpers": [],
    }


class UsRemoteHtmlRecoveryProof(unittest.TestCase):
    def test_all_six_current_html_shapes_reach_source_observations_and_complete(self):
        registry = _registry()
        pages = {url: FIXTURES[source_id] for source_id, _, url in SOURCES}
        result = USRemoteAcquirer(
            context=RunContext.start(timeout_seconds=30), http=_Http(pages), max_workers=6
        ).acquire(registry, full_sweep=True, now=NOW)

        self.assertEqual(len(result.observations), 6)
        self.assertTrue(all(isinstance(item, SourceVacancyObservation) for item in result.observations))
        self.assertTrue(result.complete)
        self.assertEqual({item.source_id for item in result.sources}, {source_id for source_id, _, _ in SOURCES})
        self.assertTrue(all(item.state == "COMPLETE" and item.candidate_count == 1 for item in result.sources))
        for source_id, company, _ in SOURCES:
            with self.subTest(source_id=source_id):
                observation = next(item for item in result.observations if item.source_provider == source_id)
                self.assertEqual(observation.company, company)
                self.assertTrue(observation.role)
                self.assertTrue(observation.source_apply_url)

    def test_card_context_ties_generic_ctas_to_roles_and_rejects_navigation(self):
        source = {"id": "akkodis", "company": "Akkodis"}
        rows = USRemoteAcquirer._html_rows(source, FIXTURES["akkodis"], SOURCES[0][2], NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].role, "Technical Program Manager")
        self.assertNotIn("search", rows[0].source_apply_url.casefold())

        duplicate = '<article class="job-card" data-job-title="Program Manager"><a href="/jobs/123">View Job</a><a href="/jobs/123">Apply</a></article>'
        duplicate_rows = USRemoteAcquirer._html_rows(source, duplicate, SOURCES[0][2], NOW)
        self.assertEqual(len(duplicate_rows), 1)

        motion = {"id": "motion-recruitment", "company": "Motion Recruitment"}
        motion_rows = USRemoteAcquirer._html_rows(motion, FIXTURES["motion-recruitment"], SOURCES[4][2], NOW)
        self.assertEqual(len(motion_rows), 1)
        self.assertEqual(motion_rows[0].role, "IT Project Manager / Duck Creek")
        self.assertTrue(motion_rows[0].source_apply_url.endswith("/888237"))
        self.assertNotIn("project-management", motion_rows[0].source_apply_url)

    def test_unprovable_html_stays_degraded_and_explicit_searched_zero_completes(self):
        source = {"id": "experis", "company": "Experis", "kind": "html", "url": SOURCES[1][2], "enabled": True}
        malformed = {"tier1_employers": [], "staffing_agencies": [source], "discovery_helpers": []}
        bad = USRemoteAcquirer(
            context=RunContext.start(timeout_seconds=30),
            http=_Http({SOURCES[1][2]: '<article class="job-card"><a href="/jobs/1">View Job</a></article>'}),
        ).acquire(malformed, full_sweep=True, now=NOW)
        self.assertFalse(bad.complete)
        self.assertEqual(bad.sources[0].state, "DEGRADED")
        self.assertEqual(bad.observations, ())

        searched_zero = USRemoteAcquirer(
            context=RunContext.start(timeout_seconds=30), http=_Http({}),
        ).acquire(
            malformed,
            browser_evidence={"sources": [{"source_id": "experis", "state": "SEARCHED_NO_TARGET_MATCHES"}]},
            full_sweep=True,
            now=NOW,
        )
        self.assertTrue(searched_zero.complete)
        self.assertEqual(searched_zero.observations, ())
        self.assertEqual(searched_zero.sources[0].state, "COMPLETE")
        self.assertEqual(searched_zero.sources[0].candidate_count, 0)

    def test_failure_details_preserve_safe_reasons_and_fail_closed(self):
        source = {"id": "experis", "company": "Experis", "kind": "html", "url": SOURCES[1][2], "enabled": True}
        registry = {"tier1_employers": [], "staffing_agencies": [source], "discovery_helpers": []}
        cases = ((RuntimeError("no-deterministic-vacancy-links"), "RuntimeError:no-deterministic-vacancy-links"),
                 (HttpError(HttpErrorKind.HTTP_STATUS, status_code=404), "HttpError:http-404"),
                 (ValueError("token=secret&cookie=private"), "ValueError"))
        for error, detail in cases:
            with self.subTest(error=type(error).__name__), patch.object(USRemoteAcquirer, "_enumerate_source", side_effect=error):
                result = USRemoteAcquirer(context=RunContext.start(timeout_seconds=30), http=_Http({})).acquire(
                    registry, full_sweep=True, now=NOW
                )
            self.assertEqual(result.sources[0].state, "DEGRADED")
            self.assertEqual(result.sources[0].detail, detail)
            self.assertEqual(result.observations, ())
            self.assertLessEqual(len(result.sources[0].detail or ""), 105)

    def test_current_source_routes_use_explicit_recovery_without_fabricating_rows(self):
        configured = load_registry()
        ids = {"akkodis", "experis", "kforce", "linkedin-jobs", "motion-recruitment", "postman", "teksystems"}
        registry = {bucket: [source for source in configured[bucket] if source["id"] in ids] for bucket in configured if bucket != "schema_version"}
        evidence = {"sources": [{"source_id": source["id"], "state": "SEARCHED_NO_TARGET_MATCHES"}
                                  for bucket in registry.values() for source in bucket]}
        result = USRemoteAcquirer(context=RunContext.start(timeout_seconds=30), http=_Http({})).acquire(
            registry, browser_evidence=evidence, full_sweep=True, now=NOW
        )
        self.assertTrue(result.complete)
        self.assertEqual({source.source_id for source in result.sources}, ids)
        self.assertTrue(all(source.state == "COMPLETE" and source.candidate_count == 0 for source in result.sources))
        self.assertEqual(result.observations, ())
        postman = next(source for bucket in configured.values() if isinstance(bucket, list) for source in bucket if source["id"] == "postman")
        self.assertEqual(postman["url"], "https://www.postman.com/company/careers/open-positions/")
        self.assertNotIn("greenhouse", postman["url"])


if __name__ == "__main__":
    unittest.main()
