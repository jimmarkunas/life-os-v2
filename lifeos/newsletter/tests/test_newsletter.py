from __future__ import annotations
import unittest
from datetime import datetime, timedelta, timezone
from lifeos.newsletter import NewsletterExecutionState, NewsletterProcessor, ParseState, RoutedNewsletterMessage, adapt_for_jobs, parse_message
from lifeos.newsletter.processor import _parse_message_or_known_empty

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
    def test_no_newsletter_source_ports_is_degraded(self):
        result=NewsletterProcessor().process_window([],self.start,self.end)
        self.assertEqual(result.state,NewsletterExecutionState.DEGRADED); self.assertFalse(result.cleanup_safe)

    def test_processor_fetch_failure_is_degraded_and_cleanup_never_parser_authorized(self):
        class FailingSource(FakeSource):
            def fetch_unprocessed(self,start,end,boundary_name): raise TimeoutError("mailbox=gmail operation=fetch_unprocessed failed_messages=[msg-stuck:TimeoutError:synthetic]")
        good=FakeSource("outlook",[msg("synthetic-ok","Jobright jobs","[Synthetic Labs\n90%\nProgram Manager\nRemote](https://jobright.ai/jobs/info/synthetic-ok)",mailbox="outlook")])
        result=NewsletterProcessor().process_window([FailingSource("gmail",[]),good],self.start,self.end)
        self.assertEqual(result.state,NewsletterExecutionState.DEGRADED); self.assertEqual(len(result.observations),1); self.assertFalse(result.cleanup_safe)
        self.assertIn("msg-stuck:TimeoutError:synthetic", result.errors[0].detail)
    def test_jobs_adapter_seam_preserves_one_input_per_observation(self):
        parsed=parse_message(msg("synthetic-adapt","Jobright jobs","[Synthetic Labs\n90%\nProgram Manager\nRemote](https://jobright.ai/jobs/info/synthetic-adapt)"))
        class Adapter:
            def to_jobs_candidate(self, observation): return {"evidence_ref":observation.evidence_ref,"role":observation.role}
        adapted=adapt_for_jobs(parsed.observations,Adapter())
        self.assertEqual(len(adapted),1); self.assertEqual(adapted[0]["evidence_ref"],parsed.observations[0].evidence_ref)

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
