"""Conservative deterministic classification for the first Mail Router slice.

The router only moves mail when both a job-alert signal and an automation signal
are present. Hiring/interview/application-status evidence has precedence so those
messages remain outside Newsletter processing.
"""

from __future__ import annotations

from .models import Classification, MailClass, MailMessage


class DeterministicMailClassifier:
    _JOB_ALERT_TERMS = (
        "job alert",
        "jobs for you",
        "recommended jobs",
        "recommended roles",
        "new jobs",
        "new roles",
        "daily jobs",
        "weekly jobs",
        "open roles",
    )
    _HIRING_TERMS = (
        "recruiter",
        "hiring manager",
        "interview",
        "schedule a call",
        "schedule time",
        "would like to speak",
        "next steps",
        "application received",
        "application update",
        "application status",
        "thank you for applying",
        "thanks for applying",
    )
    _AUTOMATED_LOCAL_PARTS = (
        "no-reply",
        "noreply",
        "alerts",
        "notifications",
        "jobs",
        "updates",
    )

    def classify(self, message: MailMessage) -> Classification:
        subject = message.subject.casefold()
        body = message.body_text.casefold()
        text = f"{subject}\n{body}"
        sender = message.sender.casefold()
        headers = {str(k).casefold(): str(v).casefold() for k, v in message.headers.items()}

        hiring_hits = tuple(term for term in self._HIRING_TERMS if term in text)
        if hiring_hits:
            return Classification(
                mail_class=MailClass.HUMAN_HIRING,
                reasons=tuple(f"hiring:{term}" for term in hiring_hits),
            )

        job_hits = tuple(term for term in self._JOB_ALERT_TERMS if term in text)
        automation_hits: list[str] = []
        local_part = sender.split("@", 1)[0]
        if any(token in local_part for token in self._AUTOMATED_LOCAL_PARTS):
            automation_hits.append("sender")
        if "list-unsubscribe" in headers:
            automation_hits.append("list-unsubscribe")
        if headers.get("precedence") in {"bulk", "list"}:
            automation_hits.append("precedence")

        if job_hits and automation_hits:
            return Classification(
                mail_class=MailClass.AUTOMATED_JOB_SOURCE,
                reasons=tuple(
                    [*(f"job:{term}" for term in job_hits), *(f"automation:{hit}" for hit in automation_hits)]
                ),
            )

        return Classification(mail_class=MailClass.UNRELATED, reasons=("no-confirmed-route",))
