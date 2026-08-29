CREATE TABLE managed_packs (
    pack_id TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL,
    retrieval_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'tombstoned', 'purged')),
    root_path TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (partition_id, pack_id),
    FOREIGN KEY (partition_id) REFERENCES partitions (partition_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX managed_packs_expiry
ON managed_packs (status, expires_at, partition_id);

CREATE TABLE managed_pack_claims (
    pack_id TEXT NOT NULL,
    partition_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    PRIMARY KEY (pack_id, claim_id),
    FOREIGN KEY (pack_id) REFERENCES managed_packs (pack_id) ON DELETE CASCADE,
    FOREIGN KEY (partition_id, claim_id)
        REFERENCES memory_claims (partition_id, claim_id) ON DELETE RESTRICT,
    FOREIGN KEY (partition_id, revision_id)
        REFERENCES memory_revisions (partition_id, revision_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX managed_pack_claims_claim
ON managed_pack_claims (partition_id, claim_id, pack_id);

CREATE TABLE managed_pack_evidence (
    pack_id TEXT NOT NULL,
    partition_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    PRIMARY KEY (pack_id, evidence_id),
    FOREIGN KEY (pack_id) REFERENCES managed_packs (pack_id) ON DELETE CASCADE,
    FOREIGN KEY (partition_id, evidence_id)
        REFERENCES evidence_artifacts (partition_id, evidence_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX managed_pack_evidence_source
ON managed_pack_evidence (partition_id, evidence_id, pack_id);
