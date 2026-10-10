"""§13.8: card-bound instructions, ambiguous questions, owner authorization."""

from .invariants import assert_invariants


def test_thread_binding_ambiguity_and_owed_reply(scenario):
    s = scenario
    first, second = s.working("Acme first"), s.working("Acme second")
    a, b = s.transport.card(first), s.transport.card(second)
    route = s.runtime.bridge.receive(s.slack.message("Use the verified source", thread_ts=a.receipt))
    s.runtime.route(route)
    assert route.binding.task_id == first.id and route.binding.revision == 1
    assert s.ledger.get_card(first.id).revision == 2
    assert s.ledger.get_card(second.id).revision == 1
    revisions = [s.ledger.get_card(card.id).revision for card in (first, second)]
    for event in (s.slack.message("Use the latest source"),
                  s.slack.message(f"Use #{second.id}", thread_ts=a.receipt)):
        assert s.runtime.bridge.receive(event).kind == "question"
    assert [s.ledger.get_card(card.id).revision for card in (first, second)] == revisions
    assert s.runtime.bridge.receive(s.slack.message("pause", owner=False)).kind == "ignored"
    assert s.ledger.get_task(second.id).state == "working"
    route = s.runtime.bridge.receive(s.slack.message("Use the notes", thread_ts=b.receipt))
    s.runtime.route(route)
    assert route.binding.task_id == second.id
    s.add_mail(needs_reply=True)
    s.mail.poll()
    s.add_mail("owner-reply", sender="owner@example.com")
    s.mail.poll()
    card = s.ledger.get_mail_card("practice", "thread-one")
    assert card.state == "replied_still_open" and card.payload["needs_reply"]
    assert s.mail.digest_section("replied_still_open")
    s.mail.mark_handled("thread-one")
    assert s.ledger.get_mail_card("practice", "thread-one").state == "closed"
    assert_invariants(s)
