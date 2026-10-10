"""Ledger-backed coworker rules. Providers and execution are injected."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta
from hashlib import sha256
import json
from uuid import uuid4

from hermes_dots.ledger import BlockedReason, Card, CheckBack, MissedRun, Question, TaskState
from hermes_dots.security.approvals import Approvals, ApprovalRejected
from hermes_dots.security.rules import RuleLevel
from .approvals import LedgerApprovalRepository
from .support import clean, encode, event, now, queue_ping, release_pings, stamp
from .usage import UsageLimitError, UsageMeter


TERMINAL = {TaskState.DONE, TaskState.CANCELLED, TaskState.FAILED}


class StepBlocked(PermissionError):
    pass


class CoworkerEngine:
    def __init__(self, ledger, settings, brain, *, clock=None, model=None, health=None,
                 scheduler=None, helpers=None, evidence=None, owner_channel="owner",
                 limit_errors=(UsageLimitError,)):
        self.ledger = ledger
        self.settings = settings
        self.brain = brain
        self.clock = clock
        self.model = model
        self.health = health
        self.scheduler = scheduler
        self.helpers = helpers
        self.evidence = evidence
        self.owner_channel = owner_channel
        self.limit_errors = limit_errors
        self.usage = UsageMeter(ledger, settings, clock)

    def _event(self, kind, payload, task_id=None):
        return event(self.ledger, kind, payload, task_id, self.evidence)

    def _runtime(self, task_id):
        with self.ledger.transaction():
            row = self.ledger.connection.execute("SELECT data FROM task_runtime WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else {"confirmed_revision": None, "citations": [], "files": [], "failures": {}, "instructions": []}

    def _save(self, task_id, data):
        with self.ledger.transaction():
            self.ledger.connection.execute("INSERT INTO task_runtime VALUES (?,?) ON CONFLICT(task_id) DO UPDATE SET data=excluded.data",
                                           (task_id, encode(data)))

    def _control(self, name, default=None):
        with self.ledger.transaction():
            row = self.ledger.connection.execute("SELECT value FROM engine_control WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set_control(self, name, value):
        with self.ledger.transaction():
            self.ledger.connection.execute("INSERT INTO engine_control VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                                           (name, encode(value)))

    def intake(self, goal, **contract):
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("A card needs a goal")
        card = Card(id=contract.pop("id", "TASK-" + uuid4().hex), goal=clean(goal), **clean(contract))
        # Brain-first recall is bounded; owner instructions remain the contract.
        matches = self.brain.search(card.goal, limit=10)
        with self.ledger.transaction():
            self.ledger.create_task(card)
            data = self._runtime(card.id)
            data["citations"] = [match["citation"] for match in matches]
            self._save(card.id, data)
            self._event("brain_first", {"citations": data["citations"]}, card.id)
        return card

    def confirm(self, task_id, revision):
        task = self.ledger.get_task(task_id)
        if (revision != task.card_revision or task.state not in {TaskState.NEW, TaskState.PAUSED, TaskState.WAITING_ON_OWNER}
                or self._runtime(task_id)["confirmed_revision"] == revision):
            raise ValueError("Confirm the current new card revision")
        with self.ledger.transaction():
            data = self._runtime(task_id)
            data["confirmed_revision"] = revision
            self._save(task_id, data)
            self._event("card_confirmed", {"revision": revision}, task_id)
            if not self._control("paused", False):
                self.ledger.transition_task(task_id, TaskState.WORKING)
        self._sync_limits()
        return self.ledger.get_task(task_id)

    def approvals(self, task_id):
        return Approvals(LedgerApprovalRepository(self.ledger, task_id))

    def schedule_check_back(self, task_id, delay_seconds, reason):
        if self.ledger.get_task(task_id).state in TERMINAL or not reason.strip():
            raise ValueError("An open task and a check-back reason are required")
        delay = max(self.settings.get("check_back_min_seconds"), min(delay_seconds, self.settings.get("check_back_max_seconds")))
        check = CheckBack(uuid4().hex, task_id, stamp(now(self.clock) + timedelta(seconds=delay)), clean(reason))
        with self.ledger.transaction():
            self.ledger.connection.execute("DELETE FROM check_backs WHERE task_id=?", (task_id,))
            self.ledger.add_check_back(check)
            self._event("check_back_scheduled", {"due_at": check.due_at, "reason": check.reason}, task_id)
        return check

    def ask_question(self, task_id, text, recommendation):
        task = self.ledger.get_task(task_id)
        if task.state in TERMINAL or not text.strip() or not recommendation.strip():
            raise ValueError("An open task, question, and recommendation are required")
        question = Question(uuid4().hex, task_id, clean(text.rstrip(" ?") + "; may I go ahead?"), clean(recommendation), stamp(now(self.clock)))
        with self.ledger.transaction():
            self.ledger.add_question(question)
            if task.state in {TaskState.NEW, TaskState.WORKING}:
                self.ledger.transition_task(task_id, TaskState.WAITING_ON_OWNER)
            self._event("pending_question", asdict(question), task_id)
        return question

    def answer_question(self, question_id, answer):
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("An answer is required")
        with self.ledger.transaction():
            row = self.ledger.connection.execute("SELECT * FROM questions WHERE id=? AND answer IS NULL", (question_id,)).fetchone()
            if row is None:
                raise ValueError("Question is no longer pending")
            self.ledger.connection.execute("UPDATE questions SET answer=? WHERE id=?", (clean(answer), question_id))
            self._event("feedback", {"kind": "question_answer", "id": question_id, "answer": answer}, row["task_id"])
            task = self.ledger.get_task(row["task_id"])
            if task.state == TaskState.WAITING_ON_OWNER and not self.pending_questions(task.id):
                if self._runtime(task.id)["confirmed_revision"] == task.card_revision:
                    self.ledger.transition_task(task.id, TaskState.WORKING)
        # An answer is not an action approval; exact-payload grant is separate.
        return self.ledger.get_task(row["task_id"])

    def pending_questions(self, task_id=None):
        with self.ledger.transaction():
            rows = self.ledger.connection.execute("SELECT * FROM questions WHERE answer IS NULL ORDER BY asked_at,id").fetchall()
        return [dict(row) for row in rows if task_id is None or row["task_id"] == task_id]

    def instruction(self, text, *, task_id=None, revision=None):
        active = [t for t in self.ledger.list_tasks() if t.state not in TERMINAL]
        if task_id is None and len(active) == 1:
            task_id = active[0].id
        if task_id is None:
            result = {"question": "Which task does this instruction apply to?", "recommendation": "Choose the intended card",
                      "task_ids": [t.id for t in active]}
            self._event("ambiguous_instruction", {**result, "instruction": text})
            return result
        task = self.ledger.get_task(task_id)
        if task.state in TERMINAL or (revision is not None and revision != task.card_revision):
            raise ValueError("Instruction must bind to the current open card")
        with self.ledger.transaction():
            data = self._runtime(task_id)
            data["instructions"].append(clean(text))
            card = self.ledger.revise_card(replace(self.ledger.get_card(task_id), revision=task.card_revision + 1))
            # An instruction revises running work, but cannot confirm unstarted work.
            if data["confirmed_revision"] == task.card_revision:
                data["confirmed_revision"] = card.revision
            self._save(task_id, data)
            self._event("owner_instruction", {"instruction": text, "revision": card.revision}, task_id)
        return card

    def priority_change(self, task_ids, *, proposals=None):
        result = []
        for task_id in task_ids:
            task = self.ledger.get_task(task_id)
            decision = (proposals or {}).get(task_id, "keep")
            if decision not in {"keep", "stop", "change"}:
                raise ValueError("Priority proposals must be keep, stop, or change")
            result.append({"task_id": task_id, "state": task.state, "proposal": decision,
                           "schedules": self.resources(task_id, "schedule"), "drafts": self.resources(task_id, "draft")})
        self._event("priority_change_proposal", {"affected": result})
        return result

    def scope(self, task_id):
        card = self.ledger.get_card(task_id)
        return {"task_id": task_id, "revision": card.revision, "sources": list(card.sources),
                "allowed_actions": list(card.allowed_actions), "approval_points": list(card.approval_points),
                "instructions": self._runtime(task_id)["instructions"]}

    def _require_scope(self, task_id, action, sources):
        scope = self.scope(task_id)
        if action not in scope["allowed_actions"] or not set(sources).issubset(scope["sources"]):
            self._block(task_id, BlockedReason.PERMISSION)
            self._event("scope_blocked", {"action": action, "sources": list(sources)}, task_id)
            raise StepBlocked("Action or source is outside the confirmed card scope")

    def _block(self, task_id, reason):
        task = self.ledger.get_task(task_id)
        if task.state in TERMINAL or task.state == TaskState.PAUSED:
            return
        data = self._runtime(task_id)
        if task.state != TaskState.BLOCKED:
            data["before_block"] = task.state
            self._save(task_id, data)
            self.ledger.transition_task(task_id, TaskState.BLOCKED, blocked_reason=reason)
        elif task.blocked_reason != reason:
            # T2 forbids blocked->blocked, so preserve the prior state explicitly.
            self.ledger.transition_task(task_id, TaskState.WAITING_ON_OWNER)
            self.ledger.transition_task(task_id, TaskState.BLOCKED, blocked_reason=reason)

    def _restore(self, task_id):
        task = self.ledger.get_task(task_id)
        data = self._runtime(task_id)
        if data["confirmed_revision"] != task.card_revision:
            state = TaskState.WAITING_ON_OWNER
        else:
            state = TaskState.WAITING_ON_OWNER if self.pending_questions(task_id) else TaskState.WORKING
        self.ledger.transition_task(task_id, state)

    def register_resource(self, task_id, kind, resource_id, **data):
        if kind not in {"helper", "schedule", "draft"} or self.ledger.get_task(task_id).state in TERMINAL:
            raise ValueError("Resource requires an open task and known kind")
        if kind == "schedule":
            required = {"instruction", "time", "timezone"}
            if not required.issubset(data):
                raise ValueError("Schedule needs stored instruction, time, and timezone")
            data.setdefault("end_date", None)
            data.setdefault("requires_model", True)
        with self.ledger.transaction():
            self.ledger.connection.execute("INSERT INTO task_resources VALUES (?,?,?,?,?)",
                                           (resource_id, task_id, kind, "active", encode(data)))
        return {"id": resource_id, "task_id": task_id, "kind": kind, **clean(data)}

    def resources(self, task_id, kind=None):
        with self.ledger.transaction():
            rows = self.ledger.connection.execute("SELECT * FROM task_resources WHERE task_id=? ORDER BY kind,id", (task_id,)).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows if kind is None or row["kind"] == kind]

    def schedules(self):
        return [resource for task in self.ledger.list_tasks() for resource in self.resources(task.id, "schedule")]

    def _resource_action(self, resource, operation, state):
        adapter = self.scheduler if resource["kind"] == "schedule" else self.helpers
        if adapter is None:
            raise StepBlocked("Resource control adapter is unavailable")
        getattr(adapter, operation)(resource["id"])
        with self.ledger.transaction():
            self.ledger.connection.execute("UPDATE task_resources SET state=? WHERE kind=? AND id=?", (state, resource["kind"], resource["id"]))

    def pause(self):
        self._set_control("paused", True)
        stopped = []
        for task in self.ledger.list_tasks():
            if task.state in TERMINAL:
                continue
            data = self._runtime(task.id)
            if task.state != TaskState.PAUSED:
                data["before_pause"] = {"state": task.state, "reason": task.blocked_reason}
                self._save(task.id, data)
                self.ledger.transition_task(task.id, TaskState.PAUSED)
                stopped.append("task:" + task.id)
            for resource in self.resources(task.id):
                if resource["state"] == "active" and resource["kind"] in {"schedule", "helper"}:
                    operation = "pause" if resource["kind"] == "schedule" else "stop"
                    self._resource_action(resource, operation, "paused" if operation == "pause" else "stopped")
                    stopped.append(resource["kind"] + ":" + resource["id"])
        self._event("paused", {"stopped": stopped})
        return stopped

    def resume(self):
        self._set_control("paused", False)
        resumed = []
        for task in self.ledger.list_tasks():
            data = self._runtime(task.id)
            if task.state != TaskState.PAUSED or "before_pause" not in data:
                continue  # Interrupted work is never resumed implicitly.
            previous = data.pop("before_pause")
            self._save(task.id, data)
            if previous["state"] == TaskState.BLOCKED:
                self.ledger.transition_task(task.id, TaskState.BLOCKED, blocked_reason=previous["reason"])
            else:
                self._restore(task.id)
            resumed.append(task.id)
            for resource in self.resources(task.id, "schedule"):
                if resource["state"] == "paused":
                    self._resource_action(resource, "resume", "active")
        self._sync_limits()
        self._event("resumed", {"tasks": resumed})
        return resumed

    def stop(self, task_id):
        task = self.ledger.get_task(task_id)
        if task.state == TaskState.CANCELLED:
            return {"stopped": [], "finished_effects": self._finished_effects(task_id)}
        if task.state == TaskState.DONE:
            raise ValueError("Task is already done")
        # Block new work first. Adapter failures remain visible rather than claiming success.
        if task.state not in {TaskState.PAUSED, TaskState.FAILED}:
            self.ledger.transition_task(task_id, TaskState.PAUSED)
        stopped = ["task:" + task_id]
        for resource in self.resources(task_id):
            if resource["state"] in {"active", "paused", "limit_paused"} and resource["kind"] in {"helper", "schedule"}:
                operation = "remove" if resource["kind"] == "schedule" else "stop"
                self._resource_action(resource, operation, "stopped")
                stopped.append(resource["kind"] + ":" + resource["id"])
        with self.ledger.transaction():
            checks = self.ledger.connection.execute("SELECT id FROM check_backs WHERE task_id=?", (task_id,)).fetchall()
            stopped += ["check_back:" + row[0] for row in checks]
            self.ledger.connection.execute("DELETE FROM check_backs WHERE task_id=?", (task_id,))
            self.ledger.connection.execute("UPDATE ping_queue SET cancelled=1 WHERE task_id=?", (task_id,))
            self.ledger.connection.execute("UPDATE questions SET answer='Task stopped' WHERE task_id=? AND answer IS NULL", (task_id,))
            self.ledger.transition_task(task_id, TaskState.CANCELLED)
            result = {"stopped": stopped, "finished_effects": self._finished_effects(task_id)}
            self._event("task_stopped", result, task_id)
        return result

    def _finished_effects(self, task_id):
        return [effect.delivery_id for effect in self.ledger.list_effects() if effect.task_id == task_id and effect.state == "sent"]

    def done(self, task_id, summary, evidence):
        if not summary.strip() or not evidence or any(not isinstance(x, str) or not x.strip() for x in evidence):
            raise ValueError("Done needs a summary and evidence")
        self._require_confirmed(task_id)
        with self.ledger.transaction():
            task = self.ledger.transition_task(task_id, TaskState.DONE, done_evidence=tuple(clean(evidence)))
            self.ledger.connection.execute("DELETE FROM check_backs WHERE task_id=?", (task_id,))
            self._event("task_done", {"summary": summary, "evidence": evidence, "citations": self._runtime(task_id)["citations"]}, task_id)
        return task

    def reopen(self, task_id):
        task = self.ledger.get_task(task_id)
        if task.state not in {TaskState.DONE, TaskState.FAILED, TaskState.BLOCKED, TaskState.PAUSED}:
            raise ValueError("Task is not closed or blocked")
        data = self._runtime(task_id)
        if data.get("in_flight"):
            raise StepBlocked("Interrupted step needs outcome reconciliation before retry")
        for failure in data["failures"].values():
            failure["retry_authorized"] = True
        self._save(task_id, data)
        self._restore(task_id)
        self._sync_limits()
        self._event("task_reopened", {}, task_id)
        return self.ledger.get_task(task_id)

    def _require_confirmed(self, task_id):
        task = self.ledger.get_task(task_id)
        if self._runtime(task_id)["confirmed_revision"] != task.card_revision:
            raise StepBlocked("Owner must confirm this card before work starts")
        return task

    def run_step(self, task_id, step, input_value, operation, *, action="read", sources=(), target="", content="",
                 approval_id=None, told=False, requires_model=False, other_allowed_route=False, route="default", paths=()):
        self._sync_limits()
        task = self._require_confirmed(task_id)
        data = self._runtime(task_id)
        if self._control("paused", False) or task.state not in {TaskState.WORKING, TaskState.WAITING_ON_OWNER, TaskState.BLOCKED}:
            raise StepBlocked("Task is not runnable")
        if data.get("in_flight"):
            raise StepBlocked("Step outcome is unknown; reconcile before retry")
        if task.state == TaskState.BLOCKED and task.blocked_reason == BlockedReason.HEALTH:
            raise StepBlocked("Task is blocked")
        self._require_scope(task_id, action, sources)
        guard = self.settings.path_guard()
        for path in paths:
            guard.check(path)
        if requires_model and self.model_blocked():
            raise StepBlocked("Model work is paused")
        if action == "code" and self.health and not self.health.status().get("docker", {}).get("ok", False):
            self._block(task_id, BlockedReason.HEALTH)
            raise StepBlocked("Docker is unavailable")
        fingerprint = sha256(encode([step, input_value, route]).encode()).hexdigest()
        failure = data["failures"].get(fingerprint)
        if failure and not failure.get("retry_authorized"):
            raise StepBlocked("Same failing step cannot retry without changed input or owner reopening")
        rules = self.settings.action_rules()
        requires_approval = rules.requires_approval(action) or action in self.ledger.get_card(task_id).approval_points
        if rules.level(action) == RuleLevel.WHEN_TOLD and not told:
            raise StepBlocked("This action requires an explicit owner instruction")
        if requires_approval:
            try:
                if not approval_id:
                    raise ApprovalRejected("Approval is required")
                self.approvals(task_id).consume(approval_id, action, target, content, task.card_revision)
            except ApprovalRejected:
                self._block(task_id, BlockedReason.PERMISSION)
                raise StepBlocked("Matching unused action approval is required") from None
        if task.state == TaskState.BLOCKED and task.blocked_reason == BlockedReason.PERMISSION:
            self._restore(task_id)
        # Claim durably before execution; a crash leaves an unknown outcome.
        data = self._runtime(task_id)
        data["in_flight"] = {"step": step, "fingerprint": fingerprint}
        if failure:
            data["failures"][fingerprint]["retry_authorized"] = False
        self._save(task_id, data)
        try:
            result = operation()
        except self.limit_errors:
            data = self._runtime(task_id)
            data.pop("in_flight", None)
            self._save(task_id, data)
            self.limit_reached()
            raise
        except Exception as error:
            self.record_failure(task_id, step, input_value, error, other_allowed_route=other_allowed_route, route=route)
            raise
        data = self._runtime(task_id)
        data.pop("in_flight", None)
        data["last_successful_step"] = step
        self._save(task_id, data)
        self._event("step_completed", {"step": step}, task_id)
        return {"result": clean(result), "citations": data["citations"]}

    def record_failure(self, task_id, step, input_value, cause, *, other_allowed_route=False, route="default"):
        data = self._runtime(task_id)
        fingerprint = sha256(encode([step, input_value, route]).encode()).hexdigest()
        count = data["failures"].get(fingerprint, {}).get("count", 0) + 1
        data["failures"][fingerprint] = {"count": count, "retry_authorized": False}
        data.pop("in_flight", None)
        report = {"cause": clean(str(cause)), "last_good_state": data.get("last_successful_step"),
                  "already_made": data["files"], "safe_to_retry": "A changed input or different allowed route",
                  "owner_action": "Reopen to authorize a retry, or supply corrected input", "step": step, "count": count}
        data["failure_report"] = report
        self._save(task_id, data)
        if count >= 2 and not other_allowed_route:
            self.ledger.transition_task(task_id, TaskState.FAILED)
        self._event("step_failed", report, task_id)
        return report

    def model_blocked(self):
        return self._control("provider_limited", False) or self.usage.cap_reached()

    def limit_reached(self):
        if not self._control("provider_limited", False):
            self._set_control("provider_limited", True)
            queue_ping(self.ledger, self.settings, self.clock, {"kind": "model_limit", "detail": "Model work paused; commands and probes remain available"})
            self._event("model_limit", {})
        self._sync_limits()

    def provider_available(self):
        self._set_control("provider_limited", False)
        self._sync_limits()

    def _sync_limits(self):
        blocked = self.model_blocked()
        capped = self.usage.cap_reached()
        if capped and not self._control("cap_notified", False):
            queue_ping(self.ledger, self.settings, self.clock, {"kind": "model_limit", "detail": "Daily token cap reached"})
            self._event("daily_token_cap", {"tokens": self.usage.daily_tokens()})
        self._set_control("cap_notified", capped)
        for task in self.ledger.list_tasks():
            if blocked and task.state in {TaskState.WORKING, TaskState.WAITING_ON_OWNER}:
                self._block(task.id, BlockedReason.MODEL_LIMIT)
            elif not blocked and task.state == TaskState.BLOCKED and task.blocked_reason == BlockedReason.MODEL_LIMIT:
                self._restore(task.id)
            for resource in self.resources(task.id, "schedule"):
                if not resource["data"].get("requires_model", True):
                    continue
                if blocked and resource["state"] == "active":
                    self._resource_action(resource, "pause", "limit_paused")
                elif not blocked and not self._control("paused", False) and resource["state"] == "limit_paused" and task.state not in TERMINAL:
                    self._resource_action(resource, "resume", "active")

    def complete_structured(self, schema, prompt):
        self._sync_limits()
        if self._control("paused", False) or self.model_blocked():
            raise UsageLimitError("Model work is paused")
        if self.model is None:
            raise StepBlocked("No model interface configured")
        before = getattr(self.model, "tokens", 0)
        try:
            return clean(self.model.complete_structured(schema, clean(prompt)))
        except self.limit_errors:
            self.limit_reached()
            raise
        finally:
            tokens = max(0, getattr(self.model, "tokens", before) - before)
            self.usage.record(tokens=tokens)
            self._sync_limits()

    def missed_runs(self):
        with self.ledger.transaction():
            return [dict(row) for row in self.ledger.connection.execute("SELECT * FROM missed_runs ORDER BY scheduled_at,id")]

    def tick(self, *, due_runs=()):
        health = self.health.tick() if self.health else {}
        if self._control("provider_limited", False) and health.get("model", {}).get("ok"):
            self.provider_available()
        self._sync_limits()
        for task in self.ledger.list_tasks():
            if task.state == TaskState.BLOCKED and task.blocked_reason == BlockedReason.HEALTH and health.get("docker", {}).get("ok"):
                self._restore(task.id)
        due = []
        with self.ledger.transaction():
            for run in due_runs:
                task_id, job_id, scheduled_at = run["task_id"], run["job_id"], run["scheduled_at"]
                if self.model_blocked() or self._control("paused", False):
                    reason = "model_limit" if self.model_blocked() else "paused"
                    self.ledger.record_missed_run(MissedRun(uuid4().hex, task_id, job_id, stamp(datetime.fromisoformat(scheduled_at)), reason))
            for question in self.pending_questions():
                task = self.ledger.get_task(question["task_id"])
                if task.state in TERMINAL or task.state == TaskState.PAUSED or self._control("paused", False):
                    continue
                if now(self.clock) < datetime.fromisoformat(question["asked_at"]) + timedelta(seconds=self.settings.get("nudge_delay_seconds")):
                    continue
                inserted = self.ledger.connection.execute("INSERT INTO question_nudges VALUES (?) ON CONFLICT DO NOTHING", (question["id"],))
                if inserted.rowcount:
                    queue_ping(self.ledger, self.settings, self.clock, {"kind": "nudge", **question},
                               task_id=question["task_id"], ping_id="nudge:" + question["id"])
            rows = self.ledger.connection.execute("SELECT * FROM check_backs WHERE due_at<=? ORDER BY due_at,id", (stamp(now(self.clock)),)).fetchall()
            for row in rows:
                task = self.ledger.get_task(row["task_id"])
                if task.state in TERMINAL:
                    self.ledger.connection.execute("DELETE FROM check_backs WHERE id=?", (row["id"],))
                    continue
                scope = self.scope(task.id)
                self._event("scope_recheck", {**scope, "reason": row["reason"], "changed_only": True}, task.id)
                if self.model_blocked() or self._control("paused", False) or task.state == TaskState.PAUSED:
                    self.ledger.record_missed_run(MissedRun(uuid4().hex, task.id, "check_back:" + row["id"], row["due_at"],
                                                           "model_limit" if self.model_blocked() else "paused"))
                elif task.state in {TaskState.WORKING, TaskState.WAITING_ON_OWNER}:
                    due.append({**dict(row), "scope": scope})
                self.ledger.connection.execute("DELETE FROM check_backs WHERE id=?", (row["id"],))
        pings = release_pings(self.ledger, self.settings, self.clock, self.owner_channel)
        return {"check_backs": due, "health": health, "pings": pings, "missed_runs": self.missed_runs()}

    def status(self):
        tasks = []
        for task in self.ledger.list_tasks():
            data = self._runtime(task.id)
            with self.ledger.transaction():
                check = self.ledger.connection.execute("SELECT due_at,reason FROM check_backs WHERE task_id=? ORDER BY due_at LIMIT 1", (task.id,)).fetchone()
            tasks.append({"id": task.id, "state": task.state, "revision": task.card_revision, "blocked_reason": task.blocked_reason,
                          "interrupted": task.interrupted, "last_successful_step": data.get("last_successful_step"),
                          "waiting_on": self.pending_questions(task.id), "next_action": check["reason"] if check else None,
                          "next_time": check["due_at"] if check else None, "files": data["files"],
                          "failure_report": data.get("failure_report"), "unknown_step": data.get("in_flight")})
        return {"tasks": tasks, "health": self.health.status() if self.health else {}, "usage": self.usage.summary(),
                "missed_runs": self.missed_runs(), "unknown_deliveries": [e.delivery_id for e in self.ledger.list_effects() if e.state == "unknown"]}

    def digest(self, start=None, end=None):
        return {"pending_questions": self.pending_questions(), "missed_runs": self.missed_runs(),
                "health": self.health.status() if self.health else {}, "usage": self.usage.summary(start, end)}
