"""Short owner-only setup chat, with confirm-before-store through T5."""

import re
from typing import Mapping, Protocol
from zoneinfo import ZoneInfoNotFoundError

from .delivery import redact_payload


# New settings are merged only by the shared wiring step.
DEFAULTS = {"first_responsibility": None}
QUESTIONS = (
    ("timezone", "What time zone should I use?"),
    ("quiet_hours", "What quiet hours should I use? (for example, 23:00 to 08:00)"),
    ("digest_times", "When should I send digests? (for example, 09:00 and 18:00)"),
    ("vips", "Any VIP email addresses? You can say skip."),
    ("first_responsibility", "What is my first responsibility?"),
)


class SettingsService(Protocol):
    """T5 owns validation, persistence and feedback; Slack owns the chat."""

    def propose_change(self, key: str, value: object) -> object: ...

    def confirm_change(self, proposal: object) -> object: ...


class Onboarding:
    def __init__(self, settings: SettingsService, *, owner_id: str):
        if not owner_id:
            raise ValueError("An owner ID is required")
        self.settings, self.owner_id = settings, owner_id
        self.index, self.pending = 0, None

    @property
    def complete(self) -> bool:
        return self.index == len(QUESTIONS)

    def prompt(self) -> str:
        return "Setup complete." if self.complete else QUESTIONS[self.index][1]

    def answer(self, user_id: str, text: str) -> str | None:
        if user_id != self.owner_id:
            return None
        if self.complete:
            return self.prompt()
        text = redact_payload(text.strip())
        if self.pending is not None:
            if text.lower() in {"yes", "confirm"}:
                proposal, key, value = self.pending
                # Do not advance on failed storage: the owner can correct/retry.
                stored = self.settings.confirm_change(proposal)
                self.pending = None
                self.index += 1
                if isinstance(stored, Mapping) and "reply" in stored:
                    return redact_payload(f"{stored['reply']}\n{self.prompt()}")
                return redact_payload(f"Stored {key}: {stored if stored is not None else value}\n{self.prompt()}")
            if text.lower() in {"no", "cancel"}:
                self.pending = None
                return self.prompt()
            return "Please reply yes to confirm, or no to change the answer."
        key, _ = QUESTIONS[self.index]
        if not text:
            return self.prompt()
        if key == "vips":
            value = [] if text.lower() == "skip" else re.split(r"[,\s]+", text)
        elif key in {"quiet_hours", "digest_times"}:
            value = re.findall(r"\d{1,2}:\d{2}", text)
            # Reject leftover input rather than silently discarding an answer.
            rest = re.sub(r"\d{1,2}:\d{2}|\b(?:to|and)\b|[,\s–-]", "", text)
            if rest or not value or (key == "quiet_hours" and len(value) != 2):
                return f"Use HH:MM times. {self.prompt()}"
            value = [item.zfill(5) for item in value]
        else:
            value = text
        try:
            proposal = self.settings.propose_change(key, value)
        except (ValueError, ZoneInfoNotFoundError):
            return f"That setting is invalid. {self.prompt()}"
        self.pending = proposal, key, value
        return f"Set {key} to {value}? Reply yes to confirm."
