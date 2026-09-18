from datetime import datetime, timezone

from lifeos.mail.classifier import DeterministicMailClassifier
from lifeos.mail.models import MailClass, MailMessage


def _message(subject: str, *, automated: bool) -> MailMessage:
    return MailMessage(
        provider="gmail",
        message_id="synthetic-linkedin-alert",
        received_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
        sender="jobs-noreply@example.invalid" if automated else "person@example.invalid",
        subject=subject,
        headers={"List-Unsubscribe": "<https://example.invalid/unsubscribe>"} if automated else {},
    )


def test_automated_is_hiring_subject_routes_as_job_source() -> None:
    result = DeterministicMailClassifier().classify(
        _message("Example Systems is hiring a Senior Program Manager", automated=True)
    )
    assert result.mail_class is MailClass.AUTOMATED_JOB_SOURCE


def test_automated_jobs_in_subject_routes_as_job_source() -> None:
    result = DeterministicMailClassifier().classify(
        _message("Senior Project Manager jobs in London", automated=True)
    )
    assert result.mail_class is MailClass.AUTOMATED_JOB_SOURCE


def test_human_is_hiring_subject_does_not_route_as_automated() -> None:
    result = DeterministicMailClassifier().classify(
        _message("Example Systems is hiring a Senior Program Manager", automated=False)
    )
    assert result.mail_class is not MailClass.AUTOMATED_JOB_SOURCE
