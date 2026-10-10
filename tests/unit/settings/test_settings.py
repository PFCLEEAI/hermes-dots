from datetime import datetime, timezone

import pytest

from fakes import FakeModel
from hermes_dots.ledger import Ledger
from hermes_dots.security.paths import PathDeniedError
from hermes_dots.security.rules import RuleViolation
from hermes_dots.settings import SettingsService


@pytest.fixture
def settings(tmp_path):
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        yield SettingsService(ledger, home=tmp_path / "home")


def test_confirm_store_feedback_persistence_and_effect(tmp_path):
    path = tmp_path / "ledger.sqlite"
    feedback = []
    with Ledger(path) as ledger:
        settings = SettingsService(ledger, feedback=feedback.append)
        settings.confirm_change(settings.propose_change("timezone", "UTC"))
        proposal = settings.parse_change("make quiet hours 22 to 7")
        assert "may I go ahead?" in proposal["confirmation"]
        assert settings.get("quiet_hours") == ["23:00", "08:00"]
        moment = datetime(2026, 10, 12, 22, 30, tzinfo=timezone.utc)
        assert not settings.is_quiet(moment)
        stored = settings.confirm_change(proposal)
        assert stored["value"] == ["22:00", "07:00"]
        assert "Stored quiet_hours" in stored["reply"]
        assert settings.is_quiet(moment)
        assert settings.quiet_end(moment).hour == 7
        assert feedback[-1]["kind"] == "setting_change"
        assert ledger.list_events()[-1].kind == "feedback"
        with pytest.raises(ValueError):
            settings.confirm_change(proposal)
    with Ledger(path) as ledger:
        assert SettingsService(ledger).get("quiet_hours") == ["22:00", "07:00"]


@pytest.mark.parametrize("text,name,value", [
    ("add VIP alex@example.com", "vips", ["alex@example.com"]),
    ("digest only at 9", "digest_times", ["09:00"]),
    ("make quiet hours 22 to 7", "quiet_hours", ["22:00", "07:00"]),
])
def test_documented_fallback_during_malformed_or_limit(settings, text, name, value):
    settings.model = FakeModel(mode="malformed")
    assert settings.parse_change(text)["value"] == value
    settings.model.mode = "limit"
    proposal = settings.parse_change(text)
    assert proposal["name"] == name
    assert settings.confirm_change(proposal)["value"] == value


def test_model_structured_change_and_all_settings_supported(settings):
    settings.model = FakeModel({None: {"name": "display_name", "value": "Acme helper"}})
    assert settings.parse_change("call yourself Acme helper")["value"] == "Acme helper"
    assert settings.model.calls == 1
    assert settings.model.requests[0]["schema"]["type"] == "object"
    assert {"timezone", "quiet_hours", "digest_times", "vips", "check_back_min_seconds", "check_back_max_seconds",
            "mail_check_interval_seconds", "snooze_days", "idea_expiry_days", "index_times", "log_retention_days",
            "daily_token_cap", "action_rules", "extra_denied_paths", "display_name"} <= settings.list_settings().keys()


def test_floor_add_only_paths_and_builtin_denial(settings, tmp_path):
    with pytest.raises(RuleViolation):
        settings.propose_change("action_rules", {"send": "auto"})
    proposal = settings.propose_change("action_rules", {"send": "hand_to_owner"})
    settings.confirm_change(proposal)
    assert settings.action_rules().level("send") == "hand_to_owner"
    denied = str(tmp_path / "private")
    settings.confirm_change(settings.propose_change("extra_denied_paths", [denied]))
    with pytest.raises(PermissionError, match="add-only"):
        settings.propose_change("extra_denied_paths", [])
    guard = settings.path_guard()
    with pytest.raises(PathDeniedError):
        guard.check(tmp_path / "home" / ".hermes" / "config.yaml")
    with pytest.raises(PathDeniedError):
        guard.check(tmp_path / "private" / "file.txt")


@pytest.mark.parametrize("name,value", [
    ("daily_token_cap", -1), ("daily_token_cap", True), ("log_retention_days", 0),
    ("quiet_hours", ["25:00", "08:00"]), ("quiet_hours", ["23:00"]),
    ("digest_times", "09:00"), ("timezone", "No/SuchZone"), ("vips", ["bad"]),
    ("check_back_min_seconds", 999999), ("display_name", ""), ("unknown", 1),
])
def test_validation(settings, name, value):
    with pytest.raises((ValueError, KeyError)):
        settings.propose_change(name, value)


def test_stale_confirmation_does_not_overwrite(settings):
    first = settings.propose_change("quiet_hours", ["22:00", "07:00"])
    stale = settings.propose_change("quiet_hours", ["21:00", "06:00"])
    settings.confirm_change(first)
    with pytest.raises(ValueError, match="fresh confirmation"):
        settings.confirm_change(stale)
    assert settings.get("quiet_hours") == ["22:00", "07:00"]


def test_secret_not_stored_or_sent_in_parse_prompt(settings):
    token = "sk" + "-" + "invented-example-value"
    with pytest.raises(ValueError, match="Secrets"):
        settings.propose_change("display_name", token)
    settings.model = FakeModel({None: {"name": "display_name", "value": "Orca"}})
    settings.parse_change("change name " + token)
    assert token not in str(settings.model.requests)
