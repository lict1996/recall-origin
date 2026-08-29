# Limitations

RecallOrigin `0.1.0a0` is an early alpha. The following constraints are part of
the current product boundary, not hidden implementation details.

## Deployment and availability

- SQLite is a high-reliability single-machine authority. There is no
  replication, leader election, failover, distributed transaction, or hosted
  control plane.
- The supported trust model is one user or trusted team. It does not isolate
  mutually hostile tenants sharing a database or OS account.
- The optional HTTP adapter has no authentication and accepts loopback peers
  as one fixed principal. Loopback is not user identity; do not expose it
  remotely or place it behind a proxy.
- MCP is stdio-only and uses one fixed startup principal. There is no
  per-request login, token exchange, or remote transport.
- The supported topology is one application process per store. Independent
  MCP, HTTP, or formation-worker processes sharing one database and managed
  pack root are not a supported deployment, even though SQLite serializes
  incidental concurrent connections.
- Memory, evidence, SQLite files, managed packs, and exports are plaintext.
  Filesystem permissions are the confidentiality boundary.
- The durable SQLite profile is not proof against every power-loss,
  filesystem, controller, or SSD failure mode.

## Authorization

- Authorization uses capabilities and exact allowed partitions supplied by
  trusted startup configuration. The boundary is the full
  `(tenant_id, namespace_kind, namespace_id)` tuple, not a namespace name or
  path prefix.
- Opaque object IDs outside the current tenant or configured exact allowed
  partitions return not-found without disclosing the owning partition.
- `partition_grants` exists in the schema, but there is no persisted grant
  administration flow and the runtime does not consult those rows.
- The local-human convenience principal intentionally auto-allows local
  scopes. Use explicit principals and OS isolation when that is too broad.
- Subject and origin metadata never narrow authorization automatically.
- Formation processing requires `write` and can lease only jobs in the
  principal's current tenant and exact allowed partitions. Formation is still
  explicitly invoked; no background worker is bundled.
- Creating an unmanaged Evidence Pack export requires both `read` and
  `export`. The MCP adapter does not expose pack export.

## Formation and memory quality

- No model-backed extractor is bundled. `capture` creates a durable job, but a
  host must run formation with a provider. The included
  `StructuredEventProvider` only validates host-supplied candidate objects.
- Automatic provider output must be a tuple containing at most 32 schema-valid
  candidates and at most 1 MiB of aggregate serialized JSON. Larger or
  malformed output is treated as `PROVIDER_OUTPUT_INVALID` before derived
  writes and follows the normal bounded retry/dead-letter policy.
- The formation policy is deliberately small. Its instruction-like pattern
  catches obvious cases, not all prompt injection or poisoned memory.
- Confirmation expresses engine governance state, not objective truth.
  RecallOrigin does not independently fact-check content.
- A configured local human's explicit non-procedural write becomes active and
  `user_confirmed`; agent/service writes, retained model-derived proposals, and
  procedural memory remain candidates.
- Non-Human writes can reinforce only an existing `candidate/unverified` head
  whose exact partition, `memory_key`, normalized content, kind, subtype, and
  valid-time bounds all match. They cannot inherit or supersede
  `active/user_confirmed` state. Only a Human trusted write or Human
  `govern(confirm)` performs same-key supersession, and confirmation plus
  supersession commit in one transaction.
- Feedback is stored but does not yet feed an automatic quality model or
  governance transition.
- Source expiry, raw-event TTL, and evidence-artifact retention policies have
  schema support but no general background retention scheduler.
- When one source is deleted from a multi-source claim, the current engine
  retains the claim text and creates a revision over the remaining evidence;
  it does not automatically re-run formation to rewrite that summary.

## Retrieval and context

- The zero-configuration retriever is exact matching plus SQLite FTS5 and a
  literal fallback. No embedding model or vector index is installed by
  default.
- A vector adapter is only an interface. Operators supply its implementation,
  storage, security, cost, and retention controls.
- Historical `known_at_seq` queries do not use FTS or vector search. They rely
  on canonical SQL resolution and the lexical fallback and can be slower or
  less complete for broad paraphrases.
- Reciprocal-rank-fusion scores are ranking signals, not confidence or truth
  probabilities.
- The default context budget uses a conservative UTF-8 token estimate, not a
  model-specific tokenizer. Hosts needing exact accounting must inject one.
- Fast Context uses an XML-like untrusted-memory frame and escapes recalled
  content and metadata attributes so delimiter text cannot change that frame.
  Escaping is structural hardening, not a complete prompt-injection defense.
- Retrieval traces expire logically after seven days, but there is no
  background row sweeper.
- MCP `memory_search` is not read-only or idempotent protocol work because
  every call persists a new retrieval trace. Its generated contract therefore
  uses `readOnlyHint=false` and `idempotentHint=false`.
