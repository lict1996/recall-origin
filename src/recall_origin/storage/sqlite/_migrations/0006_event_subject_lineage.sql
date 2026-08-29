CREATE TABLE event_subjects (
    partition_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    subject_type TEXT NOT NULL CHECK (length(subject_type) BETWEEN 1 AND 64),
    subject_id TEXT NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 256),
    created_at INTEGER NOT NULL,
    PRIMARY KEY (partition_id, event_id, subject_type, subject_id),
    FOREIGN KEY (partition_id, event_id)
        REFERENCES events (partition_id, event_id) ON DELETE RESTRICT,
    FOREIGN KEY (partition_id, subject_type, subject_id)
        REFERENCES subjects (partition_id, subject_type, subject_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX event_subjects_subject
ON event_subjects (partition_id, subject_id, subject_type, event_id);

-- Existing formed memories already have authoritative subject/evidence
-- lineage. Backfill every event whose evidence contributed to such a claim.
INSERT OR IGNORE INTO event_subjects(
    partition_id, event_id, subject_type, subject_id, created_at
)
SELECT DISTINCT
    ea.partition_id,
    ea.event_id,
    s.subject_type,
    s.subject_id,
    e.recorded_at
FROM claim_subjects AS cs
JOIN subjects AS s
  ON s.partition_id = cs.partition_id
 AND s.subject_row_id = cs.subject_row_id
JOIN memory_revisions AS r
  ON r.partition_id = cs.partition_id
 AND r.claim_id = cs.claim_id
JOIN revision_evidence AS re
  ON re.partition_id = r.partition_id
 AND re.revision_id = r.revision_id
JOIN evidence_artifacts AS ea
  ON ea.partition_id = re.partition_id
 AND ea.evidence_id = re.evidence_id
JOIN events AS e
  ON e.partition_id = ea.partition_id
 AND e.event_id = ea.event_id
WHERE ea.event_id IS NOT NULL;

-- Pending automatic captures have not formed a claim yet. Their strict
-- CaptureRequest JSON is the only pre-v6 subject lineage available.
INSERT OR IGNORE INTO subjects(
    subject_row_id, partition_id, subject_type, subject_id, created_at
)
SELECT
    'sub_migrated_' || lower(hex(randomblob(16))),
    e.partition_id,
    json_extract(subject.value, '$.subject_type'),
    json_extract(subject.value, '$.subject_id'),
    e.recorded_at
FROM events AS e
JOIN event_payloads AS ep ON ep.event_id = e.event_id
JOIN json_each(ep.content, '$.subjects') AS subject
WHERE e.formation_mode = 'automatic'
  AND json_valid(ep.content)
  AND json_type(subject.value, '$.subject_type') = 'text'
  AND json_type(subject.value, '$.subject_id') = 'text'
  AND length(json_extract(subject.value, '$.subject_type')) BETWEEN 1 AND 64
  AND length(json_extract(subject.value, '$.subject_id')) BETWEEN 1 AND 256;

INSERT OR IGNORE INTO event_subjects(
    partition_id, event_id, subject_type, subject_id, created_at
)
SELECT
    e.partition_id,
    e.event_id,
    json_extract(subject.value, '$.subject_type'),
    json_extract(subject.value, '$.subject_id'),
    e.recorded_at
FROM events AS e
JOIN event_payloads AS ep ON ep.event_id = e.event_id
JOIN json_each(ep.content, '$.subjects') AS subject
WHERE e.formation_mode = 'automatic'
  AND json_valid(ep.content)
  AND json_type(subject.value, '$.subject_type') = 'text'
  AND json_type(subject.value, '$.subject_id') = 'text';
