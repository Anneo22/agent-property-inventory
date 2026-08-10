# agent-property-inventory

[![Checks](https://github.com/Anneo22/agent-property-inventory/actions/workflows/checks.yml/badge.svg)](https://github.com/Anneo22/agent-property-inventory/actions/workflows/checks.yml)

`agent-property-inventory` is a local evidence ledger for physical possessions, exposed through a CLI and an MCP server for AI agents.

It records individual objects, where they are, their condition, the interfaces they use, and the evidence behind each claim. It is built for questions such as "Do I already own a T25 bit?", "Will this adapter fit?", and "What should go in this moving box?"

I built it because I wanted a durable way to record and query what I physically own, and to stop buying duplicates.

<p align="center">
  <img src="docs/assets/property-inventory-demo.gif" width="800" alt="A real CLI query finds one physically checked T25 Torx bit at Cambridge home, inside a named room, drawer unit, drawer, and section.">
</p>

## See it answer a real question

This exact block is reproduced from a temporary inventory by [`check-readme-example.py`](scripts/check-readme-example.py), which the [CI workflow](.github/workflows/checks.yml) runs against the real CLI.

<!-- readme-example:start -->
```console
$ property-inventory search "T25" --summary
{
  "matching_record_found": true,
  "count": 1,
  "matches": [
    {
      "name": "T25 Torx bit",
      "ownership": "confirmed",
      "condition": "working",
      "location": "Study",
      "location_path": "Cambridge home / Study / Drawer unit / Second drawer / Rear section",
      "last_physical_check_on": "2026-08-09",
      "evidence_types": [
        "physical_check"
      ]
    }
  ],
  "next_cursor": null,
  "page_count": 1,
  "truncated": false
}
```
<!-- readme-example:end -->

The item is a distinct physical unit, current possession was confirmed in person, its condition is working, and its last known location is the rear section of a specific drawer unit. A search with no match returns `unknown, not absent`; it never turns a missing record into a claim that you do not own something.

## What you can ask

| Command | Question |
|---|---|
| `search` | Do I already own this, and where is it? |
| `locations` | What is the full path to this room, drawer, bag, box, vehicle, or section? |
| `context` | What recorded items are relevant to this repair or task? |
| `compatibility` | Do these two exact items have matching interfaces? |
| `ownership-*` / `custody-*` / `access-*` | Who owns, holds, borrowed, stores, services, or can use this item? |
| `kit-status` / `torque-check` | Is this tool setup complete and within its recorded limits? |
| `fit` / `pack` / `free-volume` | Will measured items fit in this checked container? |
| `insurance-status` | Which owned items have enough evidence, and what is missing? |

The same queries are available to agents through the local stdio MCP server.

## How it works

1. **Capture a fact.** A person shows an object or checks a label, serial number, measurement, receipt, condition, or location. Every claim keeps its evidence.
2. **Match before adding.** The agent searches the existing inventory first. A known unit is updated; a genuinely different physical unit gets a new record.
3. **Commit through the CLI.** Canonical writes take one lock, create a backup, stage the complete change, rebuild the views, and run semantic and foreign-key checks. The live JSONL changes only after every check passes.
4. **Query through the CLI or MCP.** Reads use the same schema and scope rules. Read-profile MCP tools query the inventory. Write-profile tools prepare a proposal for review; the separate CLI command `proposal-apply` commits it.

The canonical write path is:

```text
lock -> backup -> stage -> rebuild -> render -> verify -> replace
```

`Data/store/*.jsonl` is the source of truth. SQLite and the Markdown catalogue are generated views and can be rebuilt.

Locations form one tree with no fixed depth. A path can be `Cambridge home / Study / Drawer unit / Second drawer / Rear section`, or continue through more containers when reality requires it. An item's usual home and its current placement are separate facts. Moving or lending it changes the current placement or custodian without erasing where it belongs or who owns it.

People and organisations are recorded separately from places. Ownership, custody, and access are evidence-backed episodes with their own start and end evidence. Several units from one quantity can be split across borrowers; unknown quantities remain unknown rather than being forced into a total.

## What it refuses to guess

| Input | Safe result |
|---|---|
| An item is in a shopping cart | `planned`, not ordered |
| A receipt says an item was bought | Purchase evidence, not proof of current possession |
| Two tools serve a similar purpose | Compatibility stays unknown until their interfaces match |
| A search returns no record | Unknown, not absent |
| An expected item is missing from one room | Follow-up required, not sold, lost, or disposed |

Unknown serial numbers, locations, measurements, quantities, and compatibility facts stay unknown.

## Where the files live

An installation keeps four paths separate:

| Path | Contents |
|---|---|
| Inventory root | Canonical JSONL and ownership metadata |
| Media root | Content-addressed photos, receipts, and other evidence |
| Runtime directory | SQLite, locks, backups, journals, and pending proposals |
| Catalogue output | A generated, scope-filtered Markdown view |

The code can live anywhere. The private JSONL does not need to sit in an Obsidian vault.

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
property-inventory status --summary
```

`status` rebuilds every generated view and runs the full integrity gate. Run `property-inventory --help` for the complete command surface.

## Record the first item

For a direct check, the CLI caller supplies what was physically observed. A person or an agent acting on explicit observations runs `discover` to record the physical check, ownership status, condition, quantity, and location in one verified transaction:

<!-- readme-capture:start -->
```bash
property-inventory add-location \
  --location-id loc-cambridge-home \
  --name "Cambridge home" \
  --kind site

property-inventory add-location \
  --location-id loc-study \
  --parent-location-id loc-cambridge-home \
  --name "Study" \
  --kind room

property-inventory add-location \
  --location-id loc-drawer-unit \
  --parent-location-id loc-study \
  --name "Drawer unit" \
  --kind furniture

property-inventory add-location \
  --location-id loc-second-drawer \
  --parent-location-id loc-drawer-unit \
  --name "Second drawer" \
  --kind compartment

property-inventory add-location \
  --location-id loc-rear-section \
  --parent-location-id loc-second-drawer \
  --name "Rear section" \
  --kind compartment

property-inventory discover \
  --actor "Owner" \
  --source-ref "Checked in person" \
  --name "T25 Torx bit" \
  --category tool \
  --checked-on "$(date +%F)" \
  --location-id loc-study \
  --container-id loc-rear-section \
  --new-model \
  --new-unit \
  --brand Wera \
  --model T25 \
  --quantity 1 \
  --unit piece \
  --condition working

property-inventory search "T25" --summary
```
<!-- readme-capture:end -->

For an item that may already exist, search first and use `--existing-model-id` or `--existing-item-id`. `--new-unit` is an explicit assertion that this is a different physical object.

`discover` defaults to confirmed ownership. For a newly distinguished object that is physically present but borrowed or unresolved, use `--ownership-state not_owned` or `--ownership-state unknown`, then record the known owner and custody episode with `ownership-start` and `custody-start`. A loan never changes the ownership fact.

## Connect an agent

The MCP server runs locally over stdio. This example gives an agent read-only access to personal-scope data:

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

Write-profile MCP tools can create a proposal in private scope, but they cannot change canonical JSONL. The CLI caller decides what becomes fact: inspect the returned ID with `property-inventory proposal-show <proposal-id>`, check the proposed identity, quantity, condition, location, and evidence, then commit it with `property-inventory proposal-apply <proposal-id>`.

A photo follows the same boundary:

```text
photo -> prepare_overview_capture -> confirm or correct observations
      -> review_overview_capture -> proposal-show -> proposal-apply
```

The agent passes a local image path and explicit crop regions to `prepare_overview_capture`. An optional adapter, selected from configuration fixed when the MCP server starts, can suggest regions and observations. Without an adapter, the caller supplies regions and manual observations. The tool stages the original and crops, then returns a digest plus any bounded suggestions and duplicate candidates. The caller confirms or corrects identity, quantity, condition, and location; `review_overview_capture` binds those decisions and the evidence to that exact image. Only the final CLI command changes the canonical inventory.

The CLI is the write-authority boundary: any person or agent given local CLI and filesystem access can invoke a direct write or `proposal-apply`. Direct commands such as `discover` have no separate proposal review, but still pass the full integrity gate before commit. An agent given only MCP access cannot bypass the proposal gate. See [MCP profiles](docs/mcp.md) and [overview capture](docs/operations.md#overview-capture-and-insurance).

## Current limits

This is a local, single-user project. It has no graphical app, hosted service, built-in recognition model, or insurer integration. [Photo and barcode adapters](docs/operations.md#overview-capture-and-insurance) can propose observations, but nothing becomes an inventory fact until it is reviewed and committed through the CLI.

## Documentation

[Architecture](docs/architecture.md) · [Schema](docs/schema.md) · [Operations](docs/operations.md) · [MCP](docs/mcp.md) · [Capture](docs/capture-adapter-protocol.md) · [Threat model](docs/threat-model.md)

## License

MIT. See [LICENSE](LICENSE).
