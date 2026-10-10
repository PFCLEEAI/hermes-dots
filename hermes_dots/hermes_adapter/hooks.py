"""Runtime guards using v2026.9.24 lifecycle kwargs and block directives."""

from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import re
import shlex
from threading import RLock

from hermes_dots.security.paths import PathDeniedError, PathGuard
from hermes_dots.security.secrets import redact
from hermes_dots.engine.support import clean, queue_ping
from hermes_dots.slack.bridge import CARD_REF, parse_command

HOOKS = ["pre_tool_call", "subagent_start", "subagent_stop", "transform_tool_result",
         "post_tool_call", "post_llm_call", "pre_gateway_dispatch", "on_session_end"]
FILE_TOOLS = {"read_file", "write_file", "patch", "search_files"}
OUTSIDE_VERBS = {"send", "post", "delete", "remove", "spend", "purchase", "buy", "account", "connect", "disconnect"}


class RuntimeHooks:
    def __init__(self, runtime, *, guard=None):
        self.runtime = runtime
        self.guard = guard or PathGuard(runtime.settings.get("extra_denied_paths"))
        self.helpers = set()
        self.active_helpers = set()
        self.sessions = {}
        self.lock = RLock()

    def helper(self, session_id):
        with self.lock:
            return session_id in self.helpers

    def event(self, kind, payload):
        self.runtime.brain.evidence.append({"kind": kind, **clean(payload)})

    def _path(self, text):
        if not isinstance(text, str):
            raise TypeError("Path must be a string")
        # Docker paths map only the explicit workspace; never map container /root/.hermes
        # to the host default home.
        if text == "/workspace" or text.startswith("/workspace/"):
            text = self.runtime.root / "workspace" / text.removeprefix("/workspace").lstrip("/")
        elif not str(text).startswith(("/", "~")):
            text = self.runtime.root / "workspace" / text
        return self.guard.check(text)

    def pre_tool_call(self, tool_name, args, *, session_id="", task_id="", **kwargs):
        args = args or {}
        try:
            # Settings are live: denied paths can only grow.
            for path in self.runtime.settings.get("extra_denied_paths"):
                self.guard.add(path)
            if self.helper(session_id) and (tool_name in {"dots_brain_write", "memory", "send_message"}
                                           or "slack" in tool_name):
                raise PermissionError("Helpers cannot write the brain or message the owner")
            if tool_name in FILE_TOOLS:
                paths = [args.get("path", ".")]
                paths += re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", args.get("patch", ""), re.M)
                for path in paths:
                    resolved = self._path(path)
                    if not resolved.is_relative_to(self.runtime.root / "workspace"):
                        raise PermissionError("File tools are limited to the sandbox workspace")
            if tool_name in {"terminal", "execute_code"}:
                command = args.get("command", args.get("code", ""))
                if "~/.hermes" in command or "${HOME}/.hermes" in command or "$HOME/.hermes" in command:
                    raise PathDeniedError("Denied home")
                for path in self.guard.denied_paths:
                    if str(path) in command:
                        raise PathDeniedError("Denied path")
                # Check concrete shell paths including symlinks and traversal.
                for token in shlex.split(command):
                    value = token.split("=", 1)[-1]
                    if value.startswith(("/", "~/", "../")):
                        self._path(value)
                if args.get("cwd"):
                    self._path(args["cwd"])
            action = self._outside_action(tool_name, args)
            if action:
                if self.helper(session_id):
                    raise PermissionError("Helpers cannot perform outside actions")
                binding = self.sessions.get(session_id)
                if not binding:
                    raise PermissionError("Outside action needs an owner-bound task and approval")
                target = str(args.get("target", args.get("channel", args.get("to", ""))))
                payload = json.dumps(args, sort_keys=True, ensure_ascii=False)
                self.runtime.consume_approval(binding, tool_name, action, target, payload)
            return None
        except Exception as error:
            # Hermes logs most hook exceptions and continues; always return a block.
            try:
                self.event("tool_blocked", {"tool_name": tool_name, "args": args, "reason": str(error)})
            except Exception:
                pass
            return {"action": "block", "message": redact(str(error))}

    @staticmethod
    def _outside_action(tool_name, args):
        if tool_name.startswith("dots_") or tool_name in FILE_TOOLS:
            return None
        words = set(re.split(r"[_\W]+", tool_name.casefold()))
        words.update(re.split(r"[_\W]+", str(args.get("action", "")).casefold()))
        found = words & OUTSIDE_VERBS
        if not found:
            return None
        if "send" in found: return "send"
        if "post" in found: return "post"
        if found & {"delete", "remove"}: return "delete"
        if found & {"spend", "purchase", "buy"}: return "spend"
        return "account"

    def subagent_start(self, parent_session_id, child_session_id, **kwargs):
        if not child_session_id:
            raise ValueError("Helper session ID required")
        with self.lock:
            self.helpers.add(child_session_id)
            self.active_helpers.add(child_session_id)
        self.event("subagent_start", {"parent_session_id": parent_session_id, "child_session_id": child_session_id, **kwargs})

    def subagent_stop(self, parent_session_id, child_session_id, **kwargs):
        with self.lock:
            self.active_helpers.discard(child_session_id)
        # Tombstone remains: a resumed helper never becomes an owner session.
        self.event("subagent_stop", {"parent_session_id": parent_session_id, "child_session_id": child_session_id, **kwargs})

    def transform_tool_result(self, tool_name, args, result, **kwargs):
        return redact(result if isinstance(result, str) else json.dumps(clean(result)))

    def post_tool_call(self, **kwargs): self.event("post_tool_call", kwargs)
    def post_llm_call(self, **kwargs): self.event("post_llm_call", kwargs)
    def on_session_end(self, **kwargs): self.event("on_session_end", kwargs)

    def pre_gateway_dispatch(self, event, gateway=None, session_store=None, **kwargs):
        try:
            return self._gateway_dispatch(event, gateway, session_store)
        except Exception as error:
            try:
                self.event("gateway_dispatch_blocked", {"error": type(error).__name__})
                transport = getattr(self.runtime, "transport", None)
                if transport:
                    transport.reply("I couldn't apply that reply. Please use the current card or request a fresh proposal.")
            except Exception:
                pass
            return {"action": "skip", "reason": "Owner dispatch failed closed"}

    def _gateway_dispatch(self, event, gateway, session_store):
        data = asdict(event) if is_dataclass(event) else dict(event) if isinstance(event, dict) else {
            "text": getattr(event, "text", ""), "source": str(getattr(event, "source", ""))}
        self.event("pre_gateway_dispatch", {"text": data.get("text", ""), "user": data.get("user")})
        source = getattr(event, "source", None)
        user = data.get("user") or getattr(source, "user_id", None)
        if isinstance(user, dict): user = user.get("id")
        if user != self.runtime.config.get("owner_slack_id"):
            return {"action": "skip", "reason": "Only the configured owner can command Orca"}
        if source is not None:
            if gateway is not None:
                key = gateway._session_key_for_source(source)
            elif session_store is not None:
                key = session_store._generate_session_key(source)
            else:
                from gateway.session import build_session_key
                key = build_session_key(source, profile=getattr(source, "profile", None))
            self.runtime.config["owner_session_key"] = key
        text = data.get("text", "")
        channel = getattr(source, "chat_id", None) or data.get("channel", "owner")
        thread_ts = getattr(source, "thread_id", None) or data.get("thread_ts")
        raw = getattr(event, "raw_message", None) or data.get("raw_message") or {}
        # Slack also supplies a synthetic root thread for a fresh DM. Only a
        # persisted card or an actual thread reply carries a reply binding.
        if source is not None and (channel, thread_ts) not in self.runtime.bridge.threads and not raw.get("thread_ts"):
            thread_ts = None
        transport = getattr(self.runtime, "transport", None)
        if transport:
            self.runtime.owner_channel(channel)
        def reply(result):
            message = result.get("reply") if isinstance(result, dict) else None
            message = message or json.dumps(clean(result), default=str)
            if transport:
                transport.reply(message, thread_ts=thread_ts)
            else:
                queue_ping(self.runtime.ledger, self.runtime.settings, None,
                           {"kind": "command_reply", "text": message})
        try:
            command = parse_command(text)
        except ValueError:
            command = None
        bound = thread_ts or CARD_REF.search(text)
        if command or bound or self.runtime.engine.pending_questions():
            route = self.runtime.bridge.receive({"user": user, "text": text, "channel": channel,
                                                 "thread_ts": thread_ts})
            result = self.runtime.route(route) if route.kind != "question" else route.text
            reply(result)
            return {"action": "skip", "reason": "Owner reply handled with its card context"}
        if transport and not transport.onboarding.complete and getattr(source, "chat_type", data.get("chat_type", "dm")) == "dm":
            reply(transport.answer_onboarding(text))
            return {"action": "skip", "reason": "Owner onboarding handled without a model call"}
        safe_text = redact(text)
        if safe_text != text: return {"action": "rewrite", "text": safe_text}
        return None
