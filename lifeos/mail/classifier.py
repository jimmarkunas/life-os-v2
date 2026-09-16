"""Evidence-prioritized deterministic Mail classification.

Body vocabulary is weak evidence. Automated Newsletter identity/headers and
transactional/human structure decide classification before arbitrary body words.
"""
from __future__ import annotations
from .models import Classification, MailClass, MailMessage

class DeterministicMailClassifier:
    _JOB_ALERT_SUBJECT_TERMS = (
        "job alert", "jobs for you", "recommended jobs", "recommended roles",
        "new jobs", "new roles", "daily jobs", "weekly jobs", "open roles",
    )
    _TRANSACTIONAL_HIRING_SUBJECT_TERMS = (
        "application received", "application update", "application status",
        "thank you for applying", "thanks for applying", "interview scheduling",
        "interview invitation", "schedule your interview",
    )
    _HUMAN_HIRING_TERMS = (
        "recruiter", "hiring manager", "interview", "schedule a call",
        "schedule time", "would like to speak", "next steps",
    )
    _AUTOMATED_LOCAL_PARTS = ("no-reply", "noreply", "alerts", "notifications", "jobs", "updates")
    _SOURCE_ADAPTER_HEADERS = ("x-lifeos-source-adapter", "x-lifeos-job-source")

    def classify(self, message: MailMessage) -> Classification:
        subject = message.subject.casefold()
        body = message.body_text.casefold()
        sender = message.sender.casefold()
        headers = {str(k).casefold(): str(v).casefold() for k, v in message.headers.items()}

        transactional_hits = tuple(term for term in self._TRANSACTIONAL_HIRING_SUBJECT_TERMS if term in subject)
        if transactional_hits:
            return Classification(MailClass.HUMAN_HIRING, tuple(f"transactional:{t}" for t in transactional_hits))

        automation = self._automation_evidence(sender, headers)
        source_adapter = tuple(
            f"source-adapter:{headers[name]}" for name in self._SOURCE_ADAPTER_HEADERS
            if headers.get(name, "").strip() not in {"", "0", "false", "none"}
        )
        job_subject_hits = tuple(term for term in self._JOB_ALERT_SUBJECT_TERMS if term in subject)

        if (source_adapter or job_subject_hits) and automation:
            return Classification(
                MailClass.AUTOMATED_JOB_SOURCE,
                tuple([*(f"job-subject:{t}" for t in job_subject_hits), *source_adapter, *(f"automation:{a}" for a in automation)]),
            )

        human_subject_hits = tuple(term for term in self._HUMAN_HIRING_TERMS if term in subject)
        human_body_hits = tuple(term for term in self._HUMAN_HIRING_TERMS if term in body)
        if human_subject_hits or (human_body_hits and not automation and not source_adapter):
            hits = (*human_subject_hits, *human_body_hits)
            return Classification(MailClass.HUMAN_HIRING, tuple(f"human-hiring:{t}" for t in hits))

        return Classification(MailClass.UNRELATED, ("no-confirmed-route",))

    def _automation_evidence(self, sender: str, headers: dict[str, str]) -> tuple[str, ...]:
        hits: list[str] = []
        local_part = sender.split("@", 1)[0]
        if any(token in local_part for token in self._AUTOMATED_LOCAL_PARTS):
            hits.append("sender")
        if "list-unsubscribe" in headers:
            hits.append("list-unsubscribe")
        if "list-id" in headers:
            hits.append("list-id")
        if headers.get("precedence") in {"bulk", "list"}:
            hits.append("precedence")
        if headers.get("auto-submitted", "").startswith("auto-"):
            hits.append("auto-submitted")
        if any(headers.get(name, "").strip() not in {"", "0", "false", "none"} for name in self._SOURCE_ADAPTER_HEADERS):
            hits.append("source-adapter")
        return tuple(dict.fromkeys(hits))
