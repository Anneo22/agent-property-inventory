# Architecture

The project is a ledger and transaction engine, not an inventory application. Interfaces remain replaceable; canonical meaning stays in JSONL.

```text
agent or operator
    │
    ├── CLI ───────────────┐
    └── stdio MCP ─ proposal/read tools
                            │
                      shared CLI core
                            │
                canonical-path writer lock
                            │
                isolated mutation and verify
                            │
          durable journal + atomic file replacement
                            │
       private Data/store/*.jsonl + proposal receipt
              │                         │
       SQLite projection         immutable media bytes
              │
       scoped Markdown catalogue
```

## Durable layers

1. The inventory root owns canonical JSONL, hash-declared source inputs, and a format-2 runtime-binding marker. The marker records an `installation_id` and the chosen runtime path.
2. The media root owns content-addressed evidence bytes.
3. The catalogue path owns one atomic human-readable projection. Its default scope is `personal`; the rendered file contains a non-reversible digest of its installation owner.
4. The runtime owns SQLite, rollback backups, pending proposals, recovery journals and a reciprocal owner marker that repeats the instance topology and `installation_id`.
5. This repository owns behavior: schema, migration, transaction engine, renderer, verifier, CLI and MCP.

Canonical JSONL, declared auxiliary inputs, and referenced media must survive. SQLite and Markdown are rebuildable. The runtime is removable only after a clean status proves that no transaction, initialization, or restore journal is pending. Runtime backups are recovery aids, not an independent archival system. The root binding and reciprocal runtime marker are installation metadata, recreated for the destination during restore rather than carried as portable payload.

All resolved inventory, runtime, media and catalogue paths must be disjoint, except for the deliberate default catalogue at `inventory_root/Inventory.md`. Inventory, runtime and media roots must also not overlap a configured forbidden root. The catalogue may be inside a forbidden notes root, but cannot be that root or contain it. This keeps canonical JSON, SQLite, journals and media outside Obsidian while allowing one generated Markdown projection inside it.

## Write path

Every ledger mutation resolves one cross-process lock from the canonical inventory path, regardless of runtime directory. A media attachment also takes a media-root lock. Rendering takes a catalogue-output lock across owner validation and atomic replacement, so two processes cannot race between checking the owner digest and replacing the same catalogue.

Before an existing root is used, format-2 bindings must agree with the reciprocal runtime owner marker. A missing binding or legacy format-1 binding is ambiguous: `init` is the explicit adoption path, and reads never adopt it. Adoption verifies the complete bundle before committing format-2 ownership.

If bytes differ, the engine fsyncs a pre-write backup and a journal containing old and new hashes. It replaces changed files one by one, verifies the live generation, then removes the journal. A crash on any replacement boundary recovers only when the observed hashes prove a complete old or new generation. Unexpected bytes fail closed.

`init` uses its own forward-recovery journal. It stages and verifies the new root, records its manifest and intended catalogue digest, then publishes the root and verifies the live bundle. A later `init` either completes that recorded work or preserves bytes when the journal no longer proves them.

Restore writes a format-2 journal in `extracting` before it writes archive members. The archive manifest is preflighted first. On restart, the journal fixes the archive hash and expected staging layout; verified extracted prefixes are reused, while unexpected, changed or unjournalled private bytes stop recovery. Only after the staged inventory and media trees verify does restore publish them and the catalogue through its remaining journal phases.

## Read path

Search, item, status and compatibility reads use the same schema and recovery checks. They validate existing format-2 ownership but never create or upgrade ownership bindings. Scope filtering happens before serialization. Public and personal callers cannot distinguish a hidden item from a missing ID, receive hidden evidence IDs, or infer private totals from status.

## Extension boundary

Capture systems may crop images, read barcodes, extract labels or interpret floor plans. They should produce files and proposed claims, not bypass the ledger. New interfaces earn a core dependency only when they change canonical guarantees rather than merely improving capture convenience.
