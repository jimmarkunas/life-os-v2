"""Deterministic JOBS_USER_OUTCOME_GATE for the Newsletter Jobs vertical slice."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from lifeos.core.runtime import RunContext
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.models import AdmissionStatus
from lifeos.jobs.newsletter_runtime import execute_newsletter
from lifeos.jobs.qualification import LaneConfig, UNIVERSAL_FIT_FLOOR
from lifeos.jobs.repository import InMemoryCareerRepository
from lifeos.jobs.terminal_evidence import TerminalVacancyEvidence
from lifeos.newsletter import ParseState, RoutedNewsletterMessage, parse_message


NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)


def _message(message_id: str, sender: str, subject: str, body: str) -> RoutedNewsletterMessage:
    return RoutedNewsletterMessage("gmail", message_id, NOW, sender, subject, body)


def _profile() -> FitProfile:
    dimensions = {
        "role_seniority": (r"years? experience", "senior", "lead"),
        "functional": ("program", "delivery", "management"),
        "technical_platform": ("cloud", "platform", "data"),
        "delivery_complexity": ("complex", "cross-functional"),
        "competitive_advantage": ("strategy", "automation", "AI"),
    }
    classes = {"DIRECT": ("required", "must", "experience"), "ADJACENT": ("preferred",), "METHOD_EQUIVALENT": ("plus",), "UNSUPPORTED": ("software engineer",)}
    return FitProfile("JOBS_USER_OUTCOME_GATE", {"DIRECT": ("program", "project"), "ADJACENT": ("architect",), "METHOD_EQUIVALENT": ("delivery",), "UNSUPPORTED": ("software engineer",)}, ("automation", "AI"), dimensions, classes, ("required", "must", "experience", "lead", "manage"), (), ("hands-on coding",))


class _Gmail:
    def __init__(self, message: SimpleNamespace):
        self.message = message
        self.marked: list[str] = []

    def enumerate_unprocessed_ids(self, _boundary):
        return (self.message.message_id,)

    def hydrate_messages(self, _ids):
        return (self.message,)

    def mark_newsletter_processed(self, message_id, _boundary):
        self.marked.append(message_id)

    def newsletter_backlog_snapshot(self, _boundary, *, now):
        return SimpleNamespace()


def main() -> int:
    jobright = parse_message(_message("jobright", "alerts@jobright.invalid", "Jobright daily jobs", "[Synthetic Labs\n92%\nSenior Program Manager\nRemote\n$120K-$150K/yr](https://jobright.ai/jobs/info/synthetic-high)"))
    lensa = parse_message(_message("lensa", "alerts@lensa.example.invalid", "Lensa job alert", "[Synthetic Works Senior Project Manager Remote $110K-$130K](https://jobs.lensa.com/synthetic-low)"))
    linkedin = parse_message(_message("linkedin", "jobs@linkedin.example.invalid", "LinkedIn jobs for you", "[Senior Product Manager\nSynthetic Systems · Remote](https://www.linkedin.com/jobs/view/123456789/)"))
    junk = parse_message(_message("junk", "alerts@jobright.invalid", "Jobright daily jobs", "[Malformed card](https://jobright.ai/jobs/info/junk)"))
    observations = list(jobright.observations + lensa.observations + linkedin.observations + junk.observations)
    observations.append(replace(observations[0], evidence_ref="duplicate-jobright"))
    message = SimpleNamespace(message_id="gate", message_ref="gmail:gate", state=ParseState.PASS, observations=tuple(observations))
    processed = SimpleNamespace(state=ParseState.PASS, messages=(message,), observations=tuple(observations), errors=())
    high_url = observations[0].source_apply_url
    low_url = observations[1].source_apply_url
    unresolved_url = observations[2].source_apply_url
    evidence = {
        high_url: TerminalVacancyEvidence("https://boards.greenhouse.io/synthetic/jobs/high", "Required: lead program delivery and cloud strategy. Must manage complex cross-functional programs with 8 years experience.", "", "vacancy_page", (high_url,)),
        low_url: TerminalVacancyEvidence("https://boards.greenhouse.io/synthetic/jobs/low", "A role with no applicable requirements.", "", "vacancy_page", (low_url,)),
        unresolved_url: None,
    }
    repo = InMemoryCareerRepository()
    gmail = _Gmail(SimpleNamespace(message_id="gate", received_at=NOW, sender="alerts@jobright.invalid", subject="Jobs", headers={}, body_text=""))
    lane = LaneConfig("US Remote", "US", UNIVERSAL_FIT_FLOOR, None, "remote_only", None, False, None)
    resolver_calls: list[str] = []

    def resolve(url, **_kwargs):
        resolver_calls.append(url)
        return evidence.get(url)

    with patch("lifeos.jobs.newsletter_runtime.MailRouter.route_window"), patch("lifeos.jobs.newsletter_runtime.NewsletterProcessor.process_messages", return_value=processed), patch("lifeos.jobs.newsletter_runtime.NotionCareerRepository", return_value=repo), patch("lifeos.jobs.newsletter_adapter.acquire_terminal_vacancy_evidence", side_effect=resolve):
        result = execute_newsletter(context=RunContext.start(timeout_seconds=30), http=None, notion=None, gmail=gmail, browser_evidence=None, lane=lane, lane_priority={"US Remote": 0}, fit_profile=_profile(), market="US", newsletter_source_lane="US Remote", notion_job_ledger_data_source_id="gate", inbox_start=NOW, inbox_mode="normal", start=NOW, end=NOW + timedelta(hours=1), dry_run=False)

    dispositions = result.body["jobs"]["dispositions"]
    rows = list(repo._store.values())
    keys = [row.job.stable_job_key for row in rows]
    assert len(rows) == len(set(keys)), "duplicate canonical Jobs"
    assert len(rows) >= 3, "legitimate vacancies did not persist before enrichment"
    assert dispositions.get("review_degraded", 0) >= 1, "unresolved terminal evidence was not degraded"
    assert not gmail.marked, "degraded Newsletter was finalized"
    assert high_url in resolver_calls and low_url in resolver_calls and unresolved_url in resolver_calls
    assert any(row.job.job.description_text for row in rows), "trustworthy terminal JD did not persist"
    high = next(row for row in rows if row.job.job.apply_url == "https://boards.greenhouse.io/synthetic/jobs/high")
    low = next(row for row in rows if row.job.job.apply_url == "https://boards.greenhouse.io/synthetic/jobs/low")
    assert high.job.fit is not None and high.job.fit >= UNIVERSAL_FIT_FLOOR
    assert low.job.fit is None or low.job.fit < UNIVERSAL_FIT_FLOOR
    assert any(row.job.admission_status is AdmissionStatus.EXCLUDED for row in rows) or low.job.eligible_lanes == ()
    print("JOBS_USER_OUTCOME_GATE: PASS")
    print(f"observations={len(observations)} canonical_jobs={len(rows)} resolver_calls={len(resolver_calls)} dispositions={dispositions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
