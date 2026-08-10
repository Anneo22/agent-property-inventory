# Product

<!-- impeccable:product-schema 1 -->

## Platform

local CLI and MCP

## Users

The primary user is a person working through a local AI agent. The agent needs reliable physical context before recommending a purchase, planning a repair, organizing or moving possessions, or preparing an insurance record. Developers and agent builders are the public repository's secondary audience.

## Product Purpose

`agent-property-inventory` gives local agents a durable, queryable record of what a person physically owns, where it is, what condition it is in, and what it works with. Success means the agent can use that record without inventing possession, compatibility, location, or absence.

## Positioning

The product is structured local data for one person's physical possessions, exposed through a CLI and MCP. The agent handles capture and reasoning. There is no inventory app or hosted service.

## Operating Context

People show or explicitly check physical objects, containers, documents, labels, measurements, serial numbers, and photos. Agents query before acting, prepare reviewed changes, and use the record for purchasing, repair, compatibility, packing, organization, maintenance, spatial planning, and insurance preparation.

## Capabilities and Constraints

- JSONL is canonical; SQLite and Markdown are rebuilt views.
- Physical possession requires current physical-check evidence. Purchase history alone is insufficient.
- Missing records and fields remain unknown. No match never proves absence.
- Compatibility uses explicit interfaces, sizes, standards, and directions.
- Writes are locked, backed up, rebuilt, rendered, verified, and foreign-key checked before replacement.
- The product has a CLI and least-privilege stdio MCP profiles. It has no graphical application, recognition model, hosted sync, or insurer integration.

## Brand Commitments

The public name is `agent-property-inventory`. The voice is plain, exact, compact, and skeptical of unsupported claims. The README leads with checked CLI behavior and uses no decorative product art.

## Evidence on Hand

- The repository's CLI and MCP implementation, test suite, and release workflow.
- A deterministic synthetic physical-check scenario for a T25 Torx bit in a tool drawer.
- No public customer claims, benchmarks, private inventory media, or real household floor plan may be fabricated or exposed.

## Product Principles

1. Evidence before assertion.
2. Unknown is a valid answer.
3. Interfaces matter more than similar purpose.
4. The agent does the work; the record stays small and local.
5. One guarded write path, many useful queries.

## Accessibility & Inclusion

The public explanation uses text, tables, and copyable terminal output. Meaning never depends on animation or color.
