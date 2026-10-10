"""Offline fixture source and the small read-only mail-source contract."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime


class CursorLost(RuntimeError):
    """The source can no longer account for the saved history position."""


@dataclass(frozen=True)
class MailBatch:
    messages: tuple[dict, ...]
    cursor: str
    gap: bool = False


class FixtureMailSource:
    def __init__(self, messages=(), sent=()):
        self.messages = deepcopy(list(messages))
        self.sent = deepcopy(list(sent))

    def poll(self, cursor=None, *, since=None):
        end = len(self.messages)
        if cursor is None:
            messages = [m for m in self.messages if m.get("unread") and
                        (since is None or datetime.fromisoformat(m["date"]) >= since)]
        else:
            try:
                offset = int(cursor)
                if not 0 <= offset <= end:
                    raise ValueError
            except (ValueError, TypeError):
                return MailBatch(tuple(deepcopy(self.messages)), str(end), gap=True)
            messages = self.messages[offset:]
        return MailBatch(tuple(deepcopy(messages)), str(end))

    def sent_mail(self):
        return deepcopy(self.sent)
