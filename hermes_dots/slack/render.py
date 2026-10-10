"""Plain text fallbacks and Slack button payloads; no provider calls."""

from dataclasses import asdict, is_dataclass
import json
from typing import Mapping, Sequence

from .delivery import redact_payload


ACTION_LABELS = {
    "approve": "Approve", "reject": "Reject", "make_vip": "Make VIP",
    "remove_vip": "Remove VIP", "accept": "Accept", "dismiss": "Dismiss",
    "snooze": "Snooze", "mark_handled": "Mark handled",
    "undo_learned": "Undo learned", "pick_version": "Pick version",
}
# The wiring step registers these action IDs with Hermes's Slack action API.
HOOKS = [f"dots_{action}" for action in ACTION_LABELS]


def _display(value):
    if value is None or value == "" or value == () or value == []:
        return "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "—"
    return str(value)


def render_update(changed, evidence, impact, decision, next_check) -> str:
    rows = [("What changed", changed), ("Evidence", evidence), ("Impact", impact),
            ("What you need to decide", decision), ("Next check time", next_check)]
    return redact_payload("\n".join(f"{label}: {_display(value)}" for label, value in rows))


def render_status(tasks: Sequence[Mapping], *, unknown_deliveries: Sequence[str] = ()) -> str:
    lines = ["Status"]
    for task in tasks:
        lines.extend([
            f"#{task['id']} — {_display(task.get('state'))}",
            f"Last successful step: {_display(task.get('last_successful_step'))}",
            f"Waiting on: {_display(task.get('waiting_on'))}",
            f"Next action: {_display(task.get('next_action'))}",
            f"Next action time: {_display(task.get('next_action_at', task.get('next_time')))}",
            f"Files: {_display(task.get('files'))}",
        ])
    if not tasks:
        lines.append("No tasks.")
    if unknown_deliveries:
        lines.append(f"Unknown Slack deliveries: {_display(unknown_deliveries)}")
    return redact_payload("\n".join(lines))


def render_card(card, *, actions: Sequence[str] = ("approve", "reject"),
                action_values: Mapping[str, object] | None = None) -> dict:
    record = asdict(card) if is_dataclass(card) else dict(card)
    task_id, revision = record["id"], record["revision"]
    fields = ["goal", "sources", "allowed_actions", "approval_points", "trigger",
              "end_date", "outputs", "notify_rules", "done_evidence", "status"]
    text = f"#{task_id} · revision {revision}\n" + "\n".join(
        f"{field.replace('_', ' ').capitalize()}: {_display(record.get(field))}" for field in fields
    )
    buttons = []
    for action in actions:
        label = ACTION_LABELS[action]
        value = {"task_id": task_id, "revision": revision,
                 "value": (action_values or {}).get(action)}
        buttons.append({"type": "button", "action_id": f"dots_{action}",
                        "text": {"type": "plain_text", "text": label},
                        "value": json.dumps(redact_payload(value), ensure_ascii=False)})
    return redact_payload({"text": text, "buttons": buttons})
