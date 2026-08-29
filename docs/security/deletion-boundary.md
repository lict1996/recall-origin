# Deletion boundary

RecallOrigin separates immediate logical invisibility from physical
engine-managed purge and from copies it cannot control. A deletion is not
reported as complete merely because search stopped returning the target.

## Lifecycle

### 1. Commit the anti-resurrection record

`forget` first appends an authenticated entry to the independent purge
registry. Only then does it commit the main-database deletion request,
tombstone fence, cancellation state, and logical changes. If the process fails
between those commits, opening the store merges the sidecar entry and applies
the missing main-database fence.

On open, the authenticated sidecar is verified before `apply_fences` runs in
one main-database `BEGIN IMMEDIATE` transaction. If a tombstone's
`deletion_id` has no matching request and its referenced partition exists,
recovery reconstructs the minimum lifecycle needed to continue:

- the original deletion ID, target, partition, and registry timestamp are
  preserved;
- the idempotency key and request hash are derived from the registry entry,
  and `cascade_policy` is conservatively set to `safe`;
- `logical_visibility` is `completed`, while `authoritative_text`, `fts`, and
  `managed_packs` are `accepted` for explicit purge.

The fence and registry checkpoint commit in the same transaction as those
rows, so `deletion_status` and `purge` can continue from the original deletion
ID. An existing lifecycle must agree with the sidecar target and is otherwise
left unchanged; repeated initialization therefore does not downgrade a later
`completed` state. If a restored snapshot predates the referenced partition,
there is no local lifecycle to materialize or content to purge, but the signed
fence remains authoritative.

Recovery does not invent the tombstoned claim revision, cancelled job status,
or other main-database maintenance that never committed. Reads, traces,
workers, governance, feedback, and claim matching instead fail closed against
the recovered fence; physical cleanup still requires explicit purge. A later
`forget` of the same typed target adopts the recovered deletion ID rather than
creating a second lifecycle, even though the original request's idempotency
metadata was never committed.

For claim deletion with `expected_revision_id`, the engine acquires the main
SQLite write lock before the final compare-and-swap check and keeps it through
the sidecar append and logical tombstone transaction. A concurrent `govern`
therefore either commits before the check or waits and later receives a
revision conflict; it cannot make `forget` record an irreversible fence and
only then discover a stale revision.

`expected_revision_id` is rejected during request validation for every
non-claim target. For partition deletion, `target_id` and the explicit scope
must serialize to the same exact partition.

The registry is an HMAC-authenticated, append-only hash chain bound to the
database identity. It protects deletion state from accidental rollback and
detectable tampering; it does not encrypt content.

### 2. Make the target logically invisible

After the fence commits:

- canonical get, search, context, historical queries, and Evidence Pack reads
  return no matching protected content;
- affected FTS entries are removed or made harmless by canonical hydration;
- relevant formation jobs are cancelled or fenced;
- affected managed packs become tombstoned and unreadable;
- claims affected by claim, partition, or subject deletion return not-found
  from governance, feedback, and retrieval-trace access;
- replay and reindex cannot reintroduce the target.

The receipt state is `logically_hidden`. Its layer list still distinguishes
pending physical work.

### Scope retirement and later writes

Partition and subject fences are permanent scope-retirement markers in the
alpha. There is no unretire operation:

- `remember` and `capture(persist=true)` check the fence inside their write
  transaction, before replay lookup, ledger allocation, idempotency
  consumption, or content insertion, and return `SCOPE_DENIED` on a match;
- `capture(persist=false)` still returns `no_store` because it writes nothing;
- a partition fence rejects every persistent write to that exact partition;
- a subject fence matches `(partition, subject_id)` without considering
  `subject_type`; any explicitly listed matching subject rejects the whole
  request, while an unlisted or different subject remains writable.

