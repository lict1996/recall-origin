# RecallOrigin MCP stdio integration

RecallOrigin runs as a local MCP stdio child process. The process identity and
its exact authorization partitions come from trusted startup arguments, not
from tool input.

## Install a release in a dedicated environment

```bash
python3.12 -m venv .recall-origin-runtime
.recall-origin-runtime/bin/python -m pip install \
  "recall-origin[mcp] @ https://github.com/lict1996/recall-origin/releases/download/v0.1.0a0/recall_origin-0.1.0a0-py3-none-any.whl"
.recall-origin-runtime/bin/python -m recall_origin.interfaces.mcp --help
```

Use the absolute paths printed by these commands in your host configuration:

```bash
realpath .recall-origin-runtime/bin/python
mkdir -p .recall-origin-data
.recall-origin-runtime/bin/recallctl init \
  --db "$(pwd)/.recall-origin-data/memory.sqlite3"
realpath .recall-origin-data/memory.sqlite3
```

Source contributors can replace the wheel install with
`python -m pip install -e '.[mcp]'` from a checkout.

## Configure one host

Start with one host, one MCP process, one database, and one managed pack root.
The alpha does not support independent MCP, HTTP, Python, or worker processes
sharing a store concurrently.

Copy and replace the absolute paths in:

- [`codex.toml`](codex.toml) for Codex;
- [`claude_desktop_config.json`](claude_desktop_config.json) for Claude
  Desktop.

The equivalent server command is:

```bash
/absolute/path/to/venv/bin/python \
  -m recall_origin.interfaces.mcp \
  --db /absolute/path/to/recall-origin.sqlite3 \
  --tenant-id local \
  --principal-id coding-agent \
  --partition workspace:example-project
```

Restart the host and verify that it discovers `memory_put`, `memory_search`,
`memory_context`, `memory_get`, and `memory_feedback`. Logs and startup
diagnostics go to stderr so stdout remains reserved for MCP JSON-RPC.

## Complete the default trust lifecycle

An Agent cannot mint human confirmation. Its
`memory_put(mode="remember", ...)` response contains a `claim_id` and
`revision_id`, but the new revision remains an `unverified` candidate and is
intentionally absent from ordinary `memory_context`.

1. Call `memory_search` with the same scope and
   `include_candidates=true`; inspect the candidate and evidence.
2. Stop the host/MCP process before opening the same store with the CLI.
3. Use the installed `recallctl` as a trusted local human:

   ```bash
   /absolute/path/to/venv/bin/recallctl govern <claim_id> \
     --expected-revision-id <revision_id> \
     --action confirm \
     --reason "Reviewed against the cited source." \
     --db /absolute/path/to/recall-origin.sqlite3
   ```

4. Restart the host. Ordinary `memory_search` and `memory_context` now return
   the active, `user_confirmed` revision.

For an operator-authored fact, stop the MCP process and use
`recallctl remember`; that local-human path creates an active revision
directly. Enabling `govern` or `--privileged` gives the Agent a mutation tool
but does not let an Agent issue `user_confirmed` confirmation.

All memory returned by tools and resources is untrusted historical data. It
never grants permissions or changes instruction precedence.
