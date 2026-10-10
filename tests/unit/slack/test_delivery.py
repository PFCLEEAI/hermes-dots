"""Real ledger restart safety and quiet timing, with no Slack network."""

from datetime import datetime, timezone

import pytest

from hermes_dots.ledger import Ledger, OutboxEffect, OutboxState
from hermes_dots.slack import QuietHours, QuietHoursDispatcher, SlackReconciler


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "ledger.sqlite") as store:
        yield store


def make_dispatcher(ledger, fake_slack, fake_clock):
    return QuietHoursDispatcher(ledger, fake_slack, quiet_hours=QuietHours(timezone="UTC"), now=fake_clock.now)


def test_2330_held_until_0800_and_ordered(ledger, fake_slack, fake_clock):
    fake_clock.set(datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc))
    dispatcher = make_dispatcher(ledger, fake_slack, fake_clock)
    for index in range(3):
        result = dispatcher.dispatch(fake_slack.channel, f"Ping {index}", idempotency_key=f"ping-{index}")
        assert result.state == OutboxState.PENDING
    assert not fake_slack.posts
    assert len(ledger.list_events()) == 3  # Immediate logs despite held delivery.
    fake_clock.set(datetime(2026, 10, 13, 7, 59, 59, tzinfo=timezone.utc))
    assert dispatcher.flush() == ()
    fake_clock.advance(seconds=1)
    assert len(dispatcher.flush()) == 3
    assert [post["text"] for post in fake_slack.posts] == ["Ping 0", "Ping 1", "Ping 2"]
    assert dispatcher.flush() == ()


def test_held_ping_survives_restart(tmp_path, fake_slack, fake_clock):
    fake_clock.set(datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc))
    path = tmp_path / "restart.sqlite"
    with Ledger(path) as store:
        dispatcher = make_dispatcher(store, fake_slack, fake_clock)
        effect = dispatcher.dispatch(fake_slack.channel, "Urgent", idempotency_key="urgent")
    with Ledger(path) as store:
        assert store.recover_on_start().pending_delivery_ids == (effect.delivery_id,)
        dispatcher = make_dispatcher(store, fake_slack, fake_clock)
        fake_clock.set(datetime(2026, 10, 13, 8, tzinfo=timezone.utc))
        dispatcher.flush()
        dispatcher.dispatch(fake_slack.channel, "Urgent", idempotency_key="urgent")
        assert len(fake_slack.posts) == 1


def test_metadata_every_post_and_card_update(ledger, fake_slack, fake_clock):
    fake_clock.set(datetime(2026, 10, 12, 12, tzinfo=timezone.utc))
    dispatcher = make_dispatcher(ledger, fake_slack, fake_clock)
    effect = dispatcher.dispatch(fake_slack.channel, "Card", idempotency_key="card", delivery_id="dots-1",
                                 thread_ts="parent", buttons=[{"action_id": "dots_approve"}])
    assert effect.state == OutboxState.SENT
    post = fake_slack.posts[0]
    assert post["metadata"] == {"event_type": "dots_delivery", "event_payload": {"dots_delivery_id": "dots-1"}}
    assert post["thread_ts"] == "parent" and post["buttons"]
    dispatcher.reconciler.update(post["ts"], text="Card updated")
    assert dispatcher.reconciler.find_delivery(effect) == effect.receipt


def test_regular_response_during_quiet_hours(ledger, fake_slack, fake_clock):
    dispatcher = make_dispatcher(ledger, fake_slack, fake_clock)
    dispatcher.dispatch(fake_slack.channel, "Health ping", idempotency_key="health")
    response = dispatcher.dispatch(fake_slack.channel, "Status response", idempotency_key="status", ping=False)
    assert response.state == OutboxState.SENT
    assert [post["text"] for post in fake_slack.posts] == ["Status response"]


@pytest.mark.parametrize("hour,expected", [(22, False), (23, True), (0, True), (7, True), (8, False)])
def test_quiet_boundaries(hour, expected):
    assert QuietHours(timezone="UTC").contains(datetime(2026, 10, 12, hour, tzinfo=timezone.utc)) is expected


def test_timezone_and_daytime_hours():
    moment = datetime(2026, 10, 12, 14, 30, tzinfo=timezone.utc)
    assert QuietHours(timezone="Asia/Seoul").contains(moment)
    assert QuietHours("13:00", "15:00", timezone="UTC").contains(moment)
    assert not QuietHours("13:00", "13:00", timezone="UTC").contains(moment)
    with pytest.raises(ValueError):
        QuietHours().contains(datetime(2026, 10, 12))


def test_lost_ack_is_reconciled_without_duplicate(ledger, fake_slack):
    effect = ledger.enqueue_effect(OutboxEffect("dots-1", "key", fake_slack.channel, {"text": "Acme"}))
    reconciler = SlackReconciler(fake_slack)
    post = fake_slack.post

    def lost_ack(*args, **kwargs):
        post(*args, **kwargs)
        raise OSError("Acknowledgement unavailable")

    fake_slack.post = lost_ack
    assert ledger.deliver_pending(effect.delivery_id, reconciler).state == OutboxState.UNKNOWN
    fake_slack.post = post
    result = ledger.reconcile_unknown(effect.delivery_id, reconciler)
    assert result.state == OutboxState.SENT and result.receipt == fake_slack.posts[0]["ts"]
    assert len(fake_slack.posts) == 1


