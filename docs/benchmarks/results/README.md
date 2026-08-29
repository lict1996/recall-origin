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
| Search p50 | 133.909 ms | 250 queries |
| Search p95 | 226.763 ms | 250 queries |
| Search p99 | 239.992 ms | 250 queries |
| Durable ingest | 23.437 writes/s | 10,000 writes |
| Ingest duration | 426.681 s | 10,000 writes |
| SQLite footprint | 41,177,088 bytes | 10,000 documents |
| Whole command wall clock | 465.59 s | one run |

The exact-query segment contained 84 queries and the lexical-marker segment contained
166. Both segments observed Recall@10 and MRR of 1.000. The artifact records every
relevance label, returned rank, component score, and query duration.

## Evidence files

- `scale-10000-durable.json`: canonical raw benchmark artifact.
- `scale-10000-durable-run.json`: command, UTC/local interval, and `/usr/bin/time` output.
- `scale-10000-durable-inspector.json`: measured Inspector-compatible projection.
- `scale-10000-durable-summary.json`: bounded values suitable for release copy.
- `scale-10000-durable-metrics.csv`: chart-ready measured rows.
- `scale-10000-durable-summary.svg`: data-derived standalone chart.
- `SHA256SUMS`: integrity values for all files above.

The canonical artifact SHA-256 is
`a8483be63b42dd93422ba74d4c3bf07cd6bb5a8f55c9c79ed9480826ded96387`.
Its corpus SHA-256 is
`430d607417cfbfa92c649fbe1914f9e7eb33fbc00d214f5d3f4200c837fd95a0`.
Its package/dependency source snapshot SHA-256 is
`9786886c23caf807d387009eb02784eeea6b1a76b69fcc91e869c5c5c52b8a72`;
the runner verified that this snapshot remained unchanged throughout the measurement.
The repository had no initial commit during this run, so Git provenance is explicitly
`null`/unavailable rather than invented.

## Reproduce

From the repository root:

```bash
uv run python -m recall_origin.benchmarks.runner \
  --scale 10000 \
  --seed 20260830 \
  --query-count 250 \
  --k 10 \
  --output docs/benchmarks/results/scale-10000-durable.json \
  --inspector-output docs/benchmarks/results/scale-10000-durable-inspector.json

uv run python scripts/render_benchmark_summary.py \
  docs/benchmarks/results/scale-10000-durable.json

(cd docs/benchmarks/results && shasum -a 256 -c SHA256SUMS)
```

The corpus hash, query targets, relevance labels, and source fingerprint are
deterministic. Timing and database size are observations of the recorded environment and
can vary with hardware, filesystem, system load, Python, and SQLite versions.

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
- 100k and 1M were not run. The measured 10k command already required 465.59 seconds with
  durable per-memory transactions, so larger measurements were outside the reasonable
  time and I/O budget for this evidence pass. No results are claimed for those scales.
