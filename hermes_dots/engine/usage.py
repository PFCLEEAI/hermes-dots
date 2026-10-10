"""Persisted usage counts; no model/provider discovery."""

from datetime import datetime

from .support import now, stamp


class UsageLimitError(RuntimeError):
    pass


class UsageMeter:
    def __init__(self, ledger, settings, clock=None):
        self.ledger = ledger
        self.settings = settings
        self.clock = clock

    def record(self, *, calls=1, tokens=0):
        if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in (calls, tokens)):
            raise ValueError("Usage counts must be nonnegative integers")
        with self.ledger.transaction():
            self.ledger.connection.execute("INSERT INTO usage_entries(occurred_at,calls,tokens) VALUES (?,?,?)",
                                           (stamp(now(self.clock)), calls, tokens))

    def summary(self, start=None, end=None):
        with self.ledger.transaction():
            rows = self.ledger.connection.execute("SELECT * FROM usage_entries ORDER BY id").fetchall()
        rows = [r for r in rows if (start is None or datetime.fromisoformat(r["occurred_at"]) >= start)
                and (end is None or datetime.fromisoformat(r["occurred_at"]) < end)]
        calls, tokens = sum(r["calls"] for r in rows), sum(r["tokens"] for r in rows)
        return {"calls": calls, "tokens": tokens, "line": f"Model usage: {calls} calls, {tokens} tokens"}

    def daily_tokens(self):
        today = self.settings.local_now(now(self.clock)).date()
        with self.ledger.transaction():
            rows = self.ledger.connection.execute("SELECT occurred_at,tokens FROM usage_entries").fetchall()
        return sum(row["tokens"] for row in rows
                   if self.settings.local_now(datetime.fromisoformat(row["occurred_at"])).date() == today)

    def cap_reached(self):
        cap = self.settings.get("daily_token_cap")
        return cap is not None and self.daily_tokens() >= cap
