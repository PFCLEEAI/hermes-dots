"""Owner authorization, command parsing, and exact card/revision routing."""

from dataclasses import dataclass
import json
import re
from typing import Callable, Mapping

from .delivery import redact_payload
from .render import ACTION_LABELS


COMMANDS = {"status", "pause", "resume", "stop", "settings", "rules", "schedules", "forget"}
GLOBAL_COMMANDS = COMMANDS - {"stop", "forget"}
CARD_REF = re.compile(r"(?<!\w)#([A-Za-z0-9][A-Za-z0-9_-]*)")


@dataclass(frozen=True)
class Command:
    name: str
    argument: str = ""


def parse_command(text: str) -> Command | None:
    parts = text.strip().split(maxsplit=1)
    if not parts or parts[0].lower() not in COMMANDS:
        return None
    name, argument = parts[0].lower(), parts[1].strip() if len(parts) > 1 else ""
    if name in {"stop", "forget"} and not argument:
        raise ValueError(f"{name} needs a target")
    if name in GLOBAL_COMMANDS and argument:
        raise ValueError(f"{name} does not take a target")
    return Command(name, argument)


@dataclass(frozen=True)
class Binding:
    task_id: str
    revision: int


@dataclass(frozen=True)
class Route:
    kind: str
    text: str = ""
    command: Command | None = None
    binding: Binding | None = None
    action: str | None = None
    value: object = None


class SlackBridge:
    """Routes to T5/T7; those services enforce approvals and execute operations.

    Register/restore bindings from persisted card deliveries at runtime startup.
    Old threads retain their original revision when a newer card is registered.
    No ambiguous message or stale button reaches a handler.
    """

    def __init__(self, owner_id: str, *, log_event: Callable[[str, dict], object],
                 handler: Callable[[Route], object] | None = None):
        if not owner_id:
            raise ValueError("An owner ID is required")
        self.owner_id, self.log_event, self.handler = owner_id, log_event, handler
        self.threads = {}
        self.cards = {}

    def bind_card(self, channel: str, thread_ts: str, task_id: str, revision: int, *, updated=False) -> None:
        if not channel or not thread_ts or not task_id or revision < 1:
            raise ValueError("A card binding needs channel, thread, task and revision")
        binding = Binding(task_id, revision)
        key = (channel, thread_ts)
        if key in self.threads and self.threads[key] != binding:
            old = self.threads[key]
            # Mail cards are edited in place after a confirmed delivery.
            if not (updated and task_id.startswith("MAIL-") and old.task_id == task_id and revision > old.revision):
                raise ValueError("A thread's task and revision cannot change")
        self.threads[key] = binding
        previous = self.cards.get((channel, task_id))
        if previous is None or revision >= previous.revision:
            self.cards[(channel, task_id)] = binding

    def _question(self, text="Which card and revision does this apply to? Reply in its thread or cite #<card>."):
        return Route("question", text)

    def receive(self, event: Mapping) -> Route:
        user = event.get("user")
        user_id = user.get("id") if isinstance(user, Mapping) else user
        safe = redact_payload(dict(event))
        if user_id != self.owner_id:
            self.log_event("slack_non_owner_ignored", safe)
            return Route("ignored")
        self.log_event("slack_owner_message", safe)
        channel = event.get("channel", "")
        if isinstance(channel, Mapping):
            channel = channel.get("id", "")
        action = event.get("action")
        if action is None and event.get("actions"):
            if not isinstance(event["actions"], (list, tuple)) or len(event["actions"]) != 1:
                return self._question()
            action = event["actions"][0]
        if action is not None:
            route = self._route_action(event, channel, action)
        else:
            if not isinstance(safe.get("text", ""), str):
                return self._question("Please send a text instruction or use a card button.")
            text = safe.get("text", "").strip()
            refs = CARD_REF.findall(text)
            clean_text = CARD_REF.sub("", text).strip()
            try:
                # A stop target may itself be a #card reference.
                head = text.split(maxsplit=1)[0].lower() if text else ""
                command = parse_command(text if head in {"stop", "forget"} else clean_text)
            except ValueError as exc:
                return self._question(str(exc))
            thread_ts = event.get("thread_ts")
            # Pause/resume always apply to everything; other thread replies
            # retain the card's task and revision, including commands.
            if command is not None and (command.name in {"pause", "resume"} or
                                        (not refs and not thread_ts)):
                route = Route("command", text, command=command)
            else:
                bindings = []
                if thread_ts:
                    binding = self.threads.get((channel, thread_ts))
                    if binding is None:
                        return self._question()
                    bindings.append(binding)
                for ref in refs:
                    binding = self.cards.get((channel, ref))
                    if binding is None:
                        return self._question()
                    bindings.append(binding)
                if not bindings or len(set(bindings)) != 1:
                    return self._question()
                if command and command.name == "stop" and command.argument.lstrip("#") != bindings[0].task_id:
                    return self._question()
                route = Route("command" if command else "instruction", text,
                              command=command, binding=bindings[0])
        if route.kind not in {"question", "ignored"} and self.handler:
            self.handler(route)
        return route

    def _route_action(self, event, channel, action) -> Route:
        if not isinstance(action, Mapping):
            return self._question("Invalid card action. Please use the current card.")
        action_id = action.get("action_id", "")
        if not isinstance(action_id, str):
            return self._question("Unknown card action.")
        name = action_id.removeprefix("dots_")
        if not action_id.startswith("dots_") or name not in ACTION_LABELS:
            return self._question("Unknown card action.")
        try:
            value = json.loads(action["value"])
            if not isinstance(value, Mapping):
                raise ValueError("Invalid binding")
            task_id, revision = value["task_id"], value["revision"]
            if not isinstance(task_id, str) or type(revision) is not int or revision < 1:
                raise ValueError("Invalid binding")
            binding = Binding(task_id, revision)
        except (KeyError, TypeError, ValueError):
            return self._question("Invalid card action. Please use the current card.")
        message = event.get("message") or {}
        message_ts = event.get("message_ts") or (message.get("ts") if isinstance(message, Mapping) else None)
        if (self.threads.get((channel, message_ts)) != binding or
                self.cards.get((channel, task_id)) != binding):
            return self._question("This card action is stale or ambiguous. Please use the current card.")
        return Route("action", binding=binding, action=name, value=redact_payload(value.get("value")))
