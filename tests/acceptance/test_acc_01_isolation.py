"""§13.1: temp install, protected paths, no profile overwrite."""

import json
from pathlib import Path
import plistlib
import subprocess

import pytest

from hermes_dots.install import HERMES_SHA, InstallError, install
from hermes_dots.security.paths import PathGuard
from .invariants import REPO, assert_invariants


class InstallRunner:
    """Record install commands and emulate only their necessary local outputs."""
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[:2] == ["git", "init"]:
            Path(argv[2]).mkdir()
        if "venv" in argv:
            (Path(argv[-1]) / "bin").mkdir(parents=True)
        return subprocess.CompletedProcess(argv, 0, stdout="3.11\n" if "-I" in argv else "")


def test_isolated_install_and_refusals(scenario, tmp_path):
    s = scenario
    runner = InstallRunner()
    denied = tmp_path / "denied"
    result = install(tmp_path / "installed", source_root=REPO, run=runner,
                     owner_slack_id=s.slack.owner_id, timezone="UTC", extra_denied_paths=[denied])
    receipt = json.loads((result.home / "install-receipt.json").read_text())
    assert receipt["hermes_ref"] == HERMES_SHA and not receipt["service_loaded"]
    assert any(HERMES_SHA in argv for argv, _ in runner.calls)
    assert not any("launchctl" in argv for argv, _ in runner.calls)
    for _, kwargs in runner.calls:
        assert kwargs["env"]["HERMES_HOME"] == str(result.profile)
        assert kwargs["env"]["HERMES_KANBAN_HOME"] == str(result.home / "kanban")
    with result.plist.open("rb") as stream:
        assert plistlib.load(stream)["WorkingDirectory"] == str(result.home / "workspace")
    terminal = json.loads((result.profile / "config.yaml").read_text())["terminal"]
    assert terminal["backend"] == "docker" and terminal["container_persistent"] is False
    assert terminal["docker_volumes"] == [f"{result.home / 'workspace'}:/workspace:rw"]
    assert terminal["credential_files"] == terminal["docker_forward_env"] == []
    guard = PathGuard([denied])
    for target in (Path.home() / ".hermes/config.yaml", denied / "notes.md"):
        with pytest.raises(PermissionError):
            guard.check(target)
    calls_before = len(runner.calls)
    for target in (Path.home() / ".hermes", denied / "dots", result.home):
        with pytest.raises(InstallError):
            install(target, source_root=REPO, run=runner, owner_slack_id=s.slack.owner_id,
                    extra_denied_paths=[denied])
    assert len(runner.calls) == calls_before
    assert not (Path.home() / ".hermes").exists() and not denied.exists()
    assert json.loads((result.home / "install-receipt.json").read_text()) == receipt
    # Docker availability affects code work, never the ability to install offline.
    card = s.working()
    s.runtime.health.probes["docker"] = lambda: False
    s.engine.tick()
    called = []
    with pytest.raises(PermissionError):
        s.engine.run_step(card.id, "code", "input", lambda: called.append(True), action="code")
    assert not called and s.ledger.get_task(card.id).blocked_reason == "health"
    assert_invariants(s)
