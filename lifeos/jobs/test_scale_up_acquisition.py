import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path
from lifeos.core.http import HttpError, HttpErrorKind, HttpResponse

from lifeos.core.runtime import RunContext
from lifeos.jobs.scale_up_acquisition import ScaleUpAcquirer, _recovery, _revolut, _sixflow, _veramed
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
    def test_recovery_complete_zero_is_coverage_not_vacancy_zero(self):
        # Production-Critical-Test: prevents empty complete recovery from being falsely degraded or treated as vacancy zero.
        source = {"company": "Synthetic Recovery Co", "source_type": "provider_html"}
        complete = {"channels": ["google_web", "linkedin_jobs"], "state": "COMPLETE", "candidates": []}
        self.assertEqual(_recovery({**source, "recovery_evidence": complete}, datetime(2026, 1, 1, tzinfo=timezone.utc)), [])
        verified = {**complete, "candidates": [{"title": "Product Manager", "url": "https://example.test/job/1", "employer_verified": True, "vacancy_verified": True}]}
        self.assertEqual(len(_recovery({**source, "recovery_evidence": verified}, datetime(2026, 1, 1, tzinfo=timezone.utc))), 1)
        for label, evidence in (
            ("missing channel", {**complete, "channels": ["google_web"]}),
            ("incomplete state", {**complete, "state": "DEGRADED"}),
            ("invalid candidate", {**complete, "candidates": [{"title": "Product Manager", "url": "https://example.test/job/1", "employer_verified": True, "vacancy_verified": False}]}),
        ):
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    _recovery({**source, "recovery_evidence": evidence}, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertNotIn("closed", source)

    def test_shared_ats_api_contract_and_observations(self):
        self.assertEqual(len(REGISTRY["sources"]), 48)
        self.assertEqual(len({s["company"] for s in REGISTRY["sources"]}), 48)
        self.assertEqual(len({s["source_type"] for s in REGISTRY["sources"]}), 22)
        acquired_at = datetime(2026,1,1,tzinfo=timezone.utc)
        result = ScaleUpAcquirer(context=RunContext.start(now=acquired_at), http=FakeHttp()).acquire(REGISTRY, now=acquired_at)
        self.assertFalse(result.complete)
        self.assertEqual(len(result.sources), 48)
        self.assertEqual(len({s.company for s in result.sources}), 48)
        self.assertTrue(all(s.state == "COMPLETE" for s in result.sources[:36]))
        self.assertTrue(all(s.state == "DEGRADED" for s in result.sources[36:]))
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
        with self.subTest("Teamtailor talent-community page shape"):
            source = next(item for item in REGISTRY["sources"] if item["company"] == "Breathe Battery Technologies Limited")
            class BreatheHttp(FakeHttp):
                def request(self, context, method, url, **kwargs):
                    return HttpResponse(200, {}, b'<a href="/jobs/4796211-breathe-battery-technologies-talent-community">Breathe Battery Technologies Talent Community</a>')
            result = ScaleUpAcquirer(context=RunContext.start(now=acquired_at), http=BreatheHttp()).acquire({"sources": [source]})
            self.assertEqual(result.sources[0].state, "COMPLETE")
            self.assertEqual(result.observations[0].role, "Breathe Battery Technologies Talent Community")
        with self.subTest("Revolut embedded inventory total"):
            source = next(item for item in REGISTRY["sources"] if item["company"] == "Revolut Ltd")
            payload = {"props": {"pageProps": {"positions": [{"id": "rv-1", "text": "Revolut Product Manager", "locations": [{"name": "London"}]}], "totalPositions": 1}}}
            text = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
            rows = _revolut(text, source, acquired_at)
            self.assertEqual(len(rows), 1)
            with self.assertRaises(ValueError):
                _revolut(text.replace(', "totalPositions": 1', ''), source, acquired_at)
        with self.subTest("Revolut current Flight payload"):
            source = next(item for item in REGISTRY["sources"] if item["company"] == "Revolut Ltd")
            payload = {"positions": [{"id": "rv-1", "text": "Revolut Product Manager", "locations": [{"name": "London"}]}], "totalPositions": 1}
            flight = json.dumps([1, json.dumps(payload)])
            rows = _revolut(f'<script>self.__next_f.push({flight})</script>', source, acquired_at)
            self.assertEqual(rows[0].provider_job_id, "rv-1")
            missing_total = json.dumps([1, json.dumps({"positions": payload["positions"]})])
            with self.assertRaises(ValueError):
                _revolut(f'<script>self.__next_f.push({missing_total})</script>', source, acquired_at)
        with self.subTest("Veramed pairs vacancy cards"):
            source = next(item for item in REGISTRY["sources"] if item["company"] == "Veramed Limited")
            text = '<section class="panel" data-role="ops"><h2>Role One</h2><a href="/job-detail/?gh_jid=1">VIEW JOB</a></section><section class="panel" data-role="ops"><h2>Role Two</h2><a href="/job-detail/?gh_jid=2">VIEW JOB</a></section>'
            rows = _veramed(text, source, acquired_at, lambda _url: "")
            self.assertEqual([(row.role, row.provider_job_id) for row in rows], [("Role One", "1"), ("Role Two", "2")])
            with self.assertRaises(ValueError):
                _veramed('<section class="panel" data-role="ops"><h2>Role One</h2><a href="/about">ABOUT</a></section>', source, acquired_at, lambda _url: "")
        with self.subTest("Six and Flow deterministic zero is COMPLETE with no observations"):
            source = next(item for item in REGISTRY["sources"] if item["company"] == "Six & Flow Ltd")
            self.assertEqual(_sixflow("We don't have any live vacancies right now", source, acquired_at), [])
            with self.assertRaises(ValueError):
                _sixflow("", source, acquired_at)

    def test_shared_ats_api_failure_is_not_complete(self):
        broken = REGISTRY["sources"][0]["canonical_endpoint"]
        result = ScaleUpAcquirer(context=RunContext.start(), http=FakeHttp(broken)).acquire(REGISTRY)
        self.assertFalse(result.complete)
        self.assertEqual(len(result.sources), 48)
        self.assertEqual(result.sources[0].state, "BLOCKED")
        with self.subTest("truncated registry"):
            result = ScaleUpAcquirer(context=RunContext.start(), http=FakeHttp()).acquire({"sources": REGISTRY["sources"][:-1]})
            self.assertFalse(result.complete)
        with self.subTest("403 on source not in fallback allowlist is BLOCKED not DEGRADED"):
            lever_endpoint = next(s["canonical_endpoint"] for s in REGISTRY["sources"] if s["source_type"] == "lever_public")
            class Http403(FakeHttp):
                def request_json(self, context, method, url, **kwargs):
                    if lever_endpoint in url:
                        raise HttpError(HttpErrorKind.HTTP_STATUS, status_code=403)
                    return super().request_json(context, method, url, **kwargs)
            result = ScaleUpAcquirer(context=RunContext.start(), http=Http403()).acquire(REGISTRY)
            lever_health = next(s for s in result.sources if s.company == "Vivacity Labs Limited")
            self.assertEqual(lever_health.state, "BLOCKED")
            self.assertFalse(result.complete)

    def test_scale_up_production_main_composes_complete_runtime(self):
        import scripts.run_scale_up_production as production
        from types import SimpleNamespace
        diagnostic = production._failure_summary(
            SimpleNamespace(
                sources=(SimpleNamespace(company="Broken Co", state="DEGRADED", candidate_count=0, detail="synthetic"),
                         SimpleNamespace(company="Good Co", state="COMPLETE", candidate_count=1, detail=None)),
                observations=(),
                complete=False,
            ),
            [SimpleNamespace(disposition=production.Disposition.REVIEW_DEGRADED, detail="fit unresolved"),
             SimpleNamespace(disposition=production.Disposition.REVIEW_DEGRADED, detail="fit unresolved")],
        )
        self.assertEqual(diagnostic["non_complete_sources"], [{"company":"Broken Co", "state":"DEGRADED", "candidate_count":0, "detail":"synthetic", "diagnostic":None}])
        self.assertEqual(diagnostic["degraded_ingest_reasons"], [{"reason":"fit unresolved", "count":2}])
        self.assertEqual(sum(item["count"] for item in diagnostic["degraded_ingest_reasons"]), diagnostic["degraded_ingest"])
        self.assertNotIn("private", json.dumps(diagnostic).lower())
        companies=[source["company"] for source in REGISTRY["sources"] if source["source_type"] in {"provider_html","generic_html"}]
        now=datetime.now(timezone.utc).isoformat()
        dimensions={key:["synthetic"] for key in ("role_seniority","functional","technical_platform","delivery_complexity","competitive_advantage")}
        classes={key:["synthetic"] for key in ("DIRECT","ADJACENT","METHOD_EQUIVALENT","UNSUPPORTED")}
        policy={"lane":{"name":"Scale-Up","market":"UK","fit_floor":72,"target_review_floor":None,"work_mode_policy":"any","compensation_floor":None,"freshness_gate":False,"freshness_max_days":None,"is_target_bucket":True},"lane_priority":{"Scale-Up":0},"fit_profile_v3":{"model_version":"V3","title_patterns":{"DIRECT":["manager"],"ADJACENT":["lead"],"METHOD_EQUIVALENT":["delivery"],"UNSUPPORTED":["engineer"]},"direct_specialization_patterns":["automation"],"dimension_patterns":dimensions,"evidence_patterns":classes,"material_patterns":["delivery"],"ignore_patterns":[],"hard_family_patterns":["engineering"]},"market":"UK","source_lane":"Scale-Up"}
        evidence={"scheduled_slot":"slot","trigger_nonce":"nonce","handoffs":[{"company":company,"channels":["google_web","linkedin_jobs"],"searched_at":now,"state":"COMPLETE","candidates":[],"authoritative_zero":True} for company in companies]}
        base_policy=json.loads(json.dumps(policy)); base_evidence=json.loads(json.dumps(evidence))
        class RuntimeHttp:
            def request_json(self,*args,**kwargs): return {"results":[{"id":"lane-config","title":[{"plain_text":"Job Lane Configuration"}]}]}
            def request(self,context,method,url,**kwargs):
                payload=policy if "policy" in url else evidence
                return HttpResponse(200,{},json.dumps(payload).encode())
        class Notion:
            def query_data_source(self,*args,**kwargs): return [{"properties":{"Lane":{"title":[{"plain_text":"Scale-up"}]},"Private Policy File":{"files":[{"type":"external","external":{"url":"policy"}}]},"Current Recovery Evidence":{"files":[{"type":"external","external":{"url":"evidence"}}]}}}]
        config=SimpleNamespace(require=lambda name: "value")
        acquired=SimpleNamespace(complete=True,sources=tuple(SimpleNamespace(company=f"Company {i}",state="COMPLETE",candidate_count=0,detail=None) for i in range(48)),observations=())
        result=SimpleNamespace(disposition=production.Disposition.CREATED)
        def run(policy_value=base_policy,evidence_value=base_evidence,complete=True,disposition=production.Disposition.CREATED):
            policy_payload=json.loads(json.dumps(policy_value)); evidence_payload=json.loads(json.dumps(evidence_value)); policy.clear(); policy.update(policy_payload); evidence.clear(); evidence.update(evidence_payload)
            with patch.object(production.RuntimeConfig,"load",return_value=config),patch.object(production.RunContext,"start",return_value=object()),patch.object(production,"HttpClient",RuntimeHttp),patch.object(production,"NotionTransport",return_value=Notion()),patch.object(production,"execute_scale_up",return_value=(SimpleNamespace(complete=complete,sources=tuple(SimpleNamespace(company=f"Company {i}",state="COMPLETE",candidate_count=0,detail=None) for i in range(48)),observations=()),[SimpleNamespace(disposition=disposition,detail=None)])),patch.dict(production.os.environ,{"SCHEDULED_SLOT":"slot","TRIGGER_NONCE":"nonce"},clear=False),patch("sys.argv",["run_scale_up_production.py"]):
                return production.main()
        self.assertEqual(run(),0)
        with self.subTest("loaded zero evidence reaches acquisition"):
            self.assertTrue(all(item["authoritative_zero"] for item in evidence["handoffs"]))
        for label,mutator in (("empty dimension",lambda p:p["fit_profile_v3"]["dimension_patterns"].update({"functional":[]})),("empty evidence",lambda p:p["fit_profile_v3"]["evidence_patterns"].update({"DIRECT":[]})),("missing employer",lambda p:p.__setitem__("handoffs",[]))):
            with self.subTest(label):
                changed=json.loads(json.dumps(base_policy if label.startswith("empty") else base_evidence)); mutator(changed)
                with self.assertRaises(Exception): run(policy_value=changed if label.startswith("empty") else base_policy,evidence_value=changed if label=="missing employer" else base_evidence)
        with self.subTest("incomplete acquisition"): self.assertEqual(run(complete=False),1)
        with self.subTest("degraded ingest"): self.assertEqual(run(disposition=production.Disposition.REVIEW_DEGRADED),1)
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
        # Phase B proof: Scale-Up two-pass persist-before-enrich
        from lifeos.jobs.scale_up_runtime import execute_scale_up as _b_run
        from lifeos.jobs.newsletter_contract import Disposition as _Dsp
        from lifeos.jobs.repository import InMemoryCareerRepository as _PhBRepo, ReadBackMismatch as _RBM
        from lifeos.jobs.qualification import LaneConfig as _LC
        from lifeos.jobs.fit_scoring import FitProfile as _FP
        from lifeos.newsletter.models import SourceVacancyObservation as _SVO
        from lifeos.core.runtime import RunContext as _RC
        _b_lane = _LC(name="Scale-Up", market="UK", fit_floor=72, target_review_floor=None, work_mode_policy="any", compensation_floor=None, freshness_gate=False, freshness_max_days=None, is_target_bucket=True)
        _b_profile = _FP("V3-su-phaseb", {"DIRECT": ("program", "technical program", "product"), "ADJACENT": ("architect",), "METHOD_EQUIVALENT": ("delivery",), "UNSUPPORTED": ("software engineer",)}, ("automation", "AI"), {"role_seniority": ("years? experience", "senior", "lead"), "functional": ("program", "delivery", "management"), "technical_platform": ("cloud", "platform", "data"), "delivery_complexity": ("complex", "cross-functional"), "competitive_advantage": ("strategy", "automation", "AI")}, {"DIRECT": ("required", "must", "experience"), "ADJACENT": ("preferred",), "METHOD_EQUIVALENT": ("plus",), "UNSUPPORTED": ("software engineer",)}, ("required", "must", "experience", "lead", "manage", "preferred", "plus"), (), ("hands-on coding", "software development"))
        _b_html = "<h1>Program Manager</h1><p>Responsibilities</p><ul><li>Lead program delivery and cloud platform adoption.</li><li>Own strategy across complex initiatives.</li></ul><p>Required qualifications: 5 years experience in program management.</p>"
        _b_url = "https://boards.greenhouse.io/scaleupco/jobs/1"
        _b_obs = _SVO(evidence_ref="scale-up:scaleupco:1", source_provider="scaleupco", source_mailbox="public-web", source_message_id="su-1", source_subject="Program Manager", company="Scale-Up Co Ltd", role="Program Manager", location_text="London, UK", compensation_text=None, source_apply_url=_b_url, provider_job_id="1", provider_score=99, source_received_at=datetime(2026, 1, 15, tzinfo=timezone.utc))
        class _BFakeHttp:
            def request(self, *a, **kw): raise RuntimeError("phase_b network boundary")
        class _BFakeAcquirer:
            def __init__(self, **kw): pass
            def acquire(self, reg, **kw): return SimpleNamespace(observations=(_b_obs,), complete=True, sources=())
        _b_recovery = {"pages": [{"url": _b_url, "final_url": _b_url, "html": _b_html}]}
        with self.subTest("phase_b happy path persist before terminal one row created"):
            _b_repo = _PhBRepo()
            with patch("lifeos.jobs.scale_up_runtime.ScaleUpAcquirer", _BFakeAcquirer), \
                 patch("lifeos.jobs.scale_up_runtime.NotionCareerRepository", lambda **kw: _b_repo):
                _, _b_results = _b_run(context=_RC.start(timeout_seconds=30), http=_BFakeHttp(), notion=SimpleNamespace(), data_source_id="synthetic", lane=_b_lane, lane_priority={"Scale-Up": 0}, fit_profile=_b_profile, market="UK", source_lane="Scale-Up", recovery_evidence=_b_recovery)
            self.assertEqual(len(_b_results), 1)
            self.assertNotEqual(_b_results[0].disposition, _Dsp.REVIEW_DEGRADED)
            self.assertEqual(len(_b_repo._store), 1)
            self.assertIsNotNone(next(iter(_b_repo._store.values())).job.fit)
        with self.subTest("phase_b persistence failure skips terminal evidence"):
            class _BFailRepo(_PhBRepo):
                def upsert(self, record): raise _RBM("synthetic ph-b fail")
                def upsert_many(self, records): raise _RBM("synthetic ph-b batch fail")
            _b_adapt_calls = []
            def _b_tracking_adapt_all(*args, **kwargs):
                _b_adapt_calls.append(1)
                return []
            with patch("lifeos.jobs.scale_up_runtime.ScaleUpAcquirer", _BFakeAcquirer), \
                 patch("lifeos.jobs.scale_up_runtime.NotionCareerRepository", lambda **kw: _BFailRepo()), \
                 patch("lifeos.jobs.scale_up_runtime._adapt_all", _b_tracking_adapt_all):
                _, _bf_results = _b_run(context=_RC.start(timeout_seconds=30), http=_BFakeHttp(), notion=SimpleNamespace(), data_source_id="synthetic", lane=_b_lane, lane_priority={"Scale-Up": 0}, fit_profile=_b_profile, market="UK", source_lane="Scale-Up", recovery_evidence=_b_recovery)
            self.assertEqual(len(_b_adapt_calls), 0, "terminal must not be called on persistence failure")
            self.assertEqual(_bf_results[0].disposition, _Dsp.REVIEW_DEGRADED)
        with self.subTest("phase_b terminal failure canonical identity preserved one row"):
            _b_term_repo = _PhBRepo()
            with patch("lifeos.jobs.scale_up_runtime.ScaleUpAcquirer", _BFakeAcquirer), \
                 patch("lifeos.jobs.scale_up_runtime.NotionCareerRepository", lambda **kw: _b_term_repo):
                _, _bt_results = _b_run(context=_RC.start(timeout_seconds=30), http=_BFakeHttp(), notion=SimpleNamespace(), data_source_id="synthetic", lane=_b_lane, lane_priority={"Scale-Up": 0}, fit_profile=_b_profile, market="UK", source_lane="Scale-Up", recovery_evidence={})
            self.assertEqual(len(_bt_results), 1)
            self.assertEqual(_bt_results[0].disposition, _Dsp.REVIEW_DEGRADED)
            self.assertEqual(len(_b_term_repo._store), 1)
        with self.subTest("phase_b replay idempotency second run same row"):
            _b_replay_repo = _PhBRepo()
            with patch("lifeos.jobs.scale_up_runtime.ScaleUpAcquirer", _BFakeAcquirer), \
                 patch("lifeos.jobs.scale_up_runtime.NotionCareerRepository", lambda **kw: _b_replay_repo):
                _b_run(context=_RC.start(timeout_seconds=30), http=_BFakeHttp(), notion=SimpleNamespace(), data_source_id="synthetic", lane=_b_lane, lane_priority={"Scale-Up": 0}, fit_profile=_b_profile, market="UK", source_lane="Scale-Up", recovery_evidence=_b_recovery)
                _b_run(context=_RC.start(timeout_seconds=30), http=_BFakeHttp(), notion=SimpleNamespace(), data_source_id="synthetic", lane=_b_lane, lane_priority={"Scale-Up": 0}, fit_profile=_b_profile, market="UK", source_lane="Scale-Up", recovery_evidence=_b_recovery)
            self.assertEqual(len(_b_replay_repo._store), 1)
        with self.subTest("phase_b qualify gate fit floor 71 excluded 72 not excluded market excluded"):
            from lifeos.jobs.qualification import qualify as _b_qualify, UNIVERSAL_FIT_FLOOR as _UFF
            from lifeos.jobs.models import Company as _Co, WorkMode as _WM, FreshnessStatus as _FS, NormalizedCandidate as _NC, JobObservation as _JO, AdmissionStatus as _AS
            from datetime import date as _d
            self.assertEqual(_UFF, 72)
            _qjob = _JO(company=_Co("Scale Co"), role="Program Manager", location="London, UK", work_mode=_WM.UNKNOWN, compensation_text=None, compensation_minimum=None, posting_date=None, apply_url="https://example.test/jobs/1", source_lane="Scale-Up")
            self.assertEqual(_b_qualify(_NC(job=_qjob, fit=71, market="UK", freshness_status=_FS.UNRESOLVED, evidence_ref="scale-up:qual:1"), lane=_b_lane, run_date=_d(2026, 1, 15)).admission_status, _AS.EXCLUDED)
            self.assertNotEqual(_b_qualify(_NC(job=_qjob, fit=72, market="UK", freshness_status=_FS.UNRESOLVED, evidence_ref="scale-up:qual:2"), lane=_b_lane, run_date=_d(2026, 1, 15)).admission_status, _AS.EXCLUDED)
            self.assertEqual(_b_qualify(_NC(job=_qjob, fit=80, market="US", freshness_status=_FS.FRESH, evidence_ref="scale-up:qual:3"), lane=_b_lane, run_date=_d(2026, 1, 15)).admission_status, _AS.EXCLUDED)

if __name__ == "__main__": unittest.main()
