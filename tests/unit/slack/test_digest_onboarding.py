"""Registered digest sections and setup with a local settings-service stub."""

import pytest

from hermes_dots.ledger import Card
from hermes_dots.slack import DigestComposer, Onboarding, SECTIONS, render_card


class StubSettings:
    """T5 validates/stores; this stub records that confirmation is required."""

    def __init__(self):
        self.proposals, self.stored = [], {}

    def propose_change(self, key, value):
        if key == "timezone" and value == "invalid":
            raise ValueError("Invalid timezone")
        proposal = {"name": key, "value": value}
        self.proposals.append(proposal)
        return proposal

    def confirm_change(self, proposal):
        self.stored[proposal["name"]] = proposal["value"]
        return proposal["value"]


def test_digest_order_independent_of_registration():
    composer = DigestComposer()
    calls = []
    for key, _ in reversed(SECTIONS):
        def provider(key=key):
            calls.append(key)
            return f"item:{key}"
        composer.register(key, provider)
    result = composer.compose()
    assert calls == [key for key, _ in SECTIONS]
    offsets = [result["text"].index(f"item:{key}") for key, _ in SECTIONS]
    assert offsets == sorted(offsets)


def test_digest_multiple_providers_retains_buttons():
    composer = DigestComposer()
    learned = render_card(Card("note-1", "Learned"), actions=["undo_learned"])
    promotion = render_card(Card("note-2", "Promote"), actions=["approve", "reject"])
    composer.register("learned", lambda: learned)
    composer.register("learned", lambda: "Second learned item")
    composer.register("promotions", lambda: [promotion])
    result = composer.compose()
    assert len(result["buttons"]) == 3
    assert result["text"].index("#note-1") < result["text"].index("Second learned item") < result["text"].index("#note-2")
    for _, title in SECTIONS:
        assert title in result["text"]
    assert "Model calls / tokens: unavailable" in result["text"]
    with pytest.raises(ValueError):
        composer.register("not-a-section", lambda: "value")


def test_usage_provider():
    composer = DigestComposer()
    composer.register("usage", lambda: "Model calls: 2 · Tokens: 100")
    assert composer.compose()["text"].endswith("Model calls: 2 · Tokens: 100")


def test_onboarding_confirm_then_store_every_answer():
    settings = StubSettings()
    chat = Onboarding(settings, owner_id="owner")
    answers = ["UTC", "23:00 to 08:00", "09:00 and 18:00", "skip", "Watch practice email"]
    for answer in answers:
        before = settings.stored.copy()
        assert "Reply yes" in chat.answer("owner", answer)
        assert settings.stored == before
        assert "Stored" in chat.answer("owner", "yes")
    assert chat.complete
    assert settings.stored == {"timezone": "UTC", "quiet_hours": ["23:00", "08:00"],
                               "digest_times": ["09:00", "18:00"], "vips": [],
                               "first_responsibility": "Watch practice email"}
    assert chat.answer("owner", "anything") == "Setup complete."


def test_onboarding_owner_only_and_correction():
    settings = StubSettings()
    chat = Onboarding(settings, owner_id="owner")
    assert chat.answer("other", "UTC") is None
    assert not settings.proposals
    assert "invalid" in chat.answer("owner", "invalid")
    assert chat.index == 0 and chat.pending is None
    chat.answer("owner", "UTC")
    assert "yes" in chat.answer("owner", "maybe")
    assert chat.answer("owner", "no") == chat.prompt()
    assert not settings.stored
    chat.answer("owner", "Asia/Seoul")
    chat.answer("owner", "yes")
    assert settings.stored["timezone"] == "Asia/Seoul"
    assert "HH:MM" in chat.answer("owner", "23:00 to 08:00 extra")
    assert chat.pending is None


def test_vip_list_and_failed_storage_does_not_advance():
    settings = StubSettings()
    chat = Onboarding(settings, owner_id="owner")
    chat.index = 3
    chat.answer("owner", "alex@example.com, you@example.com")
    chat.answer("owner", "yes")
    assert settings.stored["vips"] == ["alex@example.com", "you@example.com"]
    chat.answer("owner", "Watch practice email")

    def fail(_):
        raise OSError("Storage unavailable")

    settings.confirm_change = fail
    with pytest.raises(OSError):
        chat.answer("owner", "yes")
    assert not chat.complete and chat.pending is not None
