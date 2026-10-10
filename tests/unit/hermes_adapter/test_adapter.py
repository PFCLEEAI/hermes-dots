import ast
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import inspect
import json
import os
import subprocess
import shutil
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from hermes_dots.hermes_adapter.contracts import HOOK_PAYLOADS, CRON_CREATE_FIELDS
from hermes_dots.hermes_adapter.hooks import HOOKS
from hermes_dots.hermes_adapter.model import HermesModel
from hermes_dots.hermes_adapter.runtime import DotsRuntime
from hermes_dots.settings.defaults import SETTINGS_DEFAULTS


class CronStub:
    """Only the recorded pin API; no live Hermes scheduler required."""
    def __init__(self): self.jobs, self.calls = [], []
    def create_job(self, **kwargs):
        assert set(kwargs) <= set(CRON_CREATE_FIELDS)
        job = {"id": f"job-{len(self.jobs)}", "next_run_at": "2026-10-11T09:00:00+00:00",
               **kwargs, "schedule": {"kind": "interval", "minutes": 5}}
        self.jobs.append(job)
        return job
    def list_jobs(self, include_disabled=False): return self.jobs
    def pause_job(self, job_id): self.calls.append(("pause", job_id))
    def resume_job(self, job_id): self.calls.append(("resume", job_id))
    def remove_job(self, job_id): self.calls.append(("remove", job_id))
    def compute_next_run(self, schedule, last_run_at=None):
        return (datetime.fromisoformat(last_run_at) + timedelta(minutes=schedule["minutes"])).isoformat()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    root = tmp_path / "dots"
    root.mkdir()
    (root / "local-config.json").write_text(json.dumps({"owner_slack_id": "OWNER", "timezone": "UTC", "extra_denied_paths": []}))
    value = DotsRuntime(root, cron_api=CronStub(), probes={name: lambda: True for name in ("mail", "slack", "docker", "model", "scheduler", "disk")})
    yield value
    value.close()


@contextmanager
def memory_contract_stub():
    """The pin's abstract API stub, local to these tests, never a production fallback."""
    from abc import ABC, abstractmethod
    class MemoryProvider(ABC):
        @property
        @abstractmethod
        def name(self): pass
        @abstractmethod
        def is_available(self): pass
        @abstractmethod
        def initialize(self, session_id, **kwargs): pass
        @abstractmethod
        def get_tool_schemas(self): pass
    agent, provider = ModuleType("agent"), ModuleType("agent.memory_provider")
    provider.MemoryProvider = MemoryProvider
    previous = {key: sys.modules.get(key) for key in ("agent", "agent.memory_provider", "hermes_dots.hermes_adapter.memory")}
    sys.modules.update({"agent": agent, "agent.memory_provider": provider})
    sys.modules.pop("hermes_dots.hermes_adapter.memory", None)
    try: yield
    finally:
        for key, value in previous.items():
            if value is None: sys.modules.pop(key, None)
            else: sys.modules[key] = value


def test_hook_signatures_and_wiring(runtime):
    assert set(HOOKS) == set(HOOK_PAYLOADS)
    for name, fields in HOOK_PAYLOADS.items():
        signature = inspect.signature(getattr(runtime.hooks, name))
        signature.bind(**{field: {} if field == "args" else "example" for field in fields})
    for path in Path("hermes_dots").rglob("*.py"):
        if path.name == "defaults.py": continue
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DEFAULTS" for t in node.targets):
                for key, value in ast.literal_eval(node.value).items(): assert SETTINGS_DEFAULTS[key] == value


@pytest.mark.parametrize("name,args", [
    ("read_file", {"path": "~/.hermes/config.yaml"}),
    ("terminal", {"command": "cat ~/.hermes/config.yaml"}),
    ("terminal", {"command": "cat $HOME/.hermes/config.yaml"}),
    ("patch", {"patch": "*** Update File: ~/.hermes/config.yaml\n"}),
    ("send_message", {"target": "Acme", "content": "hello"}),
    ("slack", {"action": "post", "channel": "Acme"}),
])
def test_native_guards(runtime, name, args):
    assert runtime.hooks.pre_tool_call(name, args, session_id="primary")["action"] == "block"


