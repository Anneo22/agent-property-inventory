# agent-property-inventory

[![Checks](https://github.com/Anneo22/agent-property-inventory/actions/workflows/checks.yml/badge.svg)](https://github.com/Anneo22/agent-property-inventory/actions/workflows/checks.yml)

`agent-property-inventory` is a local JSONL ledger that lets agents answer what you own, where it is, and what it works with.

One bike-parts session removed four of seven items from my cart because the inventory showed that I already owned the equivalents. It also exposed the opposite failure: an expensive wheelset was missing while a sold one was still recorded as owned. Agents need a reliable record of the physical world before they can give useful advice about it.

## What it keeps straight

- A product model is different from the physical unit you own.
- A purchase is different from current possession.
- Similar purpose is different from verified interface compatibility.
- Unknown is a real state. An empty search never proves absence.
- Photos, receipts, serials, locations, conditions, and measurements keep their evidence and history.

The canonical store is `Data/store/*.jsonl`. SQLite and Markdown are rebuilt projections. Runtime state, backups, media, and canonical data stay outside Obsidian; only the generated catalogue belongs in the vault.

```text
inventory-root/Data/store/*.jsonl   canonical ledger
media-root/sha256/...               immutable evidence files
runtime-root/                       SQLite, backups and proposals
notes-root/Inventory.md             generated catalogue
```

## Quick start

Python 3.11 or newer is required.

```bash
git clone https://github.com/Anneo22/agent-property-inventory.git
cd agent-property-inventory
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,mcp]'

demo_root="$(mktemp -d "${TMPDIR:-/tmp}/property-inventory-demo.XXXXXX")"
export PROPERTY_INVENTORY_ROOT="$demo_root/inventory"
export PROPERTY_INVENTORY_RUNTIME="$demo_root/runtime"
export PROPERTY_INVENTORY_MEDIA_ROOT="$demo_root/media"
export PROPERTY_INVENTORY_CATALOGUE_OUTPUT="$demo_root/notes/Inventory.md"

property-inventory init
property-inventory status
```

`status` rebuilds SQLite, regenerates the catalogue, runs the semantic verifier, and checks every foreign key.

## Use it

Query before buying or claiming ownership:

```bash
property-inventory search "hex bit" --condition working
property-inventory context --task "repair a bicycle tyre"
property-inventory compatibility itm-driver itm-bit
property-inventory torque-check --tool-item-id itm-driver --requested-nm 4
```

Record a real check against an existing item and location:

```bash
property-inventory physical-check \
  --actor "Example reviewer" \
  --source-ref "Checked in person" \
  --item-id itm-example \
  --checked-on 2026-08-09 \
  --location-id loc-example \
  --quantity 1 \
  --condition working
```

The CLI also records plans, orders, deliveries, moves, loans, sales, corrections, evidence, kits, maintenance, insurance readiness, measured spaces, and offline replica proposals. Run `property-inventory --help` for the command surface and use the [operations guide](docs/operations.md) for the rules behind lifecycle changes and recovery.

## Connect an agent

The MCP server is a local stdio process. Its default read profile can search, inspect, and reason about the inventory without changing canonical JSONL.

```json
{
  "command": "/absolute/path/to/property-inventory-mcp",
  "args": [
    "--config", "/absolute/path/to/config.json",
    "--instance", "private",
    "--scope", "personal",
    "--profile", "read"
  ]
}
```

The private write profile may prepare bounded proposals, capture reviews, and replica plans. It cannot apply them or invoke direct lifecycle mutations. See [MCP profiles](docs/mcp.md).

## Reliability boundaries

Every canonical mutation acquires the single-writer lock, creates a verified backup, stages the next generation, rebuilds the projections, renders the catalogue, runs semantic verification, and checks foreign keys before replacing live data.

A passive photo, cart, order, receipt, or purchase history does not prove current possession. Physical verification requires explicit current evidence. Compatibility becomes definitive only from explicit evidence or matching normalized interfaces. Capture and imports produce reviewable proposals rather than silently creating facts.

## Current limits

There is no web or phone interface, hosted sync service, bundled recognition model, insurer integration, or proof of broad real-world adoption. Spatial packing is a deterministic measured-box heuristic, not an optimal organizer. Local capture adapters are trusted programs and still require explicit review.

## Documentation

- [Architecture](docs/architecture.md)
- [Schema](docs/schema.md)
- [Operations and recovery](docs/operations.md)
- [MCP profiles](docs/mcp.md)
- [Capture adapter protocol](docs/capture-adapter-protocol.md)
- [Threat model](docs/threat-model.md)

## Development

```bash
ruff check .
python3 -m compileall -q src property_inventory.py rebuild_inventory_sqlite.py render_inventory.py verify_inventory.py
python3 -m unittest discover -v -s tests -p 'test_*.py'
scripts/check-public-leaks.sh
```

## License

MIT. See [LICENSE](LICENSE).
