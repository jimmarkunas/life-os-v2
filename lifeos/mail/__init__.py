"""Mail Intelligence domain: mailbox classification and routing."""

from .classifier import DeterministicMailClassifier
from .models import MailClass, MailMessage, MailRef
from .router import MailRouter, MailRouteResult, MailboxPort

__all__ = [
    "DeterministicMailClassifier",
    "MailClass",
    "MailMessage",
    "MailRef",
    "MailRouter",
    "MailRouteResult",
    "MailboxPort",
]
