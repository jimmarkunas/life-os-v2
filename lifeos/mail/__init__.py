"""Mail Intelligence domain: mailbox classification and routing."""
from .classifier import DeterministicMailClassifier
from .models import MailClass, MailMessage, MailRef
from .router import MailExecutionState, MailRouter, MailRouteResult, MailboxPort, ProviderScan
__all__ = ["DeterministicMailClassifier", "MailClass", "MailMessage", "MailRef", "MailExecutionState", "MailRouter", "MailRouteResult", "MailboxPort", "ProviderScan"]
