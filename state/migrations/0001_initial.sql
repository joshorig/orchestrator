CREATE TABLE IF NOT EXISTS tasks (
    task_id             TEXT PRIMARY KEY,
    state               TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    created_at_epoch    INTEGER NOT NULL,
    state_updated_at    TEXT NOT NULL,
    engine              TEXT NOT NULL,
    role                TEXT NOT NULL,
    project             TEXT,
    feature_id          TEXT,
    parent_task_id      TEXT,
    braid_template      TEXT,
    braid_template_hash TEXT,
    summary             TEXT NOT NULL,
    claimed_at          TEXT,
    claimed_pid         INTEGER,
    claimed_slot        TEXT,
    started_at          TEXT,
    finished_at         TEXT,
    finished_at_epoch   INTEGER,
    attempt             INTEGER NOT NULL DEFAULT 1,
    blocker_code        TEXT,
    blocker_summary     TEXT,
    blocker_detail      TEXT,
    blocker_retryable   INTEGER,
    blocker_updated_at  TEXT,
    metadata_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_state_project ON tasks(state, project);
CREATE INDEX IF NOT EXISTS idx_tasks_feature ON tasks(feature_id);
CREATE INDEX IF NOT EXISTS idx_tasks_engine ON tasks(engine);
CREATE INDEX IF NOT EXISTS idx_tasks_blocker ON tasks(blocker_code);
CREATE INDEX IF NOT EXISTS idx_tasks_claimed_pid ON tasks(claimed_pid);
CREATE INDEX IF NOT EXISTS idx_tasks_created_epoch ON tasks(created_at_epoch);

CREATE TABLE IF NOT EXISTS task_transitions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          TEXT NOT NULL,
    from_state       TEXT NOT NULL,
    to_state         TEXT NOT NULL,
    reason           TEXT,
    created_at       TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trans_task ON task_transitions(task_id);
CREATE INDEX IF NOT EXISTS idx_trans_epoch ON task_transitions(created_at_epoch);

CREATE TABLE IF NOT EXISTS features (
    feature_id          TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    created_at_epoch    INTEGER NOT NULL,
    status              TEXT NOT NULL,
    project             TEXT,
    pr_body_path        TEXT,
    pr_number           INTEGER,
    pr_url              TEXT,
    pr_final_state      TEXT,
    frontier_task_id    TEXT,
    frontier_state      TEXT,
    frontier_updated_at TEXT,
    self_repair_status  TEXT,
    version             INTEGER NOT NULL DEFAULT 1,
    metadata_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_features_status ON features(status);
CREATE INDEX IF NOT EXISTS idx_features_project ON features(project);
CREATE INDEX IF NOT EXISTS idx_features_frontier ON features(frontier_task_id);

CREATE TABLE IF NOT EXISTS feature_children (
    feature_id TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    role       TEXT,
    order_idx  INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (feature_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_fc_feature ON feature_children(feature_id);
CREATE INDEX IF NOT EXISTS idx_fc_task ON feature_children(task_id);

CREATE TABLE IF NOT EXISTS self_repair_issues (
    issue_id             TEXT PRIMARY KEY,
    feature_id           TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    created_at_epoch     INTEGER NOT NULL,
    status               TEXT NOT NULL,
    last_deliberation_at TEXT,
    planner_task_id      TEXT,
    attempts             INTEGER NOT NULL DEFAULT 0,
    max_attempts         INTEGER NOT NULL DEFAULT 3,
    blocker_code         TEXT,
    blocker_summary      TEXT,
    superseded_task_ids  TEXT NOT NULL DEFAULT '[]',
    metadata_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sri_feature ON self_repair_issues(feature_id);
CREATE INDEX IF NOT EXISTS idx_sri_status ON self_repair_issues(status);
CREATE INDEX IF NOT EXISTS idx_sri_planner ON self_repair_issues(planner_task_id);

CREATE TABLE IF NOT EXISTS self_repair_deliberations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id         TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    stage            TEXT,
    verdict          TEXT,
    panel            TEXT,
    summary          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delib_issue ON self_repair_deliberations(issue_id);
CREATE INDEX IF NOT EXISTS idx_delib_epoch ON self_repair_deliberations(created_at_epoch);

CREATE TABLE IF NOT EXISTS blocker_codes (
    code          TEXT PRIMARY KEY,
    name          TEXT,
    severity      TEXT,
    retryable     INTEGER,
    category      TEXT,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS metrics (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    value            REAL NOT NULL,
    metric_type      TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    tags_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics(name, created_at_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_epoch ON metrics(created_at_epoch);

CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    payload_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_epoch ON events(created_at_epoch);

CREATE TABLE IF NOT EXISTS artifacts (
    task_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    file_path  TEXT NOT NULL,
    sha256     TEXT,
    size_bytes INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, kind)
);
