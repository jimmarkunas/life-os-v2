"""Lowest-real-boundary proof for bounded historical Inbox automated-job-alert
residue recovery, exercised through the actual production entrypoint
(entry.main), not a manual composition of pipeline pieces.

All identifiers, senders, subjects, and URLs below are synthetic.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from lifeos.core.http import HttpClient, HttpResponse
from lifeos.jobs.identity import stable_job_key
from lifeos.jobs.models import Company, Job, WorkMode
from lifeos.jobs.us_remote_acquisition import AcquisitionResult, SourceHealth
from lifeos.mail.models import MailRef
from lifeos.mail.router import MailExecutionState, MailRouteResult, ProviderScan, RoutingError, RoutingTimings
from lifeos.newsletter.models import SourceVacancyObservation

from lifeos.jobs import us_remote_runtime as runtime
from scripts import run_us_remote_production as entry
from scripts.run_newsletter_production import _GOOGLE_TOKEN_URL

PRIVATE_POLICY = {
    "market": "Synthetic-US",
    "source_lane": "Newsletter",
    "lane_priority": {"Synthetic-Newsletter": 0, "US Web": 1},
    "lane": {
        "name": "Synthetic-Newsletter",
        "market": "Synthetic-US",
        "fit_floor": 0,
        "target_review_floor": None,
        "work_mode_policy": "any",
        "compensation_floor": None,
        "freshness_gate": False,
        "freshness_max_days": None,
    },
    "fit_profile": {
        "model_version": "test-1",
        "default_role_base": 10,
        "default_role_label": "weak",
        "role_families": [],
    },
}

REQUIRED_ENV = {
    "GMAIL_OAUTH_CLIENT_ID": "synthetic-client-id",
    "GMAIL_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
    "GMAIL_OAUTH_REFRESH_TOKEN": "synthetic-refresh-token",
    "NOTION_API_TOKEN": "synthetic-notion-token",
    "NOTION_JOB_LEDGER_DATA_SOURCE_ID": "synthetic-data-source",
}

EMPTY_REGISTRY = {"schema_version": 1, "tier1_employers": [], "staffing_agencies": [], "discovery_helpers": []}

RESOLVED_APPLY_URL = "https://greenhouse.io/acme/jobs/historical-1"
EXISTING_STABLE_KEY = stable_job_key(
    Job(
        company=Company(name="Synthetic Labs"),
        role="Synthetic Historical Engineer",
        location="Remote",
        work_mode=WorkMode.REMOTE,
        compensation_text=None,
        compensation_minimum=None,
        posting_date=None,
        apply_url=RESOLVED_APPLY_URL,
        source_lane="Newsletter",
    )
)


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode("ascii").rstrip("=")


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def _synthetic_web_observation() -> SourceVacancyObservation:
    return SourceVacancyObservation(
        evidence_ref="synthetic-web-1",
        source_provider="SyntheticWeb",
        source_mailbox="web",
        source_message_id="synthetic-web-source",
        source_subject="Synthetic Web Source",
        company="Synthetic Labs",
        role="Synthetic Historical Engineer",
        location_text="Remote",
        compensation_text=None,
        source_apply_url=RESOLVED_APPLY_URL,
        source_received_at=datetime.now(timezone.utc),
    )


class HistoricalInboxBackend:
    """One synthetic Gmail-like Inbox plus a Notion Job Ledger. Messages are
    filtered by the real after:/before: epoch query, exactly like live Gmail,
    so normal vs. historical-recovery windows are proven by the real query
    boundary, not by test-side bookkeeping."""

    def __init__(self, *, existing_stable_key: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        old_job_alert_at = now - timedelta(hours=48)  # older than the 24h normal staging window
        self._messages = {
            "msg-old-job-alert": {
                "sender": "alerts@jobright.example.invalid",
                "subject": "Jobright daily jobs for you",
                "received_at": old_job_alert_at,
                "list_unsubscribe": True,
                "body": _b64(
                    "[Synthetic Labs\n90%\nSynthetic Historical Engineer\n"
                    f"Remote](https://jobright.ai/jobs/info/historical-1)\nView more opportunities"
                ),
            },
            "msg-old-recruiter": {
                "sender": "person@example.invalid",
                "subject": "Following up - recruiter here about your background",
                "received_at": old_job_alert_at,
                "list_unsubscribe": False,
                "body": _b64("Hi, I am a recruiter and wanted to connect about a role."),
            },
            "msg-old-unrelated": {
                "sender": "notifications@shipping.example.invalid",
                "subject": "Your package has shipped",
                "received_at": old_job_alert_at,
                "list_unsubscribe": True,
                "body": _b64("Your order is on its way."),
            },
        }
        self.inbox_removed_ids: list[str] = []
        self.routed_ids: list[str] = []
        self.processed_ids: list[str] = []
        self.detail_fetch_ids: list[str] = []
        self.metadata_fetch_ids: list[str] = []
        self.pages: dict[str, dict] = {}
        self._next_page = 1
        if existing_stable_key:
            page_id = "page-existing-1"
            self.pages[page_id] = {
                "id": page_id,
                "properties": {
                    "Stable Job Key": {"rich_text": [{"text": {"content": existing_stable_key}}]},
                    "Company": {"rich_text": [{"text": {"content": "Synthetic Labs"}}]},
                    "Role": {"rich_text": [{"text": {"content": "Synthetic Historical Engineer"}}]},
                    "Location / Work Mode": {"rich_text": [{"text": {"content": "Remote"}}]},
                    "Work Mode": {"select": {"name": "Remote"}},
                    "Apply URL": {"url": RESOLVED_APPLY_URL},
                    "Admission Status": {"select": {"name": "Passed / Review"}},
                    "LIFE OS Fit": {"number": 50},
                    "Fit Authority": {"select": {"name": "Non-Authoritative"}},
                    "Source Provider": {"rich_text": [{"text": {"content": "Jobright"}}]},
                    "First Surfaced": {"date": {"start": "2026-06-01"}},
                    "Last Seen": {"date": {"start": "2026-06-01"}},
                },
            }
            self._next_page += 1

    # -- HttpClient backend protocol -----------------------------------
    def request(self, method, url, *, headers, body, timeout_seconds) -> HttpResponse:
        if url == _GOOGLE_TOKEN_URL:
            return HttpResponse(200, {}, json.dumps({"access_token": "synthetic-access-token"}).encode())
        if "gmail.googleapis.com" in url:
            return self._gmail(method, url, body)
        if "api.notion.com" in url:
            return self._notion(method, url, body)
        if url == "https://jobright.ai/jobs/info/historical-1":
            return HttpResponse(
                200, {}, f'<a href="{RESOLVED_APPLY_URL}">Apply</a>'.encode(), final_url=url
            )
        if url == RESOLVED_APPLY_URL:
            body_html = (
                '<html><script type="application/ld+json">'
                '{"@type": "JobPosting", "description": "Synthetic historical role.", '
                '"datePosted": "2026-01-10"}</script></html>'
            )
            return HttpResponse(200, {}, body_html.encode(), final_url=url)
        raise AssertionError(f"unexpected URL {url}")

    def _gmail(self, method, url, body) -> HttpResponse:
        if method == "GET" and url.endswith("/labels"):
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "labels": [
                            {"id": "label-news", "name": "J Newsletters"},
                            {"id": "label-processed", "name": "J Newsletters/Processed"},
                        ]
                    }
                ).encode(),
            )
        if method == "GET" and "/messages?" in url and "labelIds=label-news" not in url:
            # Inbox metadata-first date-range scan (Mail Router /
            # scan_inbox_metadata_window, labelIds=INBOX). Real Gmail
            # semantics: filter strictly by the after:/before: query.
            parsed = urlsplit(url)
            q = parse_qs(parsed.query).get("q", [""])[0]
            after = next((int(part.split(":", 1)[1]) for part in q.split() if part.startswith("after:")), None)
            before = next((int(part.split(":", 1)[1]) for part in q.split() if part.startswith("before:")), None)
            matched = [
                mid
                for mid, msg in self._messages.items()
                if mid not in self.inbox_removed_ids
                and (after is None or _epoch(msg["received_at"]) >= after)
                and (before is None or _epoch(msg["received_at"]) <= before)
            ]
            return HttpResponse(200, {}, json.dumps({"messages": [{"id": mid} for mid in matched]}).encode())
        if method == "GET" and "/messages?" in url and "labelIds=label-news" in url:
            remaining = [mid for mid in self.routed_ids if mid not in self.processed_ids]
            return HttpResponse(200, {}, json.dumps({"messages": [{"id": mid} for mid in remaining]}).encode())
        if method == "GET" and "format=metadata" in url:
            # Metadata-first Inbox staging scan: real Gmail semantics never
            # return a body under format=metadata.
            message_id = url.split("/messages/", 1)[1].split("?", 1)[0]
            self.metadata_fetch_ids.append(message_id)
            msg = self._messages[message_id]
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "id": message_id,
                        "internalDate": str(_epoch(msg["received_at"]) * 1000),
                        "payload": {
                            "headers": [
                                {"name": "From", "value": msg["sender"]},
                                {"name": "Subject", "value": msg["subject"]},
                                *(
                                    [{"name": "List-Unsubscribe", "value": "<https://example.invalid/unsub>"}]
                                    if msg["list_unsubscribe"]
                                    else []
                                ),
                            ],
                        },
                    }
                ).encode(),
            )
        if method == "GET" and "?format=full" in url:
            message_id = url.split("/messages/", 1)[1].split("?", 1)[0]
            self.detail_fetch_ids.append(message_id)
            msg = self._messages[message_id]
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "id": message_id,
                        "internalDate": str(_epoch(msg["received_at"]) * 1000),
                        "payload": {
                            "mimeType": "text/plain",
                            "headers": [
                                {"name": "From", "value": msg["sender"]},
                                {"name": "Subject", "value": msg["subject"]},
                                *(
                                    [{"name": "List-Unsubscribe", "value": "<https://example.invalid/unsub>"}]
                                    if msg["list_unsubscribe"]
                                    else []
                                ),
                            ],
                            "body": {"data": msg["body"]},
                        },
                    }
                ).encode(),
            )
        if method == "POST" and url.endswith("/modify"):
            message_id = url.split("/messages/", 1)[1].split("/modify", 1)[0]
            payload = json.loads(body.decode("utf-8")) if body else {}
            add = payload.get("addLabelIds") or []
            remove = payload.get("removeLabelIds") or []
            if "label-news" in add:
                self.routed_ids.append(message_id)
            if "label-processed" in add:
                self.processed_ids.append(message_id)
            if "INBOX" in remove:
                self.inbox_removed_ids.append(message_id)
            return HttpResponse(200, {}, b"{}")
        raise AssertionError(f"unexpected Gmail call {method} {url}")

    def _notion(self, method, url, body) -> HttpResponse:
        payload = json.loads(body.decode("utf-8")) if body else {}
        if method == "POST" and url.endswith("/query"):
            wanted = payload["filter"]
            clauses = wanted["or"] if "or" in wanted else [wanted]

            def _clause_equals(clause):
                value_dict = next(v for k, v in clause.items() if k != "property")
                return value_dict["equals"]

            stable_key_values = {
                _clause_equals(clause) for clause in clauses if clause.get("property") == "Stable Job Key"
            }
            apply_url_values = {
                _clause_equals(clause) for clause in clauses if clause.get("property") == "Apply URL"
            }
            results = [
                p
                for p in self.pages.values()
                if (stable_key_values and self._key(p) in stable_key_values)
                or (apply_url_values and self._apply_url(p) in apply_url_values)
            ]
            return HttpResponse(200, {}, json.dumps({"results": results, "has_more": False}).encode())
        if method == "POST" and url.endswith("/pages"):
            page_id = f"page-{self._next_page}"
            self._next_page += 1
            page = {"id": page_id, "properties": payload["properties"]}
            self.pages[page_id] = page
            return HttpResponse(200, {}, json.dumps(page).encode())
        if method == "PATCH":
            page_id = url.rsplit("/", 1)[-1]
            self.pages[page_id]["properties"].update(payload["properties"])
            return HttpResponse(200, {}, json.dumps(self.pages[page_id]).encode())
        if method == "GET" and "/pages/" in url:
            page_id = url.rsplit("/", 1)[-1]
            return HttpResponse(200, {}, json.dumps(self.pages[page_id]).encode())
        raise AssertionError(f"unexpected Notion call {method} {url}")

    @staticmethod
    def _key(page):
        items = page["properties"].get("Stable Job Key", {}).get("rich_text", [])
        return "".join(i.get("text", {}).get("content", "") for i in items)

    @staticmethod
    def _apply_url(page):
        return (page["properties"].get("Apply URL") or {}).get("url")


class HistoricalInboxRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(PRIVATE_POLICY, handle)
        handle.close()
        self._policy_path = handle.name
        self.addCleanup(os.unlink, self._policy_path)
        self._env = dict(REQUIRED_ENV)
        self._env["NEWSLETTER_PRIVATE_POLICY_PATH"] = self._policy_path

    def _run(self, backend, *cli_args):
        fake_client = HttpClient(backend=backend)
        with patch.dict(os.environ, self._env, clear=True), patch.object(
            entry, "HttpClient", return_value=fake_client
        ), patch.object(entry, "load_registry", return_value=EMPTY_REGISTRY):
            exit_code = entry.main(list(cli_args) + ["--timeout-seconds", "60"])
        return exit_code

    def _run_capturing_summary(self, backend, *cli_args):
        import contextlib
        import io

        fake_client = HttpClient(backend=backend)
        stdout = io.StringIO()
        with patch.dict(os.environ, self._env, clear=True), patch.object(
            entry, "HttpClient", return_value=fake_client
        ), patch.object(entry, "load_registry", return_value=EMPTY_REGISTRY), contextlib.redirect_stdout(stdout):
            exit_code = entry.main(list(cli_args) + ["--timeout-seconds", "60"])
        return exit_code, json.loads(stdout.getvalue())

    def test_normal_production_does_not_acquire_old_inbox_job_alert(self) -> None:
        # The empty synthetic Web registry used by every test here makes
        # US Web acquisition report incomplete (zero sources), so overall
        # status/exit code reflect that unrelated fact, not Mail/Newsletter
        # behavior -- assert the Mail/Newsletter/Jobs summary sections
        # directly instead of the web-coupled top-level exit code.
        backend = HistoricalInboxBackend()

        _exit_code, summary = self._run_capturing_summary(backend)

        self.assertEqual(summary["mail"]["mode"], "normal")
        self.assertEqual(summary["mail"]["scanned"], 0)
        self.assertEqual(summary["mail"]["staged"], 0)
        self.assertTrue(summary["mail"]["staging_safe"])
        self.assertEqual(backend.routed_ids, [])
        self.assertEqual(backend.inbox_removed_ids, [])
        self.assertEqual(backend.pages, {})

    def test_historical_recovery_routes_processes_and_reconciles_idempotently(self) -> None:
        backend = HistoricalInboxBackend(existing_stable_key=EXISTING_STABLE_KEY)
        self.assertEqual(len(backend.pages), 1)

        exit_code, summary = self._run_capturing_summary(
            backend, "--historical-inbox-recovery-hours", "72"
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "DEGRADED")
        self.assertEqual(summary["mail"]["status"], "PASS")
        self.assertEqual(summary["web"]["status"], "DEGRADED")
        self.assertEqual(summary["mail"]["mode"], "historical_recovery")
        self.assertTrue(summary["mail"]["staging_safe"])
        self.assertEqual(summary["mail"]["processed"], 1)
        self.assertEqual(summary["mail"]["processed_errors"], 0)
        self.assertEqual(summary["newsletter"]["state"], "PASS")
        self.assertEqual(summary["jobs"]["dispositions"]["updated"], 1)
        self.assertEqual(summary["jobs"]["dispositions"]["created"], 0)
        # Only the confidently classified automated job alert was routed.
        self.assertEqual(backend.routed_ids, ["msg-old-job-alert"])
        self.assertEqual(backend.inbox_removed_ids, ["msg-old-job-alert"])
        # Inbox staging classified all three candidate messages from
        # metadata alone, including the one whose confirmed AUTOMATED_JOB_SOURCE
        # routing decision came from headers only.
        self.assertEqual(
            sorted(backend.metadata_fetch_ids),
            sorted(["msg-old-job-alert", "msg-old-recruiter", "msg-old-unrelated"]),
        )
        # format=full is reached only later, for the one message hydrated
        # out of J Newsletters for Newsletter body parsing -- never during
        # Inbox staging classification itself.
        self.assertEqual(backend.detail_fetch_ids, ["msg-old-job-alert"])
        # Human/recruiter and unrelated automated mail were never touched.
        self.assertNotIn("msg-old-recruiter", backend.routed_ids)
        self.assertNotIn("msg-old-unrelated", backend.routed_ids)
        self.assertNotIn("msg-old-recruiter", backend.inbox_removed_ids)
        self.assertNotIn("msg-old-unrelated", backend.inbox_removed_ids)
        # Safe accounting completed: message marked Processed.
        self.assertEqual(backend.processed_ids, ["msg-old-job-alert"])
        # Idempotent reconciliation: no duplicate canonical row created.
        self.assertEqual(len(backend.pages), 1)
        self.assertEqual(backend.pages["page-existing-1"]["properties"]["Stable Job Key"]["rich_text"][0]["text"]["content"], EXISTING_STABLE_KEY)

        # Immediate canonical replay is idempotent: the message is gone from
        # Inbox and no longer eligible, so a second recovery run touches
        # nothing further.
        _exit_code_2, summary_2 = self._run_capturing_summary(
            backend, "--historical-inbox-recovery-hours", "72"
        )
        self.assertEqual(summary_2["mail"]["staged"], 0)
        self.assertEqual(backend.routed_ids, ["msg-old-job-alert"])
        self.assertEqual(len(backend.pages), 1)

    def test_mail_degraded_does_not_poison_authoritative_web_reconciliation(self) -> None:
        backend = HistoricalInboxBackend()

        class DegradedMailRouter:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def route_window(self, *args, **kwargs) -> MailRouteResult:
                return MailRouteResult(
                    state=MailExecutionState.DEGRADED,
                    provider_scans=(ProviderScan("gmail", False, 0, "synthetic-scan-error"),),
                    records=(),
                    errors=(
                        RoutingError(
                            MailRef("gmail", "synthetic-message"),
                            "scan",
                            "synthetic-scan-error",
                        ),
                    ),
                    timings=RoutingTimings(0.0, 0.0, 0.0, 0.0),
                )

        class CompleteWebAcquirer:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def acquire(self, *args, **kwargs) -> AcquisitionResult:
                return AcquisitionResult(
                    observations=(_synthetic_web_observation(),),
                    sources=(SourceHealth("synthetic-web", "COMPLETE", 1),),
                )

        fake_client = HttpClient(backend=backend)
        import contextlib
        import io

        stdout = io.StringIO()
        with patch.dict(os.environ, self._env, clear=True), patch.object(
            entry, "HttpClient", return_value=fake_client
        ), patch.object(entry, "load_registry", return_value=EMPTY_REGISTRY), patch.object(
            runtime, "MailRouter", DegradedMailRouter
        ), patch.object(
            runtime, "USRemoteAcquirer", CompleteWebAcquirer
        ), contextlib.redirect_stdout(stdout):
            exit_code = entry.main(["--timeout-seconds", "60"])

        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "DEGRADED")
        self.assertEqual(summary["mail"]["status"], "DEGRADED")
        self.assertEqual(summary["web"]["status"], "PASS")
        self.assertFalse(summary["mail"]["staging_safe"])
        self.assertEqual(summary["mail"]["processed"], 0)
        self.assertEqual(summary["mail"]["processed_errors"], 0)
        self.assertEqual(summary["web"]["observations"], 1)
        self.assertTrue(summary["jobs"]["fully_accounted"])
        self.assertEqual(summary["jobs"]["dispositions"]["created"], 1)
        self.assertEqual(backend.processed_ids, [])
        self.assertEqual(backend.routed_ids, [])
        self.assertEqual(len(backend.pages), 1)

    def test_staging_failure_does_not_block_safe_already_staged_message_completion(self) -> None:
        backend = HistoricalInboxBackend()
        backend.routed_ids.append("msg-old-job-alert")
        backend.inbox_removed_ids.append("msg-old-job-alert")

        class DegradedMailRouter:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def route_window(self, *args, **kwargs) -> MailRouteResult:
                return MailRouteResult(
                    state=MailExecutionState.DEGRADED,
                    provider_scans=(ProviderScan("gmail", False, 0, "synthetic-scan-error"),),
                    records=(),
                    errors=(
                        RoutingError(
                            MailRef("gmail", "synthetic-message"),
                            "scan",
                            "synthetic-scan-error",
                        ),
                    ),
                    timings=RoutingTimings(0.0, 0.0, 0.0, 0.0),
                )

        fake_client = HttpClient(backend=backend)
        import contextlib
        import io

        stdout = io.StringIO()
        with patch.dict(os.environ, self._env, clear=True), patch.object(
            entry, "HttpClient", return_value=fake_client
        ), patch.object(entry, "load_registry", return_value=EMPTY_REGISTRY), patch.object(
            runtime, "MailRouter", DegradedMailRouter
        ), contextlib.redirect_stdout(stdout):
            exit_code = entry.main(["--timeout-seconds", "60"])

        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "DEGRADED")
        self.assertEqual(summary["mail"]["status"], "DEGRADED")
        self.assertFalse(summary["mail"]["staging_safe"])
        self.assertEqual(summary["mail"]["processed"], 1)
        self.assertEqual(summary["mail"]["processed_errors"], 0)
        self.assertEqual(summary["newsletter"]["state"], "PASS")
        self.assertEqual(summary["jobs"]["dispositions"]["created"], 1)
        self.assertEqual(backend.processed_ids, ["msg-old-job-alert"])
        self.assertEqual(len(backend.pages), 1)

    def test_recovery_hours_must_be_bounded(self) -> None:
        backend = HistoricalInboxBackend()
        exit_code = self._run(backend, "--historical-inbox-recovery-hours", "10000")
        self.assertEqual(exit_code, 2)
        self.assertEqual(backend.routed_ids, [])


class HistoricalInboxRecoveryNegativeControlTests(unittest.TestCase):
    """Canonical persistence/read-back fails for the recovered message:
    prove no Processed mark, no false PASS, no duplicate row, and the
    message stays staged/recoverable in canonical Gmail state."""

    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(PRIVATE_POLICY, handle)
        handle.close()
        self._policy_path = handle.name
        self.addCleanup(os.unlink, self._policy_path)
        self._env = dict(REQUIRED_ENV)
        self._env["NEWSLETTER_PRIVATE_POLICY_PATH"] = self._policy_path

    def test_persistence_failure_leaves_message_unprocessed_and_recoverable(self) -> None:
        backend = HistoricalInboxBackend()

        class FailingRepository:
            def find_existing(self, *_a, **_k):
                raise RuntimeError("synthetic Notion outage")

        fake_client = HttpClient(backend=backend)
        with patch.dict(os.environ, self._env, clear=True), patch.object(
            entry, "HttpClient", return_value=fake_client
        ), patch.object(entry, "load_registry", return_value=EMPTY_REGISTRY), patch.object(
            runtime, "NotionCareerRepository", side_effect=lambda *a, **k: FailingRepository()
        ):
            exit_code = entry.main(
                ["--historical-inbox-recovery-hours", "72", "--timeout-seconds", "60"]
            )

        self.assertEqual(exit_code, 1)
        # Message was routed to J Newsletters (confident classification is
        # independent of downstream Jobs persistence), but never marked
        # Processed, and no canonical row was created -- it remains staged
        # and recoverable through the exact existing canonical Gmail state.
        self.assertEqual(backend.routed_ids, ["msg-old-job-alert"])
        self.assertEqual(backend.processed_ids, [])
        self.assertEqual(backend.pages, {})


if __name__ == "__main__":
    unittest.main()
