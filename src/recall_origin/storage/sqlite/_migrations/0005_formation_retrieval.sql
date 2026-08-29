CREATE TABLE formation_runs (
    formation_id TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    outbox_id TEXT NOT NULL,
    formation_version TEXT NOT NULL CHECK (length(formation_version) BETWEEN 1 AND 128),
    cancellation_epoch INTEGER NOT NULL CHECK (cancellation_epoch >= 0),
    provider_fingerprint TEXT NOT NULL CHECK (length(provider_fingerprint) BETWEEN 1 AND 512),
    policy_hash TEXT NOT NULL CHECK (length(policy_hash) = 64),
    status TEXT NOT NULL CHECK (
        status IN ('running', 'completed', 'failed', 'cancelled')
    ),
    started_at INTEGER NOT NULL,
    completed_at INTEGER,
    error_code TEXT,
    UNIQUE (partition_id, event_id, formation_version, provider_fingerprint),
    UNIQUE (partition_id, formation_id),
    FOREIGN KEY (partition_id, event_id)
        REFERENCES events (partition_id, event_id) ON DELETE RESTRICT,
    FOREIGN KEY (outbox_id)
        REFERENCES outbox_messages (outbox_id) ON DELETE RESTRICT
) STRICT;

CREATE TABLE derived_candidates (
    candidate_id TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL,
    formation_id TEXT NOT NULL,
    candidate_fingerprint TEXT NOT NULL CHECK (length(candidate_fingerprint) = 64),
    operation TEXT NOT NULL CHECK (
        operation IN ('add', 'reinforce', 'supersede', 'conflict', 'ignore', 'quarantine')
    ),
    kind TEXT NOT NULL CHECK (kind IN ('semantic', 'episodic', 'procedural')),
    subtype TEXT NOT NULL CHECK (
        subtype IN ('fact', 'preference', 'outcome', 'decision', 'workflow', 'gotcha')
    ),
    memory_key TEXT,
    content TEXT NOT NULL,
    valid_from INTEGER,
    valid_to INTEGER,
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 4000),
    status TEXT NOT NULL CHECK (
        status IN ('proposed', 'committed', 'ignored', 'quarantined', 'rejected')
    ),
    claim_id TEXT,
    revision_id TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (formation_id, candidate_fingerprint),
    FOREIGN KEY (partition_id, formation_id)
        REFERENCES formation_runs (partition_id, formation_id) ON DELETE RESTRICT,
    FOREIGN KEY (partition_id, claim_id)
        REFERENCES memory_claims (partition_id, claim_id) ON DELETE RESTRICT,
    FOREIGN KEY (partition_id, revision_id)
        REFERENCES memory_revisions (partition_id, revision_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX derived_candidates_formation
ON derived_candidates (partition_id, formation_id, status, candidate_id);

CREATE TABLE memory_feedback (
    feedback_id TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    actor_principal_type TEXT NOT NULL CHECK (
        actor_principal_type IN ('human', 'agent', 'service')
    ),
    actor_principal_id TEXT NOT NULL,
    feedback_type TEXT NOT NULL CHECK (
        feedback_type IN ('helpful', 'not_helpful', 'incorrect', 'stale', 'proposal')
    ),
    reason TEXT,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (partition_id, claim_id)
        REFERENCES memory_claims (partition_id, claim_id) ON DELETE RESTRICT,
    FOREIGN KEY (partition_id, revision_id)
        REFERENCES memory_revisions (partition_id, revision_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX memory_feedback_claim
ON memory_feedback (partition_id, claim_id, created_at DESC);

CREATE TABLE retrieval_runs (
    retrieval_id TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL,
    keyed_query_hash TEXT NOT NULL CHECK (length(keyed_query_hash) = 64),
    ranking_policy_version INTEGER NOT NULL CHECK (ranking_policy_version >= 1),
    selected_ids_json TEXT NOT NULL,
    token_budget INTEGER,
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
    selected_count INTEGER NOT NULL CHECK (selected_count >= 0),
    degraded INTEGER NOT NULL CHECK (degraded IN (0, 1)),
    duration_micros INTEGER NOT NULL CHECK (duration_micros >= 0),
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    FOREIGN KEY (partition_id) REFERENCES partitions (partition_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX retrieval_runs_expiry
ON retrieval_runs (expires_at, partition_id);
