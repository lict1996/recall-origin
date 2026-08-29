# Design influences

RecallOrigin is an independent implementation. It borrows design principles
from public memory systems, research, and protocols, but does not attempt to
combine all of their features or reproduce their deployment stacks.

## What we adopted

### claude-mem: progressive disclosure

[claude-mem's progressive-disclosure design](https://docs.claude-mem.ai/progressive-disclosure)
shows a compact index first, lets the agent choose relevant IDs, and fetches
full observations only when useful. RecallOrigin applies the same
information-foraging principle through bounded search results, Fast Context
Packs, and separately addressable Evidence Pack resources.

We did not copy its lifecycle hooks, always-on worker and viewer, Chroma
dependency, cloud sync, or Claude-specific installation model. RecallOrigin's
core is host-neutral and does not automatically capture every tool call.

### Mem0: a small integration surface

[Mem0](https://github.com/mem0ai/mem0) demonstrates the value of a small
developer surface centered on adding, searching, and deleting memory. Its
[paper](https://arxiv.org/abs/2504.19413) also treats memory as a distinct
agent subsystem rather than prompt text assembled ad hoc. RecallOrigin keeps
its public entry points small across Python, CLI, and MCP while moving
lineage, authorization, and retries behind one engine.

We did not adopt a default remote model, embedding service, managed account,
or vector database. RecallOrigin also avoids treating memory as one mutable
text record: claims, revisions, and evidence have separate identities.

### Graphiti: temporal facts with episode provenance

[Graphiti](https://github.com/getzep/graphiti) and the
[Zep temporal knowledge graph paper](https://arxiv.org/abs/2501.13956)
separate raw episodes from derived facts, preserve provenance, and model when
facts are valid versus when the system learned them. RecallOrigin translates
those principles into events, evidence artifacts, immutable claims,
revisions, valid-time intervals, and monotonic transaction sequence.

We did not copy the knowledge-graph ontology, graph traversal, automatic
entity extraction, Neo4j/FalkorDB deployment, or managed Zep platform.
RecallOrigin deliberately uses a relational SQLite ledger for its alpha.

### LangMem: hot-path and background formation

[LangMem](https://github.com/langchain-ai/langmem) distinguishes memory tools
used by an agent during an active conversation from a background memory
manager that extracts and consolidates later. RecallOrigin similarly separates
synchronous explicit `remember` from durable `capture` plus leased
background formation, while routing both outcomes through common governance
and evidence rules.

We did not bind the engine to LangGraph, its store interfaces, or a required
LLM. The bundled structured provider only validates candidate objects already
prepared by a host.

### LongMemEval-V2: agentic, file-based evidence exploration

[LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2) evaluates memory
over long agent trajectories, requires compact evidence for a downstream
reader, and measures answer quality together with latency. Its released
[Codex memory baseline](https://github.com/xiaowu0162/LongMemEval-V2/blob/main/memory_modules/codex.py)
lets an agent explore local trajectory files and return bounded supporting
spans. RecallOrigin adopts the broader idea that durable memory should be
inspectable as files and evidence, not only injected as an opaque summary.
Evidence Packs, retrieval traces, and token budgets reflect that principle.

We did not bundle the benchmark's model servers, CUDA environment, trajectory
dataset, or Codex runner. RecallOrigin's synthetic benchmark is not a
LongMemEval-V2 result and makes no leaderboard claim. See the
[LongMemEval-V2 paper](https://arxiv.org/abs/2605.12493).

### MCP: portable tools and resources

The [Model Context Protocol specification](https://modelcontextprotocol.io/specification/)
influenced the separation between typed mutation tools and read-only Evidence
Pack resources. RecallOrigin generates its MCP contract from the official SDK
surface, marks destructive operations, and exposes only capabilities enabled
at trusted server startup.

We do not treat MCP as authentication. The current adapter is local stdio with
one fixed principal and exact partitions; remote identity and authorization
would require a separate security design.

## Cross-cutting choices

Across these influences, RecallOrigin keeps six principles:

1. Reveal context progressively and within a declared budget.
2. Preserve source evidence and transaction history instead of trusting a
   summary alone.
3. Keep hot-path writes separate from optional background extraction.
4. Make authorization exact and independent from subject or session labels.
5. Keep the core useful without a model, vector service, graph database, or
   hosted account.
6. Measure behavior with reproducible artifacts and state explicitly what was
   not measured.

These influences do not establish correctness by association. RecallOrigin's
implemented contract, tests, [limitations](limitations.md), and
[threat model](security/threat-model.md) are the evidence for this project.
