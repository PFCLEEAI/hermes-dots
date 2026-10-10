"""T2 checks with invented data and local provider stubs only."""

from dataclasses import replace
import sqlite3
from threading import Event as ThreadEvent, Thread

import pytest

from hermes_dots.ledger import (
    Approval, BlockedReason, Card, CheckBack, IdempotencyConflict, Idea,
    IdeaState, IllegalTransition, Ledger, MailCard, MailCardState, MailCursor,
    MailItem, MissedRun, OutboxEffect, OutboxState, Question, TaskState,
    TriageCategory,
)


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "ledger.sqlite") as store:
        yield store


def mail(message="message-1", thread="thread-1", account="alex@example.com"):
    return MailItem(account, message, thread, TriageCategory.URGENT, "Acme deadline", True)


def card(thread="thread-1", account="alex@example.com", card_id="mail-card-1"):
    return MailCard(card_id, account, thread, payload={"text": "Acme request"})


def effect(delivery="delivery-1", key="mail:message-1"):
    return OutboxEffect(delivery, key, "owner-channel", {"text": "Acme request"})


class StubReconciler:
    def __init__(self):
        self.posts = {}
        self.sends = []
        self.checks = []
        self.unavailable = False
        self.crash_after_post = False
        self.error_after_post = False

    def find_delivery(self, delivery):
        self.checks.append(delivery.delivery_id)
        if self.unavailable:
            raise OSError("History unavailable")
        return self.posts.get(delivery.delivery_id)

    def send(self, delivery):
        assert delivery.state == OutboxState.SENDING
        self.sends.append(delivery.delivery_id)
        receipt = f"receipt-{len(self.sends)}"
        self.posts[delivery.delivery_id] = receipt
        if self.crash_after_post:
            raise SystemExit("Simulated crash after provider commit")
        if self.error_after_post:
            raise OSError("Lost acknowledgement")
        return receipt


def test_enum_sets_match_contract():
    assert {x.value for x in TaskState} == {"new", "working", "waiting_on_owner", "paused", "blocked", "done", "cancelled", "failed"}
    assert {x.value for x in BlockedReason} == {"model_limit", "health", "permission"}
    assert {x.value for x in MailCardState} == {"open", "replied_still_open", "closed"}
    assert {x.value for x in IdeaState} == {"new", "accepted", "dismissed", "snoozed", "expired"}
    assert {x.value for x in TriageCategory} == {"urgent", "regular", "noise", "uncertain"}
    assert {x.value for x in OutboxState} == {"pending", "sending", "sent", "failed", "unknown"}


