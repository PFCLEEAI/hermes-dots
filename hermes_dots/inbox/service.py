"""Email responsibility using the existing ledger, brain, engine and Slack lanes."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

from hermes_dots.ledger import (
    Idea, IdeaState, MailCard, MailCardState, MailCursor, MailItem, OutboxEffect, OutboxState,
)
from hermes_dots.slack.delivery import QuietHours, QuietHoursDispatcher, SlackReconciler, redact_payload
from hermes_dots.slack.render import render_card
from .triage import MailTriage, address


DEFAULTS = {"mail_check_interval_seconds": 300, "snooze_days": 1, "idea_expiry_days": 7, "vips": []}
HOOKS = ["dots_make_vip", "dots_remove_vip", "dots_accept", "dots_dismiss", "dots_snooze", "dots_mark_handled",
         "dots_show_noise"]


def identity(prefix, *parts):
    return prefix + sha256(json.dumps(parts).encode()).hexdigest()[:24]


def stamp(moment):
    return moment.astimezone(timezone.utc).isoformat()


class MailCardReconciler(SlackReconciler):
    """The first ping creates a card; later material changes edit that same post.

    A later urgent ping is a thread reply, preserving one main card per thread.
    Updates are idempotent and reconciliation checks their visible content.
    """

    def _root(self, delivery):
        root_id = delivery.payload.get("root_delivery_id")
        return super().find_delivery(replace(delivery, delivery_id=root_id)) if root_id else None

    def send(self, delivery):
        operation = delivery.payload.get("mail_operation")
        if operation not in {"card", "ping"}:
            return super().send(delivery)
        receipt = self._root(delivery)
        if delivery.payload.get("root_delivery_id") and not receipt:
            raise RuntimeError("Resolve the original card delivery before updating it")
        if operation == "card" and receipt:
            self.update(receipt, text=delivery.payload["text"], buttons=delivery.payload.get("buttons", ()))
            return receipt
        if operation == "ping":
            if not receipt:
                raise RuntimeError("Mail card receipt is not yet confirmed")
            delivery = replace(delivery, payload={**delivery.payload, "thread_ts": receipt})
        return super().send(delivery)

    def find_delivery(self, delivery):
        found = super().find_delivery(delivery)
        if delivery.payload.get("mail_operation") != "card":
            return found
        receipt = self._root(delivery)
        if receipt is None:
            return found
        history = self.client.history(channel=delivery.channel)
        messages = history.get("messages", ()) if isinstance(history, dict) else history
        for message in messages:
            if str(message.get("ts")) == receipt:
                if (message.get("text") == delivery.payload["text"] and
                        message.get("buttons", []) == delivery.payload.get("buttons", [])):
                    return receipt
                return None  # A successful check proved the update has not happened.
        raise RuntimeError("Mail card history is unavailable")


class EmailResponsibility:
    def __init__(self, ledger, source, model, settings, brain, *, account, owner_address,
                 engine=None, dispatcher=None, owner_channel="owner", now=None):
        self.ledger, self.source, self.settings, self.brain = ledger, source, settings, brain
        self.triage = MailTriage(model)
        self.account, self.owner_address = account, address(owner_address)
        self.engine, self.dispatcher, self.owner_channel = engine, dispatcher, owner_channel
        self.now = now or (lambda: datetime.now(timezone.utc))
        if dispatcher:
            dispatcher.reconciler = MailCardReconciler(dispatcher.reconciler.client)

    def _event(self, kind, payload):
        payload = redact_payload(payload)
        self.ledger.record_event(kind, payload)
        self.brain.evidence.append({"kind": kind, **payload})

    def _runtime(self):
        row = self.ledger.connection.execute("SELECT * FROM inbox_runtime WHERE account=?", (self.account,)).fetchone()
        return dict(row) if row else {"account": self.account, "next_poll_at": None, "voice_note_id": None}

    def _save_runtime(self, data):
        self.ledger.connection.execute(
            "INSERT INTO inbox_runtime VALUES (?,?,?) ON CONFLICT(account) DO UPDATE SET "
            "next_poll_at=excluded.next_poll_at,voice_note_id=excluded.voice_note_id",
            (self.account, data["next_poll_at"], data["voice_note_id"]),
        )

    def _voice(self):
        data = self._runtime()
        if data["voice_note_id"]:
            try:
                path, note, _ = self.brain._find(data["voice_note_id"])
            except KeyError:
                return None  # Undo/forget must not silently recreate the profile.
            return {"text": note.body, "citation": "brain://" + str(path.relative_to(self.brain.root / "brain"))}
        sent = [m for m in self.source.sent_mail() if address(m.get("sender", "")) == self.owner_address][:10]
        if not sent:
            return None
        bodies = [str(m.get("body", "")) for m in sent]
        # Style features only; raw sent correspondence is never put in a prompt.
        profile = {"sample_count": len(bodies),
                   "average_words": round(sum(len(body.split()) for body in bodies) / len(bodies)),
                   "uses_thanks": any("thanks" in body.casefold() for body in bodies),
                   "uses_korean_thanks": any("감사" in body for body in bodies),
                   "uses_exclamation": any("!" in body for body in bodies)}
        receipt = self.brain.evidence.append({"kind": "voice_profile_read", "account": self.account,
                                              "message_ids": [m["message_id"] for m in sent]})
        try:
            note = self.brain.learn(json.dumps(profile, ensure_ascii=False),
                                    summary="Email voice profile", category="lessons", type="preference",
                                    confidence="inferred", evidence_refs=[receipt])
        except ValueError:
            return None  # Suppressed learning remains suppressed.
        with self.ledger.transaction():
            data["voice_note_id"] = note.id
            self._save_runtime(data)
        return {"text": note.body, "citation": "brain://" + str(self.brain.writer.path_for(note).relative_to(self.brain.root / "brain"))}

    def tick(self):
        moment = self.now()
        with self.ledger.transaction():
            runtime = self._runtime()
        if runtime["next_poll_at"] and moment < datetime.fromisoformat(runtime["next_poll_at"]):
            self.flush()
            return ()
        return self.poll()

    def poll(self):
        """Read and classify new IDs only; cursor and all intents commit together."""
        moment = self.now()
        with self.ledger.transaction():
            cursor = self.ledger.get_mail_cursor(self.account)
        try:
            batch = self.source.poll(cursor.cursor if cursor else None, since=moment - timedelta(days=3))
        except Exception:
            self._event("mail_access_failed", {"account": self.account, "reason": "Mail source unavailable"})
            raise
        handled = []
        with self.ledger.transaction():
            if batch.gap:
                self._event("mail_history_gap", {"account": self.account, "resync": True})
            for raw in batch.messages:
                mail = redact_payload(raw)
                if not self.ledger.get_mail_item(self.account, mail["message_id"]):
                    self._handle(mail, moment)
                    handled.append(mail["message_id"])
            self.ledger.set_mail_cursor(MailCursor(self.account, batch.cursor))
            runtime = self._runtime()
            runtime["next_poll_at"] = stamp(moment + timedelta(seconds=self.settings.get(
                "mail_check_interval_seconds", DEFAULTS["mail_check_interval_seconds"])))
            self._save_runtime(runtime)
            self.ledger.expire_ideas(stamp(moment))
            self.ledger.connection.execute("UPDATE ideas SET state='new',snoozed_until=NULL "
                                           "WHERE state='snoozed' AND snoozed_until<=?", (stamp(moment),))
        self.flush()
        return tuple(handled)

    def _handle(self, mail, moment):
        existing = self.ledger.get_mail_card(self.account, mail["thread_id"])
        old = existing.payload if existing else {}
        owner_reply = address(mail.get("sender", "")) == self.owner_address
        result = self.triage.classify(mail, now=moment, vips=self.settings.get("vips", []),
                                      context={key: old.get(key) for key in ("needs_reply", "deadline", "category")})
        state = existing.state if existing else MailCardState.OPEN
        if owner_reply:
            # An owner's response alone is not proof that the request is fulfilled.
            if state == MailCardState.OPEN:
                state = MailCardState.REPLIED_STILL_OPEN
            if result.handled:
                state = MailCardState.CLOSED
        elif state == MailCardState.CLOSED:
            state = MailCardState.OPEN
        payload = {**old, "subject": mail.get("subject", ""), "revision": old.get("revision", 0) + 1,
                   "message_id": mail["message_id"], "reason": result.reason,
                   "category": result.category, "deadline": result.deadline,
                   "needs_reply": result.needs_reply, "sender": mail.get("sender", "")}
        if owner_reply:
            payload["sender"] = old.get("sender", mail.get("sender", ""))
            payload["category"] = old.get("category", result.category)
            payload["deadline"] = result.deadline or old.get("deadline")
            payload["needs_reply"] = False if result.handled else old.get("needs_reply", False)
        if result.needs_reply:
            voice = self._voice()
            notes = self.brain.search(mail.get("subject", ""), limit=5)
            payload["draft"] = self.triage.draft(mail, voice=voice, notes=notes)
        card = MailCard(existing.id if existing else identity("MAIL-", self.account, mail["thread_id"]),
                        self.account, mail["thread_id"], state, payload)
        material = (not owner_reply or (old.get("category") != "urgent" and payload["category"] == "urgent") or
                    (payload["deadline"] is not None and payload["deadline"] != old.get("deadline")))
        effects = []
        if payload["category"] == "urgent" and material and state != MailCardState.CLOSED:
            card, effects = self._delivery(card, ping=True)
        elif old.get("root_delivery_id"):
            card, effects = self._delivery(card, ping=False)
        self.ledger.intake_mail(MailItem(self.account, mail["message_id"], mail["thread_id"],
                                        result.category, result.reason, result.needs_reply,
                                        {**mail, "triage": asdict(result)}), card, effects=tuple(effects))
        if state != MailCardState.CLOSED:
            for index, reason in enumerate(result.ideas):
                self.ledger.add_idea(Idea(identity("IDEA-", self.account, mail["message_id"], index), card.id,
                                          reason, (self.mail_ref(mail["message_id"]),),
                                          stamp(moment + timedelta(days=self.settings.get("idea_expiry_days", 7)))))
        self._event("mail_processed", {"account": self.account, "message_id": mail["message_id"],
                                        "category": result.category, "injection_ignored": result.injection_ignored})

    def mail_ref(self, message_id):
        return "mail://" + self.account + "/" + message_id

    def _delivery(self, card, *, ping):
        payload = dict(card.payload)
        revision = payload["revision"]
        delivery_id = identity("dots-mail-", card.id, revision)
        root = payload.get("root_delivery_id")
        awaiting_root = root is not None and self.ledger.get_effect(root).state != OutboxState.SENT
        payload["root_delivery_id"] = root or delivery_id
        card = replace(card, payload=payload)
        rendered = self.render(card)
        effects = [OutboxEffect(delivery_id, delivery_id, self.owner_channel, {
            **rendered, "mail_operation": "card", "root_delivery_id": root,
            "card_id": card.id, "revision": revision,
            "ping": ping or awaiting_root,
        })]
        if ping and root:
            ping_id = delivery_id + "-ping"
            effects.append(OutboxEffect(ping_id, ping_id, self.owner_channel, {
                "text": "Urgent mail changed: #" + card.id, "mail_operation": "ping",
                "root_delivery_id": root, "ping": True, "card_id": card.id, "revision": revision,
            }))
        return card, effects

    def flush(self):
        if self.dispatcher:
            start, end = self.settings.get("quiet_hours", ["23:00", "08:00"])
            self.dispatcher.quiet_hours = QuietHours(start, end, timezone=self.settings.get("timezone"))
            return self.dispatcher.flush()
        return ()

    def render(self, card):
        payload = card.payload
        vip = address(payload.get("sender", "")) in {address(x) for x in self.settings.get("vips", [])}
        rendered = render_card({"id": card.id, "revision": payload["revision"],
                                "goal": payload.get("subject"), "sources": [self.mail_ref(payload["message_id"])],
                                "status": card.state},
                               actions=("remove_vip" if vip else "make_vip", "mark_handled"),
                               action_values={"make_vip": payload.get("sender"), "remove_vip": payload.get("sender")})
        rendered["text"] += "\n" + payload.get("reason", "")
        if payload.get("draft"):
            draft = payload["draft"]
            rendered["text"] += "\nCopyable draft: " + draft["text"] + "\nNotes: " + ", ".join(draft["citations"])
        return redact_payload(rendered)

    def _cards(self):
        with self.ledger.transaction():
            rows = self.ledger.connection.execute("SELECT thread_id FROM mail_cards WHERE account=? ORDER BY rowid",
                                                  (self.account,)).fetchall()
            return [self.ledger.get_mail_card(self.account, row[0]) for row in rows]

    def register_digest(self, composer):
        for section in ("regular_mail", "uncertain_mail", "noise", "ideas", "replied_still_open"):
            composer.register(section, lambda section=section: self.digest_section(section))

    def digest_section(self, section):
        cards = self._cards()
        if section == "noise":
            noise = [card for card in cards if card.payload["category"] == "noise"]
            return {"text": f"Noise: {len(noise)} · show list", "buttons": [{"type": "button",
                    "action_id": "dots_show_noise", "text": {"type": "plain_text", "text": "Show list"},
                    "value": self.account}]}
        if section == "ideas":
            with self.ledger.transaction():
                self.ledger.expire_ideas(stamp(self.now()))
                ids = [row[0] for row in self.ledger.connection.execute(
                    "SELECT ideas.id FROM ideas JOIN mail_cards ON mail_cards.id=ideas.mail_card_id "
                    "WHERE mail_cards.account=? AND ideas.state='new' ORDER BY ideas.rowid", (self.account,))]
            return [self.render_idea(self.ledger.get_idea(id)) for id in ids]
        category = {"regular_mail": "regular", "uncertain_mail": "uncertain"}.get(section)
        return [self.render(card) for card in cards if card.state != MailCardState.CLOSED and
                (card.state == MailCardState.REPLIED_STILL_OPEN if section == "replied_still_open" else
                 card.payload["category"] == category)]

    def render_idea(self, idea):
        return render_card({"id": idea.id, "revision": 1, "goal": idea.reason,
                            "sources": idea.sources, "status": idea.state}, actions=("accept", "dismiss", "snooze"))

    def show_noise(self):
        return [self.render(card) for card in self._cards() if card.payload["category"] == "noise"]

    def mark_handled(self, thread_id, *, revision=None):
        with self.ledger.transaction():
            card = self.ledger.get_mail_card(self.account, thread_id)
            if card is None or (revision is not None and revision != card.payload["revision"]):
                raise ValueError("Use the current mail card")
            if card.state == MailCardState.CLOSED:
                return card
            card = replace(card, state=MailCardState.CLOSED,
                           payload={**card.payload, "needs_reply": False, "revision": card.payload["revision"] + 1})
            if card.payload.get("root_delivery_id"):
                card, effects = self._delivery(card, ping=False)
                for effect in effects:
                    self.ledger.enqueue_effect(effect)
            self.ledger.upsert_mail_card(card)
            self._event("feedback", {"feedback_type": "mark_handled", "card_id": card.id})
        self.flush()
        return card

    def propose_vip(self, sender, *, remove=False):
        sender = address(sender)
        vips = self.settings.get("vips", [])
        values = [vip for vip in vips if address(vip) != sender]
        if not remove:
            values.append(sender)
        return self.settings.propose_change("vips", values)

    def vip_chat(self, text):
        return self.settings.parse_change(text)

    def confirm_vip(self, proposal):
        """A confirmed owner setting change may raise an existing card's urgency."""
        stored = self.settings.confirm_change(proposal)
        with self.ledger.transaction():
            for card in self._cards():
                if (card.state != MailCardState.CLOSED and card.payload["category"] != "urgent" and
                        address(card.payload.get("sender", "")) in {address(vip) for vip in stored["value"]}):
                    card = replace(card, payload={**card.payload, "category": "urgent", "reason": "Sender is a VIP",
                                                  "revision": card.payload["revision"] + 1})
                    card, effects = self._delivery(card, ping=True)
                    self.ledger.upsert_mail_card(card)
                    for effect in effects:
                        self.ledger.enqueue_effect(effect)
        self.flush()
        return stored

    def handle_action(self, action_id, value):
        """T6 checks owner identity before invoking this module-local handler.

        VIP actions return a setting proposal for owner confirmation. Mail-card
        actions carry their exact revision; Idea actions are single-use decisions.
        """
        if action_id == "dots_show_noise":
            if value != self.account:
                raise ValueError("Wrong mail account")
            return self.show_noise()
        data = json.loads(value) if isinstance(value, str) else value
        id = data["task_id"]
        if action_id in {"dots_accept", "dots_dismiss", "dots_snooze"}:
            idea = self.ledger.get_idea(id)
            if not any(card.id == idea.mail_card_id for card in self._cards()):
                raise ValueError("Wrong mail account")
            return self.decide_idea(id, action_id.removeprefix("dots_"))
        card = next((card for card in self._cards() if card.id == id), None)
        if card is None or card.payload["revision"] != data["revision"]:
            raise ValueError("Use the current mail card")
        if action_id == "dots_mark_handled":
            return self.mark_handled(card.thread_id, revision=data["revision"])
        if action_id in {"dots_make_vip", "dots_remove_vip"}:
            return self.propose_vip(card.payload["sender"], remove=action_id == "dots_remove_vip")
        raise ValueError("Unknown mail action")

    def decide_idea(self, idea_id, action):
        with self.ledger.transaction():
            self.ledger.expire_ideas(stamp(self.now()))
            idea = self.ledger.get_idea(idea_id)
            if idea.state not in {IdeaState.NEW, IdeaState.SNOOZED}:
                raise ValueError("Idea already decided or expired")
            if action == "accept":
                if self.engine is None:
                    raise RuntimeError("Task engine is required")
                card = self.engine.intake(idea.reason, sources=idea.sources, allowed_actions=("read", "write"),
                                          outputs=("Follow-up evidence",), trigger="on_request")
                result = self.ledger.transition_idea(idea_id, IdeaState.ACCEPTED, task_id=card.id)
            elif action == "dismiss":
                result = self.ledger.transition_idea(idea_id, IdeaState.DISMISSED)
            elif action == "snooze":
                result = self.ledger.transition_idea(idea_id, IdeaState.SNOOZED,
                    snoozed_until=stamp(self.now() + timedelta(days=self.settings.get("snooze_days", 1))))
            else:
                raise ValueError("Unknown Idea decision")
            self._event("feedback", {"feedback_type": "idea_decision", "idea_id": idea_id, "action": action})
        return result
