# agent-property-inventory

[![Checks](https://github.com/Anneo22/agent-property-inventory/actions/workflows/checks.yml/badge.svg)](https://github.com/Anneo22/agent-property-inventory/actions/workflows/checks.yml)

`agent-property-inventory` gives local AI agents a reliable record of what you physically own, where it is, and what it works with.

Agents query it before buying, repairing, moving, organizing, or preparing an insurance record.

![A physical object becomes an evidence-backed record that an agent can use before acting](docs/assets/physical-memory.gif)

The record keeps an object's evidence, location, condition and interfaces together.

## Ask before you act

![An agent asks whether a T25 bit is already owned and gets an evidence-backed location](docs/assets/ask-before-acting.png)

No match means "not recorded," never "does not exist."

## The larger idea

![Objects across physical spaces become one queryable context for packing, repair, and insurance preparation](docs/assets/physical-world-map.png)

Today it supports physical checks, lifecycle history, compatibility, task kits, torque limits, spaces, packing, maintenance, insurance readiness, floor plans, and reviewed photo proposals. It has no recognition model, app, hosted sync, or insurer integration.

## Why agents can trust it

- **Evidence has a type.** A receipt proves a purchase. Possession needs a current check. Compatibility needs matching standards, sizes, and directions.
- **Unknowns survive.** Missing serials, locations, measurements, and items stay unknown.
- **One guarded path writes.** A change is locked, backed up, rebuilt, verified, and checked for broken links before it replaces the JSONL.

`Data/store/*.jsonl` is the source of truth. SQLite and `Inventory.md` are rebuilt views.

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

`status` rebuilds the views and runs every integrity check.

## Use it

```bash
property-inventory search "hex bit" --condition working --summary
property-inventory context --task "repair a bicycle tyre"
```

The CLI also records checks, orders, deliveries, moves, loans, sales, returns, evidence, maintenance, and offline proposals. Run `property-inventory --help` for every command.

## Connect an agent

The MCP server exposes inventory queries as local tools an agent can call over stdio.

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

The instance selects an inventory, the scope filters visible data, and the profile controls tools. Read mode cannot write; private tools prepare proposals for reviewed CLI application. See [MCP profiles](docs/mcp.md).

## Documentation

[Architecture](docs/architecture.md) · [Schema](docs/schema.md) · [Operations](docs/operations.md) · [MCP](docs/mcp.md) · [Capture](docs/capture-adapter-protocol.md) · [Threat model](docs/threat-model.md)

## License

MIT. See [LICENSE](LICENSE).
