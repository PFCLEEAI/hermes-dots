CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE setting_proposals (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, value TEXT NOT NULL,
    previous TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE task_runtime (task_id TEXT PRIMARY KEY REFERENCES tasks(id), data TEXT NOT NULL);
CREATE TABLE engine_control (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE task_resources (
    id TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL CHECK (kind IN ('helper','schedule','draft')),
    state TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (kind, id)
);
CREATE TABLE question_nudges (question_id TEXT PRIMARY KEY REFERENCES questions(id));
CREATE TABLE health_status (name TEXT PRIMARY KEY, ok INTEGER NOT NULL, detail TEXT NOT NULL, checked_at TEXT NOT NULL);
CREATE TABLE ping_queue (
    id TEXT PRIMARY KEY, payload TEXT NOT NULL, release_at TEXT NOT NULL,
    task_id TEXT REFERENCES tasks(id), cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE usage_entries (id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, calls INTEGER NOT NULL, tokens INTEGER NOT NULL);
