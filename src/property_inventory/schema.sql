-- Disposable SQLite projection of the canonical JSONL store.
PRAGMA foreign_keys = ON;

CREATE TABLE metadata (
  inventory_id TEXT PRIMARY KEY CHECK (length(inventory_id) > 0),
  schema_version INTEGER NOT NULL CHECK (schema_version = 7)
);

-- Runtime-only attestation written by the rebuild after the canonical generation
-- passes semantic validation. It is deliberately absent from JSONL.
CREATE TABLE projection_state (
  store_digest TEXT PRIMARY KEY CHECK (
    length(store_digest) = 64 AND store_digest NOT GLOB '*[^0-9a-f]*'
  )
);

-- Empty after rebuild. The orchestrator writes one row only after rebuild,
-- render, semantic verification, and foreign-key verification all pass.
CREATE TABLE verification_state (
  store_digest TEXT PRIMARY KEY CHECK (
    length(store_digest) = 64 AND store_digest NOT GLOB '*[^0-9a-f]*'
  )
);

CREATE TABLE proposal_commits (
  proposal_id TEXT PRIMARY KEY CHECK (length(proposal_id) > 0),
  base_digest TEXT NOT NULL CHECK (
    length(base_digest) = 64 AND base_digest NOT GLOB '*[^0-9a-f]*'
  ),
  operations_digest TEXT NOT NULL CHECK (
    length(operations_digest) = 64 AND operations_digest NOT GLOB '*[^0-9a-f]*'
  ),
  applied_at TEXT NOT NULL CHECK (length(applied_at) > 0)
);

CREATE TABLE locations (
  location_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  parent_location_id TEXT REFERENCES locations(location_id),
  -- One arbitrary-depth spatial tree spans every scale.  The six original kinds
  -- keep their meaning so a migrated store is never reinterpreted.
  kind TEXT NOT NULL CHECK (kind IN (
    'place','room','container','vehicle','asset','unknown',
    'site','building','floor','zone','furniture','compartment'
  )),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  notes TEXT
);

CREATE TABLE models (
  model_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  brand TEXT,
  model TEXT,
  category TEXT NOT NULL,
  specs_json TEXT NOT NULL DEFAULT '{}' CHECK (
    json_valid(specs_json) AND json_type(specs_json) = 'object'
  ),
  interfaces_json TEXT NOT NULL DEFAULT '[]' CHECK (
    json_valid(interfaces_json) AND json_type(interfaces_json) = 'array'
  ),
  identifiers_json TEXT NOT NULL DEFAULT '{}' CHECK (
    json_valid(identifiers_json) AND json_type(identifiers_json) = 'object'
  ),
  reference_url TEXT
);

CREATE TABLE evidence (
  evidence_id TEXT PRIMARY KEY,
  evidence_type TEXT NOT NULL CHECK (evidence_type IN ('user_source','merchant_account','finance_sheet','vault_note','physical_check','research')),
  source_ref TEXT NOT NULL,
  captured_on TEXT NOT NULL,
  claim_strength TEXT NOT NULL CHECK (claim_strength IN ('explicit_current','claimed_owned','purchase_only','explicit_not_owned','area_not_found','research_only')),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  notes TEXT
);

