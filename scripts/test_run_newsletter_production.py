from __future__ import annotations

import base64
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from lifeos.core.http import HttpClient, HttpResponse
from lifeos.core.runtime import ExecutionResult
from lifeos.jobs.newsletter_contract import Disposition, IngestResult
from lifeos.jobs.newsletter_feature import NewsletterFeatureResult
from lifeos.newsletter.processor import NewsletterError, NewsletterExecutionState, NewsletterProcessResult, NewsletterTimings

from . import run_newsletter_production as entry

PRIVATE_POLICY = {
    "market": "Synthetic-US",
    "source_lane": "Newsletter",
    "lane_priority": {"Synthetic-Newsletter": 0},
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
        "role_families": [{"patterns": ["\\bsynthetic engineer\\b"], "base_score": 70, "label": "match"}],
    },
}

REQUIRED_ENV = {
    "GMAIL_OAUTH_CLIENT_ID": "synthetic-client-id",
    "GMAIL_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
    "GMAIL_OAUTH_REFRESH_TOKEN": "synthetic-refresh-token",
    "NOTION_API_TOKEN": "synthetic-notion-token",
    "NOTION_JOB_LEDGER_DATA_SOURCE_ID": "synthetic-data-source",
}


class ConfigLoadingTests(unittest.TestCase):
    def test_loads_lane_priority_and_fit_profile_from_private_file(self, tmp_path=None) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(PRIVATE_POLICY, handle)
            path = handle.name
        try:
            lane, lane_priority, fit_profile, market, source_lane = entry._load_private_policy(path)
        finally:
            os.unlink(path)
        self.assertEqual(lane.name, "Synthetic-Newsletter")
        self.assertEqual(lane_priority, {"Synthetic-Newsletter": 0})
        self.assertEqual(fit_profile.model_version, "test-1")
        self.assertEqual(market, "Synthetic-US")
        self.assertEqual(source_lane, "Newsletter")

    def test_malformed_policy_json_fails_closed(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("not json")
            path = handle.name
        try:
            with self.assertRaises(entry.ProductionConfigError):
                entry._load_private_policy(path)
        finally:
            os.unlink(path)

    def test_policy_missing_required_key_fails_closed(self) -> None:
        import tempfile

        broken = {"market": "x"}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(broken, handle)
            path = handle.name
        try:
            with self.assertRaises(entry.ProductionConfigError):
                entry._load_private_policy(path)
        finally:
            os.unlink(path)


JOBPOSTING_HTML = """
<html><script type="application/ld+json">
{"@type": "JobPosting", "description": "Synthetic role.", "datePosted": "2026-01-10"}
</script></html>
"""


class SyntheticProductionBackend:
    """One in-memory backend standing in for Google OAuth, Gmail, and
    Notion. No real network. Proves the entry point's genuine composition
    of already-built components end to end."""

    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}
        self._next_page = 1
        self.routed_message_ids: list[str] = []
        self.processed_message_ids: list[str] = []

    def request(self, method, url, *, headers, body, timeout_seconds) -> HttpResponse:
        if url == entry._GOOGLE_TOKEN_URL:
            form = parse_qs(body.decode("utf-8"))
            assert form["grant_type"] == ["refresh_token"]
            return HttpResponse(200, {}, json.dumps({"access_token": "synthetic-access-token"}).encode())

        if "gmail.googleapis.com" in url:
            return self._gmail(method, url, body)

        if "api.notion.com" in url:
            return self._notion(method, url, body)

        if url == "https://jobright.ai/jobs/info/synthetic-1":
            return HttpResponse(
                200, {}, b'<a href="https://greenhouse.io/acme/jobs/1">Apply</a>', final_url=url
            )

        if url == "https://greenhouse.io/acme/jobs/1":
            return HttpResponse(200, {}, JOBPOSTING_HTML.encode(), final_url=url)

        raise AssertionError(f"unexpected URL {url}")

    def _gmail(self, method, url, body) -> HttpResponse:
        if method == "GET" and "/labels" in url:
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
        if method == "GET" and "/messages?" in url and "labelIds=" not in url:
            return HttpResponse(200, {}, json.dumps({"messages": [{"id": "msg-1"}]}).encode())
        if method == "GET" and "/messages?" in url and "labelIds=label-news" in url:
            return HttpResponse(200, {}, json.dumps({"messages": [{"id": "msg-1"}]}).encode())
        if method == "GET" and "/messages/msg-1?format=full" in url:
            job_body = base64.urlsafe_b64encode(
                b"[Synthetic Labs\n90%\nSynthetic Engineer\nRemote](https://jobright.ai/jobs/info/synthetic-1)\nView more opportunities"
            ).decode("ascii").rstrip("=")
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "id": "msg-1",
                        "internalDate": "1700000000000",
                        "payload": {
                            "mimeType": "text/plain",
                            "headers": [
                                {"name": "From", "value": "alerts@jobright.example.invalid"},
                                {"name": "Subject", "value": "Jobright daily jobs"},
                                {"name": "List-Unsubscribe", "value": "<https://example.invalid/unsub>"},
                            ],
                            "body": {"data": job_body},
                        },
                    }
                ).encode(),
            )
        if method == "POST" and "/messages/msg-1/modify" in url:
            payload = json.loads(body.decode("utf-8")) if body else {}
            labels = payload.get("addLabelIds") or []
            if "label-news" in labels:
                self.routed_message_ids.append("msg-1")
            if "label-processed" in labels:
                self.processed_message_ids.append("msg-1")
            return HttpResponse(200, {}, b"{}")
        if method == "GET" and "/messages/msg-1?" in url and "format=full" not in url:
            raise AssertionError("unexpected non-full message fetch")
        raise AssertionError(f"unexpected Gmail call {method} {url}")

    def _notion(self, method, url, body) -> HttpResponse:
        payload = json.loads(body.decode("utf-8")) if body else {}
        if method == "POST" and url.endswith("/query"):
            wanted = payload["filter"]
            key = wanted.get("rich_text", {}).get("equals") if "rich_text" in wanted else None
            results = [p for p in self.pages.values() if self._key(p) == key] if key else []
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


class MainEntryPointTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(PRIVATE_POLICY, handle)
        handle.close()
        self._policy_path = handle.name
        self.addCleanup(os.unlink, self._policy_path)

        self._env = dict(REQUIRED_ENV)
        self._env["NEWSLETTER_PRIVATE_POLICY_PATH"] = self._policy_path

    def _run(self, *cli_args, backend=None):
        backend = backend or SyntheticProductionBackend()
        fake_client = HttpClient(backend=backend)
        with patch.dict(os.environ, self._env, clear=True), patch.object(
            entry, "HttpClient", return_value=fake_client
        ):
            exit_code = entry.main(list(cli_args))
        return exit_code, backend

    def test_full_composed_run_routes_persists_reads_back_and_marks_processed(self) -> None:
        exit_code, backend = self._run("--timeout-seconds", "30")
        self.assertEqual(exit_code, 0)
        self.assertEqual(backend.routed_message_ids, ["msg-1"])
        self.assertEqual(backend.processed_message_ids, ["msg-1"])
        self.assertEqual(len(backend.pages), 1)

    def test_missing_required_env_blocks_before_any_network_call(self) -> None:
        backend = SyntheticProductionBackend()
        fake_client = HttpClient(backend=backend)
        with patch.dict(os.environ, {}, clear=True), patch.object(entry, "HttpClient", return_value=fake_client):
            exit_code = entry.main(["--timeout-seconds", "30"])
        self.assertEqual(exit_code, 2)
        self.assertEqual(backend.routed_message_ids, [])
        self.assertEqual(backend.processed_message_ids, [])
        self.assertEqual(backend.pages, {})

    def test_timeout_above_platform_ceiling_is_rejected(self) -> None:
        exit_code, _backend = self._run("--timeout-seconds", "301")
        self.assertEqual(exit_code, 2)

    def test_summary_preserves_newsletter_acquisition_error_details(self) -> None:
        process_result = NewsletterProcessResult(
            NewsletterExecutionState.DEGRADED,
            (),
            (
                NewsletterError(
                    "gmail",
                    "fetch",
                    "MailboxTransportError: Gmail Newsletter message acquisition incomplete: mailbox=gmail operation=fetch_unprocessed failed_messages=[msg-stuck:TimeoutError:synthetic]",
                ),
            ),
            NewsletterTimings(0.1, 0.0, 0.1),
        )

        summary = entry._safe_summary(
            dry_run=False,
            elapsed_seconds=0.1,
            mail_preview=None,
            mail_result=None,
            process_result=process_result,
            feature_result=None,
            processed_count=0,
            processed_errors=0,
        )

        self.assertEqual(summary["newsletter_parse"]["errors"], 1)
        self.assertEqual(summary["newsletter_parse"]["error_details"][0]["mailbox"], "gmail")
        self.assertEqual(summary["newsletter_parse"]["error_details"][0]["operation"], "fetch")
        self.assertIn("msg-stuck:TimeoutError:synthetic", summary["newsletter_parse"]["error_details"][0]["detail"])

    def test_summary_preserves_review_degraded_observation_detail(self) -> None:
        feature_result = NewsletterFeatureResult(
            execution=ExecutionResult.degraded(code="not-cleanup-safe"),
            ingest_results=(
                IngestResult(
                    "gmail:msg-1#0",
                    Disposition.REVIEW_DEGRADED,
                    None,
                    "cannot derive stable Job identity without canonical_identity, a canonical apply URL, or company+role+location",
                ),
            ),
            cleanup_safe=False,
        )

        summary = entry._safe_summary(
            dry_run=False,
            elapsed_seconds=0.1,
            mail_preview=None,
            mail_result=None,
            process_result=None,
            feature_result=feature_result,
            processed_count=0,
            processed_errors=0,
        )

        self.assertEqual(summary["jobs"]["dispositions"]["review_degraded"], 1)
        self.assertEqual(summary["jobs"]["review_degraded"][0]["evidence_ref"], "gmail:msg-1#0")
        self.assertIn("cannot derive stable Job identity", summary["jobs"]["review_degraded"][0]["detail"])