- MCP `memory_context` is also not read-only protocol work. Fast mode persists
  a retrieval trace; Evidence mode additionally creates and registers managed
  pack files, so its generated contract uses `readOnlyHint=false`.

## Deletion and retention

- `forget` makes data logically invisible; physical purge is a separate
  Python or CLI call. No autonomous purge worker is included.
- `cascade_policy` is persisted but does not currently change or schedule the
  physical purge workflow.
- `expected_revision_id` is valid only for claim deletion. A partition
  deletion's `target_id` and explicit scope must name the same exact
  partition.
- Claim deletion removes claim content but does not erase its supporting event
  payload or evidence body. Select an event, subject, or partition when source
  erasure is required.
- Partition and subject deletion are permanent scope retirement in the alpha;
  there is no unretire operation. Matching persistent `remember` and
  `capture` calls fail with `SCOPE_DENIED`, while `capture(persist=false)`
  remains `no_store`.
- Subject deletion selects `(partition, subject_id)` without a `subject_type`
  discriminator and relies entirely on explicit `claim_subjects` and
  `event_subjects` lineage. Untagged prose mentions are not discovered or
  purged, and future untagged writes are not blocked.
- Claim, partition, and subject deletion make affected governance, feedback,
  and retrieval traces not-found. Event deletion does not retire a claim that
  still has live evidence.
- Fenced current or restored lineage is excluded from claim matching,
  reinforcement, and supersession. A later write in a live scope may create
  new lineage but cannot mutate the deleted lineage.
- A managed Evidence Pack becomes unreadable at expiry, but expired registered
  pack files are not automatically swept.
- Startup only reconciles recognizable unregistered crash-orphan and staging
  directories after a five-minute grace period. It deliberately leaves
  registered paths, recent entries, unknown names, symbolic links, and
  out-of-root paths untouched; this is not comprehensive garbage collection.
- Registered pack reads and removals require the stored path to be a direct
  child of the currently configured `managed_pack_root`. Changing that
  configuration does not migrate old packs: they fail closed until the prior
  root is restored or an explicit operator migration is performed.
- Exported packs, copied databases, backups, snapshots, provider retention,
  agent contexts, logs, filesystems, and storage media are outside engine
  control.
- `secure_delete` is not a forensic-erasure or crypto-erasure guarantee.
- There is no packaged backup/restore command; operators must preserve and
  test the current purge sidecar and key.
- Sidecar-first crash recovery reconstructs only a minimal lifecycle. Request
  metadata that never reached the main database cannot be recovered, so the
  engine derives replacement idempotency metadata and uses
  `cascade_policy=safe`; a later same-target `forget` adopts the recovered
  deletion ID. Recovery does not synthesize uncommitted tombstone revisions or
  cancelled job rows. If a restored snapshot predates the referenced
  partition, the signed fence remains authoritative but no local lifecycle
  rows are materialized.
- Engine-managed purge retains privacy-relevant metadata, including event
  external/idempotency/session/host/producer/request IDs, `memory_key`,
  `subject_id`, revision actor IDs and reasons, deletion idempotency/target
  fields, and the sidecar's plaintext target ID. Do not place erasable content
  or direct PII in those fields; especially, do not use an email address as
  `subject_id`. HMAC target handles and purgeable revision-reason bodies are
  future work.

See [Deletion boundary](security/deletion-boundary.md) for exact
target-by-target behavior.

## Operations and scale

- SQLite serializes writers. Busy workloads can receive lock timeouts, and no
  cluster-level admission control or queue is provided.
- Claim deletion keeps the main write lock across the final revision check,
  signed sidecar append, and logical tombstone commit. This closes the
  governance race but can extend one write-lock interval.
- There are size and result bounds, but no per-principal storage quota, rate
  limiter, billing, or abuse-control service.
- Automatic formation processing is explicitly invoked; there is no bundled
  always-on supervisor.
- Managed packs are local files. There is no object-store backend, sync
  protocol, or web administration UI.
- Schema migrations are forward-only in the application; downgrade tooling is
  not provided.

## Evidence and release claims

- The bundled benchmark is deterministic synthetic evidence. A 100-document
  smoke result does not establish broad real-world quality.
- Large benchmark scales measure one machine and configuration; results are
  not portable performance guarantees.
- No vector baseline is reported unless a vector adapter is actually
  configured and measured.
- The project does not claim state-of-the-art quality and does not treat its
  own benchmark as an independent leaderboard.
- Availability on PyPI, an MCP registry, a container registry, or any external
  catalog must not be inferred from this repository.

The [Roadmap](roadmap.md) prioritizes closing observable gaps without turning
the project into a feature checklist.
