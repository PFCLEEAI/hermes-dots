from concurrent.futures import ThreadPoolExecutor
import logging
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_dots.security import (
    ActionRules, ApprovalRejected, Approvals, InMemoryApprovalRepository,
    PathDeniedError, PathGuard, RuleLevel, RuleViolation, Secrets, SecretsError, redact,
)
from hermes_dots.security.scanning import credential_hits
from hermes_dots.security import secrets as secret_module


@pytest.fixture(autouse=True)
def isolated_secrets(monkeypatch):
    monkeypatch.setattr(secret_module, "_loaded_values", set())


def test_path_guard_home_floor_and_logging(tmp_path, caplog):
    home = tmp_path / "home"
    home.mkdir()
    guard = PathGuard(home=home)
    with caplog.at_level(logging.WARNING):
        for path in ("~/.hermes", "~/.hermes/config.yaml", home / ".hermes/new/file"):
            with pytest.raises(PathDeniedError):
                guard.check(path)
        with pytest.raises(PathDeniedError):
            guard.remove("~/.hermes")
    assert "path_denied" in caplog.text
    assert guard.check("~/.hermes-dots/work") == home / ".hermes-dots/work"
    assert isinstance(guard.denied_paths, tuple)


def test_path_guard_extra_denials_are_add_only(tmp_path):
    guard = PathGuard([tmp_path / "private"], home=tmp_path / "home")
    guard.add(tmp_path / "other")
    for folder in ("private", "other"):
        with pytest.raises(PathDeniedError):
            guard.check(tmp_path / folder / "new.txt")
    assert guard.check(tmp_path / "private-copy") == tmp_path / "private-copy"
    with pytest.raises(AttributeError):
        guard.denied_paths = ()


def test_symlink_escape_and_parent_traversal(tmp_path):
    home = tmp_path / "home"
    denied = home / ".hermes"
    (denied / "sub").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(denied / "sub", target_is_directory=True)
    guard = PathGuard(home=home)
    for path in (alias / "new.txt", alias / ".." / "config.yaml"):
        with pytest.raises(PathDeniedError):
            guard.check(path)


def test_denied_symlink_retarget_does_not_unlock_old_or_new_path(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(private, target_is_directory=True)
    guard = PathGuard([alias], home=tmp_path / "home")
    alias.unlink()
    alias.mkdir()
    for path in (alias / "new", private / "new"):
        with pytest.raises(PathDeniedError):
            guard.check(path)


def test_loop_is_typed_logged_error(tmp_path, caplog):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    guard = PathGuard(home=tmp_path / "home")
    with pytest.raises(PathDeniedError):
        guard.check(loop)
    assert "path_denied" in caplog.text


@pytest.mark.parametrize("action", ["send", "post", "delete", "spend", "account"])
def test_action_floor_on_initialization_and_changes(action):
    with pytest.raises(RuleViolation):
        ActionRules({action: "auto"})
    rules = ActionRules()
    with pytest.raises(RuleViolation):
        rules.change(action, "auto", confirmed=True)
    assert rules.level(action) == RuleLevel.ASK_FIRST


def test_defaults_four_levels_and_confirmation():
    rules = ActionRules()
    for action in ("read", "research", "write", "code", "helpers"):
        assert rules.level(action) == "auto"
    assert rules.requires_approval("unknown-action")
    assert "may I go ahead?" in rules.confirmation_text("research", "when_told")
    with pytest.raises(RuleViolation):
        rules.change("research", "when_told")
    for level in RuleLevel:
        assert rules.change("research", level, confirmed=True) == level
    rules.change("send", "hand_to_owner", confirmed=True)
    assert rules.requires_approval("send")
    copied = rules.as_dict()
    copied["send"] = "auto"
    assert rules.level("send") == "hand_to_owner"


def test_approval_single_use_with_repository():
    repository = InMemoryApprovalRepository()
    approvals = Approvals(repository)
    approval = approvals.grant("post", "Acme", "Hello Alex", 1)
    assert "Hello Alex" not in repr(approval)
    assert approvals.consume(approval.id, "post", "Acme", "Hello Alex", 1).used
    assert repository.get(approval.id).used
    with pytest.raises(ApprovalRejected):
        approvals.consume(approval.id, "post", "Acme", "Hello Alex", 1)
    with pytest.raises(ApprovalRejected):
        repository.save(approval)


@pytest.mark.parametrize("field,value", [
    ("action", "spend"), ("target", "Other"), ("content", "Changed"), ("card_revision", 2),
])
def test_changed_approval_payload_rejected_without_consuming(field, value):
    approvals = Approvals()
    payload = {"action": "post", "target": "Acme", "content": "Hello Alex", "card_revision": 1}
    approval = approvals.grant(**payload)
    with pytest.raises(ApprovalRejected):
        approvals.consume(approval.id, **{**payload, field: value})
    assert approvals.consume(approval.id, **payload).used


def test_concurrent_approval_consume_is_atomic():
    approvals = Approvals()
    approval = approvals.grant("post", "Acme", "Hello", 1)

    def consume(_):
        try:
            approvals.consume(approval.id, "post", "Acme", "Hello", 1)
            return True
        except ApprovalRejected:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(consume, range(12))) == 1


