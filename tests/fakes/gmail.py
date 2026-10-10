"""Read-only mail history with opaque cursors and an explicit gap error."""

from copy import deepcopy
from datetime import datetime


class LostCursorError(RuntimeError):
    pass


class FakeGmail:
    scopes = ("https://www.googleapis.com/auth/gmail.readonly",)

    def __init__(self, messages=(), sent=(), *, lost_cursor=False):
        self.messages = deepcopy(list(messages))
        self.sent = deepcopy(list(sent))
        self.lost_cursor = lost_cursor
        self.calls = []

    @property
    def cursor(self):
        return str(len(self.messages))

    def history(self, cursor):
        self.calls.append(("history", cursor))
        try:
            offset = int(cursor)
        except (TypeError, ValueError):
            raise LostCursorError("invalid fake history cursor") from None
        if self.lost_cursor or offset < 0 or offset > len(self.messages):
            raise LostCursorError("fake history cursor lost; resync required")
        return {"messages": deepcopy(self.messages[offset:]), "cursor": self.cursor}

    def list_messages(self, *, unread_only=False, since=None):
        self.calls.append(("list_messages", unread_only, since))
        messages = self.messages
        if unread_only:
            messages = [m for m in messages if m.get("unread", False)]
        if since is not None:
            messages = [m for m in messages if datetime.fromisoformat(m["date"]) >= since]
        return {"messages": deepcopy(messages), "cursor": self.cursor}

    def get_message(self, message_id):
        self.calls.append(("get_message", message_id))
        for message in self.messages + self.sent:
            if message["message_id"] == message_id:
                return deepcopy(message)
        raise KeyError(message_id)

    def sent_mail(self):
        self.calls.append(("sent_mail",))
        return deepcopy(self.sent)

    def add_message(self, message):
        """Test setup only: simulate a newly received message."""
        self.messages.append(deepcopy(message))

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise AssertionError(f"FakeGmail forbids unsupported or mutating call: {name}")
