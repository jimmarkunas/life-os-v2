import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from lifeos.core.http import HttpResponse

from lifeos.core.runtime import RunContext
from lifeos.jobs.scale_up_acquisition import ScaleUpAcquirer
from lifeos.newsletter.models import SourceVacancyObservation

ROOT = Path(__file__).parents[2]
REGISTRY = json.loads((ROOT / "contracts/scale_up_sources.json").read_text())

class FakeHttp:
    def __init__(self, broken=None, intrepid_live=False, revolut_mismatch=False): self.broken = broken; self.intrepid_live = intrepid_live; self.revolut_mismatch = revolut_mismatch
    def request_json(self, context, method, url, **kwargs):
        if self.broken and self.broken in url: raise TimeoutError("synthetic source failure")
        if method == "POST":
            return {"total": 1, "jobPostings": [{"title": "Workday role", "bulletFields": ["wd-1"], "locationsText": "London", "externalPath": "/job/wd-1", "postedOn": "2026-01-01"}]}
        if "greenhouse" in url: return {"jobs": [{"id": "gh-1", "title": "Greenhouse role", "location": {"name": "London"}, "absolute_url": "https://jobs.invalid/gh-1"}]}
        if "ashby" in url: return {"jobs": [{"id": "ash-1", "title": "Ashby role", "location": "London", "jobUrl": "https://jobs.invalid/ash-1", "applyUrl": "https://jobs.invalid/ash-1/apply", "compensation": {"min": 1}}]}
        if "workable" in url: return {"jobs": [{"shortcode": "wk-1", "title": "Workable role", "city": "London", "application_url": "https://jobs.invalid/wk-1/apply", "shortlink": "https://jobs.invalid/wk-1/short", "url": "https://jobs.invalid/wk-1/source"}]}
        if "postings.json" in url: return {"data": [{"id": "pp-1", "title": "Pinpoint role", "location": {"name": "London"}, "path": "/pp-1"}]}
        return [{"id": "lv-1", "text": "Lever role", "categories": {"location": "London"}, "hostedUrl": "https://jobs.invalid/lv-1", "applyUrl": "https://jobs.invalid/lv-1/apply"}]
    def request(self, context, method, url, **kwargs):
        if "beintrepid" in url: body='<a href="/open-positions/live">Live Intrepid role</a>' if self.intrepid_live else "There are no open positions at Intrepid at the moment"
        elif "bluestonex" in url: body='<a href="/careers/role">Role Full-Time More Information</a>'
        elif "join.com" in url: body='<a href="/companies/transreport/job/role">Transreport role</a>'
        elif "stream.co" in url: body='<a href="/en/careers/role">Stream role</a>'
        elif "popsa.com" in url: body='<a href="/careers/role">Popsa role</a>'
        elif "zerogravity" in url or "welcometothejungle" in url: body='<a href="/jobs/role">WTTJ role</a>'
        elif "careers.blis" in url:
            body='''<script type="application/ld+json">{"@graph":[{"@type":["Thing","JobPosting"],"title":"Nested JSONLD role","url":"https://careers.blis.com/jobs/nested","identifier":{"value":"nested-1"}}]}</script><script>{"@type":"JobPosting","title":"Untyped script noise","url":"https://careers.blis.com/jobs/noise"}</script><a href="/jobs/product-manager">Product Manager role</a><a href="/jobs/benefits">Benefits</a><a href="/jobs/culture">Culture</a>'''
        elif "citisense" in url:
            body='<a href="/careers/product-manager-role">Product Manager role</a><a href="/careers/about">About Us</a><a href="/careers/benefits">Benefits</a><a href="https://jobs.lever.co/example/product-manager-role">External Product role</a><a href="https://example.invalid/jobs/product-manager-role">Untrusted Product role</a>'
        elif "rippling" in url:
            body='<a href="/eml-payments-ltd/jobs/product-manager-role">Rippling Product role</a>'
        elif "sixandflow" in url:
            body='<a href="/careers/product-manager">Six & Flow Product Manager</a>'
        elif "futuristictechnologies" in url:
            body='<div class="rjjobportal-careers-wrapper"><div class="rjjobportal-job-item"><span class="rjjobportal-job-title">1. Futuristic Product Manager</span><span class="rjjobportal-meta-label">Job Reference Number</span><span class="rjjobportal-meta-val">FT-1</span><span class="rjjobportal-meta-label">Location</span><span class="rjjobportal-meta-val">London</span><span class="rjjobportal-meta-label">Annual Salary</span><span class="rjjobportal-meta-val">£100k</span></div></div>'
        elif "doubleword.ai/careers" in url:
            body='<script src="/assets/index-test.js"></script>'
        elif "doubleword.ai/assets/index-test.js" in url:
            body='a=[{title:"Doubleword Product Manager",slug:"product-manager",department:"Product",type:"Full-time",seniority:"Senior",location:"London",compensation:"£100k",applyEmail:"jobs@example.com"}];a.map;Open Positions'
        elif "revolut.com" in url:
            visible=2 if self.revolut_mismatch else 1
            payload={"props":{"pageProps":{"positions":[{"id":"rv-1","text":"Revolut Product Manager","locations":[{"name":"London","type":"Hybrid","country":"UK"}]}]}}}
            body=f'We have {visible} open positions<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        elif "tg0.co.uk" in url:
            body='<h2>JOIN US</h2><h3>LONDON HQ</h3><h3>TG0 Product Manager</h3><a href="/apply">Submit your application</a>'
        elif "veramed.com/job-openings/?gh_jid=" in url:
            body='<h1>Veramed Product Manager</h1><h2>Apply for this role</h2>'
        elif "veramed.com/job-openings" in url:
            body='<h2>Job Openings</h2><a href="/job-openings/?gh_jid=vr-1">View role</a>'
        elif "teamtailor" in url or "communityfibre" in url or "sanogenetics" in url or "sharegain" in url: body='<a href="/jobs/role">HTML role</a>'
        else: body='<a href="/careers/role">Static role</a>'
        return HttpResponse(200, {}, body.encode())

