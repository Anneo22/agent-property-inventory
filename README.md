# property-inventory

[![Checks](https://github.com/Anneo22/agent-property-inventory/actions/workflows/checks.yml/badge.svg)](https://github.com/Anneo22/agent-property-inventory/actions/workflows/checks.yml)

`property-inventory` is a local-first, evidence-backed ledger for physical
property. It separates a product model from a physical unit, a purchase from
current possession, and a compatible interface from a merely similar object.

The current canonical schema is v6. The implemented core is covered by source
and integration tests. That is not a claim of real-world adoption, insurer
acceptance, or effortless upkeep.

## Why I built this

One bike-parts session removed four of seven items from my cart because the
inventory showed that I already owned the equivalents: Torx bits, hex bits,
tyre levers, spare valves, and the valve extender I nearly bought again. It
also exposed the opposite failure: an expensive wheelset was missing while a
sold wheelset was still recorded as owned. A useful agent needs a reliable
bridge to the physical world, not another shopping list.

## Why I built this

One bike-parts session removed four of seven items from my cart because the
inventory showed that I already owned the equivalents: Torx bits, hex bits,
tyre levers, spare valves, and the valve extender I nearly bought again. It
also exposed the opposite failure: an expensive wheelset was missing while a
sold wheelset was still recorded as owned. A useful agent needs a reliable
bridge to the physical world, not another shopping list.

## What is durable

- `Data/store/*.jsonl` is canonical. SQLite and Markdown are rebuildable
  projections.
- Evidence media is immutable and content-addressed outside the ledger.
- Runtime state, backups, journals, proposals, and SQLite live separately from
  canonical data.
- Obsidian receives only a generated, scope-limited catalogue. It is not a
  store for JSONL, recovery state, or media.
- Every canonical mutation is locked, backed up, staged, rebuilt, rendered,
  semantically verified, and foreign-key checked before replacement.
- Unknown remains unknown. An empty query is not proof of absence.

```text
inventory-root/                 canonical ledger and declared inputs
├── Data/store/*.jsonl
└── .property-inventory-runtime.json

media-root/sha256/...           immutable evidence bytes
runtime-root/                   SQLite, backups, journals and proposals
notes-root/Inventory.md         generated, scope-limited catalogue
```

The roots must not overlap. Keep canonical data, runtime, and media outside
Obsidian; only the generated catalogue may be placed there.

## Install and initialise

Python 3.11 or newer is required.

```bash
git clone https://github.com/Anneo22/agent-property-inventory.git property-inventory
cd property-inventory
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,mcp]'

demo_root="$(mktemp -d "${TMPDIR:-/tmp}/property-inventory-demo.XXXXXX")"
export PROPERTY_INVENTORY_ROOT="$demo_root/example-inventory"
export PROPERTY_INVENTORY_RUNTIME="$demo_root/example-runtime"
export PROPERTY_INVENTORY_MEDIA_ROOT="$demo_root/example-media"
export PROPERTY_INVENTORY_CATALOGUE_OUTPUT="$demo_root/example-notes/Inventory.md"

property-inventory init
property-inventory status
```

`init` explicitly adopts a blank or legacy-compatible root, writes reciprocal
runtime ownership bindings, and refuses a conflicting writer. `status` checks
the declared auxiliary inputs, rebuilds the projections, renders the catalogue,
and runs semantic and foreign-key verification.

## Evidence first

Search before claiming ownership or recommending a purchase:

```bash
property-inventory search "hex bit" --condition working
property-inventory context --task "find a working driver"
property-inventory compatibility itm-driver itm-bit
property-inventory torque-check --tool-item-id itm-driver --requested-nm 4
```

Compatibility is definitive only with high-confidence explicit evidence or
matching normalized interfaces. Operational answers are stricter: an item must
be currently possessed and explicitly usable. A broken item is unavailable; an
unrecorded condition is `unknown`.

Lifecycle records preserve uncertainty. For a known date, use the event's
ordinary date option. For a historical sale whose date is not known, record the
observation rather than inventing the occurrence date:

```bash
property-inventory sell --actor "Example reviewer" --item-id itm-example \
  --sold-date-unknown --observed-on 2026-08-06 \
  --source-ref "Sale confirmed; historical date unknown"
```

If an exact event is learned later, supply both dates, for example
`--sold-on 2025-01-02 --observed-on 2026-08-06`. Observation may follow
occurrence but cannot precede it.

Reacquiring a terminal item keeps one item ID and its serial history, but starts
a new ownership episode. The command clears old condition, acquisition,
purchase and receipt facts, requires any previously known quantity to be
rechecked, and records an explicit current condition only when supplied.
`ownership_corrected` is different: it preserves valid details because the
terminal event itself was mistaken.

v6 appends evidence-backed correction history rather than silently overwriting
identity, item details, dimensions, or other durable facts. Item dimensions may
be partial; fit and packing return `unknown` when the measurement is not enough.
Kits become complete or incomplete only through an explicit review of the named
requirement list.

## Capture is not a physical check

`capture-prepare` stages an overview image, deterministic crops, observations,
and bounded duplicate candidates. `capture-review` can seal an explicit
supporting link or a fully specified physical/discovery decision into a
digest-bound proposal. Nothing becomes canonical until a trusted local writer
inspects and applies that proposal. A passive link does not prove the object is
owned, current, identified, located, or in a particular condition; a physical
or discovery decision must state and replay every claimed field.

```bash
property-inventory --scope private capture-prepare \
  --overview "$demo_root/example-overview.jpg" --captured-on 2026-08-06 \
  --segments '[{"segment_id":"object-1","region":{"x":0,"y":0,"width":100,"height":80}}]' \
  --source-ref "Synthetic overview"
```

Only a directly linked image on `physical_check` evidence with
`explicit_current` strength qualifies as an insurance photo. A capture crop or
review link never qualifies by itself.

## Import, proposals, and recovery

Generic CSV/JSON import produces a bounded, provenance-preserving proposal. It
does not infer receipt, possession, location, condition, or a physical check.
Proposals bind their source and base-store digests; application is atomic and
receipted in canonical JSONL.

```bash
property-inventory --scope private import-propose \
  --input "$demo_root/example-cart.csv" --format csv --source-name example-cart.csv \
  --source-namespace example-shop --source-date 2026-08-06
property-inventory --scope private proposal-show proposal-00000000-0000-0000-0000-000000000000
```

Use `doctor` with a new path outside all managed roots for an export-and-blank-
restore drill. It retains the verified archive and removes only its temporary
restore roots. `compatibility-status` shows the executable v1-v5-to-v6 matrix;
future schemas are refused until the policy is deliberately changed.

```bash
property-inventory --scope private doctor --output "$demo_root/inventory-drill.tar.gz"
property-inventory compatibility-status
```

## Insurance and sync

`insurance-status` reports scope-safe, evidence-backed gaps. Missing photo,
serial, value, receipt, appraisal, acquisition date, or location stays
`unknown`. Private CLI commands can create and validate a deterministic package
from verified canonical bytes; that is preparation material, not insurance
coverage or a completed claim.

Receipt and appraisal readiness requires provenance-compatible evidence plus
an actual decoded image or strictly parsed PDF. Renaming a text file or assigning
a role never qualifies it.

Replica sync is offline file exchange with three-way conflict detection. It has
no listener, discovery, transport, or last-write-wins rule. Disjoint verified
transactions merge. A conflict over a committed item or fact keeps canonical
append-only history; remote intent must be re-recorded as a fresh canonical
transaction if it is still true. MCP agents may prepare, inspect, and record the
admissible plan choice, but only the trusted local CLI can apply it.

A bundle that newly references evidence media carries a deterministic sibling
`<bundle>.media` sidecar containing only the new SHA-256 bytes. Preparation
rejects missing, extra, symlinked, oversized, tampered, digest-, size-, or
MIME-inconsistent entries, then stages verified bytes for trusted apply.

Same-item event history and existing-item dependants can force a no-choice
rebase rather than a false merge. Rejecting a branch prunes only artifacts
owned exclusively by it; unrelated evidence and media remain.

## MCP

The stdio MCP defaults to scoped reads. Its explicit private write profile can
prepare and inspect bounded proposals, capture reviews, and replica plans, but
cannot apply a plan or invoke direct lifecycle mutations. There is no HTTP
server and no MCP package export. See [MCP profiles](docs/mcp.md).

## Schema, operations, and proof

- [Schema v6](docs/schema.md)
- [Operations and recovery](docs/operations.md)
- [Proposal and MCP boundary](docs/adr/0002-proposals-and-least-privilege-mcp.md)
- [Roadmap and evidence gates](docs/roadmap.md)

CI runs lint, compilation, the acceptance suite, a clean-wheel MCP smoke test
on Ubuntu and macOS with Python 3.11, 3.12, 3.13, and 3.14, plus a separate
Ubuntu public-boundary, wheel, secret, and Markdown-link audit. Run the local
source checks with:

```bash
ruff check .
python3 -m compileall -q src property_inventory.py rebuild_inventory_sqlite.py render_inventory.py verify_inventory.py
python3 -m unittest discover -v -p 'test_*.py'
scripts/check-public-leaks.sh
```

## Limits

There is no web or phone interface, remote sync service, bundled recognition
model, insurer integration, or proof of real-world adoption. Local adapters may
produce bounded observations, but they are trusted local programs and their
output still requires explicit review. Spatial packing is a deterministic
measured-box heuristic, not an optimal organizer or an item-location claim.

## License

MIT. See [LICENSE](LICENSE).