CREATE TABLE media_assets (
  asset_id TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL UNIQUE CHECK (
    length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
  media_type TEXT NOT NULL CHECK (length(media_type) > 0),
  original_name TEXT,
  captured_on TEXT,
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  uri TEXT NOT NULL UNIQUE CHECK (uri = 'media://sha256/' || sha256)
);

CREATE TABLE interfaces (
  interface_id TEXT PRIMARY KEY,
  family TEXT NOT NULL CHECK (length(family) > 0),
  standard TEXT,
  variant TEXT,
  direction TEXT NOT NULL CHECK (direction IN ('plug','socket','bidirectional','unknown')),
  properties_json TEXT NOT NULL DEFAULT '{}' CHECK (
    json_valid(properties_json) AND json_type(properties_json) = 'object'
  ),
  notes TEXT
);

CREATE TABLE items (
  item_id TEXT PRIMARY KEY,
  model_id TEXT NOT NULL REFERENCES models(model_id),
  quantity REAL CHECK (
    quantity IS NULL OR (
      quantity >= 0 AND quantity <= 1.7976931348623157e308
      AND (ownership_state IN ('not_owned','disposed','refunded') OR quantity > 0)
    )
  ),
  unit TEXT NOT NULL DEFAULT 'item',
  ownership_state TEXT NOT NULL CHECK (ownership_state IN ('confirmed','candidate','not_owned','disposed','refunded','planned','unknown')),
  location_id TEXT REFERENCES locations(location_id),
  container_id TEXT REFERENCES locations(location_id),
  -- Where the item belongs, which is not where it currently is.  NULL means the
  -- home is unknown; it never means "the same as the current placement".
  home_location_id TEXT REFERENCES locations(location_id),
  home_container_id TEXT REFERENCES locations(location_id),
  condition TEXT,
  serial_or_lot TEXT,
  acquired_on TEXT,
  purchase_price REAL CHECK (
    purchase_price IS NULL OR (
      purchase_price >= 0 AND purchase_price <= 1.7976931348623157e308
    )
  ),
  purchase_currency TEXT CHECK (
    purchase_currency IS NULL OR (
      length(purchase_currency) = 3
      AND purchase_currency GLOB '[A-Z][A-Z][A-Z]'
    )
  ),
  replacement_value REAL CHECK (
    replacement_value IS NULL OR (
      replacement_value >= 0 AND replacement_value <= 1.7976931348623157e308
    )
  ),
  value_currency TEXT CHECK (
    value_currency IS NULL OR (
      length(value_currency) = 3 AND value_currency GLOB '[A-Z][A-Z][A-Z]'
    )
  ),
  receipt_ref TEXT,
  verified_on TEXT,
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  identity_sensitivity TEXT CHECK (
    identity_sensitivity IS NULL OR identity_sensitivity IN ('low','personal','high')
  ),
  primary_evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  notes TEXT,
  CHECK ((purchase_price IS NULL) = (purchase_currency IS NULL)),
  CHECK ((replacement_value IS NULL) = (value_currency IS NULL))
);

CREATE TABLE item_evidence (
  item_id TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('primary','supporting')),
  PRIMARY KEY (item_id, evidence_id)
);

CREATE TABLE evidence_assets (
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
  asset_id TEXT NOT NULL REFERENCES media_assets(asset_id) ON DELETE RESTRICT,
  role TEXT NOT NULL CHECK (role IN ('source','crop','receipt','appraisal','manual','other')),
  region_json TEXT DEFAULT NULL CHECK (
    region_json IS NULL OR (
      json_valid(region_json) AND json_type(region_json) = 'object'
    )
  ),
  PRIMARY KEY (evidence_id, asset_id, role)
);

CREATE TABLE model_interfaces (
  model_id TEXT NOT NULL REFERENCES models(model_id) ON DELETE CASCADE,
  interface_id TEXT NOT NULL REFERENCES interfaces(interface_id) ON DELETE RESTRICT,
  role TEXT NOT NULL CHECK (role IN ('provides','requires','accepts')),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE RESTRICT,
  notes TEXT,
  PRIMARY KEY (model_id, interface_id, role)
);

CREATE TABLE relationships (
  relationship_id TEXT PRIMARY KEY,
  subject_item_id TEXT NOT NULL REFERENCES items(item_id),
  predicate TEXT NOT NULL CHECK (predicate IN ('works_with','requires','contained_in','replaces','overlaps_function','not_compatible','compatible_if','configured_on','unknown')),
  object_item_id TEXT NOT NULL REFERENCES items(item_id),
  confidence TEXT NOT NULL CHECK (confidence IN ('verified','high','medium','low','unknown')),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  notes TEXT
);

CREATE TABLE item_documents (
  document_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
  document_type TEXT NOT NULL CHECK (document_type IN ('photo','receipt','warranty','appraisal','manual','other')),
  uri TEXT NOT NULL,
  captured_on TEXT,
  notes TEXT
);

CREATE TABLE kits (
  kit_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  serves_item_id TEXT NOT NULL REFERENCES items(item_id),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  notes TEXT
);

