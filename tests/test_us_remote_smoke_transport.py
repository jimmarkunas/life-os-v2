from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "life-os-production-runtime.yml"
SMOKE = Path(__file__).resolve().parents[1] / "scripts" / "smoke_us_remote_batch.py"


def test_us_remote_smoke_is_10x10_and_read_only() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    smoke = text.split("us-remote-smoke)", 1)[1].split(";;", 1)[0]
    script = SMOKE.read_text(encoding="utf-8")

    assert 'default_window="1"' in smoke
    assert 'web_lookback_hours="24"' in smoke
    assert 'full_web_sweep="false"' in smoke
    assert 'dry_run="true"' in smoke
    assert "scripts/smoke_us_remote_batch.py" in text
    assert "--newsletter-messages 10" in text
    assert "--web-candidates 10" in text
    assert "--timeout-seconds 45" in text
    assert "MailRouter" not in script
    assert "ingest(" not in script
    assert "mark_newsletter_processed" not in script
    assert '"writes": 0' in script
