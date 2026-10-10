"""Slack SDK stays in Hermes; dots supplies routing and a receipt-based outbox."""

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json

from hermes_dots.engine.support import clean
from hermes_dots.slack.delivery import QuietHours, QuietHoursDispatcher
from hermes_dots.slack.render import render_card
from hermes_dots.slack.onboarding import Onboarding


class SlackClient:
    """Synchronous T6 interface over Hermes's AsyncApp client, called from workers."""
    def __init__(self, client, loop):
        self.client, self.loop = client, loop
        self.channels = {}

    def _call(self, name, **kwargs):
        result = asyncio.run_coroutine_threadsafe(getattr(self.client, name)(**clean(kwargs)), self.loop).result(timeout=30)
        return dict(result)

    @staticmethod
    def blocks(text, buttons):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}]
        if buttons: blocks.append({"type": "actions", "elements": list(buttons)})
        return blocks

    def post(self, text, *, channel, thread_ts=None, buttons=(), metadata=None):
        response = self._call("chat_postMessage", channel=channel, text=text, thread_ts=thread_ts,
                              blocks=self.blocks(text, buttons), metadata=metadata)
        if response.get("ts"): self.channels[response["ts"]] = channel
        return response

    def update(self, ts, *, text=None, buttons=(), channel=None):
        return self._call("chat_update", channel=channel or self.channels.get(ts), ts=ts, text=text, blocks=self.blocks(text, buttons))

    def history(self, *, channel):
        result, cursor = [], None
        while True:
            page = self._call("conversations_history", channel=channel, cursor=cursor, limit=100)
            if page.get("ok") is False: raise RuntimeError("Slack history unavailable")
            messages = page.get("messages", [])
            for message in messages: self.channels[message["ts"]] = channel
            result.extend(messages)
            cursor = page.get("response_metadata", {}).get("next_cursor")
            if not cursor: return result


class SlackRuntime:
    def __init__(self, runtime, ctx, native):
        self.runtime, self.ctx, self.native = runtime, ctx, native
        self.onboarding = Onboarding(runtime.settings, owner_id=runtime.config["owner_slack_id"])
        self.channel = runtime.config.get("owner_channel", runtime.config["owner_slack_id"])
        self.connected = False

    def attach(self):
        loop = asyncio.get_running_loop()
        client = SlackClient(self.native.client, loop)
        settings = self.runtime.settings
        self.dispatcher = QuietHoursDispatcher(self.runtime.ledger, client,
                                              quiet_hours=QuietHours(*settings.get("quiet_hours"), timezone=settings.get("timezone")),
                                              now=lambda: datetime.now(timezone.utc))
        self.runtime.mail.dispatcher = self.dispatcher
        self.runtime.delivery = self.dispatcher
        self.runtime.transport = self
        self.ctx.spawn_task(self.pump(), name="hermes-dots-slack")

    async def pump(self):
        while True:
            try:
                await asyncio.to_thread(self.drain)
            except Exception as error:
                self.runtime.brain.evidence.append({"kind": "runtime_transport_failed", "error": type(error).__name__})
            await asyncio.sleep(10)

    def drain(self):
        # Tick script writes readiness; this primary gateway runtime owns model work.
        control = self.runtime.engine._control("dots_gateway_cursor", 0)
        events = [e for e in self.runtime.ledger.list_events() if e.id > control and e.kind in {
            "mail_check_ready", "reflection_ready", "check_back_ready"}]
        for event in events:
            if self.runtime.engine.model_blocked():
                self.runtime.brain.evidence.append({"kind": "model_work_missed", "event_id": event.id, "reason": "model_limit"})
                self.runtime.engine._set_control("dots_gateway_cursor", event.id)
                continue
            if event.kind == "mail_check_ready": self.runtime.mail.tick()
            elif event.kind == "reflection_ready":
                feedback = [{**e.payload, "timestamp": e.created_at} for e in self.runtime.ledger.list_events() if e.kind == "feedback"]
                self.runtime.brain.nightly_reflection(feedback, self.propose, day=event.payload["day"])
            else:
                # Keep the task authority and scope in the wake-up prompt. This API only
                # accepts a live owner session; rejected injections remain visible.
                session_key = self.runtime.config.get("owner_session_key")
                accepted = session_key and self.ctx.inject_message(
                    "Check back on the confirmed task; inspect only what changed: " + json.dumps(clean(event.payload)),
                    session_key=session_key)
                if not accepted:
                    self.runtime.brain.evidence.append({"kind": "check_back_dispatch_unavailable", "task_id": event.task_id})
            self.runtime.engine._set_control("dots_gateway_cursor", event.id)
        self.dispatcher.quiet_hours = QuietHours(*self.runtime.settings.get("quiet_hours"), timezone=self.runtime.settings.get("timezone"))
        for effect in self.runtime.ledger.list_effects():
            if effect.state == "unknown": self.dispatcher.reconcile(effect.delivery_id)
        self.dispatcher.flush()
        for effect in self.runtime.ledger.list_effects():
            if effect.state != "sent": continue
            self.connected = True
            if effect.task_id:
                task = self.runtime.ledger.get_task(effect.task_id)
                self.runtime.bridge.bind_card(effect.channel, effect.receipt, task.id, task.card_revision)

    def propose(self, excerpts):
        schema = {"type": "object", "properties": {"proposal": {"type": "string"}}, "required": ["proposal"]}
        return self.runtime.engine.complete_structured(schema, "Propose a bounded improvement; never apply it.\n" + excerpts)["proposal"]

    def card(self, card):
        rendered = render_card(asdict(card))
        effect = self.dispatcher.dispatch(self.channel, rendered["text"], buttons=rendered["buttons"],
                                          task_id=card.id, idempotency_key=f"task-card:{card.id}:{card.revision}", ping=False)
        if effect.receipt: self.runtime.bridge.bind_card(effect.channel, effect.receipt, card.id, card.revision)
        return effect
