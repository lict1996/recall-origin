# RecallOrigin technical documentation

These documents describe the implemented `0.1.0a0` alpha. RecallOrigin is an
embedded, single-machine SQLite runtime. The documentation does not imply a
hosted service, distributed high availability, hostile multi-tenant
isolation, or state-of-the-art memory quality.

## Start here

- [Architecture](concepts/architecture.md) — components, write and retrieval
  paths, trust boundaries, and failure boundaries.
- [Data model](concepts/data-model.md) — partitions, events, evidence, claims,
  revisions, bitemporal fields, projections, and deletion lineage.
- [Threat model](security/threat-model.md) — protected assets, assumed
  attackers, mitigations, residual risks, and deployment requirements.
- [Deletion boundary](security/deletion-boundary.md) — what becomes hidden,
  what physical purge removes, and which copies remain outside engine control.
- [Limitations](limitations.md) — current functional, operational, and
  security constraints.
- [Roadmap](roadmap.md) — evidence-gated directions, without delivery-date
  promises.
- [Design influences](design-influences.md) — principles borrowed from public
  memory systems, benchmarks, and protocols, plus the parts deliberately left
  out.
- [Benchmark methodology](benchmarks/README.md) — what the offline benchmark
  measures and what it does not prove.

## Architecture decisions

The accepted ADRs explain the intended invariants and their trade-offs:

1. [Claim and revision identity](adr/0001-claim-revision-identity.md)
2. [Partition, origin, and subject separation](adr/0002-partition-origin-and-subject.md)
3. [SQLite principals and capabilities](adr/0003-sqlite-principal-and-capabilities.md)
4. [Bitemporal time model](adr/0004-bitemporal-time-model.md)
5. [Evidence retention](adr/0005-evidence-retention.md)
6. [Formation and idempotency](adr/0006-formation-and-idempotency.md)
7. [Transactional outbox](adr/0007-transactional-outbox.md)
8. [Deletion, purge, and restore](adr/0008-deletion-purge-and-restore.md)
9. [SQLite durability profile](adr/0009-sqlite-durability.md)

Where an ADR describes a stronger future state than the current alpha, the
current behavior is called out in [Limitations](limitations.md). Code and
tests remain authoritative for release behavior.
