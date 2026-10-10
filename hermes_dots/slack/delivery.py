"""Injected Slack transport and durable quiet-hours delivery via T2's outbox."""

from datetime import datetime, time
from typing import Callable, Mapping, Protocol, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo

from hermes_dots.ledger import Ledger, OutboxEffect, OutboxState
from hermes_dots.security import redact


class SlackClient(Protocol):
    """No SDK required. History must include metadata and thread replies.

    The adapter must exhaust pagination in its recent history window before
    returning a sequence. Incomplete/error responses never prove absence.
    Updates preserve the original post's metadata.
    """

    def post(self, text: str, *, channel: str, thread_ts: str | None = None,
             buttons: Sequence[dict] = (), metadata: dict | None = None) -> dict: ...

    def update(self, ts: str, *, text: str | None = None,
               buttons: Sequence[dict] | None = None) -> dict: ...

    def history(self, *, channel: str) -> Sequence[dict] | dict: ...


def redact_payload(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {redact(str(key)): redact_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_payload(item) for item in value]
    return value


class SlackReconciler:
    """Implements ledger.DeliveryReconciler; exceptions leave outcomes unknown."""

    def __init__(self, client: SlackClient):
        self.client = client

    def send(self, delivery: OutboxEffect) -> str:
        payload = redact_payload(delivery.payload)
        response = self.client.post(
            payload["text"], channel=delivery.channel,
            thread_ts=payload.get("thread_ts"), buttons=payload.get("buttons", ()),
            metadata={"event_type": "dots_delivery",
                      "event_payload": {"dots_delivery_id": delivery.delivery_id}},
        )
        if response.get("ok") is False or not response.get("ts"):
            raise RuntimeError("Slack did not confirm delivery")
        return str(response["ts"])

    def find_delivery(self, delivery: OutboxEffect) -> str | None:
        response = self.client.history(channel=delivery.channel)
        if isinstance(response, Mapping):
            if response.get("ok") is False:
                raise RuntimeError("Slack history unavailable")
            messages = response.get("messages")
            incomplete = (response.get("has_more") or
                          response.get("response_metadata", {}).get("next_cursor"))
        else:
            messages, incomplete = response, False
        if not isinstance(messages, (list, tuple)):
            raise RuntimeError("Slack history unavailable")
        for message in messages:
            if not isinstance(message, Mapping):
                raise RuntimeError("Invalid Slack history message")
            metadata = message.get("metadata") or {}
            if not isinstance(metadata, Mapping):
                raise RuntimeError("Invalid Slack message metadata")
            payload = metadata.get("event_payload") or {}
            if not isinstance(payload, Mapping):
                raise RuntimeError("Invalid Slack message metadata")
            if (metadata.get("event_type") == "dots_delivery" and
                    payload.get("dots_delivery_id") == delivery.delivery_id):
                if not message.get("ts"):
                    raise RuntimeError("Slack delivery has no receipt")
                return str(message["ts"])
        if incomplete:
            raise RuntimeError("Slack history is incomplete")
        return None

    def update(self, ts: str, *, text: str, buttons: Sequence[dict] = ()) -> dict:
        """Edit a card in place, preserving its original delivery metadata."""
        response = self.client.update(ts, text=redact(text), buttons=redact_payload(buttons))
        if response.get("ok") is False or not response.get("ts"):
            raise RuntimeError("Slack did not confirm update")
        return response


class QuietHours:
    def __init__(self, start: str = "23:00", end: str = "08:00", *, timezone: str | None = None):
        self.start, self.end = time.fromisoformat(start), time.fromisoformat(end)
        self.zone = ZoneInfo(timezone) if timezone else None

    def contains(self, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Quiet hours require a timezone-aware clock")
        local = now.astimezone(self.zone) if self.zone else now.astimezone()
        current = local.time().replace(tzinfo=None)
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= current < self.end
        return current >= self.start or current < self.end


class QuietHoursDispatcher:
    """Pending pings survive restart and release in ledger insertion order.

    Call flush on each runtime tick. Reconcile uncertain deliveries explicitly;
    flush never blindly retries them. Settings can replace quiet_hours live.
    """

    def __init__(self, ledger: Ledger, client: SlackClient, *, quiet_hours: QuietHours,
                 now: Callable[[], datetime]):
        self.ledger, self.reconciler = ledger, SlackReconciler(client)
        self.quiet_hours, self.now = quiet_hours, now

    def dispatch(self, channel: str, text: str, *, idempotency_key: str,
                 delivery_id: str | None = None, ping: bool = True,
                 thread_ts: str | None = None, buttons: Sequence[dict] = (),
                 task_id: str | None = None, card_id: str | None = None,
                 revision: int | None = None) -> OutboxEffect:
        payload = redact_payload({"text": text, "ping": ping, "thread_ts": thread_ts,
                                  "buttons": buttons})
        if card_id is not None:
            payload.update(card_id=card_id, revision=revision)
        with self.ledger.transaction():
            effect = self.ledger.enqueue_effect(OutboxEffect(
                delivery_id or f"dots-{uuid4().hex}", idempotency_key,
                channel, payload, task_id=task_id,
            ))
            self.ledger.record_event("slack_delivery_queued", {
                "delivery_id": effect.delivery_id, "text": payload["text"],
                "held": ping and self.quiet_hours.contains(self.now()),
            }, task_id=task_id)
        self.flush()
        return self.ledger.get_effect(effect.delivery_id)

    def flush(self) -> tuple[OutboxEffect, ...]:
        held = self.quiet_hours.contains(self.now())
        results = []
        for effect in self.ledger.list_effects(OutboxState.PENDING):
            if effect.payload.get("ping", True) and held:
                continue
            results.append(self.ledger.deliver_pending(effect.delivery_id, self.reconciler))
        return tuple(results)

    def reconcile(self, delivery_id: str) -> OutboxEffect:
        effect = self.ledger.get_effect(delivery_id)
        if effect.payload.get("ping", True) and self.quiet_hours.contains(self.now()):
            return effect
        return self.ledger.reconcile_unknown(delivery_id, self.reconciler)
