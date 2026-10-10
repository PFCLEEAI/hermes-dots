"""In-memory posts and incoming events with no Slack connection."""

from copy import deepcopy


class FakeSlack:
    def __init__(self, *, owner_id="U_OWNER", channel="C_PRACTICE"):
        self.owner_id = owner_id
        self.channel = channel
        self.posts = []
        self.events = []

    def post(self, text, *, channel=None, thread_ts=None, buttons=(), metadata=None):
        message = {"ts": str(len(self.posts) + 1), "channel": channel or self.channel,
                   "text": text, "thread_ts": thread_ts, "buttons": deepcopy(list(buttons)),
                   "metadata": deepcopy(metadata or {})}
        self.posts.append(message)
        return deepcopy(message)

    def update(self, ts, *, text=None, buttons=None):
        for message in self.posts:
            if message["ts"] == ts:
                if text is not None:
                    message["text"] = text
                if buttons is not None:
                    message["buttons"] = deepcopy(list(buttons))
                return deepcopy(message)
        raise KeyError(ts)

    def history(self, *, channel=None):
        return deepcopy([m for m in self.posts if m["channel"] == (channel or self.channel)])

    def thread(self, ts):
        return deepcopy([m for m in self.posts if m["ts"] == ts or m["thread_ts"] == ts])

    def message(self, text, *, owner=True, thread_ts=None):
        event = {"type": "message", "user": self.owner_id if owner else "U_OTHER",
                 "channel": self.channel, "text": text, "thread_ts": thread_ts}
        self.events.append(event)
        return deepcopy(event)

    def click(self, ts, action_id, *, owner=True):
        message = next(m for m in self.posts if m["ts"] == ts)
        button = next(b for b in message["buttons"] if b["action_id"] == action_id)
        event = {"type": "block_actions", "user": self.owner_id if owner else "U_OTHER",
                 "channel": message["channel"], "message_ts": ts, "action": deepcopy(button)}
        self.events.append(event)
        return deepcopy(event)
