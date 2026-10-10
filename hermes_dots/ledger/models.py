"""Small records exchanged with engine, inbox, and Slack adapters."""

from dataclasses import dataclass, field
from typing import Any

from .enums import BlockedReason, IdeaState, MailCardState, OutboxState, TaskState, TriageCategory


@dataclass(frozen=True)
class Card:
    id: str
    goal: str
    sources: tuple[str, ...] = ()
    allowed_actions: tuple[str, ...] = ()
    approval_points: tuple[str, ...] = ()
    trigger: str = "on_request"
    end_date: str | None = None
    outputs: tuple[str, ...] = ()
    notify_rules: tuple[str, ...] = ()
    done_evidence: tuple[str, ...] = ()
    status: TaskState = TaskState.NEW
    revision: int = 1


@dataclass(frozen=True)
class Task:
    id: str
    state: TaskState
    card_revision: int
    blocked_reason: BlockedReason | None = None
    interrupted: bool = False


@dataclass(frozen=True)
class CheckBack:
    id: str
    task_id: str
    due_at: str
    reason: str


@dataclass(frozen=True)
class Question:
    id: str
    task_id: str
    text: str
    recommendation: str
    asked_at: str
    answer: str | None = None


@dataclass(frozen=True)
class Approval:
    id: str
    task_id: str
    action: str
    target: str
    content_hash: str
    card_revision: int
    used: bool = False


@dataclass(frozen=True)
class MailItem:
    account: str
    message_id: str
    thread_id: str
    category: TriageCategory
    reason: str
    needs_reply: bool = False
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MailCursor:
    account: str
    cursor: str


@dataclass(frozen=True)
class MailCard:
    id: str
    account: str
    thread_id: str
    state: MailCardState = MailCardState.OPEN
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Idea:
    id: str
    mail_card_id: str
    reason: str
    sources: tuple[str, ...]
    expires_at: str
    state: IdeaState = IdeaState.NEW
    task_id: str | None = None
    snoozed_until: str | None = None


@dataclass(frozen=True)
class OutboxEffect:
    delivery_id: str
    idempotency_key: str
    channel: str
    payload: dict[str, Any]
    state: OutboxState = OutboxState.PENDING
    receipt: str | None = None
    task_id: str | None = None


@dataclass(frozen=True)
class MissedRun:
    id: str
    task_id: str
    job_id: str
    scheduled_at: str
    reason: str


@dataclass(frozen=True)
class Event:
    id: int
    kind: str
    payload: dict[str, Any]
    task_id: str | None
    created_at: str


@dataclass(frozen=True)
class RecoveryReport:
    interrupted_task_ids: tuple[str, ...]
    unknown_delivery_ids: tuple[str, ...]
    pending_delivery_ids: tuple[str, ...]
