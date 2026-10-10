"""Offline-testable Slack bridge; live clients are supplied by the adapter."""

from .bridge import Binding, Command, Route, SlackBridge, parse_command
from .delivery import QuietHours, QuietHoursDispatcher, SlackClient, SlackReconciler
from .digest import DigestComposer, SECTIONS
from .onboarding import DEFAULTS, Onboarding, SettingsService
from .render import ACTION_LABELS, HOOKS, render_card, render_status, render_update

__all__ = [
    "ACTION_LABELS", "Binding", "Command", "DEFAULTS", "DigestComposer", "HOOKS",
    "Onboarding", "QuietHours", "QuietHoursDispatcher", "Route", "SECTIONS",
    "SettingsService", "SlackBridge", "SlackClient", "SlackReconciler",
    "parse_command", "render_card", "render_status", "render_update",
]