class ScaleUpAcquisitionTests(unittest.TestCase):
    def test_shared_ats_api_contract_and_observations(self):
        self.assertEqual(len(REGISTRY["sources"]), 36)
        self.assertEqual(len({s["company"] for s in REGISTRY["sources"]}), 36)
        self.assertEqual(len({s["source_type"] for s in REGISTRY["sources"]}), 20)
        acquired_at = datetime(2026,1,1,tzinfo=timezone.utc)
        result = ScaleUpAcquirer(context=RunContext.start(now=acquired_at), http=FakeHttp()).acquire(REGISTRY, now=acquired_at)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.sources), 36)
        self.assertTrue(all(s.state == "COMPLETE" for s in result.sources))
        self.assertTrue(all(isinstance(o, SourceVacancyObservation) and o.source_mailbox == "public-web" for o in result.observations))
        self.assertTrue(all(o.company and o.role and o.source_apply_url for o in result.observations))
        self.assertTrue(all(o.source_received_at == acquired_at for o in result.observations))
        self.assertTrue(all(o.source_description_text is None for o in result.observations))
        workday = next(o for o in result.observations if o.company == "Garrison Technology Ltd")
        self.assertTrue(workday.source_apply_url.endswith("/en-US/external-careers2/job/wd-1"))
        ashby = next(o for o in result.observations if o.company == "Chattermill Analytics Limited")
        self.assertEqual(ashby.source_apply_url, "https://jobs.invalid/ash-1/apply")
        workable = next(o for o in result.observations if o.company == "A Y & J Solicitors")
        self.assertEqual(workable.source_apply_url, "https://jobs.invalid/wk-1/apply")
        self.assertEqual(next(s for s in result.sources if s.company == "Intrepid Ltd").state, "COMPLETE")
        self.assertFalse(any(o.company == "Intrepid Ltd" for o in result.observations))
        live = ScaleUpAcquirer(context=RunContext.start(now=acquired_at), http=FakeHttp(intrepid_live=True)).acquire(REGISTRY, now=acquired_at)
        live_intrepid = next(o for o in live.observations if o.company == "Intrepid Ltd")
        self.assertEqual(live_intrepid.role, "Live Intrepid role")
        self.assertEqual(live_intrepid.source_apply_url, "https://beintrepid.co.uk/open-positions/live")
        self.assertEqual(next(s for s in live.sources if s.company == "Intrepid Ltd").state, "COMPLETE")
        with self.subTest("nested JSON-LD and Teamtailor filtering"):
            blis = [o for o in result.observations if o.company == "Blis Global Ltd"]
            self.assertTrue(any(o.role == "Nested JSONLD role" and o.source_apply_url == "https://careers.blis.com/jobs/nested" for o in blis))
            self.assertFalse(any(o.role in {"Untyped script noise", "Benefits", "Culture"} for o in blis))
        with self.subTest("generic filtering, vetted ATS, and Rippling path"):
            citisense = [o for o in result.observations if o.company == "Citisense Ltd"]
            self.assertTrue(any(o.source_apply_url == "https://jobs.lever.co/example/product-manager-role" for o in citisense))
            self.assertFalse(any(o.source_apply_url == "https://example.invalid/jobs/product-manager-role" for o in citisense))
            self.assertFalse(any(o.role in {"About Us", "Benefits"} for o in citisense))
            rippling = [o for o in result.observations if o.company == "Prepaid Financial Services Limited"]
            self.assertTrue(any("/eml-payments-ltd/jobs/product-manager-role" in (o.source_apply_url or "") for o in rippling))
        with self.subTest("B2 bespoke source families"):
            expected={
                "Six & Flow Ltd":"Six & Flow Product Manager",
                "Futuristic Technologies Ltd":"Futuristic Product Manager",
                "TYTN Ltd":"Doubleword Product Manager",
                "Revolut Ltd":"Revolut Product Manager",
                "Tangi0 Ltd.":"TG0 Product Manager",
                "Veramed Limited":"Veramed Product Manager",
            }
            for company,title in expected.items():
                observation=next(o for o in result.observations if o.company == company)
                self.assertEqual(observation.role,title)
                self.assertEqual(observation.source_received_at,acquired_at)

    def test_shared_ats_api_failure_is_not_complete(self):
        broken = REGISTRY["sources"][0]["canonical_endpoint"]
        result = ScaleUpAcquirer(context=RunContext.start(), http=FakeHttp(broken)).acquire(REGISTRY)
        self.assertFalse(result.complete)
        self.assertEqual(len(result.sources), 36)
        self.assertEqual(result.sources[0].state, "BLOCKED")
        with self.subTest("truncated registry"):
            result = ScaleUpAcquirer(context=RunContext.start(), http=FakeHttp()).acquire({"sources": REGISTRY["sources"][:-1]})
            self.assertFalse(result.complete)
        unknown = {"sources": [{**REGISTRY["sources"][0], "source_type": "unknown"}]}
        result = ScaleUpAcquirer(context=RunContext.start(), http=FakeHttp()).acquire(unknown)
        self.assertFalse(result.complete)
        self.assertEqual(result.sources[0].state, "BLOCKED")
        with self.subTest("Revolut exhaustive mismatch"):
            result = ScaleUpAcquirer(context=RunContext.start(), http=FakeHttp(revolut_mismatch=True)).acquire(REGISTRY)
            self.assertEqual(next(s for s in result.sources if s.company == "Revolut Ltd").state, "DEGRADED")
            self.assertFalse(result.complete)
        with self.subTest("ambiguous empty HTML"):
            class EmptyHtml(FakeHttp):
                def request(self, context, method, url, **kwargs):
                    return HttpResponse(200, {}, b"") if "citisense" in url else super().request(context, method, url, **kwargs)
            result = ScaleUpAcquirer(context=RunContext.start(), http=EmptyHtml()).acquire(REGISTRY)
            self.assertEqual(next(s for s in result.sources if s.company == "Citisense Ltd").state, "DEGRADED")
            self.assertFalse(result.complete)

if __name__ == "__main__": unittest.main()
