"""Focused regression tests for the pure Batch 9 replica sync core."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from property_inventory.sync import (  # noqa: E402
    SyncError,
    build_replica_bundle,
    build_store_snapshot,
    plan_three_way_merge,
    receipt_data,
    resolve_conflicts,
    verify_replica_bundle,
    verify_store_snapshot,
)

FIXTURES = Path(__file__).resolve().parent / "test_fixtures" / "sync"


def fixture() -> dict:
    tables = json.loads((FIXTURES / "base.json").read_text())
    for table in (
        "evidence",
        "inventory_events",
        "item_evidence",
        "item_amendments",
        "item_detail_amendments",
        "fact_amendments",
        "sync_receipts",
    ):
        tables.setdefault(table, [])
    return tables


def strict_fixture_validator(tables: dict[str, list[dict]]) -> None:
    """Small stand-in for the canonical verifier used by every ready-plan test."""
    if len(tables.get("metadata", [])) != 1:
        raise ValueError("fixture store requires one metadata row")
    for item in tables.get("items", []):
        quantity = item.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("fixture item quantity must be a positive integer")


class ReplicaSyncTests(unittest.TestCase):
    def bundle(self, base: dict, head: dict) -> dict:
        """Build a branch as the transaction layer would, not direct JSON edits.

        Most unit tests focus on deterministic merge mechanics. Their compact
        fixture changes an item inline, so add exact replayable events here and
        keep the direct-edit rejection tests below deliberately raw.
        """
        head = copy.deepcopy(head)
        base_items = {row["item_id"]: row for row in base["items"]}
        replica_items = {row["item_id"]: row for row in head["items"]}
        existing_events = head.setdefault("inventory_events", [])
        existing_evidence = head.setdefault("evidence", [])
        existing_item_evidence = head.setdefault("item_evidence", [])
        existing_item_amendments = head.setdefault("item_amendments", [])
        existing_detail_amendments = head.setdefault("item_detail_amendments", [])
        next_sequence = max((row["sequence"] for row in existing_events), default=0) + 1
        for item_id, before in base_items.items():
            after = replica_items.get(item_id)
            if after is None or before == after:
                continue
            changed = {field for field in set(before) | set(after) if before.get(field) != after.get(field)}
            if "model_id" in changed and not any(
                row.get("item_id") == item_id for row in existing_item_amendments
            ):
                existing_item_amendments.append(
                    {
                        "amendment_id": f"fixture-model-{item_id}",
                        "item_id": item_id,
                        "previous_model_id": before.get("model_id"),
                        "target_model_id": after.get("model_id"),
                    }
                )
            detail_fields = changed & {
                "acquired_on", "condition", "purchase_currency", "purchase_price", "receipt_ref", "serial_or_lot"
            }
            if detail_fields and not any(
                row.get("item_id") == item_id for row in existing_detail_amendments
            ):
                existing_detail_amendments.append(
                    {
                        "detail_amendment_id": f"fixture-detail-{item_id}",
                        "item_id": item_id,
                        "previous_json": json.dumps(
                            {
                                field: before.get(field)
                                for field in (
                                    "acquired_on", "condition", "purchase_currency", "purchase_price", "receipt_ref", "serial_or_lot"
                                )
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "changes_json": json.dumps(
                            {field: after.get(field) for field in detail_fields},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    }
                )
            lifecycle = changed & {
                "ownership_state", "location_id", "container_id", "quantity", "unit", "verified_on"
            }
            if not lifecycle:
                continue

            def append_event(
                event_type: str,
                *,
                claim_strength: str,
                evidence_type: str = "user_source",
                location_id: str | None = after.get("location_id"),
                container_id: str | None = after.get("container_id"),
                details: dict | None = None,
            ) -> None:
                nonlocal next_sequence
                evidence_id = f"fixture-evidence-{item_id}-{next_sequence}"
                existing_evidence.append(
                    {
                        "evidence_id": evidence_id,
                        "evidence_type": evidence_type,
                        "source_ref": "sync fixture",
                        "captured_on": "2026-08-06",
                        "claim_strength": claim_strength,
                        "sensitivity": "low",
                        "notes": None,
                    }
                )
                existing_events.append(
                    {
                        "event_id": f"fixture-event-{item_id}-{next_sequence}",
                        "sequence": next_sequence,
                        "item_id": item_id,
                        "event_type": event_type,
                        "occurred_on": "2026-08-06",
                        "observed_on": "2026-08-06",
                        "occurred_on_precision": "exact",
                        "actor": "sync fixture",
                        "evidence_id": evidence_id,
                        "location_id": location_id,
                        "container_id": container_id,
                        "area_location_id": None,
                        "context_quality": "bound",
                        "details_json": (
                            json.dumps(details, sort_keys=True)
                            if details is not None
                            else None
                        ),
                        "notes": None,
                    }
                )
                existing_item_evidence.append(
                    {
                        "item_id": item_id,
                        "evidence_id": evidence_id,
                        "role": "supporting",
                    }
                )
                next_sequence += 1

            state_event = {
                "candidate": "ordered",
                "confirmed": "received",
                "lent": "lent",
                "disposed": "disposed",
                "refunded": "refunded",
                "planned": "planned",
                "unknown": "ownership_unresolved",
                "not_owned": "ownership_excluded",
            }
            quantity_details = {
                "previous_quantity": before.get("quantity"),
                "previous_unit": before.get("unit"),
                "quantity": after.get("quantity"),
                "unit": after.get("unit"),
            }
            quantity_covered = False
            if "ownership_state" in lifecycle:
                target_state = after.get("ownership_state")
                event_type = state_event[target_state]
                terminal = target_state in {"disposed", "refunded", "not_owned"}
                claim = (
                    "purchase_only"
                    if target_state in {"candidate", "planned"}
                    else "claimed_owned"
                    if target_state == "unknown"
                    else "explicit_not_owned"
                    if terminal
                    else "explicit_current"
                )
                planned_details = (
                    quantity_details
                    if target_state == "planned" and lifecycle & {"quantity", "unit"}
                    else None
                )
                append_event(
                    event_type,
                    claim_strength=claim,
                    location_id=before.get("location_id") if terminal else after.get("location_id"),
                    container_id=before.get("container_id") if terminal else after.get("container_id"),
                    details=planned_details,
                )
                quantity_covered = planned_details is not None
            elif lifecycle & {"location_id", "container_id"}:
                append_event("moved", claim_strength="explicit_current")
            if lifecycle & {"quantity", "unit"} and not quantity_covered:
                append_event(
                    "quantity_changed",
                    claim_strength="explicit_current",
                    details=quantity_details,
                )
            if "verified_on" in lifecycle:
                append_event(
                    "physically_verified",
                    claim_strength="explicit_current",
                    evidence_type="physical_check",
                )
        return build_replica_bundle(
            inventory_id="inventory-fixture", replica_ref="replica-fixture", base=base, head=head
        )

    def add_planned_creation(
        self, tables: dict, branch: str, *, item_id: str = "item-collision"
    ) -> None:
        evidence_id = f"evidence-{branch}"
        tables["items"].append(
            {
                "item_id": item_id, "location_id": "room-a", "container_id": None,
                "ownership_state": "planned", "quantity": 1, "unit": "item",
                "verified_on": None, "primary_evidence_id": evidence_id, "notes": branch,
            }
        )
        tables["evidence"].append(
            {
                "evidence_id": evidence_id, "evidence_type": "user_source",
                "source_ref": f"{branch} plan", "captured_on": "2026-08-06",
                "claim_strength": "purchase_only", "sensitivity": "low", "notes": None,
            }
        )
        tables["item_evidence"].append(
            {"item_id": item_id, "evidence_id": evidence_id, "role": "primary"}
        )
        tables["inventory_events"].append(
            {
                "event_id": f"event-{branch}", "sequence": 1, "item_id": item_id,
                "event_type": "planned", "occurred_on": "2026-08-06",
                "observed_on": "2026-08-06", "occurred_on_precision": "exact",
                "actor": branch, "evidence_id": evidence_id, "location_id": "room-a",
                "container_id": None, "area_location_id": None,
                "context_quality": "bound", "details_json": None, "notes": None,
            }
        )

    def fact_conflict_branches(self) -> tuple[dict, dict, dict]:
        base = fixture()
        base["locations"] = [{
            "location_id": "loc-fact", "name": "Fact location", "kind": "room",
            "parent_location_id": None, "sensitivity": "low", "notes": "base",
        }]
        base["fact_amendments"] = []

        def amendment(amendment_id: str, previous: str, replacement: str) -> dict:
            return {
                "fact_amendment_id": amendment_id,
                "table_name": "locations",
                "selector_json": '{"location_id":"loc-fact"}',
                "amended_on": "2026-08-06",
                "recorded_at": "2026-08-06T00:00:00+00:00",
                "actor": "sync test",
                "evidence_id": "ev-fixture",
                "action": "replace",
                "previous_json": json.dumps(
                    {**base["locations"][0], "notes": previous},
                    separators=(",", ":"), sort_keys=True,
                ),
                "replacement_json": json.dumps(
                    {**base["locations"][0], "notes": replacement},
                    separators=(",", ":"), sort_keys=True,
                ),
                "sensitivity": "low",
                "reason": "sync branch fixture",
                "notes": None,
            }

        canonical = copy.deepcopy(base)
        replica = copy.deepcopy(base)
        canonical["locations"][0]["notes"] = "canonical"
        replica["locations"][0]["notes"] = "replica"
        canonical["fact_amendments"] = [
            amendment("fact-canonical", "base", "canonical"),
            {**amendment("fact-canonical-other", "base", "base"), "selector_json": '{"location_id":"loc-other"}'},
        ]
        replica["fact_amendments"] = [
            amendment("fact-replica", "base", "replica"),
            {**amendment("fact-replica-other", "base", "base"), "selector_json": '{"location_id":"loc-other-2"}'},
        ]
        return base, canonical, replica

    def test_fact_conflict_preserves_committed_canonical_history(self) -> None:
        base, canonical, replica = self.fact_conflict_branches()
        plan = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )
        self.assertEqual(plan["status"], "needs_resolution")
        conflict = next(row for row in plan["conflicts"] if row["table"] == "locations")
        self.assertEqual(conflict["choices"], ["canonical"])
        self.assertIn("fresh canonical transaction", conflict["reconciliation_required"])
        with self.assertRaisesRegex(SyncError, "invalid resolution"):
            resolve_conflicts(
                plan,
                {conflict["conflict_id"]: "replica"},
                merged_store_validator=strict_fixture_validator,
            )
        resolved = resolve_conflicts(
            plan,
            {conflict["conflict_id"]: "canonical"},
            merged_store_validator=strict_fixture_validator,
        )
        retry = resolve_conflicts(
            plan,
            {conflict["conflict_id"]: "canonical"},
            merged_store_validator=strict_fixture_validator,
        )
        self.assertEqual(receipt_data(resolved), receipt_data(retry))
        location = next(
            row for row in resolved["tables"]["locations"]
            if row["location_id"] == "loc-fact"
        )
        self.assertEqual(location["notes"], "canonical")
        self.assertEqual(
            {row["fact_amendment_id"] for row in resolved["tables"]["fact_amendments"]},
            {"fact-canonical", "fact-canonical-other", "fact-replica-other"},
        )

    def test_fact_retraction_conflicts_cannot_delete_canonical_history(self) -> None:
        base, canonical, replica = self.fact_conflict_branches()
        canonical["locations"] = []
        canonical["fact_amendments"][0].update(
            {"action": "retract", "replacement_json": None}
        )
        plan = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )
        conflict = next(row for row in plan["conflicts"] if row["table"] == "locations")
        self.assertEqual(conflict["kind"], "canonical_history_conflict")
        self.assertEqual(conflict["choices"], ["canonical"])
        with self.assertRaisesRegex(SyncError, "invalid resolution"):
            resolve_conflicts(
                plan,
                {conflict["conflict_id"]: "replica"},
                merged_store_validator=strict_fixture_validator,
            )
        ready = resolve_conflicts(
            plan,
            {conflict["conflict_id"]: "canonical"},
            merged_store_validator=strict_fixture_validator,
        )
        self.assertEqual(ready["tables"]["locations"], [])
        self.assertIn(
            "fact-canonical",
            {row["fact_amendment_id"] for row in ready["tables"]["fact_amendments"]},
        )

    def test_replica_retraction_cannot_delete_canonical_replacement_history(self) -> None:
        base, canonical, replica = self.fact_conflict_branches()
        replica["locations"] = []
        replica["fact_amendments"][0].update(
            {"action": "retract", "replacement_json": None}
        )
        plan = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )
        conflict = next(row for row in plan["conflicts"] if row["table"] == "locations")
        self.assertEqual(conflict["kind"], "canonical_history_conflict")
        self.assertEqual(conflict["choices"], ["canonical"])
        ready = resolve_conflicts(
            plan,
            {conflict["conflict_id"]: "canonical"},
            merged_store_validator=strict_fixture_validator,
        )
        current = next(
            row for row in ready["tables"]["locations"]
            if row["location_id"] == "loc-fact"
        )
        self.assertEqual(current["notes"], "canonical")
        self.assertIn(
            "fact-canonical",
            {row["fact_amendment_id"] for row in ready["tables"]["fact_amendments"]},
        )

    def test_replica_only_retraction_requires_fresh_canonical_reconciliation(self) -> None:
        base, _canonical, replica = self.fact_conflict_branches()
        replica["locations"] = []
        replica["fact_amendments"][0].update({"action": "retract", "replacement_json": None})
        plan = plan_three_way_merge(
            base=base,
            canonical_head=base,
            bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )
        conflict = next(row for row in plan["conflicts"] if row["table"] == "locations")
        self.assertEqual(conflict["choices"], ["canonical"])
        self.assertIn("fresh canonical transaction", conflict["reconciliation_required"])
        ready = resolve_conflicts(
            plan, {conflict["conflict_id"]: "canonical"}, merged_store_validator=strict_fixture_validator
        )
        self.assertEqual(ready["tables"]["locations"], base["locations"])

    def test_location_snapshots_are_parent_first_independent_of_lexical_ids(self) -> None:
        base = fixture()
        base["locations"] = [
            {"location_id": "loc-box", "name": "Box", "kind": "container", "parent_location_id": "loc-room", "sensitivity": "low", "notes": None},
            {"location_id": "loc-room", "name": "Room", "kind": "room", "parent_location_id": None, "sensitivity": "low", "notes": None},
        ]
        bundle = self.bundle(base, base)
        self.assertEqual(
            [row["location_id"] for row in bundle["base"]["tables"]["locations"]],
            ["loc-room", "loc-box"],
        )

    def test_same_identity_new_fact_collision_keeps_canonical_row(self) -> None:
        base = fixture()
        base["locations"] = []
        canonical = copy.deepcopy(base)
        replica = copy.deepcopy(base)
        canonical["locations"] = [
            {
                "location_id": "loc-collision", "name": "Canonical location",
                "kind": "room", "parent_location_id": None,
                "sensitivity": "low", "notes": "canonical",
            }
        ]
        replica["locations"] = [
            {
                "location_id": "loc-collision", "name": "Replica location",
                "kind": "room", "parent_location_id": None,
                "sensitivity": "low", "notes": "replica",
            }
        ]
        plan = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )
        conflict = next(row for row in plan["conflicts"] if row["table"] == "locations")
        self.assertEqual(conflict["kind"], "canonical_history_conflict")
        self.assertEqual(conflict["choices"], ["canonical"])
        ready = resolve_conflicts(
            plan,
            {conflict["conflict_id"]: "canonical"},
            merged_store_validator=strict_fixture_validator,
        )
        self.assertEqual(ready["tables"]["locations"], canonical["locations"])

    def test_same_identity_new_item_collision_keeps_canonical_creation(self) -> None:
        base = fixture()
        base.setdefault("relationships", [])
        canonical = copy.deepcopy(base)
        replica = copy.deepcopy(base)

        self.add_planned_creation(canonical, "canonical")
        self.add_planned_creation(replica, "replica")
        plan = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )
        conflict = next(row for row in plan["conflicts"] if row["table"] == "items")
        self.assertEqual(conflict["kind"], "canonical_history_conflict")
        self.assertEqual(conflict["choices"], ["canonical"])
        ready = resolve_conflicts(
            plan,
            {conflict["conflict_id"]: "canonical"},
            merged_store_validator=strict_fixture_validator,
        )
        selected = next(
            row for row in ready["tables"]["items"] if row["item_id"] == "item-collision"
        )
        self.assertEqual(selected["notes"], "canonical")
        self.assertIn(
            "event-canonical",
            {row["event_id"] for row in ready["tables"]["inventory_events"]},
        )
        self.assertNotIn(
            "event-replica",
            {row["event_id"] for row in ready["tables"]["inventory_events"]},
        )

    def test_new_item_collision_with_extra_replica_dependents_requires_rebase(self) -> None:
        base = fixture()
        canonical = copy.deepcopy(base)
        replica = copy.deepcopy(base)
        self.add_planned_creation(canonical, "canonical")
        self.add_planned_creation(replica, "replica")
        replica["item_tags"].append({"item_id": "item-collision", "tag": "replica-tag"})
        plan = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )
        conflict = next(row for row in plan["conflicts"] if row["table"] == "items")
        self.assertEqual(conflict["kind"], "identity_collision_requires_rebase")
        self.assertEqual(conflict["choices"], [])
        self.assertIn(
            {"table": "item_tags", "identity": ["item-collision", "replica-tag"]},
            conflict["dependent_replica_rows"],
        )

    def test_same_identity_immutable_addition_keeps_canonical_row(self) -> None:
        for table, canonical_row, replica_row in (
            (
                "models",
                {"model_id": "model-collision", "name": "canonical"},
                {"model_id": "model-collision", "name": "replica"},
            ),
            (
                "evidence",
                {"evidence_id": "evidence-collision", "source_ref": "canonical"},
                {"evidence_id": "evidence-collision", "source_ref": "replica"},
            ),
        ):
            with self.subTest(table=table):
                base = fixture()
                base[table] = []
                canonical = copy.deepcopy(base)
                replica = copy.deepcopy(base)
                canonical[table] = [canonical_row]
                replica[table] = [replica_row]
                plan = plan_three_way_merge(
                    base=base,
                    canonical_head=canonical,
                    bundle=self.bundle(base, replica),
                    merged_store_validator=strict_fixture_validator,
                )
                conflict = next(row for row in plan["conflicts"] if row["table"] == table)
                self.assertEqual(conflict["kind"], "canonical_history_conflict")
                self.assertEqual(conflict["choices"], ["canonical"])
                ready = resolve_conflicts(
                    plan,
                    {conflict["conflict_id"]: "canonical"},
                    merged_store_validator=strict_fixture_validator,
                )
                self.assertEqual(ready["tables"][table], [canonical_row])

    def test_immutable_collision_with_replica_dependents_requires_rebase(self) -> None:
        base = fixture()
        base["models"] = []
        canonical = copy.deepcopy(base)
        replica = copy.deepcopy(base)
        canonical["models"] = [
            {"model_id": "model-collision", "name": "Canonical 18V tool"}
        ]
        replica["models"] = [
            {"model_id": "model-collision", "name": "Replica 12V appliance"}
        ]
        replica["items"][1]["model_id"] = "model-collision"
        replica["item_amendments"] = [
            {
                "amendment_id": "amendment-dependent",
                "item_id": "item-b", "previous_model_id": None,
                "target_model_id": "model-collision",
            }
        ]
        plan = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )
        conflict = next(row for row in plan["conflicts"] if row["table"] == "models")
        self.assertEqual(conflict["kind"], "identity_collision_requires_rebase")
        self.assertEqual(conflict["choices"], [])
        self.assertIn(
            {"table": "items", "identity": ["item-b"]},
            conflict["dependent_replica_rows"],
        )
        self.assertIn("re-ID", conflict["reconciliation_required"])
        with self.assertRaisesRegex(SyncError, "invalid resolution"):
            resolve_conflicts(
                plan,
                {conflict["conflict_id"]: "canonical"},
                merged_store_validator=strict_fixture_validator,
            )

    def test_disjoint_identity_changes_merge_without_a_winner(self) -> None:
        base = fixture()
        canonical = copy.deepcopy(base)
        replica = copy.deepcopy(base)
        canonical["items"][1]["location_id"] = "room-b"
        replica["items"][0]["quantity"] = 2

        plan = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )

        self.assertEqual(plan["status"], "ready")
        items = {row["item_id"]: row for row in plan["tables"]["items"]}
        self.assertEqual(items["item-a"]["quantity"], 2)
        self.assertEqual(items["item-b"]["location_id"], "room-b")

    def test_same_identity_semantic_conflict_requires_explicit_choice(self) -> None:
        base = fixture()
        canonical = copy.deepcopy(base)
        replica = copy.deepcopy(base)
        canonical["items"][0]["ownership_state"] = "lent"
        replica["items"][0]["quantity"] = 2

        plan = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )

        self.assertEqual(plan["status"], "needs_resolution")
        self.assertEqual(plan["conflicts"][0]["kind"], "canonical_history_conflict")
        self.assertEqual(
            plan["conflicts"][0]["semantic_fields"], ["ownership_state", "quantity"]
        )
        with self.assertRaisesRegex(SyncError, "exactly once"):
            resolve_conflicts(plan, {}, merged_store_validator=strict_fixture_validator)
        self.assertEqual(plan["conflicts"][0]["choices"], ["canonical"])
        with self.assertRaisesRegex(SyncError, "invalid resolution"):
            resolve_conflicts(
                plan,
                {plan["conflicts"][0]["conflict_id"]: "replica"},
                merged_store_validator=strict_fixture_validator,
            )
        resolved = resolve_conflicts(
            plan,
            {plan["conflicts"][0]["conflict_id"]: "canonical"},
            merged_store_validator=strict_fixture_validator,
        )
        self.assertEqual(resolved["status"], "ready")
        item = next(row for row in resolved["tables"]["items"] if row["item_id"] == "item-a")
        self.assertEqual(item["quantity"], 1)
        self.assertEqual(item["ownership_state"], "lent")

    def test_item_conflict_preserves_canonical_model_amendment_history(self) -> None:
        base = fixture()
        base["models"] = []
        canonical = copy.deepcopy(base)
        replica = copy.deepcopy(base)
        canonical["models"] = [{"model_id": "model-canonical"}]
        replica["models"] = [{"model_id": "model-replica"}]
        canonical["items"][0]["model_id"] = "model-canonical"
        replica["items"][0]["model_id"] = "model-replica"
        canonical["item_amendments"] = [
            {
                "amendment_id": "amendment-canonical",
                "item_id": "item-a",
                "previous_model_id": None,
                "target_model_id": "model-canonical",
            }
        ]
        replica["item_amendments"] = [
            {
                "amendment_id": "amendment-replica",
                "item_id": "item-a",
                "previous_model_id": None,
                "target_model_id": "model-replica",
            }
        ]
        plan = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )
        conflict = next(row for row in plan["conflicts"] if row["table"] == "items")
        self.assertEqual(conflict["kind"], "identity_collision_requires_rebase")
        self.assertEqual(conflict["choices"], [])
        self.assertIn({"table": "models", "identity": ["model-replica"]}, conflict["dependent_replica_rows"])
        with self.assertRaisesRegex(SyncError, "invalid resolution"):
            resolve_conflicts(
                plan, {conflict["conflict_id"]: "canonical"}, merged_store_validator=strict_fixture_validator
            )

    def test_two_step_fact_and_model_amendments_replay_to_the_replica_head(self) -> None:
        base, _canonical, replica = self.fact_conflict_branches()
        replica["locations"][0]["notes"] = "second"
        replica["fact_amendments"] = [
            {**replica["fact_amendments"][0], "fact_amendment_id": "fact-first", "replacement_json": json.dumps({**base["locations"][0], "notes": "first"}, sort_keys=True)},
            {**replica["fact_amendments"][0], "fact_amendment_id": "fact-second", "previous_json": json.dumps({**base["locations"][0], "notes": "first"}, sort_keys=True), "replacement_json": json.dumps({**base["locations"][0], "notes": "second"}, sort_keys=True)},
        ]
        model_base = fixture()
        previous_model = model_base["items"][0].get("model_id")
        model_base["models"] = [{"model_id": "model-b"}, {"model_id": "model-c"}]
        model_replica = copy.deepcopy(model_base)
        model_replica["items"][0]["model_id"] = "model-c"
        model_replica["item_amendments"] = [
            {"amendment_id": "model-first", "item_id": "item-a", "previous_model_id": previous_model, "target_model_id": "model-b"},
            {"amendment_id": "model-second", "item_id": "item-a", "previous_model_id": "model-b", "target_model_id": "model-c"},
        ]
        self.assertEqual(
            plan_three_way_merge(base=base, canonical_head=base, bundle=self.bundle(base, replica), merged_store_validator=strict_fixture_validator)["status"],
            "ready",
        )
        self.assertEqual(
            plan_three_way_merge(base=model_base, canonical_head=model_base, bundle=self.bundle(model_base, model_replica), merged_store_validator=strict_fixture_validator)["status"],
            "ready",
        )

    def test_delete_change_conflict_never_silently_drops_the_change(self) -> None:
        base = fixture()
        canonical = copy.deepcopy(base)
        replica = copy.deepcopy(base)
        canonical["items"] = [row for row in canonical["items"] if row["item_id"] != "item-a"]
        replica["items"][0]["location_id"] = "room-z"

        plan = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )

        self.assertEqual(plan["status"], "needs_resolution")
        conflict = plan["conflicts"][0]
        self.assertIsNone(conflict["canonical"])
        self.assertEqual(conflict["replica"]["location_id"], "room-z")
        resolved = resolve_conflicts(
            plan,
            {conflict["conflict_id"]: "replica"},
            merged_store_validator=strict_fixture_validator,
        )
        self.assertIn(
            "item-a", {row["item_id"] for row in resolved["tables"]["items"]}
        )

    def test_stale_and_tampered_bundles_fail_closed(self) -> None:
        base = fixture()
        bundle = self.bundle(base, base)
        tampered = copy.deepcopy(bundle)
        tampered["head"]["tables"]["items"][0]["quantity"] = 99
        with self.assertRaisesRegex(SyncError, "digest mismatch"):
            verify_replica_bundle(tampered)
        stale = copy.deepcopy(base)
        stale["items"][0]["quantity"] = 3
        with self.assertRaisesRegex(SyncError, "stale"):
            plan_three_way_merge(
                base=stale,
                canonical_head=stale,
                bundle=bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_portable_snapshot_envelope_binds_every_table_digest(self) -> None:
        snapshot = build_store_snapshot(fixture())
        self.assertEqual(verify_store_snapshot(snapshot), snapshot)
        snapshot["digest"] = "0" * 64
        with self.assertRaisesRegex(SyncError, "digest mismatch"):
            verify_store_snapshot(snapshot)

    def test_bundle_plan_and_receipt_are_deterministic_for_retry(self) -> None:
        base = fixture()
        canonical = copy.deepcopy(base)
        replica = copy.deepcopy(base)
        canonical["items"][1]["location_id"] = "room-b"
        replica["items"][0]["quantity"] = 2
        first_bundle = self.bundle(base, replica)
        second_bundle = self.bundle(dict(reversed(base.items())), replica)
        self.assertEqual(first_bundle, second_bundle)
        first = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=first_bundle,
            merged_store_validator=strict_fixture_validator,
        )
        second = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=second_bundle,
            merged_store_validator=strict_fixture_validator,
        )
        self.assertEqual(first, second)
        self.assertEqual(receipt_data(first), receipt_data(second))

    def test_manifest_is_bound_to_metadata_and_rejects_non_json_values(self) -> None:
        base = fixture()
        wrong_inventory = copy.deepcopy(base)
        wrong_inventory["metadata"][0]["inventory_id"] = "another-inventory"
        with self.assertRaisesRegex(SyncError, "metadata inventory_id"):
            self.bundle(wrong_inventory, wrong_inventory)

        wrong_canonical = copy.deepcopy(base)
        wrong_canonical["metadata"][0]["inventory_id"] = "another-inventory"
        with self.assertRaisesRegex(SyncError, "canonical head metadata inventory_id"):
            plan_three_way_merge(
                base=base,
                canonical_head=wrong_canonical,
                bundle=self.bundle(base, base),
                merged_store_validator=strict_fixture_validator,
            )

        invalid_id = copy.deepcopy(base)
        invalid_id["items"][0]["item_id"] = True
        with self.assertRaisesRegex(SyncError, "non-string"):
            self.bundle(invalid_id, invalid_id)

        non_finite = copy.deepcopy(base)
        non_finite["items"][0]["quantity"] = float("nan")
        with self.assertRaisesRegex(SyncError, "not JSON-serializable"):
            self.bundle(non_finite, non_finite)

        invalid_format = self.bundle(base, base)
        invalid_format["format"] = True
        with self.assertRaisesRegex(SyncError, "unsupported replica bundle format"):
            verify_replica_bundle(invalid_format)

    def test_unsupported_tables_fail_closed(self) -> None:
        base = fixture()
        base["unregistered"] = [{"unregistered_id": "u-1"}]
        with self.assertRaisesRegex(SyncError, "not supported"):
            self.bundle(base, base)

    def test_replica_cannot_unilaterally_delete_canonical_row(self) -> None:
        base = fixture()
        replica = copy.deepcopy(base)
        replica["items"] = [row for row in replica["items"] if row["item_id"] != "item-a"]

        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture", replica_ref="replica-fixture", base=base, head=replica
        )
        with self.assertRaisesRegex(SyncError, "cannot delete current item"):
            plan_three_way_merge(
                base=base,
                canonical_head=base,
                bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_replica_cannot_splice_a_new_item_without_a_creation_transaction(self) -> None:
        base = fixture()
        replica = copy.deepcopy(base)
        forged = {**replica["items"][0], "item_id": "item-forged"}
        forged["primary_evidence_id"] = "evidence-forged"
        replica["items"].append(forged)
        replica["evidence"] = [
            {
                "evidence_id": "evidence-forged",
                "evidence_type": "physical_check",
                "source_ref": "forged item",
                "captured_on": "2026-08-06",
                "claim_strength": "explicit_current",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["item_evidence"] = [
            {
                "item_id": "item-forged",
                "evidence_id": "evidence-forged",
                "role": "primary",
            }
        ]
        replica["inventory_events"] = [
            {
                "event_id": "event-forged-ingest",
                "sequence": 1,
                "item_id": "item-forged",
                "event_type": "ingested",
                "occurred_on": "2026-08-06",
                "observed_on": "2026-08-06",
                "occurred_on_precision": "exact",
                "actor": "forged writer",
                "evidence_id": "evidence-forged",
                "location_id": "room-a",
                "container_id": None,
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": None,
                "notes": None,
            }
        ]
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture", replica_ref="replica-fixture",
            base=base, head=replica,
        )
        with self.assertRaisesRegex(SyncError, "unsupported inventory event"):
            plan_three_way_merge(
                base=base, canonical_head=base, bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_new_item_creation_cannot_smuggle_impossible_initial_details(self) -> None:
        base = fixture()
        replica = copy.deepcopy(base)
        replica["items"].append(
            {
                "item_id": "item-impossible-plan", "location_id": "room-a",
                "container_id": None, "ownership_state": "planned",
                "quantity": 1, "unit": "item", "verified_on": None,
                "primary_evidence_id": "evidence-impossible-plan",
                "acquired_on": None, "condition": None,
                "purchase_currency": "GBP", "purchase_price": 10,
                "receipt_ref": None, "serial_or_lot": None,
                "replacement_value": None, "value_currency": None,
            }
        )
        replica["evidence"].append(
            {
                "evidence_id": "evidence-impossible-plan",
                "evidence_type": "user_source", "source_ref": "cart",
                "captured_on": "2026-08-06", "claim_strength": "purchase_only",
                "sensitivity": "low", "notes": None,
            }
        )
        replica["item_evidence"].append(
            {
                "item_id": "item-impossible-plan",
                "evidence_id": "evidence-impossible-plan", "role": "primary",
            }
        )
        replica["inventory_events"].append(
            {
                "event_id": "event-impossible-plan", "sequence": 1,
                "item_id": "item-impossible-plan", "event_type": "planned",
                "occurred_on": "2026-08-06", "observed_on": "2026-08-06",
                "occurred_on_precision": "exact", "actor": "forged writer",
                "evidence_id": "evidence-impossible-plan", "location_id": "room-a",
                "container_id": None, "area_location_id": None,
                "context_quality": "bound", "details_json": None, "notes": None,
            }
        )
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture", replica_ref="replica-fixture",
            base=base, head=replica,
        )
        with self.assertRaisesRegex(SyncError, "invalid purchase creation"):
            plan_three_way_merge(
                base=base, canonical_head=base, bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_replica_cannot_append_corrected_events(self) -> None:
        base = fixture()
        replica = copy.deepcopy(base)
        replica["inventory_events"] = [
            {
                "event_id": "event-forged-correction",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "corrected",
                "occurred_on": "1900-01-01",
                "observed_on": "1900-01-01",
                "occurred_on_precision": "exact",
                "actor": "forged writer",
                "evidence_id": None,
                "location_id": "room-a",
                "container_id": None,
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": None,
                "notes": "arbitrary audit claim",
            }
        ]
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture", replica_ref="replica-fixture",
            base=base, head=replica,
        )
        with self.assertRaisesRegex(SyncError, "unsupported inventory event"):
            plan_three_way_merge(
                base=base, canonical_head=base, bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_delayed_physical_observation_replays_without_inventing_a_date(self) -> None:
        base = fixture()
        replica = copy.deepcopy(base)
        replica["items"][0]["verified_on"] = "2026-08-01"
        replica["evidence"] = [
            {
                "evidence_id": "evidence-delayed-check",
                "evidence_type": "physical_check",
                "source_ref": "photo reviewed after the physical check",
                "captured_on": "2026-08-06",
                "claim_strength": "explicit_current",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["item_evidence"] = [
            {
                "item_id": "item-a",
                "evidence_id": "evidence-delayed-check",
                "role": "supporting",
            }
        ]
        replica["inventory_events"] = [
            {
                "event_id": "event-delayed-check",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "physically_verified",
                "occurred_on": "2026-08-01",
                "observed_on": "2026-08-06",
                "occurred_on_precision": "exact",
                "actor": "sync fixture",
                "evidence_id": "evidence-delayed-check",
                "location_id": "room-a",
                "container_id": None,
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": None,
                "notes": None,
            }
        ]
        bundle = build_replica_bundle(
            inventory_id="inventory-fixture",
            replica_ref="replica-fixture",
            base=base,
            head=replica,
        )
        plan = plan_three_way_merge(
            base=base,
            canonical_head=base,
            bundle=bundle,
            merged_store_validator=strict_fixture_validator,
        )
        self.assertEqual(plan["status"], "ready")

    def test_replica_rejects_an_observation_before_the_exact_event(self) -> None:
        base = fixture()
        replica = copy.deepcopy(base)
        replica["items"][0]["verified_on"] = "2026-08-06"
        replica["evidence"] = [
            {
                "evidence_id": "evidence-impossible-check",
                "evidence_type": "physical_check",
                "source_ref": "impossible physical check",
                "captured_on": "2026-08-01",
                "claim_strength": "explicit_current",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["item_evidence"] = [
            {
                "item_id": "item-a",
                "evidence_id": "evidence-impossible-check",
                "role": "supporting",
            }
        ]
        replica["inventory_events"] = [
            {
                "event_id": "event-impossible-check",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "physically_verified",
                "occurred_on": "2026-08-06",
                "observed_on": "2026-08-01",
                "occurred_on_precision": "exact",
                "actor": "sync fixture",
                "evidence_id": "evidence-impossible-check",
                "location_id": "room-a",
                "container_id": None,
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": None,
                "notes": None,
            }
        ]
        bundle = build_replica_bundle(
            inventory_id="inventory-fixture",
            replica_ref="replica-fixture",
            base=base,
            head=replica,
        )
        with self.assertRaisesRegex(SyncError, "observed before it occurred"):
            plan_three_way_merge(
                base=base,
                canonical_head=base,
                bundle=bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_replica_rejects_reacquisition_that_carries_stale_episode_facts(self) -> None:
        base = fixture()
        before = base["items"][0]
        before.update(
            {
                "acquired_on": "2020-01-02",
                "condition": "working",
                "container_id": None,
                "location_id": None,
                "ownership_state": "disposed",
                "purchase_currency": None,
                "purchase_price": None,
                "receipt_ref": None,
                "serial_or_lot": "serial-a",
                "verified_on": None,
            }
        )
        replica = copy.deepcopy(base)
        replica["items"][0].update(
            {"location_id": "room-a", "ownership_state": "confirmed"}
        )
        replica["evidence"] = [
            {
                "evidence_id": "evidence-unsafe-reacquisition",
                "evidence_type": "physical_check",
                "source_ref": "presence only",
                "captured_on": "2026-08-06",
                "claim_strength": "explicit_current",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["item_evidence"] = [
            {
                "item_id": "item-a",
                "evidence_id": "evidence-unsafe-reacquisition",
                "role": "supporting",
            }
        ]
        replica["inventory_events"] = [
            {
                "event_id": "event-unsafe-reacquisition",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "reacquired",
                "occurred_on": "2026-08-06",
                "observed_on": "2026-08-06",
                "occurred_on_precision": "exact",
                "actor": "sync fixture",
                "evidence_id": "evidence-unsafe-reacquisition",
                "location_id": "room-a",
                "container_id": None,
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": json.dumps(
                    {
                        "condition_checked": None,
                        "quantity_checked": 1,
                        "reset_fields": [
                            "acquired_on",
                            "condition",
                            "purchase_currency",
                            "purchase_price",
                            "receipt_ref",
                        ],
                        "unit": "item",
                    },
                    sort_keys=True,
                ),
                "notes": None,
            }
        ]
        bundle = build_replica_bundle(
            inventory_id="inventory-fixture",
            replica_ref="replica-fixture",
            base=base,
            head=replica,
        )
        with self.assertRaisesRegex(
            SyncError, "reacquisition lacks an ownership-episode reset artifact"
        ):
            plan_three_way_merge(
                base=base,
                canonical_head=base,
                bundle=bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_replica_accepts_episode_reset_and_same_quantity_reaffirmation(self) -> None:
        base = fixture()
        before = base["items"][0]
        before.update(
            {
                "acquired_on": "2020-01-02",
                "condition": "working",
                "container_id": None,
                "location_id": None,
                "ownership_state": "disposed",
                "purchase_currency": None,
                "purchase_price": None,
                "receipt_ref": None,
                "serial_or_lot": "serial-a",
                "verified_on": None,
            }
        )
        replica = copy.deepcopy(base)
        replica["items"][0].update(
            {
                "acquired_on": None,
                "condition": None,
                "location_id": "room-a",
                "ownership_state": "confirmed",
                "verified_on": "2026-08-06",
            }
        )
        evidence_id = "evidence-safe-reacquisition"
        replica["evidence"] = [
            {
                "evidence_id": evidence_id,
                "evidence_type": "physical_check",
                "source_ref": "presence and count checked; function unknown",
                "captured_on": "2026-08-06",
                "claim_strength": "explicit_current",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["item_evidence"] = [
            {"item_id": "item-a", "evidence_id": evidence_id, "role": "supporting"}
        ]
        previous_details = {
            field: before.get(field)
            for field in (
                "acquired_on",
                "condition",
                "purchase_currency",
                "purchase_price",
                "receipt_ref",
                "serial_or_lot",
            )
        }
        reset = {
            "acquired_on": None,
            "condition": None,
            "purchase_currency": None,
            "purchase_price": None,
            "receipt_ref": None,
        }
        replica["item_detail_amendments"] = [
            {
                "detail_amendment_id": "detail-safe-reacquisition",
                "item_id": "item-a",
                "previous_json": json.dumps(previous_details, sort_keys=True),
                "changes_json": json.dumps(reset, sort_keys=True),
                "amended_on": "2026-08-06",
                "recorded_at": "2026-08-06T00:00:00+00:00",
                "actor": "sync fixture",
                "evidence_id": evidence_id,
                "sensitivity": "low",
                "notes": None,
            }
        ]
        common = {
            "item_id": "item-a",
            "occurred_on": "2026-08-06",
            "observed_on": "2026-08-06",
            "occurred_on_precision": "exact",
            "actor": "sync fixture",
            "evidence_id": evidence_id,
            "location_id": "room-a",
            "container_id": None,
            "area_location_id": None,
            "context_quality": "bound",
            "notes": None,
        }
        replica["inventory_events"] = [
            {
                **common,
                "event_id": "event-safe-reacquisition",
                "sequence": 1,
                "event_type": "reacquired",
                "details_json": json.dumps(
                    {
                        "condition_checked": None,
                        "quantity_checked": 1,
                        "reset_fields": [
                            "acquired_on",
                            "condition",
                            "purchase_currency",
                            "purchase_price",
                            "receipt_ref",
                        ],
                        "unit": "item",
                    },
                    sort_keys=True,
                ),
            },
            {
                **common,
                "event_id": "event-safe-quantity",
                "sequence": 2,
                "event_type": "quantity_changed",
                "details_json": json.dumps(
                    {
                        "previous_quantity": 1,
                        "previous_unit": "item",
                        "quantity": 1,
                        "unit": "item",
                    },
                    sort_keys=True,
                ),
            },
            {
                **common,
                "event_id": "event-safe-physical",
                "sequence": 3,
                "event_type": "physically_verified",
                "details_json": None,
            },
        ]
        bundle = build_replica_bundle(
            inventory_id="inventory-fixture",
            replica_ref="replica-fixture",
            base=base,
            head=replica,
        )
        plan = plan_three_way_merge(
            base=base,
            canonical_head=base,
            bundle=bundle,
            merged_store_validator=strict_fixture_validator,
        )
        self.assertEqual(plan["status"], "ready")

    def test_replica_cannot_append_canonical_sync_receipts(self) -> None:
        base = fixture()
        replica = copy.deepcopy(base)
        replica["sync_receipts"] = [
            {
                "sync_receipt_id": "sync-forged",
                "replica_ref": "forged-replica",
                "payload_digest": "0" * 64,
            }
        ]
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture", replica_ref="replica-fixture",
            base=base, head=replica,
        )
        with self.assertRaisesRegex(SyncError, "canonical sync receipts"):
            plan_three_way_merge(
                base=base, canonical_head=base, bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_unrelated_event_cannot_authorize_a_direct_lifecycle_edit(self) -> None:
        base = fixture()
        replica = copy.deepcopy(base)
        replica["items"][0]["quantity"] = 2
        replica["evidence"] = [
            {
                "evidence_id": "evidence-unrelated",
                "evidence_type": "user_source",
                "source_ref": "unrelated correction",
                "captured_on": "2026-08-06",
                "claim_strength": "explicit_current",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["inventory_events"] = [
            {
                "event_id": "event-unrelated",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "corrected",
                "occurred_on": "2026-08-06",
                "observed_on": "2026-08-06",
                "occurred_on_precision": "exact",
                "actor": "sync fixture",
                "evidence_id": "evidence-unrelated",
                "location_id": "room-a",
                "container_id": None,
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": None,
                "notes": None,
            }
        ]
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture",
            replica_ref="replica-fixture",
            base=base,
            head=replica,
        )
        with self.assertRaisesRegex(SyncError, "unsupported inventory event"):
            plan_three_way_merge(
                base=base,
                canonical_head=base,
                bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_quantity_event_cannot_smuggle_a_location_edit(self) -> None:
        base = fixture()
        replica = copy.deepcopy(base)
        replica["items"][0].update({"location_id": "room-b", "quantity": 2})
        replica["evidence"] = [
            {
                "evidence_id": "evidence-quantity",
                "evidence_type": "user_source",
                "source_ref": "exact quantity change only",
                "captured_on": "2026-08-06",
                "claim_strength": "explicit_current",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["inventory_events"] = [
            {
                "event_id": "event-quantity",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "quantity_changed",
                "occurred_on": "2026-08-06",
                "observed_on": "2026-08-06",
                "occurred_on_precision": "exact",
                "actor": "sync fixture",
                "evidence_id": "evidence-quantity",
                "location_id": "room-b",
                "container_id": None,
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": json.dumps(
                    {
                        "previous_quantity": 1,
                        "previous_unit": "item",
                        "quantity": 2,
                        "unit": "item",
                    },
                    sort_keys=True,
                ),
                "notes": None,
            }
        ]
        replica["item_evidence"] = [
            {"item_id": "item-a", "evidence_id": "evidence-quantity", "role": "supporting"}
        ]
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture",
            replica_ref="replica-fixture",
            base=base,
            head=replica,
        )
        with self.assertRaisesRegex(SyncError, "exact inventory_event provenance for: location_id"):
            plan_three_way_merge(
                base=base,
                canonical_head=base,
                bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_lifecycle_event_evidence_must_be_attached_to_its_item(self) -> None:
        base = fixture()
        replica = copy.deepcopy(base)
        replica["items"][0]["quantity"] = 2
        replica["evidence"] = [
            {
                "evidence_id": "evidence-unattached",
                "evidence_type": "user_source",
                "source_ref": "counted two",
                "captured_on": "2026-08-06",
                "claim_strength": "explicit_current",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["inventory_events"] = [
            {
                "event_id": "event-unattached",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "quantity_changed",
                "occurred_on": "2026-08-06",
                "observed_on": "2026-08-06",
                "occurred_on_precision": "exact",
                "actor": "sync fixture",
                "evidence_id": "evidence-unattached",
                "location_id": "room-a",
                "container_id": None,
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": json.dumps(
                    {
                        "previous_quantity": 1,
                        "previous_unit": "item",
                        "quantity": 2,
                        "unit": "item",
                    },
                    sort_keys=True,
                ),
                "notes": None,
            }
        ]
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture", replica_ref="replica-fixture",
            base=base, head=replica,
        )
        with self.assertRaisesRegex(SyncError, "not attached to its item"):
            plan_three_way_merge(
                base=base, canonical_head=base, bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_old_physical_evidence_cannot_smuggle_a_new_verification_date(self) -> None:
        base = fixture()
        replica = copy.deepcopy(base)
        replica["items"][0]["verified_on"] = "2026-08-06"
        replica["evidence"] = [
            {
                "evidence_id": "evidence-old-photo",
                "evidence_type": "physical_check",
                "source_ref": "historical physical evidence",
                "captured_on": "2020-01-02",
                "claim_strength": "explicit_current",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["inventory_events"] = [
            {
                "event_id": "event-new-verification",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "physically_verified",
                "occurred_on": "2026-08-06",
                "observed_on": "2026-08-06",
                "occurred_on_precision": "exact",
                "actor": "sync fixture",
                "evidence_id": "evidence-old-photo",
                "location_id": "room-a",
                "container_id": None,
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": None,
                "notes": None,
            }
        ]
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture",
            replica_ref="replica-fixture",
            base=base,
            head=replica,
        )
        with self.assertRaisesRegex(SyncError, "fresh bound evidence"):
            plan_three_way_merge(
                base=base,
                canonical_head=base,
                bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_not_found_event_cannot_move_an_item_to_an_arbitrary_location(self) -> None:
        base = fixture()
        base["locations"] = [
            {
                "location_id": "loc-unknown", "name": "Unknown", "kind": "unknown",
                "parent_location_id": None, "sensitivity": "low", "notes": None,
            },
            {
                "location_id": "room-a", "name": "Room A", "kind": "room",
                "parent_location_id": None, "sensitivity": "low", "notes": None,
            },
            {
                "location_id": "room-b", "name": "Room B", "kind": "room",
                "parent_location_id": None, "sensitivity": "low", "notes": None,
            },
        ]
        replica = copy.deepcopy(base)
        replica["items"][0]["location_id"] = "room-b"
        replica["evidence"] = [
            {
                "evidence_id": "evidence-not-found",
                "evidence_type": "physical_check",
                "source_ref": "checked only room b",
                "captured_on": "2026-08-06",
                "claim_strength": "area_not_found",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["inventory_events"] = [
            {
                "event_id": "event-not-found",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "not_found_in_area",
                "occurred_on": "2026-08-06",
                "observed_on": "2026-08-06",
                "occurred_on_precision": "exact",
                "actor": "sync fixture",
                "evidence_id": "evidence-not-found",
                "location_id": "room-b",
                "container_id": None,
                "area_location_id": "room-b",
                "context_quality": "bound",
                "details_json": None,
                "notes": None,
            }
        ]
        replica["item_evidence"] = [
            {"item_id": "item-a", "evidence_id": "evidence-not-found", "role": "supporting"}
        ]
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture", replica_ref="replica-fixture",
            base=base, head=replica,
        )
        with self.assertRaisesRegex(SyncError, "location_id"):
            plan_three_way_merge(
                base=base, canonical_head=base, bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_quantity_event_requires_a_currently_possessed_source_state(self) -> None:
        base = fixture()
        base["items"][0]["ownership_state"] = "planned"
        replica = copy.deepcopy(base)
        replica["items"][0]["quantity"] = 2
        replica["evidence"] = [
            {
                "evidence_id": "evidence-planned-quantity",
                "evidence_type": "user_source",
                "source_ref": "invalid current quantity event",
                "captured_on": "2026-08-06",
                "claim_strength": "explicit_current",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["inventory_events"] = [
            {
                "event_id": "event-planned-quantity",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "quantity_changed",
                "occurred_on": "2026-08-06",
                "observed_on": "2026-08-06",
                "occurred_on_precision": "exact",
                "actor": "sync fixture",
                "evidence_id": "evidence-planned-quantity",
                "location_id": "room-a",
                "container_id": None,
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": json.dumps(
                    {
                        "previous_quantity": 1, "previous_unit": "item",
                        "quantity": 2, "unit": "item",
                    },
                    sort_keys=True,
                ),
                "notes": None,
            }
        ]
        replica["item_evidence"] = [
            {
                "item_id": "item-a",
                "evidence_id": "evidence-planned-quantity",
                "role": "supporting",
            }
        ]
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture", replica_ref="replica-fixture",
            base=base, head=replica,
        )
        with self.assertRaisesRegex(SyncError, "quantity"):
            plan_three_way_merge(
                base=base, canonical_head=base, bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_state_event_requires_its_legal_source_state(self) -> None:
        base = fixture()
        base["items"][0].update(
            {"ownership_state": "disposed", "location_id": None, "container_id": None}
        )
        replica = copy.deepcopy(base)
        replica["items"][0].update(
            {"ownership_state": "confirmed", "location_id": "room-b", "container_id": None}
        )
        replica["evidence"] = [
            {
                "evidence_id": "evidence-invalid-loan-return",
                "evidence_type": "user_source",
                "source_ref": "not actually a loan return",
                "captured_on": "2026-08-06",
                "claim_strength": "explicit_current",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["inventory_events"] = [
            {
                "event_id": "event-invalid-loan-return",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "loan_returned",
                "occurred_on": "2026-08-06",
                "observed_on": "2026-08-06",
                "occurred_on_precision": "exact",
                "actor": "sync fixture",
                "evidence_id": "evidence-invalid-loan-return",
                "location_id": "room-b",
                "container_id": None,
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": None,
                "notes": None,
            }
        ]
        replica["item_evidence"] = [
            {
                "item_id": "item-a",
                "evidence_id": "evidence-invalid-loan-return",
                "role": "supporting",
            }
        ]
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture", replica_ref="replica-fixture",
            base=base, head=replica,
        )
        with self.assertRaisesRegex(SyncError, "ownership_state"):
            plan_three_way_merge(
                base=base, canonical_head=base, bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_purchase_event_cannot_smuggle_a_unit_change(self) -> None:
        base = fixture()
        base["items"][0]["ownership_state"] = "candidate"
        replica = copy.deepcopy(base)
        replica["items"][0]["unit"] = "box"
        replica["evidence"] = [
            {
                "evidence_id": "evidence-ordered-unit",
                "evidence_type": "merchant_account",
                "source_ref": "placed order",
                "captured_on": "2026-08-06",
                "claim_strength": "purchase_only",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["inventory_events"] = [
            {
                "event_id": "event-ordered-unit",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "ordered",
                "occurred_on": "2026-08-06",
                "observed_on": "2026-08-06",
                "occurred_on_precision": "exact",
                "actor": "sync fixture",
                "evidence_id": "evidence-ordered-unit",
                "location_id": "room-a",
                "container_id": None,
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": json.dumps(
                    {
                        "previous_quantity": 1, "previous_unit": "item",
                        "quantity": 1, "unit": "box",
                    },
                    sort_keys=True,
                ),
                "notes": None,
            }
        ]
        replica["item_evidence"] = [
            {
                "item_id": "item-a",
                "evidence_id": "evidence-ordered-unit",
                "role": "supporting",
            }
        ]
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture", replica_ref="replica-fixture",
            base=base, head=replica,
        )
        with self.assertRaisesRegex(SyncError, "unit"):
            plan_three_way_merge(
                base=base, canonical_head=base, bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_purchase_event_cannot_invent_a_container(self) -> None:
        base = fixture()
        base["items"][0]["ownership_state"] = "candidate"
        replica = copy.deepcopy(base)
        replica["items"][0]["container_id"] = "box-a"
        replica["evidence"] = [
            {
                "evidence_id": "evidence-ordered-container",
                "evidence_type": "merchant_account",
                "source_ref": "placed order",
                "captured_on": "2026-08-06",
                "claim_strength": "purchase_only",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["inventory_events"] = [
            {
                "event_id": "event-ordered-container",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "ordered",
                "occurred_on": "2026-08-06",
                "observed_on": "2026-08-06",
                "occurred_on_precision": "exact",
                "actor": "sync fixture",
                "evidence_id": "evidence-ordered-container",
                "location_id": "room-a",
                "container_id": "box-a",
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": None,
                "notes": None,
            }
        ]
        replica["item_evidence"] = [
            {
                "item_id": "item-a",
                "evidence_id": "evidence-ordered-container",
                "role": "supporting",
            }
        ]
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture", replica_ref="replica-fixture",
            base=base, head=replica,
        )
        with self.assertRaisesRegex(SyncError, "container_id"):
            plan_three_way_merge(
                base=base, canonical_head=base, bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_lifecycle_event_cannot_reuse_old_evidence(self) -> None:
        base = fixture()
        base["items"][0]["ownership_state"] = "unknown"
        base["evidence"] = [
            {
                "evidence_id": "evidence-old-cart",
                "evidence_type": "merchant_account",
                "source_ref": "historical cart",
                "captured_on": "2020-01-01",
                "claim_strength": "purchase_only",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica = copy.deepcopy(base)
        replica["items"][0]["ownership_state"] = "candidate"
        replica["inventory_events"] = [
            {
                "event_id": "event-reused-evidence",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "ordered",
                "occurred_on": "2026-08-06",
                "observed_on": "2026-08-06",
                "occurred_on_precision": "exact",
                "actor": "sync fixture",
                "evidence_id": "evidence-old-cart",
                "location_id": "room-a",
                "container_id": None,
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": None,
                "notes": None,
            }
        ]
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture", replica_ref="replica-fixture",
            base=base, head=replica,
        )
        with self.assertRaisesRegex(SyncError, "fresh bound evidence"):
            plan_three_way_merge(
                base=base, canonical_head=base, bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_lifecycle_event_cannot_launder_a_historical_capture_date(self) -> None:
        base = fixture()
        base["items"][0]["ownership_state"] = "unknown"
        replica = copy.deepcopy(base)
        replica["items"][0]["ownership_state"] = "candidate"
        replica["evidence"] = [
            {
                "evidence_id": "evidence-new-but-stale",
                "evidence_type": "merchant_account",
                "source_ref": "historical cart",
                "captured_on": "2020-01-02",
                "claim_strength": "purchase_only",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        replica["inventory_events"] = [
            {
                "event_id": "event-laundered-date",
                "sequence": 1,
                "item_id": "item-a",
                "event_type": "ordered",
                "occurred_on": "2026-08-06",
                "observed_on": "2026-08-06",
                "occurred_on_precision": "exact",
                "actor": "sync fixture",
                "evidence_id": "evidence-new-but-stale",
                "location_id": "room-a",
                "container_id": None,
                "area_location_id": None,
                "context_quality": "bound",
                "details_json": None,
                "notes": None,
            }
        ]
        raw_bundle = build_replica_bundle(
            inventory_id="inventory-fixture", replica_ref="replica-fixture",
            base=base, head=replica,
        )
        with self.assertRaisesRegex(SyncError, "fresh bound evidence"):
            plan_three_way_merge(
                base=base, canonical_head=base, bundle=raw_bundle,
                merged_store_validator=strict_fixture_validator,
            )

    def test_disjoint_event_additions_are_resequenced_without_changing_canonical_events(self) -> None:
        base = fixture()
        base["inventory_events"] = [
            {"event_id": "event-base", "sequence": 1, "item_id": "item-a"}
        ]
        canonical = copy.deepcopy(base)
        replica = copy.deepcopy(base)
        canonical["inventory_events"].append(
            {"event_id": "event-canonical", "sequence": 10, "item_id": "item-a"}
        )
        for sequence, event_id, item_id in (
            (2, "event-z-first", "item-b"),
            (3, "event-a-second", "item-b"),
        ):
            evidence_id = f"evidence-{event_id}"
            replica["evidence"].append(
                {
                    "evidence_id": evidence_id,
                    "evidence_type": "user_source",
                    "source_ref": "sync resequencing fixture",
                    "captured_on": "2026-08-06",
                    "claim_strength": "explicit_current",
                    "sensitivity": "low",
                    "notes": None,
                }
            )
            replica["item_evidence"].append(
                {"item_id": item_id, "evidence_id": evidence_id, "role": "supporting"}
            )
            replica["inventory_events"].append(
                {
                    "event_id": event_id,
                    "sequence": sequence,
                    "item_id": item_id,
                    "event_type": "moved",
                    "occurred_on": "2026-08-06",
                    "observed_on": "2026-08-06",
                    "occurred_on_precision": "exact",
                    "actor": "sync fixture",
                    "evidence_id": evidence_id,
                    "location_id": "room-a",
                    "container_id": None,
                    "area_location_id": None,
                    "context_quality": "bound",
                    "details_json": None,
                    "notes": None,
                }
            )

        plan = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )

        events = {row["event_id"]: row for row in plan["tables"]["inventory_events"]}
        self.assertEqual(events["event-base"]["sequence"], 1)
        self.assertEqual(events["event-canonical"]["sequence"], 10)
        self.assertEqual(events["event-z-first"]["sequence"], 11)
        self.assertEqual(events["event-a-second"]["sequence"], 12)
        self.assertEqual(
            plan["event_sequence_rewrites"],
            [
                {"event_id": "event-z-first", "replica_sequence": 2, "sequence": 11},
                {"event_id": "event-a-second", "replica_sequence": 3, "sequence": 12},
            ],
        )

    def test_duplicate_event_sequences_are_rejected_before_merge(self) -> None:
        base = fixture()
        base["inventory_events"] = [
            {"event_id": "event-a", "sequence": 1, "item_id": "item-a"},
            {"event_id": "event-b", "sequence": 1, "item_id": "item-b"},
        ]
        with self.assertRaisesRegex(SyncError, "duplicate sequence"):
            self.bundle(base, base)

    def test_base_events_are_preserved_for_every_append_only_violation(self) -> None:
        base = fixture()
        base["inventory_events"] = [
            {"event_id": "event-base", "sequence": 1, "item_id": "item-a"}
        ]
        cases = {
            "canonical mutation": ({"item_id": "item-b"}, None),
            "canonical deletion": ("delete", None),
            "replica mutation": (None, {"item_id": "item-b"}),
            "replica deletion": (None, "delete"),
            "concurrent mutations": ({"item_id": "item-b"}, {"item_id": "item-c"}),
        }
        for label, (canonical_change, replica_change) in cases.items():
            with self.subTest(label=label):
                canonical = copy.deepcopy(base)
                replica = copy.deepcopy(base)
                for tables, change in ((canonical, canonical_change), (replica, replica_change)):
                    if change == "delete":
                        tables["inventory_events"] = []
                    elif isinstance(change, dict):
                        tables["inventory_events"][0].update(change)

                plan = plan_three_way_merge(
                    base=base,
                    canonical_head=canonical,
                    bundle=self.bundle(base, replica),
                    merged_store_validator=strict_fixture_validator,
                )

                self.assertEqual(plan["status"], "needs_resolution")
                conflict = plan["conflicts"][0]
                self.assertEqual(conflict["kind"], "inventory_event_append_only_violation")
                self.assertEqual(conflict["choices"], ["base"])
                resolved = resolve_conflicts(
                    plan,
                    {conflict["conflict_id"]: "base"},
                    merged_store_validator=strict_fixture_validator,
                )
                self.assertEqual(resolved["tables"]["inventory_events"], base["inventory_events"])

    def test_base_audit_receipts_are_append_only_on_every_branch(self) -> None:
        rows = {
            "proposal_commits": {
                "proposal_id": "proposal-base",
                "marker": "original",
            },
            "sync_receipts": {
                "sync_receipt_id": "sync-base",
                "marker": "original",
            },
        }
        for table, row in rows.items():
            for change in ("mutate", "delete"):
                with self.subTest(table=table, change=change):
                    base = fixture()
                    base[table] = [row]
                    canonical = copy.deepcopy(base)
                    replica = copy.deepcopy(base)
                    if change == "mutate":
                        replica[table][0]["marker"] = "rewritten"
                    else:
                        replica[table] = []
                    plan = plan_three_way_merge(
                        base=base,
                        canonical_head=canonical,
                        bundle=self.bundle(base, replica),
                        merged_store_validator=strict_fixture_validator,
                    )
                    self.assertEqual(plan["status"], "needs_resolution")
                    conflict = plan["conflicts"][0]
                    self.assertEqual(conflict["kind"], f"{table}_append_only_violation")
                    self.assertEqual(conflict["choices"], ["base"])
                    resolved = resolve_conflicts(
                        plan,
                        {conflict["conflict_id"]: "base"},
                        merged_store_validator=strict_fixture_validator,
                    )
                    self.assertEqual(resolved["tables"][table], [row])

    def test_canonical_append_wins_divergent_proposal_commit_identity(self) -> None:
        rows = {
            "proposal_commits": (
                {"proposal_id": "proposal-collision", "marker": "canonical"},
                {"proposal_id": "proposal-collision", "marker": "replica"},
            ),
        }
        for table, (canonical_row, replica_row) in rows.items():
            with self.subTest(table=table):
                base = fixture()
                base[table] = []
                canonical = copy.deepcopy(base)
                replica = copy.deepcopy(base)
                canonical[table] = [canonical_row]
                replica[table] = [replica_row]
                plan = plan_three_way_merge(
                    base=base,
                    canonical_head=canonical,
                    bundle=self.bundle(base, replica),
                    merged_store_validator=strict_fixture_validator,
                )
                self.assertEqual(plan["status"], "needs_resolution")
                conflict = plan["conflicts"][0]
                self.assertEqual(
                    conflict["kind"], f"{table}_append_only_identity_collision"
                )
                self.assertEqual(conflict["choices"], ["canonical"])
                resolved = resolve_conflicts(
                    plan,
                    {conflict["conflict_id"]: "canonical"},
                    merged_store_validator=strict_fixture_validator,
                )
                self.assertEqual(resolved["tables"][table], [canonical_row])

    def test_injected_validator_blocks_ready_plan_and_receipt_binds_merged_result(self) -> None:
        base = fixture()
        replica = copy.deepcopy(base)
        replica["items"][0]["quantity"] = 2
        canonical_one = copy.deepcopy(base)
        canonical_one["items"][1]["location_id"] = "room-b"
        canonical_two = copy.deepcopy(canonical_one)
        canonical_two["items"][1]["location_id"] = "room-c"
        bundle = self.bundle(base, replica)

        seen: list[dict] = []

        def strict_seen_validator(tables: dict[str, list[dict]]) -> None:
            strict_fixture_validator(tables)
            seen.append(tables)

        first = plan_three_way_merge(
            base=base,
            canonical_head=canonical_one,
            bundle=bundle,
            merged_store_validator=strict_seen_validator,
        )
        second = plan_three_way_merge(
            base=base,
            canonical_head=canonical_two,
            bundle=bundle,
            merged_store_validator=strict_fixture_validator,
        )
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(receipt_data(first), receipt_data(second))
        self.assertNotEqual(first["merged_digest"], second["merged_digest"])
        with self.assertRaisesRegex(SyncError, "validator rejected"):
            plan_three_way_merge(
                base=base,
                canonical_head=canonical_one,
                bundle=bundle,
                merged_store_validator=lambda _: (_ for _ in ()).throw(ValueError("invalid store")),
            )

    def test_ready_paths_require_and_apply_a_merged_store_validator(self) -> None:
        base = fixture()
        replica = copy.deepcopy(base)
        replica["items"][0]["quantity"] = 2
        bundle = self.bundle(base, replica)
        with self.assertRaisesRegex(SyncError, "validator is required"):
            plan_three_way_merge(base=base, canonical_head=base, bundle=bundle)

        invalid = copy.deepcopy(base)
        invalid["items"][0]["quantity"] = 0
        with self.assertRaisesRegex(SyncError, "fixture item quantity"):
            plan_three_way_merge(
                base=base,
                canonical_head=base,
                bundle=self.bundle(base, invalid),
                merged_store_validator=strict_fixture_validator,
            )

        conflicting_canonical = copy.deepcopy(base)
        conflicting_canonical["items"][0]["ownership_state"] = "lent"
        conflict = plan_three_way_merge(
            base=base,
            canonical_head=conflicting_canonical,
            bundle=bundle,
            merged_store_validator=strict_fixture_validator,
        )
        with self.assertRaisesRegex(SyncError, "validator is required"):
            resolve_conflicts(
                conflict, {conflict["conflicts"][0]["conflict_id"]: "canonical"}
            )

    def test_unknown_content_is_preserved_verbatim(self) -> None:
        base = fixture()
        canonical = copy.deepcopy(base)
        replica = copy.deepcopy(base)
        canonical["items"][1]["location_id"] = "room-b"
        replica["item_tags"].append({"item_id": "item-b", "tag": "added-on-replica"})

        plan = plan_three_way_merge(
            base=base,
            canonical_head=canonical,
            bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )

        item = next(row for row in plan["tables"]["items"] if row["item_id"] == "item-a")
        self.assertEqual(item["unknown_from_import"], {"confidence": "unknown"})
        self.assertIn(
            {"item_id": "item-b", "tag": "added-on-replica"}, plan["tables"]["item_tags"]
        )

    def test_existing_item_conflict_with_independent_dependency_requires_rebase(self) -> None:
        base = fixture()
        base.setdefault("relationships", [])
        canonical = copy.deepcopy(base)
        replica = copy.deepcopy(base)
        canonical["items"][0]["ownership_state"] = "lent"
        replica["items"][0]["quantity"] = 2
        replica.setdefault("relationships", []).append({
            "relationship_id": "rel-replica", "subject_item_id": "item-a", "object_item_id": "item-b",
            "predicate": "works_with", "evidence_id": "ev-fixture", "confidence": "verified",
        })
        plan = plan_three_way_merge(
            base=base, canonical_head=canonical, bundle=self.bundle(base, replica),
            merged_store_validator=strict_fixture_validator,
        )
        conflict = next(row for row in plan["conflicts"] if row["table"] == "items")
        self.assertEqual(conflict["kind"], "identity_collision_requires_rebase")
        self.assertEqual(conflict["choices"], [])
        self.assertIn({"table": "relationships", "identity": ["rel-replica"]}, conflict["dependent_replica_rows"])

    def test_existing_item_model_interface_dependency_requires_rebase(self) -> None:
        base = fixture()
        base["items"][0]["model_id"] = "model-base"
        base["models"] = [{"model_id": "model-base"}]
        base.setdefault("interfaces", [])
        base.setdefault("model_interfaces", [])
        canonical, replica = copy.deepcopy(base), copy.deepcopy(base)
        canonical["items"][0]["ownership_state"] = "lent"
        replica["items"][0]["model_id"] = "model-replica"
        replica["models"].append({"model_id": "model-replica"})
        replica["interfaces"].append({"interface_id": "iface-replica"})
        replica["model_interfaces"].append({"model_id": "model-replica", "interface_id": "iface-replica", "role": "provides", "evidence_id": "ev-fixture"})
        plan = plan_three_way_merge(base=base, canonical_head=canonical, bundle=self.bundle(base, replica), merged_store_validator=strict_fixture_validator)
        conflict = next(row for row in plan["conflicts"] if row["table"] == "items")
        self.assertEqual(conflict["choices"], [])
        self.assertIn({"table": "models", "identity": ["model-replica"]}, conflict["dependent_replica_rows"])
        self.assertIn({"table": "model_interfaces", "identity": ["model-replica", "iface-replica", "provides"]}, conflict["dependent_replica_rows"])

    def test_existing_item_maintenance_parent_child_dependency_requires_rebase(self) -> None:
        base = fixture()
        base.setdefault("maintenance_sessions", [])
        base.setdefault("maintenance_session_items", [])
        canonical, replica = copy.deepcopy(base), copy.deepcopy(base)
        canonical["items"][0]["ownership_state"] = "lent"
        replica["items"][0]["quantity"] = 2
        replica["maintenance_session_items"].append({"maintenance_session_id": "maint-replica", "item_id": "item-a"})
        replica["maintenance_sessions"].append({"maintenance_session_id": "maint-replica"})
        plan = plan_three_way_merge(base=base, canonical_head=canonical, bundle=self.bundle(base, replica), merged_store_validator=strict_fixture_validator)
        conflict = next(row for row in plan["conflicts"] if row["table"] == "items")
        self.assertEqual(conflict["choices"], [])
        dependencies = conflict["dependent_replica_rows"]
        self.assertIn({"table": "maintenance_session_items", "identity": ["maint-replica", "item-a"]}, dependencies)
        self.assertIn({"table": "maintenance_sessions", "identity": ["maint-replica"]}, dependencies)


if __name__ == "__main__":
    unittest.main()
