# ADR 0002: Atomic proposals and least-privilege MCP

## Status

Accepted. Reviewed for schema v6.

## Context

An agent needs useful inventory answers without becoming a general-purpose
writer. Direct lifecycle changes can create ownership, location, condition, or
insurance-relevant claims. A multi-step write can also fail between otherwise
valid operations. Runtime-only status cannot establish whether a process died
before or after canonical replacement.

## Decision

- The default stdio MCP exposes only scope-filtered reads: status, search,
  item and task context, compatibility, checked space, fit, packing, insurance
  readiness, and upkeep reporting.
- The explicit private write profile exposes bounded preparation and inspection
  for proposals, capture review, physical-discovery preparation, and offline
  replica plans and resolutions. It does not expose canonical application,
  package export, or direct lifecycle mutation.
- A proposal records supported CLI argument arrays and the digest of the
  canonical store it was prepared against.
- Application runs every operation in an isolated store, verifies the complete
  result, and submits one canonical transaction.
- That transaction appends a `proposal_commits` receipt containing the proposal
  and operation digests. A later retry can recover a committed result without
  guessing from runtime state.

## Consequences

- Tool discovery enforces the common read-only boundary.
- Scope filtering occurs before matches, counts, identifiers, and location
  traversal, so a lower scope cannot learn from hidden data.
- A stale or invalid batch leaves canonical JSONL unchanged.
- A trusted local CLI remains the only canonical writer. MCP agents hand a
  prepared identifier to that writer rather than applying it themselves.
- Schema v6 keeps the proposal receipt model and adds append-only corrections,
  dimensions, and kit reviews. These changes do not relax the MCP boundary.
- Capture links remain passive supporting evidence. Insurance accepts only a
  direct current physical-check image, not a capture artifact or review link.

## Review trigger

Revisit if authenticated remote writers or independently managed proposal
queues become a real requirement. Do not expose application or direct mutation
tools merely to remove an explicit local-writer step.
