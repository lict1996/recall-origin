# Roadmap

RecallOrigin uses evidence-gated milestones rather than delivery-date
promises. A feature is not considered complete until its contract, failure
behavior, security boundary, tests, and reproducible evidence agree.

## Current alpha focus

The `0.1` line is intentionally a small, local SQLite core:

- exact-partition authorization from trusted startup configuration;
- immutable claim/revision/evidence lineage and bitemporal reads;
- durable explicit writes and transactional automatic-formation jobs;
- exact plus FTS retrieval, optional vector integration, and versioned RRF;
- Fast and Evidence Context Packs;
- logical deletion, managed-layer purge, and anti-resurrection sidecar.

Work in this line should harden those promises rather than add unrelated
storage engines or UI surfaces.

## Priority 1: close lifecycle gaps

Exit criteria:

- a documented retention sweep for expired retrieval traces, managed packs,
  and unreferenced raw data;
- a durable purge queue or worker so recorded purge policy has operational
  meaning beyond a manual CLI call;
- explicit source-deletion/reformation semantics for multi-evidence claims;
- a packaged, fault-tested backup and restore workflow that preserves the
  current purge registry and key;
- domain-separated HMAC target handles and separately purgeable revision-reason
  bodies so identifiers and rationale do not weaken content-erasure claims;
- subject-lineage completeness tooling that can identify untagged records
  without pretending prose matching is an authorization boundary;
- deletion receipts and documentation verified against every target and
  managed copy.

## Priority 2: make authorization administrable

Exit criteria:

- one authoritative grant model instead of an unused schema table plus
  startup-only policy;
- default-deny grant creation, revocation, and audit behavior;
- compatibility tests across Python, CLI, MCP, and HTTP;
- a separate design and threat model before any remote service is introduced.

An authenticated remote multi-tenant service would require a different
deployment profile, likely including database-enforced row security and
non-owner application roles. It must not be presented as a property of the
SQLite runtime.

## Priority 3: improve formation safely

Exit criteria:

- a documented adapter kit for host-provided or model-backed extractors;
- versioned schemas, provider fingerprints, policy hashes, redacted error
  behavior, deterministic fixtures, and deletion-race tests;
- evaluation of candidate precision, unsupported claims, conflicts, and
  procedure safety;
- no bundled default that sends user data to a remote model without explicit
  operator configuration.

The core will continue treating provider output as an untrusted proposal.

## Priority 4: broaden retrieval evidence

Exit criteria:

- measured real-world datasets in addition to synthetic smoke cases;
- ablations for exact, lexical, optional vector, fusion, evidence filtering,
  and token packing;
- independent reproduction instructions and machine-readable artifacts;
- historical-retrieval improvements that preserve bitemporal and deletion
  correctness;
- calibrated quality claims that never turn rank scores into truth
  probabilities.

LongMemEval, LoCoMo, or another external benchmark may be useful, but no
single benchmark becomes a product claim by itself.

## Priority 5: ecosystem adapters

Additional integrations are justified only when they preserve:

- exact startup authorization or an equally explicit authenticated
  replacement;
- untrusted-memory labeling;
- stable idempotency and error contracts;
- object-level hydration and deletion fences;
- inspectable evidence and export-boundary warnings.

Potential adapters include more MCP host examples, host-specific extractors,
and optional local vector backends. A graph database, hosted sync service,
web dashboard, or telemetry system is not a prerequisite for the core.

## Non-goals for the current line

- Claiming distributed high availability from SQLite.
- Advertising hostile multi-tenant isolation without a new security design.
- Shipping a default remote model or embedding dependency.
- Treating automatically summarized memory as truth.
- Hiding external-copy limits behind a generic “delete complete” status.
- Optimizing leaderboard numbers without reproducible artifacts and
  real-world validation.

Current constraints are listed in [Limitations](limitations.md). Architecture
and security changes should be recorded as new ADRs beside the existing
[decision records](adr/).
