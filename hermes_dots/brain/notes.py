"""The small, JSON-compatible YAML front matter used by brain notes."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import uuid4


ENUMS = {
    "owner": {"orca", "owner"},
    "type": {"fact", "preference", "decision", "lesson", "source", "summary", "idea"},
    "confidence": {"direct", "synthesized", "inferred", "conflicting", "unverified"},
    "visibility": {"notebook", "shared"},
    "status": {"learned", "approved", "proposed", "superseded", "rejected"},
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean(value):
    # Import at use time: the security task owns this interface.
    from hermes_dots.security import redact

    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {clean(key): clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value


@dataclass
class Note:
    id: str = field(default_factory=lambda: "note-" + uuid4().hex)
    owner: str = "orca"
    category: str = "lessons"
    type: str = "lesson"
    summary: str = ""
    source_task: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    confidence: str = "unverified"
    visibility: str = "notebook"
    status: str = "learned"
    supersedes: str | None = None
    created_at: str = field(default_factory=utc_now)
    last_verified_at: str | None = None
    body: str = ""

    def validate(self):
        for name, values in ENUMS.items():
            if not isinstance(getattr(self, name), str) or getattr(self, name) not in values:
                raise ValueError(f"Invalid note {name}")
        if not isinstance(self.id, str) or not re.fullmatch(r"note-[\w-]+", self.id):
            raise ValueError("Invalid note id")
        for name in ("category", "summary", "body"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"Invalid note {name}")
        if not self.category or self.category in {".", ".."} or any(
            char in self.category for char in "/\\\n\r"
        ):
            raise ValueError("Invalid category")
        if self.source_task is not None and (
            not isinstance(self.source_task, str) or not self.source_task.startswith("TASK-")
        ):
            raise ValueError("Invalid source_task")
        if not isinstance(self.evidence_refs, list) or any(
            not isinstance(ref, str) or not ref.startswith("evidence://")
            for ref in self.evidence_refs
        ):
            raise ValueError("Invalid evidence_refs")
        if self.supersedes is not None and (
            not isinstance(self.supersedes, str) or not re.fullmatch(r"note-[\w-]+", self.supersedes)
        ):
            raise ValueError("Invalid supersedes")
        for name in ("created_at", "last_verified_at"):
            value = getattr(self, name)
            if value is None and name == "last_verified_at":
                continue
            if not isinstance(value, str):
                raise ValueError(f"Invalid {name}")
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        return self

    def metadata(self):
        data = asdict(self)
        data.pop("body")
        return data

    def markdown(self):
        self.validate()
        lines = [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in self.metadata().items()]
        return "---\n" + "\n".join(lines) + "\n---\n" + self.body

    @classmethod
    def parse(cls, text):
        if not text.startswith("---\n"):
            raise ValueError("Missing note front matter")
        header, separator, body = text[4:].partition("\n---\n")
        if not separator:
            raise ValueError("Unclosed note front matter")
        data = {}
        for line in header.splitlines():
            key, colon, value = line.partition(":")
            if not colon or key in data:
                raise ValueError("Invalid or duplicate front matter field")
            value = value.strip()
            try:
                data[key] = json.loads(value)
            except json.JSONDecodeError:
                # Bare enum names and ISO timestamps are valid YAML scalars.
                if value.startswith(("[", "{", "\"", "'", "#")):
                    raise ValueError("Use JSON-compatible YAML values") from None
                data[key] = value
        if set(data) != set(cls().metadata()):
            raise ValueError("Note front matter must match spec 10.3 exactly")
        return cls(**data, body=body).validate()
