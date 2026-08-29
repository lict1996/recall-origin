# Security Policy

RecallOrigin stores material that may be sensitive and later places selected
material into an agent's context. Security reports are treated as data-boundary
reports, not only as conventional code-execution reports.

## Support status

RecallOrigin is alpha software. Security fixes may require breaking changes.

| Version | Status |
| --- | --- |
| Latest `0.1.x` GitHub prerelease | Supported on a best-effort basis |
| `main` | Development branch; reports are accepted |
| Older prereleases and untagged snapshots | Not supported |

There is no long-term-support release. A package with the same name obtained
outside this repository's GitHub Releases should not be assumed to be an
official RecallOrigin artifact.

## Reporting a vulnerability

Use **Security → Report a vulnerability** in this GitHub repository. This opens
a private security advisory visible only to repository maintainers. Include:

- the affected version or commit;
- the interface involved (Python, CLI, MCP, or HTTP);
- a minimal, redacted reproduction;
- expected and observed authorization partitions;
- the security or privacy impact; and
- any suggested mitigation.

Do not attach real memories, database files, API keys, tokens, personal data, or
unredacted agent transcripts. If private vulnerability reporting is unavailable,
open a public issue containing only a request for a private contact channel.
Do not disclose vulnerability details in that issue.

Maintainers aim to acknowledge a report within seven calendar days and provide
a triage decision within fourteen days. These are response targets, not a
service-level agreement. Disclosure timing will be coordinated with the
reporter after a fix or mitigation is available.

## High-value report classes

Examples include:

- cross-partition reads or writes;
- authorization bypass in any interface adapter;
- deleted data becoming visible after restore, reindex, or job replay;
- Evidence Pack path traversal, active-content injection, or integrity bypass;
- query, prompt, or stored-memory injection that crosses a documented trust
  boundary;
- unsafe deserialization, SQL injection, or arbitrary file access;
- denial of service that corrupts the authoritative ledger; and
- compromised release artifacts, dependencies, or GitHub Actions.

Reports that require an attacker to already control the same OS account and
arbitrarily modify both the database and the running process may fall outside
the project's security boundary, but integrity weaknesses are still useful to
report.

## Trust and data boundaries

- The local SQLite database is not encrypted at rest by RecallOrigin. Protect it
  with OS permissions and full-disk encryption when it contains sensitive data.
- A caller must use the intended authorization partition and protect its
  principal credentials. A partition is a security boundary, not a display
  label.
- Memory and evidence content is untrusted input. Hosts must not treat recalled
  text as system instructions or authorization.
- Purge covers engine-managed state. Exported Evidence Packs, copied databases,
  logs, backups outside the managed restore flow, and third-party provider
  copies are separate data controllers and are not remotely revoked.
- MCP stdio inherits the privileges of its host process. HTTP deployments must
  add transport security, authentication, rate limiting, and network isolation
  appropriate to their environment.
- RecallOrigin `0.1.x` is a single-node SQLite runtime. It does not claim
  distributed high availability or hardened multi-tenant isolation.

## Release integrity

The release workflow is designed to publish wheels and source distributions
only from version-matching `v*` tags whose commits are reachable from `main`.
GitHub Releases include SHA-256 checksums and an SPDX SBOM. GitHub build
provenance attestations are generated with short-lived OIDC credentials; no
long-lived publishing credential is required.

See `REPRODUCING.md` for verification commands.
