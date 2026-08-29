# Threat model

This threat model covers RecallOrigin `0.1.0a0` as an embedded runtime on one
trusted machine. It does not extend the alpha into an authenticated remote
service or claim isolation between mutually hostile tenants.

## Security objectives

RecallOrigin aims to:

- prevent reads and mutations outside a principal's exact configured
  partitions and capabilities;
- keep stored memory from acquiring instruction authority;
- preserve event, evidence, claim, and revision lineage;
- make retries and concurrent governance deterministic;
- ensure a committed deletion fence wins over reads, workers, indexes, and
  restoration of an older main-database snapshot;
- avoid leaking raw queries or memory content through routine retrieval
  traces and errors.

Availability under arbitrary denial of service, confidentiality against a
machine administrator, forensic media erasure, remote identity, and
distributed consensus are outside this boundary.

## Protected assets

- Event payloads, evidence bodies, claim contents, subjects, and queries.
- Linkable lineage metadata, including external, idempotency, session, host,
  producer, request, memory-key, subject, actor, deletion-target, and sidecar
  target identifiers, plus revision reasons.
- Partition membership and object existence.
- Claim/revision integrity and governance history.
- Outbox state and idempotency behavior.
- Deletion fences, the purge-registry hash chain, and its HMAC key.
- Managed Evidence Pack files and their manifest integrity.

The purge-registry key authenticates deletion state; it does not encrypt
memory content.

## Trust boundaries

| Boundary | Trust assumption |
|---|---|
| OS account and filesystem | Trusted to isolate the database, sidecar, key, and managed packs from other users |
| Startup configuration | Trusted source of tenant, principal, capabilities, and exact `(tenant_id, namespace_kind, namespace_id)` partitions |
| Request payload | Untrusted and unable to assert tenant or principal |
| Stored and recalled memory | Untrusted data, even when active or confirmed |
| Formation provider | Untrusted proposal source; may fail or return hostile text |
| Optional vector adapter | Untrusted candidate source that must enforce the supplied exact partition and is still canonically filtered |
| MCP stdio host | Trusted to launch the server with correct arguments and protect its process transport |
| Loopback HTTP client | Not authenticated; any process that can reach the local socket may act as the fixed principal |
| Exported files and external providers | Outside RecallOrigin's deletion and confidentiality control |

## Threats and current controls

