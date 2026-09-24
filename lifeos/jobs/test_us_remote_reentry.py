"""Deterministic acceptance proof for US Remote V3 re-entry.

The proof exercises the existing runtime composition with synthetic boundaries:
the source registry/acquirer, shared Jobs ingest, terminal evidence, Fit, and
the in-memory authoritative-read-back repository. It never enables production
scheduling or calls a live source.
"""
from __future__ import annotations

__test__ = False

import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from lifeos.core.runtime import RunContext
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.repository import InMemoryCareerRepository
from lifeos.jobs.us_remote_acquisition import AcquisitionResult, SourceHealth, USRemoteAcquirer
from lifeos.jobs.us_remote_runtime import execute_us_remote, load_registry
from lifeos.newsletter.models import SourceVacancyObservation
from lifeos.newsletter.processor import NewsletterExecutionState

NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
LANE = SimpleNamespace(
    name="US Remote", market="US", fit_floor=72, target_review_floor=None,
    work_mode_policy="remote_only", compensation_floor=None,
    freshness_gate=False, freshness_max_days=None, is_target_bucket=False,
)
PROFILE = FitProfile(
    "V3-reentry-proof",
    {"DIRECT": ("program", "technical program", "product"), "ADJACENT": ("architect",), "METHOD_EQUIVALENT": ("delivery",), "UNSUPPORTED": ("software engineer",)},
    ("automation", "AI"),
    {"role_seniority": ("years? experience", "senior", "lead"), "functional": ("program", "delivery", "management"), "technical_platform": ("cloud", "platform", "data"), "delivery_complexity": ("complex", "cross-functional"), "competitive_advantage": ("strategy", "automation", "AI")},
    {"DIRECT": ("required", "must", "experience"), "ADJACENT": ("preferred",), "METHOD_EQUIVALENT": ("plus",), "UNSUPPORTED": ("software engineer",)},
    ("required", "must", "experience", "lead", "manage", "preferred", "plus"), (),
    ("hands-on coding", "software development"),
)


def _observation(source_id: str = "synthetic-source") -> SourceVacancyObservation:
    return SourceVacancyObservation(
        evidence_ref=f"us-web:{source_id}:vacancy-1", source_provider=source_id,
        source_mailbox="public-web", source_message_id=source_id,
        source_subject="Program Manager", company="Synthetic Co",
        role="Program Manager", location_text="Remote, United States",
        compensation_text=None, source_apply_url="https://boards.greenhouse.io/synthetic/jobs/1",
        provider_job_id="synthetic-1", provider_score=99, source_received_at=NOW,
    )


class _FakeHttp:
    def request(self, *args, **kwargs):
        raise RuntimeError("synthetic network boundary")


class _FakeGmail:
    def __init__(self, **kwargs):
        pass

    def newsletter_retention_candidates(self, boundary):
        return []

    def newsletter_backlog_snapshot(self, boundary, *, now):
        return SimpleNamespace(pending_source_messages=0, oldest_pending_age_seconds=None)


class _FakeMailRouter:
    def __init__(self, **kwargs):
        pass

    def route_window(self, *args, **kwargs):
        return SimpleNamespace(checkpoint_safe=True, scanned_count=0, records=(), errors=())


class _FakeNewsletterProcessor:
    def __init__(self, **kwargs):
        pass

    def process_window(self, *args, **kwargs):
        return SimpleNamespace(
            state=NewsletterExecutionState.PASS,
            messages=(), observations=(), errors=(),
        )


class _FakeAcquirer:
    def __init__(self, **kwargs):
        pass

    def acquire(self, registry, **kwargs):
        return AcquisitionResult(
            observations=(_observation(),),
            sources=(SourceHealth("synthetic-source", "COMPLETE", 1),),
        )


class _CountingAuthoritativeRepository(InMemoryCareerRepository):
    """Production-shaped lookup accounting.

    ``known`` models the repository's execution-local authoritative read-back
    index. Only persistence lookups are counted, so enrichment can prove it
    reuses the first-pass identity rather than querying Notion again.
    """

    def __init__(self):
        super().__init__()
        self.stable_key_queries = 0
        self.apply_url_queries = 0

    def known(self, stable_job_keys):
        return {
            key: self._store[key]
            for key in stable_job_keys
            if key in self._store
        }

    def get_many(self, stable_job_keys):
        self.stable_key_queries += 1
        return super().get_many(stable_job_keys)

    def get_by_apply_urls(self, apply_urls):
        self.apply_url_queries += 1
        return super().get_by_apply_urls(apply_urls)


class _BrokenSourceAcquirer(_FakeAcquirer):
    def acquire(self, registry, **kwargs):
        return AcquisitionResult(
            observations=(),
            sources=(SourceHealth("synthetic-source", "DEGRADED", 0, "synthetic-source-failure"),),
        )


