from __future__ import annotations

import base64
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from lifeos.core.http import HttpClient, HttpResponse
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
    def test_require_env_lists_missing_names_never_values(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(entry.ProductionConfigError) as caught:
                entry._require_env()
        message = str(caught.exception)
        for name in entry.REQUIRED_ENV:
            self.assertIn(name, message)

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

    def test_missing_policy_file_fails_closed(self) -> None:
        with self.assertRaises(entry.ProductionConfigError):
            entry._load_private_policy("/nonexistent/synthetic/path.json")

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

    def test_dry_run_never_routes_or_persists(self) -> None:
        exit_code, backend = self._run("--dry-run", "--timeout-seconds", "30")
        self.assertEqual(exit_code, 0)
        self.assertEqual(backend.routed_message_ids, [])
        self.assertEqual(backend.processed_message_ids, [])
        self.assertEqual(backend.pages, {})

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


if __name__ == "__main__":
    unittest.main()
