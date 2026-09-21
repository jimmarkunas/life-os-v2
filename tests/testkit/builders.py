from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Mapping

from lifeos.core.runtime import RunContext
from lifeos.integrations.gmail import GmailMailboxTransport
from lifeos.mail import MailMessage

@dataclass(frozen=True)
class SyntheticMessage:
    provider: str
    message_id: str
    received_at: datetime
    sender: str
    subject: str
    body_text: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)


def gmail_mailbox(http, *, context=None, message_factory=SyntheticMessage, max_workers=None, max_list_pages=None) -> GmailMailboxTransport:
    options = {"context": context or RunContext.start(timeout_seconds=45), "http": http, "access_token": "synthetic-token", "message_factory": message_factory}
    if max_workers is not None:
        options["max_workers"] = max_workers
    if max_list_pages is not None:
        options["max_list_pages"] = max_list_pages
    return GmailMailboxTransport(**options)

def mail_message(provider, message_id, subject, *, sender, body="", headers=None, minute=0, received_at=None):
    return MailMessage(provider=provider, message_id=message_id, received_at=received_at or datetime(2026, 1, 15, 12, minute, tzinfo=timezone.utc), sender=sender, subject=subject, body_text=body, headers=headers or {})

def gmail_message(message_id, subject, *, sender, body="", headers=None, received_at=None):
    return mail_message("gmail", message_id, subject, sender=sender, body=body, headers=headers, received_at=received_at or datetime(2026, 1, 1, tzinfo=timezone.utc))
