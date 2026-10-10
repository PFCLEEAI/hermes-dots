"""Owner and card routing with the shared offline FakeSlack."""

import json

import pytest

from hermes_dots.ledger import Card
from hermes_dots.slack import (
    ACTION_LABELS, Binding, SlackBridge, parse_command, render_card,
    render_status, render_update,
)


@pytest.fixture
def bridge(fake_slack):
    events, handled = [], []
    service = SlackBridge(fake_slack.owner_id, log_event=lambda kind, data: events.append((kind, data)),
                          handler=handled.append)
    service.bind_card(fake_slack.channel, "1", "TASK-1", 1)
    service.bind_card(fake_slack.channel, "2", "TASK-2", 3)
    return service, events, handled


@pytest.mark.parametrize("name,argument", [
    ("status", ""), ("pause", ""), ("resume", ""), ("stop", "TASK-1"),
    ("settings", ""), ("rules", ""), ("schedules", ""), ("forget", "Acme project"),
])
def test_every_command(name, argument):
    command = parse_command(f"  {name.upper()} {argument}  ")
    assert (command.name, command.argument) == (name, argument)


@pytest.mark.parametrize("text", ["stop", "forget", "pause TASK-1", "status extra"])
def test_malformed_command(text):
    with pytest.raises(ValueError):
        parse_command(text)


def test_non_command():
    assert parse_command("Please check Acme") is None
    assert parse_command("") is None


def test_non_owner_ignored_and_logged(bridge, fake_slack):
    service, events, handled = bridge
    assert service.receive(fake_slack.message("pause", owner=False)).kind == "ignored"
    assert events[-1][0] == "slack_non_owner_ignored"
    assert not handled


def test_thread_reply_exact_revision(bridge, fake_slack):
    service, _, handled = bridge
    route = service.receive(fake_slack.message("not done", thread_ts="2"))
    assert route.kind == "instruction"
    assert route.binding == Binding("TASK-2", 3)
    assert handled == [route]


def test_reference_and_old_thread_revision(bridge, fake_slack):
    service, _, _ = bridge
    service.bind_card(fake_slack.channel, "new", "TASK-1", 2)
    assert service.receive(fake_slack.message("do this #TASK-1")).binding == Binding("TASK-1", 2)
    assert service.receive(fake_slack.message("do this", thread_ts="1")).binding == Binding("TASK-1", 1)
    assert service.receive(fake_slack.message("do this #TASK-1", thread_ts="1")).kind == "question"


@pytest.mark.parametrize("text,thread", [
    ("do this", None), ("do this #UNKNOWN", None),
    ("do this #TASK-1 #TASK-2", None), ("do this #TASK-2", "1"),
    ("do this", "unknown"),
])
def test_ambiguous_asks_without_handler(bridge, fake_slack, text, thread):
    service, _, handled = bridge
    assert service.receive(fake_slack.message(text, thread_ts=thread)).kind == "question"
    assert not handled


def test_cross_channel_does_not_bind(bridge, fake_slack):
    service, _, handled = bridge
    event = fake_slack.message("yes", thread_ts="1")
    event["channel"] = "another-channel"
    assert service.receive(event).kind == "question"
    assert not handled


def test_thread_binding_cannot_be_reassigned(bridge, fake_slack):
    service, _, _ = bridge
    with pytest.raises(ValueError):
        service.bind_card(fake_slack.channel, "1", "TASK-2", 1)


def test_global_pause_and_explicit_stop(bridge, fake_slack):
    service, _, _ = bridge
    route = service.receive(fake_slack.message("pause", thread_ts="1"))
    assert route.kind == "command" and route.binding is None
    route = service.receive(fake_slack.message("stop #TASK-1"))
    assert route.kind == "command" and route.binding == Binding("TASK-1", 1)
    assert route.command.argument == "#TASK-1"


def test_render_and_route_every_button(fake_slack):
    rendered = render_card(Card("TASK-1", "Check Acme"), actions=tuple(ACTION_LABELS),
                           action_values={"make_vip": "alex@example.com"})
    message = fake_slack.post(**rendered)
    handled = []
    service = SlackBridge(fake_slack.owner_id, log_event=lambda *_: None, handler=handled.append)
    service.bind_card(fake_slack.channel, message["ts"], "TASK-1", 1)
    for name in ACTION_LABELS:
        button = next(b for b in rendered["buttons"] if b["action_id"] == f"dots_{name}")
        assert json.loads(button["value"])["revision"] == 1
        route = service.receive(fake_slack.click(message["ts"], f"dots_{name}"))
        assert route.kind == "action" and route.action == name
        assert route.binding == Binding("TASK-1", 1)
    assert len(handled) == len(ACTION_LABELS)
    route = service.receive(fake_slack.click(message["ts"], "dots_make_vip"))
    assert route.value == "alex@example.com"


def test_non_owner_and_stale_buttons(fake_slack):
    rendered = render_card(Card("TASK-1", "Check Acme"))
    message = fake_slack.post(**rendered)
    handled, logs = [], []
    service = SlackBridge(fake_slack.owner_id, log_event=lambda *args: logs.append(args),
                          handler=handled.append)
    service.bind_card(fake_slack.channel, message["ts"], "TASK-1", 1)
    assert service.receive(fake_slack.click(message["ts"], "dots_approve", owner=False)).kind == "ignored"
    service.bind_card(fake_slack.channel, "new", "TASK-1", 2)
    assert service.receive(fake_slack.click(message["ts"], "dots_approve")).kind == "question"
    assert not handled


def test_real_action_event_shape(fake_slack, bridge):
    service, _, handled = bridge
    button = render_card(Card("TASK-1", "Check Acme"))["buttons"][0]
    route = service.receive({"user": {"id": fake_slack.owner_id},
                             "channel": {"id": fake_slack.channel},
                             "message": {"ts": "1"}, "actions": [button]})
    assert route.kind == "action" and handled == [route]


def test_forged_action_revision(bridge, fake_slack):
    service, _, handled = bridge
    button = render_card(Card("TASK-1", "Check Acme", revision=2))["buttons"][0]
    event = {"user": fake_slack.owner_id, "channel": fake_slack.channel,
             "message_ts": "1", "action": button}
    assert service.receive(event).kind == "question"
    button["value"] = "invalid-json"
    assert service.receive(event).kind == "question"
    assert not handled


def test_render_order_and_status_fields():
    update = render_update("Changed", ["evidence://1"], "Impact", "Approve?", "08:00")
    assert [line.split(":")[0] for line in update.splitlines()] == [
        "What changed", "Evidence", "Impact", "What you need to decide", "Next check time",
    ]
    status = render_status([{"id": "TASK-1", "state": "working", "last_successful_step": "Read",
                            "waiting_on": "owner", "next_action": "Check", "next_action_at": "08:00",
                            "files": ["report.md"]}], unknown_deliveries=["dots-1"])
    for text in ("working", "Read", "owner", "Check", "08:00", "report.md", "dots-1"):
        assert text in status
    assert "No tasks." in render_status([])


def test_logs_and_handler_redact(bridge, fake_slack):
    service, events, handled = bridge
    secret_shape = "xox" + "b-" + "invented"
    service.receive(fake_slack.message(f"Check {secret_shape}", thread_ts="1"))
    assert secret_shape not in repr(events)
    assert secret_shape not in repr(handled)
