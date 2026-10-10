"""Notebook learning, evidence gates, forgetting, and proposals (never applied)."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import re

from .index import BrainIndex
from .notes import Note, clean, content_hash, utc_now
from .storage import BrainWriter, EvidenceLog, inside


def normalized(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


def contradicts(first, second):
    """A conservative literal negation check; callers can also supply conflict IDs."""
    negations = {"not", "never", "no"}
    left, right = normalized(first).split(), normalized(second).split()
    return (bool(set(left) & negations) != bool(set(right) & negations)
            and [word for word in left if word not in negations]
            == [word for word in right if word not in negations])


class Brain:
    def __init__(self, root):
        self.index = BrainIndex(root)
        self.root = self.index.root
        self.evidence = EvidenceLog(self.root)
        self.writer = BrainWriter(self.root, self.index, self.evidence)

    def _notes(self):
        for folder in ("shared", "notebook", "proposals"):
            for path in sorted((self.root / "brain" / folder).rglob("*.md")):
                inside(self.root, path)
                yield path, Note.parse(path.read_text(encoding="utf-8"))

    def _find(self, note_id):
        for path, note in self._notes():
            if note.id == note_id:
                text = path.read_text(encoding="utf-8")
                return path, Note.parse(text), content_hash(text)
        raise KeyError(note_id)

    def search(self, query, limit=10):
        return self.index.search(query, limit)

    def learn(self, text, *, summary=None, category="lessons", type="lesson", confidence="unverified",
              source_task=None, evidence_refs=None, actor="orca", conflicting_with=()):
        if actor not in {"owner", "orca"}:
            raise PermissionError("Helpers cannot learn into the brain")
        note = Note(owner=actor, category=category, type=type, summary=summary or text[:160],
                    confidence=confidence, source_task=source_task, evidence_refs=evidence_refs or [], body=text)
        note = Note(**clean({**note.metadata(), "body": note.body})).validate()

        def operation():
            if self.index.suppressed(note.markdown()):
                raise ValueError("Suppressed content cannot be learned")
            conflicts = []
            for _, existing in self._notes():
                if normalized(existing.body) == normalized(note.body):
                    return existing  # No second copy of the same evidence.
                if existing.id in conflicting_with or (
                    existing.category == note.category and contradicts(existing.body, note.body)
                ):
                    conflicts.append(existing)
            if conflicts:
                note.confidence = "conflicting"
            self.writer._write(note, actor)
            self.evidence.append({"kind": "learned", "note_id": note.id, "undoable": True}, actor=actor)
            for existing in conflicts:
                self.evidence.append({"kind": "brain_conflict", "versions": [
                    {**existing.metadata(), "body": existing.body}, {**note.metadata(), "body": note.body}
                ]}, actor=actor)
            return note

        return self.writer.submit(operation, actor=actor)

    def promote(self, note_id, *, owner_stated=False, verified=False, owner_approved=False,
                changes_behavior=False, category=None, actor="orca"):
        def operation():
            path, note, original_hash = self._find(note_id)
            if self.index.suppressed(note.markdown()):
                raise ValueError("Suppressed content cannot be promoted")
            if note.visibility == "shared" and note.status == "approved":
                return {"status": "approved", "note": note}
            conflicts, duplicates = [], []
            for _, existing in self._notes():
                if existing.id == note.id or existing.visibility != "shared":
                    continue
                if normalized(existing.body) == normalized(note.body):
                    duplicates.append(existing)
                elif existing.category == (category or note.category) and contradicts(existing.body, note.body):
                    conflicts.append(existing)
            if duplicates:
                return {"status": "duplicate", "note": note, "duplicate_ids": [n.id for n in duplicates]}
            if conflicts:
                self.evidence.append({"kind": "brain_conflict", "versions": [
                    {**n.metadata(), "body": n.body} for n in [note, *conflicts]
                ]}, actor=actor)
                return {"status": "conflicting", "note": note, "conflict_ids": [n.id for n in conflicts]}
            automatic = (note.type == "fact" and note.confidence == "direct" and bool(note.evidence_refs)
                         and (owner_stated or verified) and not changes_behavior)
            if note.confidence == "conflicting" or not (automatic or owner_approved):
                proposed = replace(note, status="proposed")
                self.writer._write(proposed, actor, original_hash, path)
                self.evidence.append({"kind": "promotion_pending", "note_id": note.id}, actor=actor)
                return {"status": "proposed", "note": proposed}
            promoted = replace(note, visibility="shared", status="approved", category=category or note.category,
                               last_verified_at=utc_now() if verified else note.last_verified_at)
            target = self.writer.path_for(promoted)
            if target != path and target.exists():
                raise ValueError("Promotion destination already exists")
            if content_hash(path.read_text(encoding="utf-8")) != original_hash:
                self.writer._write(promoted, actor, original_hash, path)
                return {"status": "owner_edit_preserved", "note": note}
            self.writer._write(promoted, actor, original_hash, target)
            if path != target:
                path.unlink()
            self.index.incremental_pass()
            self.evidence.append({"kind": "promoted", "note_id": note.id, "automatic": automatic}, actor=actor)
            return {"status": "approved", "note": promoted}

        return self.writer.submit(operation, actor=actor)

    def undo(self, note_id, *, actor="owner"):
        def operation():
            path, note, _ = self._find(note_id)
            if note.visibility != "notebook" or path.is_relative_to(self.root / "brain/proposals"):
                raise ValueError("Only notebook learning is undoable")
            path.unlink()
            self.index.incremental_pass()
            self.evidence.append({"kind": "learning_undone", "note_id": note_id}, actor=actor)
        return self.writer.submit(operation, actor=actor)

    def forget(self, text, *, actor="owner"):
        term = clean(text).strip().casefold()
        if not term:
            raise ValueError("Forget needs a nonempty term")

        def operation():
            terms = self.index.suppressions()
            if term not in terms:
                terms.append(term)
            inside(self.root, self.index.suppression_path).write_text(json.dumps(terms), encoding="utf-8")
            removed = []
            for path, note in list(self._notes()):
                if self.index.suppressed(note.markdown() + " " + str(path.relative_to(self.root))):
                    path.unlink()
                    removed.append(note.id)
            # Forget intentionally emits no evidence event: retained logs are byte-identical.
            self.index.rebuild()
            return removed

        return self.writer.submit(operation, actor=actor)

    def remember_again(self, text, *, actor="owner"):
        term = clean(text).strip().casefold()

        def operation():
            terms = [item for item in self.index.suppressions() if item != term]
            inside(self.root, self.index.suppression_path).write_text(json.dumps(terms), encoding="utf-8")
            self.index.rebuild()
        return self.writer.submit(operation, actor=actor)

    def record_feedback(self, kind, text, *, actor="owner", **details):
        return self.evidence.append({**details, "kind": "feedback", "feedback_type": kind, "text": text}, actor=actor)

    def digest_items(self):
        """Current notebook items, conflicts with sources, and unapproved proposals."""
        with self.writer.lock:
            notes = list(self._notes())
            conflicts = []
            for path in sorted((self.root / "evidence").glob("*.jsonl")):
                for line in inside(self.root, path).read_text(encoding="utf-8").splitlines():
                    event = json.loads(line)
                    if event.get("kind") == "brain_conflict" and not self.index.suppressed(line):
                        conflicts.append(clean(event))
            return clean({"learned": [{**note.metadata(), "undo": note.id} for path, note in notes
                                if note.visibility == "notebook" and not path.is_relative_to(self.root / "brain/proposals")],
                    "promotions": [note.metadata() for path, note in notes if note.status == "proposed"
                                   and not path.is_relative_to(self.root / "brain/proposals")],
                    "conflicts": conflicts,
                    "proposals": [note.metadata() for path, note in notes
                                  if path.is_relative_to(self.root / "brain/proposals")]})

    def nightly_reflection(self, feedback_events, propose, *, day=None):
        """One bounded model pass per day. The injected callable receives excerpts only."""
        day = day or datetime.now(timezone.utc).date().isoformat()
        datetime.strptime(day, "%Y-%m-%d")
        marker = inside(self.root, self.root / "state" / ("reflection-" + day))

        def claim():
            if marker.exists():
                return False
            marker.write_text("claimed\n", encoding="utf-8")
            return True

        excerpts = []
        refs = []
        for event in feedback_events:
            if event.get("timestamp", day)[:10] != day:
                continue
            excerpt = clean({"kind": event.get("feedback_type", event.get("kind", "feedback")),
                             "text": str(event.get("text", ""))})
            excerpt["text"] = excerpt["text"][:1000]
            if excerpt["text"] and not self.index.suppressed(json.dumps(excerpt)):
                excerpts.append(excerpt)
                ref = event.get("evidence_ref")
                if isinstance(ref, str) and ref.startswith("evidence://"):
                    refs.append(ref)
            if len(excerpts) == 100:
                break
        if not excerpts or not self.writer.submit(claim):
            return None
        text = clean(propose(json.dumps(excerpts, ensure_ascii=False)))
        if not isinstance(text, str):
            raise ValueError("Reflection must propose text")
        if not text.strip() or self.index.suppressed(text):
            return None
        note = Note(category="skills", type="lesson", summary="Nightly reflection " + day,
                    status="proposed", confidence="inferred", evidence_refs=refs, body=text)
        return self.writer.write(note, path=self.root / "brain/proposals" / (note.id + ".md"))
