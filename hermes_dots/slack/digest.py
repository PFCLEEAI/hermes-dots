"""Digest providers register independently; sections follow spec §10.4."""

from typing import Callable

from .delivery import redact_payload


SECTIONS = (
    ("regular_mail", "Regular mail"), ("uncertain_mail", "Not sure"),
    ("noise", "Noise count / show list"), ("ideas", "Ideas"),
    ("learned", "Learned items (undo)"), ("conflicts", "Conflicts / pick version"),
    ("promotions", "Promotion taps"), ("proposals", "Self-improvement proposals"),
    ("questions", "Pending questions"), ("replied_still_open", "Replied, still open"),
    ("missed_runs", "Missed runs"), ("health", "Health"), ("usage", "Usage"),
)


class DigestComposer:
    def __init__(self):
        self.providers = {key: [] for key, _ in SECTIONS}

    def register(self, section: str, provider: Callable[[], str | dict | list]) -> None:
        if section not in self.providers:
            raise ValueError(f"Unknown digest section: {section}")
        self.providers[section].append(provider)

    def compose(self) -> dict:
        """Providers may return text or rendered cards to retain their buttons."""
        text, buttons = ["Digest"], []
        for key, title in SECTIONS:
            text.append(title)
            items = []
            for provider in self.providers[key]:
                result = provider()
                items.extend(result if isinstance(result, (list, tuple)) else [result])
            items = [item for item in items if item is not None and item != ""]
            for item in items:
                if isinstance(item, dict):
                    text.append(str(item.get("text", "")))
                    buttons.extend(item.get("buttons", ()))
                elif item is not None:
                    text.append(str(item))
            if not items:
                text.append("Model calls / tokens: unavailable." if key == "usage" else "None.")
        return redact_payload({"text": "\n".join(text), "buttons": buttons})
