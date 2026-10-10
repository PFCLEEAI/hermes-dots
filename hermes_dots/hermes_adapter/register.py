"""Hermes registration scaffold; integration is implemented in T8."""

HOOK_MODULES: list[str] = [
    "dots_approve",
    "dots_reject",
    "dots_make_vip",
    "dots_remove_vip",
    "dots_accept",
    "dots_dismiss",
    "dots_snooze",
    "dots_mark_handled",
    "dots_undo_learned",
    "dots_pick_version",
]


def register(ctx):
    pass
