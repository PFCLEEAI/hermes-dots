"""T5 behavior with real persistence and offline dependencies."""

from dataclasses import replace
from datetime import datetime, timezone
from threading import Thread

import pytest

from fakes import FakeClock, FakeModel
from fakes.model import ModelLimitError
from hermes_dots.brain import Brain, bootstrap
from hermes_dots.engine import CoworkerEngine, LedgerApprovalRepository, StepBlocked, UsageLimitError
from hermes_dots.health import HealthMonitor, PROBE_NAMES
from hermes_dots.ledger import Card, Ledger, OutboxEffect
from hermes_dots.security.approvals import ApprovalRejected, Approvals
from hermes_dots.settings import SettingsService


class StubBrain:
    def __init__(self):
        self.searches = []

    def search(self, query, limit=10):
        self.searches.append(query)
        return [{"citation": "brain://shared/Decisions/acme.md"}]


class ResourceAdapter:
    def __init__(self):
        self.actions = []

    def pause(self, resource_id):
        self.actions.append(("pause", resource_id))

    def resume(self, resource_id):
        self.actions.append(("resume", resource_id))

    def remove(self, resource_id):
        self.actions.append(("remove", resource_id))

    def stop(self, resource_id):
        self.actions.append(("stop", resource_id))


@pytest.fixture
def engine(tmp_path):
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        settings = SettingsService(ledger, home=tmp_path / "home")
        settings.confirm_change(settings.propose_change("timezone", "UTC"))
        clock = FakeClock(datetime(2026, 10, 12, 9, tzinfo=timezone.utc))
        yield CoworkerEngine(ledger, settings, StubBrain(), clock=clock,
                             scheduler=ResourceAdapter(), helpers=ResourceAdapter())


def working(engine, task_id="TASK-1", **contract):
    engine.intake("Review Acme request", id=task_id, sources=("mail",),
                  allowed_actions=("read", "post", "code"), **contract)
    engine.confirm(task_id, 1)
    return task_id


def test_confirm_before_work_and_brain_first(engine):
    card = engine.intake("Review Acme", id="TASK-1", allowed_actions=("read",))
    assert engine.brain.searches == ["Review Acme"]
    assert engine.ledger.get_task(card.id).state == "new"
    with pytest.raises(StepBlocked, match="confirm"):
        engine.run_step(card.id, "read", "input", lambda: "result")
    with pytest.raises(ValueError):
        engine.confirm(card.id, 2)
    engine.confirm(card.id, 1)
    output = engine.run_step(card.id, "read", "input", lambda: "result")
    assert output["citations"] == ["brain://shared/Decisions/acme.md"]


def test_real_brain_citations(tmp_path):
    bootstrap(tmp_path / "data")
    brain = Brain(tmp_path / "data")
    note = brain.learn("Acme deadline", confidence="direct", actor="owner")
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        engine = CoworkerEngine(ledger, SettingsService(ledger), brain)
        card = engine.intake("Acme", allowed_actions=("read",))
        engine.confirm(card.id, 1)
        assert note.id in brain.search("Acme")[0]["id"]
        assert engine.run_step(card.id, "read", "x", lambda: "ok")["citations"]


def test_check_back_clamp_reason_and_scope(engine):
    task_id = working(engine)
    assert engine.schedule_check_back(task_id, 1, "New mail may arrive").due_at.endswith("09:01:00+00:00")
    assert engine.schedule_check_back(task_id, 999999, "Acme may reply").due_at.endswith("15:00:00+00:00")
    engine.clock.advance(hours=6)
    due = engine.tick()["check_backs"]
    assert len(due) == 1
    assert due[0]["scope"]["sources"] == ["mail"]
    assert any(e.kind == "scope_recheck" and e.payload["changed_only"] for e in engine.ledger.list_events())
    assert engine.tick()["check_backs"] == []
    with pytest.raises(StepBlocked, match="scope"):
        engine.run_step(task_id, "read", "x", lambda: None, sources=("outside",))


def test_question_nudge_once_then_digest_other_work_continues(engine):
    task_id = working(engine)
    other = working(engine, "TASK-2")
    question = engine.ask_question(task_id, "Use Acme's report?", "Use the verified report")
    assert "may I go ahead?" in question.text
    assert engine.ledger.get_task(task_id).state == "waiting_on_owner"
    engine.clock.advance(seconds=3599)
    assert engine.tick()["pings"] == ()
    assert engine.run_step(other, "read", "x", lambda: "ok")["result"] == "ok"
    engine.clock.advance(seconds=1)
    assert len(engine.tick()["pings"]) == 1
    engine.clock.advance(hours=1)
    assert engine.tick()["pings"] == ()
    assert engine.digest()["pending_questions"][0]["recommendation"] == "Use the verified report"
    engine.answer_question(question.id, "Yes")
    assert engine.ledger.get_task(task_id).state == "working"
    assert engine.pending_questions() == []


