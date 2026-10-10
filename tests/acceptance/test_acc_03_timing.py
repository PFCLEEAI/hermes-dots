"""§13.3: held urgent delivery and complete ordered digest."""

from datetime import datetime, timezone

from .invariants import assert_invariants


def test_quiet_ping_releases_at_end_and_digest_is_ordered(scenario):
    s = scenario
    s.clock.set(datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc))
    s.add_mail(category="urgent")
    s.mail.poll()
    assert not s.slack.posts
    assert len(s.ledger.list_effects("pending")) == 1
    s.clock.set(datetime(2026, 10, 13, 7, 59, 59, tzinfo=timezone.utc))
    s.mail.flush()
    assert not s.slack.posts
    s.clock.advance(seconds=1)
    s.mail.flush()
    assert len(s.slack.posts) == 1
    s.mail.flush()
    assert len(s.slack.posts) == 1
    # Register real providers with populated mail, brain, questions, health, usage.
    s.add_mail("regular", needs_reply=True, thread_id="regular-thread")
    s.add_mail("uncertain", category="uncertain", thread_id="uncertain-thread")
    s.add_mail("noise", category="noise", thread_id="noise-thread")
    s.mail.poll()
    s.brain.learn("Acme lesson")
    card = s.working()
    s.engine.ask_question(card.id, "Use the notes?", "Use verified notes")
    s.engine.tick()
    digest = s.runtime.digest.compose()
    titles = ["Regular mail", "Not sure", "Noise count / show list", "Ideas", "Learned items (undo)",
              "Conflicts / pick version", "Promotion taps", "Self-improvement proposals", "Pending questions",
              "Replied, still open", "Missed runs", "Health", "Usage"]
    offsets = [digest["text"].index(title) for title in titles]
    assert offsets == sorted(offsets)
    assert "Acme lesson" in digest["text"] and "Use verified notes" in digest["text"]
    assert "calls" in digest["text"] and "tokens" in digest["text"]
    assert_invariants(s)
