"""One isolated runtime connecting plain domain services to Hermes."""

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
from zoneinfo import ZoneInfo

from hermes_dots.brain import Brain
from hermes_dots.engine.coworker import CoworkerEngine
from hermes_dots.engine.support import clean, queue_ping
from hermes_dots.health.monitor import HealthMonitor
from hermes_dots.inbox.sources import FixtureMailSource
from hermes_dots.inbox.service import EmailResponsibility
from hermes_dots.ledger import Ledger
from hermes_dots.security.paths import PathGuard
from hermes_dots.settings.service import SettingsService
from hermes_dots.slack.bridge import SlackBridge
from hermes_dots.slack.digest import DigestComposer
from .hooks import RuntimeHooks
from .model import HermesHelpers, HermesModel
from .scheduler import HermesScheduler


def data_root():
    value = os.environ.get("HERMES_DOTS_HOME")
    if not value:
        raise RuntimeError("HERMES_DOTS_HOME is required; refusing the default Hermes home")
    return PathGuard().check(value)


class DotsRuntime:
    def __init__(self, root=None, *, ctx=None, cron_api=None, probes=None):
        self.root = PathGuard().check(root if root is not None else data_root())
        self.config = json.loads((self.root / "local-config.json").read_text())
        self.guard = PathGuard(self.config.get("extra_denied_paths", []))
        self.guard.check(self.root)
        self.brain = Brain(self.root)
        self.ledger = Ledger(self.root / "state/ledger.sqlite")
        self.model = HermesModel(ctx.llm) if ctx is not None else None
        self.settings = SettingsService(self.ledger, model=self.model, feedback=self._setting_feedback)
        # Local setup seeds only unset values; subsequent chat settings persist.
        with self.ledger.transaction():
            for key in ("timezone", "extra_denied_paths"):
                value = self.config.get(key)
                if value is not None:
                    self.ledger.connection.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT DO NOTHING", (key, json.dumps(value)))
        self.scheduler = HermesScheduler(self.ledger, cron_api)
        self.helpers = HermesHelpers(ctx.subagent_lifecycle) if ctx is not None else None
        self.delivery = None
        channel = self.config.get("owner_channel", self.config.get("owner_slack_id", "owner"))
        row = self.ledger.connection.execute("SELECT value FROM engine_control WHERE name='dots_owner_channel'").fetchone()
        if row:
            channel = json.loads(row[0])
        self.health = HealthMonitor(self.ledger, self.settings, probes or self._probes(),
                                    evidence=self.brain.evidence, owner_channel=channel)
        self.engine = CoworkerEngine(self.ledger, self.settings, self.brain, model=self.model, health=self.health,
                                     scheduler=self.scheduler, helpers=self.helpers, evidence=self.brain.evidence,
                                     owner_channel=channel)
        self.hooks = RuntimeHooks(self, guard=self.guard)
        self.bridge = SlackBridge(self.config["owner_slack_id"], log_event=self._slack_log, handler=None)
        self.digest = DigestComposer()
        fixture = self.root / "practice-mail.json"
        source = FixtureMailSource(json.loads(fixture.read_text()) if fixture.exists() else [])
        self.mail = EmailResponsibility(self.ledger, source, self.engine, self.settings, self.brain,
                                        account="practice", owner_address="you@example.com", engine=self.engine,
                                        owner_channel=channel)
        self.mail.register_digest(self.digest)
        for section in ("learned", "conflicts", "promotions", "proposals"):
            self.digest.register(section, lambda s=section: self.brain.digest_items()[s])
        self.digest.register("questions", self.engine.pending_questions)
        self.digest.register("missed_runs", self.engine.missed_runs)
        self.digest.register("health", self.health.status)
        self.digest.register("usage", self.engine.usage.summary)

    def owner_channel(self, channel):
        # Slack receives a U… member ID at install, but real DM events use D…
        # channel IDs. Keep deliveries and restored card bindings in that lane.
        self.engine._set_control("dots_owner_channel", channel)
        self.engine.owner_channel = self.health.owner_channel = self.mail.owner_channel = channel
        transport = getattr(self, "transport", None)
        if transport:
            transport.channel = channel

    def _setting_feedback(self, value):
        self.brain.record_feedback("setting_change", json.dumps(value))
        if value.get("name") == "timezone":
            # The pinned cron clock reads top-level timezone, separate from dots settings.
            profile = self.guard.check(self.root / "profiles/orca")
            path = profile / "config.yaml"
            if path.exists():
                # Installer emits JSON-compatible YAML. Preserve every unrelated key.
                config = json.loads(path.read_text())
                config["timezone"] = value["value"]
                temporary = profile / "config.yaml.dots-tmp"
                temporary.write_text(json.dumps(config, indent=2) + "\n")
                temporary.chmod(0o600)
                temporary.replace(path)
            try:
                from hermes_time import reset_cache
                reset_cache()
            except ImportError:
                pass

    def close(self): self.ledger.close()

    def _slack_log(self, kind, payload):
        self.brain.evidence.append({"kind": kind, **clean(payload)})

    def _probes(self):
        def docker():
            executable = shutil.which("docker")
            return bool(executable) and subprocess.run([executable, "info"], capture_output=True, timeout=5).returncode == 0
        return {"docker": docker, "disk": lambda: shutil.disk_usage(self.root).free > 100 * 1024 * 1024,
                "scheduler": lambda: bool(self.scheduler.list()), "mail": lambda: True,
                # These are local capability checks, never provider/network requests.
                "slack": lambda: {"ok": False, "detail": "Slack connection not verified by a transport receipt"},
                "model": lambda: {"ok": False, "detail": "Model availability awaits a successful configured completion"}}

    def consume_approval(self, binding, tool_name, action, target, payload):
        task_id, revision = binding
        task = self.ledger.get_task(task_id)
        if task.card_revision != revision: raise PermissionError("Card revision changed")
        approvals = self.engine.approvals(task_id)
        with self.ledger.transaction():
            rows = self.ledger.connection.execute("SELECT id FROM approvals WHERE task_id=? AND action=? AND used=0", (task_id, action)).fetchall()
        for row in rows:
            try:
                approvals.consume(row["id"], action, target, payload, revision)
                return
            except PermissionError:
                continue
        raise PermissionError("A matching unused approval is required")

    def brain_write(self, args, *, session_id=""):
        if not session_id or self.hooks.helper(session_id):
            raise PermissionError("Only primary sessions may request brain writes")
        return self.brain.learn(args["text"], source_task=args.get("task_id"), actor="orca")

    def task(self, args, *, session_id=""):
        if self.hooks.helper(session_id): raise PermissionError("Helpers cannot operate owner tasks")
        operation = args["operation"]
        values = {k: v for k, v in args.items() if k != "operation"}
        allowed = {"intake", "schedule_check_back", "ask_question", "done", "status", "instruction"}
        if operation not in allowed:
            raise PermissionError("Owner confirmations and controls must arrive through Slack")
        task_id = values.get("task_id")
        if task_id:
            task = self.ledger.get_task(task_id)
            self.hooks.sessions[session_id] = (task.id, task.card_revision)
        result = getattr(self.engine, operation)(**values)
        if operation == "intake" and getattr(self, "transport", None): self.transport.card(result)
        return result

    def settings_tool(self, args):
        if args.get("text"):
            transport = getattr(self, "transport", None)
            if transport is None:
                raise RuntimeError("Slack must be connected to confirm a setting change")
            proposal = self.settings.parse_change(args["text"])
            transport.settings_card(proposal)
            return proposal
        return self.settings.list_settings()

    def route(self, route):
        if route.kind == "command":
            name, argument = route.command.name, route.command.argument
            if name == "settings": return self.settings.list_settings()
            if name == "rules": return self.settings.get("action_rules")
            if name == "schedules": return self.scheduler.list()
            if name == "forget": return self.brain.forget(argument)
            if name == "stop": return self.engine.stop(argument.lstrip("#"))
            return getattr(self.engine, name)()
        if route.kind == "instruction":
            task_id, revision = route.binding.task_id, route.binding.revision
            transport = getattr(self, "transport", None)
            answer = route.text.strip().lower()
            if transport and transport.onboarding_binding() == (task_id, revision):
                return {"reply": transport.answer_onboarding(answer)}
            if task_id.startswith("SETTINGS-"):
                if answer not in {"yes", "confirm", "no", "cancel"}:
                    return {"reply": "Reply yes to confirm, or no to cancel this setting change."}
                return self.setting_decision(task_id.removeprefix("SETTINGS-"), answer in {"yes", "confirm"})
            if task_id.startswith("MAIL-"):
                return {"reply": "Use this mail card's buttons to change its state."}
            task = self.ledger.get_task(task_id)
            if task.card_revision != revision:
                raise ValueError("Reply in the current card's thread")
            questions = self.engine.pending_questions(task_id)
            if len(questions) == 1:
                return self.engine.answer_question(questions[0]["id"], route.text)
            if questions:
                return {"reply": "Which pending question on this card are you answering?"}
            result = self.engine.instruction(route.text, task_id=task_id, revision=revision)
            if transport:
                transport.card(result)
            return result
        if route.kind == "action":
            value = route.value or {}
            task_id, revision = route.binding.task_id, route.binding.revision
            transport = getattr(self, "transport", None)
            if transport and transport.onboarding_binding() == (task_id, revision) and route.action in {"approve", "reject"}:
                return {"reply": transport.answer_onboarding("yes" if route.action == "approve" else "no")}
            if task_id.startswith("SETTINGS-"):
                proposal_id = task_id.removeprefix("SETTINGS-")
                if not isinstance(value, dict) or value.get("proposal_id") != proposal_id or route.action not in {"approve", "reject"}:
                    raise PermissionError("Use the bound setting proposal")
                return self.setting_decision(proposal_id, route.action == "approve")
            if task_id.startswith(("MAIL-", "IDEA-")):
                result = self.mail.handle_action("dots_" + route.action, {
                    "task_id": task_id, "revision": revision, "value": value})
                if route.action in {"make_vip", "remove_vip"} and transport:
                    transport.settings_card(result)
                if transport:
                    transport.drain()
                return result
            task = self.ledger.get_task(route.binding.task_id)
            if task.card_revision != route.binding.revision: raise PermissionError("Stale card action")
            if route.action == "approve":
                if value.get("action"):
                    return self.engine.approvals(task.id).grant(value["action"], value["target"], value["content"], task.card_revision)
                return self.engine.confirm(task.id, task.card_revision)
            if route.action == "reject": return self.brain.record_feedback("rejection", str(value))
            if route.action == "undo_learned": return self.brain.undo(value["note_id"])
            if route.action == "pick_version": return self.brain.promote(value["note_id"], owner_approved=True)
            return self.mail.handle_action("dots_" + route.action, value)

    def setting_decision(self, proposal_id, confirm):
        if not confirm:
            return self.settings.cancel_change(proposal_id)
        row = self.ledger.connection.execute("SELECT name FROM setting_proposals WHERE id=?", (proposal_id,)).fetchone()
        if row and row["name"] == "vips":
            result = self.mail.confirm_vip(proposal_id)
        else:
            result = self.settings.confirm_change(proposal_id)
        transport = getattr(self, "transport", None)
        if transport:
            transport.drain()
        return result

    def _claim_slot(self, kind, slot):
        key = "dots_slot:" + kind
        with self.ledger.transaction():
            row = self.ledger.connection.execute("SELECT value FROM engine_control WHERE name=?", (key,)).fetchone()
            if row and json.loads(row["value"]) == slot: return False
            self.ledger.connection.execute("INSERT INTO engine_control VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (key, json.dumps(slot)))
            return True

    def tick(self, *, now=None):
        """Script tick does local work and queues model work; it never invokes a model."""
        now = now or datetime.now(timezone.utc)
        zone = self.settings.get("timezone")
        local = now.astimezone(ZoneInfo(zone)) if zone else now.astimezone()
        self.scheduler.capture_missed(now=now, limited=self.engine.model_blocked())
        result = self.engine.tick()
        for check in result["check_backs"]:
            self.ledger.record_event("check_back_ready", clean(check), task_id=check["task_id"])
        wall, day = local.strftime("%H:%M"), local.date().isoformat()
        if wall in self.settings.get("index_times") and self._claim_slot("index", day + wall):
            self.brain.index.incremental_pass()
        if wall in self.settings.get("digest_times") and self._claim_slot("digest", day + wall):
            queue_ping(self.ledger, self.settings, None, {"kind": "digest", **self.digest.compose()}, ping_id="digest:" + day + wall)
        if wall == "22:00" and self._claim_slot("reflection", day):
            self.ledger.record_event("reflection_ready", {"day": day, "proposals_only": True})
        if self._claim_slot("retention", day): self.brain.evidence.retain(self.settings.get("log_retention_days"))
        # Mail classification needs the primary model facade. Script records readiness only.
        interval = self.settings.get("mail_check_interval_seconds")
        slot = str(int(now.timestamp()) // interval)
        if self._claim_slot("mail", slot):
            self.ledger.record_event("mail_check_ready", {"interval_seconds": interval})
        self.brain.evidence.append({"kind": "dots_tick", "result": result})
        return result


def json_result(value):
    if is_dataclass(value): value = asdict(value)
    return json.dumps(clean(value), ensure_ascii=False, default=str)