class UsRemoteReentryProof(unittest.TestCase):
    def test_full_sweep_covers_every_enabled_registry_source_and_accounts_health(self):
        registry = load_registry()
        expected = {
            str(source["id"])
            for bucket in ("tier1_employers", "staffing_agencies", "discovery_helpers")
            for source in registry[bucket]
            if source.get("enabled", True)
        }
        seen: list[str] = []

        def enumerate_source(self, source, now, since):
            seen.append(str(source["id"]))
            return []

        with patch.object(USRemoteAcquirer, "_enumerate_source", enumerate_source):
            result = USRemoteAcquirer(context=RunContext.start(timeout_seconds=30), http=SimpleNamespace(), max_workers=4).acquire(
                registry, since=NOW, full_sweep=True, now=NOW
            )
        self.assertEqual(set(seen), expected)
        self.assertEqual({item.source_id for item in result.sources}, expected)
        self.assertTrue(result.complete)
        self.assertTrue(all(item.state == "COMPLETE" for item in result.sources))

    def test_source_health_failure_is_incomplete_and_not_pass(self):
        registry = {"tier1_employers": [{"id": "synthetic-source", "kind": "html", "url": "https://example.invalid"}], "staffing_agencies": [], "discovery_helpers": []}
        def enumerate_source(self, source, now, since):
            raise RuntimeError("synthetic source failure")
        with patch.object(USRemoteAcquirer, "_enumerate_source", enumerate_source):
            result = USRemoteAcquirer(context=RunContext.start(timeout_seconds=30), http=SimpleNamespace()).acquire(
                registry, full_sweep=True, now=NOW
            )
        self.assertFalse(result.complete)
        self.assertEqual(result.sources[0].state, "DEGRADED")

    def test_actual_runtime_composition_persists_before_enrichment_and_fails_closed_on_incomplete_source(self):
        repository = InMemoryCareerRepository()
        browser_evidence = {
            "pages": [{
                "url": "https://boards.greenhouse.io/synthetic/jobs/1",
                "final_url": "https://boards.greenhouse.io/synthetic/jobs/1",
                "html": "<h1>Program Manager</h1><p>Responsibilities</p><ul><li>Lead program delivery and cloud platform adoption.</li><li>Own strategy across complex initiatives.</li></ul><p>Required qualifications: 5 years experience in program management.</p>",
            }]
        }
        common = dict(
            context=RunContext.start(timeout_seconds=30), http=_FakeHttp(), notion=SimpleNamespace(),
            gmail=_FakeGmail(), registry={"tier1_employers": [], "staffing_agencies": [], "discovery_helpers": []},
            browser_evidence=browser_evidence, lane=LANE, lane_priority={"US Remote": 0},
            fit_profile=PROFILE, market="US", newsletter_source_lane="US Remote",
            notion_job_ledger_data_source_id="synthetic", inbox_start=NOW, inbox_mode="historical_recovery",
            start=NOW, end=NOW, web_since=NOW, dry_run=False, full_web_sweep=True,
        )
        with patch("lifeos.jobs.us_remote_runtime.MailRouter", _FakeMailRouter), \
             patch("lifeos.jobs.us_remote_runtime.NewsletterProcessor", _FakeNewsletterProcessor), \
             patch("lifeos.jobs.us_remote_runtime.USRemoteAcquirer", _FakeAcquirer), \
             patch("lifeos.jobs.us_remote_runtime.NotionCareerRepository", lambda **kwargs: repository):
            passed = execute_us_remote(**common)
        self.assertEqual(passed.exit_code, 0)
        self.assertEqual(passed.body["status"], "PASS")
        self.assertEqual(passed.body["web"]["status"], "PASS")
        self.assertEqual(passed.body["jobs"]["fully_accounted"], True)
        self.assertEqual(passed.body["web"]["full_sweep"], True)
        self.assertEqual(len(repository._store), 1)
        record = next(iter(repository._store.values()))
        self.assertEqual(record.job.job.apply_url, "https://boards.greenhouse.io/synthetic/jobs/1")
        self.assertIsNotNone(record.job.fit)
        self.assertEqual(record.job.job.provider_score, 99)

        with patch("lifeos.jobs.us_remote_runtime.MailRouter", _FakeMailRouter), \
             patch("lifeos.jobs.us_remote_runtime.NewsletterProcessor", _FakeNewsletterProcessor), \
             patch("lifeos.jobs.us_remote_runtime.USRemoteAcquirer", _BrokenSourceAcquirer), \
             patch("lifeos.jobs.us_remote_runtime.NotionCareerRepository", lambda **kwargs: repository):
            degraded = execute_us_remote(**{**common, "context": RunContext.start(timeout_seconds=30)})
        self.assertEqual(degraded.exit_code, 1)
        self.assertEqual(degraded.body["status"], "DEGRADED")
        self.assertEqual(degraded.body["web"]["status"], "DEGRADED")
        self.assertEqual(degraded.body["web"]["degraded_sources"][0]["source_id"], "synthetic-source")
        self.assertEqual(len(repository._store), 1)

    def test_execute_us_remote_two_pass_reuses_authoritative_identity_during_enrichment(self):
        """The real runtime's initial and terminal ingest passes share identity state."""
        repository = _CountingAuthoritativeRepository()
        browser_evidence = {
            "pages": [{
                "url": "https://boards.greenhouse.io/synthetic/jobs/1",
                "final_url": "https://boards.greenhouse.io/synthetic/jobs/1",
                "html": "<h1>Program Manager</h1><p>Responsibilities</p><ul><li>Lead program delivery and cloud platform adoption.</li><li>Own strategy across complex initiatives.</li></ul><p>Required qualifications: 5 years experience in program management.</p>",
            }]
        }
        common = dict(
            context=RunContext.start(timeout_seconds=30), http=_FakeHttp(), notion=SimpleNamespace(),
            gmail=_FakeGmail(), registry={"tier1_employers": [], "staffing_agencies": [], "discovery_helpers": []},
            browser_evidence=browser_evidence, lane=LANE, lane_priority={"US Remote": 0},
            fit_profile=PROFILE, market="US", newsletter_source_lane="US Remote",
            notion_job_ledger_data_source_id="synthetic", inbox_start=NOW, inbox_mode="historical_recovery",
            start=NOW, end=NOW, web_since=NOW, dry_run=False, full_web_sweep=True,
        )
        with patch("lifeos.jobs.us_remote_runtime.MailRouter", _FakeMailRouter), \
             patch("lifeos.jobs.us_remote_runtime.NewsletterProcessor", _FakeNewsletterProcessor), \
             patch("lifeos.jobs.us_remote_runtime.USRemoteAcquirer", _FakeAcquirer), \
             patch("lifeos.jobs.us_remote_runtime.NotionCareerRepository", lambda **kwargs: repository):
            result = execute_us_remote(**common)

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.body["status"], "PASS")
        self.assertEqual(len(repository._store), 1)
        self.assertGreater(repository.stable_key_queries, 0, "initial ingest did not perform canonical lookup")
        self.assertGreaterEqual(repository.apply_url_queries, 0)
        # The terminal candidate carries the first pass's authoritative key;
        # execution-local known() satisfies enrichment without either query.
        self.assertEqual(repository.stable_key_queries, 1)
        self.assertEqual(repository.apply_url_queries, 0)

    def test_production_script_is_the_composition_root_and_passes_full_sweep(self):
        from scripts import run_us_remote_production

        repository = InMemoryCareerRepository()
        registry = {"tier1_employers": [], "staffing_agencies": [], "discovery_helpers": []}
        recovery_evidence = {
            "pages": [{
                "url": "https://boards.greenhouse.io/synthetic/jobs/1",
                "final_url": "https://boards.greenhouse.io/synthetic/jobs/1",
                "html": "<h1>Program Manager</h1><p>Responsibilities</p><ul><li>Lead program delivery and cloud platform adoption.</li><li>Own strategy across complex initiatives.</li></ul><p>Required qualifications: 5 years experience in program management.</p>",
            }]
        }
        output = StringIO()
        with patch.object(run_us_remote_production, "_require_env", return_value={
            "GMAIL_OAUTH_CLIENT_ID": "synthetic", "GMAIL_OAUTH_CLIENT_SECRET": "synthetic",
            "GMAIL_OAUTH_REFRESH_TOKEN": "synthetic", "NOTION_API_TOKEN": "synthetic",
            "NOTION_JOB_LEDGER_DATA_SOURCE_ID": "synthetic",
        }), \
             patch.object(run_us_remote_production, "load_registry", return_value=registry), \
             patch.object(run_us_remote_production, "browser_evidence", return_value=recovery_evidence), \
             patch.object(run_us_remote_production, "_load_private_policy_from_notion", return_value=(LANE, {"US Remote": 0}, PROFILE, "US", "US Remote", recovery_evidence)), \
             patch.object(run_us_remote_production, "_exchange_gmail_access_token", return_value="synthetic-token"), \
             patch.object(run_us_remote_production, "GmailMailboxTransport", _FakeGmail), \
             patch.object(run_us_remote_production, "NotionTransport", lambda **kwargs: SimpleNamespace()), \
             patch("lifeos.jobs.us_remote_runtime.MailRouter", _FakeMailRouter), \
             patch("lifeos.jobs.us_remote_runtime.NewsletterProcessor", _FakeNewsletterProcessor), \
             patch("lifeos.jobs.us_remote_runtime.USRemoteAcquirer", _FakeAcquirer), \
             patch("lifeos.jobs.us_remote_runtime.NotionCareerRepository", lambda **kwargs: repository), \
             patch.object(run_us_remote_production, "execute_us_remote", wraps=execute_us_remote) as composed, \
             redirect_stdout(output):
            exit_code = run_us_remote_production.main([
                "--full-web-sweep", "--historical-inbox-recovery-hours", "24",
            ])
        self.assertEqual(exit_code, 0)
        self.assertTrue(composed.call_args.kwargs["full_web_sweep"])
        self.assertEqual(composed.call_args.kwargs["inbox_mode"], "historical_recovery")
        self.assertIn('"status": "PASS"', output.getvalue())
        self.assertEqual(len(repository._store), 1)


if __name__ == "__main__":
    unittest.main()
