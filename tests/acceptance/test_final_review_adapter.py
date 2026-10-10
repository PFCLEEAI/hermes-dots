"""Final-review regressions through registered tools, actions and gateway hooks."""

import asyncio
from dataclasses import dataclass
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from hermes_dots.hermes_adapter.slack import SlackRuntime
from hermes_dots.inbox.service import MailCardReconciler
from .invariants import assert_invariants


@pytest.fixture
def adapter(scenario, monkeypatch):
    s = scenario
    from hermes_dots.hermes_adapter import register as module
    # Only the unavailable Hermes discovery shell is stubbed; all domain services
    # and callback bodies below are the running product implementations.
    memory = ModuleType("hermes_dots.hermes_adapter.memory")
    memory.BrainMemoryProvider = lambda: SimpleNamespace(name="hermes_dots_brain")
    monkeypatch.setitem(sys.modules, memory.__name__, memory)
    monkeypatch.setattr(module, "DotsRuntime", lambda **kwargs: s.runtime)
    profile = s.root / "profiles/orca"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))

    class Context:
        def __init__(self):
            self.tools, self.actions, self.hooks = {}, {}, {}
        def register_tool(self, **kwargs): self.tools[kwargs["name"]] = kwargs["handler"]
        def register_hook(self, name, callback): self.hooks[name] = callback
        def register_slack_action_handler(self, name, callback): self.actions[name] = callback
        def register_memory_provider(self, provider): self.provider = provider
        def register_platform_handler(self, name, callback): self.platform = callback
        def on_unload(self, callback): self.cleanup = callback

    ctx = Context()
    assert module.register(ctx) is s.runtime

    @dataclass
    class Source:
        user_id: str
        chat_id: str
        thread_id: str | None
        chat_type: str = "dm"
        profile: str | None = "custom"

    @dataclass
    class Event:
        text: str
        source: Source
        raw_message: dict

    class Adapter:
        def tool(self, name, args, *, session_id="primary"):
            return json.loads(ctx.tools[name](args, session_id=session_id))

        def message(self, text, *, thread_ts=None, owner=True, synthetic=False):
            source = Source(s.slack.owner_id if owner else "U_OTHER", s.slack.channel,
                            "fresh-message-ts" if synthetic else thread_ts)
            raw = {"thread_ts": thread_ts} if thread_ts else {}
            return ctx.hooks["pre_gateway_dispatch"](
                Event(text, source, raw),
                gateway=SimpleNamespace(_session_key_for_source=lambda source: "agent:main:slack:dm:Acme"))

        def click(self, ts, action_id, *, owner=True):
            event = s.slack.click(ts, action_id, owner=owner)
            async def ack(): pass
            return asyncio.run(ctx.actions[action_id](ack, event, event["action"]))

    return Adapter()


def proposal_post(s, proposal):
    return next(post for post in s.slack.posts if "#SETTINGS-" + proposal["id"] in post["text"])


def test_settings_buttons_and_thread_yes_no_use_registered_adapter(adapter, scenario):
    s = scenario
    proposal = adapter.tool("dots_settings", {"text": "make quiet hours 22 to 7"})
    post = proposal_post(s, proposal)
    assert [button["text"]["text"] for button in post["buttons"]] == ["Confirm", "Cancel"]
    assert json.loads(post["buttons"][0]["value"])["value"]["proposal_id"] == proposal["id"]
    assert s.settings.get("quiet_hours") == ["23:00", "08:00"]
    adapter.click(post["ts"], "dots_approve", owner=False)
    assert s.settings.get("quiet_hours") == ["23:00", "08:00"]
    stored = adapter.click(post["ts"], "dots_approve")
    assert stored["value"] == ["22:00", "07:00"]
    assert "Stored quiet_hours" in s.slack.posts[-1]["text"]
    for text, answer, expected in (("digest only at 9", "yes", ["09:00"]),
                                   ("digest only at 10", "no", ["09:00"])):
        proposal = adapter.tool("dots_settings", {"text": text})
        post = proposal_post(s, proposal)
        assert adapter.message(answer, thread_ts=post["ts"])["action"] == "skip"
        assert s.settings.get("digest_times") == expected
    proposal = adapter.tool("dots_settings", {"text": "digest only at 11"})
    post = proposal_post(s, proposal)
    adapter.click(post["ts"], "dots_reject")
    adapter.click(post["ts"], "dots_approve")
    assert s.settings.get("digest_times") == ["09:00"]
    assert s.model.fake.calls == 0
    assert_invariants(s)


