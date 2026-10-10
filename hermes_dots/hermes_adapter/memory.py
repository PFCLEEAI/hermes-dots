"""Pinned Hermes memory contract; lexical, bounded recall with source citations."""

from pathlib import Path
import os

from agent.memory_provider import MemoryProvider
from hermes_dots.brain import Brain
from hermes_dots.security.paths import PathGuard
from hermes_dots.engine.support import clean


class BrainMemoryProvider(MemoryProvider):
    name = "hermes_dots_brain"

    def __init__(self):
        self.brain = None
        self.contexts = {}

    def is_available(self):
        return True  # Local stdlib storage, no credential or network requirement.

    def initialize(self, session_id, **kwargs):
        profile = PathGuard().check(kwargs["hermes_home"])
        root = PathGuard().check(os.environ.get("HERMES_DOTS_HOME") or profile.parents[1])
        self.brain = Brain(root)
        self.contexts[session_id] = kwargs.get("agent_context", "primary")

    def system_prompt_block(self):
        return "Search the brain before work. Cite recalled notes. Explicit owner instructions override learned notes."

    def prefetch(self, query, *, session_id=""):
        if self.brain is None:
            return ""
        lines = []
        for hit in self.brain.search(query[:1000], limit=5):
            lines.append(f"[{hit['id']}] {hit['snippet'][:500]} — {hit['citation']}")
        return clean("\n".join(lines))[:4000]

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None, turn_author=None):
        if self.brain is not None and self.contexts.get(session_id) == "primary":
            self.brain.evidence.append({"kind": "conversation", "session_id": session_id,
                                       "user": user_content, "assistant": assistant_content,
                                       "messages": messages or [], "author": turn_author})

    def on_session_switch(self, new_session_id, *, parent_session_id="", **kwargs):
        self.contexts[new_session_id] = self.contexts.get(parent_session_id, "primary")

    def get_tool_schemas(self):
        return []  # General plugin owns the tools; one registration per tool.

    def shutdown(self):
        self.brain = None
        self.contexts.clear()
