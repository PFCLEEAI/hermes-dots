"""Serialized brain writes and append-only redacted evidence."""

from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
from queue import Empty, Queue
from threading import RLock
from uuid import uuid4

from .notes import Note, clean, content_hash, utc_now


CATEGORIES = ("People", "Projects", "Work & Clients", "Topics & Learning", "About Me",
              "Decisions", "Lessons & Incidents", "Sources", "Daily Log")
DEFAULTS = {"index_times": ["08:30", "17:30"], "log_retention_days": None}
_locks = {}
_locks_lock = RLock()


def root_lock(root):
    with _locks_lock:
        return _locks.setdefault(str(Path(root).resolve()), RLock())


def inside(root, path):
    root = Path(root).resolve()
    path = Path(path)
    if not path.resolve().is_relative_to(root):
        raise ValueError("Brain path escapes its data root")
    return path


def bootstrap(root):
    root = Path(root).resolve()
    folders = ["brain/shared/" + category for category in CATEGORIES]
    folders += ["brain/notebook/lessons", "brain/notebook/skills", "brain/proposals", "evidence", "state"]
    for folder in folders:
        inside(root, root / folder).mkdir(parents=True, exist_ok=True)
    readme = inside(root, root / "brain/README.md")
    try:
        with readme.open("x", encoding="utf-8") as stream:
            stream.write("# Brain notes\n\nMarkdown is the source of truth. Notes use the exact spec 10.3 "
                         "front matter, with JSON-compatible YAML values. One writer labels all changes; "
                         "helpers cannot write. Owner edits win. Notebook learning is undoable; "
                         "inferences and behavior changes need approval before promotion. "
                         "Proposals are never applied automatically. Forget preserves evidence logs.\n")
    except FileExistsError:
        pass
    return root


class EvidenceLog:
    def __init__(self, root):
        self.root = bootstrap(root)
        self.lock = root_lock(self.root)

    def append(self, event, *, actor="orca", now=None):
        if actor not in {"owner", "orca"}:
            raise PermissionError("Helpers cannot write evidence")
        now = now or datetime.now(timezone.utc)
        record = clean(dict(event))
        record.update(id="event-" + uuid4().hex, actor=actor, timestamp=now.isoformat())
        path = inside(self.root, self.root / "evidence" / (now.date().isoformat() + ".jsonl"))
        with self.lock, path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
        return f"evidence://{path.name}#{record['id']}"

    def retain(self, days=None, *, now=None):
        if days is None:
            return []
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise ValueError("Retention must be a positive number of days")
        now = now or datetime.now(timezone.utc)
        cutoff = now.date() - timedelta(days=days)
        removed = []
        with self.lock:
            for path in sorted((self.root / "evidence").glob("*.jsonl")):
                inside(self.root, path)
                try:
                    day = datetime.strptime(path.stem, "%Y-%m-%d").date()
                except ValueError:
                    day = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date()
                if day < cutoff:
                    path.unlink()
                    removed.append(path.name)
            self.append({"kind": "log_retention", "days": days, "removed": removed}, now=now)
        return removed


class BrainWriter:
    def __init__(self, root, index=None, evidence=None):
        self.root = bootstrap(root)
        if index is None:
            from .index import BrainIndex
            index = BrainIndex(self.root)
        self.index = index
        self.evidence = evidence or EvidenceLog(self.root)
        self.lock = root_lock(self.root)
        self.queue = Queue()

    def submit(self, operation, *, actor="orca"):
        if actor not in {"owner", "orca"}:
            raise PermissionError("Helpers cannot write the brain")
        future = Future()
        self.queue.put((future, operation))
        lock_path = inside(self.root, self.root / "state/brain.lock")
        with self.lock, lock_path.open("a") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            while True:
                try:
                    queued, operation = self.queue.get_nowait()
                except Empty:
                    break
                try:
                    queued.set_result(operation())
                except Exception as error:
                    queued.set_exception(error)
                finally:
                    self.queue.task_done()
        return future.result()

    def path_for(self, note):
        note.validate()
        return inside(self.root, self.root / "brain" / note.visibility / note.category / (note.id + ".md"))

    def read(self, path):
        with self.lock:
            text = inside(self.root / "brain", path).read_text(encoding="utf-8")
            return Note.parse(text), content_hash(text)

    def write(self, note, *, actor="orca", expected_hash=None, path=None):
        if actor not in {"owner", "orca"}:
            raise PermissionError("Helpers cannot write the brain")
        # Snapshot mutable caller data before it enters the queue.
        note = Note(**clean({**note.metadata(), "body": note.body})).validate()
        return self.submit(lambda: self._write(note, actor, expected_hash, path), actor=actor)

    def _write(self, note, actor, expected_hash=None, path=None):
        note.owner = actor
        text = note.markdown()
        if self.index.suppressed(text):
            raise ValueError("Suppressed content cannot be written")
        path = inside(self.root / "brain", path or self.path_for(note))
        path.parent.mkdir(parents=True, exist_ok=True)
        conflict = False
        if path.exists():
            current = path.read_text(encoding="utf-8")
            if expected_hash is None or content_hash(current) != expected_hash:
                # Append preserves the owner's exact bytes, including front matter.
                addition = f"\n\n## Addition by {actor} at {utc_now()}\n\n{note.summary}\n\n{note.body}\n"
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(addition)
                conflict = True
                self.evidence.append({"kind": "owner_edit_preserved", "note_id": note.id,
                                      "told_owner": True, "notification": "Owner edit preserved; update appended."},
                                     actor=actor)
            else:
                path.write_text(text, encoding="utf-8")
        else:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(text)
        self.index.index_note(path)
        self.evidence.append({"kind": "brain_write", "note_id": note.id,
                              "path": str(path.relative_to(self.root)), "addition": conflict}, actor=actor)
        return path