def test_gateway_drives_onboarding_and_persists_progress(adapter, scenario):
    s = scenario
    assert adapter.message("", synthetic=True)["action"] == "skip"
    assert "What time zone" in s.slack.posts[-1]["text"]
    assert adapter.message("UTC", owner=False)["action"] == "skip"
    assert s.transport.onboarding.pending is None
    for i, answer in enumerate(("UTC", "23:00 to 08:00", "09:00 and 18:00", "skip", "Watch practice email")):
        assert adapter.message(answer, synthetic=True)["action"] == "skip"
        card = next(post for post in reversed(s.slack.posts) if post["buttons"])
        if i == 0:
            # Restore setup state, as a gateway restart would, before confirming.
            restored = SlackRuntime(s.runtime, SimpleNamespace(), SimpleNamespace())
            restored.dispatcher = s.dispatcher
            s.runtime.transport = s.transport = restored
        if i % 2:
            adapter.click(card["ts"], "dots_approve")
        else:
            assert adapter.message("yes", thread_ts=card["ts"])["action"] == "skip"
        assert s.transport.onboarding.index == i + 1
    assert s.transport.onboarding.complete
    assert s.engine._control("dots_onboarding")["index"] == 5
    assert s.ledger.list_tasks()[0].state == "working"
    assert "Setup complete" in s.slack.posts[-1]["text"]
    assert s.runtime.config["owner_session_key"].startswith("agent:main:")
    assert s.model.fake.calls == 0
    assert_invariants(s)


def test_drain_preserves_sent_revision_and_binds_later_effects(adapter, scenario):
    s = scenario
    card = s.working()
    old = s.transport.card(card)
    assert adapter.message("Use the verified source", thread_ts=old.receipt)["action"] == "skip"
    assert s.ledger.get_task(card.id).card_revision == 2
    latest = next(effect for effect in s.ledger.list_effects() if effect.payload.get("card_id") == card.id
                  and effect.payload.get("revision") == 2)
    other = s.transport.card(s.engine.intake("Acme second task"))
    s.dispatcher.dispatch(s.slack.channel, "Noise count", idempotency_key="noise-digest",
                          buttons=[{"action_id": "dots_show_noise", "value": "practice"}], ping=False)
    s.transport.drain()
    s.transport.drain()
    assert s.runtime.bridge.threads[(old.channel, old.receipt)].revision == 1
    assert s.runtime.bridge.threads[(latest.channel, latest.receipt)].revision == 2
    assert s.runtime.bridge.threads[(other.channel, other.receipt)].task_id != card.id
    assert adapter.message("Change it again", thread_ts=old.receipt)["action"] == "skip"
    assert s.ledger.get_task(card.id).card_revision == 2
    assert_invariants(s)


def test_delivered_mail_buttons_make_vip_and_mark_handled(adapter, scenario):
    s = scenario
    s.add_mail("delivered", category="urgent", needs_reply=True)
    s.mail.poll()
    s.transport.drain()
    card = s.ledger.get_mail_card("practice", "thread-one")
    root = s.ledger.get_effect(card.payload["root_delivery_id"])
    assert s.runtime.bridge.threads[(root.channel, root.receipt)].task_id == card.id
    proposal = adapter.click(root.receipt, "dots_make_vip")
    assert s.settings.get("vips") == []  # The VIP setting still needs confirmation.
    adapter.click(proposal_post(s, proposal)["ts"], "dots_approve")
    assert s.settings.get("vips") == ["alex@example.com"]
    s.add_mail("later-message", category="regular", needs_reply=True)
    s.mail.poll()
    s.transport.drain()
    assert len([post for post in s.slack.posts if post["ts"] == root.receipt]) == 1
    assert s.runtime.bridge.threads[(root.channel, root.receipt)].revision == 2
    adapter.click(root.receipt, "dots_mark_handled")
    assert s.ledger.get_mail_card("practice", "thread-one").state == "closed"
    s.transport.drain()
    s.transport.drain()
    binding = s.runtime.bridge.threads[(root.channel, root.receipt)]
    assert binding.revision == s.ledger.get_mail_card("practice", "thread-one").payload["revision"]
    s.runtime.bridge.threads.clear()
    s.runtime.bridge.cards.clear()
    s.transport.drain()
    assert s.runtime.bridge.threads[(root.channel, root.receipt)].revision == binding.revision
    assert_invariants(s)