CREATE TABLE kit_requirements (
  kit_id TEXT NOT NULL REFERENCES kits(kit_id) ON DELETE CASCADE,
  requirement_key TEXT NOT NULL,
  item_id TEXT REFERENCES items(item_id),
  status TEXT NOT NULL CHECK (status IN ('source_present','exists_unassigned','purchase_candidate','not_recorded','needs_verification')),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  recorded_at TEXT NOT NULL,
  verified_event_sequence INTEGER CHECK (
    verified_event_sequence IS NULL OR verified_event_sequence > 0
  ),
  notes TEXT,
  CHECK (status NOT IN ('source_present','exists_unassigned') OR (
    item_id IS NOT NULL AND verified_event_sequence IS NOT NULL
  )),
  CHECK (status IN ('source_present','exists_unassigned') OR verified_event_sequence IS NULL),
  CHECK (status != 'not_recorded' OR item_id IS NULL),
  PRIMARY KEY (kit_id, requirement_key)
);

CREATE TABLE kit_reviews (
  review_id TEXT PRIMARY KEY,
  kit_id TEXT NOT NULL REFERENCES kits(kit_id) ON DELETE CASCADE,
  reviewed_on TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  completeness TEXT NOT NULL CHECK (completeness IN ('complete','incomplete')),
  requirement_keys_json TEXT NOT NULL CHECK (
    json_valid(requirement_keys_json) AND json_type(requirement_keys_json) = 'array'
  ),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  notes TEXT
);

CREATE TABLE torque_paths (
  path_id TEXT PRIMARY KEY,
  tool_item_id TEXT NOT NULL REFERENCES items(item_id),
  output_drive TEXT NOT NULL,
  min_torque_nm REAL CHECK (
    min_torque_nm IS NULL OR (min_torque_nm >= 0 AND min_torque_nm <= 1.7976931348623157e308)
  ),
  max_torque_nm REAL CHECK (
    max_torque_nm IS NULL OR (max_torque_nm >= 0 AND max_torque_nm <= 1.7976931348623157e308)
  ),
  adapter_description TEXT,
  adapter_max_torque_nm REAL CHECK (
    adapter_max_torque_nm IS NULL OR (
      adapter_max_torque_nm >= 0 AND adapter_max_torque_nm <= 1.7976931348623157e308
    )
  ),
  status TEXT NOT NULL CHECK (status IN ('direct','adapter_rating_unknown','attachment_only','needs_verification')),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  notes TEXT,
  CHECK ((min_torque_nm IS NULL) = (max_torque_nm IS NULL)),
  CHECK (min_torque_nm IS NULL OR min_torque_nm <= max_torque_nm),
  CHECK (
    (status = 'direct' AND min_torque_nm IS NOT NULL
      AND adapter_description IS NULL AND adapter_max_torque_nm IS NULL)
    OR (status = 'adapter_rating_unknown' AND min_torque_nm IS NOT NULL
      AND adapter_description IS NOT NULL AND length(trim(adapter_description)) > 0
      AND adapter_max_torque_nm IS NULL)
    OR (status = 'attachment_only' AND min_torque_nm IS NULL
      AND adapter_description IS NOT NULL AND length(trim(adapter_description)) > 0)
    OR (status = 'needs_verification'
      AND (adapter_max_torque_nm IS NULL OR (
        adapter_description IS NOT NULL AND length(trim(adapter_description)) > 0
      )))
  )
);

CREATE TABLE item_tags (
  item_id TEXT NOT NULL REFERENCES items(item_id),
  tag TEXT NOT NULL CHECK (
    length(tag) BETWEEN 1 AND 64
    AND tag = lower(tag)
    AND tag NOT GLOB '*[^a-z0-9-]*'
    AND tag NOT GLOB '-*'
    AND tag NOT GLOB '*-'
    AND tag NOT GLOB '*--*'
  ),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  notes TEXT,
  PRIMARY KEY (item_id, tag)
);

CREATE TABLE aliases (
  alias_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
  alias TEXT NOT NULL CHECK (length(alias) > 0),
  alias_kind TEXT NOT NULL CHECK (length(alias_kind) > 0),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  notes TEXT,
  UNIQUE (item_id, alias)
);

CREATE TABLE spatial_profiles (
  profile_id TEXT PRIMARY KEY,
  location_id TEXT NOT NULL REFERENCES locations(location_id) ON DELETE CASCADE,
  profile_json TEXT NOT NULL CHECK (json_valid(profile_json)),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  notes TEXT
);

