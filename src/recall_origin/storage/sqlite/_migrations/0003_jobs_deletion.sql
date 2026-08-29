CREATE TABLE outbox_messages (
    outbox_id TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL,
    event_id TEXT,
    job_type TEXT NOT NULL CHECK (length(job_type) BETWEEN 1 AND 64),
    formation_version TEXT,
    dedupe_key TEXT NOT NULL CHECK (length(dedupe_key) BETWEEN 1 AND 512),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'leased', 'retry_wait', 'done', 'dead_letter', 'cancelled')
    ),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    available_at INTEGER NOT NULL,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    leased_until INTEGER,
    cancellation_epoch INTEGER NOT NULL CHECK (cancellation_epoch >= 0),
    last_error_code TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER,
    UNIQUE (partition_id, dedupe_key),
    FOREIGN KEY (partition_id) REFERENCES partitions (partition_id) ON DELETE RESTRICT,
    FOREIGN KEY (partition_id, event_id)
        REFERENCES events (partition_id, event_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX outbox_messages_claimable
ON outbox_messages (status, available_at, partition_id, outbox_id)
WHERE status IN ('pending', 'retry_wait');

CREATE INDEX outbox_messages_lease
ON outbox_messages (status, leased_until)
WHERE status = 'leased';

CREATE TABLE deletion_requests (
    deletion_id TEXT PRIMARY KEY,
    partition_id TEXT,
    scope_key TEXT GENERATED ALWAYS AS (coalesce(partition_id, '*')) STORED
        CHECK (length(scope_key) BETWEEN 1 AND 512),
    target_type TEXT NOT NULL CHECK (
        target_type IN ('event', 'claim', 'subject', 'partition', 'managed_pack')
    ),
    target_id TEXT NOT NULL CHECK (length(target_id) BETWEEN 1 AND 512),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 512),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    cascade_policy TEXT NOT NULL CHECK (cascade_policy IN ('safe', 'purge')),
    state TEXT NOT NULL CHECK (
        state IN ('accepted', 'logically_hidden', 'completed', 'failed')
    ),
    requested_at INTEGER NOT NULL,
    logically_hidden_at INTEGER,
    completed_at INTEGER,
    tx_seq INTEGER NOT NULL,
    external_copies_json TEXT NOT NULL DEFAULT '[]',
    CHECK (logically_hidden_at IS NULL OR logically_hidden_at >= requested_at),
    CHECK (completed_at IS NULL OR completed_at >= requested_at),
    UNIQUE (scope_key, idempotency_key),
    FOREIGN KEY (partition_id) REFERENCES partitions (partition_id) ON DELETE RESTRICT,
    FOREIGN KEY (tx_seq) REFERENCES ledger_transactions (tx_seq) ON DELETE RESTRICT
) STRICT;

CREATE INDEX deletion_requests_target
ON deletion_requests (scope_key, target_type, target_id, requested_at DESC);

CREATE TABLE deletion_layers (
    deletion_id TEXT NOT NULL,
    layer TEXT NOT NULL CHECK (length(layer) BETWEEN 1 AND 64),
    state TEXT NOT NULL CHECK (
        state IN ('accepted', 'logically_hidden', 'completed', 'failed')
    ),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    last_verified_at INTEGER,
    error_code TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (deletion_id, layer),
    FOREIGN KEY (deletion_id) REFERENCES deletion_requests (deletion_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE tombstone_fences (
    scope_key TEXT NOT NULL CHECK (length(scope_key) BETWEEN 1 AND 512),
    target_type TEXT NOT NULL CHECK (
        target_type IN ('event', 'claim', 'subject', 'partition', 'managed_pack')
    ),
    target_id TEXT NOT NULL CHECK (length(target_id) BETWEEN 1 AND 512),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    deletion_id TEXT NOT NULL,
    registry_entry_id TEXT NOT NULL,
    registry_digest TEXT NOT NULL CHECK (length(registry_digest) = 64),
    recorded_at INTEGER NOT NULL,
    applied_at INTEGER NOT NULL,
    PRIMARY KEY (scope_key, target_type, target_id)
) STRICT;

CREATE INDEX tombstone_fences_generation
ON tombstone_fences (generation, scope_key, target_type);

CREATE TABLE purge_registry_checkpoint (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    database_id TEXT NOT NULL,
    applied_generation INTEGER NOT NULL CHECK (applied_generation >= 0),
    head_digest TEXT NOT NULL CHECK (length(head_digest) = 64),
    verified_at INTEGER NOT NULL
) STRICT;
