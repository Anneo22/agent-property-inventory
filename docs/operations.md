# Operations and recovery

## Health and compatibility

```bash
property-inventory status
property-inventory compatibility-status
```

`status` validates declared auxiliary inputs, rebuilds SQLite, atomically
renders the configured catalogue, runs the semantic verifier, and runs
`PRAGMA foreign_key_check`. A successful mutation performs the same checks
before it returns.

`compatibility-status` reports the executable Python floor and every explicit
v1-v5-to-v6 action. It does not report that an inventory has been migrated.
Future schemas are refused until the policy is deliberately extended.

## Boundaries and configuration

One configuration names independent inventory, media, runtime, and catalogue
roots. Command-line values override environment values, which override the
config file. Forbidden roots are additive.

```json
{
  "version": 1,
  "default_instance": "example",
  "instances": {
    "example": {
      "inventory_root": "/path/to/private/inventory",
      "runtime_dir": "/path/to/private/runtime",
      "media_root": "/path/to/private/media",
      "catalogue_output": "/path/to/notes/Inventory.md",
      "catalogue_scope": "personal",
      "forbidden_roots": ["/path/to/notes", "/path/to/source"]
    }
  }
}
```

Inventory, runtime, and media must not overlap each other or a forbidden root.
The catalogue can be the deliberate projection in a notes root, but cannot be
that root or contain it. Keep JSONL, auxiliary inputs, SQLite, journals, and
media outside Obsidian; expose only the generated catalogue there.

New inventories receive a format-2 root binding and reciprocal runtime owner
marker. Reads and ordinary mutations fail closed when those bindings are absent,
legacy, or inconsistent. `init` is the explicit adoption path. Do not create,
edit, or delete a marker by hand.

When an intentional filesystem rename changes the runtime, media, or catalogue
path, use the private `runtime-rebind` transaction and name the paths currently
recorded by the owner marker. The command refuses pending recovery work and
prepared proposals, verifies the old runtime, rebuilds at the configured new
catalogue path, and updates the reciprocal binding without editing a marker by
hand:

```bash
property-inventory --scope private runtime-rebind \
  --from-runtime /old/runtime \
  --from-media-root /old/media \
  --from-catalogue-output /old/notes/Inventory.md
```

## Evidence, corrections, and operational advice

Use a physical-check command only for a real current check. It creates the
required `physical_check` / `explicit_current` evidence and matching lifecycle
event. A passive overview capture or an import must not be used as a substitute.

An unknown historical event date is recorded with a null `occurred_on`,
`occurred_on_precision: unknown`, and the required `observed_on`. The observation
date is not a guessed occurrence date. For an exact historical event,
`--observed-on` may record a later review or report date; it defaults to the
occurrence date and cannot precede it.

Use `restore-current-ownership --reason ownership_corrected` only when the
terminal record was wrong. Use `--reason reacquired` for a real new ownership
episode. Reacquisition preserves the item ID, serial and history, but resets old
condition, acquisition, purchase and receipt details. A previously known
quantity must be supplied again. Omit condition when function was not checked;
the operational answer will remain unknown. Add later acquisition or receipt
facts through `enrich-item` with their own evidence.

v6 corrections preserve their predecessor. Use identity correction for a model
mistake, `enrich-item` for supported item details, and `amend-fact` to replace
or retract a current durable fact. All need evidence, actor, and amendment date.
Append item dimensions instead of overwriting a prior measurement.

```bash
property-inventory add-item-dimensions --item-id itm-example \
  --width 120 --height 30 --unit mm --measured-on 2026-08-06 \
  --evidence-id ev-example --sensitivity personal
property-inventory review-kit --actor "Example reviewer" --kit-id kit-example \
  --reviewed-on 2026-08-06 --completeness incomplete \
  --source-ref "Checked example requirement list"
```

Fit, packing, kit status, torque checks, and compatibility expose a separate
operational result. Current custody plus a usable condition is available; an
explicitly unusable condition is unavailable; an unknown condition stays
unknown. Do not interpret a possession record alone as safe operating advice.

## Overview capture and insurance

`capture-prepare` stages one real overview image and deterministic crops in
private runtime state. It does not write canonical JSONL or durable media.
`capture-review` seals explicit observation-to-item links and may also seal a
fully specified physical or discovery decision into a proposal. A passive link
records supporting evidence only. A reviewed physical/discovery decision must
bind its crop, matching observation, identity, location, date, quantity,
condition and other claimed fields; application replays those exact fields
through the ordinary transaction writer. Nothing becomes canonical during
capture preparation or review.

The optional named adapter comes from a server-owned startup registry. Tool
calls cannot provide commands. The implementation validates the protocol and
bounds its outputs, but configured adapters are trusted local programs and are
not sandboxed.

For insurance, a photo is present only when a directly linked image belongs to
that item's `physical_check` / `explicit_current` evidence. Capture artifacts,
candidate matches, and sealed capture-review links do not qualify. The package
workflow is private because it can contain private values, locations, serials,
receipts, and media:

