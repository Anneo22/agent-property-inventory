"""Schema-v6 catalogue facts, privacy boundaries, and digest coverage."""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
RENDER = HERE / "src" / "property_inventory" / "render.py"
SCHEMA = HERE / "src" / "property_inventory" / "schema.sql"
MODULE_SPEC = importlib.util.spec_from_file_location("catalogue_render_v6", RENDER)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
RENDER_MODULE = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(RENDER_MODULE)


class CatalogueProjectionV6Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="catalogue-v6-")
        self.root = Path(self.temp.name)
        self.database = self.root / "inventory.sqlite"
        self.output = self.root / "Inventory.md"
        with sqlite3.connect(self.database) as con:
            con.executescript(SCHEMA.read_text())
            con.execute(
                "INSERT INTO metadata (inventory_id, schema_version) VALUES (?, 7)",
                ("catalogue-v6",),
            )
            con.executemany(
                "INSERT INTO models (model_id, name, category) VALUES (?, ?, 'tool')",
                (
                    ("mdl-old", "CANARY_HIGH_IDENTITY_OLD_MODEL"),
                    ("mdl-current", "CANARY_HIGH_IDENTITY_CURRENT_MODEL"),
                ),
            )
            con.execute(
                """
                UPDATE models
                SET brand='CANARY_HIGH_IDENTITY_BRAND',
                    model='CANARY_HIGH_IDENTITY_MODEL',
                    interfaces_json='["CANARY_HIGH_IDENTITY_INTERFACE"]',
                    specs_json='{"identity":"CANARY_HIGH_IDENTITY_SPEC"}',
                    reference_url='https://example.test/CANARY_HIGH_IDENTITY_URL'
                WHERE model_id='mdl-current'
                """
            )
            con.executemany(
                """
                INSERT INTO evidence (
                    evidence_id, evidence_type, source_ref, captured_on,
                    claim_strength, sensitivity
                ) VALUES (?, 'physical_check', ?, '2026-08-06',
                          'explicit_current', ?)
                """,
                (
                    ("ev-low", "CANARY_LOW_PHYSICAL_CHECK", "low"),
                    ("ev-high", "CANARY_HIGH_EVIDENCE_SOURCE", "high"),
                ),
            )
            con.execute(
                """
                INSERT INTO items (
                    item_id, model_id, quantity, unit, ownership_state,
                    condition, serial_or_lot, acquired_on, purchase_price,
                    purchase_currency, replacement_value, value_currency,
                    receipt_ref, sensitivity, identity_sensitivity,
                    primary_evidence_id, notes
                ) VALUES (
                    'itm-tool', 'mdl-current', 1, 'item', 'confirmed',
                    'CANARY_PRIVATE_CONDITION', 'CANARY_PRIVATE_SERIAL', '2025-01-02',
                    12.50, 'GBP', 30.00, 'GBP', 'CANARY_PRIVATE_RECEIPT',
                    'low', 'high', 'ev-low', 'CANARY_PRIVATE_NOTES'
                )
                """
            )
            con.executemany(
                """
                INSERT INTO aliases (
                    alias_id, item_id, alias, alias_kind, evidence_id, sensitivity
                ) VALUES (?, 'itm-tool', ?, ?, ?, ?)
                """,
                (
                    ("alias-low", "CANARY_LOW_ALIAS", "common_name", "ev-low", "low"),
                    ("alias-high", "CANARY_HIGH_ALIAS", "serial_name", "ev-high", "high"),
                ),
            )
            con.executemany(
                """
                INSERT INTO item_tags (item_id, tag, evidence_id, sensitivity)
                VALUES ('itm-tool', ?, ?, ?)
                """,
                (
                    ("canary-low-tag", "ev-low", "low"),
                    ("canary-high-tag", "ev-high", "high"),
                ),
            )
            con.executemany(
                """
                INSERT INTO item_dimensions (
                    dimension_id, item_id, width, height, depth, unit,
                    measured_on, recorded_at, evidence_id, sensitivity
                ) VALUES (?, 'itm-tool', ?, ?, ?, 'cm', ?, ?, ?, ?)
                """,
                (
                    ("dim-low-early", 12, 2, 2, "2026-08-01", "2026-08-01T08:00:00+00:00", "ev-low", "low"),
                    ("dim-old-low", None, 4, 3, "2026-08-01", "2026-08-01T09:00:00+00:00", "ev-low", "low"),
                    ("dim-current-high", 99, None, None, "2026-08-06", "2026-08-06T09:00:00+00:00", "ev-high", "high"),
                ),
            )
            con.executemany(
                """
                INSERT INTO valuations (
                    valuation_id, item_id, amount, currency, valued_on, basis,
                    evidence_id, sensitivity
                ) VALUES (?, 'itm-tool', ?, 'GBP', ?, ?, ?, ?)
                """,
                (
                    ("val-old-low", 10, "2026-08-01", "replacement", "ev-low", "low"),
                    ("val-current-high", 999, "2026-08-06", "replacement", "ev-high", "high"),
                    ("val-market-low", 11, "2026-08-02", "market", "ev-low", "low"),
                ),
            )
            con.execute(
                """
                INSERT INTO item_documents (document_id, item_id, document_type, uri)
                VALUES ('doc-receipt', 'itm-tool', 'receipt', 'CANARY_PRIVATE_RECEIPT_URI')
                """
            )
            con.execute(
                """
                INSERT INTO item_amendments (
                    amendment_id, item_id, amended_on, recorded_at, actor, evidence_id,
                    previous_model_id, target_model_id, reason
                ) VALUES (
                    'amd-high', 'itm-tool', '2026-08-06', '2026-08-06T09:00:00+00:00', 'CANARY_PRIVATE_ACTOR',
                    'ev-high', 'mdl-old', 'mdl-current', 'identity_correction'
                )
                """
            )
            con.execute(
                """
                INSERT INTO item_detail_amendments (
                    detail_amendment_id, item_id, amended_on, recorded_at, actor,
                    evidence_id, previous_json, changes_json, sensitivity, notes
                ) VALUES (?, 'itm-tool', '2026-08-06', '2026-08-06T10:00:00+00:00',
                          'CANARY_DETAIL_ACTOR', 'ev-high', ?, ?, 'high',
                          'CANARY_DETAIL_NOTES')
                """,
                (
                    "detail-high",
                    json.dumps(
                        {
                            "acquired_on": "2025-01-02",
                            "condition": "CANARY_DETAIL_PREVIOUS_CONDITION",
                            "purchase_currency": "GBP",
                            "purchase_price": 12.5,
                            "receipt_ref": "CANARY_PRIVATE_RECEIPT",
                            "serial_or_lot": "CANARY_PRIVATE_SERIAL",
                        },
                        sort_keys=True,
                    ),
                    json.dumps({"condition": "CANARY_PRIVATE_CONDITION"}, sort_keys=True),
                ),
            )
            con.execute(
                """
                INSERT INTO fact_amendments (
                    fact_amendment_id, table_name, selector_json, amended_on,
                    recorded_at, actor, evidence_id, action, previous_json,
                    replacement_json, sensitivity, reason, notes
                ) VALUES (?, 'valuations', ?, '2026-08-06', '2026-08-06T11:00:00+00:00',
                          'CANARY_FACT_ACTOR', 'ev-high', 'replace', ?, ?, 'high',
                          'CANARY_FACT_REASON', 'CANARY_FACT_NOTES')
                """,
                (
                    "fact-high",
                    json.dumps({"valuation_id": "val-fact"}, sort_keys=True),
                    json.dumps(
                        {
                            "amount": 19,
                            "basis": "replacement",
                            "currency": "GBP",
                            "evidence_id": "ev-high",
                            "item_id": "itm-tool",
                            "sensitivity": "high",
                            "valuation_id": "val-fact",
                            "valued_on": "2026-08-05",
                        },
                        sort_keys=True,
                    ),
                    json.dumps(
                        {
                            "amount": 29,
                            "basis": "replacement",
                            "currency": "GBP",
                            "evidence_id": "ev-high",
                            "item_id": "itm-tool",
                            "sensitivity": "high",
                            "valuation_id": "val-fact",
                            "valued_on": "2026-08-06",
                        },
                        sort_keys=True,
                    ),
                ),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def render(self, scope: str) -> str:
        completed = subprocess.run(
            [
                sys.executable,
                str(RENDER),
                "--database",
                str(self.database),
                "--output",
                str(self.output),
                "--scope",
                scope,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return self.output.read_text()

    def add_reviewed_kit(self) -> None:
        with sqlite3.connect(self.database) as con:
            con.execute(
                "INSERT INTO models (model_id, name, category) VALUES ('mdl-kit', 'Visible kit subject', 'tool')"
            )
            con.execute(
                """
                INSERT INTO items (
                    item_id, model_id, quantity, unit, ownership_state,
                    sensitivity, primary_evidence_id
                ) VALUES ('itm-kit', 'mdl-kit', 1, 'item', 'confirmed', 'low', 'ev-low')
                """
            )
            con.execute(
                "INSERT INTO kits (kit_id, name, serves_item_id, evidence_id) VALUES ('kit-visible', 'Visible kit', 'itm-kit', 'ev-low')"
            )
            con.execute(
                """
                INSERT INTO kit_requirements (
                    kit_id, requirement_key, item_id, status, evidence_id,
                    recorded_at, verified_event_sequence
                ) VALUES (
                    'kit-visible', 'carry', NULL, 'needs_verification', 'ev-low',
                    '2026-08-06T08:00:00+00:00', NULL
                )
                """
            )
            con.executemany(
                """
                INSERT INTO kit_reviews (
                    review_id, kit_id, reviewed_on, recorded_at, actor,
                    completeness, requirement_keys_json, evidence_id, sensitivity
                ) VALUES (?, 'kit-visible', ?, ?, 'test', ?, '["carry"]', ?, ?)
                """,
                (
                    ("review-low-early", "2026-08-06", "2026-08-06T09:00:00+00:00", "complete", "ev-low", "low"),
                    ("review-low-late", "2026-08-06", "2026-08-06T10:00:00+00:00", "incomplete", "ev-low", "low"),
                    ("review-high-new", "2026-08-07", "2026-08-07T09:00:00+00:00", "complete", "ev-high", "high"),
                ),
            )

    def test_kit_completeness_comes_only_from_the_latest_visible_review(self) -> None:
        self.add_reviewed_kit()
        public = self.render("public")
        self.assertIn("Visible kit", public)
        self.assertIn("| Visible kit |", public)
        self.assertIn("| Unknown |", public)
        self.assertNotIn("Incomplete [reviewed 2026-08-06", public)
        self.assertNotIn("CANARY_HIGH_EVIDENCE_SOURCE", public)
        private = self.render("private")
        self.assertIn("Complete [reviewed 2026-08-07", private)
        self.assertIn("CANARY_HIGH_EVIDENCE_SOURCE", private)

    def test_stale_kit_source_presence_is_unknown_and_blocks_review_completeness(self) -> None:
        self.add_reviewed_kit()
        with sqlite3.connect(self.database) as con:
            con.execute(
                "INSERT INTO models (model_id, name, category) VALUES ('mdl-component', 'Kit component', 'tool')"
            )
            con.execute(
                """
                INSERT INTO items (
                    item_id, model_id, quantity, unit, ownership_state,
                    condition, sensitivity, primary_evidence_id
                ) VALUES ('itm-component', 'mdl-component', 1, 'item', 'confirmed', 'good', 'low', 'ev-low')
                """
            )
            con.execute(
                """
                INSERT INTO inventory_events (
                    event_id, sequence, item_id, event_type, occurred_on,
                    observed_on, occurred_on_precision, actor,
                    evidence_id, location_id, container_id, area_location_id,
                    context_quality, notes
                ) VALUES (
                    'event-component-verified', 100, 'itm-component',
                    'physically_verified', '2026-08-06', '2026-08-06', 'exact',
                    'test', 'ev-low',
                    NULL, NULL, NULL, 'bound', NULL
                )
                """
            )
            con.execute(
                """
                UPDATE kit_requirements
                SET item_id='itm-component', status='source_present',
                    verified_event_sequence=100
                WHERE kit_id='kit-visible' AND requirement_key='carry'
                """
            )
        current = self.render("private")
        self.assertIn("| source present | Complete [reviewed 2026-08-07", current)
        with sqlite3.connect(self.database) as con:
            con.execute(
                """
                INSERT INTO inventory_events (
                    event_id, sequence, item_id, event_type, occurred_on,
                    observed_on, occurred_on_precision, actor,
                    evidence_id, location_id, container_id, area_location_id,
                    context_quality, notes
                ) VALUES (
                    'event-component-moved', 101, 'itm-component', 'moved',
                    '2026-08-07', '2026-08-07', 'exact', 'test', 'ev-low',
                    NULL, NULL, NULL, 'bound', NULL
                )
                """
            )
        stale = self.render("private")
        self.assertIn("| Unknown | Unknown |", stale)
        self.assertNotIn("| source present | Complete [reviewed 2026-08-07", stale)

    def test_kit_source_present_requires_an_explicitly_usable_condition(self) -> None:
        self.add_reviewed_kit()
        with sqlite3.connect(self.database) as con:
            con.execute(
                "INSERT INTO models (model_id, name, category) VALUES ('mdl-ready-component', 'Ready component', 'tool')"
            )
            con.execute(
                """
                INSERT INTO items (
                    item_id, model_id, quantity, unit, ownership_state,
                    condition, sensitivity, primary_evidence_id
                ) VALUES (
                    'itm-ready-component', 'mdl-ready-component', 1, 'item',
                    'confirmed', 'functional', 'low', 'ev-low'
                )
                """
            )
            con.execute(
                """
                INSERT INTO inventory_events (
                    event_id, sequence, item_id, event_type, occurred_on,
                    observed_on, occurred_on_precision, actor,
                    evidence_id, location_id, container_id, area_location_id,
                    context_quality, notes
                ) VALUES (
                    'event-ready-component-verified', 100, 'itm-ready-component',
                    'physically_verified', '2026-08-06', '2026-08-06', 'exact',
                    'test', 'ev-low',
                    NULL, NULL, NULL, 'bound', NULL
                )
                """
            )
            con.execute(
                """
                UPDATE kit_requirements
                SET item_id='itm-ready-component', status='source_present',
                    verified_event_sequence=100
                WHERE kit_id='kit-visible' AND requirement_key='carry'
                """
            )
        usable = self.render("private")
        self.assertIn("| source present | Complete [reviewed 2026-08-07", usable)

        with sqlite3.connect(self.database) as con:
            con.execute(
                "UPDATE items SET condition='broken' WHERE item_id='itm-ready-component'"
            )
        broken = self.render("private")
        self.assertIn("| Unknown | Unknown |", broken)
        self.assertNotIn("| source present | Complete [reviewed 2026-08-07", broken)

        with sqlite3.connect(self.database) as con:
            con.execute(
                "UPDATE items SET condition=NULL WHERE item_id='itm-ready-component'"
            )
        missing = self.render("private")
        self.assertIn("| Unknown | Unknown |", missing)
        self.assertNotIn("| source present | Complete [reviewed 2026-08-07", missing)

    def test_latest_identity_amendment_uses_recorded_at_before_id(self) -> None:
        with sqlite3.connect(self.database) as con:
            con.execute(
                """
                INSERT INTO item_amendments (
                    amendment_id, item_id, amended_on, recorded_at, actor, evidence_id,
                    previous_model_id, target_model_id, reason
                ) VALUES (
                    'aaa-earlier-id-but-later-record', 'itm-tool', '2026-08-06',
                    '2026-08-06T10:00:00+00:00', 'test', 'ev-high',
                    'mdl-old', 'mdl-current', 'model_split'
                )
                """
            )
        note = self.render("private")
        self.assertIn("model split [amended 2026-08-06", note)
        self.assertIn("recorded 2026-08-06T10:00:00+00:00", note)
        self.assertNotIn("identity correction [amended 2026-08-06", note)

    def test_private_catalogue_projects_all_insurance_and_fact_context(self) -> None:
        note = self.render("private")
        for value in (
            "CANARY_PRIVATE_CONDITION",
            "CANARY_HIGH_IDENTITY_CURRENT_MODEL",
            "CANARY_HIGH_IDENTITY_INTERFACE",
            "CANARY_HIGH_IDENTITY_SPEC",
            "CANARY_HIGH_IDENTITY_URL",
            "CANARY_PRIVATE_SERIAL",
            "2025-01-02",
            "GBP 12.5",
            "GBP 30",
            "CANARY_PRIVATE_RECEIPT",
            "CANARY_PRIVATE_RECEIPT_URI",
            "CANARY_LOW_ALIAS",
            "CANARY_HIGH_ALIAS",
            "#canary-low-tag",
            "#canary-high-tag",
            "width 99 cm",
            "height 4 cm",
            "depth 3 cm",
            "recorded 2026-08-01T09:00:00+00:00",
            "replacement: GBP 999",
            "market: GBP 11",
            "identity correction",
            "CANARY_HIGH_EVIDENCE_SOURCE",
        ):
            with self.subTest(value=value):
                self.assertIn(value, note)
        self.assertNotIn("width 12 cm", note)

    def test_private_catalogue_projects_complete_amendment_audit_only_privately(self) -> None:
        private = self.render("private")
        self.assertIn("## Private amendment audit", private)
        for value in (
            "item_detail_amendments: detail-high",
            "fact_amendments: fact-high",
            "2026-08-06T10:00:00+00:00",
            "2026-08-06T11:00:00+00:00",
            "CANARY_DETAIL_ACTOR",
            "CANARY_FACT_ACTOR",
            "CANARY_HIGH_EVIDENCE_SOURCE",
            "table items; action change",
            "table valuations; action replace; reason CANARY_FACT_REASON",
            "CANARY_DETAIL_PREVIOUS_CONDITION",
            "amount: 19",
            "amount: 29",
            "valuation_id: val-fact",
        ):
            with self.subTest(value=value):
                self.assertIn(value, private)
        for scope in ("public", "personal"):
            with self.subTest(scope=scope):
                note = self.render(scope)
                self.assertNotIn("## Private amendment audit", note)
                for hidden in (
                    "detail-high",
                    "fact-high",
                    "CANARY_DETAIL_ACTOR",
                    "CANARY_FACT_ACTOR",
                    "CANARY_FACT_REASON",
                    "CANARY_DETAIL_PREVIOUS_CONDITION",
                    "amount: 29",
                ):
                    self.assertNotIn(hidden, note)

    def test_lower_scopes_never_fall_back_to_stale_or_high_fact_rows(self) -> None:
        for scope in ("public", "personal"):
            with self.subTest(scope=scope):
                note = self.render(scope)
                self.assertIn("[identity redacted]", note)
                for hidden in (
                    "CANARY_HIGH_IDENTITY_CURRENT_MODEL",
                    "CANARY_HIGH_IDENTITY_OLD_MODEL",
                    "CANARY_HIGH_IDENTITY_BRAND",
                    "CANARY_HIGH_IDENTITY_MODEL",
                    "CANARY_HIGH_IDENTITY_INTERFACE",
                    "CANARY_HIGH_IDENTITY_SPEC",
                    "CANARY_HIGH_IDENTITY_URL",
                    "CANARY_LOW_ALIAS",
                    "#canary-low-tag",
                    "CANARY_PRIVATE_CONDITION",
                    "CANARY_PRIVATE_SERIAL",
                    "CANARY_PRIVATE_RECEIPT",
                    "CANARY_PRIVATE_RECEIPT_URI",
                    "CANARY_PRIVATE_NOTES",
                    "CANARY_HIGH_ALIAS",
                    "#canary-high-tag",
                    "CANARY_HIGH_EVIDENCE_SOURCE",
                    "width 99 cm",
                    "width 12 cm",
                    "replacement: GBP 999",
                    "replacement: GBP 10",
                    "identity correction",
                ):
                    self.assertNotIn(hidden, note)
                self.assertIn("height 4 cm", note)
                self.assertIn("depth 3 cm", note)
                self.assertIn("market: GBP 11", note)

    def test_identity_redaction_keeps_lower_scope_digest_model_blind(self) -> None:
        private_before = self.render("private")
        public_before = self.render("public")
        private_digest = re.search(r"canonical-inventory-sha256:([0-9a-f]{64})", private_before)
        public_digest = re.search(r"canonical-inventory-sha256:([0-9a-f]{64})", public_before)
        self.assertIsNotNone(private_digest)
        self.assertIsNotNone(public_digest)
        with sqlite3.connect(self.database) as con:
            con.execute(
                "UPDATE models SET name=? WHERE model_id='mdl-current'",
                ("CANARY_HIGH_IDENTITY_CORRECTION_AFTER",),
            )
        private_after = self.render("private")
        public_after = self.render("public")
        self.assertIn("CANARY_HIGH_IDENTITY_CORRECTION_AFTER", private_after)
        self.assertNotIn("CANARY_HIGH_IDENTITY_CORRECTION_AFTER", public_after)
        self.assertNotEqual(
            private_digest.group(1),
            re.search(r"canonical-inventory-sha256:([0-9a-f]{64})", private_after).group(1),
        )
        self.assertEqual(
            public_digest.group(1),
            re.search(r"canonical-inventory-sha256:([0-9a-f]{64})", public_after).group(1),
        )

    def test_private_digest_changes_for_every_rendered_v6_item_field(self) -> None:
        row = {
            "item_id": "itm-tool",
            "name": "Measured tool",
            "category": "tool",
            "quantity": 1,
            "unit": "item",
            "ownership_state": "confirmed",
            "location": None,
            "container": None,
            "verified_on": "2026-08-06",
            "evidence_type": "physical_check",
            "claim_strength": "explicit_current",
            "interfaces_json": "[]",
            "specs_json": "{}",
            "aliases": [{"alias": "Alias", "alias_kind": "common_name", "evidence_type": "physical_check", "claim_strength": "explicit_current", "captured_on": "2026-08-06", "source_ref": "alias source"}],
            "tags": [{"tag": "tag", "evidence_type": "physical_check", "claim_strength": "explicit_current", "captured_on": "2026-08-06", "source_ref": "tag source"}],
            "dimensions": {"axes": {"width": {"width": 1, "unit": "cm", "measured_on": "2026-08-06", "recorded_at": "2026-08-06T09:00:00+00:00", "evidence_type": "physical_check", "claim_strength": "explicit_current", "captured_on": "2026-08-06", "source_ref": "dimension source"}}},
            "valuations": [{"amount": 3, "currency": "GBP", "valued_on": "2026-08-06", "basis": "replacement", "evidence_type": "physical_check", "claim_strength": "explicit_current", "captured_on": "2026-08-06", "source_ref": "valuation source"}],
            "amendment": {"amended_on": "2026-08-06", "recorded_at": "2026-08-06T09:00:00+00:00", "reason": "identity_correction", "evidence_type": "physical_check", "claim_strength": "explicit_current", "captured_on": "2026-08-06", "source_ref": "amendment source"},
            "condition": "Good",
            "serial_or_lot": "ABC",
            "acquired_on": "2025-01-01",
            "purchase_price": 1,
            "purchase_currency": "GBP",
            "replacement_value": 2,
            "value_currency": "GBP",
            "receipt_ref": "receipt",
            "receipt_documents": [{"uri": "receipt-uri"}],
            "audit_history": [
                {
                    "item": "Measured tool",
                    "item_id": "itm-tool",
                    "record_kind": "fact_amendments",
                    "record_id": "fact-1",
                    "table_name": "valuations",
                    "action": "replace",
                    "reason": "correction",
                    "selector": {"valuation_id": "val-1"},
                    "amended_on": "2026-08-06",
                    "recorded_at": "2026-08-06T10:00:00+00:00",
                    "actor": "auditor",
                    "evidence_id": "ev-1",
                    "evidence_type": "physical_check",
                    "claim_strength": "explicit_current",
                    "captured_on": "2026-08-06",
                    "source_ref": "audit source",
                    "previous": {"amount": 3},
                    "replacement_or_changes": {"amount": 4},
                }
            ],
            "notes": "note",
            "reference_url": "https://example.test/tool",
        }
        payload = {
            "inventory": [row],
            "states": [{"state": "confirmed", "rows": 1}],
            "relationships": [],
            "kits": [],
            "torque_paths": [],
        }

        def digest(value: dict, scope: str = "private") -> str:
            return RENDER_MODULE.catalogue_digest(
                value["inventory"], value["states"], value["relationships"],
                value["kits"], value["torque_paths"], scope,
            )

        baseline = digest(payload)
        for field in (
            "condition", "serial_or_lot", "acquired_on", "purchase_price",
            "purchase_currency", "replacement_value", "value_currency",
            "receipt_ref", "notes", "reference_url",
        ):
            with self.subTest(field=field):
                changed = deepcopy(payload)
                changed["inventory"][0][field] = f"changed-{field}"
                self.assertNotEqual(baseline, digest(changed))
        for nested, field in (
            ("aliases", "alias"), ("tags", "tag"),
            ("amendment", "reason"),
            ("receipt_documents", "uri"),
        ):
            with self.subTest(nested=nested, field=field):
                changed = deepcopy(payload)
                target = changed["inventory"][0][nested]
                if isinstance(target, list):
                    target[0][field] = f"changed-{nested}"
                else:
                    target[field] = f"changed-{nested}"
                self.assertNotEqual(baseline, digest(changed))

        changed_dimension = deepcopy(payload)
        changed_dimension["inventory"][0]["dimensions"]["axes"]["width"]["width"] = 9
        self.assertNotEqual(baseline, digest(changed_dimension))
        changed_dimension_timestamp = deepcopy(payload)
        changed_dimension_timestamp["inventory"][0]["dimensions"]["axes"]["width"]["recorded_at"] = "2026-08-06T10:00:00+00:00"
        self.assertNotEqual(baseline, digest(changed_dimension_timestamp))
        changed_valuation = deepcopy(payload)
        changed_valuation["inventory"][0]["valuations"][0]["amount"] = 9
        self.assertNotEqual(baseline, digest(changed_valuation))
        changed_amendment_timestamp = deepcopy(payload)
        changed_amendment_timestamp["inventory"][0]["amendment"]["recorded_at"] = "2026-08-06T10:00:00+00:00"
        self.assertNotEqual(baseline, digest(changed_amendment_timestamp))

        changed_audit_actor = deepcopy(payload)
        changed_audit_actor["inventory"][0]["audit_history"][0]["actor"] = "changed"
        self.assertNotEqual(baseline, digest(changed_audit_actor))
        changed_audit_previous = deepcopy(payload)
        changed_audit_previous["inventory"][0]["audit_history"][0]["previous"]["amount"] = 9
        self.assertNotEqual(baseline, digest(changed_audit_previous))
        changed_audit_replacement = deepcopy(payload)
        changed_audit_replacement["inventory"][0]["audit_history"][0]["replacement_or_changes"]["amount"] = 9
        self.assertNotEqual(baseline, digest(changed_audit_replacement))

        hidden_source_change = deepcopy(payload)
        hidden_source_change["inventory"][0]["dimensions"]["axes"]["width"]["source_ref"] = "changed"
        self.assertEqual(digest(payload, "personal"), digest(hidden_source_change, "personal"))
        self.assertNotEqual(digest(payload), digest(hidden_source_change))
        self.assertEqual(digest(payload, "public"), digest(changed_audit_actor, "public"))
        self.assertEqual(digest(payload, "personal"), digest(changed_audit_actor, "personal"))

    def test_private_render_is_deterministic(self) -> None:
        first = self.render("private")
        second = self.render("private")
        self.assertEqual(first, second)
        self.assertRegex(first, r"canonical-inventory-sha256:[0-9a-f]{64}")


if __name__ == "__main__":
    unittest.main()