CREATE TABLE valuations (
  valuation_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
  amount REAL NOT NULL CHECK (
    amount >= 0 AND amount <= 1.7976931348623157e308
  ),
  currency TEXT NOT NULL CHECK (
    length(currency) = 3 AND currency GLOB '[A-Z][A-Z][A-Z]'
  ),
  valued_on TEXT NOT NULL CHECK (length(valued_on) > 0),
  basis TEXT NOT NULL CHECK (length(basis) > 0),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  notes TEXT
);

CREATE TABLE capture_sessions (
  capture_session_id TEXT PRIMARY KEY,
  captured_on TEXT NOT NULL CHECK (length(captured_on) > 0),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  provenance_state TEXT NOT NULL CHECK (provenance_state IN ('bound','legacy_unbound')),
  artifact_sha256 TEXT CHECK (
    artifact_sha256 IS NULL OR (
      length(artifact_sha256) = 64 AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'
    )
  ),
  artifact_json TEXT CHECK (
    artifact_json IS NULL OR (json_valid(artifact_json) AND json_type(artifact_json) = 'object')
  ),
  review_sha256 TEXT CHECK (
    review_sha256 IS NULL OR (
      length(review_sha256) = 64 AND review_sha256 NOT GLOB '*[^0-9a-f]*'
    )
  ),
  review_json TEXT CHECK (
    review_json IS NULL OR (json_valid(review_json) AND json_type(review_json) = 'object')
  ),
  notes TEXT,
  CHECK (
    (
      provenance_state = 'bound'
      AND artifact_sha256 IS NOT NULL
      AND artifact_json IS NOT NULL
      AND review_sha256 IS NOT NULL
      AND review_json IS NOT NULL
    )
    OR (
      provenance_state = 'legacy_unbound'
      AND artifact_sha256 IS NULL
      AND artifact_json IS NULL
      AND review_sha256 IS NULL
      AND review_json IS NULL
    )
  )
);

CREATE TABLE capture_observations (
  observation_id TEXT PRIMARY KEY,
  capture_session_id TEXT NOT NULL REFERENCES capture_sessions(capture_session_id) ON DELETE CASCADE,
  observation_index INTEGER NOT NULL CHECK (
    typeof(observation_index) = 'integer' AND observation_index > 0
  ),
  item_id TEXT REFERENCES items(item_id) ON DELETE SET NULL,
  observation_json TEXT NOT NULL CHECK (json_valid(observation_json)),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  validation_state TEXT NOT NULL CHECK (validation_state IN ('validated','legacy_unknown')),
  notes TEXT,
  UNIQUE (capture_session_id, observation_index)
);

CREATE TABLE maintenance_sessions (
  maintenance_session_id TEXT PRIMARY KEY,
  performed_on TEXT NOT NULL CHECK (length(performed_on) > 0),
  activity TEXT NOT NULL CHECK (length(activity) > 0),
  elapsed_seconds INTEGER NOT NULL CHECK (typeof(elapsed_seconds) = 'integer' AND elapsed_seconds >= 0),
  correction_count INTEGER NOT NULL CHECK (typeof(correction_count) = 'integer' AND correction_count >= 0),
  review_count INTEGER NOT NULL CHECK (typeof(review_count) = 'integer' AND review_count >= 0),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  notes TEXT
);

CREATE TABLE maintenance_session_items (
  maintenance_session_id TEXT NOT NULL REFERENCES maintenance_sessions(maintenance_session_id) ON DELETE CASCADE,
  item_id TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
  PRIMARY KEY (maintenance_session_id, item_id)
);

CREATE TABLE sync_receipts (
  sync_receipt_id TEXT PRIMARY KEY,
  replica_ref TEXT NOT NULL CHECK (length(replica_ref) > 0),
  payload_digest TEXT NOT NULL CHECK (
    length(payload_digest) = 64 AND payload_digest NOT GLOB '*[^0-9a-f]*'
  ),
  recorded_at TEXT NOT NULL CHECK (length(recorded_at) > 0),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  notes TEXT
);

