# Schema v7

Canonical data is UTF-8 JSONL in `Data/store`. SQLite and the Markdown
catalogue are disposable projections. IDs are stable, and an unavailable value
is `null`, never a guessed default.

| Files | Purpose |
|---|---|
| `metadata` | Inventory identity and schema version. |
| `models`, `items`, `locations` | Product identity, physical units or stock, and a location/container tree. |
| `parties`, `item_party_relations` | Named counterparties and evidence-backed owner, custodian, and access claims. |
| `location_embodiments` | The one-to-one link where an owned item provides a location node. |
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

## Space, custody and access

`locations` is the only arbitrary-depth spatial tree. There is no placements table: `items.location_id` and `items.container_id` remain the single current-placement projection, and a store carrying any second placement file is rejected before a byte is read. Location kinds now span `site`, `building`, `floor`, `zone`, `room`, `furniture`, `compartment`, `container`, `vehicle`, `asset`, `place` and `unknown`; the six original kinds keep their v6 meaning.

`items.home_location_id` and `items.home_container_id` say where an item belongs, which is a different fact from where it is. `null` means the home is unknown. It never means "the same as the current placement", and nothing derives one from the other. A home container must sit inside its home location.

Every read that returns an item also returns `location_path` and `home_location_path`: the full root-to-leaf ancestry of the most specific placement, container before area. Scope is applied to the whole chain first, so a partly visible path is withheld rather than published with a gap; an empty path means unknown or out of scope, and the existing `location` and `container` fields still mark redaction. Location search matches the whole path, so an intermediate name need not be known, and each match carries its own `path`.

Ownership, custody and access are three separate evidence-backed claims in `item_party_relations`, never one ownership state. A loan moves custody and leaves ownership untouched. Custody episodes record `loan`, `storage`, `service`, `transit`, `possession`, or honestly `unknown`, plus optional due date and quantity/unit. Multiple known partial allocations may be active, but their sum cannot exceed the item quantity; an unknown allocation cannot overlap any other active allocation. A `party` exists only when evidence names it, so an unresolved custodian is recorded with a null `party_id` rather than an invented borrower; ownership and access always name a party. An active relation cannot declare an end date. Its starting and ending evidence remain separate, and both must already support the item. Party and relation sensitivity inherit a floor from evidence, item and party.

The CLI exposes these facts directly through `add-party`, `ownership-start`, `ownership-end`, `custody-start`, `custody-end`, `access-grant`, `access-revoke`, `set-home`, and `embody-location`. Each relation change appends a lifecycle event bound to the exact relation ID. The generic write-profile MCP proposal tool accepts the same commands, but only the private CLI can apply the reviewed proposal.

`location_embodiments` links one owned current item to one location node, so a toolbox or a van can hold things while remaining an item. The embodied node's parent must be the item's most-specific current placement. Moving the item atomically reparents that node, so every descendant's full path follows without rewriting child items. An arrangement that puts the item inside the node it provides, directly or through another embodiment, is rejected as impossible rather than recorded.

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

v7 does not overwrite a material correction without a trail. Identity changes
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

Schema v7 is current. v1, v2, v3, v4, v5, and v6 have explicit forward migration paths to v7, each validated against the supported Python floor (3.11). A migration backs up and verifies the complete prior generation before canonical replacement, recovers a pending transaction first, and refuses future or malformed schemas without mutation. This describes code and test coverage, not evidence that any particular private inventory has been migrated.

The v6 step preserves every ID, evidence link, and history row unchanged. It leaves all home facts unknown, because migration cannot check where something belongs, and it creates no parties, because no legacy row names one. It does correct the one thing v6 recorded wrongly: a `lent` item was never unowned, so it becomes `confirmed` with one active custodian relation whose party and dates are null. The loan event supplies the relation's evidence when that evidence already supports the item, otherwise the item's primary evidence does. Legacy stores that still carry the `lent` ownership state stay readable.
