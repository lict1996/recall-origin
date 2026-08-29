# Measured 10k durable benchmark

This directory contains one bounded, reproducible local measurement for RecallOrigin
`0.1.0a0`. It is release evidence, not a leaderboard or a cross-system performance claim.

![RecallOrigin 10k durable local benchmark](scale-10000-durable-summary.svg)

## Result

| Measurement | Observed value | Sample |
| --- | ---: | ---: |
| Required behavioral cases | 4/4 passed | 4 cases |
| Recall@10 | 1.000 | 250 queries |
| MRR | 1.000 | 250 queries |
| Search p50 | 138.209 ms | 250 queries |
| Search p95 | 266.073 ms | 250 queries |
| Search p99 | 283.301 ms | 250 queries |
| Durable ingest | 19.783 writes/s | 10,000 writes |
| Ingest duration | 505.491 s | 10,000 writes |
| SQLite footprint | 41,177,088 bytes | 10,000 documents |
| Whole command wall clock | 551.52 s | one run |

The exact-query segment contained 84 queries and the lexical-marker segment contained
166. Both segments observed Recall@10 and MRR of 1.000. The artifact records every
relevance label, returned rank, component score, and query duration.

## Evidence files

- `scale-10000-durable.json`: canonical raw benchmark artifact.
- `scale-10000-durable-run.json`: command, commit, environment, UTC/local interval,
  wall time, and child-process CPU time.
- `scale-10000-durable-inspector.json`: measured Inspector-compatible projection.
- `scale-10000-durable-summary.json`: bounded values suitable for release copy.
- `scale-10000-durable-metrics.csv`: chart-ready measured rows.
- `scale-10000-durable-summary.svg`: data-derived standalone chart.
- `SHA256SUMS`: integrity values for all files above.

The canonical artifact SHA-256 is
`b72e398c1b1bb387f6b549de5d4a598f412784145997ab503240c99a1e0b9e6f`.
Its corpus SHA-256 is
`430d607417cfbfa92c649fbe1914f9e7eb33fbc00d214f5d3f4200c837fd95a0`.
Its package/dependency source snapshot SHA-256 is
`44c4b01ffdb9cf7bb9592f89dfa4fcdac93ce85ab648d1566fc10a4bddb75f4d`;
the runner verified that this snapshot remained unchanged throughout the measurement.
The run started and ended with a clean checkout at commit
`534b7d66dfb27d235c49ee4e0d00a847d1a6abd2`; the artifact and run metadata
record that commit rather than inferring provenance after measurement.

## Reproduce

From the repository root:

```bash
test -z "$(git status --porcelain=v1 --untracked-files=all)"
uv run python scripts/benchmark_release.py
(cd docs/benchmarks/results && shasum -a 256 -c SHA256SUMS)
uv run pytest -q tests/benchmarks
```

The wrapper fixes the scale, seed, query count, `k`, and durable configuration,
then rejects missing Git provenance, a dirty checkout, source drift, an
inconsistent Inspector projection, or an incomplete checksum set. The corpus
hash, query targets, relevance labels, and source fingerprint are deterministic.
Timing and database size are observations of the recorded environment and can
vary with hardware, filesystem, system load, Python, and SQLite versions.

## Boundaries

- The corpus is deterministic and synthetic. Its unique markers make it a retrieval
  correctness and regression workload, not a proxy for production semantic relevance.
- The default 100-document run is only a synthetic smoke test. No 100-document
  performance number is presented here as release evidence.
- This is one sequential, same-process local run on macOS arm64 with CPython 3.12.5 and
  SQLite 3.53.3. Search followed ingest without a restart or cache-control protocol. It
  does not measure concurrency, multi-process contention, cache-state variance, confidence
  intervals, or another memory engine.
- No vector retriever was configured. Vector scores and the vector baseline are `null`;
  there is no estimated or fabricated candidate comparison.
- 100k and 1M were not run. The measured 10k command already required 551.52 seconds with
  durable per-memory transactions, so larger measurements were outside the reasonable
  time and I/O budget for this evidence pass. No results are claimed for those scales.
