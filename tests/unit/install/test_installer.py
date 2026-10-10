import ast
import html
import io
import json
import logging
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

from hermes_dots.install import HERMES_SHA, InstallError, install
from hermes_dots.install.installer import _extract
from hermes_dots.install import installer


@pytest.fixture(autouse=True)
def isolated_user_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    for relative in ("hermes_dots", "profiles/orca", "plugins/memory/hermes_dots_brain"):
        (root / relative).mkdir(parents=True)
    repo = Path(__file__).resolve().parents[3]
    for name in ("config.yaml", "SOUL.md"):
        (root / "profiles/orca" / name).write_bytes((repo / "profiles/orca" / name).read_bytes())
    for name in ("pyproject.toml", "README.md", "LICENSE", "plugin.yaml", "hermes-pack.yaml"):
        (root / name).write_text("# invented fixture\n")
    (root / "plugins/memory/hermes_dots_brain/plugin.yaml").write_text("name: hermes_dots_brain\n")
    (root / "plugins/memory/hermes_dots_brain/__init__.py").write_text("# provider fixture\n")
    (root / "tests/fixtures").mkdir(parents=True)
    (root / "tests/fixtures/mail.json").write_bytes((repo / "tests/fixtures/mail.json").read_bytes())
    return root


class FakeRun:
    def __init__(self, source=None, version="3.13"):
        self.calls = []
        self.source = source
        self.version = version

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if "-I" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=self.version + "\n")
        if argv[:2] == ["git", "init"]:
            Path(argv[2]).mkdir()
        elif "archive" in argv:
            # Emulate git archive in memory; never execute package/network commands.
            with tarfile.open(fileobj=kwargs["stdout"], mode="w") as archive:
                if argv[-1] == HERMES_SHA:
                    data = b"# pinned Hermes fixture\n"
                    member = tarfile.TarInfo("pyproject.toml")
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
                else:
                    for child in self.source.iterdir():
                        archive.add(child, arcname=child.name)
        elif "venv" in argv:
            (Path(argv[-1]) / "bin").mkdir(parents=True)
        return subprocess.CompletedProcess(argv, 0)


def test_temp_install_pinned_and_confined(tmp_path, source):
    home = tmp_path / "isolated"
    fake = FakeRun()
    result = install(home, source_root=source, run=fake,
                     owner_slack_id="invented-owner", timezone="UTC",
                     extra_denied_paths=[tmp_path / "unrelated"])
    assert result.profile == home / "profiles/orca"
    receipt = json.loads((home / "install-receipt.json").read_text())
    assert receipt["hermes_ref"] == HERMES_SHA
    assert receipt["pack_ref"] == "working-tree"
    assert receipt["service_loaded"] is False
    assert any(HERMES_SHA in argv for argv, _ in fake.calls)
    assert all("launchctl" not in argv for argv, _ in fake.calls)
    assert json.loads((home / "local-config.json").read_text())["owner_slack_id"] == "invented-owner"
    assert (home / "local-config.json").stat().st_mode & 0o777 == 0o600
    assert (home / "brain/shared/Decisions").is_dir()
    assert (home / "evidence").is_dir()
    assert (result.profile / "plugins/hermes_dots_brain/__init__.py").is_file()
    assert (home / "practice-mail.json").read_bytes() == (source / "tests/fixtures/mail.json").read_bytes()
    assert json.loads((home / "practice-mail.json").read_text())
    assert (home / "secrets.env").read_bytes() == b""
    assert (home / "secrets.env").stat().st_mode & 0o777 == 0o600
    assert (result.profile / "plugins/hermes-dots/__init__.py").read_text().strip() == (
        "from hermes_dots.hermes_adapter.register import register")
    for _, kwargs in fake.calls:
        env = kwargs["env"]
        assert env["HERMES_HOME"] == str(result.profile)
        assert env["HERMES_DOTS_HOME"] == str(home)
        assert env["HERMES_KANBAN_HOME"] == str(home / "kanban")
        for key in ("TMPDIR", "PIP_CACHE_DIR", "PYTHONPYCACHEPREFIX"):
            assert Path(env[key]).is_relative_to(home)
    assert (tmp_path / "unrelated").exists() is False


