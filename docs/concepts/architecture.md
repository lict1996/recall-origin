# Architecture

RecallOrigin `0.1.0a0` is a local-first memory runtime embedded in one trusted
machine. One application core serves the Python API, CLI, MCP stdio adapter,
and optional loopback-only HTTP adapter. SQLite is the authority; FTS,
optional vector candidates, retrieval traces, and Evidence Packs are derived
views.

![RecallOrigin evidence-first architecture](../assets/architecture.svg)

## Design boundary

RecallOrigin is designed for a single user or trusted team sharing local
storage. It aims for deterministic transactions, auditable lineage, and
fail-closed deletion behavior on one machine. It is not a replicated database,
a distributed high-availability service, or an authenticated remote
multi-tenant platform.

All stored and recalled memory is untrusted historical data. A claim can be
active or confirmed under engine policy without becoming a system
instruction or an objectively proven fact.

## Components

| Layer | Implemented responsibility |
|---|---|
| CLI, Python, MCP, HTTP | Validate protocol input and translate it to the shared application contract |
| `MemoryEngine` | Authorize exact partitions; coordinate formation, governance, retrieval, packs, and deletion |
| Formation boundary | Accept a `FormationProvider`, validate candidates, and apply a small inspectable policy gate |
| SQLite authority | Store the ledger, provenance, immutable claims and revisions, current heads, jobs, and deletion state |
| Derived retrieval | Produce exact and FTS5 candidates, optionally accept partition-safe vector candidates, then fuse with versioned RRF |
| Context packaging | Build a token-bounded Fast Context Pack or a managed, integrity-described Evidence Pack |
| Purge sidecar | Keep an HMAC-authenticated, append-only deletion registry outside the main database |
| Managed pack directory | Hold private, bounded Evidence Pack resources until expiry blocks access or purge removes them |

## Exact authorization partition

The authorization boundary is the exact tuple:

```text
(tenant_id, namespace_kind, namespace_id)
```

Supported namespace kinds are `workspace`, `user`, `agent_private`, and
`session_private`. Origin fields such as session, producer, request, and host
agent describe where a write came from; they do not grant access. A subject
describes whom or what a memory concerns; it also does not grant access.

The embedded engine evaluates capabilities and allowed partitions supplied by
trusted startup configuration. A request may select one of those exact
partitions, but it cannot assert a different tenant or principal. Object reads,
governance, feedback, deletion and purge status, formation-job status,
retrieval traces, and pack resources resolve the owning tenant and repeat
object-level partition checks. Object IDs outside the current tenant or
configured exact allowed partitions return not-found without revealing the
owning partition. Export first resolves a pack visible in an allowed
partition, then requires both `read` and `export`; `read` alone cannot create
an unmanaged copy. Candidate IDs from FTS or an optional vector adapter are
always canonically re-hydrated inside the requested partition.

The schema contains `partition_grants`, but the alpha runtime does not use
database rows as its authorization source. Persisted grant administration is
future work; see [Limitations](../limitations.md).

## Write paths

### Persistent write admission

In the alpha, a partition or subject deletion fence permanently retires that
persistent write scope. Inside the same transaction and before replay lookup,
ledger allocation, idempotency consumption, or content insertion, `remember`
and `capture(persist=true)` reject a matching fence with `SCOPE_DENIED`.
`capture(persist=false)` still returns `no_store` because it persists nothing.

A partition fence rejects every persistent write to that exact partition. A
subject fence matches `(partition, subject_id)`; `subject_type` is not a
discriminator. A request that explicitly names any retired subject is rejected
as a unit, while a write with no subject or only unrelated subjects remains
allowed. The fence uses explicit request lineage; it does not inspect prose for
untagged subject mentions. There is no unretire operation in the alpha.

### Explicit remember

An explicit write authorizes one exact partition and commits the following in
one short `BEGIN IMMEDIATE` transaction:

1. a monotonic ledger transaction;
2. an immutable event and its payload;
3. an evidence artifact and body;
4. a new claim and revision, or a reinforcement revision when the complete
   claim identity matches;
5. the compare-and-swap current head and FTS projection.

The tuple `(partition, producer_id, external_event_id)` provides durable replay
identity. Repeating the same logical request returns the original receipt;
reusing the identity for different content fails.

Claim matching, reinforcement, and supersession exclude any lineage covered by
a claim, subject, partition, or relevant event fence. A later authorized write
in a non-retired scope can create new lineage, but it cannot mutate or attach
to deleted lineage, including after restoring an old main-database snapshot.

