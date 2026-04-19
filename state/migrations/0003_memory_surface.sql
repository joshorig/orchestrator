CREATE TABLE IF NOT EXISTS memory_observations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project             TEXT NOT NULL,
    source_doc          TEXT NOT NULL,
    section_key         TEXT NOT NULL,
    type                TEXT NOT NULL,
    title               TEXT NOT NULL,
    content             TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    created_at_epoch    INTEGER NOT NULL,
    last_accessed_epoch INTEGER,
    access_count        INTEGER NOT NULL DEFAULT 0,
    importance          INTEGER NOT NULL DEFAULT 5,
    tags_json           TEXT NOT NULL DEFAULT '[]',
    metadata_json       TEXT NOT NULL DEFAULT '{}',
    UNIQUE(project, source_doc, section_key)
);
CREATE INDEX IF NOT EXISTS idx_memory_obs_project_type ON memory_observations(project, type);
CREATE INDEX IF NOT EXISTS idx_memory_obs_project_created ON memory_observations(project, created_at_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_memory_obs_source ON memory_observations(project, source_doc);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_obs_fts USING fts5(
    title,
    content,
    tags,
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS memory_obs_ai AFTER INSERT ON memory_observations BEGIN
    INSERT INTO memory_obs_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags_json);
END;

CREATE TRIGGER IF NOT EXISTS memory_obs_ad AFTER DELETE ON memory_observations BEGIN
    DELETE FROM memory_obs_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS memory_obs_au AFTER UPDATE ON memory_observations BEGIN
    DELETE FROM memory_obs_fts WHERE rowid = old.id;
    INSERT INTO memory_obs_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags_json);
END;
