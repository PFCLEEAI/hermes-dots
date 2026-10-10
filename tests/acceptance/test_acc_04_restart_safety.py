"""§13.4: actual database reopen, uncertain Slack effects, interrupted steps."""

import pytest

from hermes_dots.engine import StepBlocked
from hermes_dots.ledger import Ledger, OutboxEffect
from hermes_dots.inbox import MailCardReconciler
from .invariants import assert_invariants


@pytest.mark.parametrize("crash_after_post", [False, True])
def test_restart_preserves_intake_and_reconciles_once(scenario, crash_after_post):
    s = scenario
    s.mail.dispatcher = None
    s.add_mail(category="urgent")
    s.mail.poll()
    effect = s.ledger.list_effects()[0]
    reconciler = MailCardReconciler(s.slack)
    if crash_after_post:
        effect = s.ledger.transition_effect(effect.delivery_id, "sending")
        reconciler.send(effect)  # Provider commit; deliberately omit receipt persistence.
    card = s.working()
    def interrupted():
        raise SystemExit("Invented interruption")
    with pytest.raises(SystemExit):
        s.engine.run_step(card.id, "read", "same", interrupted)
    path = s.root / "state/ledger.sqlite"
    s.ledger.close()
    s.ledger = Ledger(path)
    for service in (s.runtime, s.engine, s.engine.usage, s.settings, s.runtime.health,
                    s.runtime.scheduler, s.mail, s.dispatcher):
        service.ledger = s.ledger
    report = s.ledger.recover_on_start()
    assert report.interrupted_task_ids == (card.id,)
    assert s.engine.status()["tasks"][0]["unknown_step"]["step"] == "read"
    assert s.engine.resume() == []
    with pytest.raises(StepBlocked, match="reconciliation"):
        s.engine.reopen(card.id)
    if crash_after_post:
        assert report.unknown_delivery_ids == (effect.delivery_id,)
        s.ledger.reconcile_unknown(effect.delivery_id, reconciler)
    else:
        assert report.pending_delivery_ids == (effect.delivery_id,)
        s.ledger.deliver_pending(effect.delivery_id, reconciler)
    s.ledger.deliver_pending(effect.delivery_id, reconciler)
    assert len(s.slack.posts) == 1
    calls = s.model.fake.calls
    assert s.mail.poll() == () and s.model.fake.calls == calls
    assert s.ledger.recover_on_start().interrupted_task_ids == ()
    # A history failure proves neither presence nor absence: no replay.
    unknown = s.ledger.enqueue_effect(OutboxEffect("unknown", "unknown", s.slack.channel, {"text": "Acme"}))
    s.ledger.transition_effect(unknown.delivery_id, "sending")
    s.ledger.recover_on_start()
    class UnavailableHistory:
        def find_delivery(self, delivery):
            raise OSError("History unavailable")
        def send(self, delivery):
            pytest.fail("Unknown outcome must not be replayed")
    assert s.ledger.reconcile_unknown(unknown.delivery_id, UnavailableHistory()).state == "unknown"
    assert "unknown" in s.engine.status()["unknown_deliveries"]
    # A successful absence check permits exactly one send.
    assert s.ledger.reconcile_unknown(unknown.delivery_id, reconciler).state == "sent"
    s.ledger.reconcile_unknown(unknown.delivery_id, reconciler)
    assert len(s.slack.posts) == 2
    assert_invariants(s)