CREATE TABLE item_dimensions (
  dimension_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
  width REAL CHECK (
    width IS NULL OR (width > 0 AND width <= 1.7976931348623157e308)
  ),
  height REAL CHECK (
    height IS NULL OR (height > 0 AND height <= 1.7976931348623157e308)
  ),
  depth REAL CHECK (
    depth IS NULL OR (depth > 0 AND depth <= 1.7976931348623157e308)
  ),
  unit TEXT NOT NULL CHECK (unit IN ('mm','cm','m','in','ft')),
  measured_on TEXT NOT NULL CHECK (length(measured_on) > 0),
  recorded_at TEXT NOT NULL,
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  notes TEXT,
  CHECK (width IS NOT NULL OR height IS NOT NULL OR depth IS NOT NULL)
);

CREATE TABLE item_amendments (
  amendment_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
  amended_on TEXT NOT NULL CHECK (length(amended_on) > 0),
  recorded_at TEXT NOT NULL,
  actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  previous_model_id TEXT NOT NULL REFERENCES models(model_id),
  target_model_id TEXT NOT NULL REFERENCES models(model_id),
  reason TEXT NOT NULL CHECK (
    reason IN ('identity_correction','reclassification','model_merge','model_split')
  ),
  notes TEXT,
  CHECK (previous_model_id != target_model_id)
);

CREATE TABLE item_detail_amendments (
  detail_amendment_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES items(item_id),
  amended_on TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  previous_json TEXT NOT NULL CHECK (
    json_valid(previous_json) AND json_type(previous_json) = 'object'
  ),
  changes_json TEXT NOT NULL CHECK (
    json_valid(changes_json) AND json_type(changes_json) = 'object'
  ),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  notes TEXT
);

CREATE TABLE fact_amendments (
  fact_amendment_id TEXT PRIMARY KEY,
  table_name TEXT NOT NULL CHECK (table_name IN (
    'locations','aliases','item_tags','relationships','torque_paths',
    'spatial_profiles','kits','kit_requirements','model_interfaces',
    'valuations','item_documents','item_dimensions'
  )),
  selector_json TEXT NOT NULL CHECK (
    json_valid(selector_json) AND json_type(selector_json) = 'object'
  ),
  amended_on TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  action TEXT NOT NULL CHECK (action IN ('replace','retract')),
  previous_json TEXT NOT NULL CHECK (
    json_valid(previous_json) AND json_type(previous_json) = 'object'
  ),
  replacement_json TEXT CHECK (
    replacement_json IS NULL OR (
      json_valid(replacement_json) AND json_type(replacement_json) = 'object'
    )
  ),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  reason TEXT NOT NULL,
  notes TEXT,
  CHECK ((action = 'replace') = (replacement_json IS NOT NULL))
);

-- A party is a counterparty in a custody, ownership or access claim. It exists
-- only when evidence names it, so an unresolved custodian invents nobody.
CREATE TABLE parties (
  party_id TEXT PRIMARY KEY,
  name TEXT NOT NULL CHECK (length(name) > 0),
  party_kind TEXT NOT NULL CHECK (
    party_kind IN ('person','household','organisation','unknown')
  ),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  notes TEXT
);

-- Custody, ownership and access are separate claims about the same item. A loan
-- moves custody and leaves ownership untouched.
CREATE TABLE item_party_relations (
  relation_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
  party_id TEXT REFERENCES parties(party_id),
  role TEXT NOT NULL CHECK (role IN ('owner','custodian','access')),
  custody_kind TEXT CHECK (custody_kind IN ('loan','storage','service','transit','possession','unknown')),
  status TEXT NOT NULL CHECK (status IN ('active','ended')),
  started_on TEXT,
  ended_on TEXT,
  due_on TEXT,
  quantity REAL CHECK (quantity IS NULL OR quantity > 0),
  unit TEXT,
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  ended_evidence_id TEXT REFERENCES evidence(evidence_id),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  notes TEXT,
  CHECK (party_id IS NOT NULL OR role = 'custodian'),
  CHECK ((role = 'custodian') = (custody_kind IS NOT NULL)),
  CHECK ((quantity IS NULL) = (unit IS NULL)),
  CHECK (due_on IS NULL OR role = 'custodian'),
  CHECK (status = 'active' OR status = 'ended'),
  CHECK (
    (status = 'active' AND ended_on IS NULL AND ended_evidence_id IS NULL)
    OR (status = 'ended' AND ended_on IS NOT NULL AND ended_evidence_id IS NOT NULL)
  )
);

