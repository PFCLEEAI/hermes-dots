# Invented practice mail

`mail.json` is a scenario-grouped input set of 30 messages (15 English, 15 Korean),
including four owner replies. `sent.json` contains ten separate read-only voice
samples (five per language). Every identity and Acme scenario is invented.
Owner identity is `owner@example.com`; all addresses use `example.com`.

Reference time for backlog and deadline tests: 2026-10-12 12:00 UTC.
`old-unread-*` falls outside the three-day initial backlog; `read-*` is already
read. For chronological polling, sort on `date` (the set is grouped by scenario).

Message fields: `account`, `message_id`, `thread_id`, `sender`, `to`, `subject`,
`body`, timezone-aware ISO `date`, `unread`, and `direction` (`inbound` or `sent`).
Malformed samples also include `raw_mime` with a genuinely missing multipart
boundary; `malformed_mime` flags the scenario, not a substitute for parsing it.

`expected.json` maps each practice message ID to `category`, `needs_reply`, and
`language` (`en` / `ko`). Optional fields describe injection handling and the
card's state after an owner reply. A promise to send later has `needs_reply=false`
for that outgoing message, while `still_owed=true` keeps its thread open.
VIP scenario entries list `vip_addresses`; configure these only for those tests,
not as production defaults. Owner replies and deadline changes share thread IDs
with their preceding inbound messages.

The fake API is intentionally small: `FakeModel.register(key, response, tokens=10)`
and `complete_structured(schema, prompt, key=...)`, `FakeClock.now/advance/set`,
`FakeSlack.post/update/history/thread/message/click`, and
`FakeGmail.list_messages/history/get_message/sent_mail`. `FakeGmail.add_message`
is test setup for simulated arrivals; other unsupported methods fail immediately.
History returns `{messages, cursor}`; cursor loss raises `LostCursorError`.
Model `calls` counts attempts, including limits; `tokens` counts scripted usage
(success: registered amount; malformed: one; rejected limit: zero), not estimates
of real provider billing. Responses are copied to prevent cross-test mutation.

Pytest fixtures: `fixture_dir`, `practice_mail`, `expected_mail`, `sent_mail`,
`fake_clock`, `fake_model`, `fake_slack`, `fake_gmail`. Import classes from `fakes`
in tests (the root conftest exposes this package).