@pytest.mark.parametrize("revision", [0, -1, True, "1"])
def test_invalid_revisions(revision):
    approvals = Approvals()
    with pytest.raises(ValueError):
        approvals.grant("post", "Acme", "Hello", revision)
    with pytest.raises(ValueError):
        approvals.consume("missing", "post", "Acme", "Hello", revision)


@pytest.mark.parametrize("prefix", ["xox" + letter + "-" for letter in "baprs"] + ["ya29" + ".", "sk" + "-", "Bearer ", "bearer "])
def test_token_shapes_are_redacted(prefix):
    token = prefix + "invented_123-ABC"
    assert redact("before " + token + " after") == "before [REDACTED] after"


def test_0600_secrets_loaded_and_repr_safe(tmp_path):
    path = tmp_path / "secrets.env"
    value = "invented-value-for-Acme"
    path.write_text("# comment\nexport ACME_KEY='" + value + "'\nOTHER=\"invented-other\"\n")
    path.chmod(0o600)
    secrets = Secrets().load_env(path)
    assert secrets["ACME_KEY"] == value
    assert value not in repr(secrets)
    assert redact("key=" + value) == "key=[REDACTED]"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o400, 0o700])
def test_secret_file_permissions_refused(tmp_path, mode):
    path = tmp_path / "secrets.env"
    path.write_text("KEY=invented\n")
    path.chmod(mode)
    with pytest.raises(SecretsError, match="0600"):
        Secrets().load_env(path)
    assert redact("invented") == "invented"


