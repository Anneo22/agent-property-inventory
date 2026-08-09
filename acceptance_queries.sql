-- Property Inventory v1 acceptance queries. Each query must execute against inventory.sqlite.

-- 1. Edit requested_nm to the specified torque. This returns every drive path and its limiting fact.
WITH parameter(requested_nm) AS (VALUES (20.0))
SELECT p.requested_nm, v.name AS tool, tp.output_drive, tp.min_torque_nm, tp.max_torque_nm,
       tp.adapter_description, tp.adapter_max_torque_nm,
       CASE
         WHEN tp.status = 'attachment_only' THEN 'requires compatible torque wrench'
         WHEN tp.status = 'needs_verification' THEN 'path limits need verification; do not claim safe transfer'
         WHEN tp.min_torque_nm IS NULL OR tp.max_torque_nm IS NULL THEN 'tool range unknown; do not claim safe transfer'
         WHEN p.requested_nm < tp.min_torque_nm OR p.requested_nm > tp.max_torque_nm THEN 'outside tool range'
         WHEN tp.status = 'adapter_rating_unknown' THEN 'adapter is limiting unknown; do not claim safe transfer'
         WHEN tp.adapter_max_torque_nm IS NOT NULL
              AND p.requested_nm > tp.adapter_max_torque_nm THEN 'above recorded adapter rating'
         ELSE 'within recorded direct-tool range'
       END AS decision,
       e.source_ref AS evidence, tp.notes
FROM parameter p
CROSS JOIN torque_paths tp
JOIN v_inventory v ON v.item_id = tp.tool_item_id
JOIN evidence e ON e.evidence_id = tp.evidence_id
ORDER BY tp.output_drive, tool;

-- 2. Roadside-kit requirements. not_recorded means unknown, not proven absent.
SELECT k.name AS kit, kr.requirement_key, kr.status, v.name AS matched_item,
       v.location, v.container, e.source_ref AS evidence, kr.notes
FROM kits k
JOIN kit_requirements kr ON kr.kit_id = k.kit_id
LEFT JOIN v_inventory v ON v.item_id = kr.item_id
JOIN evidence e ON e.evidence_id = kr.evidence_id
ORDER BY k.name, kr.requirement_key;

-- 3. Explicitly modelled functional overlap, including pump uncertainty.
SELECT a.name AS subject, r.predicate, b.name AS object, r.confidence,
       e.source_ref AS evidence, r.notes
FROM relationships r
JOIN v_inventory a ON a.item_id = r.subject_item_id
JOIN v_inventory b ON b.item_id = r.object_item_id
JOIN evidence e ON e.evidence_id = r.evidence_id
WHERE r.predicate IN ('overlaps_function','replaces','unknown')
ORDER BY subject, object;

-- 4. Ownership classification with primary evidence and total evidence count.
SELECT v.ownership_state, v.name, v.claim_strength, v.evidence_type,
       count(ie.evidence_id) AS evidence_records
FROM v_inventory v
JOIN item_evidence ie ON ie.item_id = v.item_id
GROUP BY v.item_id
ORDER BY v.ownership_state, v.name;

-- 5. Known place allocations plus confirmed objects and containers still unallocated.
WITH RECURSIVE place_tree(root_id, location_id) AS (
  SELECT location_id, location_id FROM locations
  WHERE kind = 'place' AND parent_location_id IS NULL
  UNION ALL
  SELECT pt.root_id, l.location_id
  FROM locations l JOIN place_tree pt ON l.parent_location_id = pt.location_id
)
SELECT root.name AS place, 'item' AS record_type, v.name, v.quantity, v.unit, e.source_ref AS evidence
FROM place_tree pt
JOIN locations root ON root.location_id = pt.root_id
JOIN items i ON i.location_id = pt.location_id OR i.container_id = pt.location_id
JOIN v_inventory v ON v.item_id = i.item_id
JOIN evidence e ON e.evidence_id = i.primary_evidence_id
WHERE i.ownership_state = 'confirmed'
UNION ALL
SELECT root.name, 'container', l.name, NULL, NULL, 'locations containment tree'
FROM place_tree pt
JOIN locations root ON root.location_id = pt.root_id
JOIN locations l ON l.location_id = pt.location_id
WHERE l.location_id != root.location_id
UNION ALL
SELECT 'UNALLOCATED', 'item', v.name, v.quantity, v.unit, e.source_ref
FROM items i
JOIN v_inventory v ON v.item_id = i.item_id
JOIN evidence e ON e.evidence_id = i.primary_evidence_id
WHERE i.ownership_state = 'confirmed'
  AND NOT EXISTS (
    SELECT 1 FROM place_tree pt
    WHERE pt.location_id = i.location_id OR pt.location_id = i.container_id
  )
UNION ALL
SELECT 'UNALLOCATED', 'container', l.name, NULL, NULL, 'locations containment tree'
FROM locations l
WHERE l.kind = 'container' AND l.location_id NOT IN (SELECT location_id FROM place_tree)
ORDER BY place, record_type, name;