def test_nudge_quiet_hours_held_and_persistent(engine):
    task_id = working(engine)
    engine.clock.set(datetime(2026, 10, 12, 22, 30, tzinfo=timezone.utc))
    engine.ask_question(task_id, "Proceed?", "Proceed with reading")
    engine.clock.advance(hours=1)
    assert engine.tick()["pings"] == ()
    assert engine.ledger.connection.execute("SELECT count(*) FROM ping_queue").fetchone()[0] == 1
    engine.clock.set(datetime(2026, 10, 13, 8, tzinfo=timezone.utc))
    assert len(engine.tick()["pings"]) == 1
    assert engine.tick()["pings"] == ()


def test_approval_missing_changed_used_and_cross_task(engine):
    task_id = working(engine)
    calls = []
    kwargs = dict(action="post", target="Acme", content="Review ready")
    with pytest.raises(StepBlocked, match="approval"):
        engine.run_step(task_id, "post", "x", lambda: calls.append(1), **kwargs)
    assert not calls
    assert engine.ledger.get_task(task_id).blocked_reason == "permission"
    approval = engine.approvals(task_id).grant("post", "Acme", "Review ready", 1)
    with pytest.raises(StepBlocked):
        engine.run_step(task_id, "post", "x", lambda: calls.append(1), approval_id=approval.id,
                        **{**kwargs, "content": "Changed"})
    other = working(engine, "TASK-2")
    with pytest.raises(StepBlocked):
        engine.run_step(other, "post", "x", lambda: calls.append(1), approval_id=approval.id, **kwargs)
    engine.run_step(task_id, "post", "x", lambda: calls.append(1), approval_id=approval.id, **kwargs)
    with pytest.raises(StepBlocked):
        engine.run_step(task_id, "post", "x", lambda: calls.append(1), approval_id=approval.id, **kwargs)
    assert calls == [1]