def test_gateway_thread_answers_stay_bound_and_ambiguity_asks(adapter, scenario):
    s = scenario
    first, second = s.working("Acme first"), s.working("Acme second")
    a, b = s.transport.card(first), s.transport.card(second)
    question_a = s.engine.ask_question(first.id, "Which source?", "Use verified notes")
    question_b = s.engine.ask_question(second.id, "Which source?", "Use verified notes")
    assert adapter.message("Use verified notes")["action"] == "skip"
    assert "Which card" in s.slack.posts[-1]["text"]
    assert len(s.engine.pending_questions()) == 2
    assert adapter.message("Use the first source", thread_ts=a.receipt)["action"] == "skip"
    assert not s.engine.pending_questions(first.id)
    assert s.engine.pending_questions(second.id)[0]["id"] == question_b.id
    feedback = [event for event in s.ledger.list_events() if event.payload.get("id") == question_a.id]
    assert feedback[-1].task_id == first.id
    assert adapter.message("Use the second source", thread_ts=b.receipt)["action"] == "skip"
    assert not s.engine.pending_questions()
    assert s.model.fake.calls == 0
    assert_invariants(s)


@pytest.mark.parametrize("tool,args", [("read_file", {"path": 7}), ("read_file", {"path": None}),
                                      ("read_file", {"path": ["/workspace"]}),
                                      ("patch", {"patch": {}}), ("terminal", {"command": ["cat"]}),
                                      ("execute_code", {"code": 42})])
def test_pre_tool_argument_inspection_fails_closed(adapter, scenario, tool, args):
    assert scenario.runtime.hooks.pre_tool_call(tool, args, session_id="primary")["action"] == "block"


def test_gateway_errors_cannot_fall_through_even_if_reporting_fails(adapter, scenario, monkeypatch):
    def failed_event(*args, **kwargs):
        raise RuntimeError("Evidence unavailable")
    monkeypatch.setattr(scenario.runtime.hooks, "event", failed_event)
    assert adapter.message("yes")["action"] == "skip"


def test_registered_handler_uses_hermes_session_identity(adapter, scenario):
    s = scenario
    s.runtime.hooks.subagent_start("primary", "helper-session")
    with pytest.raises(PermissionError):
        adapter.tool("dots_brain_write", {"text": "Acme helper note", "session_id": "primary"}, session_id="helper-session")
    result = adapter.tool("dots_brain_write", {"text": "Acme verified note"}, session_id="primary")
    assert result["owner"] == "orca"
    assert s.brain.search("Acme verified note")
    assert_invariants(s)


def test_owner_dm_channel_replaces_install_member_id_for_card_bindings(adapter, scenario):
    s = scenario
    s.mail.owner_channel = s.slack.owner_id
    adapter.message("status", synthetic=True)
    assert s.engine._control("dots_owner_channel") == s.slack.channel
    assert s.mail.owner_channel == s.slack.channel
    # The native Slack client can also resolve a user-addressed post to a DM.
    s.slack.channels = {}
    card = s.engine.intake("Acme member-addressed card")
    s.transport.channel = s.slack.owner_id
    effect = s.transport.card(card)
    s.runtime.bridge.threads.clear()
    s.slack.channels[effect.receipt] = s.slack.channel
    s.transport.bind_effect(effect)
    assert s.runtime.bridge.threads[(s.slack.channel, effect.receipt)].task_id == card.id
    assert_invariants(s)


def test_native_attach_keeps_mail_update_reconciler_and_spawns_pump(scenario):
    s = scenario
    spawned = []
    def spawn(coro, *, name=None):
        spawned.append(name)
        coro.close()  # No background loop or provider calls in acceptance.
    transport = SlackRuntime(s.runtime, SimpleNamespace(spawn_task=spawn), SimpleNamespace(client=object()))
    async def attach():
        transport.attach()
    asyncio.run(attach())
    assert spawned == ["hermes-dots-slack"]
    assert isinstance(transport.dispatcher.reconciler, MailCardReconciler)
    assert s.runtime.mail.dispatcher is transport.dispatcher