def test_profile_config_tick_and_valid_unloaded_plist(tmp_path, source):
    home = tmp_path / "isolated home"
    fake = FakeRun()
    result = install(home, source_root=source, run=fake, timezone="UTC", owner_slack_id="invented-owner")
    config = json.loads((result.profile / "config.yaml").read_text())
    assert config["memory"] == {"provider": "hermes_dots_brain", "write_approval": True}
    assert config["skills"]["write_approval"] is True
    assert config["skills"]["project_discovery"] is False
    assert config["skills"]["external_dirs"] == []
    assert config["skills"]["trusted_project_dirs"] == []
    assert config["cron"]["catch_up_missed"] is False
    assert config["plugins"]["entries"]["hermes-dots"]["allow_gateway_injection"] is True
    terminal = config["terminal"]
    assert terminal["backend"] == "docker"
    assert terminal["container_persistent"] is False
    assert terminal["docker_mount_cwd_to_workspace"] is False
    assert terminal["docker_volumes"] == [f"{home / 'workspace'}:/workspace:rw"]
    assert terminal["credential_files"] == []
    assert terminal["docker_forward_env"] == []
    cron = json.loads(fake.calls[-1][0][-1])
    assert cron["no_agent"] is True
    assert cron["script"] == str(result.profile / "scripts/dots-tick.sh")
    assert cron["name"] == "hermes-dots-tick"
    assert cron["deliver"] == "none"
    script = (result.profile / "scripts/dots-tick.sh").read_text()
    assert "-m hermes_dots.hermes_adapter.tick" in script
    assert "export HERMES_HOME=" in script
    assert "export HERMES_DOTS_HOME=" in script
    with result.plist.open("rb") as stream:
        plist = plistlib.load(stream)
    assert plist["ProgramArguments"] == [str(result.launcher), "gateway", "run"]
    assert plist["EnvironmentVariables"]["HERMES_HOME"] == str(result.profile)
    assert result.plist.is_relative_to(home)
    for key in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
        assert Path(plist[key]).is_relative_to(home)


def test_local_archive_and_exact_pack_ref(tmp_path, source):
    fake = FakeRun(source)
    result = install(tmp_path / "isolated", source_root=source, hermes_source=tmp_path / "clone",
                     pack_ref="a" * 40, run=fake, timezone="UTC", owner_slack_id="invented-owner")
    archives = [argv for argv, _ in fake.calls if "archive" in argv]
    assert [argv[-1] for argv in archives] == [HERMES_SHA, "a" * 40]
    assert not any("fetch" in argv for argv, _ in fake.calls)
    assert not list((result.home / "tmp").glob("*.tar"))
    assert json.loads((result.home / "install-receipt.json").read_text())["pack_mode"] == "immutable"
    pack = json.loads((result.home / "hermes-pack.yaml").read_text())
    assert all(entry["ref"] == "a" * 40 for entry in pack["plugins"])
    assert all(Path(entry["repo"]).is_relative_to(result.home) for entry in pack["plugins"])


def test_exact_ref_install_uses_archived_practice_fixture(tmp_path, source):
    archived = tmp_path / "archived-source"
    shutil.copytree(source, archived)
    mail = [{"account": "practice", "message_id": "archived", "sender": "alex@example.com"}]
    (archived / "tests/fixtures/mail.json").write_text(json.dumps(mail))
    result = install(tmp_path / "isolated", source_root=source, pack_ref="a" * 40,
                     run=FakeRun(archived), owner_slack_id="invented-owner")
    assert json.loads((result.home / "practice-mail.json").read_text()) == mail
    assert (source / "tests/fixtures/mail.json").read_bytes() != (result.home / "practice-mail.json").read_bytes()