def test_approvals_survive_restart_revision_and_atomic_consumption(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with Ledger(path) as first:
        first.create_task(Card("TASK-1", "Review Acme"))
        repository = LedgerApprovalRepository(first, "TASK-1")
        approval = Approvals(repository).grant("post", "Acme", "Ready", 1)
        stale = Approvals(repository).grant("post", "Acme", "Other", 1)
    with Ledger(path) as first, Ledger(path) as second:
        a = LedgerApprovalRepository(first, "TASK-1")
        b = LedgerApprovalRepository(second, "TASK-1")
        assert a.get(approval.id) == approval
        results = []
        threads = [Thread(target=lambda repo=repo: results.append(repo.consume(approval))) for repo in (a, b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == [False, True]
        assert a.get(approval.id).used
        first.revise_card(replace(first.get_card("TASK-1"), revision=2))
        assert not a.consume(stale)
        with pytest.raises(ApprovalRejected):
            a.save(approval)


def test_instruction_ambiguity_revision_and_priority_proposal(engine):
    task_id = working(engine)
    other = working(engine, "TASK-2")
    approval = engine.approvals(task_id).grant("post", "Acme", "Ready", 1)
    assert "question" in engine.instruction("Use the latest report")
    card = engine.instruction("Use only the owner's current instruction", task_id=task_id, revision=1)
    assert card.revision == 2
    assert engine.scope(task_id)["instructions"] == ["Use only the owner's current instruction"]
    assert not engine.approvals(task_id).repository.consume(approval)
    engine.register_resource(task_id, "draft", "draft-1", text="Acme draft")
    engine.register_resource(task_id, "schedule", "job-1", instruction="Read mail", time="09:00", timezone="UTC")
    affected = engine.priority_change([task_id, other], proposals={task_id: "change", other: "stop"})
    assert affected[0]["drafts"][0]["id"] == "draft-1"
    assert affected[0]["schedules"][0]["id"] == "job-1"
    assert engine.ledger.get_task(other).state == "working"  # Proposal only.


def test_stop_exact_resources_and_finished_effects(engine):
    task_id = working(engine)
    engine.register_resource(task_id, "helper", "helper-1")
    engine.register_resource(task_id, "schedule", "job-1", instruction="Read mail", time="09:00", timezone="UTC")
    check = engine.schedule_check_back(task_id, 60, "Check files")
    engine.ask_question(task_id, "Proceed?", "Read")
    effect = OutboxEffect("delivery-1", "key-1", "owner", {"text": "Finished"}, task_id=task_id)
    engine.ledger.enqueue_effect(effect)
    engine.ledger.transition_effect(effect.delivery_id, "sending")
    engine.ledger.transition_effect(effect.delivery_id, "sent", receipt="receipt-1")
    stopped = engine.stop(task_id)
    assert stopped["stopped"] == ["task:TASK-1", "helper:helper-1", "schedule:job-1", "check_back:" + check.id]
    assert stopped["finished_effects"] == ["delivery-1"]
    assert engine.helpers.actions == [("stop", "helper-1")]
    assert engine.scheduler.actions == [("remove", "job-1")]
    assert engine.ledger.get_task(task_id).state == "cancelled"
    assert not engine.pending_questions()
    assert not engine.stop(task_id)["stopped"]
    assert not engine.tick()["check_backs"]


def test_pause_resume_all_without_replaying_helpers_or_unconfirmed_work(engine):
    task_id = working(engine)
    engine.intake("Unconfirmed task", id="TASK-2")
    engine.register_resource(task_id, "schedule", "job-1", instruction="Read mail", time="09:00", timezone="UTC")
    engine.register_resource(task_id, "helper", "helper-1")
    assert engine.pause() == ["task:TASK-1", "helper:helper-1", "schedule:job-1", "task:TASK-2"]
    with pytest.raises(StepBlocked):
        engine.run_step(task_id, "read", "x", lambda: None)
    assert engine.resume() == ["TASK-1", "TASK-2"]
    assert engine.ledger.get_task(task_id).state == "working"
    assert engine.ledger.get_task("TASK-2").state == "waiting_on_owner"
    engine.confirm("TASK-2", 1)
    assert engine.ledger.get_task("TASK-2").state == "working"
    assert engine.scheduler.actions == [("pause", "job-1"), ("resume", "job-1")]
    assert engine.helpers.actions == [("stop", "helper-1")]


def test_done_proof_and_reopen(engine):
    task_id = working(engine)
    with pytest.raises(ValueError, match="evidence"):
        engine.done(task_id, "Finished", [])
    assert engine.done(task_id, "Report ready", ["evidence://tests"]).state == "done"
    assert engine.reopen(task_id).state == "working"


def test_same_failure_not_retried_then_owner_retry_fails_task(engine):
    task_id = working(engine)
    calls = []

    def fail():
        calls.append(1)
        raise ValueError("Acme input is incomplete")

    with pytest.raises(ValueError):
        engine.run_step(task_id, "parse", "same-input", fail)
    with pytest.raises(StepBlocked, match="Same failing"):
        engine.run_step(task_id, "parse", "same-input", fail)
    assert calls == [1]
    engine.record_failure(task_id, "parse", "same-input", "Independent second failure")
    assert engine.ledger.get_task(task_id).state == "failed"
    report = engine.status()["tasks"][0]["failure_report"]
    assert {"cause", "last_good_state", "already_made", "safe_to_retry", "owner_action"} <= report.keys()
    engine.reopen(task_id)
    assert engine.run_step(task_id, "parse", "same-input", lambda: "fixed")["result"] == "fixed"


def test_changed_input_and_other_route_are_retryable(engine):
    task_id = working(engine)
    engine.record_failure(task_id, "parse", "bad", "bad input", other_allowed_route=True)
    engine.record_failure(task_id, "parse", "bad", "bad input", other_allowed_route=True)
    assert engine.ledger.get_task(task_id).state == "working"
    assert engine.run_step(task_id, "parse", "corrected", lambda: "ok")["result"] == "ok"
    assert engine.run_step(task_id, "parse", "bad", lambda: "alternative", route="allowed-alternative")["result"] == "alternative"


def test_restart_unknown_step_is_not_replayed(engine):
    task_id = working(engine)

    def crash():
        raise SystemExit("Interrupted step")

    with pytest.raises(SystemExit):
        engine.run_step(task_id, "read", "x", crash)
    report = engine.ledger.recover_on_start()
    assert report.interrupted_task_ids == (task_id,)
    assert engine.status()["tasks"][0]["unknown_step"]["step"] == "read"
    assert engine.resume() == []
    with pytest.raises(StepBlocked, match="reconciliation"):
        engine.reopen(task_id)


def test_provider_limit_probes_commands_missed_runs_no_catchup(engine):
    task_id = working(engine)
    checks = []
    health = HealthMonitor(engine.ledger, engine.settings,
                           {name: lambda name=name: checks.append(name) or (name != "model") for name in PROBE_NAMES},
                           clock=engine.clock)
    engine.health = health
    engine.model = FakeModel(mode="limit")
    engine.limit_errors = (UsageLimitError, ModelLimitError)
    engine.register_resource(task_id, "schedule", "job-1", instruction="Read mail", time="09:00", timezone="UTC")
    engine.schedule_check_back(task_id, 60, "Review updated mail")
    with pytest.raises(ModelLimitError):
        engine.complete_structured({}, "Read the excerpt")
    assert engine.ledger.get_task(task_id).blocked_reason == "model_limit"
    # Non-model work remains possible; it does not lift the model block.
    assert engine.run_step(task_id, "local", "x", lambda: "local result")["result"] == "local result"
    assert engine.ledger.get_task(task_id).blocked_reason == "model_limit"
    engine.clock.advance(seconds=60)
    run = {"task_id": task_id, "job_id": "job-1", "scheduled_at": engine.clock.now().isoformat()}
    assert engine.tick(due_runs=[run])["check_backs"] == []
    assert set(checks) == set(PROBE_NAMES)
    assert len(engine.digest()["missed_runs"]) == 2
    engine.tick(due_runs=[run])
    assert len(engine.missed_runs()) == 2
    assert engine.status()["usage"]["calls"] == 1
    health.probes["model"] = lambda: True
    assert engine.tick()["check_backs"] == []
    assert engine.ledger.get_task(task_id).state == "working"
    assert engine.scheduler.actions == [("pause", "job-1"), ("resume", "job-1")]
    assert engine.model.calls == 1


def test_daily_cap_persists_to_next_local_day(engine):
    task_id = working(engine)
    engine.settings.confirm_change(engine.settings.propose_change("timezone", "Asia/Seoul"))
    engine.settings.confirm_change(engine.settings.propose_change("daily_token_cap", 10))
    engine.model = FakeModel({None: {"ok": True}})
    assert engine.complete_structured({}, "Acme excerpt") == {"ok": True}
    assert engine.usage.daily_tokens() == 10
    assert engine.ledger.get_task(task_id).blocked_reason == "model_limit"
    with pytest.raises(UsageLimitError):
        engine.complete_structured({}, "More work")
    engine.provider_available()
    assert engine.ledger.get_task(task_id).blocked_reason == "model_limit"
    engine.clock.set(datetime(2026, 10, 12, 15, tzinfo=timezone.utc))  # Next KST day.
    engine.tick()
    assert engine.ledger.get_task(task_id).state == "working"
    assert engine.usage.daily_tokens() == 0


def test_docker_failure_blocks_code_and_recovers(engine):
    task_id = working(engine)
    engine.health = HealthMonitor(engine.ledger, engine.settings, {name: lambda: True for name in PROBE_NAMES}, clock=engine.clock)
    engine.health.probes["docker"] = lambda: False
    engine.tick()
    with pytest.raises(StepBlocked, match="Docker"):
        engine.run_step(task_id, "code", "x", lambda: None, action="code")
    assert engine.ledger.get_task(task_id).blocked_reason == "health"
    engine.health.probes["docker"] = lambda: True
    engine.tick()
    assert engine.run_step(task_id, "code", "x", lambda: "passed", action="code")["result"] == "passed"


def test_model_prompts_results_and_failure_reports_redacted(engine):
    task_id = working(engine)
    token = "sk" + "-" + "invented-example-value"
    engine.model = FakeModel({None: {"result": token}})
    assert engine.complete_structured({}, token)["result"] == "[REDACTED]"
    assert token not in str(engine.model.requests)
    engine.record_failure(task_id, "read", "x", token)
    assert token not in str(engine.ledger.list_events())


def test_step_paths_denied_before_operation(engine):
    task_id = working(engine)
    called = []
    with pytest.raises(PermissionError):
        engine.run_step(task_id, "read", "x", lambda: called.append(1), paths=("~/.hermes/config.yaml",))
    assert not called


def test_pausing_limit_work_keeps_schedules_paused_until_provider_recovers(engine):
    task_id = working(engine)
    engine.register_resource(task_id, "schedule", "job-1", instruction="Read mail", time="09:00", timezone="UTC")
    engine.limit_reached()
    engine.pause()
    engine.resume()
    assert engine.ledger.get_task(task_id).blocked_reason == "model_limit"
    assert engine.scheduler.actions == [("pause", "job-1")]
    engine.provider_available()
    assert engine.scheduler.actions == [("pause", "job-1"), ("resume", "job-1")]