def _jobright_body(vacancy_id: str) -> str:
    raw = (
        f"[Synthetic Labs\n90%\nSynthetic Engineer {vacancy_id}\n"
        f"Remote](https://jobright.ai/jobs/info/synthetic-{vacancy_id})\nView more opportunities"
    )
    return base64.urlsafe_b64encode(raw.encode()).decode("ascii").rstrip("=")


def _unparseable_body() -> str:
    return base64.urlsafe_b64encode(b"no vacancy card here at all").decode("ascii").rstrip("=")


class FiveMessageBacklogBackend:
    """Canonical Gmail-like source A,B,C,D,E, oldest-first once reversed
    (Gmail lists newest-first). No platform-owned cursor: list results
    simply reflect which messages currently carry the processed label,
    exactly like real Gmail."""

    def __init__(
        self,
        *,
        degrade: str | None = None,
        fail_list: bool = False,
        fail_detail_for: str | None = None,
    ) -> None:
        self.pages: dict[str, dict] = {}
        self._next_page = 1
        self.processed_message_ids: list[str] = []
        self.detail_fetch_ids: list[str] = []
        self._newest_first_order = ["msg-E", "msg-D", "msg-C", "msg-B", "msg-A"]
        self._degrade = degrade  # message id whose body fails to parse, or None
        self._fail_list = fail_list  # simulate a malformed/failed Gmail list response
        self._fail_detail_for = fail_detail_for  # message id whose detail fetch fails

    def request(self, method, url, *, headers, body, timeout_seconds) -> HttpResponse:
        if url == entry._GOOGLE_TOKEN_URL:
            return HttpResponse(200, {}, json.dumps({"access_token": "synthetic-access-token"}).encode())
        if "gmail.googleapis.com" in url:
            return self._gmail(method, url, body)
        if "api.notion.com" in url:
            return self._notion(method, url, body)
        raise AssertionError(f"unexpected URL {url}")

    def _gmail(self, method, url, body) -> HttpResponse:
        if method == "GET" and "/labels" in url:
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
        if method == "GET" and "/messages?" in url and "labelIds=label-news" in url:
            if self._fail_list:
                # Malformed Gmail list response -- gmail.py's own
                # _list_message_ids raises MailboxTransportError for this,
                # a real acquisition-layer failure, not a test-injected one.
                return HttpResponse(200, {}, json.dumps(["not", "an", "object"]).encode())
            remaining = [mid for mid in self._newest_first_order if mid not in self.processed_message_ids]
            return HttpResponse(200, {}, json.dumps({"messages": [{"id": mid} for mid in remaining]}).encode())
        if method == "GET" and "?format=full" in url:
            message_id = url.split("/messages/", 1)[1].split("?", 1)[0]
            self.detail_fetch_ids.append(message_id)
            if message_id == self._fail_detail_for:
                # Malformed Gmail message-detail response -- gmail.py's own
                # _fetch_message_fields raises MailboxTransportError for
                # this, simulating a hydration failure on a selected item.
                return HttpResponse(200, {}, json.dumps(["not", "an", "object"]).encode())
            letter = message_id.rsplit("-", 1)[-1]
            body_data = _unparseable_body() if letter == self._degrade else _jobright_body(letter)
            index = ord(letter) - ord("A")
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "id": message_id,
                        "internalDate": str((1_700_000_000 + index) * 1000),
                        "payload": {
                            "mimeType": "text/plain",
                            "headers": [
                                {"name": "From", "value": "alerts@jobright.example.invalid"},
                                {"name": "Subject", "value": f"Jobright daily jobs {letter}"},
                                {"name": "List-Unsubscribe", "value": "<https://example.invalid/unsub>"},
                            ],
                            "body": {"data": body_data},
                        },
                    }
                ).encode(),
            )
        if method == "POST" and url.endswith("/modify"):
            message_id = url.split("/messages/", 1)[1].split("/modify", 1)[0]
            payload = json.loads(body.decode("utf-8")) if body else {}
            if "label-processed" in (payload.get("addLabelIds") or []):
                self.processed_message_ids.append(message_id)
            return HttpResponse(200, {}, b"{}")
        raise AssertionError(f"unexpected Gmail call {method} {url}")

    def _notion(self, method, url, body) -> HttpResponse:
        payload = json.loads(body.decode("utf-8")) if body else {}
        if method == "POST" and url.endswith("/query"):
            wanted = payload["filter"]
            key = wanted.get("rich_text", {}).get("equals") if "rich_text" in wanted else None
            results = [p for p in self.pages.values() if self._key(p) == key] if key else []
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


