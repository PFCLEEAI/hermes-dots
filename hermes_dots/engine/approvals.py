"""T1's repository protocol backed by T2's atomic approval operations."""

import sqlite3

from hermes_dots.ledger.models import Approval as LedgerApproval
from hermes_dots.security.approvals import Approval, ApprovalRejected


class LedgerApprovalRepository:
    def __init__(self, ledger, task_id):
        self.ledger = ledger
        self.task_id = task_id

    def save(self, approval):
        if approval.used:
            raise ApprovalRejected("Cannot grant an already used approval")
        try:
            self.ledger.add_approval(LedgerApproval(
                approval.id, self.task_id, approval.action, approval.target,
                approval.content_hash, approval.card_revision, approval.used,
            ))
        except (sqlite3.IntegrityError, ValueError):
            raise ApprovalRejected("Approval already exists or card revision changed") from None

    def get(self, approval_id):
        with self.ledger.transaction():
            row = self.ledger.connection.execute(
                "SELECT * FROM approvals WHERE id=? AND task_id=?", (approval_id, self.task_id),
            ).fetchone()
        if row is None:
            return None
        return Approval(row["id"], row["action"], row["target"], row["content_hash"], row["card_revision"], bool(row["used"]))

    def consume(self, expected):
        if expected.used:
            return False
        with self.ledger.transaction():
            if self.get(expected.id) != expected:
                return False
            return self.ledger.consume_approval(
                expected.id, action=expected.action, target=expected.target,
                content_hash=expected.content_hash, card_revision=expected.card_revision,
            )
