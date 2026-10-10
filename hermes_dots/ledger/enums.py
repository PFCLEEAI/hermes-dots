"""Persisted vocabulary from the core spec and T2 delivery contract."""

from enum import StrEnum


class TaskState(StrEnum):
    NEW = "new"
    WORKING = "working"
    WAITING_ON_OWNER = "waiting_on_owner"
    PAUSED = "paused"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


class BlockedReason(StrEnum):
    MODEL_LIMIT = "model_limit"
    HEALTH = "health"
    PERMISSION = "permission"


class MailCardState(StrEnum):
    OPEN = "open"
    REPLIED_STILL_OPEN = "replied_still_open"
    CLOSED = "closed"


class IdeaState(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    SNOOZED = "snoozed"
    EXPIRED = "expired"


class TriageCategory(StrEnum):
    URGENT = "urgent"
    REGULAR = "regular"
    NOISE = "noise"
    UNCERTAIN = "uncertain"


class OutboxState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"
