# Contributing to RecallOrigin

Thank you for helping improve RecallOrigin. The project favors small,
evidence-backed changes over broad feature accumulation.
Participation is governed by `CODE_OF_CONDUCT.md`.

## Before opening a change

- Use an issue for behavior changes or substantial design work.
- Use a private security advisory for vulnerabilities; do not open a public
  security report.
- Search existing issues and pull requests before starting duplicate work.
- Keep the `0.1.x` boundary in mind: this is a local, single-node SQLite runtime,
  not a distributed memory service.

## Development setup

Install Python 3.11 or 3.12 and
[uv](https://docs.astral.sh/uv/). CI currently pins uv `0.12.5`.

```bash
git clone https://github.com/lict1996/recall-origin.git
cd recall-origin
uv sync --locked --all-extras --dev
```

The core engine works without network services or model credentials. Optional
MCP and HTTP dependencies are included by `--all-extras`.

## Required checks

Run these from the repository root:

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src scripts
uv run --extra mcp python scripts/generate_mcp_contract.py --check
uv run pytest --cov=recall_origin --cov-branch --cov-report=term-missing --cov-report=xml
```

The release-only 10,000-replay gate is intentionally slower:

```bash
uv run python scripts/reliability_gate.py --iterations 10000
```

See `REPRODUCING.md` for package, benchmark, and artifact verification.

## Design and compatibility rules

### Ledger and migrations

- Treat events, claims, revisions, and evidence links as audit records. Avoid
  in-place history rewrites.
- Never edit a migration that may already have been applied. Add a new,
  forward-only migration and a migration regression test.
- Test crash recovery, retries, and deletion non-resurrection when changing
  transaction or job boundaries.

### Authorization and deletion

- Use exact partition matching. A namespace or origin identifier is not an
  authorization shortcut.
- Add a negative cross-partition test for every new read path.
- Keep engine-managed purge behavior separate from external exports, backups,
  logs, and provider copies.
- Never put memory content, evidence content, personal data, or direct
  identifiers into retrieval telemetry.

### Interfaces and contracts

- Python, CLI, MCP, and HTTP are adapters over one application core. Do not
  duplicate business rules in an adapter.
- Preserve the CLI's single JSON stdout envelope for machine-readable commands.
  Send diagnostics to stderr.
- Update the relevant contract and parity tests with interface changes.
- `contracts/mcp-tools.json` is generated from the SDK surface. Change the
  implementation or generator, then regenerate it; do not hand-edit drift.

### Dependencies and workflows

- Add a dependency only when it materially improves the core product. Keep
  optional integrations optional.
- Update both `pyproject.toml` and `uv.lock`.
- GitHub Actions are pinned to immutable commit SHAs. Do not replace them with
  floating branch or major-version references.

## Pull requests

A pull request should:

1. solve one coherent problem;
2. explain the user-visible behavior and trust-boundary impact;
3. include tests that would fail without the change;
4. update contracts, ADRs, or user documentation when applicable;
5. avoid unrelated formatting or refactors; and
6. add a changelog entry for user-visible behavior.

Benchmark numbers must include the generated artifact and exact configuration.
Do not describe a synthetic smoke run as a production benchmark, infer an
unmeasured vector baseline, or claim SOTA without independent evidence.

By contributing, you agree that your contribution is licensed under the
Apache License 2.0 in `LICENSE`.
