# Reproducing RecallOrigin checks and artifacts

This document covers source checkout, verification, offline experiments, and
release-artifact validation for RecallOrigin `0.1.0a0`. It does not imply that a
PyPI package has been published.

## Environment

- Python 3.11 or 3.12
- uv `0.12.5` (the version pinned by CI)
- Git
- SQLite with FTS5, as supplied by the supported CPython builds

Record the exact source and host before comparing results:

```bash
git rev-parse HEAD
git status --short
uv --version
uv run python -VV
uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

A dirty checkout is valid for development, but its benchmark artifact records
that state and should not be presented as a release result.

## Restore the locked environment

```bash
uv lock --check
uv sync --locked --all-extras --dev
```

`uv.lock` fixes application, optional, and development dependency resolution.
The PEP 517 build-system requirement in `pyproject.toml` is a version range, so
local builds are not claimed to be byte-for-byte reproducible across time.
Official GitHub artifacts additionally carry checksums, an SBOM, and GitHub
provenance.

## Run the source gates

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src scripts
uv run --extra mcp python scripts/generate_mcp_contract.py --check
uv run pytest --cov=recall_origin --cov-branch --cov-report=term-missing --cov-report=xml
git diff --check
```

Audit the locked runtime dependency set, including optional MCP and HTTP
dependencies but excluding development-only tools:

```bash
repro_dir="$(mktemp -d)"
uv export \
  --locked \
  --all-extras \
  --no-dev \
  --no-emit-project \
  --format requirements-txt \
  --output-file "$repro_dir/runtime-requirements.txt"
uv run pip-audit \
  --requirement "$repro_dir/runtime-requirements.txt" \
  --strict \
  --require-hashes \
  --progress-spinner off \
  --desc off
```

## Build and inspect distributions

Use the commit time as the archive timestamp input:

```bash
repro_epoch="$(git show -s --format=%ct HEAD)"
SOURCE_DATE_EPOCH="$repro_epoch" uv build --no-sources --clear
uvx --from twine==7.0.0 twine check dist/*
```

Test the wheel in a clean virtual environment:

```bash
repro_venv="$(mktemp -d)/venv"
uv venv --python 3.12 "$repro_venv"
uv pip install --python "$repro_venv/bin/python" dist/*.whl
"$repro_venv/bin/python" -c \
  "import recall_origin; print(recall_origin.__version__)"
"$repro_venv/bin/recallctl" --help
```

The GitHub release workflow also rebuilds a wheel from the source distribution
and validates both distributions before creating a release.

## Run the deterministic offline benchmark

The default 100-document run is a functional smoke experiment, not a production
performance claim:

```bash
uv run python -m recall_origin.benchmarks.runner \
  --scale 100 \
  --seed 20260830 \
  --query-count 25 \
  --k 10 \
  --output /tmp/recall-origin-smoke.json \
  --inspector-output /tmp/recall-origin-inspector.json
```

The artifact contains the corpus hash, raw ranked results, aggregate retrieval
metrics, latency observations, database sizes, engine configuration, Python and
platform details, and Git provenance. For the same `(scale, seed)`, corpus
content and labels are deterministic. Latency, throughput, timestamps, file
sizes, and generated identifiers can vary with hardware and runtime.

Supported larger scales are `10000`, `100000`, and `1000000`. Run only scales
you actually report. Large scales use one real public-API transaction per
memory and can require substantial time and disk space.

For the checked-in 10k release evidence, do not assemble the artifact,
Inspector projection, run timing, chart, and checksums with separate commands.
Start from an existing clean commit and use the fail-closed release wrapper:

```bash
test -z "$(git status --porcelain=v1 --untracked-files=all)"
uv run python scripts/benchmark_release.py
(cd docs/benchmarks/results && shasum -a 256 -c SHA256SUMS)
uv run pytest -q tests/benchmarks
```

The wrapper fixes the official configuration, stages all outputs outside the
repository, records wall/user/system timing plus uv and lock-file provenance,
validates the raw artifact and exact Inspector projection, derives the bounded
summary assets, and replaces `SHA256SUMS` last. It refuses a missing commit,
dirty checkout, Git/source change during measurement, failed quality suite,
stale Inspector payload, inconsistent run metadata, or incomplete checksum
set.

The resulting artifact normally records clean commit A. Commit the measured
bundle and synchronized release copy as commit B; the release gate verifies
that A is an ancestor of B and that the executable source fingerprint still
matches. If executable source or the lock file changes after the measurement,
run the official benchmark again from a new clean commit.

## Run reliability gates

The normal test suite covers transaction, migration, deletion, lease takeover,
and crash-path cases. The expensive replay gate verifies that repeated delivery
does not create duplicate visible state:

```bash
uv run python scripts/reliability_gate.py --iterations 10000
```

Its elapsed time is a local observation, not a benchmark claim.

## Verify GitHub release artifacts

After a GitHub prerelease exists:

```bash
gh release download v0.1.0a0 --dir dist/verified-v0.1.0a0
cd dist/verified-v0.1.0a0
sha256sum --check SHA256SUMS
repro_repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
gh attestation verify recall_origin-0.1.0a0-py3-none-any.whl \
  --repo "$repro_repo"
```

On macOS, use `shasum -a 256 -c SHA256SUMS` if `sha256sum` is unavailable.
An attestation proves which GitHub workflow built a digest; it does not replace
source review, dependency review, or runtime isolation.