@pytest.mark.parametrize("relative", ["secrets.env", "practice-mail.json"])
def test_existing_local_seed_refused_before_mutation(tmp_path, source, relative):
    home = tmp_path / "isolated"
    home.mkdir()
    sentinel = home / relative
    sentinel.write_text("preserve this")
    fake = FakeRun()
    with pytest.raises(InstallError, match="overwrite"):
        install(home, source_root=source, run=fake, owner_slack_id="invented-owner")
    assert sentinel.read_text() == "preserve this"
    assert not fake.calls


@pytest.mark.parametrize("document", ["README.md", "docs/install.md", "site/index.html"])
def test_documented_quickstart_accepts_installer_cli(tmp_path, monkeypatch, document):
    repo = Path(__file__).resolve().parents[3]
    text = html.unescape((repo / document).read_text())
    # Extract just the command from the HTML code block, preserving its quoted placeholder.
    line = text.split("python3 scripts/install.py ", 1)[1].split("\n", 1)[0].split("</code>", 1)[0]
    args = shlex.split(line.replace("<your Slack member ID>", "invented-owner"))
    calls = []
    def fake_install(home, **kwargs):
        calls.append((home, kwargs))
        return SimpleNamespace(profile=tmp_path / "profile", launcher=tmp_path / "bin/hermes-dots", plist=tmp_path / "item.plist")
    monkeypatch.setattr(installer, "install", fake_install)
    assert installer.main(args) == 0
    assert calls[0][0] == "~/.hermes-dots"
    assert calls[0][1]["owner_slack_id"] == "invented-owner"
    assert "Python 3.11–3.13" in text