| Threat | Implemented controls | Residual risk |
|---|---|---|
| Memory or prompt injection | Protocol outputs and packs label memory as untrusted; Fast Context XML-escapes attributes and recalled content so stored delimiters cannot alter its wrapper; context says it cannot override higher-priority instructions; automatic instruction-like candidates are quarantined; Inspector uses `textContent` and a restrictive CSP | Structural escaping and the policy gate do not detect every semantic prompt injection. Hosts must preserve instruction hierarchy and tool authorization |
| Memory poisoning or false claims | Agent/service writes and retained model-derived proposals remain unverified candidates; they can reinforce only a complete-identity-matched `candidate/unverified` head and cannot inherit, overwrite, or supersede `active/user_confirmed` state; procedural memory does not auto-activate; only Human trusted writes or Human confirmation can perform same-key supersession; `govern(confirm)` commits its CAS, confirmation, and supersession atomically | Confirmation is policy metadata, not independent fact verification. A trusted local human or compromised host can store false data |
| Partition confusion | Exact `(tenant_id, namespace_kind, namespace_id)` authorization; no prefix or parent scope; object-level checks; FTS/vector IDs are canonically hydrated | The local-human default intentionally has broad access within the local tenant. This is not hostile tenant isolation |
| Identity spoofing in requests | Tenant and principal are fixed at startup; origin and subject fields do not grant access | A compromised launcher can configure a more powerful principal |
| Insecure direct object reference | Get, govern, feedback, forget, purge/status, formation-job status, retrieval traces, pack reads, and pack export resolve the object's owning tenant and exact partition; IDs outside the current tenant or configured exact allowed partitions return not-found without scope details | Timing and local filesystem observation are not hardened against a same-account attacker |
| Unauthorized formation worker | Leasing requires `write`, the current tenant, and an exact allowed partition for both targeted and untargeted polling; commit repeats those checks | A trusted formation provider receives the already-authorized event outside the write transaction and may retain it externally |
| Unauthorized export | A managed pack visible in an allowed partition requires both `read` and `export`; packs outside the current tenant or configured exact allowed partitions are treated as not-found | Once created, an unmanaged export is outside RecallOrigin's access and deletion control |
| Replay and duplicate formation | Per-partition producer/external-event uniqueness, logical request hashes, idempotency receipts, candidate fingerprints, job dedupe, and lease-generation fencing | Callers must reuse stable identities. External provider calls can still happen more than once |
| Stale worker commits after deletion | Cancellation epochs, lease fencing, job cancellation, tombstone checks before formation commit, and canonical hydration | A provider may already have received data before cancellation; its retention is external |
| Post-deletion invisible data accumulation | Partition and subject fences permanently reject matching persisted `remember`/`capture` requests before replay, ledger, idempotency, or content writes; claim, partition, and subject fences make affected governance, feedback, and retrieval traces not-found; claim matching excludes fenced lineage | Scope retirement has no undo in the alpha. Subject matching uses explicit `(partition, subject_id)` lineage only, so untagged text mentions are not discovered and `subject_type` cannot distinguish reused IDs |
| Claim CAS race records an irreversible fence | Claim forget holds the main write lock from its expected-revision check through sidecar append and logical tombstone commit, so concurrent governance cannot slip between them; startup reconstructs a minimal lifecycle from an authenticated sidecar-only tombstone | Recovery cannot reproduce original request metadata that never committed, so it uses recovery-derived idempotency metadata and the conservative `safe` cascade policy |
| Old backup resurrects deleted data | Deletion is appended to an HMAC-authenticated sidecar before the main fence; startup verifies identity, signatures, generations, and checkpoints, then merges fences. Reads, traces, managed packs, governance, feedback, reinforcement, and supersession re-check restored lineage against current fences | Losing both current registry and key makes the store unavailable by design. Copying or exposing the key permits an attacker with filesystem write access to forge state |
| Index leakage | FTS is derived; every hit passes canonical status, evidence, partition, and tombstone checks; deletion removes or invalidates rows; reindex uses canonical data | Other tools reading SQLite or raw files directly bypass engine checks |
| Vector cross-scope leakage | Vector search is disabled by default; adapters must declare exact partition filtering; returned IDs are re-hydrated inside the exact partition | Query text and identifiers may leave the process if an operator installs a remote vector adapter |
| Evidence Pack traversal or script injection | Safe relative paths, no-follow/private file creation, direct-child containment under the currently configured managed root for reads and removals, file and pack size limits, manifest SHA-256, read-only Inspector, CSP, and text-only rendering | Changing `managed_pack_root` does not migrate registered packs; old paths fail closed until the original root is restored or an explicit migration is performed. SHA-256 is integrity metadata, not encryption |
| Crash leaves an unregistered plaintext pack | Initialization removes only recognizable unregistered direct-child pack/staging directories older than five minutes and refuses to follow symlinks or paths outside the managed root | Plaintext can remain during the grace window; recent, unknown-name, symlink, or externally copied content is deliberately not treated as safe-to-delete garbage |
| Query leakage in traces | Retrieval traces store a domain-separated keyed HMAC of the query and content-free ranks/IDs rather than raw query text | Evidence Packs intentionally include their query and selected excerpts; exported packs are external copies |
| Purge mistaken for complete metadata erasure | The deletion contract enumerates retained fields and separates content bodies from lineage; operators are told not to encode erasable text or direct PII in identifiers or revision reasons | Event and deletion identifiers, `memory_key`, `subject_id`, actor principal IDs, revision reasons, and plaintext sidecar target IDs can remain after managed purge. Domain-separated HMAC target handles and purgeable reason bodies are future work |
| Remote HTTP exposure | Adapter documentation and app metadata declare loopback-only operation; middleware rejects non-loopback peer addresses and ignores forwarded identity headers | Loopback is not authentication. Reverse proxies, containers, port forwarding, or binding to a public interface violate the threat model |
| Database tampering | STRICT tables, foreign keys, immutable-row triggers, content digests, migration checksums, integrity checks, and signed deletion state | The main database is not cryptographically authenticated as a whole. A same-account attacker with file access remains outside the intended adversary model |
| Resource exhaustion | Request, resource, pack, result, and token limits; automatic provider output is capped at 32 candidates and 1 MiB aggregate serialized JSON before formation commit; bounded retries and dead-letter state | A provider still runs outside the engine transaction and may consume arbitrary resources internally. Claim deletion holds a main write lock across the sidecar append; there are no per-principal quotas, admission control, or distributed rate limits |
| Unsupported multi-process deployment | Documentation and examples constrain one application process per store; HTTP is one-worker and MCP is stdio | SQLite locking is not a complete coordination protocol for independent worker processes and managed-pack filesystem lifecycle |

## Memory handling rule

Every host must treat retrieved text as quoted historical data:

```text
stored memory < current system/developer instructions < current authorization
```

A memory must never change identity, tool permissions, approval requirements,
network policy, or instruction precedence. This rule applies to explicit human
memory, agent memory, evidence excerpts, and imported provider output alike.

## Deployment requirements

For the supported local boundary:

1. Run under a dedicated, trusted OS account when multiple people share a
   machine.
2. Restrict filesystem access to the main database, its WAL/SHM files, the
   purge sidecar, the sidecar key, and the managed-pack directory.
3. Back up the current purge registry and key independently from ordinary
   main-database snapshots; test restoration without rolling the registry
   backward.
4. Keep MCP governance and deletion capabilities disabled unless the launched
   agent is explicitly trusted for them.
5. Keep the HTTP adapter on loopback or a local Unix socket with one worker.
   Do not expose it through a proxy or remote bind, and do not run independent
   MCP/HTTP/worker processes against one store.
6. Review any custom formation or vector adapter for data egress, exact
   partition enforcement, retries, logging, and retention.
7. Do not place secrets in memory unless the plaintext local-storage and
   export boundaries are acceptable.
8. Treat identifiers and revision reasons as retained metadata, not purgeable
   content. Do not use direct PII such as an email address for `subject_id`.

## Explicit non-claims

RecallOrigin currently does not provide:

- encryption at rest or crypto-erasure;
- remote user authentication, TLS termination, or hostile multi-tenant
  isolation;
- a sandbox for model/provider code;
- complete prompt-injection, malware, or truth detection;
- protection from a root user, machine administrator, or attacker controlling
  the same OS account;
- guaranteed erasure from filesystems, SSD firmware, snapshots, exports, or
  provider systems;
- complete erasure of retained lineage identifiers or revision reasons;
- supported multi-process worker or managed-pack coordination;
- distributed availability or consensus.

See [Architecture](../concepts/architecture.md),
[Deletion boundary](deletion-boundary.md), and
[Limitations](../limitations.md) for the corresponding operational behavior.
