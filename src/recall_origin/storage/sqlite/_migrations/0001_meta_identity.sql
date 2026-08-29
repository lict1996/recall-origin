CREATE TABLE storage_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;

CREATE TABLE partitions (
    partition_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    namespace_kind TEXT NOT NULL CHECK (
        namespace_kind IN ('workspace', 'user', 'agent_private', 'session_private')
    ),
    namespace_id TEXT NOT NULL CHECK (length(namespace_id) BETWEEN 1 AND 256),
    cancellation_epoch INTEGER NOT NULL DEFAULT 0 CHECK (cancellation_epoch >= 0),
    created_at INTEGER NOT NULL,
    UNIQUE (tenant_id, namespace_kind, namespace_id),
    UNIQUE (partition_id, tenant_id)
) STRICT;

CREATE TABLE partition_grants (
    grant_id TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL,
    principal_type TEXT NOT NULL CHECK (principal_type IN ('human', 'agent', 'service')),
    principal_id TEXT NOT NULL CHECK (length(principal_id) BETWEEN 1 AND 256),
    capability TEXT NOT NULL CHECK (
        capability IN ('read', 'write', 'govern', 'delete', 'export', 'stats', 'admin')
    ),
    granted_at INTEGER NOT NULL,
    revoked_at INTEGER,
    CHECK (revoked_at IS NULL OR revoked_at >= granted_at),
    FOREIGN KEY (partition_id) REFERENCES partitions (partition_id) ON DELETE RESTRICT
) STRICT;

CREATE UNIQUE INDEX partition_grants_active_identity
ON partition_grants (partition_id, principal_type, principal_id, capability)
WHERE revoked_at IS NULL;

CREATE INDEX partition_grants_principal_lookup
ON partition_grants (principal_type, principal_id, capability, partition_id)
WHERE revoked_at IS NULL;

CREATE TABLE operation_idempotency (
    scope_key TEXT NOT NULL CHECK (length(scope_key) BETWEEN 1 AND 512),
    operation TEXT NOT NULL CHECK (length(operation) BETWEEN 1 AND 64),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 512),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    resource_type TEXT NOT NULL CHECK (length(resource_type) BETWEEN 1 AND 64),
    resource_id TEXT NOT NULL CHECK (length(resource_id) BETWEEN 1 AND 256),
    receipt_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (scope_key, operation, idempotency_key)
) STRICT;
