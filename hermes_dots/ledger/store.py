"""SQLite authority and transactional outbox; no provider calls live here."""

from contextlib import contextmanager
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Iterator, Protocol

from .enums import BlockedReason, IdeaState, MailCardState, OutboxState, TaskState, TriageCategory
from .models import (
    Approval, Card, CheckBack, Event, Idea, MailCard, MailCursor, MailItem,
    MissedRun, OutboxEffect, Question, RecoveryReport, Task,
)


class IllegalTransition(ValueError):
    """A persisted state cannot move to the requested state."""


class IdempotencyConflict(ValueError):
    """An existing delivery key was reused for different content or target."""


class DeliveryReconciler(Protocol):
    """T6 searches channel history by delivery_id before any uncertain resend.

    find_delivery returns the provider receipt if found, None only after a
    successful search proving absence, and raises when the check cannot run.
    send must attach delivery.delivery_id to the provider message metadata.
    """

    def find_delivery(self, delivery: OutboxEffect) -> str | None: ...

    def send(self, delivery: OutboxEffect) -> str: ...


_TASK_TRANSITIONS = {
    TaskState.NEW: {TaskState.WORKING, TaskState.WAITING_ON_OWNER, TaskState.PAUSED, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.WORKING: {TaskState.WAITING_ON_OWNER, TaskState.PAUSED, TaskState.BLOCKED, TaskState.DONE, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.WAITING_ON_OWNER: {TaskState.WORKING, TaskState.PAUSED, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.PAUSED: {TaskState.WORKING, TaskState.WAITING_ON_OWNER, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.BLOCKED: {TaskState.WORKING, TaskState.WAITING_ON_OWNER, TaskState.PAUSED, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.DONE: {TaskState.WORKING},
    TaskState.CANCELLED: set(),
    TaskState.FAILED: {TaskState.WORKING, TaskState.CANCELLED},
}
_MAIL_TRANSITIONS = {
    MailCardState.OPEN: {MailCardState.REPLIED_STILL_OPEN, MailCardState.CLOSED},
    MailCardState.REPLIED_STILL_OPEN: {MailCardState.CLOSED},
    MailCardState.CLOSED: {MailCardState.OPEN},
}
_IDEA_TRANSITIONS = {
    IdeaState.NEW: {IdeaState.ACCEPTED, IdeaState.DISMISSED, IdeaState.SNOOZED, IdeaState.EXPIRED},
    IdeaState.SNOOZED: {IdeaState.NEW, IdeaState.ACCEPTED, IdeaState.DISMISSED, IdeaState.EXPIRED},
    IdeaState.ACCEPTED: set(), IdeaState.DISMISSED: set(), IdeaState.EXPIRED: set(),
}
_OUTBOX_TRANSITIONS = {
    OutboxState.PENDING: {OutboxState.SENDING, OutboxState.FAILED},
    OutboxState.SENDING: {OutboxState.SENT, OutboxState.FAILED, OutboxState.UNKNOWN},
    OutboxState.SENT: set(),
    OutboxState.FAILED: {OutboxState.PENDING},
    OutboxState.UNKNOWN: set(),  # Only reconciliation can resolve uncertainty.
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _check_transition(current: str, target: str, allowed: dict) -> None:
    if target not in allowed[current]:
        raise IllegalTransition(f"Illegal transition: {current} -> {target}")


class Ledger:
    """One serialized connection, with nested transactions via savepoints.

    The caller supplies a database path in its isolated data root. Calls can
    be grouped with transaction() so a polling batch commits before its cursor.
    recover_on_start() is called once by the runtime before starting workers.
    """

    def __init__(self, path: str | Path, *, migrations_dir: Path | None = None):
        self._lock = RLock()
        self._savepoint = 0
        self.connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        try:
            self.apply_migrations(migrations_dir or Path(__file__).with_name("migrations"))
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self, *, durable: bool = False) -> Iterator["Ledger"]:
        with self._lock:
            nested = self.connection.in_transaction
            if durable and nested:
                raise RuntimeError("Delivery requires a committed intake transaction")
            self._savepoint += 1
            name = f"ledger_{self._savepoint}"
            self.connection.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
            try:
                yield self
                self.connection.execute(f"RELEASE SAVEPOINT {name}" if nested else "COMMIT")
            except BaseException:
                if nested:
                    self.connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    self.connection.execute(f"RELEASE SAVEPOINT {name}")
                else:
                    self.connection.rollback()
                raise

    def apply_migrations(self, directory: Path) -> tuple[str, ...]:
        """Apply *.sql in filename order, recording each name atomically.

        execute(), rather than executescript(), preserves the transaction that
        covers both the schema change and its applied-name record.
        """
        applied = []
        with self.transaction():
            self.connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
            for path in sorted(directory.glob("*.sql")):
                if self.connection.execute("SELECT 1 FROM schema_migrations WHERE name=?", (path.name,)).fetchone():
                    continue
                statement = ""
                for char in path.read_text(encoding="utf-8"):
                    statement += char
                    if char == ";" and sqlite3.complete_statement(statement):
                        self.connection.execute(statement)
                        statement = ""
                if statement.strip():
                    # Also supports a final statement without a semicolon.
                    self.connection.execute(statement)
                self.connection.execute("INSERT INTO schema_migrations(name) VALUES (?)", (path.name,))
                applied.append(path.name)
        return tuple(applied)

    def _row(self, table: str, key: str, value: object) -> sqlite3.Row:
        # Table/key identifiers below are constants controlled by this module.
        with self._lock:
            row = self.connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
        if row is None:
            raise KeyError(value)
        return row

    def create_task(self, card: Card) -> Task:
        if card.revision != 1 or TaskState(card.status) != TaskState.NEW:
            raise ValueError("A task starts with revision 1 in state new")
        self._validate_card(card)
        with self.transaction():
            self.connection.execute("INSERT INTO tasks(id,state,card_revision) VALUES (?,?,?)", (card.id, TaskState.NEW, 1))
            self.connection.execute("INSERT INTO cards VALUES (?,?,?)", (card.id, 1, _json(asdict(card))))
            self.record_event("task_created", {"revision": 1}, task_id=card.id)
        return self.get_task(card.id)

    @staticmethod
    def _validate_card(card: Card) -> None:
        if card.trigger not in {"on_request", "schedule", "event"} or card.revision < 1:
            raise ValueError("Invalid card trigger or revision")

    def get_task(self, task_id: str) -> Task:
        row = self._row("tasks", "id", task_id)
        return Task(row["id"], TaskState(row["state"]), row["card_revision"],
                    BlockedReason(row["blocked_reason"]) if row["blocked_reason"] else None,
                    bool(row["interrupted"]))

    def list_tasks(self) -> tuple[Task, ...]:
        with self._lock:
            return tuple(self.get_task(row[0]) for row in self.connection.execute("SELECT id FROM tasks ORDER BY id"))

    def get_card(self, task_id: str, revision: int | None = None) -> Card:
        with self._lock:
            if revision is None:
                revision = self.get_task(task_id).card_revision
            row = self.connection.execute("SELECT contract FROM cards WHERE task_id=? AND revision=?", (task_id, revision)).fetchone()
            if row is None:
                raise KeyError((task_id, revision))
            data = json.loads(row[0])
        for key in ("sources", "allowed_actions", "approval_points", "outputs", "notify_rules", "done_evidence"):
            data[key] = tuple(data[key])
        data["status"] = TaskState(data["status"])
        return Card(**data)

    def revise_card(self, card: Card) -> Card:
        self._validate_card(card)
        with self.transaction():
            task = self.get_task(card.id)
            if card.revision != task.card_revision + 1 or TaskState(card.status) != task.state:
                raise ValueError("Revision must advance by one and retain the task state")
            self.connection.execute("INSERT INTO cards VALUES (?,?,?)", (card.id, card.revision, _json(asdict(card))))
            self.connection.execute("UPDATE tasks SET card_revision=? WHERE id=?", (card.revision, card.id))
            self.record_event("card_revised", {"revision": card.revision}, task_id=card.id)
        return self.get_card(card.id)

    def transition_task(self, task_id: str, state: TaskState, *, blocked_reason: BlockedReason | None = None,
                        done_evidence: tuple[str, ...] | None = None) -> Task:
        state = TaskState(state)
        reason = BlockedReason(blocked_reason) if blocked_reason is not None else None
        if (state == TaskState.BLOCKED) != (reason is not None):
            raise ValueError("Only blocked tasks require a blocked reason")
        with self.transaction():
            task = self.get_task(task_id)
            _check_transition(task.state, state, _TASK_TRANSITIONS)
            card = self.get_card(task_id)
            evidence = card.done_evidence if done_evidence is None else done_evidence
            if state == TaskState.DONE and not evidence:
                raise ValueError("Done requires evidence")
            self.connection.execute("UPDATE tasks SET state=?,blocked_reason=?,interrupted=0 WHERE id=?", (state, reason, task_id))
            card = replace(card, status=state, done_evidence=evidence)
            self.connection.execute("UPDATE cards SET contract=? WHERE task_id=? AND revision=?", (_json(asdict(card)), task_id, card.revision))
            self.record_event("task_transition", {"from": task.state, "to": state}, task_id=task_id)
        return self.get_task(task_id)

    def add_check_back(self, check_back: CheckBack) -> None:
        with self.transaction():
            self.connection.execute("INSERT INTO check_backs VALUES (?,?,?,?)", tuple(asdict(check_back).values()))

    def add_question(self, question: Question) -> None:
        with self.transaction():
            self.connection.execute("INSERT INTO questions VALUES (?,?,?,?,?,?)", tuple(asdict(question).values()))

    def add_approval(self, approval: Approval) -> None:
        with self.transaction():
            if approval.card_revision != self.get_task(approval.task_id).card_revision:
                raise ValueError("Approval must reference the current card revision")
            self.connection.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?,?)", tuple(asdict(approval).values()))

    def consume_approval(self, approval_id: str, *, action: str, target: str, content_hash: str,
                         card_revision: int) -> bool:
        with self.transaction():
            result = self.connection.execute(
                "UPDATE approvals SET used=1 WHERE id=? AND action=? AND target=? AND content_hash=? "
                "AND card_revision=? AND used=0 AND card_revision=(SELECT card_revision FROM tasks WHERE id=approvals.task_id)",
                (approval_id, action, target, content_hash, card_revision),
            )
            return result.rowcount == 1

    def get_mail_card(self, account: str, thread_id: str) -> MailCard | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM mail_cards WHERE account=? AND thread_id=?", (account, thread_id)).fetchone()
        return MailCard(row["id"], row["account"], row["thread_id"], MailCardState(row["state"]), json.loads(row["payload"])) if row else None

    def upsert_mail_card(self, card: MailCard) -> MailCard:
        state = MailCardState(card.state)
        with self.transaction():
            existing = self.get_mail_card(card.account, card.thread_id)
            if existing and existing.state != state:
                _check_transition(existing.state, state, _MAIL_TRANSITIONS)
            self.connection.execute(
                "INSERT INTO mail_cards VALUES (?,?,?,?,?) ON CONFLICT(account,thread_id) DO UPDATE SET state=excluded.state,payload=excluded.payload",
                (card.id, card.account, card.thread_id, state, _json(card.payload)),
            )
            if state == MailCardState.CLOSED:
                stored = self.get_mail_card(card.account, card.thread_id)
                self.connection.execute("UPDATE ideas SET state='expired',snoozed_until=NULL WHERE mail_card_id=? AND state IN ('new','snoozed')", (stored.id,))
        return self.get_mail_card(card.account, card.thread_id)

    def get_mail_item(self, account: str, message_id: str) -> MailItem | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM mail_items WHERE account=? AND message_id=?", (account, message_id)).fetchone()
        return MailItem(row["account"], row["message_id"], row["thread_id"], TriageCategory(row["category"]),
                        row["reason"], bool(row["needs_reply"]), json.loads(row["payload"])) if row else None

    def intake_mail(self, item: MailItem, card: MailCard, *, effects: tuple[OutboxEffect, ...] = (),
                    cursor: str | None = None) -> bool:
        """Persist intake, same-thread card update, effects, then cursor atomically.

        False means this account/message was already handled: neither the card
        nor delivery intent is rewritten. A duplicate can advance the cursor.
        Wrap several calls in transaction() to commit an entire polling batch.
        """
        if (item.account, item.thread_id) != (card.account, card.thread_id):
            raise ValueError("Mail item and card must share account and thread")
        category = TriageCategory(item.category)
        with self.transaction():
            inserted = self.get_mail_item(item.account, item.message_id) is None
            if inserted:
                self.upsert_mail_card(card)
                self.connection.execute("INSERT INTO mail_items VALUES (?,?,?,?,?,?,?)", (
                    item.account, item.message_id, item.thread_id, category, item.reason, item.needs_reply, _json(item.payload),
                ))
                for effect in effects:
                    self.enqueue_effect(effect)
                self.record_event("mail_intake", {"account": item.account, "message_id": item.message_id, "category": category})
            if cursor is not None:
                self.set_mail_cursor(MailCursor(item.account, cursor))
        return inserted

    def set_mail_cursor(self, cursor: MailCursor) -> None:
        with self.transaction():
            self.connection.execute("INSERT INTO mail_cursors VALUES (?,?) ON CONFLICT(account) DO UPDATE SET cursor=excluded.cursor", (cursor.account, cursor.cursor))

    def get_mail_cursor(self, account: str) -> MailCursor | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM mail_cursors WHERE account=?", (account,)).fetchone()
        return MailCursor(row["account"], row["cursor"]) if row else None

    def add_idea(self, idea: Idea) -> None:
        if IdeaState(idea.state) != IdeaState.NEW or idea.task_id is not None or idea.snoozed_until is not None:
            raise ValueError("Ideas start new, without an accepted task or snooze")
        with self.transaction():
            card = self._row("mail_cards", "id", idea.mail_card_id)
            if card["state"] == MailCardState.CLOSED:
                raise ValueError("Cannot add an idea to a closed card")
            self.connection.execute("INSERT INTO ideas VALUES (?,?,?,?,?,?,?,?)", (
                idea.id, idea.mail_card_id, idea.reason, _json(idea.sources), idea.expires_at,
                idea.state, idea.task_id, idea.snoozed_until,
            ))

    def get_idea(self, idea_id: str) -> Idea:
        row = self._row("ideas", "id", idea_id)
        return Idea(row["id"], row["mail_card_id"], row["reason"], tuple(json.loads(row["sources"])),
                    row["expires_at"], IdeaState(row["state"]), row["task_id"], row["snoozed_until"])

    def transition_idea(self, idea_id: str, state: IdeaState, *, task_id: str | None = None,
                        snoozed_until: str | None = None) -> Idea:
        state = IdeaState(state)
        if (state == IdeaState.ACCEPTED) != (task_id is not None) or (state == IdeaState.SNOOZED) != (snoozed_until is not None):
            raise ValueError("Accepted ideas require a task; snoozed ideas require a time")
        with self.transaction():
            idea = self.get_idea(idea_id)
            _check_transition(idea.state, state, _IDEA_TRANSITIONS)
            self.connection.execute("UPDATE ideas SET state=?,task_id=?,snoozed_until=? WHERE id=?", (state, task_id, snoozed_until, idea_id))
        return self.get_idea(idea_id)

    def expire_ideas(self, now: str) -> tuple[str, ...]:
        with self.transaction():
            ids = tuple(row[0] for row in self.connection.execute("SELECT id FROM ideas WHERE state IN ('new','snoozed') AND expires_at<=? ORDER BY id", (now,)))
            self.connection.execute("UPDATE ideas SET state='expired',snoozed_until=NULL WHERE state IN ('new','snoozed') AND expires_at<=?", (now,))
        return ids

    @staticmethod
    def _effect(row: sqlite3.Row) -> OutboxEffect:
        return OutboxEffect(row["delivery_id"], row["idempotency_key"], row["channel"], json.loads(row["payload"]),
                            OutboxState(row["state"]), row["receipt"], row["task_id"])

    def enqueue_effect(self, effect: OutboxEffect) -> OutboxEffect:
        if OutboxState(effect.state) != OutboxState.PENDING or effect.receipt is not None:
            raise ValueError("New effects must be pending without a receipt")
        with self.transaction():
            row = self.connection.execute("SELECT * FROM outbox WHERE idempotency_key=?", (effect.idempotency_key,)).fetchone()
            if row:
                existing = self._effect(row)
                if (existing.channel, existing.payload, existing.task_id) != (effect.channel, effect.payload, effect.task_id):
                    raise IdempotencyConflict("Delivery key already bound to a different payload or target")
                return existing
            self.connection.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?)", (
                effect.delivery_id, effect.idempotency_key, effect.channel, _json(effect.payload), effect.state, None, effect.task_id,
            ))
        return self.get_effect(effect.delivery_id)

    def get_effect(self, delivery_id: str) -> OutboxEffect:
        return self._effect(self._row("outbox", "delivery_id", delivery_id))

    def list_effects(self, state: OutboxState | None = None) -> tuple[OutboxEffect, ...]:
        with self._lock:
            rows = self.connection.execute("SELECT * FROM outbox ORDER BY rowid" if state is None else "SELECT * FROM outbox WHERE state=? ORDER BY rowid",
                                           () if state is None else (OutboxState(state),)).fetchall()
        return tuple(self._effect(row) for row in rows)

    def transition_effect(self, delivery_id: str, state: OutboxState, *, receipt: str | None = None) -> OutboxEffect:
        state = OutboxState(state)
        if state == OutboxState.SENT and not receipt:
            raise ValueError("Sent delivery requires a provider receipt")
        if state != OutboxState.SENT and receipt is not None:
            raise ValueError("Only sent deliveries have receipts")
        with self.transaction():
            effect = self.get_effect(delivery_id)
            _check_transition(effect.state, state, _OUTBOX_TRANSITIONS)
            self.connection.execute("UPDATE outbox SET state=?,receipt=? WHERE delivery_id=?", (state, receipt, delivery_id))
        return self.get_effect(delivery_id)

    def _send_claimed(self, effect: OutboxEffect, reconciler: DeliveryReconciler) -> OutboxEffect:
        try:
            receipt = reconciler.send(effect)
            if not receipt:
                raise ValueError("Provider did not confirm delivery")
        except Exception:
            # A send error can happen after the provider committed the post.
            self.transition_effect(effect.delivery_id, OutboxState.UNKNOWN)
            self.record_event("delivery_unknown", {"delivery_id": effect.delivery_id})
        else:
            self.transition_effect(effect.delivery_id, OutboxState.SENT, receipt=receipt)
        return self.get_effect(effect.delivery_id)

    def deliver_pending(self, delivery_id: str, reconciler: DeliveryReconciler) -> OutboxEffect:
        """Claim durably before sending. Sent, sending, failed, unknown are inert."""
        with self.transaction(durable=True):
            effect = self.get_effect(delivery_id)
            if effect.state != OutboxState.PENDING:
                return effect
            effect = self.transition_effect(delivery_id, OutboxState.SENDING)
        return self._send_claimed(effect, reconciler)

    def reconcile_unknown(self, delivery_id: str, reconciler: DeliveryReconciler) -> OutboxEffect:
        # Claim before the history check, so two dispatchers cannot act on
        # separate absence results for the same delivery.
        with self.transaction(durable=True):
            effect = self.get_effect(delivery_id)
            if effect.state != OutboxState.UNKNOWN:
                return effect
            self.connection.execute("UPDATE outbox SET state='sending' WHERE delivery_id=?", (delivery_id,))
            effect = replace(effect, state=OutboxState.SENDING)
        try:
            receipt = reconciler.find_delivery(effect)
            if receipt is not None and not receipt:
                raise ValueError("Invalid provider receipt")
        except Exception:
            self.transition_effect(delivery_id, OutboxState.UNKNOWN)
            self.record_event("delivery_check_unavailable", {"delivery_id": delivery_id})
            return self.get_effect(delivery_id)
        if receipt is not None:
            return self.transition_effect(delivery_id, OutboxState.SENT, receipt=receipt)
        return self._send_claimed(effect, reconciler)

    def recover_on_start(self) -> RecoveryReport:
        """Report interrupted work and uncertain effects; perform no sends."""
        with self.transaction():
            ids = tuple(row[0] for row in self.connection.execute("SELECT id FROM tasks WHERE state='working' ORDER BY id"))
            for task_id in ids:
                self.transition_task(task_id, TaskState.PAUSED)
                self.connection.execute("UPDATE tasks SET interrupted=1 WHERE id=?", (task_id,))
                self.record_event("task_interrupted", {"reason": "restart"}, task_id=task_id)
            sending = self.list_effects(OutboxState.SENDING)
            for effect in sending:
                self.transition_effect(effect.delivery_id, OutboxState.UNKNOWN)
                self.record_event("delivery_interrupted", {"delivery_id": effect.delivery_id})
            unknown = tuple(effect.delivery_id for effect in self.list_effects(OutboxState.UNKNOWN))
            pending = tuple(effect.delivery_id for effect in self.list_effects(OutboxState.PENDING))
        return RecoveryReport(ids, unknown, pending)

    def record_missed_run(self, run: MissedRun) -> bool:
        with self.transaction():
            result = self.connection.execute("INSERT INTO missed_runs VALUES (?,?,?,?,?) ON CONFLICT(job_id,scheduled_at) DO NOTHING", tuple(asdict(run).values()))
        return result.rowcount == 1

    def record_event(self, kind: str, payload: dict, *, task_id: str | None = None) -> Event:
        with self.transaction():
            result = self.connection.execute("INSERT INTO events(kind,payload,task_id) VALUES (?,?,?)", (kind, _json(payload), task_id))
            row = self._row("events", "id", result.lastrowid)
        return Event(row["id"], row["kind"], json.loads(row["payload"]), row["task_id"], row["created_at"])

    def list_events(self) -> tuple[Event, ...]:
        with self._lock:
            rows = self.connection.execute("SELECT * FROM events ORDER BY id").fetchall()
        return tuple(Event(row["id"], row["kind"], json.loads(row["payload"]), row["task_id"], row["created_at"]) for row in rows)
