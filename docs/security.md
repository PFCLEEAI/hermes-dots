# Security and data handling

[README](../README.md) · [Install](install.md) · [Settings](settings.md)

These are v1 safety requirements. Local and mock verification do not establish live-provider or production behavior.

## Isolation and code execution

Orca runs in its own dots home with its own Hermes environment, profile, and kanban home. The installer does not read or write stock `~/.hermes` and will not overwrite a profile. The path guard resolves symlinks and always denies `~/.hermes`; extra denied paths are add-only and cannot be unlocked by chat.

Agent shell and code use non-persistent Docker containers. Orca's workspace is writable; Hermes skills and cache, when mounted, are read-only inside the dots home. Credential files and host paths outside the dots home are not mounted. Without Docker, code work pauses and health reports failure. Orca watches its sandbox work and files you provide through Slack.

## Owner and approvals

Only the configured owner Slack user can command Orca. A task card records the goal, sources, allowed actions, approval points, trigger, outputs, notification rules, and done evidence. Work begins after confirmation. Thread replies apply to a specific card and revision; an ambiguous reply prompts a question.

Action rules are `auto`, `when_told`, `ask_first`, and `hand_to_owner`. Reading, research, writing in Orca's space, sandbox code, and temporary helpers default to `auto`. Sending or posting to anyone except the owner, deleting outside scratch, spending, and account changes default to `ask_first` and can never become `auto`.

Approval is single-use and binds the exact action, target, content, and card revision. Changing any part requires new approval. Emails, web pages, and files cannot grant permission, change settings, or override rules. Temporary helpers cannot write the brain or message the owner.

**v1 cannot send email.** Gmail uses only `gmail.readonly`; there are no mailbox mutations or Gmail draft creation. Drafts are text on Slack cards for you to copy.

## Secrets and provider data

Credentials live in a local secrets file with mode `0600` or the macOS keychain. They do not belong in source control, notes, logs, prompts, or tool results. Redaction applies before evidence logging and model calls. Only needed excerpts go to your configured provider; whole raw logs are not sent. Slack receives cards, drafts, and digests.

Local storage does not mean no data leaves the machine: Slack and the chosen model provider process the content sent to them, and later Gmail connection reads mailbox content. Choose connections with that boundary in mind.

## Memory, learning, and forgetting

Markdown is the source of truth; the index is rebuildable. One serialized writer records who wrote each change. If you edited a note since Orca read it, its update becomes a separate labeled addition and you are told, rather than having your edit overwritten.

Corrections and lessons become undoable learned notebook items. Only owner-stated facts or verified results may be promoted automatically to shared notes. Inferred preferences and behavior changes wait for your approval. Conflicts show both versions with sources. Nightly reflection writes skill or prompt proposals, never applies them.

“Forget X” removes X from shared notes, notebook, proposals, and search, and suppresses re-learning and re-indexing from kept logs. “Remember X again” lifts suppression. **Forget does not erase evidence logs.** Logs are append-only, kept forever by default; only owner-configured retention deletes old logs and records that deletion.

## Quiet hours, failures, and recovery

Urgent mail, nudges, and health pings wait until quiet hours end; logging is immediate. Failed access is reported as failure, never “no change.” Model limits pause model work while checks and commands continue. Missed runs appear in digests and are not replayed automatically.

Interrupted work is reported at restart. Slack delivery IDs support history reconciliation; if the outcome cannot be checked, it stays `unknown` in status. Completion requires evidence; “not done” reopens a task. Use `pause`, `resume`, `stop <task>`, and `status` to control work.

## Public package checks

Never include owner-specific names, addresses, IDs, paths, tokens, or research notes. Use invented examples at `example.com`. Local configuration stays outside the repo; `_pm/` and `.dryforge/` are excluded.

`bash scripts/verify.sh` runs tests and credential and personal-data scanners. The latter reads a local terms file through `HERMES_DOTS_PRIVATE_TERMS`; missing terms mean no owner-specific check. A clean scan is necessary before publication, not evidence of live integrations. Report suspected leaks privately to the maintainer rather than including sensitive content in public issues.