Subject deletion has a strict lineage-completeness assumption. It selects only
records explicitly linked through `claim_subjects` or `event_subjects`.
Existing untagged text that merely mentions the person is not discovered or
purged, and a future untagged write is not blocked by the subject fence.

Claim, event, and managed-pack deletion do not retire the whole partition.
Later authorized writes may create new IDs and lineage in a live scope, but
claim matching, reinforcement, and supersession never reuse lineage covered
by a current or restored fence. Deleting one event also does not retire a
claim whose rebuilt current revision still has live evidence; that claim
remains governable and can receive feedback.

### 3. Purge engine-managed content

Physical purge is currently a separate Python or `recallctl purge` operation.
The `cascade_policy` value is recorded with the deletion request, but the alpha
does not run an autonomous purge worker and the MCP and HTTP adapters do not
expose a physical-purge operation.

Purge updates the independently reported `authoritative_text`, `fts`, and
`managed_packs` layers, then marks the deletion complete. Failed or skipped
layers must not be interpreted as physical completion. Even
`authoritative_text=completed` means the documented purgeable body stores were
scrubbed; it does not mean every retained identifier or revision reason was
erased.

## Target behavior

| Target | Immediate logical effect | Physical purge behavior |
|---|---|---|
| `claim` | Tombstones the claim head, removes it from FTS, invalidates all managed packs in the partition, and makes governance, feedback, and affected trace reads not-found | Removes that claim's content, FTS row, feedback, and derived-candidate rows. It does **not** erase the originating event payload or evidence body |
| `event` | Fences the event, cancels its jobs, marks its evidence deleted, and removes only claims with no remaining live evidence from visibility | Removes the event payload, evidence bodies, formation candidate content, and scrubs job payloads. Claim content is removed only when no live evidence remains |
| `subject` | Permanently fences explicitly linked `subject_id` data in one exact partition, rejects future tagged writes, cancels linked event jobs, and tombstones linked claims | Purges explicitly linked event and claim content. Matching is by `(partition, subject_id)`, not by `(type, id)`, and does not discover untagged prose |
| `partition` | Permanently retires the exact partition, increments its cancellation epoch, rejects future persisted writes, cancels jobs, and tombstones claims and packs | Purges event-derived and claim content, FTS, feedback, candidates, retrieval traces, and managed packs for that partition |
| `managed_pack` | Tombstones one pack so its resources are no longer readable | Removes that registered managed-pack directory and marks it purged |

For every non-pack target, pack invalidation is deliberately conservative:
all active managed Evidence Packs in the partition are tombstoned, and purge
removes them. This avoids retaining a derived snapshot whose complete lineage
may no longer be valid.

Deleting a claim is not the same as erasing its source observation. To remove
source text, target the supporting event, an appropriately linked subject, or
the partition. Event deletion on a multi-source claim retains claim text while
at least one live evidence source remains.

## Engine-managed copies

Depending on target lineage, purge can address:

- event payload text;
- evidence bodies and excerpts;
- automatic-formation candidate text;
- outbox payload copies;
- claim contents and feedback;
- SQLite FTS rows;
- retrieval traces for the affected partition;
- registered managed Evidence Pack directories.

Purge intentionally retains lineage and receipt metadata. It is not all
opaque, and it can remain linkable or textual. Retained fields can include:

- event `external_event_id`, `idempotency_key`, `session_id`,
  `host_agent_id`, `producer_id`, and `request_id`;
- claim `memory_key`;
- `subject_id` and subject lineage;
- revision `actor_principal_id` and `memory_revisions.reason`;
- deletion idempotency keys, typed target IDs, hashes, timestamps, ledger
  records, relationship shape, and layer state;
- the sidecar's plaintext `target_id`, plus its authenticated hashes and
  deletion ID.

These fields must never carry content or PII that the caller expects purge to
erase. In particular, do not use a direct email address as `subject_id`, and
do not place secrets or body excerpts in memory keys, caller identifiers,
idempotency keys, target IDs, or revision reasons. Longer-term hardening
includes domain-separated HMAC target handles in the sidecar and a separately
purgeable revision-reason body; neither is implemented in the alpha.

