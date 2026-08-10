# Product

<!-- impeccable:product-schema 1 -->

## Platform

local CLI and MCP

## Users

The primary user is a person working through a local AI agent. The agent needs reliable physical context before recommending a purchase, planning a repair, organizing or moving possessions, or preparing an insurance record. Developers and agent builders are the public repository's secondary audience.

## Product Purpose

`agent-property-inventory` gives local agents a durable, queryable record of what a person physically owns, where it is, what condition it is in, and what it works with. Success means the agent can use that record without inventing possession, compatibility, location, or absence.

## Positioning

The product is a bridge between an agent and one person's physical world: evidence-backed structured local data, exposed through a CLI and MCP, with the agent doing the capture and reasoning work. It is not an inventory app or hosted service.

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

The public name is `agent-property-inventory`. The voice is plain, exact, compact, and skeptical of unsupported claims. Visual explanations must be object-led, low-copy, and understandable without reading architecture documentation.

## Evidence on Hand

- The repository's CLI and MCP implementation, test suite, and release workflow.
- A deterministic synthetic physical-check scenario for a 1/4-inch hex-bit set in a tool drawer.
- No public customer claims, benchmarks, private inventory media, or real household floor plan may be fabricated or exposed.

## Product Principles

1. Evidence before assertion.
2. Unknown is a valid answer.
3. Interfaces matter more than similar purpose.
4. The agent does the work; the record stays small and local.
5. One guarded write path, many useful queries.

## Accessibility & Inclusion

Public visuals must remain understandable without animation, avoid color-only meaning, and include descriptive alternative text.
