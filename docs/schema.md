# Schema v6

Canonical data is UTF-8 JSONL in `Data/store`. SQLite and the Markdown
catalogue are disposable projections. IDs are stable, and an unavailable value
is `null`, never a guessed default.

| Files | Purpose |
|---|---|
| `metadata` | Inventory identity and schema version. |
| `models`, `items`, `locations` | Product identity, physical units or stock, and a location/container tree. |
| `evidence`, `item_evidence`, `media_assets`, `evidence_assets` | Provenance and immutable content-addressed bytes. |
| `interfaces`, `model_interfaces`, `relationships` | Evidence-backed compatibility claims. |
| `aliases`, `item_tags`, `item_documents`, `valuations` | Search names and supporting facts. |
| `kits`, `kit_requirements`, `kit_reviews`, `torque_paths` | Operational-kit structure, latest explicit completeness review, and tool limits. |
| `spatial_profiles`, `item_dimensions` | Measured location geometry and append-only, evidence-backed item measurements. |
| `capture_sessions`, `capture_observations` | Reviewable overview capture provenance, not possession proof. |
| `maintenance_sessions`, `maintenance_session_items` | Measured inventory-upkeep work. |
| `proposal_commits`, `sync_receipts` | Atomic proposal and offline-replica audit receipts. |
| `item_amendments`, `item_detail_amendments`, `fact_amendments` | Append-only corrections to identity, item details, and current durable facts. |
| `inventory_events` | Ordered lifecycle history. |

## Evidence and lifecycle

- A purchase, import, capture link, or model match never proves current
  possession.
- `confirmed` ownership requires an appropriate lifecycle event. A
  `physically_verified` event requires `physical_check` evidence with
  `explicit_current` strength.
- Event dates are honest about uncertainty. `occurred_on` is nullable;
  `occurred_on_precision` is `exact` or `unknown`; every event has
  `observed_on`, the date the fact was checked or reported. An unknown event
  date must not be filled from its observation date, and a stale observation
  cannot override a later exact lifecycle fact. An exact event may be observed
  later than it occurred, but never earlier; its evidence is captured on the
  observation date.
- Terminal ownership states are not silently resurrected. An absence from one
  checked area is only `not_found_in_area`, not a sale, loss, or disposal.
- `ownership_corrected` preserves valid item details because the terminal event
  was wrong. `reacquired` begins a new ownership episode. It carries an exact
  reset declaration and a same-evidence reset amendment, clears prior condition,
  acquisition, purchase and receipt facts, and records current condition and
  quantity reaffirmations separately when they were actually checked. Serial
  identity and older audit history remain.
- Projected lifecycle fields are replayable from their append-only events.
  `quantity_changed` is only a current-possession event for confirmed or lent
  items. A cart or checkout quantity correction is instead carried by the
  purchase-only `planned` or `ordered` event, with the exact predecessor and
  result. Checkout changes to price, currency, or receipt also append an item
  detail amendment rather than silently rewriting the item.
- Offline replicas may append only CLI-shaped, contiguous events with fresh
  date-matched evidence attached to the affected item. Replica-only items must
  replay from a planned, ordered, or physical-discovery creation transaction.
  Canonical sync receipts are never accepted from a replica.

## Corrections and measurements

v6 does not overwrite a material correction without a trail. Identity changes
append `item_amendments`; receipt, acquisition, condition, and serial changes
append `item_detail_amendments`; other replace-or-retract changes append
`fact_amendments`. Each records the previous value, evidence, actor, and time.

`item_dimensions` are append-only partial measurements. A missing width,
height, or depth remains unknown. Fit and packing use only a visible,
evidence-backed measurement and return `unknown` when a required dimension is
not known.

`kit_reviews` seal an explicitly named requirement list as complete or
incomplete. Operational advice is condition-aware: current custody alone is
not enough. A usable condition can make a recorded item operationally
available, an explicitly unusable condition makes it unavailable, and an
unrecorded condition keeps the result unknown.

## Capture and insurance

Overview capture is passive until reviewed. It stages an overview image, crops,
and duplicate candidates. A review may link an observation to an existing item
as supporting evidence, or seal a fully specified physical/discovery decision
into a proposal. A passive link asserts none of identity, possession, location,
condition, lifecycle or physical verification. An explicit decision becomes
canonical only after separate CLI proposal application and exact-field replay.

Insurance has the stricter rule: a qualifying image is a directly linked image
on `physical_check` / `explicit_current` evidence for that item. Capture
artifacts and review links do not qualify as insurance photography. Missing
photo, value, receipt, serial, date, or place is reported as `unknown`.

Insurance documents have their own provenance contract. A `receipt` asset must
be an actual decoded image or parseable PDF linked to `purchase_only`
`merchant_account` or `user_source` evidence. An `appraisal` asset must be an
actual image or parseable PDF linked to reviewed `research_only` `user_source`
or `vault_note` evidence, and appraisal readiness also requires a valuation
with that same evidence. A filename, MIME label, document URI, purchase event,
or physical check cannot manufacture receipt or appraisal readiness. The
rebuild verifier enforces these semantics even when JSONL did not come through
the CLI.

## Scope and compatibility

Every derived row inherits a sensitivity floor from its evidence and parents.
Query scope is applied before matching, filtering, counts, IDs, or location
traversal, so hidden aliases, serials, interfaces, and places cannot leak.
Materialized item details inherit the latest amendment's evidence sensitivity
per field. If the current value is hidden, lower-scope retrieval, operational
advice, insurance, and catalogue projections return `unknown`; they never fall
back to an older visible value or reveal it through a filter or readiness result.

Compatibility is definitive only with sufficiently confident explicit evidence,
or complementary normalized interfaces with a named standard or variant.
Similar purpose, broad interface family, legacy text, or an empty search result
means `unknown`.

## Migration

Schema v6 is current. v1, v2, v3, v4, and v5 have explicit forward migration
paths to v6, each validated against the supported Python floor (3.11). A
migration backs up and verifies the complete prior generation before canonical
replacement, recovers a pending transaction first, and refuses future or
malformed schemas without mutation. This describes code and test coverage, not
evidence that any particular private inventory has been migrated.
