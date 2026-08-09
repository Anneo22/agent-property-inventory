# ADR 0003: Keep the private ledger outside Obsidian

## Status

Accepted

## Context

The first private inventory lived under an Obsidian Garden note because that was where the prototype started. The JSONL was never useful as a note. Its location made Obsidian index machine state, mixed private serial and location data with a readable catalogue, and tied a reusable tool to one person's vault. Moving the Python elsewhere fixed only half of that mistake.

The ledger, evidence bytes, generated catalogue, runtime, and reusable code have different readers and different failure modes. Treating them as one folder makes every permission wider than it needs to be.

## Decision

Use five independent paths:

1. The inventory root holds the private canonical JSONL ledger, declared source inputs, and a format-2 runtime-binding marker. The marker records the local `installation_id` and runtime path used for crash recovery. It is a private repository or equivalently protected directory.
2. The media root holds immutable, content-addressed evidence bytes.
3. The catalogue output is an atomic, generated Markdown projection. It may live in Obsidian, but its default scope is `personal`, so high-sensitivity rows and private fields stay out. Its hidden owner digest prevents a different installation from taking over the same output; replacement is serialized by a cross-process catalogue lock.
4. The runtime holds rebuildable SQLite, rollback copies, transaction, initialization and restore journals, pending proposals, temporary work, and a reciprocal owner marker for the installed root.
5. This repository holds reusable code, tests, schemas, and documentation. It contains no private inventory data.

The per-user JSON config in the operating system's application-support directory names these paths. Command-line arguments override environment variables, which override config values. Inventory, media, runtime and catalogue paths must not overlap, except for the deliberate default catalogue under the inventory root. Inventory, media and runtime may not overlap code or vault forbidden roots. A generated catalogue may sit inside a forbidden vault because it is the only intended machine-written note there, but it cannot be or contain that root.

An existing root is never claimed by a read. A missing or legacy binding is adopted explicitly through `init`, which verifies the bundle before committing the format-2 binding and reciprocal runtime marker. Media attachment and restore use a media-root lock; ledger writes and restore use a canonical-root lock. These locks serialize cooperating local processes, rather than promising anything about external filesystem behaviour.

Every non-store input under `Data/` is declared in a hash manifest. Health checks, exports, and restores fail if a file is missing, changed without review, undeclared, or reached through a symlink. Restore preflights the archive before extracting private bytes and records a durable `extracting` journal first. A restart can use only a verified staging prefix; unexpected private bytes are preserved for inspection. A retired inventory root contains a marker that makes old commands fail. It never redirects writes through a symlink.

## Consequences

- Obsidian contains notes and one readable projection, not canonical JSONL, SQLite, raw imports, transaction state, or evidence blobs.
- Canonical JSONL, declared auxiliary inputs and referenced media are durable. The catalogue and SQLite are rebuildable projections.
- Removing the catalogue loses no canonical information. A quiescent runtime can also be rebuilt, but deleting one with a pending transaction, initialization or restore journal destroys the recovery proof and is forbidden.
- A complete export contains the ledger, every declared source input, and every referenced media byte.
- The private data repository and media backup need their own retention and access policy. Git history is useful recovery for JSONL, but it does not protect uncommitted media or replace an independent backup.
- A caller that asks for a private catalogue must do so explicitly. The safe default is `personal`.
- The root binding and reciprocal runtime marker are operational metadata, not canonical payload. Export and restore recreate them for the destination runtime.
- New inventory roots include a `.gitignore` rule for the machine-specific runtime marker. A clone therefore binds itself to its local runtime on first use instead of inheriting another machine's absolute path.

## Review trigger

Revisit this boundary if a real deployment needs several concurrent writers or a remote service. Keep the separation of readers even if the storage engine changes.