def test_installer_help_matches_documented_options(capsys):
    with pytest.raises(SystemExit) as result:
        installer.main(["--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    for option in ("--home", "--owner-slack-id", "--pack-ref", "--hermes-source", "--python"):
        assert option in help_text
    assert "Python 3.11–3.13" in help_text


@pytest.mark.parametrize("child", ["", "profiles/orca"])
def test_stock_home_refused_without_read_or_write(tmp_path, source, monkeypatch, child):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    fake = FakeRun()
    with pytest.raises(InstallError, match="existing Hermes home"):
        install(tmp_path / ".hermes" / child, source_root=source, run=fake)
    assert not fake.calls
    assert not (tmp_path / ".hermes").exists()


def test_existing_profile_refused_before_mutation(tmp_path, source):
    home = tmp_path / "isolated"
    profile = home / "profiles/orca"
    profile.mkdir(parents=True)
    sentinel = profile / "SOUL.md"
    sentinel.write_text("preserve this")
    fake = FakeRun()
    with pytest.raises(InstallError, match="overwrite"):
        install(home, source_root=source, run=fake)
    assert sentinel.read_text() == "preserve this"
    assert not fake.calls
    assert not (home / "runtime").exists()


@pytest.mark.parametrize("relative", ["profiles", "runtime", "brain/shared"])
def test_destination_symlink_escape_refused(tmp_path, source, relative):
    home = tmp_path / "isolated"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    target = home / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)
    fake = FakeRun()
    with pytest.raises(InstallError, match="escapes|symlink"):
        install(home, source_root=source, run=fake)
    assert not list(outside.iterdir())
    assert not fake.calls


def test_extra_deny_and_nonimmutable_ref_refused(tmp_path, source):
    fake = FakeRun()
    with pytest.raises(InstallError, match="denied"):
        install(tmp_path / "blocked/root", source_root=source, run=fake,
                extra_denied_paths=[tmp_path / "blocked"])
    with pytest.raises(InstallError, match="40-character"):
        install(tmp_path / "isolated", source_root=source, run=fake, pack_ref="main")
    assert not fake.calls


def test_install_home_inside_public_repo_refused(source):
    with pytest.raises(InstallError, match="outside the public"):
        install(source / "private", source_root=source, run=FakeRun())
    assert not (source / "private").exists()


def test_stock_home_ancestor_refused(tmp_path, source):
    fake = FakeRun()
    with pytest.raises(InstallError, match="existing Hermes home"):
        install(Path.home(), source_root=source, run=fake)
    assert not fake.calls
    assert not (tmp_path / "user").exists()


def test_empty_owner_refused_before_mutation(tmp_path, source):
    home = tmp_path / "isolated"
    fake = FakeRun()
    with pytest.raises(InstallError, match="owner-slack-id"):
        install(home, source_root=source, run=fake)
    assert not home.exists()
    assert not fake.calls


def test_unsupported_python_refused_before_writes(tmp_path, source):
    home = tmp_path / "isolated"
    fake = FakeRun(version="3.14")
    with pytest.raises(InstallError, match="Python 3.11"):
        install(home, source_root=source, run=fake, owner_slack_id="invented-owner")
    assert not home.exists()
    assert len(fake.calls) == 1


def test_missing_parent_does_not_create_outside_home(tmp_path, source):
    parent = tmp_path / "not-created"
    with pytest.raises(InstallError, match="parent of --home"):
        install(parent / "isolated", source_root=source, run=FakeRun())
    assert not parent.exists()


def test_unsafe_archive_rejected(tmp_path):
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as stream:
        info = tarfile.TarInfo("../escaped")
        info.size = 3
        stream.addfile(info, io.BytesIO(b"bad"))
    root = tmp_path / "extract"
    root.mkdir()
    with pytest.raises(InstallError, match="unsafe"):
        _extract(archive, root)
    assert not (tmp_path / "escaped").exists()


def test_computed_docker_run_mounts_against_pin(tmp_path, source):
    """Run only the pin's pure argv builders; no daemon/container is started."""
    clone = os.environ.get("HERMES_DOTS_HERMES_SOURCE")
    if not clone:
        pytest.skip("Set HERMES_DOTS_HERMES_SOURCE to the read-only pinned clone for argv contract check")
    raw = subprocess.run(["git", "-C", clone, "show", "v2026.9.24:tools/environments/docker.py"],
                         check=True, capture_output=True, text=True).stdout
    functions = []
    for item in ast.walk(ast.parse(raw)):
        if isinstance(item, ast.FunctionDef) and item.name in {"_mount_args", "_run_command"}:
            functions.append(item)
    assert len(functions) == 2
    namespace = {"os": os, "logger": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "pinned-docker", "exec"), namespace)
    home = tmp_path / "isolated"
    result = install(home, source_root=source, run=FakeRun(), timezone="UTC", owner_slack_id="invented-owner")
    config = json.loads((result.profile / "config.yaml").read_text())["terminal"]
    env = SimpleNamespace(_persistent=False)
    volume, writable = namespace["_mount_args"](
        env, config["docker_volumes"], str(source), config["docker_mount_cwd_to_workspace"], "orca")
    # The pin automatically appends these readonly profile-local mounts, independent of docker_volumes.
    for relative in ("skills", "cache/documents", "cache/images", "cache/audio"):
        volume += ["-v", f"{result.profile / relative}:/root/.hermes/{relative}:ro"]
    env._docker_exe = "docker"
    env._image_uses_s6_init = False
    env._snap_compat = False
    env._labels = {}
    env._image = "invented-image"
    env._all_run_args = writable + volume
    argv = namespace["_run_command"](env, "orca", "/workspace")
    mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "-v"]
    assert mounts[0] == f"{home / 'workspace'}:/workspace:rw"
    assert all(Path(mount.split(":", 1)[0]).is_relative_to(home) for mount in mounts)
    assert all(mount.endswith(":ro") for mount in mounts[1:])
    assert not any(mount.endswith(":/root") for mount in mounts)
