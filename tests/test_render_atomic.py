"""Focused renderer scope and atomic-output tests."""

from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
RENDER = HERE / "src" / "property_inventory" / "render.py"
SCHEMA = HERE / "src" / "property_inventory" / "schema.sql"
MODULE_SPEC = importlib.util.spec_from_file_location("inventory_render", RENDER)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
RENDER_MODULE = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(RENDER_MODULE)


class RenderScopeAndAtomicWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-render-")
        self.root = Path(self.temp.name)
        self.database = self.root / "inventory.sqlite"
        self.output = self.root / "Inventory.md"
        self.seed_database()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def seed_database(self) -> None:
        with sqlite3.connect(self.database) as con:
            con.executescript(SCHEMA.read_text())
            con.execute(
                "INSERT INTO metadata (inventory_id, schema_version) VALUES (?, 7)",
                ("inv-render-test-owner-a",),
            )
            con.executemany(
                """
                INSERT INTO locations (
                    location_id, name, parent_location_id, kind, sensitivity
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("loc-low-safe", "CANARY_LOW_SAFE_LOCATION_RETAINED", None, "room", "low"),
                    ("loc-high-parent", "CANARY_HIGH_ANCESTOR_SECRET", None, "place", "high"),
                    (
                        "loc-low-child",
                        "CANARY_EXACT_LOCATION_SECRET",
                        "loc-high-parent",
                        "room",
                        "low",
                    ),
                    (
                        "loc-low-container",
                        "CANARY_EXACT_CONTAINER_SECRET",
                        "loc-low-child",
                        "container",
                        "low",
                    ),
                ],
            )
            con.executemany(
                "INSERT INTO models (model_id, name, category) VALUES (?, ?, 'test')",
                [
                    ("mdl-low", "CANARY_LOW_ITEM_RETAINED"),
                    ("mdl-personal", "CANARY_PERSONAL_ITEM_RETAINED"),
                    ("mdl-high", "CANARY_HIGH_ITEM_SECRET"),
                ],
            )
            con.execute(
                """
                UPDATE models
                SET interfaces_json=?, specs_json=?, reference_url=?
                WHERE model_id='mdl-low'
                """,
                (
                    '["CANARY_LOW_INTERFACE_RETAINED"]',
                    '{"detail":"CANARY_LOW_SPEC_RETAINED"}',
                    "https://private.example/CANARY_REFERENCE_URL_SECRET",
                ),
            )
            con.executemany(
                """
                INSERT INTO evidence (
                    evidence_id, evidence_type, source_ref, captured_on, claim_strength, sensitivity
                ) VALUES (?, 'physical_check', ?, '2026-08-05', 'explicit_current', ?)
                """,
                [
                    ("ev-low", "CANARY_EVIDENCE_SOURCE_REF_SECRET", "low"),
                    ("ev-personal", "CANARY_PERSONAL_EVIDENCE_SOURCE", "personal"),
                    ("ev-high", "CANARY_HIGH_EVIDENCE_SOURCE_SECRET", "high"),
                ],
            )
            con.execute(
                "UPDATE evidence SET evidence_type='research', "
                "claim_strength='research_only' WHERE evidence_id='ev-high'"
            )
            con.executemany(
                """
                INSERT INTO items (
                    item_id, model_id, quantity, unit, ownership_state, sensitivity,
                    location_id, container_id, primary_evidence_id, notes
                ) VALUES (?, ?, 1, 'item', ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "itm-low",
                        "mdl-low",
                        "confirmed",
                        "low",
                        "loc-low-child",
                        "loc-low-container",
                        "ev-high",
                        "CANARY_ITEM_NOTES_SECRET",
                    ),
                    (
                        "itm-personal",
                        "mdl-personal",
                        "candidate",
                        "personal",
                        "loc-low-safe",
                        None,
                        "ev-personal",
                        "CANARY_PERSONAL_ITEM_NOTES_SECRET",
                    ),
                    ("itm-high", "mdl-high", "confirmed", "high", "loc-high-parent", None, "ev-high", "CANARY_HIGH_ITEM_NOTES_SECRET"),
                ],
            )
            con.executemany(
                """
                INSERT INTO relationships (
                    relationship_id, subject_item_id, predicate, object_item_id, confidence,
                    evidence_id, notes
                ) VALUES (?, ?, 'works_with', ?, 'verified', ?, ?)
                """,
                [
                    ("rel-low", "itm-low", "itm-personal", "ev-low", "CANARY_RELATIONSHIP_NOTES_SECRET"),
                    ("rel-high", "itm-low", "itm-high", "ev-high", "CANARY_HIGH_RELATIONSHIP_SECRET"),
                ],
            )
            con.execute(
                """
                INSERT INTO relationships (
                    relationship_id, subject_item_id, predicate, object_item_id,
                    confidence, evidence_id, notes
                ) VALUES (
                    'rel-hidden-claim', 'itm-low', 'requires', 'itm-personal',
                    'verified', 'ev-high', 'CANARY_HIGH_EVIDENCE_RELATIONSHIP'
                )
                """
            )
            con.executemany(
                "INSERT INTO kits (kit_id, name, serves_item_id, evidence_id) VALUES (?, ?, ?, ?)",
                [
                    ("kit-visible", "Visible kit", "itm-low", "ev-low"),
                    ("kit-hidden", "Hidden kit", "itm-low", "ev-high"),
                ],
            )
            con.executemany(
                """
                INSERT INTO kit_requirements (
                    kit_id, requirement_key, item_id, status, evidence_id, recorded_at,
                    verified_event_sequence, notes
                ) VALUES (?, 'test_requirement', ?, 'source_present', ?, '2026-08-05T00:00:00+00:00', 1, ?)
                """,
                [
                    ("kit-visible", "itm-personal", "ev-personal", "CANARY_KIT_NOTES_SECRET"),
                    ("kit-hidden", "itm-low", "ev-low", "CANARY_HIGH_KIT_SECRET"),
                ],
            )
            con.execute(
                """
                INSERT INTO kit_requirements (
                    kit_id, requirement_key, item_id, status, evidence_id, recorded_at,
                    verified_event_sequence, notes
                ) VALUES (
                    'kit-visible', 'CANARY_HIGH_EVIDENCE_REQUIREMENT', 'itm-low',
                    'source_present', 'ev-high', '2026-08-05T00:00:00+00:00', 1,
                    'CANARY_HIGH_EVIDENCE_REQUIREMENT_NOTE'
                )
                """
            )
            con.executemany(
                """
                INSERT INTO torque_paths (
                    path_id, tool_item_id, output_drive, min_torque_nm, max_torque_nm,
                    status, evidence_id, notes
                ) VALUES (?, ?, ?, 2, 10, 'direct', ?, ?)
                """,
                [
                    ("tp-low", "itm-low", "1/4 inch", "ev-low", "CANARY_TORQUE_NOTES_SECRET"),
                    (
                        "tp-high",
                        "itm-low",
                        "CANARY_HIGH_EVIDENCE_DRIVE",
                        "ev-high",
                        "CANARY_HIGH_TORQUE_SECRET",
                    ),
                ],
            )

    def render(
        self,
        scope: str,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(RENDER),
                "--database",
                str(self.database),
                "--output",
                str(self.output),
                "--scope",
                scope,
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

    def test_non_private_scopes_remove_every_sensitive_canary(self) -> None:
        public = self.render("public")
        self.assertEqual(public.returncode, 0, public.stderr)
        public_note = self.output.read_text()
        self.assertIn('scope: "public"', public_note)
        self.assertIn("scope:public", public_note)
        self.assertIn("CANARY_LOW_ITEM_RETAINED", public_note)
        self.assertIn("CANARY_LOW_INTERFACE_RETAINED", public_note)
        self.assertIn("CANARY_LOW_SPEC_RETAINED", public_note)
        self.assertIn("[redacted]", public_note)
        self.assertNotIn("research only", public_note)
        self.assertNotIn("CANARY_PERSONAL_ITEM_RETAINED", public_note)
        self.assertNotIn("| candidate |", public_note)
        self.assertNotIn("| Lent |", public_note)
        self.assertNotIn("Visible kit", public_note)
        self.assertNotIn("Hidden kit", public_note)
        self.assertIn("**1 item or stock rows** across **1 lifecycle states**", public_note)

        personal = self.render("personal")
        self.assertEqual(personal.returncode, 0, personal.stderr)
        personal_note = self.output.read_text()
        self.assertIn("CANARY_PERSONAL_ITEM_RETAINED", personal_note)
        self.assertIn("CANARY_LOW_SAFE_LOCATION_RETAINED", personal_note)
        self.assertIn("Visible kit", personal_note)
        self.assertNotIn("CANARY_HIGH_ITEM_SECRET", personal_note)
        self.assertNotIn("Hidden kit", personal_note)
        self.assertNotIn("| Lent |", personal_note)

        secret_canaries = (
            "CANARY_REFERENCE_URL_SECRET",
            "CANARY_ITEM_NOTES_SECRET",
            "CANARY_PERSONAL_ITEM_NOTES_SECRET",
            "CANARY_HIGH_ITEM_NOTES_SECRET",
            "CANARY_HIGH_ITEM_SECRET",
            "CANARY_HIGH_ANCESTOR_SECRET",
            "CANARY_EXACT_LOCATION_SECRET",
            "CANARY_EXACT_CONTAINER_SECRET",
            "CANARY_EVIDENCE_SOURCE_REF_SECRET",
            "CANARY_PERSONAL_EVIDENCE_SOURCE",
            "CANARY_HIGH_EVIDENCE_SOURCE_SECRET",
            "CANARY_RELATIONSHIP_NOTES_SECRET",
            "CANARY_HIGH_RELATIONSHIP_SECRET",
            "CANARY_KIT_NOTES_SECRET",
            "CANARY_HIGH_KIT_SECRET",
            "CANARY_TORQUE_NOTES_SECRET",
            "CANARY_HIGH_TORQUE_SECRET",
            "CANARY_HIGH_EVIDENCE_RELATIONSHIP",
            "CANARY_HIGH_EVIDENCE_REQUIREMENT",
            "CANARY_HIGH_EVIDENCE_REQUIREMENT_NOTE",
            "CANARY_HIGH_EVIDENCE_DRIVE",
        )
        for canary in secret_canaries:
            with self.subTest(scope="public", canary=canary):
                self.assertNotIn(canary, public_note)
            with self.subTest(scope="personal", canary=canary):
                self.assertNotIn(canary, personal_note)

        private = self.render("private")
        self.assertEqual(private.returncode, 0, private.stderr)
        private_note = self.output.read_text()
        self.assertIn("CANARY_HIGH_ITEM_SECRET", private_note)
        self.assertIn("Hidden kit", private_note)
        self.assertIn("research only", private_note)
        self.assertIn("scope=private", private.stdout)
        for canary in secret_canaries:
            with self.subTest(scope="private", canary=canary):
                self.assertIn(canary, private_note)

        personal_digest = re.search(
            r"canonical-inventory-sha256:([0-9a-f]{64})", personal_note
        )
        self.assertIsNotNone(personal_digest)
        with sqlite3.connect(self.database) as con:
            con.execute(
                "UPDATE items SET notes=? WHERE item_id='itm-low'",
                ("CANARY_DIGEST_ONLY_PRIVATE_NOTE_CHANGE",),
            )
        changed = self.render("personal")
        self.assertEqual(changed.returncode, 0, changed.stderr)
        changed_note = self.output.read_text()
        changed_digest = re.search(
            r"canonical-inventory-sha256:([0-9a-f]{64})", changed_note
        )
        self.assertIsNotNone(changed_digest)
        self.assertEqual(personal_digest.group(1), changed_digest.group(1))
        self.assertNotIn("CANARY_DIGEST_ONLY_PRIVATE_NOTE_CHANGE", changed_note)
        original_stdout_digest = re.search(
            r"canonical_digest=([0-9a-f]{64})", personal.stdout
        )
        changed_stdout_digest = re.search(
            r"canonical_digest=([0-9a-f]{64})", changed.stdout
        )
        self.assertIsNotNone(original_stdout_digest)
        self.assertIsNotNone(changed_stdout_digest)
        self.assertEqual(
            original_stdout_digest.group(1), changed_stdout_digest.group(1)
        )

        with sqlite3.connect(self.database) as con:
            con.execute(
                "UPDATE items SET sensitivity='personal' WHERE item_id='itm-low'"
            )
        sensitivity_changed = self.render("personal")
        self.assertEqual(sensitivity_changed.returncode, 0, sensitivity_changed.stderr)
        self.assertEqual(self.output.read_text(), changed_note)
        sensitivity_stdout_digest = re.search(
            r"canonical_digest=([0-9a-f]{64})", sensitivity_changed.stdout
        )
        self.assertIsNotNone(sensitivity_stdout_digest)
        self.assertEqual(
            changed_stdout_digest.group(1), sensitivity_stdout_digest.group(1)
        )

    def test_default_scope_is_personal(self) -> None:
        default = subprocess.run(
            [
                sys.executable,
                str(RENDER),
                "--database",
                str(self.database),
                "--output",
                str(self.output),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(default.returncode, 0, default.stderr)
        note = self.output.read_text()
        self.assertIn('scope: "personal"', note)
        self.assertIn("CANARY_PERSONAL_ITEM_RETAINED", note)
        self.assertNotIn("CANARY_HIGH_ITEM_SECRET", note)
        self.assertIn("scope=personal", default.stdout)

    def test_catalogue_digest_covers_every_visible_table_datum(self) -> None:
        payload = {
            "inventory": [{
                "item_id": "item",
                "name": "Item",
                "category": "Category",
                "quantity": 1,
                "unit": "item",
                "ownership_state": "confirmed",
                "location": "Location",
                "container": "Container",
                "verified_on": "2026-08-05",
                "evidence_type": "physical_check",
                "claim_strength": "explicit_current",
                "interfaces_json": '["interface"]',
                "specs_json": '{"spec":"value"}',
                "notes": "Private note",
                "reference_url": "https://example.test/item",
            }],
            "states": [{"state": "confirmed", "rows": 1}],
            "relationships": [{
                "subject": "Item",
                "predicate": "works with",
                "object": "Other item",
                "confidence": "verified",
                "source_ref": "Private relationship evidence",
                "notes": "Private relationship note",
            }],
            "kits": [{
                "kit": "Kit",
                "serves": "Item",
                "requirement": "Requirement",
                "matched_item": "Other item",
                "status": "source present",
                "source_ref": "Private kit evidence",
                "notes": "Private kit note",
            }],
            "torque_paths": [{
                "tool": "Item",
                "output_drive": "1/4 inch",
                "range_nm": "2-10",
                "adapter": "Adapter",
                "status": "direct",
                "source_ref": "Private torque evidence",
                "notes": "Private torque note",
            }],
        }
        def digest(value: dict, scope: str = "private") -> str:
            return RENDER_MODULE.catalogue_digest(
                value["inventory"],
                value["states"],
                value["relationships"],
                value["kits"],
                value["torque_paths"],
                scope,
            )
        baseline = digest(payload)
        visible_fields = {
            "inventory": (
                "item_id", "name", "category", "quantity", "unit",
                "ownership_state", "location", "container", "verified_on",
                "evidence_type", "claim_strength", "interfaces_json", "specs_json",
                "notes", "reference_url",
            ),
            "states": ("state", "rows"),
            "relationships": (
                "subject", "predicate", "object", "confidence", "source_ref", "notes",
            ),
            "kits": (
                "kit", "serves", "requirement", "matched_item", "status",
                "source_ref", "notes",
            ),
            "torque_paths": (
                "tool", "output_drive", "range_nm", "adapter", "status",
                "source_ref", "notes",
            ),
        }
        for table_name, fields in visible_fields.items():
            for field in fields:
                with self.subTest(table=table_name, field=field):
                    changed = deepcopy(payload)
                    changed[table_name][0][field] = f"changed-{field}"
                    self.assertNotEqual(baseline, digest(changed))

        changed_item_count = deepcopy(payload)
        changed_item_count["inventory"].append(deepcopy(payload["inventory"][0]))
        self.assertNotEqual(baseline, digest(changed_item_count))
        changed_state_count = deepcopy(payload)
        changed_state_count["states"].append({"state": "candidate", "rows": 1})
        self.assertNotEqual(baseline, digest(changed_state_count))

    def test_non_private_catalogue_digest_excludes_private_fields(self) -> None:
        payload = {
            "inventory": [{
                "item_id": "item",
                "name": "Item",
                "category": "Category",
                "quantity": 1,
                "unit": "item",
                "ownership_state": "confirmed",
                "location": "Location",
                "container": "Container",
                "verified_on": "2026-08-05",
                "evidence_type": "physical_check",
                "claim_strength": "explicit_current",
                "interfaces_json": "[]",
                "specs_json": "{}",
                "notes": "Private item note",
                "reference_url": "https://example.test/private",
            }],
            "states": [{"state": "confirmed", "rows": 1}],
            "relationships": [{
                "subject": "Item", "predicate": "works with", "object": "Other",
                "confidence": "verified", "source_ref": "Private evidence", "notes": "Private note",
            }],
            "kits": [{
                "kit": "Kit", "serves": "Item", "requirement": "Requirement",
                "matched_item": "Other", "status": "source present",
                "source_ref": "Private evidence", "notes": "Private note",
            }],
            "torque_paths": [{
                "tool": "Item", "output_drive": "1/4 inch", "range_nm": "2-10",
                "adapter": "", "status": "direct", "source_ref": "Private evidence",
                "notes": "Private note",
            }],
        }
        for scope in ("public", "personal"):
            with self.subTest(scope=scope):
                baseline = RENDER_MODULE.catalogue_digest(
                    payload["inventory"], payload["states"], payload["relationships"],
                    payload["kits"], payload["torque_paths"], scope,
                )
                changed = deepcopy(payload)
                changed["inventory"][0]["notes"] = "Changed private item note"
                changed["inventory"][0]["reference_url"] = "https://example.test/changed"
                for table_name in ("relationships", "kits", "torque_paths"):
                    changed[table_name][0]["source_ref"] = "Changed private evidence"
                    changed[table_name][0]["notes"] = "Changed private note"
                self.assertEqual(
                    baseline,
                    RENDER_MODULE.catalogue_digest(
                        changed["inventory"], changed["states"],
                        changed["relationships"], changed["kits"],
                        changed["torque_paths"], scope,
                    ),
                )

    def test_failure_before_replace_preserves_old_bytes_and_cleans_temporary_file(self) -> None:
        current = self.render("private")
        self.assertEqual(current.returncode, 0, current.stderr)
        original = re.sub(
            r"^<!-- canonical-inventory-owner-sha256:[0-9a-f]{64} -->\n",
            "",
            self.output.read_text(),
            flags=re.MULTILINE,
        ).encode()
        self.output.write_bytes(original)
        environment = {**os.environ, "PROPERTY_INVENTORY_FAIL_BEFORE_RENDER_REPLACE": "1"}

        failed = self.render("private", environment=environment)

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("injected failure before render output replacement", failed.stderr)
        self.assertEqual(self.output.read_bytes(), original)
        self.assertEqual(list(self.root.glob(f".{self.output.name}.tmp-*")), [])

    def test_render_preserves_created_property_and_is_idempotent(self) -> None:
        first = self.render("private")
        self.assertEqual(first.returncode, 0, first.stderr)
        initial = self.output.read_text()
        self.assertIn(f"Created: {date.today().isoformat()}\n", initial)

        historical = initial.replace(
            f"Created: {date.today().isoformat()}\n",
            "Created: 2020-01-02\n",
        )
        self.output.write_text(historical)
        second = self.render("private")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.output.read_text(), historical)
        third = self.render("private")
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(self.output.read_text(), historical)

    def test_explicit_created_property_requires_a_valid_iso_date(self) -> None:
        for invalid in ("", "not-a-date", "2026-02-30"):
            with self.subTest(invalid=invalid):
                self.output.unlink(missing_ok=True)
                failed = self.render("private", "--created-on", invalid)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn("must be an ISO date", failed.stderr)
                self.assertFalse(self.output.exists())

    def test_catalogue_owner_rejects_another_inventory_and_adopts_exact_legacy(self) -> None:
        first = self.render("private")
        self.assertEqual(first.returncode, 0, first.stderr)
        owner_a_note = self.output.read_text()
        owner_a = re.search(
            r"canonical-inventory-owner-sha256:([0-9a-f]{64})", owner_a_note
        )
        self.assertIsNotNone(owner_a)

        with sqlite3.connect(self.database) as con:
            con.execute(
                "UPDATE metadata SET inventory_id=?",
                ("inv-render-test-owner-b",),
            )
        second = self.render("private")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("different inventory owner", second.stderr)
        self.assertEqual(self.output.read_text(), owner_a_note)

        with sqlite3.connect(self.database) as con:
            con.execute(
                "UPDATE metadata SET inventory_id=?",
                ("inv-render-test-owner-a",),
            )
        legacy_note = re.sub(
            r"^<!-- canonical-inventory-owner-sha256:[0-9a-f]{64} -->\n",
            "",
            owner_a_note,
            flags=re.MULTILINE,
        )
        created = re.search(r"^Created: (\d{4}-\d{2}-\d{2})$", legacy_note, re.MULTILINE)
        self.assertIsNotNone(created)
        legacy_without_created = re.sub(
            r"^Created: \d{4}-\d{2}-\d{2}\n",
            "",
            legacy_note,
            flags=re.MULTILINE,
        )
        relocated = legacy_without_created.replace(
            "# Property Inventory Catalogue\n",
            f"# Property Inventory Catalogue\n\nCreated: {created.group(1)}\n",
        )
        self.output.write_text(relocated)
        rejected = self.render("private")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("not an exact legacy render", rejected.stderr)
        self.assertEqual(self.output.read_text(), relocated)

        legacy_note = legacy_without_created
        self.output.write_text(legacy_note)
        adopted = self.render("private")
        self.assertEqual(adopted.returncode, 0, adopted.stderr)
        self.assertEqual(self.output.read_text(), owner_a_note)

    def test_symlinked_output_boundaries_preserve_external_bytes(self) -> None:
        payload = b"external renderer sentinel\n"
        external = self.root / "external"
        external.mkdir()
        sentinel = external / "Inventory.md"
        sentinel.write_bytes(payload)

        leaf = self.root / "leaf-link.md"
        leaf.symlink_to(sentinel)
        self.output = leaf
        leaf_failed = self.render("private")
        self.assertNotEqual(leaf_failed.returncode, 0)
        self.assertIn("managed symlink", leaf_failed.stderr)
        self.assertEqual(sentinel.read_bytes(), payload)

        leaf.unlink()
        parent = self.root / "parent-link"
        parent.symlink_to(external, target_is_directory=True)
        self.output = parent / "Inventory.md"
        parent_failed = self.render("private")
        self.assertNotEqual(parent_failed.returncode, 0)
        self.assertIn("managed symlink", parent_failed.stderr)
        self.assertEqual(sentinel.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
