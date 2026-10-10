CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('new','working','waiting_on_owner','paused','blocked','done','cancelled','failed')),
    card_revision INTEGER NOT NULL CHECK (card_revision > 0),
    blocked_reason TEXT CHECK (blocked_reason IN ('model_limit','health','permission')),
    interrupted INTEGER NOT NULL DEFAULT 0 CHECK (interrupted IN (0,1)),
    CHECK ((state = 'blocked' AND blocked_reason IS NOT NULL) OR (state != 'blocked' AND blocked_reason IS NULL))
);
CREATE TABLE cards (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    contract TEXT NOT NULL,
    PRIMARY KEY (task_id, revision)
);
CREATE TABLE check_backs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    due_at TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE questions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    text TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    asked_at TEXT NOT NULL,
    answer TEXT
);
CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    card_revision INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0,1)),
    FOREIGN KEY (task_id, card_revision) REFERENCES cards(task_id, revision)
);
CREATE TABLE mail_cards (
    id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open','replied_still_open','closed')),
    payload TEXT NOT NULL,
    UNIQUE (account, thread_id)
);
CREATE TABLE mail_items (
    account TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('urgent','regular','noise','uncertain')),
    reason TEXT NOT NULL,
    needs_reply INTEGER NOT NULL CHECK (needs_reply IN (0,1)),
    payload TEXT NOT NULL,
    PRIMARY KEY (account, message_id),
    FOREIGN KEY (account, thread_id) REFERENCES mail_cards(account, thread_id)
);
CREATE TABLE mail_cursors (
    account TEXT PRIMARY KEY,
    cursor TEXT NOT NULL
);
CREATE TABLE ideas (
    id TEXT PRIMARY KEY,
    mail_card_id TEXT NOT NULL REFERENCES mail_cards(id),
    reason TEXT NOT NULL,
    sources TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('new','accepted','dismissed','snoozed','expired')),
    task_id TEXT REFERENCES tasks(id),
    snoozed_until TEXT,
    CHECK (state != 'accepted' OR task_id IS NOT NULL),
    CHECK (state != 'snoozed' OR snoozed_until IS NOT NULL)
);
CREATE TABLE outbox (
    delivery_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','sending','sent','failed','unknown')),
    receipt TEXT,
    task_id TEXT REFERENCES tasks(id)
);
CREATE TABLE missed_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    job_id TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    UNIQUE (job_id, scheduled_at)
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    task_id TEXT REFERENCES tasks(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX outbox_state ON outbox(state);
CREATE INDEX check_backs_due ON check_backs(due_at);
