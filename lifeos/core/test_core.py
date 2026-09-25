from __future__ import annotations

import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from time import sleep
from datetime import datetime, timezone

from lifeos.core.runtime import (
    DeadlineExceeded,
    ExecutionResult,
    ExecutionStatus,
    MAX_RUNTIME_SECONDS,
    RunContext,
)
from lifeos.core.http import HttpClient, HttpResponse


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class RunContextTests(unittest.TestCase):
    def test_bounded_timeout_never_exceeds_remaining_budget(self) -> None:
        clock = FakeClock()
        context = RunContext.start(timeout_seconds=10, monotonic_clock=clock)
        clock.value += 7
        self.assertAlmostEqual(context.bounded_timeout(8), 3.0)

        class BlockingBackend:
            def __init__(self):
                self.active = 0
                self.peak = 0
                self.lock = threading.Lock()

            def request(self, method, url, *, headers, body, timeout_seconds):
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                sleep(0.03)
                with self.lock:
                    self.active -= 1
                return HttpResponse(200, {}, b"ok")

        def measure(context):
            backend = BlockingBackend()
            client = HttpClient(backend)
            with ThreadPoolExecutor(max_workers=18) as pool:
                list(pool.map(lambda _: client.request(context, "GET", "https://example.invalid"), range(18)))
            return backend.peak

        self.assertEqual(measure(RunContext.start(timeout_seconds=30)), 8)
        self.assertEqual(measure(RunContext.start(timeout_seconds=30, http_concurrency=18)), 18)

    def test_deadline_budget_and_hard_max(self) -> None:
        clock = FakeClock()
        context = RunContext.start(
            timeout_seconds=45,
            run_id="synthetic-run",
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            monotonic_clock=clock,
        )
        self.assertEqual(context.run_id, "synthetic-run")
        self.assertAlmostEqual(context.remaining_seconds(), 45.0)
        clock.value += 12.5
        self.assertAlmostEqual(context.remaining_seconds(), 32.5)
        self.assertFalse(context.expired())
        clock.value += 32.5
        self.assertTrue(context.expired())
        with self.assertRaises(DeadlineExceeded):
            context.require_time()
        with self.assertRaises(ValueError):
            RunContext.start(timeout_seconds=MAX_RUNTIME_SECONDS + 0.1)

    def test_terminal_result_shape_is_small_and_structured(self) -> None:
        passed = ExecutionResult.passed(7, count=2)
        self.assertEqual(passed.status, ExecutionStatus.PASS)
        self.assertEqual(passed.value, 7)
        degraded = ExecutionResult.degraded(code="timeout", affected=3)
        self.assertEqual(degraded.status, ExecutionStatus.DEGRADED)
        failed = ExecutionResult.failed(code="config", raw={"not": "serialized"})
        self.assertEqual(failed.status, ExecutionStatus.FAILED)
        self.assertEqual(failed.detail["raw"], "dict")


from lifeos.core.config import ConfigField, ConfigurationError, RuntimeConfig


class RuntimeConfigTests(unittest.TestCase):
    def test_loads_only_declared_runtime_values_and_hides_values(self) -> None:
        config = RuntimeConfig.load(
            (ConfigField("SYNTHETIC_TOKEN"), ConfigField("OPTIONAL_ID", required=False)),
            environ={"SYNTHETIC_TOKEN": "private-runtime-value", "UNDECLARED": "ignored"},
        )
        self.assertEqual(config.require("SYNTHETIC_TOKEN"), "private-runtime-value")
        self.assertIsNone(config.optional("OPTIONAL_ID"))
        self.assertNotIn("private-runtime-value", repr(config))
        self.assertNotIn("UNDECLARED", repr(config))

    def test_missing_required_key_fails_without_value_material(self) -> None:
        with self.assertRaises(ConfigurationError) as caught:
            RuntimeConfig.load((ConfigField("SYNTHETIC_TOKEN"),), environ={})
        self.assertIn("SYNTHETIC_TOKEN", str(caught.exception))

    def test_duplicate_schema_name_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            RuntimeConfig.load((ConfigField("A"), ConfigField("A")), environ={"A": "x"})


import socket
from unittest.mock import patch

from lifeos.core.http import HttpClient, HttpError, HttpErrorKind, HttpResponse, RetryPolicy


class HttpFakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class QueueBackend:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def request(self, method, url, *, headers, body, timeout_seconds):
        self.calls.append((method, url, dict(headers), body, timeout_seconds))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class HttpClientTests(unittest.TestCase):
    def test_timeout_is_clamped_to_run_deadline(self) -> None:
        clock = HttpFakeClock()
        context = RunContext.start(timeout_seconds=5, monotonic_clock=clock)
        clock.value = 3
        backend = QueueBackend([HttpResponse(200, {}, b"{}")])
        client = HttpClient(backend)
        client.request(context, "GET", "https://example.invalid", timeout_seconds=30)
        self.assertAlmostEqual(backend.calls[0][-1], 2.0)

    def test_bounded_retry_on_rate_limit_then_success(self) -> None:
        context = RunContext.start(timeout_seconds=30)
        backend = QueueBackend([
            HttpResponse(429, {"Retry-After": "0"}, b"{}"),
            HttpResponse(200, {}, b'{"ok":true}'),
        ])
        client = HttpClient(backend)
        result = client.request_json(
            context,
            "GET",
            "https://example.invalid/resource",
            retry=RetryPolicy(max_attempts=2, backoff_seconds=0),
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(backend.calls), 2)

    def test_default_does_not_retry_writes(self) -> None:
        context = RunContext.start(timeout_seconds=30)
        backend = QueueBackend([HttpResponse(503, {}, b"unavailable")])
        client = HttpClient(backend)
        with self.assertRaises(HttpError) as caught:
            client.request(context, "POST", "https://example.invalid", json_body={"x": 1})
        self.assertEqual(caught.exception.kind, HttpErrorKind.HTTP_STATUS)
        self.assertEqual(len(backend.calls), 1)

    def test_normalized_error_does_not_leak_url_token_or_body(self) -> None:
        context = RunContext.start(timeout_seconds=30)
        backend = QueueBackend([socket.timeout("sensitive-network-detail")])
        client = HttpClient(backend)
        with self.assertRaises(HttpError) as caught:
            client.request(
                context,
                "GET",
                "https://example.invalid/private/resource",
                headers={"Authorization": "Bearer synthetic-secret"},
            )
        text = str(caught.exception)
        self.assertNotIn("synthetic-secret", text)
        self.assertNotIn("example.invalid", text)
        self.assertEqual(caught.exception.kind, HttpErrorKind.TIMEOUT)

    def test_invalid_json_is_normalized(self) -> None:
        context = RunContext.start(timeout_seconds=30)
        backend = QueueBackend([HttpResponse(200, {}, b"not-json")])
        client = HttpClient(backend)
        with self.assertRaises(HttpError) as caught:
            client.request_json(context, "GET", "https://example.invalid")
        self.assertEqual(caught.exception.kind, HttpErrorKind.INVALID_RESPONSE)

    def test_backoff_without_retry_after_adds_jitter(self) -> None:
        context = RunContext.start(timeout_seconds=30)
        backend = QueueBackend([
            HttpResponse(503, {}, b"unavailable"),
            HttpResponse(200, {}, b'{"ok":true}'),
        ])
        client = HttpClient(backend)
        with patch("lifeos.core.http.random.random", return_value=1.0), patch("lifeos.core.http.sleep") as sleep_mock:
            result = client.request_json(
                context,
                "GET",
                "https://example.invalid/resource",
                retry=RetryPolicy(max_attempts=2, backoff_seconds=1.0, max_backoff_seconds=10.0),
            )
        self.assertEqual(result, {"ok": True})
        sleep_mock.assert_called_once_with(2.0)  # base 1.0 * 2**0 + jitter 1.0 * 1.0

    def test_backoff_jitter_is_capped_by_max_backoff(self) -> None:
        context = RunContext.start(timeout_seconds=30)
        backend = QueueBackend([
            HttpResponse(503, {}, b"unavailable"),
            HttpResponse(200, {}, b'{"ok":true}'),
        ])
        client = HttpClient(backend)
        with patch("lifeos.core.http.random.random", return_value=1.0), patch("lifeos.core.http.sleep") as sleep_mock:
            client.request_json(
                context,
                "GET",
                "https://example.invalid/resource",
                retry=RetryPolicy(max_attempts=2, backoff_seconds=1.0, max_backoff_seconds=1.5),
            )
        sleep_mock.assert_called_once_with(1.5)

    def test_retry_after_header_is_not_jittered(self) -> None:
        context = RunContext.start(timeout_seconds=30)
        backend = QueueBackend([
            HttpResponse(429, {"Retry-After": "10"}, b"{}"),
            HttpResponse(200, {}, b'{"ok":true}'),
        ])
        client = HttpClient(backend)
        with patch("lifeos.core.http.random.random", return_value=1.0), patch("lifeos.core.http.sleep") as sleep_mock:
            client.request_json(
                context,
                "GET",
                "https://example.invalid/resource",
                retry=RetryPolicy(max_attempts=2, backoff_seconds=1.0, max_backoff_seconds=1.0),
            )
        sleep_mock.assert_called_once_with(10.0)
        with self.assertRaises(HttpError) as caught:
            HttpClient(QueueBackend([HttpResponse(429, {"Retry-After": "10"}, b"{}")])).request(
                RunContext.start(timeout_seconds=5), "GET", "https://example.invalid/resource",
                retry=RetryPolicy(max_attempts=2, max_backoff_seconds=1.0),
            )
        self.assertEqual(caught.exception.kind, HttpErrorKind.DEADLINE)
