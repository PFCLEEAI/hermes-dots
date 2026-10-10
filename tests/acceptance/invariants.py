"""Spec section 11: shared state assertions and bounded negative probes.

The scenario assertions prove the effects; this helper checks their final safety
state. The public scan uses the caller's external terms when provided. It cannot
claim absence of private terms the caller has not configured.
"""

import ast
import json
import os
from pathlib import Path

import pytest

from hermes_dots.brain import Note
from hermes_dots.security import redact
from hermes_dots.security.approvals import content_hash
from hermes_dots.security.scanning import credential_hits, personal_hits

REPO = Path(__file__).resolve().parents[2]


def assert_invariants(s):
    # 1: action floor, and each recorded outside effect consumed its exact grant.
    for action in ("send", "post", "delete", "spend", "account"):
        assert s.settings.action_rules().level(action) != "auto"
    for task_id, approval, action, target, content, revision in s.approved_effects:
        saved = s.engine.approvals(task_id).repository.get(approval.id)
        assert saved.used
        assert (saved.action, saved.target, saved.content_hash, saved.card_revision) == (
            action, target, content_hash(content), revision)
        with pytest.raises(PermissionError):
            s.engine.approvals(task_id).consume(saved.id, action, target, content, revision)

    # 2: no mailbox mutation surface or calls in either adapter or fake transport.
    forbidden = {"send", "send_message", "modify", "batchModify", "trash", "delete", "drafts", "insert", "import_"}
    for path in (REPO / "hermes_dots/inbox/gmail.py", REPO / "hermes_dots/inbox/sources.py"):
        tree = ast.parse(path.read_text())
        assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and node.func.attr in forbidden for node in ast.walk(tree))
        assert not any(isinstance(node, ast.FunctionDef) and node.name in forbidden for node in ast.walk(tree))
    assert all(call[0] in {"history", "list_messages", "get_message", "sent_mail"} for call in s.gmail.calls)
    assert s.gmail.scopes == ("https://www.googleapis.com/auth/gmail.readonly",)

    # 3: helpers cannot write even through the public writer, nor post to Slack.
    notes_before = {str(p): p.read_bytes() for p in (s.root / "brain").rglob("*.md")}
    posts_before = list(s.slack.posts)
    with pytest.raises(PermissionError):
        s.brain.writer.write(Note(body="Helper suggestion"), actor="helper")
    with pytest.raises(PermissionError):
        s.brain.evidence.append({"kind": "helper"}, actor="helper")
    s.runtime.hooks.subagent_start("primary", "invariant-helper")
    assert s.runtime.hooks.pre_tool_call("send_message", {"target": "owner", "content": "hello"},
                                         session_id="invariant-helper")["action"] == "block"
    s.runtime.hooks.subagent_stop("primary", "invariant-helper")
    assert notes_before == {str(p): p.read_bytes() for p in (s.root / "brain").rglob("*.md")}
    assert s.slack.posts == posts_before

    # 4: all runtime notes have a serialized, labeled writer receipt; edits survive.
    events = [json.loads(line) for data in s.logs().values() for line in data.decode().splitlines()]
    written = {event["note_id"] for event in events if event["kind"] == "brain_write" and event["actor"] in {"owner", "orca"}}
    for path in (s.root / "brain").rglob("note-*.md"):
        note = Note.parse(path.read_text())
        assert note.id in written and note.owner in {"owner", "orca"}
    assert s.brain.writer.queue.empty()
    for path, owner_bytes in s.owner_edits.items():
        assert path.read_bytes().startswith(owner_bytes)

    # 5: append-only evidence, persistent suppression and no search/relearning.
    for name, before in s.initial_logs.items():
        assert s.logs()[name].startswith(before)
    for term in s.forgotten:
        assert s.brain.index.suppressed(term)
        assert not s.brain.search(term)
        with pytest.raises(ValueError, match="Suppressed"):
            s.brain.learn(term + " learned from retained evidence")

    # 6: unique message identity, exactly one category, and a surviving thread card.
    rows = s.ledger.connection.execute("SELECT account,message_id,thread_id,category,reason FROM mail_items").fetchall()
    identities = [(row["account"], row["message_id"]) for row in rows]
    assert len(identities) == len(set(identities))
    for row in rows:
        assert row["category"] in {"urgent", "regular", "noise", "uncertain"} and row["reason"]
        assert s.ledger.get_mail_card(row["account"], row["thread_id"]) is not None

    # 7: durable delivery IDs and no duplicate posts; unknown outcomes stay visible.
    ids = []
    for post in s.slack.posts:
        metadata = post["metadata"]
        assert metadata["event_type"] == "dots_delivery"
        delivery_id = metadata["event_payload"]["dots_delivery_id"]
        assert s.ledger.get_effect(delivery_id) is not None
        ids.append(delivery_id)
    assert len(ids) == len(set(ids))
    unknown = {effect.delivery_id for effect in s.ledger.list_effects() if effect.state == "unknown"}
    assert unknown == set(s.engine.status()["unknown_deliveries"])
    for task in s.ledger.list_tasks():
        if task.interrupted:
            assert task.state == "paused"
            assert any(event.kind == "task_interrupted" and event.task_id == task.id for event in s.ledger.list_events())

    # 8: persisted notes, logs, ledger payloads, and model-bound excerpts are redacted.
    captures = [p.read_text() for p in (s.root / "brain").rglob("*.md")]
    captures += [data.decode() for data in s.logs().values()]
    captures += [json.dumps(s.model.fake.requests), str(s.ledger.list_events()), str(s.slack.posts)]
    assert all(redact(text) == text for text in captures)

    # 9: denied-path checks are pure guard probes against an invented home.
    with pytest.raises(PermissionError):
        s.settings.path_guard().check(Path.home() / ".hermes/config.yaml")
    for path in s.settings.get("extra_denied_paths"):
        with pytest.raises(PermissionError):
            s.settings.path_guard().check(Path(path) / "file.txt")

    # 10: real scanner, including untracked public files; never ignored PM/history.
    assert list(credential_hits(REPO)) == []
    terms_path = os.environ.get("HERMES_DOTS_PRIVATE_TERMS")
    if terms_path:
        path = Path(terms_path).resolve()
        assert path.is_file() and not path.is_relative_to(REPO)
        terms = [line.strip() for line in path.read_text().splitlines() if line.strip()]
        assert list(personal_hits(REPO, terms)) == []
