"""Read-only email responsibility; drafts remain copyable card content."""

from .gmail import GmailReadClient, GmailReadonlySource
from .service import DEFAULTS, HOOKS, EmailResponsibility, MailCardReconciler
from .sources import CursorLost, FixtureMailSource, MailBatch
from .triage import HermesMailModel, MailTriage, Triage, detect_language

__all__ = ["CursorLost", "DEFAULTS", "HOOKS", "EmailResponsibility", "FixtureMailSource",
           "GmailReadClient", "GmailReadonlySource", "HermesMailModel", "MailBatch",
           "MailCardReconciler", "MailTriage", "Triage", "detect_language"]
