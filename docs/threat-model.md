# Threat model

## Protected properties

- Canonical rows are not partially written or silently lost.
- Purchase history is not promoted into possession.
- Unknown compatibility, location, quantity and identity are not invented.
- Private locations, serials, evidence and totals are not returned through a lower scope.
- Referenced evidence bytes retain identity and sensitivity.
- A generic MCP client cannot discover or call mutation tools.

## In scope

- Process death on every multi-file replacement boundary.
- Two writers selecting different runtime directories.
- Stale or malformed proposals.
- Unsupported future schemas.
- Missing, corrupt or cloud-evicted media.
- Archive traversal, links and non-empty restore targets.
- Hidden-ID and aggregate-count oracles across scopes.
- Accidental lifecycle resurrection and duplicate candidate creation.

## Out of scope

- A malicious local user who can edit the repository, private JSONL and runtime together.
- Encryption at rest, device compromise and operating-system access control.
- Authenticated remote or multi-tenant service operation.
- Adversarial image recognition. Extracted labels remain proposals until evidence is checked.
- Backup retention after loss of both canonical data and runtime.

## Deployment rules

Keep private data out of the code repository, use filesystem permissions appropriate to its sensitivity, run MCP over local stdio, and expose the narrowest scope. A public repository should contain no home paths, email addresses, private identifiers or sample personal data; `scripts/check-public-leaks.sh` enforces the first two mechanically.
