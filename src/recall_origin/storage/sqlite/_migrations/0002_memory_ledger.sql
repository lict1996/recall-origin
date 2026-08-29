CREATE TABLE ledger_transactions (
    tx_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id TEXT NOT NULL UNIQUE,
    operation TEXT NOT NULL CHECK (length(operation) BETWEEN 1 AND 64),
    committed_at INTEGER NOT NULL
) STRICT;

CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL,
    external_event_id TEXT NOT NULL CHECK (length(external_event_id) BETWEEN 1 AND 512),
    idempotency_key TEXT CHECK (
        idempotency_key IS NULL OR length(idempotency_key) BETWEEN 1 AND 512
    ),
    formation_mode TEXT NOT NULL CHECK (formation_mode IN ('explicit', 'automatic')),
    event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 64),
    origin_type TEXT NOT NULL CHECK (
        origin_type IN ('explicit_user', 'agent_claim', 'tool_result', 'model_derived')
    ),
    session_id TEXT CHECK (session_id IS NULL OR length(session_id) <= 256),
    host_agent_id TEXT CHECK (host_agent_id IS NULL OR length(host_agent_id) <= 256),
    producer_id TEXT NOT NULL CHECK (length(producer_id) BETWEEN 1 AND 256),
    request_id TEXT CHECK (request_id IS NULL OR length(request_id) <= 256),
    tx_seq INTEGER NOT NULL,
    occurred_at INTEGER,
    recorded_at INTEGER NOT NULL,
    UNIQUE (partition_id, producer_id, external_event_id),
    UNIQUE (partition_id, event_id),
    FOREIGN KEY (partition_id) REFERENCES partitions (partition_id) ON DELETE RESTRICT,
    FOREIGN KEY (tx_seq) REFERENCES ledger_transactions (tx_seq) ON DELETE RESTRICT
) STRICT;

