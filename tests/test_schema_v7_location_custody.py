#!/usr/bin/env python3
"""Focused schema-v7 location, custody, and access tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from property_inventory import cli as cli_module
from property_inventory import rebuild as rebuild_module
from property_inventory.compatibility_policy import compatibility_matrix

HERE = Path(__file__).resolve().parents[1]
CLI = HERE / "property_inventory.py"


def v7_rows(**overrides: list[dict]) -> dict[str, list[dict]]:
    """Build one minimal, semantically valid current generation."""
    rows: dict[str, list[dict]] = {table: [] for table in rebuild_module.TABLES}
    rows["metadata"] = [{"inventory_id": "inv-test", "schema_version": 7}]
    rows["locations"] = [
        {
            "location_id": "loc-site",
            "name": "Site",
            "parent_location_id": None,
            "kind": "site",
            "sensitivity": "personal",
            "notes": None,
        },
        {
            "location_id": "loc-building",
            "name": "Building",
            "parent_location_id": "loc-site",
            "kind": "building",
            "sensitivity": "personal",
            "notes": None,
        },
        {
            "location_id": "loc-floor",
            "name": "First floor",
            "parent_location_id": "loc-building",
            "kind": "floor",
            "sensitivity": "personal",
            "notes": None,
        },
        {
            "location_id": "loc-room",
            "name": "Study",
            "parent_location_id": "loc-floor",
            "kind": "room",
            "sensitivity": "personal",
            "notes": None,
        },
        {
            "location_id": "loc-zone",
            "name": "Desk zone",
            "parent_location_id": "loc-room",
            "kind": "zone",
            "sensitivity": "personal",
            "notes": None,
        },
        {
            "location_id": "loc-furniture",
            "name": "Drawer unit",
            "parent_location_id": "loc-zone",
            "kind": "furniture",
            "sensitivity": "personal",
            "notes": None,
        },
        {
            "location_id": "loc-compartment",
            "name": "Top drawer",
            "parent_location_id": "loc-furniture",
            "kind": "compartment",
            "sensitivity": "personal",
            "notes": None,
        },
    ]
    rows["models"] = [
        {
            "model_id": "mdl-test",
            "name": "Test object",
            "brand": None,
            "model": None,
            "category": "test",
            "specs_json": "{}",
            "interfaces_json": "[]",
            "identifiers_json": "{}",
            "reference_url": None,
        }
    ]
    rows["evidence"] = [
        {
            "evidence_id": "ev-test",
            "evidence_type": "physical_check",
            "source_ref": "test fixture",
            "captured_on": "2026-08-05",
            "claim_strength": "explicit_current",
            "sensitivity": "personal",
            "notes": None,
        }
    ]
    rows["items"] = [
        {
            "item_id": "itm-test",
            "model_id": "mdl-test",
            "quantity": 1,
            "unit": "item",
            "ownership_state": "confirmed",
            "location_id": "loc-room",
            "container_id": "loc-compartment",
            "home_location_id": None,
            "home_container_id": None,
            "condition": None,
            "serial_or_lot": None,
            "acquired_on": None,
            "purchase_price": None,
            "purchase_currency": None,
            "replacement_value": None,
            "value_currency": None,
            "receipt_ref": None,
            "verified_on": "2026-08-05",
            "sensitivity": "personal",
            "identity_sensitivity": "personal",
            "primary_evidence_id": "ev-test",
            "notes": None,
        }
    ]
    rows["item_evidence"] = [
        {"item_id": "itm-test", "evidence_id": "ev-test", "role": "primary"}
    ]
    rows["inventory_events"] = [
        {
            "event_id": "evt-test",
            "sequence": 1,
            "item_id": "itm-test",
            "event_type": "physically_verified",
            "occurred_on": "2026-08-05",
            "observed_on": "2026-08-05",
            "occurred_on_precision": "exact",
            "actor": "Test",
            "evidence_id": "ev-test",
            "location_id": "loc-room",
            "container_id": "loc-compartment",
            "area_location_id": None,
            "context_quality": "bound",
            "details_json": None,
            "notes": None,
        }
    ]
    rows.update(overrides)
    return rows


def party(**overrides: object) -> dict:
    row = {
        "party_id": "party-test",
        "name": "Named party",
        "party_kind": "person",
        "evidence_id": "ev-test",
        "sensitivity": "personal",
        "notes": None,
    }
    row.update(overrides)
    return row


def relation(**overrides: object) -> dict:
    row = {
        "relation_id": "rel-test",
        "item_id": "itm-test",
        "party_id": "party-test",
        "role": "custodian",
        "custody_kind": "possession",
        "status": "active",
        "started_on": None,
        "ended_on": None,
        "ended_evidence_id": None,
        "due_on": None,
        "quantity": None,
        "unit": None,
        "evidence_id": "ev-test",
        "sensitivity": "personal",
        "notes": None,
    }
    row.update(overrides)
    return row


def embodiment(**overrides: object) -> dict:
    row = {
        "embodiment_id": "emb-test",
        "item_id": "itm-test",
        "location_id": "loc-furniture",
        "evidence_id": "ev-test",
        "sensitivity": "personal",
        "notes": None,
    }
    row.update(overrides)
    return row


def items_placed_at(location_id: str, container_id: str | None = None) -> list[dict]:
    """Move the fixture item, and its one location event, together."""
    rows = v7_rows()
    rows["items"][0]["location_id"] = location_id
    rows["items"][0]["container_id"] = container_id
    rows["inventory_events"][0]["location_id"] = location_id
    rows["inventory_events"][0]["container_id"] = container_id
    return rows["items"], rows["inventory_events"]


class SemanticContractTest(unittest.TestCase):
    """Exercise the canonical semantics without paying for a subprocess."""

    def failures(self, **overrides: list[dict]) -> list[str]:
        return rebuild_module.semantic_failures(v7_rows(**overrides))

    def test_current_schema_is_v7_with_custody_tables(self) -> None:
        self.assertEqual(rebuild_module.SCHEMA_VERSION, 7)
        for table in ("parties", "item_party_relations", "location_embodiments"):
            self.assertIn(table, rebuild_module.TABLES)
        self.assertNotIn("placements", rebuild_module.TABLES)

    def test_extended_and_legacy_location_kinds_are_both_retained(self) -> None:
        self.assertEqual(self.failures(), [])
        self.assertLessEqual(
            {"place", "room", "container", "vehicle", "asset", "unknown"},
            rebuild_module.LOCATION_KINDS,
        )
        self.assertLessEqual(
            {"site", "building", "floor", "zone", "furniture", "compartment"},
            rebuild_module.LOCATION_KINDS,
        )
        self.assertLessEqual(
            {"furniture", "compartment"}, rebuild_module.CONTAINER_LOCATION_KINDS
        )

    def test_unknown_custodian_needs_no_invented_party(self) -> None:
        self.assertEqual(
            self.failures(item_party_relations=[relation(party_id=None)]), []
        )

    def test_owner_and_access_relations_require_a_named_party(self) -> None:
        for role in ("owner", "access"):
            failures = self.failures(
                item_party_relations=[relation(role=role, party_id=None)]
            )
            self.assertTrue(
                any("requires a named party" in failure for failure in failures),
                f"{role}: {failures}",
            )

    def test_relation_evidence_must_support_its_item(self) -> None:
        unrelated = {
            "evidence_id": "ev-other",
            "evidence_type": "user_source",
            "source_ref": "unrelated",
            "captured_on": "2026-08-05",
            "claim_strength": "claimed_owned",
            "sensitivity": "personal",
            "notes": None,
        }
        failures = self.failures(
            evidence=[*v7_rows()["evidence"], unrelated],
            parties=[party()],
            item_party_relations=[relation(evidence_id="ev-other")],
        )
        self.assertTrue(
            any("does not support" in failure for failure in failures), failures
        )

    def test_relation_sensitivity_floors_evidence_item_and_party(self) -> None:
        high_party = party(party_id="party-high", sensitivity="high")
        failures = self.failures(
            parties=[high_party],
            item_party_relations=[
                relation(party_id="party-high", sensitivity="personal")
            ],
        )
        self.assertTrue(
            any("sensitivity is lower than" in failure for failure in failures), failures
        )
        self.assertEqual(
            self.failures(
                parties=[high_party],
                item_party_relations=[relation(party_id="party-high", sensitivity="high")],
            ),
            [],
        )

    def test_party_sensitivity_floors_its_evidence(self) -> None:
        failures = self.failures(parties=[party(sensitivity="low")])
        self.assertTrue(
            any("sensitivity is lower than" in failure for failure in failures), failures
        )

    def test_unknown_and_partial_active_custody_allocations_are_coherent(self) -> None:
        second = party(party_id="party-second", name="Second party")
        conflicting = self.failures(
            parties=[party(), second],
            item_party_relations=[
                relation(),
                relation(relation_id="rel-second", party_id="party-second"),
            ],
        )
        self.assertTrue(
            any("more than one unknown active custody" in failure for failure in conflicting),
            conflicting,
        )
        self.assertEqual(
            self.failures(
                parties=[party(), second],
                item_party_relations=[
                    relation(
                        relation_id="rel-past",
                        status="ended",
                        ended_on="2026-08-06",
                        ended_evidence_id="ev-test",
                    ),
                    relation(relation_id="rel-second", party_id="party-second"),
                ],
            ),
            [],
        )

    def test_active_relation_may_not_claim_an_end_date(self) -> None:
        failures = self.failures(
            parties=[party()],
            item_party_relations=[relation(ended_on="2026-08-05")],
        )
        self.assertTrue(
            any("active" in failure and "ended" in failure for failure in failures),
            failures,
        )

    def test_embodiment_is_one_to_one(self) -> None:
        items, events = items_placed_at("loc-zone")
        duplicate_location = self.failures(
            items=items,
            inventory_events=events,
            location_embodiments=[
                embodiment(),
                embodiment(embodiment_id="emb-second", item_id="itm-other"),
            ],
        )
        self.assertTrue(
            any(
                "claimed more than once" in failure or "missing item" in failure
                for failure in duplicate_location
            ),
            duplicate_location,
        )

    def test_embodiment_rejects_self_containment_and_cycles(self) -> None:
        # The cabinet standing in the desk zone is coherent: it holds the drawer,
        # it does not stand inside its own drawer.
        items, events = items_placed_at("loc-zone")
        self.assertEqual(
            self.failures(
                items=items, inventory_events=events, location_embodiments=[embodiment()]
            ),
            [],
        )

        inside_itself = self.failures(
            location_embodiments=[embodiment(location_id="loc-compartment")]
        )
        self.assertTrue(any("cycle" in failure for failure in inside_itself), inside_itself)

        one_level_up = self.failures(location_embodiments=[embodiment()])
        self.assertTrue(any("cycle" in failure for failure in one_level_up), one_level_up)

    def test_home_facts_may_be_unknown_but_never_incoherent(self) -> None:
        rows = v7_rows()
        rows["items"][0]["home_location_id"] = "loc-room"
        rows["items"][0]["home_container_id"] = "loc-compartment"
        self.assertEqual(rebuild_module.semantic_failures(rows), [])

        rows = v7_rows()
        rows["items"][0]["home_location_id"] = "loc-zone"
        rows["items"][0]["home_container_id"] = "loc-compartment"
        rows["locations"][6]["parent_location_id"] = "loc-site"
        failures = rebuild_module.semantic_failures(rows)
        self.assertTrue(
            any("home location/container" in failure for failure in failures), failures
        )

        rows = v7_rows()
        rows["items"][0]["home_container_id"] = "loc-compartment"
        failures = rebuild_module.semantic_failures(rows)
        self.assertTrue(
            any("home location/container" in failure for failure in failures), failures
        )

    def test_location_path_cycles_are_still_rejected(self) -> None:
        rows = v7_rows()
        rows["locations"][0]["parent_location_id"] = "loc-compartment"
        failures = rebuild_module.semantic_failures(rows)
        self.assertTrue(any("cycle" in failure for failure in failures), failures)


class CliContractTest(unittest.TestCase):
    """Exercise the retrieval and migration surfaces an agent actually calls."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-schema-v7-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.assertEqual(self.cli("init")["status"], "initialized")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def store(self) -> Path:
        return self.root / "Data" / "store"

    def command(self, *arguments: str) -> list[str]:
        return [
            sys.executable,
            str(CLI),
            "--inventory-root",
            str(self.root),
            "--runtime-dir",
            str(self.runtime),
            *arguments,
        ]

    def cli(self, *arguments: str) -> dict:
        completed = subprocess.run(
            self.command(*arguments), text=True, capture_output=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def cli_fails(self, *arguments: str) -> dict:
        completed = subprocess.run(
            self.command(*arguments), text=True, capture_output=True, check=False
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return json.loads(completed.stderr)

    def add_location(self, name: str, kind: str, parent: str | None = None) -> str:
        arguments = ["add-location", "--name", name, "--kind", kind]
        if parent is not None:
            arguments += ["--parent-location-id", parent]
        return self.cli(*arguments)["result"]["location_id"]

    def deep_tree(self) -> dict[str, str]:
        site = self.add_location("Riverside site", "site")
        building = self.add_location("North building", "building", site)
        floor = self.add_location("Second floor", "floor", building)
        room = self.add_location("Workshop", "room", floor)
        zone = self.add_location("Bench zone", "zone", room)
        furniture = self.add_location("Tool cabinet", "furniture", zone)
        compartment = self.add_location("Third drawer", "compartment", furniture)
        return {
            "site": site,
            "building": building,
            "floor": floor,
            "room": room,
            "zone": zone,
            "furniture": furniture,
            "compartment": compartment,
        }

    def discover(
        self,
        name: str,
        location_id: str,
        container_id: str | None = None,
        *,
        quantity: float | None = None,
        unit: str = "item",
    ) -> str:
        arguments = [
            "discover",
            "--actor",
            "Test",
            "--source-ref",
            f"v7 fixture {name}",
            "--name",
            name,
            "--category",
            "test fixture",
            "--checked-on",
            "2026-08-05",
            "--location-id",
            location_id,
            "--new-model",
            "--new-unit",
        ]
        if container_id is not None:
            arguments += ["--container-id", container_id]
        if quantity is not None:
            arguments += ["--quantity", str(quantity), "--unit", unit]
        return self.cli(*arguments)["result"]["item_id"]

    def add_party(self, name: str) -> str:
        return self.cli(
            "add-party",
            "--source-ref",
            f"named by Test: {name}",
            "--name",
            name,
            "--party-kind",
            "person",
            "--captured-on",
            "2026-08-05",
        )["result"]["party_id"]

    def test_new_items_leave_home_facts_unknown(self) -> None:
        tree = self.deep_tree()
        item_id = self.discover("home-unknown probe", tree["room"], tree["compartment"])
        item = self.cli("show", item_id)["item"]
        self.assertIsNone(item["home_location_id"])
        self.assertIsNone(item["home_container_id"])

    def test_home_can_be_evidence_backed_then_cleared_to_unknown(self) -> None:
        tree = self.deep_tree()
        item_id = self.discover("clear-home probe", tree["room"])
        self.cli(
            "set-home",
            "--actor",
            "Test",
            "--source-ref",
            "usual home checked",
            "--item-id",
            item_id,
            "--set-on",
            "2026-08-06",
            "--location-id",
            tree["room"],
            "--container-id",
            tree["compartment"],
        )
        self.cli(
            "set-home",
            "--actor",
            "Test",
            "--source-ref",
            "owner says the former home is no longer applicable",
            "--item-id",
            item_id,
            "--set-on",
            "2026-08-07",
            "--clear",
        )
        item = self.cli("show", item_id)["item"]
        self.assertIsNone(item["home_location_id"])
        self.assertIsNone(item["home_container_id"])

    def test_item_views_expose_root_to_leaf_paths(self) -> None:
        tree = self.deep_tree()
        item_id = self.discover("path probe", tree["room"], tree["compartment"])
        shown = self.cli("show", item_id)
        self.assertEqual(
            [step["location_id"] for step in shown["location_path"]],
            [
                tree["site"],
                tree["building"],
                tree["floor"],
                tree["room"],
                tree["zone"],
                tree["furniture"],
                tree["compartment"],
            ],
        )
        self.assertEqual(shown["home_location_path"], [])
        self.assertEqual(shown["container"], "Third drawer")

        for result in (
            self.cli("search", "path probe"),
            self.cli("list-items"),
            self.cli("context", "--task", "path probe"),
        ):
            match = next(
                entry
                for entry in result["matches"]
                if entry["item"]["item_id"] == item_id
            )
            self.assertEqual(
                [step["name"] for step in match["location_path"]][0], "Riverside site"
            )
            self.assertEqual(match["home_location_path"], [])

    def test_location_search_is_path_aware(self) -> None:
        tree = self.deep_tree()
        matched = self.cli("locations", "--query", "riverside third drawer")["matches"]
        self.assertEqual(
            [row["location_id"] for row in matched], [tree["compartment"]]
        )
        record = matched[0]
        self.assertEqual(
            [step["location_id"] for step in record["path"]][0], tree["site"]
        )
        self.assertEqual([step["location_id"] for step in record["chain"]][0], tree["compartment"])

    def test_cli_records_and_ends_ownership_and_access_without_moving_item(self) -> None:
        tree = self.deep_tree()
        item_id = self.discover("relation probe", tree["room"], tree["compartment"])
        party_id = self.add_party("Alex Example")
        before = self.cli("show", item_id)["item"]

        owner = self.cli(
            "ownership-start",
            "--actor",
            "Test",
            "--source-ref",
            "Alex states they own the borrowed item",
            "--item-id",
            item_id,
            "--party-id",
            party_id,
            "--started-on",
            "2026-08-06",
        )["result"]
        access = self.cli(
            "access-grant",
            "--actor",
            "Test",
            "--source-ref",
            "Alex granted current access",
            "--item-id",
            item_id,
            "--party-id",
            party_id,
            "--granted-on",
            "2026-08-06",
        )["result"]
        self.assertEqual(self.cli("show", item_id)["item"], before)

        self.cli(
            "access-revoke",
            "--actor",
            "Test",
            "--source-ref",
            "Access revoked by Alex",
            "--relation-id",
            access["relation_id"],
            "--revoked-on",
            "2026-08-07",
        )
        self.cli(
            "ownership-end",
            "--actor",
            "Test",
            "--source-ref",
            "Alex no longer owns the item",
            "--relation-id",
            owner["relation_id"],
            "--ended-on",
            "2026-08-07",
        )
        relations = {
            row["relation_id"]: row for row in self.read_table("item_party_relations")
        }
        for result in (owner, access):
            relation = relations[result["relation_id"]]
            self.assertEqual(relation["status"], "ended")
            self.assertEqual(relation["evidence_id"], result["evidence_id"])
            self.assertIsNotNone(relation["ended_evidence_id"])
            self.assertNotEqual(relation["evidence_id"], relation["ended_evidence_id"])

    def test_physical_discovery_can_preserve_non_owned_status(self) -> None:
        tree = self.deep_tree()
        result = self.cli(
            "discover",
            "--actor",
            "Test",
            "--source-ref",
            "friend's labelled drill physically checked",
            "--name",
            "borrowed drill",
            "--category",
            "test fixture",
            "--checked-on",
            "2026-08-05",
            "--location-id",
            tree["room"],
            "--new-model",
            "--new-unit",
            "--ownership-state",
            "not_owned",
        )["result"]
        shown = self.cli("show", result["item_id"])
        self.assertEqual(shown["item"]["ownership_state"], "not_owned")
        self.assertIsNone(result["received_event_id"])
        self.assertIsNotNone(result["ingested_event_id"])
        evidence = next(
            row
            for row in self.read_table("evidence")
            if row["evidence_id"] == result["evidence_id"]
        )
        self.assertEqual(evidence["evidence_type"], "physical_check")
        self.assertEqual(evidence["claim_strength"], "explicit_current")
        self.cli(
            "physical-check",
            "--actor",
            "Test",
            "--source-ref",
            "borrowed drill checked again",
            "--item-id",
            result["item_id"],
            "--checked-on",
            "2026-08-06",
            "--location-unchanged",
        )
        self.assertEqual(
            self.cli("show", result["item_id"])["item"]["ownership_state"],
            "not_owned",
        )

    def test_lower_scope_custody_summary_does_not_leak_hidden_relation(self) -> None:
        tree = self.deep_tree()
        item_id = self.discover("scope relation probe", tree["room"])
        party_id = self.cli(
            "add-party",
            "--source-ref",
            "private named party",
            "--name",
            "Hidden custodian",
            "--party-kind",
            "person",
            "--captured-on",
            "2026-08-05",
            "--sensitivity",
            "high",
        )["result"]["party_id"]
        self.cli(
            "access-grant",
            "--actor",
            "Test",
            "--source-ref",
            "private access check",
            "--notes",
            "RELATION_SCOPE_CANARY",
            "--item-id",
            item_id,
            "--party-id",
            party_id,
            "--granted-on",
            "2026-08-06",
        )
        shown = subprocess.run(
            [*self.command("--scope", "personal", "show", item_id)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertNotIn("RELATION_SCOPE_CANARY", shown.stdout)
        payload = json.loads(shown.stdout)
        self.assertEqual(payload["custody"]["access"], [])
        self.assertNotIn(party_id, shown.stdout)

    def test_cli_supports_partial_custody_and_keeps_home_independent(self) -> None:
        tree = self.deep_tree()
        item_id = self.discover(
            "pair of stands",
            tree["room"],
            tree["compartment"],
            quantity=2,
            unit="stands",
        )
        self.cli(
            "set-home",
            "--actor",
            "Test",
            "--source-ref",
            "usual storage confirmed",
            "--item-id",
            item_id,
            "--set-on",
            "2026-08-06",
            "--location-id",
            tree["room"],
            "--container-id",
            tree["compartment"],
        )
        first_party = self.add_party("First borrower")
        second_party = self.add_party("Second borrower")
        relations = []
        for party_id in (first_party, second_party):
            relations.append(
                self.cli(
                    "custody-start",
                    "--actor",
                    "Test",
                    "--source-ref",
                    "one stand handed over",
                    "--item-id",
                    item_id,
                    "--party-id",
                    party_id,
                    "--custody-kind",
                    "loan",
                    "--started-on",
                    "2026-08-07",
                    "--quantity",
                    "1",
                    "--unit",
                    "stands",
                )["result"]
            )
        item = self.cli("show", item_id)
        self.assertEqual(item["item"]["ownership_state"], "confirmed")
        self.assertEqual(item["item"]["home_container_id"], tree["compartment"])
        self.assertEqual(len(item["custody"]["custodians"]), 2)
        store = cli_module.Store(self.store)
        fully_external = cli_module.possession_availability(
            store, store.get("items", item_id), "private"
        )
        self.assertFalse(fully_external["available"])
        self.assertEqual(fully_external["available_quantity"], 0)
        failure = self.cli_fails(
            "custody-start",
            "--actor",
            "Test",
            "--source-ref",
            "impossible third allocation",
            "--item-id",
            item_id,
            "--party-id",
            first_party,
            "--custody-kind",
            "loan",
            "--started-on",
            "2026-08-08",
            "--quantity",
            "1",
            "--unit",
            "stands",
        )
        self.assertIn("active custody allocation would exceed", failure["error"])
        self.cli(
            "custody-end",
            "--actor",
            "Test",
            "--source-ref",
            "one stand returned",
            "--relation-id",
            relations[0]["relation_id"],
            "--ended-on",
            "2026-08-08",
            "--location-id",
            tree["room"],
            "--container-id",
            tree["compartment"],
        )
        store = cli_module.Store(self.store)
        partly_available = cli_module.possession_availability(
            store, store.get("items", item_id), "private"
        )
        self.assertTrue(partly_available["available"])
        self.assertEqual(partly_available["available_quantity"], 1)

    def test_embodied_container_can_move_below_a_later_location_row(self) -> None:
        tree = self.deep_tree()
        item_id = self.discover("mobile cabinet", tree["room"])
        embodied_location = self.add_location(
            "Mobile cabinet interior", "furniture", tree["room"]
        )
        self.cli(
            "embody-location",
            "--source-ref",
            "physical cabinet identity check",
            "--item-id",
            item_id,
            "--location-id",
            embodied_location,
            "--recorded-on",
            "2026-08-06",
        )
        later_parent = self.add_location("New alcove cabinet", "furniture", tree["room"])
        result = self.cli(
            "move",
            "--actor",
            "Test",
            "--source-ref",
            "cabinet moved into alcove",
            "--item-id",
            item_id,
            "--moved-on",
            "2026-08-07",
            "--location-id",
            tree["room"],
            "--container-id",
            later_parent,
        )
        self.assertTrue(result["result"]["embodied_location_reparented"])
        location = next(
            row
            for row in self.read_table("locations")
            if row["location_id"] == embodied_location
        )
        self.assertEqual(location["parent_location_id"], later_parent)
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_store_rejects_a_second_placement_truth(self) -> None:
        (self.store / "placements.jsonl").write_text("")
        failure = self.cli_fails("status")
        self.assertIn("placements.jsonl", failure["error"])

    def test_v6_migration_preserves_history_and_corrects_lent_ownership(self) -> None:
        tree = self.deep_tree()
        item_id = self.discover("lent probe", tree["room"], tree["compartment"])
        self.cli(
            "change",
            "--actor",
            "Test",
            "--source-ref",
            "v7 loan fixture",
            "--item-id",
            item_id,
            "--event-type",
            "lent",
            "--occurred-on",
            "2026-08-06",
        )
        self.assertEqual(self.cli("show", item_id)["item"]["ownership_state"], "confirmed")
        before_evidence = self.read_table("evidence")

        self.downgrade_store_to_v6()
        legacy_events = self.read_table("inventory_events")
        for event in legacy_events:
            if event["event_type"] in {"lent", "loan_returned"}:
                event["details_json"] = None
        (self.store / "inventory_events.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in legacy_events)
        )
        # A v6 ledger expressed the active loan by spending ownership.  This
        # mutation models that historical shape, not the v7 compatibility alias.
        items = self.read_table("items")
        items[0]["ownership_state"] = "lent"
        (self.store / "items.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in items)
        )
        migrated = self.cli("migrate")
        self.assertEqual(migrated["result"]["from_schema"], 6)
        self.assertEqual(migrated["result"]["to_schema"], 7)

        self.assertEqual(self.read_table("inventory_events"), legacy_events)
        self.assertEqual(self.read_table("evidence"), before_evidence)
        self.assertEqual(self.read_table("parties"), [])

        item = self.cli("show", item_id)["item"]
        self.assertEqual(item["ownership_state"], "confirmed")
        self.assertIsNone(item["home_location_id"])
        self.assertIsNone(item["home_container_id"])

        relations = self.read_table("item_party_relations")
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["item_id"], item_id)
        self.assertEqual(relations[0]["role"], "custodian")
        self.assertEqual(relations[0]["status"], "active")
        self.assertIsNone(relations[0]["party_id"])
        self.assertIsNone(relations[0]["started_on"])
        self.assertIsNone(relations[0]["ended_on"])
        self.assertIsNone(relations[0]["ended_evidence_id"])
        self.assertEqual(relations[0]["custody_kind"], "loan")
        self.assertIsNone(relations[0]["due_on"])
        self.assertIsNone(relations[0]["quantity"])
        self.assertIsNone(relations[0]["unit"])
        self.assertEqual(self.cli("status")["verification"]["failures"], [])
        self.cli(
            "return-loan",
            "--actor",
            "Test",
            "--source-ref",
            "legacy loan physically returned",
            "--item-id",
            item_id,
            "--returned-on",
            "2026-08-07",
            "--location-id",
            tree["room"],
            "--container-id",
            tree["compartment"],
        )
        ended_relation = self.read_table("item_party_relations")[0]
        self.assertEqual(ended_relation["status"], "ended")
        self.assertEqual(ended_relation["ended_on"], "2026-08-07")
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def read_table(self, table: str) -> list[dict]:
        return [
            json.loads(line)
            for line in (self.store / f"{table}.jsonl").read_text().splitlines()
        ]

    def downgrade_store_to_v6(self) -> None:
        """Rewrite the current generation into a faithful historical v6 shape."""
        for table in cli_module.V7_TABLES:
            if table not in cli_module.V6_TABLES:
                (self.store / f"{table}.jsonl").unlink()
        items = self.read_table("items")
        for item in items:
            item.pop("home_location_id", None)
            item.pop("home_container_id", None)
        (self.store / "items.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in items)
        )
        metadata = json.loads((self.store / "metadata.jsonl").read_text())
        metadata["schema_version"] = 6
        (self.store / "metadata.jsonl").write_text(
            json.dumps(metadata, sort_keys=True) + "\n"
        )


class CompatibilityPolicyTest(unittest.TestCase):
    def test_matrix_covers_v1_to_v7(self) -> None:
        matrix = compatibility_matrix((3, 11))
        self.assertEqual(matrix.current_schema_version, 7)
        self.assertEqual(
            [entry.schema_version for entry in matrix.entries], [1, 2, 3, 4, 5, 6, 7]
        )
        self.assertEqual(matrix.entry_for(6).action, "migrate_v6_to_v7")
        self.assertEqual(matrix.entry_for(7).action, "read_current")


if __name__ == "__main__":
    unittest.main()