def test_schema_wal_and_foreign_keys(ledger):
    assert ledger.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert ledger.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    names = {row[0] for row in ledger.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names >= {"tasks", "cards", "check_backs", "questions", "approvals", "mail_items", "mail_cursors", "mail_cards", "ideas", "outbox", "missed_runs", "events"}
    with pytest.raises(sqlite3.IntegrityError):
        ledger.add_check_back(CheckBack("cb", "missing", "2026-01-01T09:00:00Z", "Check result"))


def test_migrations_filename_order_recorded_and_not_repeated(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0002_second.sql").write_text("INSERT INTO order_check VALUES ('second');")
    (directory / "0001_first.sql").write_text("CREATE TABLE order_check (value TEXT); INSERT INTO order_check VALUES ('first');")
    path = tmp_path / "ledger.sqlite"
    with Ledger(path, migrations_dir=directory) as store:
        assert [row[0] for row in store.connection.execute("SELECT value FROM order_check")] == ["first", "second"]
        assert [row[0] for row in store.connection.execute("SELECT name FROM schema_migrations ORDER BY name")] == ["0001_first.sql", "0002_second.sql"]
        assert store.apply_migrations(directory) == ()
    with Ledger(path, migrations_dir=directory) as store:
        assert store.connection.execute("SELECT count(*) FROM order_check").fetchone()[0] == 2


def test_failed_migration_rolls_back_schema_and_applied_name(ledger, tmp_path):
    directory = tmp_path / "extra"
    directory.mkdir()
    (directory / "0002_bad.sql").write_text("CREATE TABLE partial (id TEXT); INSERT INTO nonexistent VALUES ('x');")
    with pytest.raises(sqlite3.OperationalError):
        ledger.apply_migrations(directory)
    assert ledger.connection.execute("SELECT 1 FROM sqlite_master WHERE name='partial'").fetchone() is None
    assert ledger.connection.execute("SELECT 1 FROM schema_migrations WHERE name='0002_bad.sql'").fetchone() is None


def test_legal_task_transitions_revision_and_done_proof(ledger):
    original = Card("TASK-1", "Review Acme request", sources=("mail",), allowed_actions=("read",))
    assert ledger.create_task(original).state == TaskState.NEW
    assert ledger.get_card(original.id) == original
    with pytest.raises(IllegalTransition):
        ledger.transition_task(original.id, TaskState.DONE, done_evidence=("evidence://result",))
    ledger.transition_task(original.id, TaskState.WORKING)
    with pytest.raises(ValueError, match="evidence"):
        ledger.transition_task(original.id, TaskState.DONE)
    with pytest.raises(ValueError, match="blocked reason"):
        ledger.transition_task(original.id, TaskState.BLOCKED)
    blocked = ledger.transition_task(original.id, TaskState.BLOCKED, blocked_reason=BlockedReason.HEALTH)
    assert blocked.blocked_reason == BlockedReason.HEALTH
    ledger.transition_task(original.id, TaskState.WORKING)
    revised = replace(ledger.get_card(original.id), revision=2, goal="Review updated request")
    assert ledger.revise_card(revised) == revised
    assert ledger.get_card(original.id, 1).goal == original.goal
    with pytest.raises(ValueError):
        ledger.revise_card(revised)
    ledger.transition_task(original.id, TaskState.DONE, done_evidence=("evidence://result",))
    assert ledger.get_card(original.id).status == TaskState.DONE
    ledger.transition_task(original.id, TaskState.WORKING)  # Owner reopens.
    ledger.transition_task(original.id, TaskState.CANCELLED)
    with pytest.raises(IllegalTransition):
        ledger.transition_task(original.id, TaskState.WORKING)


def test_auxiliary_records_and_single_use_exact_approval(ledger):
    ledger.create_task(Card("TASK-1", "Review Acme"))
    ledger.add_check_back(CheckBack("cb-1", "TASK-1", "2026-01-01T09:00:00Z", "Review progress"))
    ledger.add_question(Question("q-1", "TASK-1", "Continue?", "Continue reading", "2026-01-01T08:00:00Z"))
    ledger.add_approval(Approval("approval-1", "TASK-1", "post", "Acme", "hash-1", 1))
    matching = dict(action="post", target="Acme", content_hash="hash-1", card_revision=1)
    assert not ledger.consume_approval("approval-1", **{**matching, "content_hash": "hash-2"})
    assert ledger.consume_approval("approval-1", **matching)
    assert not ledger.consume_approval("approval-1", **matching)
    ledger.add_approval(Approval("approval-2", "TASK-1", "post", "Acme", "hash-1", 1))
    ledger.revise_card(replace(ledger.get_card("TASK-1"), revision=2))
    assert not ledger.consume_approval("approval-2", **matching)
    run = MissedRun("run-1", "TASK-1", "job-1", "2026-01-01T09:00:00Z", "model_limit")
    assert ledger.record_missed_run(run)
    assert not ledger.record_missed_run(replace(run, id="run-2"))
    event = ledger.record_event("feedback", {"text": "Use a shorter draft"}, task_id="TASK-1")
    assert event == ledger.list_events()[-1]


def test_duplicate_mail_ignored_but_different_accounts_independent(ledger):
    assert ledger.intake_mail(mail(), card(), effects=(effect(),), cursor="cursor-1")
    assert not ledger.intake_mail(replace(mail(), category=TriageCategory.NOISE), replace(card(), payload={"text": "changed"}),
                                  effects=(effect("delivery-2", "different-key"),), cursor="cursor-2")
    assert ledger.get_mail_item("alex@example.com", "message-1") == mail()
    assert ledger.get_mail_card("alex@example.com", "thread-1") == card()
    assert ledger.get_mail_cursor("alex@example.com").cursor == "cursor-2"
    assert len(ledger.list_effects()) == 1
    assert ledger.intake_mail(mail(account="other@example.com"), card(account="other@example.com", card_id="mail-card-2"))


def test_mail_card_unique_per_thread_and_updates_in_place(ledger):
    ledger.intake_mail(mail(), card())
    later = replace(card(card_id="ignored-new-id"), payload={"text": "New Acme message"})
    ledger.intake_mail(mail("message-2"), later)
    stored = ledger.get_mail_card("alex@example.com", "thread-1")
    assert stored.id == "mail-card-1"
    assert stored.payload == later.payload
    assert ledger.connection.execute("SELECT count(*) FROM mail_cards").fetchone()[0] == 1
    assert ledger.connection.execute("SELECT count(*) FROM mail_items").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        ledger.connection.execute("INSERT INTO mail_cards VALUES (?,?,?,?,?)", ("duplicate", "alex@example.com", "thread-1", "open", "{}"))


def test_intake_and_effect_rollback_before_cursor_advance(ledger, monkeypatch):
    ledger.set_mail_cursor(MailCursor("alex@example.com", "cursor-0"))
    original = ledger.set_mail_cursor

    def crash(cursor):
        assert ledger.get_mail_item("alex@example.com", "message-1") is not None
        assert ledger.get_effect("delivery-1").state == OutboxState.PENDING
        original(cursor)
        raise SystemExit("Crash before commit")

    monkeypatch.setattr(ledger, "set_mail_cursor", crash)
    with pytest.raises(SystemExit):
        ledger.intake_mail(mail(), card(), effects=(effect(),), cursor="cursor-1")
    assert ledger.get_mail_item("alex@example.com", "message-1") is None
    assert ledger.get_mail_card("alex@example.com", "thread-1") is None
    assert ledger.list_effects() == ()
    assert ledger.get_mail_cursor("alex@example.com").cursor == "cursor-0"
    assert ledger.list_events() == ()


def test_idempotency_key_returns_original_and_rejects_different_payload(ledger):
    stored = ledger.enqueue_effect(effect())
    assert ledger.enqueue_effect(effect("delivery-2")) == stored
    with pytest.raises(IdempotencyConflict):
        ledger.enqueue_effect(replace(effect(), payload={"text": "changed"}))
    with pytest.raises(IdempotencyConflict):
        ledger.enqueue_effect(replace(effect(), channel="different-channel"))
    assert ledger.list_effects() == (stored,)


def test_duplicate_key_failure_rolls_back_entire_intake(ledger):
    ledger.enqueue_effect(effect())
    with pytest.raises(IdempotencyConflict):
        ledger.intake_mail(mail(), card(), effects=(replace(effect(), payload={"text": "changed"}),), cursor="cursor-1")
    assert ledger.get_mail_item("alex@example.com", "message-1") is None
    assert ledger.get_mail_cursor("alex@example.com") is None


def test_restart_after_intake_before_delivery_has_no_loss_or_duplicate(tmp_path):
    path = tmp_path / "ledger.sqlite"
    provider = StubReconciler()
    with Ledger(path) as store:
        store.intake_mail(mail(), card(), effects=(effect(),), cursor="cursor-1")
    with Ledger(path) as store:
        report = store.recover_on_start()
        assert report.pending_delivery_ids == ("delivery-1",)
        assert provider.sends == []
        assert store.get_mail_cursor("alex@example.com").cursor == "cursor-1"
        assert not store.intake_mail(mail(), card(), effects=(effect(),))
        assert store.deliver_pending("delivery-1", provider).state == OutboxState.SENT
        assert store.deliver_pending("delivery-1", provider).state == OutboxState.SENT
    with Ledger(path) as store:
        assert store.recover_on_start().pending_delivery_ids == ()
        assert store.deliver_pending("delivery-1", provider).state == OutboxState.SENT
    assert provider.sends == ["delivery-1"]


def test_crash_after_provider_commit_reconciles_without_second_send(tmp_path):
    path = tmp_path / "ledger.sqlite"
    provider = StubReconciler()
    provider.crash_after_post = True
    with Ledger(path) as store:
        store.enqueue_effect(effect())
        with pytest.raises(SystemExit):
            store.deliver_pending("delivery-1", provider)
        assert store.get_effect("delivery-1").state == OutboxState.SENDING
    with Ledger(path) as store:
        assert store.recover_on_start().unknown_delivery_ids == ("delivery-1",)
        assert store.deliver_pending("delivery-1", provider).state == OutboxState.UNKNOWN
        assert store.reconcile_unknown("delivery-1", provider).receipt == "receipt-1"
    assert provider.sends == ["delivery-1"]
    assert provider.checks == ["delivery-1"]


def test_unknown_absent_sends_once_and_unavailable_stays_visible(ledger):
    provider = StubReconciler()
    ledger.enqueue_effect(effect())
    ledger.transition_effect("delivery-1", OutboxState.SENDING)
    ledger.recover_on_start()
    provider.unavailable = True
    assert ledger.reconcile_unknown("delivery-1", provider).state == OutboxState.UNKNOWN
    assert ledger.list_effects(OutboxState.UNKNOWN)[0].delivery_id == "delivery-1"
    assert provider.sends == []
    provider.unavailable = False
    assert ledger.reconcile_unknown("delivery-1", provider).state == OutboxState.SENT
    assert ledger.reconcile_unknown("delivery-1", provider).state == OutboxState.SENT
    assert provider.sends == ["delivery-1"]


def test_send_exception_is_unknown_not_retryable_failure(ledger):
    provider = StubReconciler()
    provider.error_after_post = True
    ledger.enqueue_effect(effect())
    assert ledger.deliver_pending("delivery-1", provider).state == OutboxState.UNKNOWN
    assert ledger.deliver_pending("delivery-1", provider).state == OutboxState.UNKNOWN
    assert ledger.reconcile_unknown("delivery-1", provider).state == OutboxState.SENT
    assert provider.sends == ["delivery-1"]


def test_illegal_outbox_transitions_and_explicit_failed_retry(ledger):
    ledger.enqueue_effect(effect())
    with pytest.raises(IllegalTransition):
        ledger.transition_effect("delivery-1", OutboxState.SENT, receipt="receipt-1")
    ledger.transition_effect("delivery-1", OutboxState.FAILED)
    assert ledger.deliver_pending("delivery-1", StubReconciler()).state == OutboxState.FAILED
    ledger.transition_effect("delivery-1", OutboxState.PENDING)
    ledger.transition_effect("delivery-1", OutboxState.SENDING)
    ledger.transition_effect("delivery-1", OutboxState.UNKNOWN)
    with pytest.raises(IllegalTransition):
        ledger.transition_effect("delivery-1", OutboxState.PENDING)


def test_delivery_refuses_uncommitted_intake(ledger):
    provider = StubReconciler()
    with ledger.transaction():
        ledger.intake_mail(mail(), card(), effects=(effect(),))
        with pytest.raises(RuntimeError, match="committed intake"):
            ledger.deliver_pending("delivery-1", provider)
    assert provider.sends == []
    assert ledger.get_effect("delivery-1").state == OutboxState.PENDING


@pytest.mark.parametrize("unknown", [False, True])
def test_two_connections_claim_only_once(tmp_path, unknown):
    path = tmp_path / "ledger.sqlite"
    entered = ThreadEvent()
    release = ThreadEvent()
    errors = []

    class BlockingProvider(StubReconciler):
        def find_delivery(self, delivery):
            entered.set()
            assert release.wait(5)
            return super().find_delivery(delivery)

        def send(self, delivery):
            if not unknown:
                entered.set()
                assert release.wait(5)
            return super().send(delivery)

    provider = BlockingProvider()
    with Ledger(path) as first, Ledger(path) as second:
        first.enqueue_effect(effect())
        if unknown:
            first.transition_effect("delivery-1", OutboxState.SENDING)
            first.recover_on_start()

        def run():
            try:
                method = first.reconcile_unknown if unknown else first.deliver_pending
                method("delivery-1", provider)
            except BaseException as exc:
                errors.append(exc)

        worker = Thread(target=run)
        worker.start()
        try:
            assert entered.wait(5)
            method = second.reconcile_unknown if unknown else second.deliver_pending
            assert method("delivery-1", provider).state == OutboxState.SENDING
        finally:
            release.set()
            worker.join(5)
        assert not worker.is_alive()
        assert errors == []
        assert second.get_effect("delivery-1").state == OutboxState.SENT
    assert provider.sends == ["delivery-1"]


def test_recovery_marks_work_interrupted_and_never_replays(ledger):
    ledger.create_task(Card("TASK-1", "Review Acme"))
    ledger.transition_task("TASK-1", TaskState.WORKING)
    ledger.create_task(Card("TASK-2", "Wait for owner"))
    ledger.transition_task("TASK-2", TaskState.WAITING_ON_OWNER)
    ledger.enqueue_effect(effect())
    ledger.transition_effect("delivery-1", OutboxState.SENDING)
    report = ledger.recover_on_start()
    assert report.interrupted_task_ids == ("TASK-1",)
    assert report.unknown_delivery_ids == ("delivery-1",)
    assert ledger.get_task("TASK-1").state == TaskState.PAUSED
    assert ledger.get_task("TASK-1").interrupted
    assert ledger.get_card("TASK-1").status == TaskState.PAUSED
    assert ledger.get_task("TASK-2").state == TaskState.WAITING_ON_OWNER
    assert any(e.kind == "task_interrupted" for e in ledger.list_events())
    assert ledger.recover_on_start().interrupted_task_ids == ()
    ledger.transition_task("TASK-1", TaskState.WORKING)
    assert not ledger.get_task("TASK-1").interrupted


def test_mail_and_idea_transitions_expiry_and_accept_task_link(ledger):
    ledger.upsert_mail_card(card())
    idea = Idea("idea-1", "mail-card-1", "Follow up", ("mail:message-1",), "2026-01-08T00:00:00Z")
    ledger.add_idea(idea)
    with pytest.raises(ValueError):
        ledger.transition_idea(idea.id, IdeaState.ACCEPTED)
    ledger.create_task(Card("TASK-1", "Follow up"))
    accepted = ledger.transition_idea(idea.id, IdeaState.ACCEPTED, task_id="TASK-1")
    assert accepted.task_id == "TASK-1"
    with pytest.raises(IllegalTransition):
        ledger.transition_idea(idea.id, IdeaState.NEW)
    ledger.add_idea(replace(idea, id="idea-2"))
    ledger.transition_idea("idea-2", IdeaState.SNOOZED, snoozed_until="2026-01-03T00:00:00Z")
    replied = replace(card(), state=MailCardState.REPLIED_STILL_OPEN)
    ledger.upsert_mail_card(replied)
    assert ledger.get_mail_card(replied.account, replied.thread_id).state == MailCardState.REPLIED_STILL_OPEN
    assert ledger.get_idea("idea-2").state == IdeaState.SNOOZED
    with pytest.raises(IllegalTransition):
        ledger.upsert_mail_card(card())
    ledger.upsert_mail_card(replace(card(), state=MailCardState.CLOSED))
    assert ledger.get_idea("idea-2").state == IdeaState.EXPIRED
    assert ledger.get_idea("idea-1").state == IdeaState.ACCEPTED
    ledger.upsert_mail_card(card())  # New inbound message can reopen a thread.
    ledger.add_idea(replace(idea, id="idea-3"))
    assert ledger.expire_ideas("2026-01-08T00:00:00Z") == ("idea-3",)
