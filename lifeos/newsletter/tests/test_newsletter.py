from __future__ import annotations
import unittest
from datetime import datetime, timedelta, timezone
from time import perf_counter
from lifeos.newsletter import NewsletterExecutionState, NewsletterProcessor, ParseState, RoutedNewsletterMessage, adapt_for_jobs, parse_message

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
    def test_jobs_adapter_seam_preserves_one_input_per_observation(self):
        parsed=parse_message(msg("synthetic-adapt","Jobright jobs","[Synthetic Labs\n90%\nProgram Manager\nRemote](https://jobright.ai/jobs/info/synthetic-adapt)"))
        class Adapter:
            def to_jobs_candidate(self, observation): return {"evidence_ref":observation.evidence_ref,"role":observation.role}
        adapted=adapt_for_jobs(parsed.observations,Adapter())
        self.assertEqual(len(adapted),1); self.assertEqual(adapted[0]["evidence_ref"],parsed.observations[0].evidence_ref)
    def test_lensa_source_parses_source_facts_without_resolving_final_url_or_posting_date(self):
        body="[Synthetic Works Senior Project Manager Remote $110K-$130K](https://jobs.lensa.com/synthetic-role)"
        result=parse_message(msg("synthetic-lensa","Lensa job alert",body,sender="alerts@lensa.example.invalid"))
        self.assertEqual(len(result.observations),1); obs=result.observations[0]
        self.assertEqual(obs.source_provider,"Lensa"); self.assertEqual(obs.company,"Synthetic Works")
        self.assertEqual(obs.source_apply_url,"https://jobs.lensa.com/synthetic-role")
    def test_linkedin_source_extracts_provider_job_id_without_canonical_identity(self):
        body="[Senior Product Manager\nSynthetic Systems · Remote](https://www.linkedin.com/jobs/view/123456789/)"
        result=parse_message(msg("synthetic-li","LinkedIn jobs for you",body,sender="jobs@linkedin.example.invalid"))
        self.assertEqual(result.observations[0].provider_job_id,"123456789"); self.assertEqual(result.observations[0].source_apply_url,"https://www.linkedin.com/jobs/view/123456789/")
    def test_processor_fetch_failure_is_degraded_and_cleanup_never_parser_authorized(self):
        class FailingSource(FakeSource):
            def fetch_unprocessed(self,start,end,boundary_name): raise TimeoutError("synthetic")
        good=FakeSource("outlook",[msg("synthetic-ok","Jobright jobs","[Synthetic Labs\n90%\nProgram Manager\nRemote](https://jobright.ai/jobs/info/synthetic-ok)",mailbox="outlook")])
        result=NewsletterProcessor().process_window([FailingSource("gmail",[]),good],self.start,self.end)
        self.assertEqual(result.state,NewsletterExecutionState.DEGRADED); self.assertEqual(len(result.observations),1); self.assertFalse(result.cleanup_safe)
    def test_synthetic_parse_performance_reveals_stage_time(self):
        messages=[msg(f"synthetic-{i}","Jobright jobs",f"[Synthetic Labs\\n90%\\nProgram Manager\\nRemote](https://jobright.ai/jobs/info/synthetic-{i})",minute=i%60) for i in range(500)]
        source=FakeSource("gmail",messages); started=perf_counter(); result=NewsletterProcessor(max_workers=8).process_window([source],self.start,self.end); elapsed=perf_counter()-started
        self.assertEqual(len(result.observations),500); self.assertLess(result.timings.total_seconds,90.0); self.assertLess(elapsed,90.0); self.assertGreaterEqual(result.timings.fetch_seconds,0); self.assertGreaterEqual(result.timings.parse_seconds,0)

if __name__ == "__main__": unittest.main()
