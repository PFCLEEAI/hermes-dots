# Settings by chat

[README](../README.md) · [Install](install.md) · [Security](security.md)

Every v1 tunable is a setting. Tell Orca what you want in Slack; it proposes a value, waits for confirmation, saves it, and replies with what was stored. Changes are recorded as feedback. `settings` lists current values.

```text
make quiet hours 22 to 7
add VIP alex@example.com
digest only at 9
settings
```

## Settings and defaults

| Setting | v1 default / behavior |
| --- | --- |
| Time zone | Machine's local time zone |
| Quiet hours | 23:00–08:00; holds every ping |
| Digest times | 09:00 and 18:00 local time |
| VIPs | Empty; VIP mail is urgent |
| Check-back bounds | 1 minute to 6 hours |
| Mail check interval | 5 minutes |
| Snooze length | 1 day |
| Idea expiry | 7 days, or when its thread card closes |
| Index times | 08:30 and 17:30 local time; notes also index on write |
| Log retention | Forever unless you set a period |
| Daily token cap | Off; when reached, pauses model work until the next local day |
| Action rules | Reading, research, own-space writing, sandbox code, helpers: `auto`; outside sending/posting, deletion outside scratch, spending, account changes: `ask_first` |
| Extra denied paths | User-configured, add-only; `~/.hermes` always denied |
| Display name | Orca |

Questions get a nudge after one hour, then in digests. Quiet hours still apply. Settings cannot lower the authority floor: send, delete, spend, and account changes can never be `auto`, and denied paths cannot be unlocked.

Known outside-action tools are blocked by default. The running product cannot yet approve outside actions; that flow is planned for a later version. The sandbox has network access for research, so tool-name checks are not a network isolation boundary. Email can never be sent in v1; its adapter is read-only.

## Commands

| Command | What it does |
| --- | --- |
| `status` | Task state, last successful step, waiting on, next action and time, files, and health |
| `pause` | Pauses all work and linked schedules |
| `resume` | Resumes allowed work |
| `stop <task>` | Stops current task work, helpers, and future schedules; reports exactly what stopped |
| `settings` | Lists stored settings |
| `rules` | Lists action rules |
| `schedules` | Lists schedules for review or disabling |
| `forget <topic>` | Removes notes and index entries and suppresses re-learning; keeps logs |

“Remember X again” lifts suppression. “Not done” in a card thread reopens a completed task. Reply in the relevant thread or cite its card ID; if the intended task is unclear, Orca asks.

## Schedules and limits

Schedules are created by chat and linked to a task. Confirm the instruction, time, time zone, and optional end date; Orca reports stored values and offers a test run. A monitoring claim requires an actual schedule or trigger.

Provider limits and daily caps block model work while health checks and commands remain available. Missed runs are listed in the next digest rather than replayed. Quiet hours hold notifications, not evidence logging.
