import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from lifeos.core.runtime import RunContext
from lifeos.jobs.scale_up_acquisition import ScaleUpAcquirer
from lifeos.newsletter.models import SourceVacancyObservation

ROOT = Path(__file__).parents[2]
REGISTRY = json.loads((ROOT / "contracts/scale_up_sources.json").read_text())

class FakeHttp:
    def __init__(self, broken=None): self.broken = broken
    def request_json(self, context, method, url, **kwargs):
        if self.broken and self.broken in url: raise TimeoutError("synthetic source failure")
        if method == "POST":
            return {"total": 1, "jobPostings": [{"title": "Workday role", "bulletFields": ["wd-1"], "locationsText": "London", "externalPath": "/job/wd-1", "postedOn": "2026-01-01"}]}
        if "greenhouse" in url: return {"jobs": [{"id": "gh-1", "title": "Greenhouse role", "location": {"name": "London"}, "absolute_url": "https://jobs.invalid/gh-1"}]}
        if "ashby" in url: return {"jobs": [{"id": "ash-1", "title": "Ashby role", "location": "London", "jobUrl": "https://jobs.invalid/ash-1", "compensation": {"min": 1}}]}
        if "workable" in url: return {"jobs": [{"shortcode": "wk-1", "title": "Workable role", "city": "London", "application_url": "https://jobs.invalid/wk-1"}]}
        if "postings.json" in url: return {"data": [{"id": "pp-1", "title": "Pinpoint role", "location": {"name": "London"}, "path": "/pp-1"}]}
        return [{"id": "lv-1", "text": "Lever role", "categories": {"location": "London"}, "hostedUrl": "https://jobs.invalid/lv-1", "applyUrl": "https://jobs.invalid/lv-1/apply"}]

class ScaleUpAcquisitionTests(unittest.TestCase):
    def test_shared_ats_api_contract_and_observations(self):
        self.assertEqual(len(REGISTRY["sources"]), 13)
        self.assertEqual({s["source_type"] for s in REGISTRY["sources"]}, {"workable_public", "ashby", "workday_public", "greenhouse", "pinpoint_json", "lever_public"})
        acquired_at = datetime(2026,1,1,tzinfo=timezone.utc)
        result = ScaleUpAcquirer(context=RunContext.start(now=acquired_at), http=FakeHttp()).acquire(REGISTRY, now=acquired_at)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.sources), 13)
        self.assertTrue(all(s.state == "COMPLETE" for s in result.sources))
        self.assertTrue(all(isinstance(o, SourceVacancyObservation) and o.source_mailbox == "public-web" for o in result.observations))
        self.assertTrue(all(o.company and o.role and o.source_apply_url for o in result.observations))
        self.assertTrue(all(o.source_received_at == acquired_at for o in result.observations))
        self.assertTrue(all(o.source_description_text is None for o in result.observations))
        workday = next(o for o in result.observations if o.company == "Garrison Technology Ltd")
        self.assertTrue(workday.source_apply_url.endswith("/en-US/external-careers2/job/wd-1"))

    def test_shared_ats_api_failure_is_not_complete(self):
        broken = REGISTRY["sources"][0]["canonical_endpoint"]
        result = ScaleUpAcquirer(context=RunContext.start(), http=FakeHttp(broken)).acquire(REGISTRY)
        self.assertFalse(result.complete)
        self.assertEqual(len(result.sources), 13)
        self.assertEqual(result.sources[0].state, "BLOCKED")
        with self.subTest("truncated registry"):
            result = ScaleUpAcquirer(context=RunContext.start(), http=FakeHttp()).acquire({"sources": REGISTRY["sources"][:-1]})
            self.assertFalse(result.complete)
        unknown = {"sources": [{**REGISTRY["sources"][0], "source_type": "unknown"}]}
        result = ScaleUpAcquirer(context=RunContext.start(), http=FakeHttp()).acquire(unknown)
        self.assertFalse(result.complete)
        self.assertEqual(result.sources[0].state, "BLOCKED")

if __name__ == "__main__": unittest.main()
