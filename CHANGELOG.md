# Changelog

All notable changes to RecallOrigin will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Prereleases may contain breaking changes.

## [Unreleased]

No changes yet.

## [0.1.0a0] - 2026-08-30

Initial alpha prepared for GitHub prerelease.

### Added

- Evidence-first SQLite and FTS5 memory ledger with immutable events, claims,
  revisions, evidence links, and bitemporal metadata.
- Exact authorization partitions, idempotent capture and remember operations,
  CAS governance, and feedback.
- Leased automatic-formation jobs with retries, cancellation, and a dead-letter
  path.
- Exact, lexical, and optional vector retrieval with versioned reciprocal-rank
  fusion and content-free retrieval traces.
- Compact and Evidence Context Packs, including an offline read-only inspector.
- Python, CLI, MCP stdio, and HTTP adapters over the same application core.
- Engine-managed deletion and signed purge-registry checks to prevent deleted
  content from reappearing during supported restore and reindex flows.
- Deterministic synthetic quality and retrieval experiments plus reliability,
  migration, contract, integration, property, and unit tests.

### Security

- Object-ID operations now resolve the owning tenant and exact partition
  before exposing authorization errors. IDs outside the current tenant or
  configured exact allowed partitions return not-found without partition
  details, including pack export and typed object deletion.
- Partition deletion requires `target_id` to exactly match the supplied scope,
  and `expected_revision_id` is accepted only for claim deletion.
- Automatic-formation workers require `write` in the current tenant and exact
  allowed partition both when leasing and before commit. Unauthorized polling
  leaves job state and attempt counts unchanged.
- Automatic-formation provider output is accepted only as a tuple of at most
  32 schema-valid candidates and at most 1 MiB of aggregate serialized JSON.
  Oversized or malformed output fails as `PROVIDER_OUTPUT_INVALID` before any
  formation run, candidate, or claim is committed.
- Managed Evidence Pack export requires both `read` and `export`.
- Claim deletion holds its revision CAS write lock through the signed sidecar
  append and logical tombstone commit, closing the concurrent-governance fence
  race.
- Startup atomically reconstructs a minimal deletion lifecycle when an
  authenticated sidecar tombstone committed before its main-database request.
  When the referenced partition exists, the original deletion ID remains
  usable for status and purge; same-target retries adopt it, and repeated
  recovery preserves any later lifecycle state.
- Partition and subject fences permanently retire those persistent write
  scopes in the alpha. `remember` and `capture(persist=true)` fail with
  `SCOPE_DENIED` before replay lookup, ledger allocation, idempotency
  consumption, or content writes. Non-persistent capture remains `no_store`,
  and unrelated subjects in the partition remain writable.
- Claims affected by claim, partition, or subject deletion reject governance,
  feedback, and retrieval-trace reads as not-found. Event deletion leaves a
  claim mutable when live evidence remains, and no deletion fence can be used
  to reinforce or supersede deleted lineage.
- Restored managed packs are checked against current sidecar claim, event,
  subject, partition, and pack fences before resource reads.
- Startup conservatively removes recognizable unregistered pack/staging
  directories older than five minutes while preserving registered, recent,
  unknown-name, symlink, and out-of-root paths.
- The generated MCP contract marks `memory_context` with
  `readOnlyHint=false` because it persists a retrieval trace and Evidence mode
  also creates a managed pack.
- Evidence Pack resources use opaque identifiers rather than server file paths.
- Fast Context rendering XML-escapes both metadata attributes and recalled
  content, so stored delimiter text cannot close or spoof its untrusted-memory
  wrapper.
- The Inspector forbids network access with a restrictive content-security
  policy and renders untrusted content as text.
- Managed-pack reads and removals require every registered pack path to remain
  a direct child of the currently configured `managed_pack_root`. Reconfiguring
  that root leaves old packs inaccessible until the operator restores the
  original configuration or explicitly migrates them.
- Release automation produces SHA-256 checksums, an SPDX SBOM, and GitHub build
  provenance.

### Known limitations

- `0.1.x` is a local, single-node SQLite runtime; it is not distributed HA.
- One application process per store is the supported alpha topology;
  independent MCP, HTTP, or formation-worker processes are not coordinated.
- The built-in benchmark is synthetic and does not establish SOTA.
- External exports, unmanaged backups, and provider copies are outside
  engine-managed purge.
- Purge intentionally retains privacy-relevant lineage fields, including
  caller identifiers, `memory_key`, `subject_id`, revision reasons, deletion
  targets, and plaintext sidecar target IDs. These fields must not carry
  erasable content or direct PII; in particular, `subject_id` should not be an
  email address.
- Subject deletion covers only explicit subject lineage. Untagged prose that
  merely mentions a person cannot be discovered or erased automatically.
- Vector retrieval is optional and is not reported when it was not measured.