```bash
property-inventory --scope personal insurance-status
property-inventory --scope private insurance-export --output /path/to/private/insurance.zip
property-inventory --scope private insurance-validate --package /path/to/private/insurance.zip
```

Receipt and appraisal roles are not labels of convenience. `attach-media`
decodes claimed images and strictly parses claimed PDFs before accepting them.
A receipt requires purchase-only merchant-account or user-source evidence. An
appraisal requires reviewed research-only user-source or vault-note evidence;
an appraisal value must cite that evidence, and readiness also requires the
appraisal document linked with role `appraisal`. If any part is absent or the
canonical bytes later disagree with their hash, size, or declared format, the
field stays `unknown` and package export fails closed.

An internally valid package proves only its recorded bytes and evidence, not
coverage or a settled claim.

## Transactions, backup, and restore

Every changed transaction writes a timestamped verified pre-write backup under
the runtime root before canonical replacement. An interrupted transaction has a
journal. The next read or write accepts only a provable complete generation,
rolls back a provable partial generation, and refuses unexpected bytes. Do not
delete a marker, journal, or workspace to force progress.

`doctor` performs an export and blank restore into fresh temporary roots with an
isolated catalogue. The archive path must be new and outside every managed or
forbidden root. The archive is retained; temporary restore roots are cleaned on
both success and failure.

```bash
property-inventory --scope private doctor --output /path/to/private/inventory-drill.tar.gz
```

Restore preflights the archive before extracting private bytes. It rejects links,
path traversal, unexpected members, identity disagreement, and row or media
hash mismatches. Interrupted extraction and installation are journalled and may
resume only from their verified prefix. Old format-1 archives require explicit
unsafe-legacy acceptance and remain in a degraded quarantine that blocks later
mutation and export.

## Offline replica sync

Replica sync is local, content-bound file handling. It needs a trusted base
copy; only disjoint identities merge automatically. There is no timestamp
winner and no last-write-wins behaviour. If both heads changed one current item
or mutable fact, committed canonical history cannot be deleted: the only safe
sync resolution keeps the canonical branch. Any still-valid replica intent must
then be recorded through a fresh canonical command with current evidence. The
plan says this explicitly as `reconciliation_required` instead of offering an
impossible branch choice.

Replica-only retractions are canonical-only conflicts too: the plan exposes
only `canonical` and the same fresh-canonical-transaction instruction. A bundle
does not transport all media. Its deterministic `<bundle>.media` sidecar carries
only digests newly referenced after the trusted base. Prepare requires an exact,
bounded sidecar and rejects missing, extra, path-traversing, symlinked, oversized,
tampered, digest-, size-, or MIME-conflicting bytes before private staging.
If both sides independently allocate the same immutable ID and replica rows
depend on the losing meaning, the plan has no admissible choice. Re-ID that
replica transaction against the canonical identity and prepare a new bundle;
silently rebinding its dependants would corrupt meaning.

An existing-item branch may also have no admissible choice when dependent
relationships, model interfaces, capture rows, maintenance rows, or same-item
lifecycle history cannot survive the selected identity or event branch. Rebase
that complete transaction. When a branch is explicitly rejected, only evidence
and media owned exclusively by that losing branch are pruned; unrelated support
evidence and bytes survive.

Ready plans are rebuilt and verified in a temporary sandbox before save and
again under the canonical transaction lock on apply. Application writes a
high-sensitivity transport receipt only. That receipt does not make an
ownership, possession, location, quantity, condition, or lifecycle claim.

For an existing item, the replica boundary replays every newly appended event
in sequence from the trusted base row. It checks legal source states, exact
location effects, quantity predecessor/result payloads, and physical evidence
dates. Every replica event must be a contiguous append with newly captured,
observation-date-matched evidence attached to the item. Reacquisition also
requires its episode-reset declaration, reset amendment, and any current
condition or quantity reaffirmation. Replica-only items must replay from
a real planned, ordered, or physically checked creation transaction, and a
replica cannot manufacture canonical sync receipts. An unrelated or merely
plausible event cannot authorize a direct JSONL edit. Planned and ordered
quantity adjustments remain purchase-only; they do not become
current-possession quantity events.

Fact, model, and item-detail amendments may form an ordered offline chain, but
each hop must name the exact predecessor and the chain must replay to the head.
Gaps, branches, and reordered hops fail closed. Location
serialization is deterministically parent-before-child, so a lexical child ID
cannot make a valid replica fail SQLite foreign-key loading.

There is intentionally no sync listener, peer discovery, HTTP endpoint,
`sync-pull`, or `sync-push`. Move a protected bundle through a deliberate
separate process.

## Repair rule

Fix data or code when a check fails. Never edit SQLite or generated Markdown,
remove a constraint, lower a policy baseline, or hand-edit a journal to make a
verification result pass.
