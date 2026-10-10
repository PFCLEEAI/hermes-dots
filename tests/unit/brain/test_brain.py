from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

import pytest

from hermes_dots.brain import Brain, BrainIndex, BrainWriter, EvidenceLog, Note, bootstrap, content_hash
from hermes_dots import security


@pytest.fixture(autouse=True)
def security_interface(monkeypatch):
    # T1 owns redaction; use the real implementation as soon as it is present.
    if not hasattr(security, "redact"):
        def redact(text):
            return re.sub(r"Bearer\s+\S+", "[REDACTED]", text, flags=re.I)
        monkeypatch.setattr(security, "redact", redact, raising=False)


@pytest.fixture
def brain(tmp_path):
    return Brain(tmp_path)


def snapshot_logs(root):
    return {path.name: path.read_bytes() for path in (root / "evidence").glob("*.jsonl")}


def events(root):
    return [json.loads(line) for path in (root / "evidence").glob("*.jsonl")
            for line in path.read_text().splitlines()]


def ids(results):
    return {item["id"] for item in results}


def test_bootstrap_layout_is_empty_and_non_overwriting(tmp_path):
    bootstrap(tmp_path)
    for category in ("People", "Projects", "Work & Clients", "Topics & Learning", "About Me",
                     "Decisions", "Lessons & Incidents", "Sources", "Daily Log"):
        assert (tmp_path / "brain/shared" / category).is_dir()
    for folder in ("brain/notebook/lessons", "brain/notebook/skills", "brain/proposals", "evidence", "state"):
        assert (tmp_path / folder).is_dir()
    readme = tmp_path / "brain/README.md"
    readme.write_text("Owner rules")
    bootstrap(tmp_path)
    assert readme.read_text() == "Owner rules"
    assert list((tmp_path / "brain/shared").rglob("*.md")) == []


def test_exact_front_matter_round_trip():
    note = Note(summary='Acme: "decision"', body="한국어 lesson\n---\nBody")
    assert Note.parse(note.markdown()) == note
    assert list(note.metadata()) == ["id", "owner", "category", "type", "summary", "source_task",
                                    "evidence_refs", "confidence", "visibility", "status", "supersedes",
                                    "created_at", "last_verified_at"]
    with pytest.raises(ValueError):
        Note.parse(note.markdown().replace("owner:", "extra: null\nowner:"))
    with pytest.raises(ValueError):
        Note.parse(note.markdown().replace("owner:", "owner: owner\nowner:"))


@pytest.mark.parametrize("field,value", [
    ("owner", "helper"), ("type", "instruction"), ("confidence", "certain"),
    ("visibility", "public"), ("status", "active"), ("category", "../../escape"),
    ("id", "../escape"), ("source_task", "task-1"), ("evidence_refs", ["https://example.com"]),
    ("created_at", "yesterday"), ("supersedes", "../escape"),
])
def test_invalid_note_metadata(field, value):
    with pytest.raises(ValueError):
        replace(Note(), **{field: value}).markdown()


def test_evidence_append_only_and_redacted(brain):
    ref = brain.evidence.append({"kind": "message", "text": "Bearer " + "invented-token"})
    before = snapshot_logs(brain.root)
    brain.evidence.append({"kind": "tool", "nested": ["Bearer " + "another-token"]})
    after = snapshot_logs(brain.root)
    for name in before:
        assert after[name].startswith(before[name])
    assert ref.startswith("evidence://")
    assert "invented-token" not in str(after)
    assert "another-token" not in str(after)
    assert all(event["actor"] == "orca" for event in events(brain.root))


