# Install hermes-dots

[README](../README.md) · [Security](security.md) · [Settings](settings.md)

This guide describes the v1 setup contract while implementation is in progress. Tests use temporary homes and fake services. Live connections are separate from local test evidence.

## Before you start

macOS is the first target. Use Python 3.11 or newer, Git, and Docker Desktop. You need a Slack workspace where you can install an app and a model provider configured in Hermes. Docker must be running for code work; if it goes down, code tasks pause and a health alert is held until quiet hours end.

## Install into a separate home

```bash
git clone https://github.com/PFCLEEAI/hermes-dots.git
cd hermes-dots
python3 scripts/install.py --home ~/.hermes-dots
```

The installer pins stock Hermes to `v2026.9.24` (`f97608f178d1ffeca59860195ab7da295f7c8e5f`), creates an isolated environment and Orca profile, and installs the pack with exact refs. Its own `HERMES_HOME` and `HERMES_KANBAN_HOME` live under the dots home. It refuses stock `~/.hermes` and refuses to overwrite an existing profile.

Follow installer output to use the installed environment. Do not use an unrelated global Hermes executable: it may select another installation. Keep configuration, model login, and gateway setup pointed at the dots environment. If install refuses an existing profile, inspect the reported target and select a new empty home rather than deleting it blindly.

## Connect Slack

1. Create one app in your own workspace. Use the [Slack guide at the pinned Hermes release](https://github.com/NousResearch/hermes-agent/blob/v2026.9.24/website/docs/user-guide/messaging/slack.md) for app and Socket Mode setup. That upstream guide shows stock-home paths; substitute the isolated dots home throughout.
2. Enable Socket Mode, the Messages tab, and direct-message event delivery. Install the app with the scopes needed for owner messages, cards, threads, and attachments. Channel mentions are outside dots v1.
3. Store bot and app credentials only in the installer-designated local secrets file with mode `0600`, or the macOS keychain. Never paste them into Slack chat, notes, tests, or this repository.
4. Copy your Slack member ID from your profile and configure it as the sole owner in local config. Match the Hermes allowlist to that owner. Other users cannot command Orca.
5. Configure your model in the isolated Hermes environment. Start the dots gateway using installer-provided instructions, then open a direct message with Orca.

The generated login-item plist is not loaded automatically. You choose whether to enable it after reviewing setup. For always-on use, also set Docker Desktop to start at login.

## Onboarding chat

Orca asks for your time zone, quiet hours, digest times, optional VIPs, and first responsibility. Every answer is a setting. Start with “Watch the practice inbox,” review the card's sources and allowed actions, and confirm before work begins.

Defaults: quiet hours 23:00–08:00, digests at 09:00 and 18:00, mail checks every 5 minutes. VIPs start empty. Use `status` to inspect work and its next check time; `settings` lists stored values. See [settings](settings.md).

## Gmail read-only later

This round uses invented Korean and English practice mail. The Gmail adapter is tested against mocks; connecting your real Google account is a later, owner-authorized step. There is no Gmail sign-in command prescribed here before that flow is available.

The only scope is `gmail.readonly`. The adapter reads incrementally, begins with unread mail from the last three days, and reports history gaps on resync. It does not label, mark read, archive, create mailbox drafts, or send. Drafts appear on Slack cards for you to copy. Sent mail can be read to learn your writing voice.

## Development verification

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[test]'
bash scripts/verify.sh
```

Tests use temporary homes, fake services, and a fake clock. Docker-dependent tests skip when Docker is unavailable. These checks do not validate a live account or running login item. Personal-data scanning additionally needs an external terms file configured through `HERMES_DOTS_PRIVATE_TERMS`.
