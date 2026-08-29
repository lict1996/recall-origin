CREATE VIRTUAL TABLE claim_fts USING fts5(
    claim_id UNINDEXED,
    revision_id UNINDEXED,
    partition_id UNINDEXED,
    content,
    memory_key,
    subject_text,
    tokenize = 'unicode61 remove_diacritics 2',
    prefix = '2 3 4'
);

CREATE VIEW current_claim_records AS
SELECT
    c.claim_id,
    c.partition_id,
    h.current_revision_id AS revision_id,
    h.head_version,
    c.memory_key,
    c.kind,
    c.subtype,
    cc.content,
    cc.normalized_content,
    cc.content_sha256,
    r.status,
    r.confirmation,
    r.source_count,
    r.evidence_set_hash,
    c.valid_from,
    c.valid_to,
    c.tx_from_seq AS claim_tx_from_seq,
    r.tx_from_seq AS revision_tx_from_seq,
    r.revision_time,
    c.created_at
FROM memory_claims AS c
JOIN claim_heads AS h
    ON h.partition_id = c.partition_id
   AND h.claim_id = c.claim_id
JOIN memory_revisions AS r
    ON r.partition_id = h.partition_id
   AND r.claim_id = h.claim_id
   AND r.revision_id = h.current_revision_id
JOIN claim_contents AS cc
    ON cc.claim_id = c.claim_id;

CREATE VIEW revision_history AS
SELECT
    r.*,
    (
        SELECT MIN(next_revision.tx_from_seq)
        FROM memory_revisions AS next_revision
        WHERE next_revision.partition_id = r.partition_id
          AND next_revision.claim_id = r.claim_id
          AND next_revision.tx_from_seq > r.tx_from_seq
    ) AS tx_to_seq
FROM memory_revisions AS r;
