"""Exact-payload, single-use approvals with an atomic repository contract."""

from dataclasses import dataclass, replace
from hashlib import sha256
from threading import RLock
from typing import Protocol
from uuid import uuid4


class ApprovalRejected(PermissionError):
    pass


def content_hash(content):
    return sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Approval:
    id: str
    action: str
    target: str
    content_hash: str
    card_revision: int
    used: bool = False


class ApprovalRepository(Protocol):
    def save(self, approval: Approval) -> None: ...
    def get(self, approval_id: str) -> Approval | None: ...
    def consume(self, expected: Approval) -> bool:
        """Atomically mark used only if the stored unused record equals expected."""
        ...


class InMemoryApprovalRepository:
    def __init__(self):
        self._records = {}
        self._lock = RLock()

    def save(self, approval):
        with self._lock:
            if approval.id in self._records:
                raise ApprovalRejected("Approval already exists")
            self._records[approval.id] = approval

    def get(self, approval_id):
        with self._lock:
            return self._records.get(approval_id)

    def consume(self, expected):
        with self._lock:
            if expected.used or self._records.get(expected.id) != expected:
                return False
            self._records[expected.id] = replace(expected, used=True)
            return True


class Approvals:
    def __init__(self, repository=None):
        self.repository = repository if repository is not None else InMemoryApprovalRepository()

    @staticmethod
    def _validate_revision(card_revision):
        if isinstance(card_revision, bool) or not isinstance(card_revision, int) or card_revision < 1:
            raise ValueError("Card revision must be a positive integer")

    def grant(self, action, target, content, card_revision):
        self._validate_revision(card_revision)
        approval = Approval(uuid4().hex, action, target, content_hash(content), card_revision)
        self.repository.save(approval)
        return approval

    def consume(self, approval_id, action, target, content, card_revision):
        self._validate_revision(card_revision)
        expected = Approval(approval_id, action, target, content_hash(content), card_revision)
        if not self.repository.consume(expected):
            raise ApprovalRejected("Approval missing, used, or does not match the action payload")
        return replace(expected, used=True)
