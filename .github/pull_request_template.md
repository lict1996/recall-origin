## What changed

Describe the user-visible behavior and why this is the smallest coherent change.

## Evidence

List tests, commands, benchmark artifacts, or failure reproductions. Do not
include secrets, real memory content, personal data, or unredacted transcripts.

## Trust-boundary review

- [ ] I considered exact partition authorization.
- [ ] I considered deletion, purge, restore, and external-copy boundaries.
- [ ] I treated stored and recalled content as untrusted input.
- [ ] I introduced no new telemetry or sensitive logging.
- [ ] These items are not applicable, and I explained why below.

## Compatibility

- [ ] Python, CLI, MCP, and HTTP parity is preserved or tested where applicable.
- [ ] Contracts and generated files are updated where applicable.
- [ ] Existing migrations were not rewritten.
- [ ] User-visible behavior is recorded in `CHANGELOG.md`.

## Verification

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy --strict src scripts`
- [ ] `uv run --extra mcp python scripts/generate_mcp_contract.py --check`
- [ ] `uv run pytest --cov=recall_origin --cov-branch --cov-report=term-missing`

Additional notes or justified exceptions:
