CREATE TABLE IF NOT EXISTS token_savior_events (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                         TEXT NOT NULL,
    ts_epoch                   INTEGER NOT NULL,
    task_id                    TEXT,
    feature_id                 TEXT,
    project                    TEXT,
    role                       TEXT,
    status                     TEXT NOT NULL,
    query_present              INTEGER NOT NULL DEFAULT 0,
    sections_json              TEXT NOT NULL DEFAULT '[]',
    rows_found                 INTEGER NOT NULL DEFAULT 0,
    context_chars              INTEGER NOT NULL DEFAULT 0,
    estimated_context_tokens   INTEGER NOT NULL DEFAULT 0,
    estimated_full_read_chars  INTEGER NOT NULL DEFAULT 0,
    estimated_tokens_saved     INTEGER NOT NULL DEFAULT 0,
    native_total_calls         INTEGER NOT NULL DEFAULT 0,
    native_tokens_used         INTEGER NOT NULL DEFAULT 0,
    native_tokens_naive        INTEGER NOT NULL DEFAULT 0,
    native_tokens_saved        INTEGER NOT NULL DEFAULT 0,
    payload_json               TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_token_savior_events_ts ON token_savior_events(ts_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_token_savior_events_project_ts ON token_savior_events(project, ts_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_token_savior_events_task ON token_savior_events(task_id);
CREATE INDEX IF NOT EXISTS idx_token_savior_events_status ON token_savior_events(status);