def test_symlink_and_live_denials(runtime, tmp_path):
    denied = tmp_path / "denied"
    denied.mkdir()
    (runtime.root / "workspace").mkdir(exist_ok=True)
    (runtime.root / "workspace/link").symlink_to(denied, target_is_directory=True)
    proposal = runtime.settings.propose_change("extra_denied_paths", [str(denied)])
    runtime.settings.confirm_change(proposal)
    assert runtime.hooks.pre_tool_call("read_file", {"path": "link/file"})["action"] == "block"
    assert runtime.hooks.pre_tool_call("read_file", {"path": "allowed.txt"}) is None


def test_helpers_cannot_write_even_after_stop(runtime):
    runtime.hooks.subagent_start("parent", "child", child_role="leaf", child_goal="Acme")
    runtime.hooks.subagent_stop("parent", "child", child_status="completed")
    assert not runtime.hooks.active_helpers
    with pytest.raises(PermissionError): runtime.brain_write({"text": "Acme lesson"}, session_id="child")
    assert runtime.hooks.pre_tool_call("dots_brain_write", {"text": "Acme lesson"}, session_id="child")["action"] == "block"


def test_approval_is_exact_and_single_use(runtime):
    task = runtime.engine.intake("Acme task", allowed_actions=["post"])
    task_id = task["id"] if isinstance(task, dict) else task.id
    runtime.engine.confirm(task_id, 1)
    runtime.hooks.sessions["primary"] = (task_id, 1)
    args = {"target": "Acme", "content": "hello"}
    runtime.engine.approvals(task_id).grant("post", "Acme", json.dumps(args, sort_keys=True), 1)
    assert runtime.hooks.pre_tool_call("slack_post", {**args, "content": "changed"}, session_id="primary")["action"] == "block"
    assert runtime.hooks.pre_tool_call("slack_post", args, session_id="primary") is None
    assert runtime.hooks.pre_tool_call("slack_post", args, session_id="primary")["action"] == "block"


def test_redacted_tool_results_and_evidence(runtime):
    token = "xoxb-" + "1234567890-abcdef"
    assert token not in runtime.hooks.transform_tool_result("read_file", {}, token)
    runtime.hooks.post_tool_call(tool_name="read_file", args={}, result=token)
    runtime.hooks.post_llm_call(user_message=token, assistant_response="hello")
    runtime.hooks.on_session_end(session_id="primary", completed=True)
    assert token not in "".join(p.read_text() for p in (runtime.root / "evidence").glob("*.jsonl"))
    assert runtime.hooks.pre_gateway_dispatch({"user": "OTHER", "text": "pause"})["action"] == "skip"


def test_provider_layout_bounded_recall(runtime, monkeypatch):
    monkeypatch.setenv("HERMES_DOTS_HOME", str(runtime.root))
    with memory_contract_stub():
        from hermes_dots.hermes_adapter.memory import BrainMemoryProvider
        provider = BrainMemoryProvider()
        provider.initialize("primary", hermes_home=str(runtime.root / "profiles/orca"))
        runtime.brain.learn("Acme has a verified milestone", actor="owner")
        recalled = provider.prefetch("Acme", session_id="primary")
        assert "Acme" in recalled and "brain://" in recalled and len(recalled) <= 4000
        provider.initialize("child", hermes_home=str(runtime.root / "profiles/orca"), agent_context="subagent")
        before = list((runtime.root / "evidence").glob("*.jsonl"))[0].read_bytes()
        provider.sync_turn("child", "result", session_id="child")
        assert list((runtime.root / "evidence").glob("*.jsonl"))[0].read_bytes() == before
        entry = Path("plugins/memory/hermes_dots_brain/__init__.py")
        assert "register_memory_provider" in entry.read_text()