def unknown(ledger, fake_slack):
    effect = ledger.enqueue_effect(OutboxEffect("dots-1", "key", fake_slack.channel, {"text": "Acme"}))
    ledger.transition_effect(effect.delivery_id, OutboxState.SENDING)
    ledger.recover_on_start()
    return effect


def test_successful_absence_sends_once(ledger, fake_slack):
    effect = unknown(ledger, fake_slack)
    reconciler = SlackReconciler(fake_slack)
    assert ledger.reconcile_unknown(effect.delivery_id, reconciler).state == OutboxState.SENT
    ledger.reconcile_unknown(effect.delivery_id, reconciler)
    assert len(fake_slack.posts) == 1


@pytest.mark.parametrize("response", [
    None, {"ok": False}, {"ok": True, "messages": [], "has_more": True},
    {"messages": [], "response_metadata": {"next_cursor": "cursor"}},
    {"ok": True}, [None], [{"metadata": "invalid"}],
])
def test_failed_or_incomplete_history_stays_unknown(ledger, fake_slack, response):
    effect = unknown(ledger, fake_slack)
    fake_slack.history = lambda **_: response
    assert ledger.reconcile_unknown(effect.delivery_id, SlackReconciler(fake_slack)).state == OutboxState.UNKNOWN
    assert not fake_slack.posts


def test_history_exception_stays_unknown(ledger, fake_slack):
    effect = unknown(ledger, fake_slack)

    def fail(**_):
        raise OSError("History unavailable")

    fake_slack.history = fail
    assert ledger.reconcile_unknown(effect.delivery_id, SlackReconciler(fake_slack)).state == OutboxState.UNKNOWN
    assert not fake_slack.posts


def test_only_matching_metadata_and_channel(ledger, fake_slack):
    effect = unknown(ledger, fake_slack)
    fake_slack.post("dots-1")  # ID in body does not count.
    fake_slack.post("Other channel", channel="another-channel",
                    metadata={"event_type": "dots_delivery", "event_payload": {"dots_delivery_id": "dots-1"}})
    reconciler = SlackReconciler(fake_slack)
    assert reconciler.find_delivery(effect) is None
    receipt = reconciler.send(effect)
    assert reconciler.find_delivery(effect) == receipt


def test_redact_before_outbox_logs_and_send(ledger, fake_slack, fake_clock):
    dispatcher = make_dispatcher(ledger, fake_slack, fake_clock)
    secret_shape = "xox" + "b-" + "invented"
    dispatcher.dispatch(fake_slack.channel, secret_shape, idempotency_key="redact", ping=False,
                        buttons=[{"value": secret_shape}])
    assert secret_shape not in repr(ledger.list_effects())
    assert secret_shape not in repr(ledger.list_events())
    assert secret_shape not in repr(fake_slack.posts)


def test_all_ping_types_hold_and_release_in_order(ledger, fake_slack, fake_clock):
    fake_clock.set(datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc))
    dispatcher = make_dispatcher(ledger, fake_slack, fake_clock)
    for kind in ("urgent mail", "nudge", "health"):
        dispatcher.dispatch(fake_slack.channel, kind, idempotency_key=kind)
    assert not fake_slack.posts and len(ledger.list_events()) == 3
    fake_clock.set(datetime(2026, 10, 13, 8, tzinfo=timezone.utc))
    dispatcher.flush()
    assert [post["text"] for post in fake_slack.posts] == ["urgent mail", "nudge", "health"]


def test_matching_delivery_in_incomplete_history_is_sent(ledger, fake_slack):
    effect = unknown(ledger, fake_slack)
    receipt = SlackReconciler(fake_slack).send(effect)
    messages = fake_slack.history(channel=fake_slack.channel)
    fake_slack.history = lambda **_: {"ok": True, "messages": messages, "has_more": True}
    result = ledger.reconcile_unknown(effect.delivery_id, SlackReconciler(fake_slack))
    assert result.state == OutboxState.SENT and result.receipt == receipt
    assert len(fake_slack.posts) == 1


def test_unknown_delivery_not_resent_in_quiet_hours(ledger, fake_slack, fake_clock):
    effect = unknown(ledger, fake_slack)
    fake_clock.set(datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc))
    dispatcher = make_dispatcher(ledger, fake_slack, fake_clock)
    assert dispatcher.reconcile(effect.delivery_id).state == OutboxState.UNKNOWN
    assert not fake_slack.posts
    fake_clock.set(datetime(2026, 10, 13, 8, tzinfo=timezone.utc))
    assert dispatcher.reconcile(effect.delivery_id).state == OutboxState.SENT
    assert len(fake_slack.posts) == 1