def test_concurrent_writers_serialize_additions(brain):
    note = brain.learn("Original body", summary="Shared destination")
    path = brain.writer.path_for(note)
    before = path.read_text()
    second = BrainWriter(brain.root)

    def write(number):
        writer = brain.writer if number % 2 else second
        return writer.write(replace(note, body=f"Addition {number}"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(list(pool.map(write, range(32)))) == 32
    text = path.read_text()
    assert text.startswith(before)
    assert text.count("## Addition by orca") == 32
    for number in range(32):
        assert f"Addition {number}\n" in text
    assert brain.writer.queue.empty() and second.queue.empty()


def test_owner_edit_wins_and_owner_is_told(brain):
    note = brain.learn("Original body")
    path = brain.writer.path_for(note)
    _, digest = brain.writer.read(path)
    owner_text = path.read_text() + "\nOwner: keep this exact sentence.\n"
    path.write_text(owner_text)
    brain.writer.write(replace(note, body="New result"), expected_hash=digest)
    assert path.read_text().startswith(owner_text)
    assert "Addition by orca" in path.read_text()
    assert "New result" in path.read_text()
    notification = next(event for event in events(brain.root) if event["kind"] == "owner_edit_preserved")
    assert notification["told_owner"] and notification["notification"]


def test_matching_hash_allows_update(brain):
    note = brain.learn("Earlier result")
    path = brain.writer.path_for(note)
    _, digest = brain.writer.read(path)
    brain.writer.write(replace(note, body="Verified result"), expected_hash=digest, actor="owner")
    updated = Note.parse(path.read_text())
    assert updated.owner == "owner" and updated.body == "Verified result"
    assert "Addition by" not in path.read_text()


def test_promotion_preserves_edit_after_read(brain, monkeypatch):
    note = brain.learn("Acme starts Monday", type="fact", confidence="direct",
                       evidence_refs=["evidence://owner-correction"])
    find = brain._find
    owner_text = []

    def edit_after_read(note_id):
        path, saved, digest = find(note_id)
        text = path.read_text() + "\nOwner: the start date needs confirmation.\n"
        path.write_text(text)
        owner_text.append(text)
        return path, saved, digest

    monkeypatch.setattr(brain, "_find", edit_after_read)
    assert brain.promote(note.id, owner_stated=True)["status"] == "owner_edit_preserved"
    assert brain.writer.path_for(note).read_text().startswith(owner_text[0])
    assert not list((brain.root / "brain/shared").rglob("*.md"))


def test_helpers_cannot_write(brain):
    with pytest.raises(PermissionError):
        brain.writer.write(Note(body="Helper content"), actor="helper")
    with pytest.raises(PermissionError):
        brain.evidence.append({"kind": "message"}, actor="helper")
    with pytest.raises(PermissionError):
        brain.forget("Acme", actor="helper")
    assert list((brain.root / "brain/notebook").rglob("*.md")) == []


def test_immediate_search_and_citations(brain):
    note = brain.learn("Acme launch findings", summary="Launch")
    result = brain.search("launch")
    assert note.id in ids(result)
    assert result[0]["citation"].startswith("brain://notebook/")
    assert "[launch]" in result[0]["snippet"].casefold()
    assert brain.search('" OR *) --') == []


def test_incremental_equals_rebuild_and_removes_deleted_sources(brain):
    note = brain.learn("Acme launch findings")
    brain.evidence.append({"kind": "message", "text": "Acme conversation"})
    brain.index.incremental_pass()
    incremental = brain.search("Acme", 100)
    brain.index.rebuild()
    assert brain.search("Acme", 100) == incremental
    brain.writer.path_for(note).unlink()
    brain.index.incremental_pass()
    assert note.id not in ids(brain.search("Acme", 100))
    assert any(item["citation"].startswith("evidence://") for item in brain.search("Acme"))
    assert brain.index.incremental_pass() == 0


def test_status_and_confidence_boost(brain):
    first = Note(id="note-a", body="Acme", summary="Acme", confidence="inferred")
    second = replace(first, id="note-b", status="approved", confidence="direct")
    brain.writer.write(first)
    brain.writer.write(second)
    assert [item["id"] for item in brain.search("Acme")] == ["note-b", "note-a"]


def test_inferred_preference_waits_for_owner_and_is_undoable(brain):
    note = brain.learn("Alex prefers short replies", type="preference", confidence="inferred")
    result = brain.promote(note.id, owner_stated=True, verified=True)
    assert result["status"] == "proposed"
    assert brain.writer.path_for(note).exists()
    assert not list((brain.root / "brain/shared").rglob("*.md"))
    digest = brain.digest_items()
    assert digest["learned"][0]["undo"] == note.id
    assert digest["promotions"][0]["id"] == note.id
    assert brain.promote(note.id, owner_approved=True)["status"] == "approved"
    another = brain.learn("Another lesson")
    brain.undo(another.id)
    assert another.id not in ids(brain.search("Another"))
    with pytest.raises(ValueError):
        brain.undo(note.id)


@pytest.mark.parametrize("authority", ["owner_stated", "verified"])
def test_evidenced_direct_fact_auto_promotes(brain, authority):
    ref = brain.record_feedback("correction", "Acme launches Friday")
    note = brain.learn("Acme launches Friday", type="fact", confidence="direct", evidence_refs=[ref])
    result = brain.promote(note.id, category="Projects", **{authority: True})
    assert result["status"] == "approved"
    assert result["note"].visibility == "shared"
    assert not brain.writer.path_for(note).exists()
    assert brain.writer.path_for(result["note"]).exists()
    assert note.id in ids(brain.search("Friday"))
    if authority == "verified":
        assert result["note"].last_verified_at


def test_missing_evidence_and_behavior_change_wait_for_tap(brain):
    note = brain.learn("Acme starts Monday", type="fact", confidence="direct")
    assert brain.promote(note.id, verified=True)["status"] == "proposed"
    ref = brain.record_feedback("correction", "Reply with a greeting")
    other = brain.learn("Reply with a greeting", type="fact", confidence="direct", evidence_refs=[ref])
    assert brain.promote(other.id, verified=True, changes_behavior=True)["status"] == "proposed"


def test_duplicate_and_conflict_surface_both_sources(brain):
    first_ref = brain.record_feedback("correction", "Acme is ready")
    first = brain.learn("Acme is ready", category="Projects", type="fact", confidence="direct",
                        evidence_refs=[first_ref])
    brain.promote(first.id, owner_stated=True)
    assert brain.learn("Acme is ready").id == first.id
    second_ref = brain.record_feedback("correction", "Acme is not ready")
    second = brain.learn("Acme is not ready", category="Projects", evidence_refs=[second_ref])
    assert second.confidence == "conflicting"
    assert brain.promote(second.id, owner_approved=True)["status"] == "conflicting"
    conflict = brain.digest_items()["conflicts"][0]
    assert {n["id"] for n in conflict["versions"]} == {first.id, second.id}
    assert {ref for n in conflict["versions"] for ref in n["evidence_refs"]} == {first_ref, second_ref}
    assert "Acme is ready" in brain.writer.path_for(replace(first, visibility="shared")).read_text()


def test_explicit_conflict_is_preserved(brain):
    first = brain.learn("Acme launches Monday")
    second = brain.learn("Acme launches Friday", conflicting_with=[first.id])
    assert second.confidence == "conflicting"
    assert len(brain.digest_items()["conflicts"]) == 1


def test_forget_everywhere_preserves_logs_and_blocks_relearning(brain):
    ref = brain.record_feedback("correction", "Acme launches Friday")
    shared = brain.learn("Acme launches Friday", type="fact", confidence="direct", evidence_refs=[ref])
    brain.promote(shared.id, owner_stated=True)
    notebook = brain.learn("Acme lesson")
    proposal = brain.nightly_reflection([{"text": "Acme feedback"}], lambda text: "Improve Acme replies")
    assert proposal.exists()
    brain.index.incremental_pass()
    before = snapshot_logs(brain.root)
    removed = brain.forget("Acme")
    assert {shared.id, notebook.id}.issubset(removed)
    assert not proposal.exists()
    assert snapshot_logs(brain.root) == before
    assert brain.search("Acme") == []
    with pytest.raises(ValueError, match="Suppressed"):
        brain.learn("Acme lesson from retained logs")
    brain.index.rebuild()
    assert brain.search("Acme") == []
    restarted = Brain(brain.root)
    assert restarted.search("Acme") == []
    restarted.remember_again("Acme")
    assert restarted.search("Acme")  # Retained raw feedback can now be recalled.
    assert restarted.learn("Acme new lesson")


def test_suppression_rechecked_for_unchanged_index_and_promotion(brain):
    note = brain.learn("Acme learned fact", type="fact", confidence="direct", evidence_refs=["evidence://test"])
    brain.index.suppression_path.write_text('["acme"]')
    brain.index.incremental_pass()
    assert brain.search("fact") == []
    with pytest.raises(ValueError, match="Suppressed"):
        brain.promote(note.id, verified=True)
    with sqlite3.connect(brain.index.database) as db:
        assert db.execute("SELECT count(*) FROM documents WHERE id = ?", (note.id,)).fetchone()[0] == 0
    brain.index.rebuild()
    assert brain.search("fact") == []


def test_retention_is_off_by_default_and_records_deletion(brain):
    brain.evidence.append({"kind": "old"}, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    brain.evidence.append({"kind": "recent"}, now=datetime(2026, 1, 10, tzinfo=timezone.utc))
    before = snapshot_logs(brain.root)
    assert brain.evidence.retain() == []
    assert snapshot_logs(brain.root) == before
    removed = brain.evidence.retain(7, now=datetime(2026, 1, 10, tzinfo=timezone.utc))
    assert removed == ["2026-01-01.jsonl"]
    assert any(event["kind"] == "log_retention" and event["removed"] == removed for event in events(brain.root))


def test_reflection_is_bounded_redacted_once_and_never_applied(brain):
    seen = []
    feedback = [{"text": "Bearer " + "invented-token", "timestamp": "2026-01-10T00:00:00Z"},
                {"text": "Yesterday", "timestamp": "2026-01-09T00:00:00Z"}]
    feedback += [{"text": "x" * 2000} for _ in range(150)]

    def propose(text):
        seen.append(json.loads(text))
        return "Propose shorter replies"

    path = brain.nightly_reflection(feedback, propose, day="2026-01-10")
    assert path.is_relative_to(brain.root / "brain/proposals")
    assert len(seen) == 1 and len(seen[0]) == 100
    assert max(len(item["text"]) for item in seen[0]) <= 1000
    assert "invented-token" not in str(seen) and "Yesterday" not in str(seen)
    assert Note.parse(path.read_text()).status == "proposed"
    assert list((brain.root / "brain/shared").rglob("*.md")) == []
    assert list((brain.root / "brain/notebook").rglob("*.md")) == []
    assert Brain(brain.root).nightly_reflection(feedback, propose, day="2026-01-10") is None
    assert len(seen) == 1


def test_symlink_escape_refused(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "brain").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        bootstrap(root)
    assert not list(outside.iterdir())
