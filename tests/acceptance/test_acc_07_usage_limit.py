"""§13.7: limits block model work, preserve probes, and never catch up."""

import pytest

from fakes.model import ModelLimitError
from hermes_dots.engine import StepBlocked
from hermes_dots.health import PROBE_NAMES
from .invariants import assert_invariants


def test_limit_pause_health_and_missed_runs_without_replay(scenario):
    s = scenario
    card = s.working()
    job = s.runtime.scheduler.create(s.engine, card.id, "Review Acme mail", "5m", "UTC")
    s.engine.schedule_check_back(card.id, 60, "Look for changed mail")
    s.runtime.health.probes["model"] = lambda: s.probe_calls.append("model") or False
    s.model.fake.mode = "limit"
    with pytest.raises(ModelLimitError):
        s.engine.complete_structured({}, "Acme excerpt")
    assert s.ledger.get_task(card.id).blocked_reason == "model_limit"
    effects = []
    with pytest.raises(StepBlocked):
        s.engine.run_step(card.id, "model", "same", lambda: effects.append(True), requires_model=True)
    assert not effects
    s.clock.advance(minutes=11)
    s.runtime.scheduler.capture_missed(now=s.clock.now(), limited=True)
    result = s.engine.tick()
    assert set(s.probe_calls) == set(PROBE_NAMES) and not result["check_backs"]
    missed = s.engine.missed_runs()
    assert len(missed) == 4  # Three scheduler slots and the one check-back.
    assert ("pause", job["id"]) in s.cron.calls
    s.runtime.scheduler.capture_missed(now=s.clock.now(), limited=True)
    s.engine.tick()
    assert s.engine.missed_runs() == missed
    route = s.runtime.bridge.receive(s.slack.message("status"))
    assert route.kind == "command" and s.engine.status()["usage"]["calls"] == 1
    assert "model_limit" in s.runtime.digest.compose()["text"]
    s.model.fake.mode = "normal"
    s.runtime.health.probes["model"] = lambda: True
    assert s.engine.tick()["check_backs"] == []
    assert s.ledger.get_task(card.id).state == "working"
    assert ("resume", job["id"]) in s.cron.calls
    assert s.model.fake.calls == 1 and s.engine.missed_runs() == missed
    assert_invariants(s)
