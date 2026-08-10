# MCP profiles

The MCP adapter is a local stdio process. It calls the same core as the CLI and does not expose an HTTP listener.

## Read profile

The default is `--profile read --scope personal`:

| Tool | Purpose |
|---|---|
| `inventory_status` | Verify scope-safe store health. |
| `get_insurance_readiness` | Return confirmed visible items' evidence-backed readiness and explicit unknown gaps. |
| `get_upkeep_report` | Return scope-safe observed upkeep records and counts. |
| `search_inventory` | Query before ownership or purchase claims, with the CLI's typed retrieval filters. |
| `list_inventory_items` | Enumerate scope-visible items with typed filters and opaque cursor pagination. |
| `list_inventory_locations` | List or resolve scope-visible spatial nodes, from a site down to a compartment, each with its full root-to-leaf path, with cursor pagination. The query matches the whole path. |
| `inventory_task_context` | Return task-scoped matches and explicit unknown fields. |
| `get_inventory_item` | Read one visible item with evidence, event, relationship, compatibility, kit, and amendment context. |
| `get_kit_status` | Return conservative requirement and reviewed readiness for a visible kit or served item. |
| `check_torque_path` | Check a requested torque against recorded tool and adapter limits; unknown is never safe. |
| `check_compatibility` | Return compatible, incompatible, conditional or unknown with evidence-safe reasoning. |
| `get_space_context` | Return only checked profiles whose location, profile and evidence are visible. |
| `check_spatial_fit` | Compare caller-supplied explicit dimensions with one visible checked container box. |
| `calculate_free_volume` | Calculate remaining visible checked container volume from supplied positioned occupied boxes. |
| `plan_spatial_packing` | Produce deterministic first-fit packing from caller-supplied explicit dimensions. |

Private scope also permits `show_inventory_proposal` and `capture_status`, which can contain private item and location IDs.

## Write profile

`--profile write` is rejected unless `--scope private` is also explicit. It adds only:

- `prepare_inventory_proposal`
- `prepare_overview_capture`
- `review_overview_capture`
- `prepare_replica_sync`
- `inspect_replica_sync`
- `resolve_replica_sync`

There are no direct order, receive, move, sell, physical-check, maintenance,
proposal-apply, sync-apply or deletion tools. Preparation writes reviewable
runtime state but does not change JSONL.

`prepare_inventory_proposal` accepts the supported CLI operations as argument
arrays. This includes arbitrary-depth `add-location`, `set-home`, `add-party`,
`ownership-start` / `ownership-end`, `custody-start` / `custody-end`,
`access-grant` / `access-revoke`, and `embody-location`. The returned proposal
is only staged runtime state. A separate private CLI caller must inspect it with
`proposal-show` and commit it with `proposal-apply`; no MCP tool can apply it.

`search_inventory` and `inventory_task_context` call the same retrieval module as the CLI. Their optional filters are `category`, `ownership_state`, `condition`, `location`, `tag`, `alias_kind`, `interface_family`, `interface_standard`, `interface_variant`, `interface_direction`, and `location_known` (`known` or `unknown`). Matching is case- and punctuation-insensitive. Scope filtering precedes matching, counting, and IDs, including aliases, serials, normalized-interface evidence, and location hierarchies. An empty result always means `unknown, not absent`.

Current item details are field-scoped by the latest evidence-backed amendment.
If a condition, serial, acquisition, purchase, or receipt value is hidden, the
read tools, compatibility, torque, kit readiness, insurance, filters, and
generated catalogue treat it as unknown. They do not expose the materialized
private value or revive an older visible value.

The spatial read tools call the CLI's shared spatial surface. A hidden or missing location, profile, or evidence record returns `unknown` without profile counts or IDs. A visible result includes its visible profile ID and evidence source, type, claim strength, date, and sensitivity so the measurement is auditable. They accept only typed measurement objects, never model-specification prose. The MCP has no spatial mutation tool: checked floor-plan import and container-profile storage remain private CLI or proposal operations.

`get_insurance_readiness` calls the same scope-filtered CLI read as
`insurance-status`. It reports only confirmed visible items, their supported
evidence, and explicit unknown gaps. It does not claim that a hidden item is
missing or include its IDs, counts, media, location, serial, receipt, or value.
Receipt and appraisal states use the same evidence-type, claim-strength,
valuation-link, and decoded-media rules as the CLI; a document label alone is
not readiness evidence.
There is deliberately no MCP insurance export or validation tool: packages are
private CLI files with an explicit user-selected destination.

`prepare_overview_capture` copies a supplied local overview image and exact segments into private staging and returns bounded OCR/barcode observations, duplicate candidates and the artifact SHA-256. It may select only an adapter name from the server-owned registry loaded at MCP startup; the tool schema has no command field. `review_overview_capture` requires that returned digest, then seals explicit links and explicitly reviewed physical or discovery decisions into a digest-bound proposal without rerunning the adapter. A sealed decision can describe physical evidence, current possession, location, condition, quantity, or discovery, but none becomes canonical until the separately reviewed proposal is applied through the private CLI. Configured adapters are trusted local executables, not sandboxed or prevented from network access. They receive exact original image bytes and MUST apply EXIF orientation; every segment and observation region uses the declared post-transpose `exif_transposed_pixels` coordinate space. `capture_status` reports `awaiting_review`, `prepared`, or `applied`. For an applied bound session it derives the artifact/review digests and media counts from canonical JSONL, so removal of private runtime staging does not erase provenance. Migrated v4 history is reported as `legacy_unbound`, never upgraded from runtime files. It is a pure read. MCP exposes no cleanup or deletion tool; interrupted staging retirement is resumed explicitly with the private CLI `capture-cleanup` command.

Passing an empty `segments` list requires the selected adapter to return bounded
`predicted_segments`; otherwise preparation fails without staging. Explicit
segments take precedence. The returned capture includes the resolved segments
and whether they came from the caller or adapter. Registry commands are frozen
when the MCP process starts, and tool calls cannot alter them or extend
state-bearing evidence to a new item.
Adapter implementers should use the exact
[capture adapter protocol](capture-adapter-protocol.md), including its request,
response, EXIF coordinate and resource-limit contract.

The three replica tools are private-write-profile-only because their bundle and
plan rows may contain high-sensitivity inventory content. `prepare_replica_sync`
accepts one local offline bundle plus an independently supplied trusted-base
snapshot or verified export and prepares a three-way plan; it does not contact
a peer or apply data. `inspect_replica_sync` exposes that plan only to
the private write profile. `resolve_replica_sync` records every admissible
conflict choice and reruns full sandbox verification. A conflict over a
committed item or mutable fact exposes only the canonical choice, plus a
`reconciliation_required` instruction to re-record any still-valid remote intent
with fresh evidence. This prevents resolution from deleting canonical audit
history. An immutable identity collision with dependent replica rows exposes no
choice and requires a re-ID plus a new bundle. There is no MCP sync-apply tool,
no network transport tool, and no
last-write-wins behaviour. Read profiles do not list or expose sync plans.

When a replica bundle names new evidence media, preparation validates and stages
its signed bounded sibling sidecar before producing a plan. The caller cannot
redirect that path or cause the MCP to import arbitrary files.

## Generic client configuration

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

The config supplies independent inventory, runtime, media, and catalogue paths plus forbidden roots. Use one server process per inventory. The filesystem lock protects canonical writes, but the product contract still requires one intentional writer at a time.
