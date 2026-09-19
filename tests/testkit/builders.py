from lifeos.core.runtime import RunContext
from lifeos.integrations.gmail import GmailMailboxTransport

def gmail_mailbox(http) -> GmailMailboxTransport:
    return GmailMailboxTransport(context=RunContext.start(timeout_seconds=45), http=http, access_token="synthetic-token", message_factory=lambda **kwargs: kwargs)
