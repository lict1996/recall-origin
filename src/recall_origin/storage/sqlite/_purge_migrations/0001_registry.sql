CREATE TABLE registry_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    database_id TEXT NOT NULL UNIQUE,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    head_digest TEXT NOT NULL CHECK (length(head_digest) = 64),
    key_fingerprint TEXT NOT NULL CHECK (length(key_fingerprint) = 64),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    state_signature TEXT NOT NULL CHECK (length(state_signature) = 64)
) STRICT;

CREATE TABLE purge_tombstones (
    entry_id TEXT PRIMARY KEY,
    database_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    scope_key TEXT NOT NULL CHECK (length(scope_key) BETWEEN 1 AND 512),
    target_type TEXT NOT NULL CHECK (
        target_type IN ('event', 'claim', 'subject', 'partition', 'managed_pack')
    ),
    target_id TEXT NOT NULL CHECK (length(target_id) BETWEEN 1 AND 512),
    deletion_id TEXT NOT NULL CHECK (length(deletion_id) BETWEEN 1 AND 256),
    recorded_at INTEGER NOT NULL,
    previous_digest TEXT NOT NULL CHECK (length(previous_digest) = 64),
    entry_digest TEXT NOT NULL CHECK (length(entry_digest) = 64),
    signature TEXT NOT NULL CHECK (length(signature) = 64),
    UNIQUE (database_id, generation),
    UNIQUE (database_id, scope_key, target_type, target_id, deletion_id),
    FOREIGN KEY (database_id) REFERENCES registry_state (database_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX purge_tombstones_target
ON purge_tombstones (database_id, scope_key, target_type, target_id, generation);

CREATE TRIGGER purge_tombstones_no_update
BEFORE UPDATE ON purge_tombstones
BEGIN
    SELECT RAISE(ABORT, 'purge tombstones are append-only');
END;

CREATE TRIGGER purge_tombstones_no_delete
BEFORE DELETE ON purge_tombstones
BEGIN
    SELECT RAISE(ABORT, 'purge tombstones are append-only');
END;