## Managed packs versus exports

A managed Evidence Pack is registered in SQLite, stored below the configured
managed root, authorized on read, and made inaccessible after its TTL or a
matching deletion. Expiry currently blocks reads; there is no background
expiry sweeper, so expired files can remain on disk until explicit cleanup or
purge.

`export_evidence_pack` and CLI `context --out` create a new unmanaged
directory. Exporting requires both `read` and `export` on the same exact
partition; `read` alone is insufficient. That export is intentionally outside
RecallOrigin's purge boundary. The engine cannot find or delete later copies
of it.

Pack files are written to a private staging directory and atomically renamed
before SQLite registration. A crash in that interval can leave an
unregistered plaintext directory. Initialization removes only recognizable
unregistered `pack_*` or `.pack_*.*` direct-child directories older than five
minutes. Registered paths, recent entries, unknown names, symbolic links, and
paths outside the configured managed root are preserved. This conservative
reconciliation is not an expiry sweeper or general-purpose filesystem
cleanup.

Pack reads and removals resolve each registered path and require it to be a
direct child of the currently configured `managed_pack_root`. Reconfiguring
the root does not silently follow or delete the old location: old packs become
not-found for reads, and removal fails closed until the operator restores the
original root configuration or explicitly migrates the packs.

## External and residual copies

RecallOrigin cannot delete:

- user-created exports, copied files, screenshots, clipboard contents, shell
  history, or logs;
- memory already injected into an agent context or copied into another
  database;
- OS, filesystem, VM, Time Machine, cloud, or storage-provider snapshots;
- offline backups that are not restored through the current purge registry;
- remote model, embedding, vector, observability, or provider retention;
- deleted blocks retained by filesystems, SSD controllers, or forensic media.

Deletion receipts explicitly report this class as external. Operators must
apply the relevant retention and deletion controls in each external system.

## WAL and media caveat

The main and sidecar connections use `secure_delete=ON`; the durable profile
also uses WAL and `synchronous=FULL`. These settings improve SQLite behavior
but do not prove immediate byte-level erasure from WAL files, free pages,
filesystem journals, copy-on-write snapshots, or flash media. RecallOrigin
does not currently encrypt content at rest or provide crypto-erasure.

## Backup and restore contract

Treat these as one operational set:

- the main SQLite database and its WAL/SHM state;
- the current purge-registry SQLite sidecar;
- the purge-registry key;
- the managed-pack directory when packs must survive.

Ordinary point-in-time snapshots may copy the main database, but must not roll
the purge registry backward with it. A restored old main database must be
opened with the current registry and key. Initialization verifies database
identity, migration checksums, SQLite integrity, HMAC signatures, the
generation chain, and the main checkpoint. A missing, stale, mismatched, or
invalid registry fails closed.

Restoring an old main-database snapshot can also restore an `active`
`managed_packs` row. Reads do not trust that status alone: they match the
pack's recorded claim, evidence/event, and subject lineage against the merged
sidecar fences, and also enforce partition- and pack-level fences. A
pre-deletion pack containing the target therefore remains unreadable after
restore. A pack created later from unaffected lineage is not rejected merely
because another claim, event, or subject in the same partition was deleted.

The alpha has no end-to-end backup command. Operators are responsible for
using SQLite's backup API or another SQLite-safe procedure and for testing the
sidecar/key restore process.

The supported alpha deployment uses one application process per store. The
restore, pack reconciliation, and purge contract does not claim coordination
between independent MCP, HTTP, or worker processes sharing the same database
and managed-pack root.

See [ADR-0008](../adr/0008-deletion-purge-and-restore.md),
[ADR-0009](../adr/0009-sqlite-durability.md), and the
[Threat model](threat-model.md).