def test_structured_model_uses_pin_kwargs_and_redaction():
    calls = []
    def complete_structured(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(parsed={"answer": "Acme"}, usage={"total_tokens": 4})
    model = HermesModel(SimpleNamespace(complete_structured=complete_structured))
    assert model.complete_structured({"type": "object"}, "Quoted Acme") == {"answer": "Acme"}
    assert calls[0]["input"] == [{"type": "text", "text": "Quoted Acme"}]
    assert "tools" not in calls[0] and model.tokens == 4


def test_tick_no_agent_and_missed_dedup(runtime):
    task = runtime.engine.intake("Acme schedule")
    task_id = task["id"] if isinstance(task, dict) else task.id
    runtime.engine.confirm(task_id, 1)
    job = runtime.scheduler.create(runtime.engine, task_id, "Check Acme", "5m", "UTC")
    tick = runtime.scheduler.ensure_tick("tick.sh", runtime.root / "workspace")
    assert tick["no_agent"] is True and tick["script"] == "tick.sh"
    assert runtime.scheduler.ensure_tick("tick.sh", runtime.root / "workspace") is tick
    runtime.engine.limit_reached()
    runtime.scheduler.capture_missed(now=datetime(2026, 10, 11, 9, 11, tzinfo=timezone.utc), limited=True)
    runtime.scheduler.capture_missed(now=datetime(2026, 10, 11, 9, 11, tzinfo=timezone.utc), limited=True)
    assert len(runtime.engine.missed_runs()) == 3
    assert ("pause", job["id"]) in runtime.scheduler.api.calls


def test_registration_discovers_tools_hooks_provider(runtime, monkeypatch):
    monkeypatch.setenv("HERMES_DOTS_HOME", str(runtime.root))
    monkeypatch.setenv("HERMES_HOME", str(runtime.root / "profiles/orca"))
    with memory_contract_stub():
        from hermes_dots.hermes_adapter import register as module
        monkeypatch.setattr(module, "DotsRuntime", lambda **kwargs: runtime)
        class Context:
            def __init__(self): self.tools, self.hooks, self.actions, self.provider = {}, {}, {}, None
            def register_tool(self, **kwargs): self.tools[kwargs["name"]] = kwargs
            def register_hook(self, name, callback): self.hooks[name] = callback
            def register_memory_provider(self, provider): self.provider = provider
            def register_slack_action_handler(self, action_id, callback): self.actions[action_id] = callback
            def on_unload(self, callback): self.cleanup = callback
            def register_platform_handler(self, name, callback): self.platform = (name, callback)
        context = Context()
        assert module.register(context) is runtime
        assert set(context.tools) == set(module.TOOLS)
        assert set(context.hooks) == set(HOOK_PAYLOADS)
        assert context.provider.name == "hermes_dots_brain"
        assert {"dots_make_vip", "dots_approve"} <= set(context.actions)
        assert json.loads(context.tools["dots_settings"]["handler"]({}))["mail_check_interval_seconds"] == 300


def test_real_discovery(tmp_path):
    python = os.environ.get("HERMES_DOTS_PIN_PYTHON")
    if not python:
        pytest.skip("Real pin test requires HERMES_DOTS_PIN_PYTHON (Python 3.11-3.13 venv with editable pinned Hermes)")
    root = tmp_path / "dots"
    profile = root / "profiles/orca"
    plugin = profile / "plugins/hermes-dots"
    plugin.mkdir(parents=True)
    shutil.copyfile("plugin.yaml", plugin / "plugin.yaml")
    (plugin / "__init__.py").write_text("from hermes_dots.hermes_adapter.register import register\n")
    shutil.copytree("plugins/memory/hermes_dots_brain", profile / "plugins/hermes_dots_brain")
    (root / "local-config.json").write_text(json.dumps({"owner_slack_id": "OWNER", "timezone": "UTC", "extra_denied_paths": []}))
    (profile / "config.yaml").write_text(json.dumps({"memory": {"provider": "hermes_dots_brain"},
        "plugins": {"enabled": ["hermes-dots", "hermes_dots_brain"]}}))
    script = """
import socket
socket.socket.connect = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('No network in discovery test'))
import hermes_cli, sys
from pathlib import Path
# Match the stock CLI bootstrap; editable namespace packages need the source root.
sys.path.insert(0, str(Path(hermes_cli.__file__).resolve().parent.parent))
from hermes_cli.plugins import PluginManager
manager = PluginManager()
manager.discover_and_load()
assert 'hermes-dots' in manager._plugins, manager._plugins
assert not manager._plugins['hermes-dots'].error, manager._plugins['hermes-dots'].error
assert 'dots_brain_write' in manager._plugin_tool_names, manager._plugin_tool_names
assert 'pre_tool_call' in manager._hooks
assert any(item[0] == 'dots_approve' for item in manager._slack_action_handlers)
from plugins.memory import find_provider_dir, load_memory_provider
assert find_provider_dir('hermes_dots_brain')
provider = load_memory_provider('hermes_dots_brain')
assert provider is not None and provider.name == 'hermes_dots_brain'
import os
provider.initialize('primary', hermes_home=os.environ['HERMES_HOME'])
assert provider.prefetch('Acme') == ''
provider.shutdown()
manager.unload()
print('Pinned tools/hooks/provider discovered')
"""
    env = {**os.environ, "HERMES_HOME": str(profile), "HERMES_DOTS_HOME": str(root),
           "HERMES_KANBAN_HOME": str(root / "kanban"), "PYTHONPATH": str(Path.cwd()),
           "HERMES_ENABLE_PROJECT_PLUGINS": "false", "TMPDIR": str(tmp_path), "HOME": str(tmp_path / "user")}
    result = subprocess.run([python, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'Pinned tools/hooks/provider discovered' in result.stdout


def test_script_tick_has_no_model_calls_and_interval(runtime):
    class Model:
        def complete_structured(self, *args, **kwargs): raise AssertionError("no model in script tick")
    runtime.engine.model = Model()
    moment = datetime(2026, 10, 11, 8, 30, tzinfo=timezone.utc)
    runtime.tick(now=moment)
    runtime.tick(now=moment + timedelta(seconds=60))
    events = runtime.ledger.list_events()
    assert sum(e.kind == "mail_check_ready" for e in events) == 1


def test_primary_command_remains_available_during_limit(runtime):
    runtime.engine.limit_reached()
    response = runtime.hooks.pre_gateway_dispatch({"user": "OWNER", "channel": "Acme", "text": "status"})
    assert response["action"] == "skip"
    assert any(e.kind == "slack_owner_message" for e in runtime.ledger.list_events()) is False
    assert list(runtime.ledger.connection.execute("SELECT * FROM ping_queue"))


def test_slack_drain_uses_real_domain_mail_and_binds_card(runtime):
    from hermes_dots.hermes_adapter.slack import SlackRuntime
    from hermes_dots.slack.delivery import QuietHours, QuietHoursDispatcher
    from tests.fakes.slack import FakeSlack
    slack = FakeSlack()
    transport = SlackRuntime(runtime, SimpleNamespace(inject_message=lambda *a, **k: False), SimpleNamespace())
    transport.dispatcher = QuietHoursDispatcher(runtime.ledger, slack, quiet_hours=QuietHours("23:00", "08:00"),
                                               now=lambda: datetime(2026, 10, 11, 12, tzinfo=timezone.utc))
    card = runtime.engine.intake("Acme task")
    effect = transport.card(card)
    assert effect.receipt
    assert runtime.bridge.threads[(effect.channel, effect.receipt)].task_id == card.id
    runtime.ledger.record_event("mail_check_ready", {})
    transport.drain()
    assert runtime.engine._control("dots_gateway_cursor") > 0


def test_timezone_change_updates_pinned_cron_clock(runtime):
    profile = runtime.root / "profiles/orca"
    profile.mkdir(parents=True)
    config = profile / "config.yaml"
    config.write_text(json.dumps({"timezone": "UTC", "memory": {"provider": "hermes_dots_brain"}}))
    proposal = runtime.settings.propose_change("timezone", "Europe/London")
    runtime.settings.confirm_change(proposal)
    actual = json.loads(config.read_text())
    assert actual["timezone"] == "Europe/London"
    assert actual["memory"]["provider"] == "hermes_dots_brain"
