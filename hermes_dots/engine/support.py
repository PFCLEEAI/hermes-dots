"""Small persistence helpers shared by the domain services."""

from datetime import datetime, timezone
import json
from uuid import uuid4

from hermes_dots.ledger import OutboxEffect
from hermes_dots.security.secrets import redact


def clean(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {clean(key): clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value


def encode(value):
    return json.dumps(clean(value), ensure_ascii=False, sort_keys=True)


def now(clock=None):
    value = clock.now() if clock else datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("Clock must be timezone-aware")
    return value


def stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def event(ledger, kind, payload, task_id=None, evidence=None):
    payload = clean(payload)
    result = ledger.record_event(kind, payload, task_id=task_id)
    if evidence:
        evidence.append({"kind": kind, "task_id": task_id, **payload})
    return result


def queue_ping(ledger, settings, clock, payload, *, task_id=None, ping_id=None):
    moment = now(clock)
    release = settings.quiet_end(moment) if settings.is_quiet(moment) else moment
    ping_id = ping_id or uuid4().hex
    with ledger.transaction():
        ledger.connection.execute(
            "INSERT INTO ping_queue(id,payload,release_at,task_id) VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (ping_id, encode(payload), stamp(release), task_id),
        )
    return ping_id


def release_pings(ledger, settings, clock, channel):
    """Move held pings to T2's outbox atomically; T6 owns actual delivery."""
    moment = now(clock)
    if settings.is_quiet(moment):
        return ()
    released = []
    with ledger.transaction():
        rows = ledger.connection.execute(
            "SELECT * FROM ping_queue WHERE cancelled=0 AND release_at<=? ORDER BY rowid", (stamp(moment),)
        ).fetchall()
        for row in rows:
            effect = ledger.enqueue_effect(OutboxEffect(
                row["id"], "ping:" + row["id"], channel, json.loads(row["payload"]), task_id=row["task_id"],
            ))
            ledger.connection.execute("DELETE FROM ping_queue WHERE id=?", (row["id"],))
            released.append(effect)
    return tuple(released)