-- An owned item may itself be a place: a toolbox, a van, a filing cabinet. This
-- is one node of the same location tree, never a second placement truth.
CREATE TABLE location_embodiments (
  embodiment_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL UNIQUE REFERENCES items(item_id) ON DELETE CASCADE,
  location_id TEXT NOT NULL UNIQUE REFERENCES locations(location_id),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('low','personal','high')),
  notes TEXT
);

CREATE TABLE inventory_events (
  event_id TEXT PRIMARY KEY,
  sequence INTEGER NOT NULL UNIQUE CHECK (sequence > 0),
  item_id TEXT NOT NULL REFERENCES items(item_id),
  event_type TEXT NOT NULL CHECK (event_type IN (
    'ingested',
    'ordered',
    'received',
    'physically_verified',
    'moved',
    'quantity_changed',
    'lent',
    'returned',
    'sold',
    'gifted',
    'disposed',
    'lost',
    'cancelled',
    'refunded',
    'loan_returned',
    'custody_started',
    'custody_ended',
    'access_granted',
    'access_revoked',
    'ownership_started',
    'ownership_ended',
    'planned',
    'ownership_unresolved',
    'ownership_excluded',
    'not_found_in_area',
    'reacquired',
    'ownership_corrected',
    'corrected'
  )),
  occurred_on TEXT,
  observed_on TEXT NOT NULL,
  occurred_on_precision TEXT NOT NULL DEFAULT 'exact' CHECK (
    occurred_on_precision IN ('exact','unknown')
  ),
  actor TEXT NOT NULL,
  evidence_id TEXT REFERENCES evidence(evidence_id),
  location_id TEXT REFERENCES locations(location_id),
  container_id TEXT REFERENCES locations(location_id),
  area_location_id TEXT REFERENCES locations(location_id),
  context_quality TEXT NOT NULL CHECK (context_quality IN ('bound','legacy_unknown')),
  details_json TEXT CHECK (
    details_json IS NULL OR (
      json_valid(details_json) AND json_type(details_json) = 'object'
    )
  ),
  notes TEXT,
  CHECK (
    (
      occurred_on_precision = 'exact'
      AND occurred_on IS NOT NULL
      AND observed_on >= occurred_on
    )
    OR (occurred_on_precision = 'unknown' AND occurred_on IS NULL)
  )
);

-- Full root-to-leaf ancestry for every node, so a reader never has to walk the
-- tree itself. A cycle would make the walk unbounded, and the semantic checks
-- reject one before this projection is ever built.
CREATE VIEW v_location_paths AS
WITH RECURSIVE walk(location_id, ancestor_id, depth, path_text) AS (
  SELECT location_id, parent_location_id, 1, name FROM locations
  UNION ALL
  SELECT w.location_id, p.parent_location_id, w.depth + 1, p.name || ' / ' || w.path_text
  FROM walk w
  JOIN locations p ON p.location_id = w.ancestor_id
)
SELECT location_id, path_text, depth FROM walk WHERE ancestor_id IS NULL;

CREATE VIEW v_inventory AS
SELECT i.item_id, m.name, m.brand, m.model, m.category, i.quantity, i.unit,
       i.ownership_state, l.name AS location, c.name AS container,
       lp.path_text AS location_path, hp.path_text AS home_location_path,
       i.condition, i.serial_or_lot, i.verified_on, i.sensitivity,
       e.evidence_type, e.claim_strength, m.interfaces_json, m.specs_json,
       m.identifiers_json, i.purchase_price, i.purchase_currency,
       i.replacement_value, i.value_currency, i.receipt_ref,
       i.notes, m.reference_url
FROM items i
JOIN models m ON m.model_id = i.model_id
JOIN evidence e ON e.evidence_id = i.primary_evidence_id
LEFT JOIN locations l ON l.location_id = i.location_id
LEFT JOIN locations c ON c.location_id = i.container_id
LEFT JOIN v_location_paths lp
  ON lp.location_id = COALESCE(i.container_id, i.location_id)
LEFT JOIN v_location_paths hp
  ON hp.location_id = COALESCE(i.home_container_id, i.home_location_id);

CREATE VIRTUAL TABLE inventory_search USING fts5(
  item_id UNINDEXED,
  name,
  brand,
  model,
  category,
  interfaces,
  specs,
  notes
);
