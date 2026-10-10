"""Shared setting defaults; None uses the documented unset behavior."""

SETTINGS_DEFAULTS = {
    "timezone": None,  # Machine local time zone.
    "quiet_hours": ["23:00", "08:00"],
    "digest_times": ["09:00", "18:00"],
    "mail_check_interval_seconds": 300,
    "snooze_days": 1,
    "idea_expiry_days": 7,
    "check_back_min_seconds": 60,
    "check_back_max_seconds": 21600,
    "nudge_delay_seconds": 3600,
    "index_times": ["08:30", "17:30"],
    "log_retention_days": None,  # Keep logs forever.
    "daily_token_cap": None,
    "display_name": "Orca",
    'extra_denied_paths': [],
    'action_rules': {'read': 'auto', 'research': 'auto', 'write': 'auto', 'code': 'auto', 'helpers': 'auto', 'send': 'ask_first', 'post': 'ask_first', 'delete': 'ask_first', 'spend': 'ask_first', 'account': 'ask_first'},
    "vips": [],
    "first_responsibility": None,
}


def merge_defaults(*dicts):
    """Merge module defaults idempotently, rejecting conflicting values."""
    merged = {}
    for defaults in dicts:
        for key, value in defaults.items():
            if key in merged and merged[key] != value:
                raise ValueError(f"Conflicting setting default: {key}")
            merged[key] = value
    return merged
