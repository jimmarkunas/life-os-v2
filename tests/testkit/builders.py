from lifeos.core.runtime import RunContext
from lifeos.integrations.gmail import GmailMailboxTransport
from datetime import datetime, timezone
from lifeos.mail import MailMessage

def gmail_mailbox(http) -> GmailMailboxTransport:
    return GmailMailboxTransport(context=RunContext.start(timeout_seconds=45), http=http, access_token="synthetic-token", message_factory=lambda **kwargs: kwargs)

def mail_message(provider, message_id, subject, *, sender, body="", headers=None, minute=0):
    return MailMessage(provider=provider, message_id=message_id, received_at=datetime(2026, 1, 15, 12, minute, tzinfo=timezone.utc), sender=sender, subject=subject, body_text=body, headers=headers or {})
