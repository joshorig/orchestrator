CREATE TABLE IF NOT EXISTS environment_check_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    ts_epoch        INTEGER NOT NULL,
    project         TEXT,
    result          TEXT NOT NULL,
    blocker_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_env_check_project_ts ON environment_check_log(project, ts_epoch DESC);

CREATE TABLE IF NOT EXISTS orphan_recovery_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    ts_epoch    INTEGER NOT NULL,
    task_id     TEXT NOT NULL,
    from_state  TEXT NOT NULL,
    age_seconds INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orphan_recovery_task_ts ON orphan_recovery_log(task_id, ts_epoch DESC);

CREATE TABLE IF NOT EXISTS task_bypass_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    ts_epoch  INTEGER NOT NULL,
    task_id   TEXT NOT NULL,
    gate      TEXT NOT NULL,
    reason    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_bypass_task_ts ON task_bypass_log(task_id, ts_epoch DESC);

CREATE TABLE IF NOT EXISTS task_costs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    ts_epoch   INTEGER NOT NULL,
    task_id    TEXT NOT NULL,
    engine     TEXT NOT NULL,
    model      TEXT,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_tokens   INTEGER NOT NULL DEFAULT 0,
    search_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd   REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_task_costs_task_ts ON task_costs(task_id, ts_epoch DESC);

CREATE VIEW IF NOT EXISTS meta_harness_view AS
SELECT
    t.task_id,
    t.state,
    t.engine,
    t.role,
    t.project,
    t.feature_id,
    t.attempt,
    t.blocker_code,
    t.summary,
    t.created_at_epoch,
    t.finished_at_epoch
FROM tasks AS t;
