# Agent-first roadmap completion plan

## Finish line

The agent-first v0.1.1 product is complete: schema-v6 source and tests cover the
implementable ledger, the named private instance has migrated from v4 to v6,
and the public boundary has clean-history, wheel, platform-matrix, scan, and
independent-review evidence without publishing private data. Repeated catalogue
renders also preserve Obsidian-managed creation metadata without dirtying the
vault.

## Binding boundaries

- Reusable code, private canonical data, immutable media, runtime state, and
  the generated Obsidian catalogue are separate non-overlapping roots.
- JSONL and declared auxiliary inputs are canonical; SQLite and Markdown are
  rebuildable projections.
- Obsidian receives only a scope-limited generated catalogue. High-sensitivity
  rows remain CLI-only.
- Extraction, passive capture, and generic import propose or support evidence;
  they never silently establish identity, ownership, location, quantity,
  condition, value, compatibility, or a physical check.
- Direct current physical-check imagery is required for insurance photos.
- An unknown lifecycle date is null `occurred_on` with required `observed_on`
  and `unknown` precision.
- Local and replica writes are explicit transactions. No sync transport uses
  last-write-wins to resolve a real-world semantic conflict.

## Completed source batches

1. **Completion contract and boundaries.** Configuration, non-overlap checks,
   atomic catalogue projection, declared inputs, and backup/restore coverage.
2. **Schema and migration.** v1-v5-to-v6 policy, lossless-forward migration
   coverage, malformed/future schema rejection, and recovery before migration.
3. **Retrieval and spatial reasoning.** Scope-first retrieval, checked local
   geometry, item dimensions, fit, free volume, and deterministic packing.
4. **Capture and insurance.** Bounded adapter protocol, reviewable capture,
   direct current physical-check insurance photos, and deterministic packages.
5. **Replica and ecosystem operations.** Explicit sync conflicts, proposal-only
   import, export/restore doctor, and executable compatibility policy.
6. **MCP and upkeep.** Read-only default, proposal-only private MCP profile,
   condition-aware operational answers, and synthetic upkeep aggregation.

## Completed release evidence

12. **Release acceptance.**
    Verified by the named private v4-to-v6 migration and retained backups;
    post-migration rebuild, render, semantic and foreign-key checks; an
    export-and-blank-restore doctor drill; live CLI and MCP probes; all 470
    source tests; clean sdist and wheel builds; the eight-cell public CI matrix;
    full-history boundary and secret scans; and independent adversarial review.
    The release boundary excludes private data, media, runtime state, and these
    internal plans.

## Resume protocol

Product work resumes only for a reproduced defect or evidence from real use.
The active next step lives in the Property Inventory verification note: name
one area or container and supply one overview photo.
