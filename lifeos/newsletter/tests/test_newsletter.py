from __future__ import annotations
import unittest
from datetime import datetime, timedelta, timezone
from lifeos.newsletter import NewsletterExecutionState, NewsletterProcessor, ParseState, RoutedNewsletterMessage, parse_message
from lifeos.newsletter.processor import _parse_message_or_known_empty
from lifeos.jobs.newsletter_contract import Disposition, IngestResult, derive_review_these_jobs, ingest
from types import SimpleNamespace
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.fit_title_semantics import classify_title
from lifeos.jobs.fit_requirement_extraction import extract_requirements
from lifeos.jobs.fit_scoreability import compile_fit
from lifeos.jobs.models import Company, FitAuthority, FitEvidenceKind, FreshnessStatus, JobObservation, NormalizedCandidate, WorkMode
from lifeos.jobs.repository import InMemoryCareerRepository
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.identity import IdentityCollision, derive_identity_evidence, resolve_existing_identity
from lifeos.jobs.newsletter_adapter import NewsletterJobsAdapter, NewsletterAdapterConfig

def msg(message_id, subject, body, *, sender="alerts@jobright.example.invalid", mailbox="gmail", minute=0, headers=None):
    return RoutedNewsletterMessage(mailbox,message_id,datetime(2026,1,15,12,minute,tzinfo=timezone.utc),sender,subject,body,headers or {})

class FakeSource:
    def __init__(self, mailbox, messages): self.mailbox, self.messages = mailbox, messages
    def fetch_unprocessed(self,start,end,boundary_name):
        if boundary_name != "J Newsletters": raise AssertionError(boundary_name)
        return [m for m in self.messages if start <= m.received_at < end]

