"""§13.6: exact-payload single-use approvals and immutable floors."""

import pytest

from hermes_dots.engine import StepBlocked
from hermes_dots.security.rules import RuleViolation
from .invariants import assert_invariants


def test_bound_approval_is_required_single_use_and_revision_specific(scenario):
    s = scenario
    card = s.working()
    effects = []
    kwargs = {"action": "post", "target": "Acme", "content": "Review ready"}
    def post(approval_id=None, **changes):
        return s.engine.run_step(card.id, "post", "same", lambda: effects.append("posted"),
                                 approval_id=approval_id, **{**kwargs, **changes})
    with pytest.raises(StepBlocked):
        post()
    grant = s.engine.approvals(card.id).grant("post", "Acme", "Review ready", 1)
    for changes in ({"content": "changed"}, {"target": "Other"}, {"action": "read"}):
        # read is explicitly auto; test exact-action binding at the approvals interface.
        with pytest.raises(PermissionError):
            s.engine.approvals(card.id).consume(grant.id, changes.get("action", "post"),
                changes.get("target", "Acme"), changes.get("content", "Review ready"), 1)
    assert not effects
    post(grant.id)
    s.approved_effects.append((card.id, grant, "post", "Acme", "Review ready", 1))
    with pytest.raises(StepBlocked):
        post(grant.id)
    assert effects == ["posted"]
    stale = s.engine.approvals(card.id).grant("post", "Acme", "New payload", 1)
    s.engine.instruction("Use current notes", task_id=card.id, revision=1)
    with pytest.raises(PermissionError):
        s.engine.approvals(card.id).consume(stale.id, "post", "Acme", "New payload", 2)
    for action in ("send", "post", "delete", "spend", "account"):
        with pytest.raises(RuleViolation):
            s.settings.propose_change("action_rules", {action: "auto"})
    assert_invariants(s)
