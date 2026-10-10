"""§13.10: confirmed changes take effect; first responsibility is a task."""

from datetime import datetime, timezone

import pytest

from hermes_dots.security.rules import RuleViolation
from .invariants import assert_invariants


def test_chat_change_store_effect_and_denial_floor(scenario, tmp_path):
    s = scenario
    # Malformed model response uses the documented deterministic chat parser.
    s.model.fake.mode = "malformed"
    proposal = s.settings.parse_change("make quiet hours 22 to 7")
    moment = datetime(2026, 10, 12, 22, 30, tzinfo=timezone.utc)
    assert "may I go ahead?" in proposal["confirmation"] and not s.settings.is_quiet(moment)
    reply = s.settings.confirm_change(proposal)
    assert reply["value"] == ["22:00", "07:00"] and "Stored" in reply["reply"]
    assert s.settings.is_quiet(moment)
    s.clock.set(moment)
    # SlackRuntime.drain applies current settings to the real dispatcher.
    s.transport.drain()
    before = len(s.slack.posts)
    held = s.dispatcher.dispatch(s.slack.channel, "Held with new hours", idempotency_key="new-hours-held")
    assert held.state == "pending" and len(s.slack.posts) == before
    s.clock.set(datetime(2026, 10, 13, 7, tzinfo=timezone.utc))
    s.transport.drain()
    assert s.ledger.get_effect(held.delivery_id).state == "sent"
    assert any(e.kind == "feedback" and e.payload.get("name") == "quiet_hours" for e in s.ledger.list_events())
    with pytest.raises(RuleViolation):
        s.settings.propose_change("action_rules", {"send": "auto"})
    denied = str(tmp_path / "extra-denied")
    s.settings.confirm_change(s.settings.propose_change("extra_denied_paths", [denied]))
    with pytest.raises(PermissionError):
        s.settings.propose_change("extra_denied_paths", [])
    assert_invariants(s)


def test_full_onboarding_routes_first_responsibility_to_task_confirmation(scenario):
    s = scenario
    chat = s.transport.onboarding
    for answer in ("UTC", "23:00 to 08:00", "09:00 and 18:00", "skip"):
        before = s.settings.list_settings()
        assert "Reply yes" in chat.answer(s.slack.owner_id, answer)
        assert s.settings.list_settings() == before
        assert "Stored" in chat.answer(s.slack.owner_id, "yes")
    before = s.settings.list_settings()
    assert chat.answer("U_OTHER", "Watch practice email") is None
    assert not s.ledger.list_tasks()
    assert "Reply yes" in chat.answer(s.slack.owner_id, "Watch practice email")
    task = s.ledger.list_tasks()[0]
    assert task.state == "new"
    assert s.ledger.get_card(task.id).goal == "Watch practice email"
    assert s.settings.list_settings() == before
    assert s.settings.get("first_responsibility") is None
    assert f"#{task.id}" in s.slack.posts[-1]["text"]
    assert s.slack.posts[-1]["buttons"]
    assert "Confirmed task" in chat.answer(s.slack.owner_id, "yes")
    assert s.ledger.get_task(task.id).state == "working" and chat.complete
    assert s.settings.list_settings() == before
    assert_invariants(s)
