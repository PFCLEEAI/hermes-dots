"""Install an isolated Hermes profile without activating services or accounts."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from zoneinfo import ZoneInfo

from hermes_dots.brain import bootstrap

HERMES_TAG = "v2026.9.24"
HERMES_SHA = "f97608f178d1ffeca59860195ab7da295f7c8e5f"
HERMES_REPO = "https://github.com/NousResearch/hermes-agent.git"


class InstallError(ValueError):
    """The requested install cannot preserve isolation."""


@dataclass(frozen=True)
class InstallResult:
    home: Path
    profile: Path
    python: Path
    launcher: Path
    plist: Path


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _destination(home: Path, relative: str) -> Path:
    path = home / relative
    if not _inside(path.resolve(), home):
        raise InstallError(f"Install destination escapes --home: {relative}")
    # Reject internal symlinks even when their target happens to be inside home.
    for item in (path, *path.parents):
        if item == home:
            break
        if item.is_symlink():
            raise InstallError(f"Install destination is a symlink: {relative}")
    return path


def _copy_tree(source: Path, target: Path) -> None:
    """Copy regular source files only; no source link may become a host mount."""
    if source.is_symlink() or any(path.is_symlink() for path in source.rglob("*")):
        raise InstallError(f"Source contains a symlink: {source.name}")
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _extract(archive: Path, target: Path) -> None:
    with tarfile.open(archive) as stream:
        members = stream.getmembers()
        for member in members:
            if (not _inside((target / member.name).resolve(), target)
                    or not (member.isfile() or member.isdir())):
                raise InstallError("Source archive contains an unsafe path or link")
        stream.extractall(target, members=members, filter="data")


def _archive_source(source: Path, ref: str, target: Path, archive: Path, run, env) -> None:
    target.mkdir()
    with archive.open("wb") as output:
        run(["git", "-C", str(source), "archive", ref], check=True, stdout=output, env=env)
    _extract(archive, target)
    archive.unlink()


def _write_json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")
    path.chmod(0o600)


def _environment_lines(env: dict[str, str]) -> str:
    return "".join(f"export {key}={shlex.quote(value)}\n" for key, value in env.items())


def install(
    home: str | Path,
    *,
    source_root: str | Path | None = None,
    hermes_source: str | Path | None = None,
    pack_ref: str | None = None,
    owner_slack_id: str = "",
    timezone: str | None = None,
    extra_denied_paths=(),
    run=subprocess.run,
    python: str = sys.executable,
) -> InstallResult:
    """Install from the local dots source, optionally archived at an exact commit.

    Without ``pack_ref`` this is explicitly a development working-tree install;
    its receipt does not claim an immutable public pack. Hermes is always pinned.
    ``run`` is injectable so tests never install packages or contact a service.
    """
    requested = Path(home).expanduser().absolute()
    forbidden = Path.home() / ".hermes"
    home = requested.resolve()
    if (_inside(requested, forbidden) or _inside(forbidden, requested)
            or _inside(home, forbidden.resolve()) or _inside(forbidden.resolve(), home)):
        raise InstallError("Refusing to access the existing Hermes home")
    source = Path(source_root or Path(__file__).resolve().parents[2]).resolve()
    if _inside(home, source):
        raise InstallError("The install home must be outside the public source tree")
    denied = [str(Path(value).expanduser().absolute()) for value in extra_denied_paths]
    for value in denied:
        if _inside(home, Path(value).resolve()):
            raise InstallError("The install home is inside an extra denied path")
    if pack_ref is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", pack_ref):
        raise InstallError("--pack-ref must be a 40-character commit SHA")
    if timezone is not None:
        ZoneInfo(timezone)
    # None follows the machine's local zone, including DST; never invent an IANA name
    # from abbreviations such as KST. Onboarding can replace it with an explicit zone.

    destinations = ["profiles/orca", "runtime/hermes", "runtime/hermes-dots", "venv",
                    "local-config.json", "install-receipt.json", "bin/hermes-dots",
                    "login-items/org.hermes-dots.orca.plist", "hermes-pack.yaml", "workspace", "kanban",
                    "tmp", "cache/pip", "brain", "state", "evidence"]
    for relative in destinations:
        target = _destination(home, relative)
        if target.exists() and relative in destinations[:9]:
            raise InstallError(f"Refusing to overwrite existing install path: {relative}")
        for value in denied:
            if _inside(target.resolve(), Path(value).resolve()):
                raise InstallError(f"Install destination is denied: {relative}")
        if target.is_dir() and any(item.is_symlink() for item in target.rglob("*")):
            raise InstallError(f"Existing install directory contains a symlink: {relative}")
    if not home.parent.is_dir():
        raise InstallError("The parent of --home must already exist; installer writes only inside --home")
    if not isinstance(owner_slack_id, str) or not owner_slack_id.strip():
        raise InstallError("--owner-slack-id is required to activate the owner-only Slack bridge")
    profile = _destination(home, "profiles/orca")
    hermes = _destination(home, "runtime/hermes")
    dots = _destination(home, "runtime/hermes-dots")
    venv = _destination(home, "venv")
    executable = venv / "bin/python"
    profile_env = {
        "HERMES_HOME": str(profile),
        "HERMES_DOTS_HOME": str(home),
        "HERMES_KANBAN_HOME": str(home / "kanban"),
        "TMPDIR": str(home / "tmp"),
        "PIP_CACHE_DIR": str(home / "cache/pip"),
        "PYTHONPYCACHEPREFIX": str(home / "cache/python"),
        "PYTHONNOUSERSITE": "1",
    }
    env = {**os.environ, **profile_env}
    version = run([python, "-I", "-S", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                  check=True, capture_output=True, text=True, env=env)
    try:
        python_version = tuple(int(part) for part in version.stdout.strip().split("."))
    except (AttributeError, TypeError, ValueError) as error:
        raise InstallError("Could not verify the selected Python interpreter") from error
    if not (3, 11) <= python_version < (3, 14):
        raise InstallError("Pinned Hermes requires Python 3.11–3.13; choose --python accordingly")
    home.mkdir(exist_ok=True)
    home.chmod(0o700)
    for relative in ("runtime", "bin", "login-items", "workspace", "kanban", "tmp", "cache/pip"):
        _destination(home, relative).mkdir(parents=True, exist_ok=True)

    if hermes_source is not None:
        _archive_source(Path(hermes_source).resolve(), HERMES_SHA, hermes,
                        home / "tmp/hermes.tar", run, env)
    else:
        run(["git", "init", str(hermes)], check=True, env=env)
        run(["git", "-C", str(hermes), "fetch", "--depth=1", HERMES_REPO, HERMES_SHA],
            check=True, env=env)
        run(["git", "-C", str(hermes), "checkout", "--detach", HERMES_SHA], check=True, env=env)
    if pack_ref:
        _archive_source(source, pack_ref, dots, home / "tmp/dots.tar", run, env)
    else:
        dots.mkdir()
        for name in ("hermes_dots", "profiles", "plugins"):
            _copy_tree(source / name, dots / name)
        for name in ("pyproject.toml", "README.md", "LICENSE", "plugin.yaml", "hermes-pack.yaml"):
            item = source / name
            if item.is_symlink():
                raise InstallError(f"Source file is a symlink: {name}")
            shutil.copyfile(item, dots / name)

    run([python, "-m", "venv", str(venv)], check=True, env=env)
    # Editable installs stay entirely in the copied, isolated runtime roots.
    run([str(executable), "-m", "pip", "install", "-e", str(hermes)],
        check=True, cwd=str(home), env=env)
    run([str(executable), "-m", "pip", "install", "-e", str(dots)],
        check=True, cwd=str(home), env=env)

    profile.mkdir(parents=True)
    for name in ("scripts", "skills", "cache", "plugins"):
        (profile / name).mkdir()
    shutil.copyfile(dots / "profiles/orca/SOUL.md", profile / "SOUL.md")
    config = json.loads((dots / "profiles/orca/config.yaml").read_text(encoding="utf-8"))
    config["terminal"]["docker_volumes"] = [f"{home / 'workspace'}:/workspace:rw"]
    config["timezone"] = timezone
    _write_json(profile / "config.yaml", config)  # JSON is valid YAML, no new dependency.
    plugin = profile / "plugins/hermes-dots"
    plugin.mkdir()
    shutil.copyfile(dots / "plugin.yaml", plugin / "plugin.yaml")
    (plugin / "__init__.py").write_text(
        "from hermes_dots.hermes_adapter.register import register\n", encoding="utf-8")
    # Bundled providers are under plugins/memory; USER providers are direct plugin children at the pin.
    _copy_tree(dots / "plugins/memory/hermes_dots_brain", profile / "plugins/hermes_dots_brain")
    _write_json(home / "local-config.json", {
        "owner_slack_id": owner_slack_id.strip(), "timezone": timezone,
        "extra_denied_paths": denied,
    })
    bootstrap(home)

    tick = profile / "scripts/dots-tick.sh"
    tick.write_text("#!/bin/sh\nset -eu\n" + _environment_lines(profile_env)
                    + f"exec {shlex.quote(str(executable))} -m hermes_dots.hermes_adapter.tick\n",
                    encoding="utf-8")
    tick.chmod(0o700)
    tick_kwargs = {"prompt": "", "schedule": "every 1m", "name": "hermes-dots-tick",
                   "script": str(tick), "no_agent": True,
                   "workdir": str(home / "workspace"), "deliver": "none"}
    run([str(executable), "-c", "from cron.jobs import create_job; import json,sys; "
         "create_job(**json.loads(sys.argv[1]))", json.dumps(tick_kwargs)],
        check=True, cwd=str(hermes), env=env)

    launcher = home / "bin/hermes-dots"
    launcher.write_text("#!/bin/sh\nset -eu\n" + _environment_lines(profile_env)
                        + f"exec {shlex.quote(str(venv / 'bin/hermes'))} \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o700)
    plist = home / "login-items/org.hermes-dots.orca.plist"
    with plist.open("xb") as output:
        plistlib.dump({
            "Label": "org.hermes-dots.orca",
            "ProgramArguments": [str(launcher), "gateway", "run"],
            "EnvironmentVariables": profile_env,
            "WorkingDirectory": str(home / "workspace"),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(home / "evidence/gateway.stdout.log"),
            "StandardErrorPath": str(home / "evidence/gateway.stderr.log"),
        }, output)
    plist.chmod(0o600)
    if pack_ref:
        _write_json(home / "hermes-pack.yaml", {
            "name": "hermes-dots", "version": "0.1.0",
            "plugins": [
                {"repo": str(dots), "ref": pack_ref.lower()},
                {"repo": str(dots), "ref": pack_ref.lower(), "subdir": "plugins/memory/hermes_dots_brain"},
            ],
        })
    _write_json(home / "install-receipt.json", {
        "hermes_tag": HERMES_TAG, "hermes_ref": HERMES_SHA,
        "pack_ref": pack_ref.lower() if pack_ref else "working-tree",
        "pack_mode": "immutable" if pack_ref else "local-development",
        "profile": "orca", "service_loaded": False,
        "tick": tick_kwargs,
    })
    return InstallResult(home, profile, executable, launcher, plist)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default="~/.hermes-dots")
    parser.add_argument("--hermes-source", help="local Hermes git clone; archive the exact pinned SHA")
    parser.add_argument("--pack-ref", help="exact local dots commit SHA; omitted = development working tree")
    parser.add_argument("--owner-slack-id", required=True)
    parser.add_argument("--timezone")
    parser.add_argument("--python", default=sys.executable, help="Python 3.11–3.13 interpreter for Hermes")
    parser.add_argument("--deny-path", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        result = install(args.home, hermes_source=args.hermes_source, pack_ref=args.pack_ref,
                         owner_slack_id=args.owner_slack_id, timezone=args.timezone,
                         extra_denied_paths=args.deny_path, python=args.python)
    except (InstallError, OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"Install stopped: {error}", file=sys.stderr)
        return 1
    print(f"Installed Orca in {result.profile}\nLauncher: {result.launcher}")
    print(f"Login item generated, not loaded: {result.plist}")
    print("Enable the login item yourself when ready. Set Docker Desktop to start at login.")
    return 0
