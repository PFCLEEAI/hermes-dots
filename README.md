# hermes-dots

![Soft glowing dots connected by thin lines on a navy-to-indigo background](site/assets/hero-readme.webp)

A dots-style personal assistant for Hermes Agent — remembers, follows up, and watches your inbox.

An open-source plugin pack on stock [Hermes Agent](https://github.com/NousResearch/hermes-agent), with one personal assistant called **Orca**. One owner, one isolated install, and Slack as the interface. Your model stays the one you configure in Hermes.

[Landing page](https://pfcleeai.github.io/hermes-dots/) · [Install](docs/install.md) · [Security](docs/security.md) · [Settings](docs/settings.md)

**v1 is under development.** The features below describe the v1 contract. Verification uses invented practice mail and fake services; it does not establish live Slack, Gmail, or model operation. Real Gmail connection comes later.

## Features

- **Memory with sources.** Markdown notes, a rebuildable search index, and redacted append-only event and conversation logs. Tasks search the brain first and cite notes. Your edits win; conflicts surface for review.
- **Proactive follow-ups.** Confirm a task card, then Orca picks check-back times with reasons. Questions include a recommendation; nudges respect quiet hours while other allowed work continues. Done means a summary with evidence.
- **Email watching.** Urgent mail gets a ping; regular and uncertain mail goes into a digest; noise gets a count. Reply drafts stay on Slack cards for you to copy. Practice mail is in Korean and English; the Gmail read-only adapter is tested with mocks.
- **Settings by chat.** Change timing, VIPs, limits, display name, and action rules through a confirm-and-save conversation.
- **Safe by default.** An isolated home, Docker for code, denied paths, owner-only commands, and single-use approvals for specific outside actions. Email sending is outside v1.

## Quickstart

macOS first. Have Python 3.11+, Git, Docker Desktop, a Slack workspace you can install an app into, and your chosen Hermes model provider available.

```bash
git clone https://github.com/PFCLEEAI/hermes-dots.git
cd hermes-dots
python3 scripts/install.py --home ~/.hermes-dots
```

1. **Install separately.** The installer uses Hermes `v2026.9.24` in the dots home, creates the Orca profile, and refuses `~/.hermes` and existing profiles. Follow the installer output for the isolated environment; keep all setup commands pointed at that home.
2. **Connect Slack.** Create one app for your workspace using the pinned Hermes Slack setup, configure Socket Mode and direct messages, and store credentials only in the local protected secrets file or keychain. Set your own Slack member ID as the sole owner. See [install details](docs/install.md#connect-slack).
3. **Chat with Orca.** Complete onboarding: time zone, quiet hours, digest times, optional VIPs, and your first responsibility. Start with “Watch the practice inbox.” Review and confirm the task card before work starts.

**Gmail read-only later:** v1 builds and mock-tests the adapter; it does not connect a real mailbox during the build. A later owner-authorized connection must use only `gmail.readonly`. It will not label, mark as read, create Gmail drafts, or send mail.

The installer generates a login-item plist but does not load it. You choose when to enable the gateway and Docker Desktop at login. See [the full installation guide](docs/install.md).

## Settings by chat

```text
make quiet hours 22 to 7
add VIP alex@example.com
digest only at 9
settings
```

Orca proposes the new value, waits for your confirmation, saves it, and reports what was stored. Defaults: local time zone, quiet hours 23:00–08:00, digests at 09:00 and 18:00, and mail checks every 5 minutes. [All settings and commands](docs/settings.md).

## Safety model

- **Isolation:** separate `HERMES_HOME` and kanban home. `~/.hermes` is always denied; extra denied paths can only be added. Code runs in non-persistent Docker containers without credential mounts.
- **Authority:** reading, research, writing in Orca's space, sandbox code, and temporary helpers are allowed by default. Sending or posting to anyone but you, deletion outside scratch, spending, and account changes ask first and can never become `auto`. Approval binds the action, target, content, and card revision; it is consumed once.
- **Privacy:** secrets stay local and are redacted from notes, logs, prompts, and tool results. Only necessary excerpts reach your configured model provider. Email, web pages, and files are data, never permission to change rules.
- **Memory control:** “forget” removes notes and search entries and suppresses re-learning; retained logs stay intact. Logs are kept forever unless you set retention. Skill and prompt changes are proposals for you to approve.
- **Recovery:** interrupted work is reported, uncertain deliveries remain visible, and missed scheduled runs are listed rather than caught up automatically. Health and usage limits pause affected work.

See [security and data handling](docs/security.md) for the boundaries.

## Roadmap

These are later scope, outside v1:

- Multiple persistent agents, handoffs between agents, saved workflows, and an independent reviewer.
- Web UI with chat, a collapsible sidebar, editable pages, and a visual agent map.
- Sending email.
- Slack channel mentions.
- Server hosting.
- Voice.
- Formal A/B testing of self-improvements.

## Contributing

Keep changes small, use Python's standard library where practical, and test with invented data and fake services. Never include personal data, credentials, live service responses, or local configuration. Development planning folders `_pm/` and `.dryforge/` are excluded from the public package.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[test]'
bash scripts/verify.sh
```

Verification runs unit and acceptance tests, credential scanning, and personal-data scanning. The latter needs an external local terms file via `HERMES_DOTS_PRIVATE_TERMS`; without it, there is no owner-specific scan coverage. Never commit that file. Do not use real Gmail, Slack, or model calls in tests. Open a [pull request](https://github.com/PFCLEEAI/hermes-dots/pulls) with the behavior changed and test evidence.

## License

[MIT](LICENSE). Built on [Hermes Agent by Nous Research (MIT)](https://github.com/NousResearch/hermes-agent/tree/v2026.9.24). hermes-dots is an independent plugin pack.
