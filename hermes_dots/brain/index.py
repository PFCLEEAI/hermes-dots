"""A disposable lexical index of Markdown and redacted JSONL evidence."""

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3

from .notes import Note, clean, content_hash
from .storage import bootstrap, inside, root_lock


class BrainIndex:
    def __init__(self, root):
        self.root = bootstrap(root)
        self.lock = root_lock(self.root)
        self.database = inside(self.root, self.root / "state/index.sqlite")
        self.suppression_path = inside(self.root, self.root / "state/suppression.json")
        with self.lock, self._connect() as db:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS documents USING fts5("
                       "id UNINDEXED, content, citation UNINDEXED, status UNINDEXED, confidence UNINDEXED)")
            db.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, hash TEXT NOT NULL)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(inside(self.root, self.database))
        try:
            with db:
                yield db
        finally:
            db.close()

    def suppressions(self):
        path = inside(self.root, self.suppression_path)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def suppressed(self, text):
        text = text.casefold()
        return any(term in text for term in self.suppressions())

    def _insert(self, db, id, content, citation, status="", confidence=""):
        db.execute("DELETE FROM documents WHERE id = ?", (id,))
        content = clean(content)
        if not self.suppressed(content + " " + id + " " + citation):
            db.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
                       (id, content, citation, status, confidence))

    def _note(self, db, path, text):
        note = Note.parse(text)
        self._insert(db, note.id, text, "brain://" + str(path.relative_to(self.root / "brain")),
                     note.status, note.confidence)

    def index_note(self, path):
        path = inside(self.root / "brain", Path(path))
        text = path.read_text(encoding="utf-8")
        with self.lock, self._connect() as db:
            self._note(db, path, text)
            db.execute("INSERT OR REPLACE INTO files VALUES (?, ?)",
                       (str(path.relative_to(self.root)), content_hash(text)))

    def incremental_pass(self):
        return self._scan(False)

    def rebuild(self):
        return self._scan(True)

    def _scan(self, rebuild):
        changed = 0
        with self.lock, self._connect() as db:
            if rebuild:
                db.execute("DELETE FROM documents")
                db.execute("DELETE FROM files")
            # Reapply suppression even to otherwise unchanged documents.
            for rowid, id, content, citation in db.execute("SELECT rowid, id, content, citation FROM documents").fetchall():
                if self.suppressed(content + " " + id + " " + citation):
                    db.execute("DELETE FROM documents WHERE rowid = ?", (rowid,))
            paths = []
            for folder in ("shared", "notebook", "proposals"):
                paths.extend((self.root / "brain" / folder).rglob("*.md"))
            paths.extend((self.root / "evidence").glob("*.jsonl"))
            present = set()
            for path in sorted(paths):
                inside(self.root, path)
                key = str(path.relative_to(self.root))
                present.add(key)
                text = path.read_text(encoding="utf-8")
                digest = content_hash(text)
                previous = db.execute("SELECT hash FROM files WHERE path = ?", (key,)).fetchone()
                if previous and previous[0] == digest:
                    continue
                citation = ("evidence://" + path.name + "#") if path.suffix == ".jsonl" else ("brain://" + str(path.relative_to(self.root / "brain")))
                db.execute("DELETE FROM documents WHERE citation = ? OR substr(citation, 1, ?) = ?",
                           (citation, len(citation), citation))
                if path.suffix == ".md":
                    self._note(db, path, text)
                else:
                    for number, line in enumerate(text.splitlines(), 1):
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue  # An append may be incomplete; retry on the next changed-file pass.
                        event_id = str(event.get("id", number))
                        self._insert(db, f"log-{path.stem}-{event_id}", json.dumps(event, ensure_ascii=False),
                                     citation + event_id)
                db.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (key, digest))
                changed += 1
            for (key,) in db.execute("SELECT path FROM files").fetchall():
                if key not in present:
                    citation = "evidence://" + Path(key).name + "#" if key.startswith("evidence/") else "brain://" + key.removeprefix("brain/")
                    db.execute("DELETE FROM documents WHERE citation = ? OR substr(citation, 1, ?) = ?",
                               (citation, len(citation), citation))
                    db.execute("DELETE FROM files WHERE path = ?", (key,))
        return changed

    def search(self, query, limit=10):
        tokens = re.findall(r"\w+", clean(query), re.UNICODE)
        if not tokens or self.suppressed(query):
            return []
        expression = " AND ".join('"' + token + '"' for token in tokens)
        with self.lock, self._connect() as db:
            rows = db.execute(
                "SELECT id, snippet(documents, 1, '[', ']', '…', 24), citation, content, "
                "bm25(documents) - CASE WHEN status = 'approved' THEN 0.000001 ELSE 0 END "
                "- CASE WHEN confidence = 'direct' THEN 0.0000005 ELSE 0 END AS score "
                "FROM documents WHERE documents MATCH ? ORDER BY score, id", (expression,)).fetchall()
        return [{"id": id, "snippet": snippet, "citation": citation, "score": score}
                for id, snippet, citation, content, score in rows
                if not self.suppressed(content + " " + id + " " + citation)][:max(0, limit)]