CREATE UNIQUE INDEX events_idempotency
ON events (partition_id, producer_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX events_partition_recorded
ON events (partition_id, recorded_at DESC, event_id);

CREATE TABLE event_payloads (
    event_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    stored_at INTEGER NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events (event_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE evidence_artifacts (
    evidence_id TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL,
    event_id TEXT,
    evidence_type TEXT NOT NULL CHECK (length(evidence_type) BETWEEN 1 AND 64),
    uri TEXT,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    availability TEXT NOT NULL DEFAULT 'available' CHECK (
        availability IN ('available', 'deleted', 'expired')
    ),
    tx_seq INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (partition_id, evidence_id),
    FOREIGN KEY (partition_id) REFERENCES partitions (partition_id) ON DELETE RESTRICT,
    FOREIGN KEY (partition_id, event_id)
        REFERENCES events (partition_id, event_id) ON DELETE RESTRICT,
    FOREIGN KEY (tx_seq) REFERENCES ledger_transactions (tx_seq) ON DELETE RESTRICT
) STRICT;

CREATE INDEX evidence_artifacts_event
ON evidence_artifacts (partition_id, event_id, availability);

CREATE TABLE evidence_bodies (
    evidence_id TEXT PRIMARY KEY,
    body TEXT,
    excerpt TEXT,
    stored_at INTEGER NOT NULL,
    CHECK (body IS NOT NULL OR excerpt IS NOT NULL),
    FOREIGN KEY (evidence_id) REFERENCES evidence_artifacts (evidence_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE memory_claims (
    claim_id TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL,
    memory_key TEXT CHECK (memory_key IS NULL OR length(memory_key) <= 512),
    kind TEXT NOT NULL CHECK (kind IN ('semantic', 'episodic', 'procedural')),
    subtype TEXT NOT NULL CHECK (
        subtype IN ('fact', 'preference', 'outcome', 'decision', 'workflow', 'gotcha')
    ),
    valid_from INTEGER,
    valid_to INTEGER,
    origin_type TEXT NOT NULL CHECK (
        origin_type IN ('explicit_user', 'agent_claim', 'tool_result', 'model_derived')
    ),
    created_by_event_id TEXT,
    tx_from_seq INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from),
    UNIQUE (partition_id, claim_id),
    FOREIGN KEY (partition_id) REFERENCES partitions (partition_id) ON DELETE RESTRICT,
    FOREIGN KEY (partition_id, created_by_event_id)
        REFERENCES events (partition_id, event_id) ON DELETE RESTRICT,
    FOREIGN KEY (tx_from_seq) REFERENCES ledger_transactions (tx_seq) ON DELETE RESTRICT
) STRICT;

CREATE INDEX memory_claims_partition_key
ON memory_claims (partition_id, memory_key, created_at DESC);

CREATE INDEX memory_claims_validity
ON memory_claims (partition_id, valid_from, valid_to);

CREATE TABLE claim_contents (
    claim_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    normalized_content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    stored_at INTEGER NOT NULL,
    FOREIGN KEY (claim_id) REFERENCES memory_claims (claim_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE memory_revisions (
    revision_id TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    previous_revision_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'candidate', 'active', 'conflicted', 'quarantined', 'superseded',
            'unsupported', 'withheld', 'rebuilding', 'rejected', 'tombstoned'
        )
    ),
    confirmation TEXT NOT NULL CHECK (
        confirmation IN ('unverified', 'user_confirmed', 'source_verified', 'test_verified')
    ),
    source_count INTEGER NOT NULL CHECK (source_count >= 0),
    evidence_set_hash TEXT NOT NULL CHECK (length(evidence_set_hash) = 64),
    reason TEXT CHECK (reason IS NULL OR length(reason) <= 4000),
    tx_from_seq INTEGER NOT NULL,
    revision_time INTEGER NOT NULL,
    actor_principal_type TEXT CHECK (
        actor_principal_type IS NULL OR actor_principal_type IN ('human', 'agent', 'service')
    ),
    actor_principal_id TEXT CHECK (
        actor_principal_id IS NULL OR length(actor_principal_id) BETWEEN 1 AND 256
    ),
    governance_action TEXT CHECK (
        governance_action IS NULL
        OR governance_action IN (
            'confirm', 'reject', 'quarantine', 'activate', 'forget', 'forget_event'
        )
    ),
    CHECK (
        (actor_principal_type IS NULL AND actor_principal_id IS NULL)
        OR (actor_principal_type IS NOT NULL AND actor_principal_id IS NOT NULL)
    ),
    UNIQUE (partition_id, revision_id),
    UNIQUE (partition_id, claim_id, revision_id),
    FOREIGN KEY (partition_id, claim_id)
        REFERENCES memory_claims (partition_id, claim_id) ON DELETE RESTRICT,
    FOREIGN KEY (partition_id, claim_id, previous_revision_id)
        REFERENCES memory_revisions (partition_id, claim_id, revision_id) ON DELETE RESTRICT,
    FOREIGN KEY (tx_from_seq) REFERENCES ledger_transactions (tx_seq) ON DELETE RESTRICT
) STRICT;

CREATE INDEX memory_revisions_history
ON memory_revisions (partition_id, claim_id, tx_from_seq DESC, revision_id);

CREATE TABLE claim_heads (
    claim_id TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL,
    current_revision_id TEXT NOT NULL UNIQUE,
    head_version INTEGER NOT NULL DEFAULT 1 CHECK (head_version >= 1),
    updated_at INTEGER NOT NULL,
    UNIQUE (partition_id, claim_id),
    FOREIGN KEY (partition_id, claim_id)
        REFERENCES memory_claims (partition_id, claim_id) ON DELETE RESTRICT,
    FOREIGN KEY (partition_id, claim_id, current_revision_id)
        REFERENCES memory_revisions (partition_id, claim_id, revision_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX claim_heads_partition
ON claim_heads (partition_id, current_revision_id);

CREATE TABLE revision_evidence (
    partition_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    relation_type TEXT NOT NULL DEFAULT 'supports' CHECK (
        relation_type IN ('supports', 'contradicts', 'context')
    ),
    PRIMARY KEY (revision_id, evidence_id),
    UNIQUE (revision_id, ordinal),
    FOREIGN KEY (partition_id, revision_id)
        REFERENCES memory_revisions (partition_id, revision_id) ON DELETE RESTRICT,
    FOREIGN KEY (partition_id, evidence_id)
        REFERENCES evidence_artifacts (partition_id, evidence_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX revision_evidence_evidence
ON revision_evidence (partition_id, evidence_id, revision_id);

CREATE TABLE subjects (
    subject_row_id TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL,
    subject_type TEXT NOT NULL CHECK (length(subject_type) BETWEEN 1 AND 64),
    subject_id TEXT NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 256),
    created_at INTEGER NOT NULL,
    UNIQUE (partition_id, subject_type, subject_id),
    UNIQUE (partition_id, subject_row_id),
    FOREIGN KEY (partition_id) REFERENCES partitions (partition_id) ON DELETE RESTRICT
) STRICT;

CREATE TABLE claim_subjects (
    partition_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    subject_row_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'about' CHECK (length(role) BETWEEN 1 AND 64),
    created_at INTEGER NOT NULL,
    PRIMARY KEY (partition_id, claim_id, subject_row_id, role),
    FOREIGN KEY (partition_id, claim_id)
        REFERENCES memory_claims (partition_id, claim_id) ON DELETE RESTRICT,
    FOREIGN KEY (partition_id, subject_row_id)
        REFERENCES subjects (partition_id, subject_row_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX claim_subjects_subject
ON claim_subjects (partition_id, subject_row_id, claim_id);

CREATE TABLE memory_relations (
    relation_id TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL,
    source_claim_id TEXT NOT NULL,
    target_claim_id TEXT NOT NULL,
    relation_type TEXT NOT NULL CHECK (
        relation_type IN (
            'supersedes', 'conflicts_with', 'supports', 'derived_from', 'related_to'
        )
    ),
    tx_seq INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    CHECK (source_claim_id <> target_claim_id),
    UNIQUE (partition_id, source_claim_id, target_claim_id, relation_type),
    FOREIGN KEY (partition_id, source_claim_id)
        REFERENCES memory_claims (partition_id, claim_id) ON DELETE RESTRICT,
    FOREIGN KEY (partition_id, target_claim_id)
        REFERENCES memory_claims (partition_id, claim_id) ON DELETE RESTRICT,
    FOREIGN KEY (tx_seq) REFERENCES ledger_transactions (tx_seq) ON DELETE RESTRICT
) STRICT;

CREATE INDEX memory_relations_target
ON memory_relations (partition_id, target_claim_id, relation_type);

CREATE TRIGGER event_payloads_no_update
BEFORE UPDATE ON event_payloads
BEGIN
    SELECT RAISE(ABORT, 'event payloads are immutable; delete only for purge');
END;

CREATE TRIGGER evidence_bodies_no_update
BEFORE UPDATE ON evidence_bodies
BEGIN
    SELECT RAISE(ABORT, 'evidence bodies are immutable; delete only for purge');
END;

CREATE TRIGGER claim_contents_no_update
BEFORE UPDATE ON claim_contents
BEGIN
    SELECT RAISE(ABORT, 'claim contents are immutable; create a new claim');
END;

CREATE TRIGGER events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are immutable');
END;

CREATE TRIGGER events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'event metadata cannot be deleted');
END;

CREATE TRIGGER evidence_artifacts_lineage_immutable
BEFORE UPDATE ON evidence_artifacts
WHEN NEW.evidence_id IS NOT OLD.evidence_id
  OR NEW.partition_id IS NOT OLD.partition_id
  OR NEW.event_id IS NOT OLD.event_id
  OR NEW.evidence_type IS NOT OLD.evidence_type
  OR NEW.uri IS NOT OLD.uri
  OR NEW.content_sha256 IS NOT OLD.content_sha256
  OR NEW.tx_seq IS NOT OLD.tx_seq
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'evidence lineage is immutable');
END;

CREATE TRIGGER evidence_artifacts_no_resurrection
BEFORE UPDATE OF availability ON evidence_artifacts
WHEN OLD.availability IN ('deleted', 'expired') AND NEW.availability = 'available'
BEGIN
    SELECT RAISE(ABORT, 'deleted or expired evidence cannot be resurrected');
END;

CREATE TRIGGER evidence_artifacts_no_delete
BEFORE DELETE ON evidence_artifacts
BEGIN
    SELECT RAISE(ABORT, 'evidence metadata cannot be deleted');
END;

CREATE TRIGGER memory_claims_no_update
BEFORE UPDATE ON memory_claims
BEGIN
    SELECT RAISE(ABORT, 'memory claims are immutable; create a new claim');
END;

CREATE TRIGGER memory_claims_no_delete
BEFORE DELETE ON memory_claims
BEGIN
    SELECT RAISE(ABORT, 'memory claim metadata cannot be deleted');
END;

CREATE TRIGGER memory_revisions_no_update
BEFORE UPDATE ON memory_revisions
BEGIN
    SELECT RAISE(ABORT, 'memory revisions are immutable; create a new revision');
END;

CREATE TRIGGER memory_revisions_no_delete
BEFORE DELETE ON memory_revisions
BEGIN
    SELECT RAISE(ABORT, 'memory revision metadata cannot be deleted');
END;

CREATE TRIGGER revision_evidence_no_update
BEFORE UPDATE ON revision_evidence
BEGIN
    SELECT RAISE(ABORT, 'revision evidence sets are immutable; create a new revision');
END;

CREATE TRIGGER revision_evidence_no_delete
BEFORE DELETE ON revision_evidence
BEGIN
    SELECT RAISE(ABORT, 'revision evidence lineage cannot be deleted');
END;

CREATE TRIGGER ledger_transactions_no_update
BEFORE UPDATE ON ledger_transactions
BEGIN
    SELECT RAISE(ABORT, 'ledger transactions are immutable');
END;

CREATE TRIGGER ledger_transactions_no_delete
BEFORE DELETE ON ledger_transactions
BEGIN
    SELECT RAISE(ABORT, 'ledger transactions are immutable');
END;
