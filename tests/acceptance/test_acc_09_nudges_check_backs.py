"""§13.9: one-hour nudge, digest repeats, scope and time bounds."""

from datetime import datetime, timezone

from .invariants import assert_invariants


def test_nudge_and_changed_only_bounded_check_back(scenario):
    s = scenario
    card, other = s.working(), s.working("Independent review")
    question = s.engine.ask_question(card.id, "Use the report?", "Use the verified report")
    assert "may I go ahead?" in question.text
    s.clock.advance(seconds=3599)
    assert s.engine.tick()["pings"] == ()
    assert s.engine.run_step(other.id, "read", "input", lambda: "Allowed work")['result'] == "Allowed work"
    s.clock.advance(seconds=1)
    pings = s.engine.tick()["pings"]
    assert len(pings) == 1
    s.dispatcher.flush()
    assert len(s.slack.posts) == 1
    s.clock.advance(hours=1)
    assert s.engine.tick()["pings"] == ()
    digest = s.runtime.digest.compose()["text"]
    assert question.text in digest and question.recommendation in digest
    low = s.engine.schedule_check_back(other.id, 0, "Changes might arrive")
    assert (datetime.fromisoformat(low.due_at) - s.clock.now()).total_seconds() == 60
    high = s.engine.schedule_check_back(other.id, 999999, "Review changed input")
    assert (datetime.fromisoformat(high.due_at) - s.clock.now()).total_seconds() == 21600
    s.clock.advance(hours=6)
    ready = s.engine.tick()["check_backs"]
    assert len(ready) == 1 and ready[0]["reason"] == "Review changed input"
    assert ready[0]["scope"]["sources"] == ["practice"]
    assert ready[0]["scope"]["allowed_actions"] == ["read", "post", "code"]
    assert any(e.kind == "scope_recheck" and e.payload["changed_only"] for e in s.ledger.list_events())
    s.clock.set(datetime(2026, 10, 12, 22, 30, tzinfo=timezone.utc))
    third = s.working("Quiet nudge")
    s.engine.ask_question(third.id, "Read tomorrow?", "Read after quiet hours")
    s.clock.advance(hours=1)
    assert s.engine.tick()["pings"] == ()
    before = len(s.slack.posts)
    s.dispatcher.flush()
    assert len(s.slack.posts) == before
    s.clock.set(datetime(2026, 10, 13, 8, tzinfo=timezone.utc))
    assert len(s.engine.tick()["pings"]) == 1
    s.dispatcher.flush()
    assert len(s.slack.posts) == before + 1
    assert_invariants(s)