def test_secret_symlink_and_malformed_file_refused(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("INVALID LINE invented-value\n")
    path.chmod(0o600)
    with pytest.raises(SecretsError) as error:
        Secrets().load_env(path)
    assert "invented-value" not in str(error.value)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(SecretsError):
        Secrets().load_env(link)


def test_keychain_loading_mocked_and_failure_safe():
    calls = []

    def reader(service, account):
        calls.append((service, account))
        return "invented-keychain-value"

    secrets = Secrets().load_keychain("KEY", service="Acme", account="Alex", reader=reader)
    assert calls == [("Acme", "Alex")]
    assert redact(secrets["KEY"]) == "[REDACTED]"

    def fail(*args):
        raise RuntimeError("invented-sensitive-value")

    with pytest.raises(SecretsError) as error:
        secrets.load_keychain("OTHER", service="Acme", account="Alex", reader=fail)
    assert "invented-sensitive-value" not in str(error.value)


@pytest.fixture
def scan_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("ignored/\n")
    (root / "tracked.txt").write_text("Clean\n")
    subprocess.run(["git", "-C", str(root), "add", ".gitignore", "tracked.txt"], check=True)
    return root


def run_scan(name, root, *, env=None, extra=()):
    script = Path(__file__).resolve().parents[3] / "scripts" / name
    environment = dict(os.environ)
    environment.pop("HERMES_DOTS_PRIVATE_TERMS", None)
    environment.update(env or {})
    return subprocess.run([sys.executable, str(script), str(root), *extra],
                          capture_output=True, text=True, env=environment)


def test_credential_scanner_tracked_untracked_ignored(scan_repo):
    token = "xox" + "b-" + "invented-secret"
    (scan_repo / "tracked.txt").write_text("Clean\n" + token + "\n")
    (scan_repo / "untracked.txt").write_text(token)
    ignored = scan_repo / "ignored"
    ignored.mkdir()
    (ignored / "secret.txt").write_text(token)
    result = run_scan("scan_credentials.py", scan_repo)
    assert result.returncode == 1
    assert "tracked.txt:2:" in result.stdout
    assert "untracked.txt:1:" in result.stdout
    assert "ignored" not in result.stdout
    assert token not in result.stdout + result.stderr
    (scan_repo / "tracked.txt").write_text("Clean")
    (scan_repo / "untracked.txt").unlink()
    assert run_scan("scan_credentials.py", scan_repo).returncode == 0


def test_credential_scanner_loaded_value(scan_repo, tmp_path):
    value = "invented-plain-value"
    secret_file = tmp_path / "secrets.env"
    secret_file.write_text("KEY=" + value)
    secret_file.chmod(0o600)
    Secrets().load_env(secret_file)
    (scan_repo / "tracked.txt").write_text(value)
    assert list(credential_hits(scan_repo))
    result = run_scan("scan_credentials.py", scan_repo, extra=("--secrets-file", str(secret_file)))
    assert result.returncode == 1
    assert value not in result.stdout + result.stderr


def test_personal_scanner_case_insensitive_masks_and_git_scope(scan_repo, tmp_path):
    terms = tmp_path / "terms.txt"
    terms.write_text("Alex Example\nAcme\nZ\nAl\n")
    (scan_repo / "tracked.txt").write_text("Clean\naLEX eXAMPLE\n")
    (scan_repo / "untracked.txt").write_text("ACME")
    (scan_repo / "ignored").mkdir()
    (scan_repo / "ignored" / "private.txt").write_text("Alex Example")
    result = run_scan("scan_personal.py", scan_repo, env={"HERMES_DOTS_PRIVATE_TERMS": str(terms)})
    assert result.returncode == 1
    assert "tracked.txt:2:" in result.stdout and "untracked.txt:1:" in result.stdout
    assert "Al**********" in result.stdout and "Ac**" in result.stdout
    assert "Alex Example" not in result.stdout
    assert "aLEX eXAMPLE" not in result.stdout
    assert "ignored" not in result.stdout


def test_personal_missing_terms_notice_and_inside_root_refused(scan_repo):
    result = run_scan("scan_personal.py", scan_repo)
    assert result.returncode == 0 and "nothing to check" in result.stdout
    terms = scan_repo / "terms.txt"
    terms.write_text("Alex")
    result = run_scan("scan_personal.py", scan_repo, env={"HERMES_DOTS_PRIVATE_TERMS": str(terms)})
    assert result.returncode == 2


@pytest.mark.parametrize("scanner", ["scan_credentials.py", "scan_personal.py"])
def test_scanners_refuse_symlink_without_reading_outside(scan_repo, tmp_path, scanner):
    outside = tmp_path / "outside.txt"
    outside.write_text("Alex")
    (scan_repo / "alias.txt").symlink_to(outside)
    terms = tmp_path / "terms.txt"
    terms.write_text("Alex")
    result = run_scan(scanner, scan_repo, env={"HERMES_DOTS_PRIVATE_TERMS": str(terms)})
    assert result.returncode == 2
    assert "Alex" not in result.stdout + result.stderr
