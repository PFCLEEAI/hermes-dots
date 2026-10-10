from datetime import datetime, timezone

from fakes import FakeClock
from hermes_dots.health import HealthMonitor, PROBE_NAMES
from hermes_dots.ledger import Ledger
from hermes_dots.settings import SettingsService


def test_every_probe_failure_immediate_and_ping_held_persisted(tmp_path):
    path = tmp_path / "ledger.sqlite"
    clock = FakeClock(datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc))
    calls = []

    def failed_mail():
        calls.append("mail")
        raise OSError("Sign-in required")

    probes = {name: lambda name=name: calls.append(name) or True for name in PROBE_NAMES}
    probes["mail"] = failed_mail
    with Ledger(path) as ledger:
        settings = SettingsService(ledger)
        settings.confirm_change(settings.propose_change("timezone", "UTC"))
        monitor = HealthMonitor(ledger, settings, probes, clock=clock)
        status = monitor.tick()
        assert sorted(calls) == sorted(PROBE_NAMES)
        assert not status["mail"]["ok"]
        assert "Sign-in required" in status["mail"]["detail"]
        assert len([event for event in ledger.list_events() if event.kind == "health_probe"]) == 6
        assert monitor.release_pings() == ()
        assert ledger.list_effects() == ()
        monitor.tick()
        assert ledger.connection.execute("SELECT count(*) FROM ping_queue").fetchone()[0] == 1
    with Ledger(path) as ledger:
        monitor = HealthMonitor(ledger, SettingsService(ledger), probes, clock=clock)
        assert not monitor.status()["mail"]["ok"]
        clock.set(datetime(2026, 10, 13, 8, tzinfo=timezone.utc))
        assert len(monitor.release_pings()) == 1
        assert len(ledger.list_effects()) == 1
        assert monitor.release_pings() == ()


def test_missing_probe_fails_closed_and_recovery_allows_next_alert(tmp_path):
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        monitor = HealthMonitor(ledger, SettingsService(ledger), {})
        assert all(not value["ok"] for value in monitor.tick().values())
        monitor.probes = {name: lambda: {"ok": True} for name in PROBE_NAMES}
        assert all(value["ok"] for value in monitor.tick().values())
        monitor.probes["slack"] = lambda: {"ok": False, "detail": "Access denied"}
        assert monitor.tick()["slack"]["detail"] == "Access denied"
        assert ledger.connection.execute("SELECT count(*) FROM ping_queue").fetchone()[0] == 7