class NewsletterTests(unittest.TestCase):
    def setUp(self): self.start=datetime(2026,1,15,12,0,tzinfo=timezone.utc); self.end=self.start+timedelta(hours=1)
    def test_jobright_parses_every_candidate_and_preserves_unresolved_card(self):
        body = """[Synthetic Labs\n92%\nSenior Program Manager\nRemote\n$120K-$150K/yr](https://jobright.ai/jobs/info/synthetic-1)\n[Malformed card](https://jobright.ai/jobs/info/synthetic-2)\n[Unsubscribe](https://jobright.ai/unsubscribe)"""
        result = parse_message(msg("synthetic-news-1","Jobright daily jobs",body))
        self.assertEqual(len(result.observations),2); self.assertEqual(result.observations[0].company,"Synthetic Labs")
        self.assertEqual(result.observations[0].provider_score,92); self.assertIn("unresolved-card-shape",result.observations[1].issues)
        self.assertEqual(result.state,ParseState.DEGRADED)
    def test_jobright_company_name_containing_stage_is_not_mistaken_for_metadata(self):
        body = """[Stage 4 Synthetic Solutions\nAdvertising · Growth Stage\n98%\nProject Manager – Technology\nRemote\n$120K-$150K/yr](https://jobright.ai/jobs/info/synthetic-stage-4)\nView more opportunities"""
        result = parse_message(msg("synthetic-stage-company", "Jobright daily jobs", body))
        self.assertEqual(len(result.observations), 1)
        obs = result.observations[0]
        self.assertEqual(obs.company, "Stage 4 Synthetic Solutions")
        self.assertEqual(obs.role, "Project Manager – Technology")
        self.assertEqual(obs.provider_score, 98)
        self.assertEqual(obs.source_apply_url, "https://jobright.ai/jobs/info/synthetic-stage-4")
        self.assertNotIn("unresolved-card-shape", obs.issues)
        self.assertEqual(result.state, ParseState.PASS)

    def test_lensa_source_parses_source_facts_without_resolving_final_url_or_posting_date(self):
        body="[Synthetic Works Senior Project Manager Remote $110K-$130K](https://jobs.lensa.com/synthetic-role)"
        result=parse_message(msg("synthetic-lensa","Lensa job alert",body,sender="alerts@lensa.example.invalid"))
        self.assertEqual(len(result.observations),1); obs=result.observations[0]
        self.assertEqual(obs.source_provider,"Lensa"); self.assertEqual(obs.company,"Synthetic Works")
        self.assertEqual(obs.source_apply_url,"https://jobs.lensa.com/synthetic-role")

    def test_raw_dice_mail_uses_generic_parser_contract(self):
        raw = """From: Dice <alerts@dice.example.invalid>
Subject: Dice job alert: new jobs for you
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<html><body>
  <a href="https://www.dice.example.invalid/job-detail/synthetic-123">
    <div>Acme Synthetic Co</div>
    <div>Technical Program Manager</div>
    <div>Remote - Synthetic Country</div>
    <div>$120K - $145K / yr</div>
  </a>
  <a href="https://www.dice.example.invalid/settings">Manage alerts</a>
  unsubscribe
</body></html>
"""
        result = parse_message(
            RoutedNewsletterMessage(
                mailbox="gmail",
                message_id="synthetic-dice",
                received_at=datetime(2026,1,15,12,0,tzinfo=timezone.utc),
                sender="alerts@dice.example.invalid",
                subject="Dice job alert: new jobs for you",
                body_text="",
                headers={"List-Unsubscribe": "<https://www.dice.example.invalid/unsubscribe>"},
                raw_mime=raw,
            )
        )
        self.assertEqual(result.source_provider, "Dice")
        self.assertEqual(result.state, ParseState.PASS)
        self.assertEqual(len(result.observations), 1)
        obs = result.observations[0]
        self.assertEqual(obs.source_provider, "Dice")
        self.assertEqual(obs.company, "Acme Synthetic Co")
        self.assertEqual(obs.role, "Technical Program Manager")
        self.assertEqual(obs.location_text, "Remote - Synthetic Country")
        self.assertEqual(obs.compensation_text, "$120K - $145K / yr")
        self.assertEqual(obs.source_apply_url, "https://www.dice.example.invalid/job-detail/synthetic-123")
        self.assertEqual(obs.issues, ())

    def test_malformed_dice_mail_degrades_without_fabricated_vacancy(self):
        raw = """From: Dice <alerts@dice.example.invalid>
Subject: Dice job alert
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<html><body>
  <a href="https://www.dice.example.invalid/job-detail/synthetic-malformed">View job</a>
  unsubscribe
</body></html>
"""
        result = parse_message(
            RoutedNewsletterMessage(
                mailbox="gmail",
                message_id="synthetic-dice-bad",
                received_at=datetime(2026,1,15,12,0,tzinfo=timezone.utc),
                sender="alerts@dice.example.invalid",
                subject="Dice job alert",
                body_text="",
                raw_mime=raw,
            )
        )
        self.assertEqual(result.source_provider, "Dice")
        self.assertEqual(result.state, ParseState.DEGRADED)
        self.assertEqual(result.observations, ())
        self.assertEqual([issue.code for issue in result.issues], ["no-vacancy-cards-parsed"])

    def test_linkedin_source_extracts_provider_job_id_without_canonical_identity(self):
        body="[Senior Product Manager\nSynthetic Systems · Remote](https://www.linkedin.com/jobs/view/123456789/)"
        result=parse_message(msg("synthetic-li","LinkedIn jobs for you",body,sender="jobs@linkedin.example.invalid"))
        self.assertEqual(result.observations[0].provider_job_id,"123456789"); self.assertEqual(result.observations[0].source_apply_url,"https://www.linkedin.com/jobs/view/123456789/")

        # Real LinkedIn raw MIME carries a hidden preheader shaped like:
        #   <span data-email-preheader="true">Company Role: description…</span>
        # Description attaches only when the prefix exactly and uniquely matches one card.
        ts = result.observations[0].source_received_at
        C1 = "Product Manager\nSynthetic Systems\nUnited States (Remote)\nView job: https://www.linkedin.com/jobs/view/123456790/"
        C2 = "Project Manager\nAcme Corp\nUnited States (Remote)\nView job: https://www.linkedin.com/jobs/view/123456791/"
        C2b = "Product Manager\nSynthetic Systems\nChicago, IL\nView job: https://www.linkedin.com/jobs/view/123456792/"

        def _li(preheader: str, *cards: str) -> MessageParseResult:
            body = (
                "From: jobs-noreply@linkedin.example.invalid\r\nSubject: LI\r\n"
                "MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=li-boundary\r\n\r\n"
                "--li-boundary\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
                f'<html><body><span data-email-preheader="true">{preheader}</span></body></html>\r\n'
                "--li-boundary\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
                + "\n".join(cards)
                + '\nUnsubscribe\r\n--li-boundary--\r\n'
            )
            return parse_message(RoutedNewsletterMessage("gmail", "li-ph", ts, "jobs-noreply@linkedin.example.invalid", "LI", body_text="", raw_mime=body))

        # (name, preheader, cards, {job_id: expected_description})
        ph_cases = (
            ("preheader-matches-card-1", "Synthetic Systems Product Manager: Drives platform roadmap.", (C1, C2), {"123456790": "Drives platform roadmap.", "123456791": None}),
            ("preheader-matches-card-2", "Acme Corp Project Manager: Leads cross-functional delivery.", (C1, C2), {"123456790": None, "123456791": "Leads cross-functional delivery."}),
            ("unmatched-preheader",      "Globex Senior Director: Oversees all divisions.",             (C1, C2), {"123456790": None, "123456791": None}),
            ("no-preheader",             "",                                                             (C1, C2), {"123456790": None, "123456791": None}),
            ("ambiguous-preheader",      "Synthetic Systems Product Manager: Matches both.",            (C1, C2b), {"123456790": None, "123456792": None}),
        )
        for name, preheader, cards, exp in ph_cases:
            with self.subTest(name=name):
                parsed = _li(preheader, *cards)
                obs_map = {o.provider_job_id: o for o in parsed.observations}
                self.assertTrue(obs_map, f"{name}: no observations parsed")
                for jid, exp_desc in exp.items():
                    if jid in obs_map:
                        self.assertEqual(obs_map[jid].source_description_text, exp_desc, f"{name}: job {jid}")
                self.assertIn("123456790", obs_map, f"{name}: card-1 missing")
                self.assertEqual(obs_map["123456790"].source_apply_url, "https://www.linkedin.com/jobs/view/123456790/", f"{name}: url")
    def test_no_newsletter_source_ports_is_degraded(self):
        result=NewsletterProcessor().process_window([],self.start,self.end)
        self.assertEqual(result.state,NewsletterExecutionState.DEGRADED); self.assertFalse(result.cleanup_safe)
        earliest = datetime(2026, 1, 10, 12, tzinfo=timezone.utc); latest = datetime(2026, 1, 11, 12, tzinfo=timezone.utc)
        observations = [SimpleNamespace(evidence_ref="b", source_provider="LinkedIn Jobs", provider_job_id="4468005853", source_apply_url="https://jobs.invalid/a", source_description_text=None, source_received_at=latest, role="Program Lead", company="Synthetic Co"), SimpleNamespace(evidence_ref="a", source_provider="LinkedIn Jobs", provider_job_id="4468005853", source_apply_url="https://jobs.invalid/a", source_description_text=None, source_received_at=earliest, role="Technical Program Manager", company="Synthetic Co")]
        candidates = [SimpleNamespace(evidence_ref=ref, fit_evidence_kind=SimpleNamespace(value="none"), job=SimpleNamespace(apply_url=None)) for ref in ("a", "b")]
        review = derive_review_these_jobs(observations, candidates, [IngestResult(ref, Disposition.REVIEW_DEGRADED, None, "missing trustworthy JD") for ref in ("a", "b")])
        self.assertEqual(len(review), 1); self.assertEqual(review[0]["First Surfaced"], earliest.isoformat()); self.assertIn("job_title", review[0]["Evidence Available"]); self.assertIn("employer_ats_jd", review[0]["Evidence Missing"]); self.assertIsNone(review[0].get("Fit")); self.assertEqual(review[0]["Retry Status"], "retryable")
        self.assertEqual(derive_review_these_jobs(observations[:1], candidates[:1], [IngestResult("a", Disposition.CREATED, "job-1")]), [])
        profile = FitProfile("synthetic", {"DIRECT": ("program", "product"), "ADJACENT": ("architect",), "METHOD_EQUIVALENT": ("method",), "UNSUPPORTED": ("software engineer",)}, ("automation",), {k: ("program" if k == "functional" else "software" if k == "technical_platform" else k,) for k in ("role_seniority", "functional", "technical_platform", "delivery_complexity", "competitive_advantage")}, {"DIRECT": ("must",), "ADJACENT": ("adjacent",), "METHOD_EQUIVALENT": ("method",), "UNSUPPORTED": ("unsupported",)}, ("must", "experience"), (), (r"hands-on software development",))
        direct = classify_title("Technical Program Manager", profile.title_patterns, profile.direct_specialization_patterns)
        reqs = extract_requirements("Must lead program delivery and collaborate with cloud engineers", profile.dimension_patterns, profile.evidence_patterns, profile.material_patterns, profile.ignore_patterns, profile.hard_family_patterns).requirements
        self.assertEqual(compile_fit(title="Technical Program Manager", title_semantics=direct, requirements=list(reqs), evidence_kind=FitEvidenceKind.EMPLOYER_ATS_JD).authority.value, "authoritative")
        self.assertEqual(compile_fit(title="Technical Program Manager", title_semantics=direct, requirements=list(reqs), evidence_kind=FitEvidenceKind.SOURCE_DESCRIPTION).authority.value, "non_authoritative")
        self.assertIsNone(compile_fit(title="Technical Program Manager", title_semantics=direct, requirements=[], evidence_kind=FitEvidenceKind.NONE).fit_result)
        hard = classify_title("Software Engineer", profile.title_patterns, profile.direct_specialization_patterns)
        hard_reqs = extract_requirements("Must perform hands-on software development and software engineering", profile.dimension_patterns, profile.evidence_patterns, profile.material_patterns, profile.ignore_patterns, profile.hard_family_patterns).requirements
        self.assertEqual(compile_fit(title="Software Engineer", title_semantics=hard, requirements=list(hard_reqs), evidence_kind=FitEvidenceKind.EMPLOYER_ATS_JD, hard_family_mismatch=True).fit_result.final_score, 40)
        job = SimpleNamespace(company=SimpleNamespace(name="Sardine"), provider_job_id="4463920520", canonical_identity=None, apply_url=None, role="Technical Program Manager", location="United States")
        evidence = derive_identity_evidence(job)
        legacy = SimpleNamespace(job=SimpleNamespace(stable_job_key="Sardine::4463920520"))
        self.assertEqual(resolve_existing_identity(evidence, records_by_stable_key={"Sardine::4463920520": legacy}, records_by_apply_url={}), "Sardine::4463920520")
        current = SimpleNamespace(job=SimpleNamespace(stable_job_key="sardine|technical program manager|united states"))
        with self.assertRaises(IdentityCollision):
            resolve_existing_identity(evidence, records_by_stable_key={"Sardine::4463920520": legacy, "sardine|technical program manager|united states": current}, records_by_apply_url={})

    def test_jobs_persist_before_fit_evaluation_and_fail_closed(self):
        import lifeos.jobs.newsletter_contract as contract
        lane = LaneConfig("US Remote", "US", 72, None, "remote_only", None, False, None)
        def candidate(ref, fit):
            return NormalizedCandidate(
                JobObservation(Company("Synthetic Co"), "Program Manager", "US", WorkMode.REMOTE, None, None, None, "https://jobs.example/synthetic", "US Remote", canonical_identity="job:synthetic"),
                fit, "US", FreshnessStatus.FRESH, ref, fit_authority=FitAuthority.AUTHORITATIVE if fit is not None else FitAuthority.NON_AUTHORITATIVE,
            )
        repo = InMemoryCareerRepository()
        original = contract.qualify
        contract.qualify = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fit engine unavailable"))
        try:
            broken = ingest([candidate("broken", None)], lane=lane, lane_priority={"US Remote": 0}, repository=repo, run_date=self.start.date())
        finally:
            contract.qualify = original
        self.assertEqual(broken[0].disposition, Disposition.REVIEW_DEGRADED)
        self.assertEqual(broken[0].detail, "evaluation pending: qualification error: fit engine unavailable")
        self.assertEqual(len(repo.get_many(["job:synthetic"])), 1)
        self.assertEqual(repo.get_many(["job:synthetic"])["job:synthetic"].job.fit, None)

        working = ingest([candidate("working", 80)], lane=lane, lane_priority={"US Remote": 0}, repository=repo, run_date=self.start.date())
        self.assertEqual(working[0].stable_job_key, "job:synthetic")
        self.assertEqual(repo.get_many(["job:synthetic"])["job:synthetic"].job.fit, 80)

        low_repo = InMemoryCareerRepository()
        low = ingest([candidate("low", 40)], lane=lane, lane_priority={"US Remote": 0}, repository=low_repo, run_date=self.start.date())
        self.assertEqual(low[0].disposition, Disposition.EXCLUDED)
        self.assertIsNotNone(low_repo.get_many(["job:synthetic"])["job:synthetic"])
        self.assertEqual(low_repo.get_many(["job:synthetic"])["job:synthetic"].job.eligible_lanes, ())

    def test_malformed_fit_evidence_is_not_a_source_fatality(self):
        import lifeos.jobs.newsletter_adapter as adapter_module
        profile = SimpleNamespace(title_patterns={}, direct_specialization_patterns={}, dimension_patterns={}, evidence_patterns={}, material_patterns=(), ignore_patterns=(), hard_family_patterns=())
        observation = SimpleNamespace(
            company="Safe Identity Co", role="Program Manager", location_text="Remote", compensation_text=None,
            source_apply_url="https://jobs.example/safe", provider_job_id="safe-1", provider_score=None,
            source_description_text="malformed evidence", source_provider="LinkedIn Jobs", source_mailbox="gmail",
            evidence_ref="malformed-fit", issues=(),
        )
        original_compile, original_title, original_extract = adapter_module.compile_fit, adapter_module.classify_title, adapter_module.extract_requirements
        adapter_module.classify_title = lambda *args, **kwargs: SimpleNamespace(evidence_class="DIRECT")
        adapter_module.extract_requirements = lambda *args, **kwargs: SimpleNamespace(requirements=[])
        adapter_module.compile_fit = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("malformed"))
        try:
            candidate = NewsletterJobsAdapter(NewsletterAdapterConfig(fetcher=None, fit_profile=profile, market="US", source_lane="US Remote")).to_jobs_candidate(observation)
        finally:
            adapter_module.compile_fit, adapter_module.classify_title, adapter_module.extract_requirements = original_compile, original_title, original_extract
        self.assertEqual(candidate.unresolved_reason, "malformed")
        self.assertIsNone(candidate.fit)
        self.assertEqual(candidate.fit_evidence_kind, FitEvidenceKind.SOURCE_DESCRIPTION)
        self.assertEqual((candidate.job.company.name, candidate.job.role, candidate.job.location), ("Safe Identity Co", "Program Manager", "Remote"))

    def test_processor_fetch_failure_is_degraded_and_cleanup_never_parser_authorized(self):
        class FailingSource(FakeSource):
            def fetch_unprocessed(self,start,end,boundary_name): raise TimeoutError("mailbox=gmail operation=fetch_unprocessed failed_messages=[msg-stuck:TimeoutError:synthetic]")
        good=FakeSource("outlook",[msg("synthetic-ok","Jobright jobs","[Synthetic Labs\n90%\nProgram Manager\nRemote](https://jobright.ai/jobs/info/synthetic-ok)",mailbox="outlook")])
        result=NewsletterProcessor().process_window([FailingSource("gmail",[]),good],self.start,self.end)
        self.assertEqual(result.state,NewsletterExecutionState.DEGRADED); self.assertEqual(len(result.observations),1); self.assertFalse(result.cleanup_safe)
        self.assertIn("msg-stuck:TimeoutError:synthetic", result.errors[0].detail)
    def test_bridgeview_linked_card_extracts_context_and_excludes_controls(self):
        body = '<div>Synthetic Technical Program Manager</div><div>Denver, CO</div><div>$80 - $90 Hourly</div><a href="https://l1.boostie.jobs.invalid/et/click/job">View This Job</a><a href="https://l1.boostie.jobs.invalid/et/click/unsub">Unsubscribe</a>'
        result = parse_message(msg("bridgeview", "Latest jobs", body, sender="synthetic-alert@match.boostie.jobs.invalid"))
        self.assertEqual(len(result.observations), 1); obs = result.observations[0]
        self.assertEqual(obs.role, "Synthetic Technical Program Manager"); self.assertEqual(obs.location_text, "Denver, CO")
        self.assertIsNone(obs.company); self.assertEqual(result.state, ParseState.PASS)

    def test_bridgeview_multi_card_contract_and_malformed_fail_closed(self):
        body = '<div>Technical Program Manager</div><div>Denver, CO</div><div>$80 - $90 Hourly</div><a href="https://l1.boostie.jobs.invalid/et/click/one">View This Job</a><div>Senior Project Manager</div><div>Gallatin, TN</div><div>$65 - $72 Hourly</div><a href="https://l1.boostie.jobs.invalid/et/click/two">View This Job</a><a href="https://l1.boostie.jobs.invalid/et/click/all">View All Jobs</a><a href="https://l1.boostie.jobs.invalid/et/click/dash">Go To Dashboard</a><a href="https://l1.boostie.jobs.invalid/et/click/prefs">Manage Preferences</a><a href="https://l1.boostie.jobs.invalid/et/click/unsub">Unsubscribe</a>'
        result = parse_message(msg("bridgeview-two", "Latest jobs", body, sender="synthetic-alert@match.boostie.jobs.invalid"))
        self.assertEqual([(o.role, o.location_text, o.compensation_text, o.source_apply_url) for o in result.observations], [("Technical Program Manager", "Denver, CO", "$80 - $90 Hourly", "https://l1.boostie.jobs.invalid/et/click/one"), ("Senior Project Manager", "Gallatin, TN", "$65 - $72 Hourly", "https://l1.boostie.jobs.invalid/et/click/two")])
        malformed = parse_message(msg("bridgeview-bad", "Latest jobs", '<div>Role only</div><a href="https://l1.boostie.jobs.invalid/et/click/bad">View This Job</a>', sender="synthetic-alert@match.boostie.jobs.invalid"))
        self.assertEqual(malformed.state, ParseState.DEGRADED); self.assertEqual(malformed.observations, ())

    def test_known_empty_linkedin_notice_does_not_fabricate_vacancy(self):
        cases = [("networking", "Message people you know at Synthetic Co to learn more", "Now that you've applied to Project Manager at Synthetic Co, message your connections to learn more about the company."), ("guidance", "Synthetic Person, looking for a new job?", "Learn how to find the jobs you want. Search for jobs and update your profile."), ("disabled", "We‘ve turned off your job alert for Synthetic Role in Synthetic City", "We've turned off this job alert since you haven't viewed it in over 90 days.")]
        for name, subject, body in cases:
            with self.subTest(name=name):
                result = _parse_message_or_known_empty(msg("linkedin-empty-" + name, subject, body, sender="Synthetic LinkedIn notification via linkedin.com"))
                self.assertEqual(result.state, ParseState.PASS); self.assertEqual(result.observations, ()); self.assertEqual(result.issues, ())
        unknown = _parse_message_or_known_empty(msg("linkedin-unknown", "A LinkedIn notification", "There are no vacancy cards in this synthetic message.", sender="Synthetic LinkedIn notification via linkedin.com"))
        self.assertEqual(unknown.state, ParseState.DEGRADED); self.assertEqual(unknown.observations, ())
if __name__ == "__main__": unittest.main()
