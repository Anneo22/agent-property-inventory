# ADR 0001: Separate code, canonical data, and runtime projections

> Superseded in part by [ADR 0003](0003-keep-the-private-ledger-outside-obsidian.md): the catalogue and evidence media are independent paths, not children of the canonical inventory root.

## Status

Superseded by [ADR 0003](0003-keep-the-private-ledger-outside-obsidian.md)

## Context

The first working inventory kept Python, SQL, JSONL, SQLite, exports, and readable notes in one Obsidian Garden folder. It was convenient for one session and wrong as a durable boundary. Executable and generated files caused vault churn, tied the implementation to one private dataset, and made an eventual open-source release inseparable from personal records.

The inventory still needs one user-owned source of truth, a human-readable catalogue, transactional updates, rollback backups, and a fast relational query surface.

## Decision

Keep three explicit layers:

1. The inventory root owns durable private state: canonical `Data/store/*.jsonl`, optional migration evidence and dataset-specific verification policy.
2. This repository owns reusable behavior: schema, CLI, renderer, verifier, migration helper, tests, and documentation.
3. A gitignored runtime directory owns disposable or operational state: SQLite, transaction journals, proposals, and rollback backups. Export destinations are caller-selected and must sit outside every managed namespace.

The CLI resolves independent inventory, media, catalogue, and runtime paths. No code path assumes a particular home directory or vault. SQLite and Markdown remain projections of JSONL, and the transaction path rebuilds and verifies both before accepting a mutation.

## Consequences

- A private inventory can stay in a notes vault without turning that vault into an application repository.
- The same CLI can operate on isolated test fixtures or another user's dataset.
- Personal baseline assertions live in `verification_policy.json` instead of reusable Python.
- Removing the runtime directory loses caches and local backups, not canonical state. Backups still need an independent retention policy if they matter beyond rollback.
- The stdio MCP calls this transaction layer rather than creating a second write path.
- The split adds two paths to configure. Environment variables and agent skills should hide that routine detail from users.

## Review trigger

Revisit this decision only if a real deployment requires concurrent remote writers or JSONL can no longer satisfy deterministic rebuild and review. Do not collapse the layers merely to reduce the number of directories.