A configured local human creates active, `user_confirmed` semantic or episodic
memory. Writes from Agent and service principals remain unverified candidates,
as does model-derived formation output retained by the policy gate. Such a path
may reinforce only an existing `candidate/unverified` head when the exact
partition, `memory_key`, normalized content, kind, subtype, `valid_from`, and
`valid_to` all match. It never matches an `active/user_confirmed` head, inherits
its status, or supersedes it.

Only a Human write that produces `active/user_confirmed`, or a Human
`govern(confirm)`, may supersede other unfenced live claims under the same key.
The trusted revision and same-key supersession commit in one transaction;
`govern(confirm)` also applies its expected-revision CAS in that transaction.
These labels express policy state, not truth. See the normative
[claim-transition rules](data-model.md#claim-transition-authority).

### Automatic capture and formation

Capture commits an event, its evidence, and an outbox job in one transaction.
Formation is separate and eventually consistent:

1. a worker with `write` capability leases a job only from its configured
   tenant and one exact allowed partition, then increments a lease-generation
   fencing token;
2. a `FormationProvider` returns untrusted candidate proposals outside the
   SQLite write transaction; the engine accepts only a tuple of at most 32
   schema-valid candidates and at most 1 MiB of aggregate serialized JSON;
3. the policy gate rejects, quarantines, or retains candidates;
4. derived writes and the job's final state commit atomically only after
   tenant, capability, exact partition, lease generation, and deletion epoch
   are checked again;
5. failures retry with bounded backoff and eventually reach a dead-letter
   state.

Supplying an unauthorized `job_id`, or polling without one when only
unauthorized jobs are pending, does not lease the job, increment its attempt,
or expose its payload.

Malformed, over-count, or over-byte provider output becomes
`PROVIDER_OUTPUT_INVALID` and enters the bounded retry/dead-letter path before
any formation run, derived candidate, or claim is committed.

The bundled `StructuredEventProvider` only validates candidates already placed
in a structured event by the host. No model-backed extractor is bundled, and
the engine does not silently call a model. Regardless of provider operation,
retained formation output can only propose a new unverified claim or reinforce
a fully matching `candidate/unverified` head; it cannot perform trusted
supersession.

## Retrieval path

Retrieval stays within one authorized exact partition:

1. exact normalized-content and `memory_key` matches are collected;
2. current lexical candidates come from SQLite FTS5, with a safe literal
   fallback;
3. an optional vector adapter may contribute IDs only when it declares exact
   partition filtering;
4. candidates are fused with versioned reciprocal-rank fusion;
5. canonical hydration checks partition, revision, status, valid time,
   transaction time, live evidence, subjects, and deletion fences;
6. a content-free trace records a domain-separated keyed query digest,
   component ranks, selected IDs, latency, and degradation reasons;
7. selected results are packed into Fast or Evidence context.

No vector implementation is configured by default. Historical
`known_at_seq` retrieval skips FTS and vector paths and relies on canonical SQL
resolution plus the lexical fallback.

Fast Context Packs mark recalled text as untrusted, apply a token budget, and
XML-escape both metadata attributes and recalled content before constructing
the injection frame. Stored text containing closing tags or other delimiters
therefore remains text rather than changing the wrapper structure. This
framing does not make the memory trusted or eliminate semantic prompt
injection. The default token counter is a conservative UTF-8 estimate; a host
can inject its model's tokenizer. Evidence Packs add revision history,
accessible evidence excerpts, an integrity manifest, a retrieval trace, and a
standalone read-only Inspector.

## Governance and deletion

Governance appends a revision and advances `claim_heads` with compare-and-swap.
Normal governance does not rewrite prior claims, revisions, or evidence links.
Physical privacy purge is the explicit exception that may remove content
bodies while retaining lineage metadata. Some retained identifiers and
revision reasons remain linkable or textual; they are enumerated in the
[deletion boundary](../security/deletion-boundary.md).

For claim deletion with an expected revision, the engine holds the main
SQLite write lock from the final compare-and-swap check through the sidecar
append and logical tombstone commit. A concurrent governance write therefore
cannot cause an irreversible fence to be recorded before a late revision
conflict. The sidecar entry remains the anti-resurrection authority if the
process fails before the main-database commit.

On startup, an authenticated sidecar tombstone whose deletion request is
missing is recovered under the same main-database `BEGIN IMMEDIATE`
transaction that applies fences and advances the registry checkpoint. When
the referenced partition exists, recovery preserves the deletion ID, target,
partition, and registry timestamp; creates conservative lifecycle metadata;
marks logical visibility complete; and leaves the physical layers accepted
for explicit purge. Existing lifecycle rows are validated and preserved, so
repeated initialization cannot downgrade a later completed state. Recovery
does not synthesize uncommitted tombstone revisions or job states; the fence
makes those paths fail closed. A same-target retry adopts the recovered
deletion ID.

Partition deletion requires the typed target ID and supplied scope to name the
same exact partition. `expected_revision_id` is valid only for claim deletion.
Claims covered by claim, partition, or subject fences return not-found from
governance, feedback, and retrieval-trace access, so no hidden mutation history
accumulates behind the fence. Event deletion is narrower: if a claim still has
live evidence, its rebuilt current revision remains readable, governable, and
eligible for feedback.

Deletion fences win over retrieval, reindexing, workers, and historical
queries. When an old main-database snapshot is restored with the current
sidecar, managed-pack reads also compare restored pack lineage with merged
claim, event, subject, partition, and pack fences. A later pack that contains
no deleted lineage remains readable. Physical purge is a separate operation
and reports individual managed layers. See
[Deletion boundary](../security/deletion-boundary.md) for target-specific
behavior and external-copy limits.

## Storage and failure model

Every main SQLite connection enables foreign keys, WAL, a bounded busy
timeout, `secure_delete`, and `trusted_schema=OFF`. The default durable profile
uses `synchronous=FULL` and requests full-fsync behavior where the platform
exposes it. Provider, network, and filesystem pack assembly work is kept
outside database write transactions.

Pack assembly uses a private staging directory and an atomic rename before
SQLite registration. On initialization, the engine conservatively removes
only recognizable, unregistered `pack_*` or `.pack_*.*` direct-child
directories older than five minutes. The grace period avoids racing a recent
writer; registered paths, recent entries, unknown names, symbolic links, and
paths outside the managed root are not removed. This is crash-orphan
reconciliation, not a general filesystem garbage collector.

Registered managed-pack reads and removals also resolve the stored path and
require it to be a direct child of the currently configured
`managed_pack_root`. Reconfiguring that root can initialize a new pack
location, but existing packs under the old root fail closed until the operator
restores the prior configuration or explicitly migrates them.

This profile is intended to survive process failure without partial visible
writes. It does not prove losslessness under every OS crash, power loss,
filesystem, controller, or SSD behavior. The balanced profile deliberately
uses `synchronous=NORMAL` and has a weaker power-loss boundary.

The supported alpha topology is one application process per store. SQLite
locking and lease fencing still serialize incidental concurrent connections
and worker objects, but RecallOrigin does not claim coordinated
multi-process worker, pack-filesystem, or lifecycle semantics. Multiple HTTP
workers or independent MCP/worker processes must not share one store as a
supported deployment.

The main database, purge-registry database, and purge-registry key form one
operational unit. The store refuses normal access when the registry is
missing, has an invalid signature, is older than the main checkpoint, or is
bound to another database identity. Restoring an old main-database snapshot
with the current registry re-applies deletion fences before reads.

## Protocol boundaries

- **Python and CLI:** embedded local access under the configured OS account.
- **MCP stdio:** one fixed principal, exact startup partitions, and a
  non-privileged default tool set. Governance and deletion require explicit
  startup capabilities. `memory_search` is deliberately annotated
  `readOnlyHint=false` and `idempotentHint=false` because each call persists a
  new retrieval trace. `memory_context` also uses `readOnlyHint=false`: Fast
  mode persists a retrieval trace, while Evidence mode additionally registers
  and writes a managed pack.
- **HTTP:** fixed principal and loopback clients only. It has no remote
  authentication and must not be bound to a public interface or treated as a
  network security boundary.

See the [threat model](../security/threat-model.md) before deploying an
adapter.

## Decision records

The main invariants are documented in:

- [ADR-0001: claim and revision identity](../adr/0001-claim-revision-identity.md)
- [ADR-0002: partition, origin, and subject separation](../adr/0002-partition-origin-and-subject.md)
- [ADR-0003: local principals and capabilities](../adr/0003-sqlite-principal-and-capabilities.md)
- [ADR-0004: bitemporal time](../adr/0004-bitemporal-time-model.md)
- [ADR-0005: evidence retention](../adr/0005-evidence-retention.md)
- [ADR-0006: formation and idempotency](../adr/0006-formation-and-idempotency.md)
- [ADR-0007: transactional outbox](../adr/0007-transactional-outbox.md)
- [ADR-0008: deletion and restore](../adr/0008-deletion-purge-and-restore.md)
- [ADR-0009: SQLite durability](../adr/0009-sqlite-durability.md)