class ProductionBoundaryBoundedBacklogTests(unittest.TestCase):
    """The required acceptance scenario, through the actual production
    entrypoint (entry.main), not a manual composition of the primitive with
    Gmail helpers: canonical source A,B,C,D,E, batch size 3."""

    def setUp(self) -> None:
        import tempfile

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
        ), patch.object(entry, "BACKLOG_BATCH_SIZE", 3):
            exit_code = entry.main(list(cli_args) + ["--timeout-seconds", "30", "--skip-mail-router"])
        return exit_code

    def _run_capturing_summary(self, backend, *cli_args):
        import contextlib
        import io

        fake_client = HttpClient(backend=backend)
        stdout = io.StringIO()
        with patch.dict(os.environ, self._env, clear=True), patch.object(
            entry, "HttpClient", return_value=fake_client
        ), patch.object(entry, "BACKLOG_BATCH_SIZE", 3), contextlib.redirect_stdout(stdout):
            exit_code = entry.main(list(cli_args) + ["--timeout-seconds", "30", "--skip-mail-router"])
        summary = json.loads(stdout.getvalue())
        return exit_code, summary

    def test_shared_primitive_owns_the_real_production_bounded_selection(self) -> None:
        backend = FiveMessageBacklogBackend()

        exit_code = self._run(backend)

        self.assertEqual(exit_code, 0)
        # The primitive itself selected exactly the leading three of the
        # complete five-message backlog -- D and E were never hydrated.
        self.assertEqual(backend.detail_fetch_ids, ["msg-A", "msg-B", "msg-C"])
        self.assertEqual(backend.processed_message_ids, ["msg-A", "msg-B", "msg-C"])
        self.assertEqual(len(backend.pages), 3)

        # Natural resume: rereading canonical Gmail state (no platform
        # checkpoint) shows only D and E now eligible.
        exit_code_2 = self._run(backend)
        self.assertEqual(exit_code_2, 0)
        self.assertEqual(backend.detail_fetch_ids, ["msg-A", "msg-B", "msg-C", "msg-D", "msg-E"])
        self.assertEqual(backend.processed_message_ids, ["msg-A", "msg-B", "msg-C", "msg-D", "msg-E"])

        # Third execution against a fully drained backlog is a clean no-op:
        # no further hydration, no further Jobs pages, no further marking.
        exit_code_3 = self._run(backend)
        self.assertEqual(exit_code_3, 0)
        self.assertEqual(backend.detail_fetch_ids, ["msg-A", "msg-B", "msg-C", "msg-D", "msg-E"])
        self.assertEqual(len(backend.pages), 5)

    def test_negative_control_unsafe_batch_marks_zero_messages_processed(self) -> None:
        """B fails to parse, which under Newsletter's existing all-or-nothing
        cleanup_safe policy degrades the WHOLE selected batch -- not just B.
        This is Newsletter's real semantics, not a fabricated per-item
        A=True/B=False/C=True result."""
        backend = FiveMessageBacklogBackend(degrade="B")

        exit_code = self._run(backend)

        self.assertEqual(exit_code, 1)
        self.assertEqual(backend.detail_fetch_ids, ["msg-A", "msg-B", "msg-C"])
        # cleanup_safe was False for the whole batch: NONE of A, B, C are marked.
        self.assertEqual(backend.processed_message_ids, [])
        # D and E were never selected, so never hydrated or touched.
        self.assertNotIn("msg-D", backend.detail_fetch_ids)
        self.assertNotIn("msg-E", backend.detail_fetch_ids)

        # No source item was lost: canonical reread still shows the entire
        # uncompleted selected batch (plus D, E, never touched).
        exit_code_2 = self._run(backend)
        self.assertEqual(exit_code_2, 1)
        self.assertEqual(backend.detail_fetch_ids, ["msg-A", "msg-B", "msg-C", "msg-A", "msg-B", "msg-C"])
        self.assertEqual(backend.processed_message_ids, [])

    def test_enumeration_failure_degrades_closed_without_crashing(self) -> None:
        """Gmail backlog enumeration fails before any selection/hydration
        happens. entry.main() must not raise; it must report DEGRADED with
        a real acquisition error, exactly as NewsletterProcessor.process_window
        used to before this path called Gmail enumeration directly."""
        backend = FiveMessageBacklogBackend(fail_list=True)

        exit_code, summary = self._run_capturing_summary(backend)

        self.assertEqual(exit_code, 1)
        self.assertEqual(backend.detail_fetch_ids, [])
        self.assertEqual(backend.processed_message_ids, [])
        self.assertEqual(backend.pages, {})

        parse = summary["newsletter_parse"]
        self.assertEqual(parse["state"], "DEGRADED")
        self.assertEqual(parse["errors"], 1)
        detail = parse["error_details"][0]
        self.assertEqual(detail["mailbox"], "gmail")
        self.assertEqual(detail["operation"], "fetch")
        self.assertIn("MailboxTransportError", detail["detail"])
        self.assertNotIn("jobs", summary)

        # Canonical Gmail state is untouched -- fully resumable. Clearing
        # the injected failure and rerunning against the SAME backend
        # proves nothing was silently consumed/lost during the failure.
        backend._fail_list = False
        exit_code_2 = self._run(backend)
        self.assertEqual(exit_code_2, 0)
        self.assertEqual(backend.detail_fetch_ids, ["msg-A", "msg-B", "msg-C"])
        self.assertEqual(backend.processed_message_ids, ["msg-A", "msg-B", "msg-C"])

    def test_hydration_failure_degrades_closed_without_partial_completion(self) -> None:
        """Enumeration succeeds and a batch is selected, but hydrating one
        selected message fails. No message in the incomplete batch may be
        marked processed, and no Jobs mutation may occur."""
        backend = FiveMessageBacklogBackend(fail_detail_for="msg-B")

        exit_code, summary = self._run_capturing_summary(backend)

        self.assertEqual(exit_code, 1)
        self.assertEqual(backend.processed_message_ids, [])
        self.assertEqual(backend.pages, {})

        parse = summary["newsletter_parse"]
        self.assertEqual(parse["state"], "DEGRADED")
        self.assertEqual(parse["errors"], 1)
        detail = parse["error_details"][0]
        self.assertEqual(detail["mailbox"], "gmail")
        self.assertEqual(detail["operation"], "fetch")
        self.assertIn("MailboxTransportError", detail["detail"])
        self.assertNotIn("jobs", summary)

        # Canonical Gmail state remains resumable: nothing was marked
        # processed, so clearing the injected failure and rerunning
        # succeeds against the exact same selected batch.
        backend._fail_detail_for = None
        exit_code_2 = self._run(backend)
        self.assertEqual(exit_code_2, 0)
        self.assertEqual(backend.processed_message_ids, ["msg-A", "msg-B", "msg-C"])

    def test_empty_backlog_is_a_clean_pass_no_op(self) -> None:
        backend = FiveMessageBacklogBackend()
        backend.processed_message_ids = ["msg-A", "msg-B", "msg-C", "msg-D", "msg-E"]

        exit_code = self._run(backend)

        self.assertEqual(exit_code, 0)
        self.assertEqual(backend.detail_fetch_ids, [])
        self.assertEqual(backend.pages, {})
        self.assertEqual(backend.processed_message_ids, ["msg-A", "msg-B", "msg-C", "msg-D", "msg-E"])


if __name__ == "__main__":
    unittest.main()
