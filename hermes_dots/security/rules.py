"""Action levels and the non-negotiable outside-action floor."""

from enum import StrEnum


class RuleLevel(StrEnum):
    AUTO = "auto"
    WHEN_TOLD = "when_told"
    ASK_FIRST = "ask_first"
    HAND_TO_OWNER = "hand_to_owner"


DEFAULTS = {"action_rules": {
    "read": "auto", "research": "auto", "write": "auto", "code": "auto", "helpers": "auto",
    "send": "ask_first", "post": "ask_first", "delete": "ask_first",
    "spend": "ask_first", "account": "ask_first",
}}
FLOOR_ACTIONS = frozenset({"send", "post", "delete", "spend", "account"})


class RuleViolation(PermissionError):
    pass


class ActionRules:
    def __init__(self, rules=None):
        self._rules = dict(DEFAULTS["action_rules"])
        for action, level in (rules or {}).items():
            self._rules[action] = self.validate(action, level).value

    @staticmethod
    def validate(action, level):
        level = RuleLevel(level)
        if action in FLOOR_ACTIONS and level == RuleLevel.AUTO:
            raise RuleViolation("Send, post, delete, spend and account changes cannot be auto")
        return level

    def level(self, action):
        return RuleLevel(self._rules.get(action, "ask_first"))

    def confirmation_text(self, action, level):
        level = self.validate(action, level)
        return f"Change {action} from {self.level(action).value} to {level.value}; may I go ahead?"

    def change(self, action, level, *, confirmed=False):
        level = self.validate(action, level)
        if not confirmed:
            raise RuleViolation("Owner confirmation is required to change an action rule")
        self._rules[action] = level.value
        return level

    def requires_approval(self, action):
        return self.level(action) in {RuleLevel.ASK_FIRST, RuleLevel.HAND_TO_OWNER}

    def as_dict(self):
        return dict(self._rules)
