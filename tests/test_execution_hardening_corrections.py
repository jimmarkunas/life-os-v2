from __future__ import annotations

from concurrent.futures import Future
from contextlib import contextmanager

from lifeos.core.http import HttpClient, HttpResponse
from lifeos.core.runtime import RunContext
from lifeos.jobs import newsletter_feature
from lifeos.newsletter.models import SourceVacancyObservation
from lifeos.newsletter.processor import NewsletterExecutionState, NewsletterProcessResult, NewsletterTimings


def test_http_concurrency_is_hard_capped_at_eight():
    context = RunContext.start(timeout_seconds=10, http_concurrency=100)
    assert context._http_permits._value == 8


def test_http_backend_timeout_is_computed_after_permit_acquisition():
    calls: list[str] = []

    class Context:
        @contextmanager
        def http_permit(self):
            calls.append("permit")
            yield

        def bounded_timeout(self, requested):
            calls.append("timeout")
            return min(float(requested), 3.0)

    class Backend:
        def request(self, method, url, *, headers, body, timeout_seconds):
            calls.append(f"backend:{timeout_seconds}")
            return HttpResponse(200, {}, b"{}")

    HttpClient(Backend()).request(Context(), "GET", "https://example.invalid", timeout_seconds=10)
    assert calls == ["permit", "timeout", "backend:3.0"]


def _observation(index: int) -> SourceVacancyObservation:
    return SourceVacancyObservation(
        evidence_ref=f"ev:{index}",
        source_provider="synthetic",
        source_mailbox="INBOX",
        source_message_id=f"msg:{index}",
        source_subject="Synthetic",
        company="Synthetic Co",
        role="Synthetic Engineer",
        location_text="Remote",
        compensation_text=None,
        source_apply_url=f"https://example.invalid/jobs/{index}",
        provider_job_id=str(index),
        provider_score=1,
    )


def test_jobs_adaptation_submits_only_worker_sized_chunks(monkeypatch):
    submitted_batches: list[int] = []
    current_submissions = 0

    class FakeExecutor:
        def __init__(self, *, max_workers):
            assert max_workers == 8

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn, *args):
            nonlocal current_submissions
            current_submissions += 1
            future = Future()
            try:
                fn(*args)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(None)
            return future

    def fake_as_completed(futures):
        nonlocal current_submissions
        submitted_batches.append(current_submissions)
        current_submissions = 0
        return list(futures)

    class FakeAdapter:
        def to_jobs_candidate(self, observation):
            return observation

    monkeypatch.setattr(newsletter_feature, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(newsletter_feature, "as_completed", fake_as_completed)

    observations = tuple(_observation(i) for i in range(20))
    results = newsletter_feature._adapt_all(
        observations,
        adapter=FakeAdapter(),
        context=RunContext.start(timeout_seconds=10),
        max_workers=100,
    )

    assert len(results) == 20
    assert submitted_batches == [8, 8, 4]


def test_feature_passes_run_context_into_ingest(monkeypatch):
    captured = {}

    def fake_ingest(candidates, **kwargs):
        captured["context"] = kwargs.get("context")
        return []

    monkeypatch.setattr(newsletter_feature, "ingest", fake_ingest)
    context = RunContext.start(timeout_seconds=10)
    process_result = NewsletterProcessResult(
        state=NewsletterExecutionState.PASS,
        messages=(),
        errors=(),
        timings=NewsletterTimings(0.0, 0.0, 0.0),
    )

    result = newsletter_feature.run_newsletter_feature(
        process_result,
        adapter=object(),
        lane=object(),
        lane_priority={},
        repository=object(),
        run_date=None,
        context=context,
    )

    assert captured["context"] is context
    assert result.cleanup_safe is True
