# Agent-first v2 plan

> Superseded on 2026-08-06 by [the roadmap completion plan](roadmap-completion.md) and its JSONL ledger. This file preserves the completed v2 contract and evidence; it is not the current resume point or feature boundary.

## Finish line

A clean installation exposes a versioned CLI and least-privilege local MCP server; reviewable proposals apply through one crash-recoverable transaction path; export and blank restore preserve identifiers and media hashes; and the migrated private inventory passes rebuild, render, semantic verification, foreign keys, scoped queries and MCP acceptance with zero failures.

## Starting evidence

The v1 lifecycle suite passes 4/4. An isolated cold-agent acceptance run completed planning, ordering, physical receipt, relationship recording, an area absence and a sale without a failed verification cycle. The live private inventory contains 200 item rows and currently passes semantic verification and `PRAGMA foreign_key_check`. Phase 0 also confirmed that v1 can tear a multi-file commit on process death, places its writer lock under a caller-selected runtime directory, has no schema version, permits invalid lifecycle transitions, stores but does not enforce sensitivity, and cannot persist real document bytes through the CLI.

## Architecture

The CLI and MCP adapter call the same importable query, proposal and transaction functions. Versioned JSONL remains canonical. SQLite and Markdown remain disposable projections. Immutable media bytes live outside the notes vault in a configurable content-addressed directory; canonical JSONL stores hashes, metadata and `media://` references. Proposals live under the runtime directory, carry the canonical store digest they were prepared against, and apply every operation in one transaction.

The transaction engine owns the complete write boundary: resolve one lock from the canonical inventory path, recover an interrupted transaction, back up the current store, stage and verify the complete new store on the same filesystem, durably record commit intent, replace the store, rebuild and verify the live projections, and either finish or restore. No adapter may write JSONL directly.

## Package and files

- `pyproject.toml` defines the package, Python support, console scripts, optional MCP dependency and development tools.
- `src/property_inventory/` owns the CLI, store, transactions, lifecycle operations, migrations, media, proposals, compatibility, renderer, verifier and MCP adapter.
- Existing root scripts remain small compatibility wrappers until downstream calls use the installed commands.
- `tests/` adds crash, contention, migration, sensitivity, media, export, proposal, compatibility, packaging and MCP acceptance coverage while retaining the existing lifecycle characterisation tests.
- `docs/` explains architecture, schema evolution, MCP profiles, threat boundaries, recovery and export.
- The private Property Inventory skill is updated only after the installed CLI is proven.

## Dependencies

- `mcp>=2,<3` is an optional extra and supplies the standard protocol, structured outputs and in-memory client tests.
- `filelock>=3,<4` supplies a maintained cross-process lock instead of another hand-written platform branch.
- Hatchling builds the package. Ruff remains development-only.

No OCR, vision, vector database, web framework, remote service or custom cryptography enters the core.

## Schema v3

`metadata.jsonl` carries the inventory identity and schema version. `media_assets.jsonl` and `evidence_assets.jsonl` bind immutable bytes to evidence, including optional image regions. `interfaces.jsonl` and `model_interfaces.jsonl` carry normalized, evidence-bearing compatibility claims while preserving legacy free-text interfaces. Events gain stable sequence numbers; unknown historical recording timestamps remain unknown. `proposal_commits.jsonl` gives every applied proposal a canonical crash-recovery receipt. Schema v3 supersedes the earlier v2 implementation without stranding it: both v1 and v2 migrate forward, backed up and verified, while an older binary still refuses a newer schema.

## Access contract

The generic MCP server starts read-only and exposes only search, get, compatibility, proposal inspection and status. A separately enabled write profile adds proposal preparation and application. Field and sensitivity filters run in the shared serialization layer, so CLI and MCP cannot disagree about redaction. The trusted private profile may explicitly include high-sensitivity locations; generic profiles do not.

## Edge cases

- A process death during commit recovers to a complete old or complete new store.
- Writers using different runtime directories still contend on one lock.
- A stale proposal or unsupported schema fails without mutation; a post-commit crash is recovered from its canonical receipt.
- Any invalid operation in a proposal rolls back the entire proposal.
- Missing, corrupt or cloud-evicted media makes verification and export fail with the exact asset named.
- Compatibility without sufficient normalized evidence returns `unknown`.
- Purchase evidence never confirms possession, and absence from one area never asserts loss or disposal.
- Invalid lifecycle resurrection is rejected unless a future explicit correction path exists.
- Restore rejects unsafe archive paths and non-empty targets.
- Unknown values remain unknown throughout migration, rendering and export.

## Test and release gates

The suite covers current lifecycle behaviour, injected crashes at every commit boundary, two writers with different runtimes, v1/v2-to-v3 and future-schema refusal, media hashing and corruption, proposal atomicity, compatibility outcomes, read redaction, real stdio MCP calls, wheel installation, and export-to-blank-restore identity. CI runs on supported Python versions across Ubuntu and the macOS target. Every batch must pass its focused tests plus the full suite, update the ledger and CHANGES.log, then commit and push. The repository remains private; publication requires a separate machine-run publish audit and user decision.

## Execution order

1. Persist this plan and its machine-readable ledger.
2. Package the project and consolidate the shared in-process core without changing behaviour.
3. Repair transaction durability, writer exclusion and lifecycle transition correctness.
4. Add schema migration and scoped reads.
5. Add durable media plus verified export and restore.
6. Add typed compatibility and atomic proposals.
7. Add and test the stdio MCP adapter.
8. Migrate and verify the private inventory, then update the skill and project notes.
9. Run an independent adversarial review, clean-install acceptance and final private release checks.

## Non-goals

No web or mobile interface, automatic vision assertion, barcode or OCR subsystem, remote HTTP MCP, multi-user service, spatial packing solver, insurance claim generator, custom encryption or public release is included.
