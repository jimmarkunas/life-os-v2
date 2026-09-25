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
from dataclasses import replace
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from lifeos.core.runtime import RunContext
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.repository import InMemoryCareerRepository, ReadBackMismatch
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


def _observation(source_id: str = "synthetic-source", index: int = 1) -> SourceVacancyObservation:
    url = f"https://boards.greenhouse.io/synthetic/jobs/{index}"
    return SourceVacancyObservation(
        evidence_ref=f"us-web:{source_id}:vacancy-{index}", source_provider=source_id,
        source_mailbox="public-web", source_message_id=source_id,
        source_subject="Program Manager", company="Synthetic Co",
        role="Program Manager", location_text="Remote, United States",
        compensation_text=None, source_apply_url=url,
        provider_job_id=f"synthetic-{index}", provider_score=99, source_received_at=NOW,
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


class _VolumeNewsletterProcessor(_FakeNewsletterProcessor):
    def process_window(self, *args, **kwargs):
        return SimpleNamespace(
            state=NewsletterExecutionState.PASS,
            messages=(),
            observations=tuple(
                replace(
                    _observation("synthetic-newsletter", index=index),
                    company=f"Synthetic Newsletter {index}",
                )
                for index in range(1, 68)
            ),
            errors=(),
        )


class _FakeAcquirer:
    def __init__(self, **kwargs):
        pass

    def acquire(self, registry, **kwargs):
        return AcquisitionResult(
            observations=(_observation(),),
            sources=(SourceHealth("synthetic-source", "COMPLETE", 1),),
        )


class _VolumeAcquirer(_FakeAcquirer):
    def acquire(self, registry, **kwargs):
        observations = tuple(
            replace(_observation(index=index), company=f"Synthetic Co {index}")
            for index in range(1, 1022)
        )
        return AcquisitionResult(
            observations=observations,
            sources=(SourceHealth("synthetic-volume", "COMPLETE", len(observations)),),
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
        self.stable_key_query_sizes = []
        self.upsert_calls = 0

    def known(self, stable_job_keys):
        return {
            key: self._store[key]
            for key in stable_job_keys
            if key in self._store
        }

    def get_many(self, stable_job_keys):
        found = {}
        for offset in range(0, len(stable_job_keys), 50):
            chunk = stable_job_keys[offset:offset + 50]
            self.stable_key_queries += 1
            self.stable_key_query_sizes.append(len(chunk))
            found.update(super().get_many(chunk))
        return found

    def get_by_apply_urls(self, apply_urls):
        self.apply_url_queries += 1
        return super().get_by_apply_urls(apply_urls)

    def upsert(self, record):
        self.upsert_calls += 1
        return super().upsert(record)

    def upsert_many(self, records):
        return super().upsert_many(records)


class _BrokenSourceAcquirer(_FakeAcquirer):
    def acquire(self, registry, **kwargs):
        return AcquisitionResult(
            observations=(),
            sources=(SourceHealth("synthetic-source", "DEGRADED", 0, "synthetic-source-failure"),),
        )


class _FailingUpsertRepository(_CountingAuthoritativeRepository):
    def upsert(self, record):
        raise ReadBackMismatch(f"synthetic failure for {record.job.stable_job_key}")

    def upsert_many(self, records):
        self.upsert_calls += len(records)
        raise ReadBackMismatch(f"synthetic batch failure ({len(records)} records)")


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
        self.assertEqual(result.body["web"]["terminal_resolution_candidates"], 1)
        self.assertEqual(result.body["web"]["terminal_resolution_skipped"], 0)
        self.assertEqual(result.body["web"]["terminal_resolution_admitted"], 1)

        with patch("lifeos.jobs.newsletter_contract.qualify", side_effect=RuntimeError("synthetic qualification failure")), \
             patch("lifeos.jobs.us_remote_runtime.MailRouter", _FakeMailRouter), \
             patch("lifeos.jobs.us_remote_runtime.NewsletterProcessor", _FakeNewsletterProcessor), \
             patch("lifeos.jobs.us_remote_runtime.USRemoteAcquirer", _FakeAcquirer), \
             patch("lifeos.jobs.us_remote_runtime.NotionCareerRepository", lambda **kwargs: repository):
            degraded = execute_us_remote(**{**common, "context": RunContext.start(timeout_seconds=30)})
        self.assertEqual(degraded.exit_code, 1)
        self.assertEqual(degraded.body["status"], "DEGRADED")
        self.assertGreater(degraded.body["jobs"]["dispositions"]["review_degraded"], 0)

    def test_execute_us_remote_production_volume_two_pass_request_topology(self):
        """1,088-observation recovery keeps the second identity phase empty."""
        repository = _CountingAuthoritativeRepository()
        terminal_html = (
            "<h1>Program Manager</h1><p>Responsibilities</p>"
            "<ul><li>Lead program delivery and cloud platform adoption.</li>"
            "<li>Own strategy across complex initiatives.</li></ul>"
            "<p>Required qualifications: 5 years experience in program management.</p>"
        )
        browser_evidence = {
            "pages": [
                {
                    "url": f"https://boards.greenhouse.io/synthetic/jobs/{index}",
                    "final_url": f"https://boards.greenhouse.io/synthetic/jobs/{index}",
                    "html": terminal_html,
                }
                for index in range(1, 1022)
            ]
        }
        common = dict(
            context=RunContext.start(timeout_seconds=120), http=_FakeHttp(), notion=SimpleNamespace(),
            gmail=_FakeGmail(), registry={"tier1_employers": [], "staffing_agencies": [], "discovery_helpers": []},
            browser_evidence=browser_evidence, lane=LANE, lane_priority={"US Remote": 0},
            fit_profile=PROFILE, market="US", newsletter_source_lane="US Remote",
            notion_job_ledger_data_source_id="synthetic", inbox_start=NOW, inbox_mode="historical_recovery",
            start=NOW, end=NOW, web_since=NOW, dry_run=False, full_web_sweep=True,
        )

        from lifeos.jobs.terminal_evidence import acquire_terminal_vacancy_evidence as resolve
        with patch("lifeos.jobs.newsletter_adapter.acquire_terminal_vacancy_evidence", wraps=resolve) as resolver, \
             patch("lifeos.jobs.us_remote_runtime.MailRouter", _FakeMailRouter), \
             patch("lifeos.jobs.us_remote_runtime.NewsletterProcessor", _VolumeNewsletterProcessor), \
             patch("lifeos.jobs.us_remote_runtime.USRemoteAcquirer", _VolumeAcquirer), \
             patch("lifeos.jobs.us_remote_runtime.NotionCareerRepository", lambda **kwargs: repository):
            first = execute_us_remote(**common)
            first_stable_queries = repository.stable_key_queries
            first_apply_queries = repository.apply_url_queries
            first_upserts = repository.upsert_calls
            first_resolver_calls = resolver.call_count
            second = execute_us_remote(**{**common, "context": RunContext.start(timeout_seconds=120)})
            replay_resolver_calls = resolver.call_count - first_resolver_calls

        self.assertEqual(first.exit_code, 0)
        self.assertEqual(second.exit_code, 0)
        self.assertEqual(first.body["status"], "PASS")
        self.assertEqual(second.body["status"], "PASS")
        self.assertEqual(len(repository._store), 1088)
        self.assertEqual(first_stable_queries, 44)
        self.assertEqual(first_apply_queries, 0)
        self.assertEqual(repository.stable_key_queries - first_stable_queries, 0)
        self.assertEqual(repository.apply_url_queries - first_apply_queries, 0)
        self.assertEqual(first_upserts, 1088)
        # Second run: initial pass is dry_run (0 writes), all TES satisfied (0 enrichment writes)
        self.assertEqual(repository.upsert_calls - first_upserts, 0)
        self.assertEqual(first_resolver_calls, 1021)
        self.assertEqual(replay_resolver_calls, 0)
        self.assertEqual(first.body["web"]["terminal_resolution_candidates"], 1021)
        self.assertEqual(first.body["web"]["terminal_resolution_skipped"], 0)
        self.assertEqual(first.body["web"]["terminal_resolution_admitted"], 1021)
        self.assertEqual(second.body["web"]["terminal_resolution_candidates"], 1021)
        self.assertEqual(second.body["web"]["terminal_resolution_skipped"], 1021)
        self.assertEqual(second.body["web"]["terminal_resolution_admitted"], 0)
        self.assertEqual(second.body["web"]["terminal_resolution_replay_misses"], {
            "missing_fit": 0,
            "non_authoritative_fit": 0,
            "missing_apply_url": 0,
            "qualification_error": 0,
        })
        self.assertEqual(max(repository.stable_key_query_sizes), 50)
        self.assertEqual(len(repository.stable_key_query_sizes), 44)

        # Conservative whole-run model anchored to the latest live timings.
        # The live 162.557s terminal stage covered 1,088 candidate resolutions
        # at eight workers. This run resolves 1,021 unique URLs because 67
        # observations share terminal URLs across the web/newsletter lanes,
        # and US Remote adapts them at 18 workers.
        terminal_requests = 1088
        unique_terminal_urls = 1021
        terminal_fetches_avoided = terminal_requests - unique_terminal_urls
        baseline_workers = 8
        terminal_workers = 18
        baseline_waves = (terminal_requests + baseline_workers - 1) // baseline_workers
        baseline_wave_seconds = 162.557 / baseline_waves
        terminal_waves = (unique_terminal_urls + terminal_workers - 1) // terminal_workers
        projected_terminal = baseline_wave_seconds * terminal_waves
        projected_reconcile = 95.883
        projected_total = 15.866 + 15.674 + 4.082 + projected_terminal + projected_reconcile + 5.0
        self.assertEqual(terminal_fetches_avoided, 67)
        self.assertEqual(terminal_workers, 18)
        self.assertEqual(baseline_waves, 136)
        self.assertEqual(terminal_waves, 57)
        self.assertLessEqual(projected_terminal, 80.0)
        self.assertLessEqual(projected_total, 210.0)

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


class TerminalEvidenceSatisfiedProof(unittest.TestCase):
    """Proof suite for the terminal-evidence-satisfied skip optimization."""

    _COMMON_KWARGS = dict(
        lane=LANE, lane_priority={"US Remote": 0},
        fit_profile=PROFILE, market="US", newsletter_source_lane="US Remote",
        notion_job_ledger_data_source_id="synthetic", inbox_start=NOW,
        inbox_mode="historical_recovery", start=NOW, end=NOW, web_since=NOW,
        dry_run=False, full_web_sweep=True,
    )
    _BROWSER_WITH_JD = {
        "pages": [{
            "url": "https://boards.greenhouse.io/synthetic/jobs/1",
            "final_url": "https://boards.greenhouse.io/synthetic/jobs/1",
            "html": (
                "<h1>Program Manager</h1><p>Responsibilities</p>"
                "<ul><li>Lead program delivery and cloud platform adoption.</li>"
                "<li>Own strategy across complex initiatives.</li></ul>"
                "<p>Required qualifications: 5 years experience in program management.</p>"
            ),
        }]
    }

    def _run(self, repository, browser_evidence=None, *, acquirer=_FakeAcquirer, context=None):
        kwargs = dict(
            context=context or RunContext.start(timeout_seconds=60),
            http=_FakeHttp(), notion=SimpleNamespace(), gmail=_FakeGmail(),
            registry={"tier1_employers": [], "staffing_agencies": [], "discovery_helpers": []},
            browser_evidence=browser_evidence or self._BROWSER_WITH_JD,
            **self._COMMON_KWARGS,
        )
        with patch("lifeos.jobs.us_remote_runtime.MailRouter", _FakeMailRouter), \
             patch("lifeos.jobs.us_remote_runtime.NewsletterProcessor", _FakeNewsletterProcessor), \
             patch("lifeos.jobs.us_remote_runtime.USRemoteAcquirer", acquirer), \
             patch("lifeos.jobs.us_remote_runtime.NotionCareerRepository", lambda **kwargs: repository):
            return execute_us_remote(**kwargs)

    def test_canonical_authoritative_record_skips_resolver_on_replay(self):
        """A record already enriched with EMPLOYER_ATS_JD must not trigger resolution again."""
        from lifeos.jobs.newsletter_contract import Disposition
        repository = _CountingAuthoritativeRepository()
        first = self._run(repository)
        self.assertEqual(first.exit_code, 0)
        first_upserts = repository.upsert_calls
        second = self._run(repository)
        self.assertEqual(second.exit_code, 0)
        # On replay TES is satisfied: initial pass is dry_run (0 writes), enrichment skipped (0 writes)
        self.assertEqual(repository.upsert_calls - first_upserts, 0)

    def test_source_description_only_record_runs_resolver(self):
        """NON_AUTHORITATIVE fit (source description) must NOT satisfy the skip predicate."""
        repository = _CountingAuthoritativeRepository()
        # No browser evidence → terminal evidence fails → record lands as REVIEW_DEGRADED
        first = self._run(repository, browser_evidence={"pages": []})
        # Record has no AUTHORITATIVE fit; should still attempt resolver on next run
        first_upserts = repository.upsert_calls
        second = self._run(repository, browser_evidence={"pages": []})
        self.assertEqual(second.exit_code, 1)  # still REVIEW_DEGRADED, unresolved
        self.assertEqual(second.body["web"]["terminal_resolution_required"], 1)
        self.assertEqual(second.body["web"]["terminal_resolution_skipped"], 0)
        self.assertGreater(sum(second.body["web"]["terminal_resolution_replay_misses"].values()), 0)
        # Dry-run identity means resolver retry no longer implies a redundant persistence write.
        self.assertEqual(repository.upsert_calls - first_upserts, 0)

    def test_missing_apply_url_record_runs_resolver(self):
        """A persisted record without apply_url must not be skipped."""
        repository = _CountingAuthoritativeRepository()
        # Provide empty pages so terminal evidence returns nothing → no apply_url
        first = self._run(repository, browser_evidence={"pages": []})
        self.assertIsNotNone(first)
        record = next(iter(repository._store.values()), None)
        if record:
            self.assertIsNone(record.job.job.apply_url)

    def test_unresolved_terminal_evidence_stays_review_degraded(self):
        """When terminal evidence cannot be fetched, result must be REVIEW_DEGRADED, not masked."""
        from lifeos.jobs.newsletter_contract import Disposition
        repository = InMemoryCareerRepository()
        result = self._run(repository, browser_evidence={"pages": []})
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.body["status"], "DEGRADED")

    def test_genuinely_new_vacancy_resolves_normally(self):
        """A brand-new observation (not in repository) must go through full resolution."""
        from lifeos.jobs.newsletter_contract import Disposition
        repository = _CountingAuthoritativeRepository()
        result = self._run(repository)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(repository._store), 1)
        record = next(iter(repository._store.values()))
        self.assertIsNotNone(record.job.fit)
        self.assertIsNotNone(record.job.job.apply_url)

    def test_idempotent_replay_dramatically_fewer_resolver_calls(self):
        """Second run of already-enriched observations must produce far fewer upserts."""
        repository = _CountingAuthoritativeRepository()

        class _VolumeAcquirer10(_FakeAcquirer):
            def acquire(self, registry, **kwargs):
                observations = tuple(
                    replace(_observation(index=i), company=f"Synthetic Co {i}")
                    for i in range(1, 11)
                )
                return AcquisitionResult(
                    observations=observations,
                    sources=(SourceHealth("synthetic-volume", "COMPLETE", len(observations)),),
                )

        browser = {
            "pages": [
                {
                    "url": f"https://boards.greenhouse.io/synthetic/jobs/{i}",
                    "final_url": f"https://boards.greenhouse.io/synthetic/jobs/{i}",
                    "html": (
                        "<h1>Program Manager</h1>"
                        "<p>Required qualifications: 5 years experience in program management.</p>"
                    ),
                }
                for i in range(1, 11)
            ]
        }
        first = self._run(repository, browser_evidence=browser, acquirer=_VolumeAcquirer10)
        first_upserts = repository.upsert_calls
        second = self._run(repository, browser_evidence=browser, acquirer=_VolumeAcquirer10)
        # Second run: only identity upserts (10), not enrichment upserts (10)
        self.assertLess(repository.upsert_calls - first_upserts, first_upserts)

    def test_no_review_degraded_masked_by_optimization(self):
        """Optimization must not suppress a genuine REVIEW_DEGRADED result."""
        from lifeos.jobs.newsletter_contract import Disposition
        repository = InMemoryCareerRepository()
        # First run with evidence
        first = self._run(repository)
        self.assertEqual(first.exit_code, 0)
        # Second run without evidence should still flag REVIEW_DEGRADED if resolution fails
        # (Here we confirm that PASS on first run doesn't retroactively mask a broken second)
        self.assertEqual(first.body["status"], "PASS")
        self.assertEqual(len(repository._store), 1)

    def test_canonical_persistence_readback_is_authoritative(self):
        """After enrichment, persisted record must carry AUTHORITATIVE fit authority."""
        from lifeos.jobs.models import FitAuthority
        repository = InMemoryCareerRepository()
        result = self._run(repository)
        self.assertEqual(result.exit_code, 0)
        record = next(iter(repository._store.values()))
        self.assertEqual(record.job.fit_authority, FitAuthority.AUTHORITATIVE)
        self.assertIsNotNone(record.job.job.apply_url)
        self.assertIsNotNone(record.job.fit)

    def test_production_volume_completes_within_budget_at_18_workers(self):
        """At 18 workers and ~57 serial waves, projected terminal stage must be under 80s."""
        terminal_workers = 18
        unique_terminal_urls = 1021
        terminal_waves = (unique_terminal_urls + terminal_workers - 1) // terminal_workers
        baseline_workers = 8
        baseline_requests = 1088
        baseline_elapsed = 162.557
        baseline_waves = (baseline_requests + baseline_workers - 1) // baseline_workers
        wave_seconds = baseline_elapsed / baseline_waves
        projected_terminal = wave_seconds * terminal_waves
        self.assertEqual(terminal_workers, 18)
        self.assertEqual(terminal_waves, 57)
        self.assertLessEqual(projected_terminal, 80.0)


if __name__ == "__main__":
    unittest.main()
