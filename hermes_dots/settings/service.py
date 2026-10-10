"""Confirmed chat changes, persisted settings, and shared local-time policy."""

from copy import deepcopy
from datetime import datetime, timedelta
import json
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from hermes_dots.security.paths import PathGuard
from hermes_dots.security.rules import ActionRules, DEFAULTS as RULE_DEFAULTS
from hermes_dots.security.secrets import redact
from .defaults import SETTINGS_DEFAULTS


DEFAULTS = {"vips": []}
CHANGE_SCHEMA = {
    "type": "object", "required": ["name", "value"], "additionalProperties": False,
    "properties": {"name": {"type": "string", "enum": list(SETTINGS_DEFAULTS) + list(DEFAULTS)}, "value": {}},
}


class SettingsService:
    def __init__(self, ledger, *, model=None, feedback=None, home=None):
        self.ledger = ledger
        self.model = model
        self.feedback = feedback
        self.home = home
        self.defaults = {**deepcopy(SETTINGS_DEFAULTS), **deepcopy(DEFAULTS)}
        self.defaults.setdefault("action_rules", deepcopy(RULE_DEFAULTS["action_rules"]))
        self.defaults.setdefault("extra_denied_paths", [])

    def list_settings(self):
        values = deepcopy(self.defaults)
        with self.ledger.transaction():
            for row in self.ledger.connection.execute("SELECT name,value FROM settings ORDER BY name"):
                values[row["name"]] = json.loads(row["value"])
        return values

    def get(self, name, default=None):
        return self.list_settings().get(name, default)

    @staticmethod
    def _time(value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
            raise ValueError("Times must be HH:MM")
        datetime.strptime(value, "%H:%M")
        return value

    def validate(self, name, value):
        if name not in self.defaults:
            raise ValueError("Unknown setting")
        if name == "timezone":
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("Time zone must be an IANA name")
                ZoneInfo(value)
        elif name in {"quiet_hours", "digest_times", "index_times"}:
            if not isinstance(value, list) or (name == "quiet_hours" and len(value) != 2):
                raise ValueError("Expected a list of times")
            for item in value:
                self._time(item)
        elif name in {"vips", "extra_denied_paths"}:
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                raise ValueError("Expected a list of nonempty strings")
            if name == "vips" and any(not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", item) for item in value):
                raise ValueError("Invalid VIP address")
            if name == "extra_denied_paths":
                if not set(self.get(name)).issubset(value):
                    raise PermissionError("Denied paths are add-only; ~/.hermes is always denied")
                PathGuard(value, home=self.home)
        elif name == "action_rules":
            if not isinstance(value, dict):
                raise ValueError("Expected action rules")
            value = ActionRules(value).as_dict()
        elif name == "display_name":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Display name must be nonempty")
        else:
            nullable = name in {"daily_token_cap", "log_retention_days"}
            if not (nullable and value is None):
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError("Expected a positive integer")
            candidate = {**self.list_settings(), name: value}
            if candidate["check_back_min_seconds"] > candidate["check_back_max_seconds"]:
                raise ValueError("Check-back minimum exceeds maximum")
        return deepcopy(value)

    def propose_change(self, name, value):
        value = self.validate(name, value)
        # Settings are not a credential store.
        if redact(json.dumps(value)) != json.dumps(value):
            raise ValueError("Secrets cannot be stored as settings")
        proposal = {"id": uuid4().hex, "name": name, "value": value,
                    "confirmation": f"Set {name} to {json.dumps(value, ensure_ascii=False)}; may I go ahead?"}
        with self.ledger.transaction():
            self.ledger.connection.execute("INSERT INTO setting_proposals(id,name,value,previous) VALUES (?,?,?,?)",
                                           (proposal["id"], name, json.dumps(value), json.dumps(self.get(name))))
        return proposal

    def parse_change(self, text):
        text = redact(text)
        parsed = None
        if self.model:
            try:
                parsed = self.model.complete_structured(CHANGE_SCHEMA, "Parse this owner setting request: " + text)
            except Exception:
                # Commands remain available during provider limits.
                parsed = None
        if not isinstance(parsed, dict) or set(parsed) != {"name", "value"}:
            parsed = self._fallback(text)
        return self.propose_change(parsed["name"], parsed["value"])

    def _fallback(self, text):
        quiet = re.fullmatch(r"(?:make |set )?quiet hours (\d{1,2})(?::(\d{2}))? to (\d{1,2})(?::(\d{2}))?", text.strip(), re.I)
        if quiet:
            a, b, c, d = quiet.groups()
            return {"name": "quiet_hours", "value": [f"{int(a):02}:{int(b or 0):02}", f"{int(c):02}:{int(d or 0):02}"]}
        vip = re.fullmatch(r"(add|remove) VIP (\S+)", text.strip(), re.I)
        if vip:
            action, address = vip.groups()
            values = self.get("vips")
            values = list(dict.fromkeys(values + [address])) if action.lower() == "add" else [x for x in values if x != address]
            return {"name": "vips", "value": values}
        digest = re.fullmatch(r"digest only at (\d{1,2})(?::(\d{2}))?", text.strip(), re.I)
        if digest:
            hour, minute = digest.groups()
            return {"name": "digest_times", "value": [f"{int(hour):02}:{int(minute or 0):02}"]}
        raise ValueError("Please name a setting and its new value")

    def confirm_change(self, proposal_id):
        if isinstance(proposal_id, dict):
            proposal_id = proposal_id["id"]
        with self.ledger.transaction():
            row = self.ledger.connection.execute("SELECT * FROM setting_proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None or row["confirmed"]:
                raise ValueError("Unknown or already confirmed setting proposal")
            name, value = row["name"], json.loads(row["value"])
            if self.get(name) != json.loads(row["previous"]):
                raise ValueError("Setting changed; request a fresh confirmation")
            value = self.validate(name, value)
            self.ledger.connection.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                                           (name, json.dumps(value)))
            self.ledger.connection.execute("UPDATE setting_proposals SET confirmed=1 WHERE id=?", (proposal_id,))
            feedback = {"kind": "setting_change", "name": name, "value": value}
            self.ledger.record_event("feedback", feedback)
        if self.feedback:
            self.feedback(feedback)
        return {"name": name, "value": value, "reply": f"Stored {name}: {json.dumps(value, ensure_ascii=False)}"}

    def local_now(self, moment):
        zone = self.get("timezone")
        return moment.astimezone(ZoneInfo(zone)) if zone else moment.astimezone()

    def is_quiet(self, moment):
        local = self.local_now(moment)
        start, end = self.get("quiet_hours")
        current = local.strftime("%H:%M")
        return start <= current < end if start <= end else current >= start or current < end

    def quiet_end(self, moment):
        local = self.local_now(moment)
        hour, minute = map(int, self.get("quiet_hours")[1].split(":"))
        end = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if end <= local:
            end += timedelta(days=1)
        return end

    def path_guard(self):
        return PathGuard(self.get("extra_denied_paths"), home=self.home)

    def action_rules(self):
        return ActionRules(self.get("action_rules"))
