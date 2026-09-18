"""Evidence-prioritized deterministic classification for the Mail Router.

Body vocabulary is weak evidence. Automated Newsletter identity/headers and
transactional/human structure decide classification before arbitrary body words.
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
        "is hiring",
        " jobs in ",
    )
    _TRANSACTIONAL_HIRING_SUBJECT_TERMS = (
        "application received",
        "application update",
        "application status",
        "thank you for applying",
        "thanks for applying",
        "interview scheduling",
        "interview invitation",
        "schedule your interview",
    )
    _HIRING_TERMS = (
        "recruiter",
        "hiring manager",
        "interview",
        "schedule a call",
        "schedule time",
        "would like to speak",
        "next steps",
    )
    _AUTOMATED_LOCAL_PARTS = (
        "no-reply",
        "noreply",
        "alerts",
        "notifications",
        "jobs",
        "updates",
    )
    _SOURCE_ADAPTER_HEADERS = ("x-lifeos-source-adapter", "x-lifeos-job-source")

    def classify(self, message: MailMessage) -> Classification:
        subject = message.subject.casefold()
        body = message.body_text.casefold()
        sender = message.sender.casefold()
        headers = {str(k).casefold(): str(v).casefold() for k, v in message.headers.items()}

        transactional_hits = tuple(
            term for term in self._TRANSACTIONAL_HIRING_SUBJECT_TERMS if term in subject
        )
        if transactional_hits:
            return Classification(
                mail_class=MailClass.HUMAN_HIRING,
                reasons=tuple(f"transactional:{term}" for term in transactional_hits),
            )

        automation_hits: list[str] = []
        local_part = sender.split("@", 1)[0]
        if any(token in local_part for token in self._AUTOMATED_LOCAL_PARTS):
            automation_hits.append("sender")
        if "list-unsubscribe" in headers:
            automation_hits.append("list-unsubscribe")
        if "list-id" in headers:
            automation_hits.append("list-id")
        if headers.get("precedence") in {"bulk", "list"}:
            automation_hits.append("precedence")
        if headers.get("auto-submitted", "").startswith("auto-"):
            automation_hits.append("auto-submitted")

        source_adapter_hits = tuple(
            headers[name]
            for name in self._SOURCE_ADAPTER_HEADERS
            if headers.get(name, "").strip() not in {"", "0", "false", "none"}
        )
        if source_adapter_hits:
            automation_hits.append("source-adapter")

        job_hits = tuple(term for term in self._JOB_ALERT_TERMS if term in subject)
        if (job_hits or source_adapter_hits) and automation_hits:
            return Classification(
                mail_class=MailClass.AUTOMATED_JOB_SOURCE,
                reasons=tuple(
                    [
                        *(f"job:{term}" for term in job_hits),
                        *(f"source-adapter:{value}" for value in source_adapter_hits),
                        *(f"automation:{hit}" for hit in dict.fromkeys(automation_hits)),
                    ]
                ),
            )

        human_subject_hits = tuple(term for term in self._HIRING_TERMS if term in subject)
        human_body_hits = tuple(term for term in self._HIRING_TERMS if term in body)
        if human_subject_hits or (human_body_hits and not automation_hits and not source_adapter_hits):
            return Classification(
                mail_class=MailClass.HUMAN_HIRING,
                reasons=tuple(
                    f"hiring:{term}" for term in (*human_subject_hits, *human_body_hits)
                ),
            )

        return Classification(mail_class=MailClass.UNRELATED, reasons=("no-confirmed-route",))
