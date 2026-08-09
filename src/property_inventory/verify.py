#!/usr/bin/env python3
"""Evolution-safe acceptance proof for a canonical Property Inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

try:
    from .rebuild import (
        EVENT_CLAIM_REQUIREMENTS,
        EVENT_EVIDENCE_TYPE_REQUIREMENTS,
        SCHEMA_VERSION,
        TABLES,
        SchemaVersionError,
        canonical_store_digest,
        load_store_current,
        semantic_failures,
    )
    from .render import catalogue_created_on
except ImportError:  # Direct execution during local development.
    from rebuild import (
        EVENT_CLAIM_REQUIREMENTS,
        EVENT_EVIDENCE_TYPE_REQUIREMENTS,
        SCHEMA_VERSION,
        TABLES,
        SchemaVersionError,
        canonical_store_digest,
        load_store_current,
        semantic_failures,
    )
    from render import catalogue_created_on

HERE = Path(__file__).resolve().parent


def scalar(con: sqlite3.Connection, query: str, params: tuple = ()):
    return con.execute(query, params).fetchone()[0]


def normalise(value: object) -> object:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: normalise(part) for key, part in value.items()}
    if isinstance(value, list):
        return [normalise(part) for part in value]
    return value


def canonical_rows(rows: list[dict]) -> list[str]:
    return sorted(
        json.dumps(normalise(row), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        for row in rows
    )


def ordered_rows(rows: list[dict]) -> list[str]:
    return [
        json.dumps(normalise(row), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        for row in rows
    ]


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def current_store_failures(rows: dict[str, list[dict]]) -> list[str]:
    """Return source-level failures that SQLite types alone cannot preserve."""
    failures: list[str] = []
    metadata = rows["metadata"]
    if len(metadata) != 1 or metadata[0].get("schema_version") != SCHEMA_VERSION:
        failures.append(
            f"metadata does not declare exactly schema version {SCHEMA_VERSION}"
        )

    proposal_ids: set[str] = set()
    for proposal in rows["proposal_commits"]:
        proposal_id = proposal.get("proposal_id")
        if not isinstance(proposal_id, str) or not re.fullmatch(
            r"proposal-[0-9a-f-]{36}", proposal_id
        ):
            failures.append(f"invalid canonical proposal receipt id: {proposal_id}")
        elif proposal_id in proposal_ids:
            failures.append(f"duplicate canonical proposal receipt: {proposal_id}")
        else:
            proposal_ids.add(proposal_id)
        for field in ("base_digest", "operations_digest"):
            if not isinstance(proposal.get(field), str) or not re.fullmatch(
                r"[0-9a-f]{64}", proposal[field]
            ):
                failures.append(f"invalid {field} in proposal receipt: {proposal_id}")
        try:
            datetime.fromisoformat(proposal.get("applied_at"))
        except (TypeError, ValueError):
            failures.append(f"invalid applied_at in proposal receipt: {proposal_id}")

    asset_ids: set[str] = set()
    asset_hashes: set[str] = set()
    for asset in rows["media_assets"]:
        asset_id = asset.get("asset_id")
        digest = asset.get("sha256")
        byte_size = asset.get("byte_size")
        if not isinstance(asset_id, str) or not asset_id:
            failures.append("a media asset has a blank asset_id")
        elif asset_id in asset_ids:
            failures.append(f"duplicate media asset_id: {asset_id}")
        else:
            asset_ids.add(asset_id)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            failures.append(f"media asset has an invalid sha256: {asset_id}")
        elif digest in asset_hashes:
            failures.append(f"duplicate media asset sha256: {digest}")
        else:
            asset_hashes.add(digest)
        if type(byte_size) is not int or byte_size < 0:
            failures.append(f"media asset has an invalid byte_size: {asset_id}")
        if asset.get("uri") != f"media://sha256/{digest}":
            failures.append(f"media asset URI disagrees with sha256: {asset_id}")

    evidence_ids = {
        evidence_id
        for row in rows["evidence"]
        if isinstance((evidence_id := row.get("evidence_id")), str)
    }
    evidence_asset_links: set[tuple[object, object, object]] = set()
    for link in rows["evidence_assets"]:
        key = (link.get("evidence_id"), link.get("asset_id"), link.get("role"))
        if all(isinstance(value, str) for value in key):
            if key in evidence_asset_links:
                failures.append(f"duplicate evidence asset link: {key}")
            evidence_asset_links.add(key)
        else:
            failures.append(f"evidence asset link has a non-string identity: {key}")
        if link.get("evidence_id") not in evidence_ids:
            failures.append(f"evidence asset has an unknown evidence_id: {link.get('evidence_id')}")
        if link.get("asset_id") not in asset_ids:
            failures.append(f"evidence asset has an unknown asset_id: {link.get('asset_id')}")
        if link.get("role") not in {
            "source",
            "crop",
            "receipt",
            "appraisal",
            "manual",
            "other",
        }:
            failures.append(f"evidence asset has an invalid role: {link.get('role')}")
        region = link.get("region_json")
        if region is not None:
            try:
                decoded_region = json.loads(region)
            except (TypeError, json.JSONDecodeError):
                failures.append(
                    f"evidence asset has invalid region_json: {link.get('evidence_id')}"
                )
            else:
                def valid_rectangle(value: object) -> bool:
                    return (
                        isinstance(value, dict)
                        and set(value) == {"x", "y", "width", "height"}
                        and all(
                            isinstance(value[field], int)
                            and not isinstance(value[field], bool)
                            and value[field] >= (1 if field in {"width", "height"} else 0)
                            for field in ("x", "y", "width", "height")
                        )
                    )

                if (
                    isinstance(decoded_region, dict)
                    and set(decoded_region) == {"regions"}
                ):
                    regions = decoded_region["regions"]
                    if not (
                        isinstance(regions, list)
                        and regions
                        and all(valid_rectangle(value) for value in regions)
                        and len(
                            {
                                json.dumps(value, sort_keys=True, separators=(",", ":"))
                                for value in regions
                            }
                        )
                        == len(regions)
                    ):
                        failures.append(
                            "evidence asset reserved regions envelope must contain "
                            f"unique strict rectangles: {link.get('evidence_id')}"
                        )
                elif isinstance(decoded_region, dict):
                    # Generic media annotations predate capture and deliberately
                    # remain open JSON objects. Only the reserved capture
                    # envelope receives the stricter rectangle contract.
                    pass
                else:
                    failures.append(
                        f"evidence asset region_json must be an object: {link.get('evidence_id')}"
                    )

    interface_ids: set[str] = set()
    for interface in rows["interfaces"]:
        interface_id = interface.get("interface_id")
        if not isinstance(interface_id, str) or not interface_id:
            failures.append("an interface has a blank interface_id")
        elif interface_id in interface_ids:
            failures.append(f"duplicate interface_id: {interface_id}")
        else:
            interface_ids.add(interface_id)
        if interface.get("direction") not in {"plug", "socket", "bidirectional", "unknown"}:
            failures.append(f"interface has an invalid direction: {interface_id}")
        try:
            decoded_properties = json.loads(interface.get("properties_json"))
        except (TypeError, json.JSONDecodeError):
            failures.append(f"interface has invalid properties_json: {interface_id}")
        else:
            if not isinstance(decoded_properties, dict):
                failures.append(f"interface properties_json must be an object: {interface_id}")

    model_ids = {
        model_id
        for row in rows["models"]
        if isinstance((model_id := row.get("model_id")), str)
    }
    item_models = {
        row.get("item_id"): row.get("model_id") for row in rows["items"]
    }
    evidence_item_models: set[tuple[object, object]] = {
        (link.get("evidence_id"), item_models.get(link.get("item_id")))
        for link in rows["item_evidence"]
    }
    model_interface_links: set[tuple[object, object, object]] = set()
    for link in rows["model_interfaces"]:
        key = (link.get("model_id"), link.get("interface_id"), link.get("role"))
        if all(isinstance(value, str) for value in key):
            if key in model_interface_links:
                failures.append(f"duplicate model interface link: {key}")
            model_interface_links.add(key)
        else:
            failures.append(f"model interface link has a non-string identity: {key}")
        if link.get("model_id") not in model_ids:
            failures.append(f"model interface has an unknown model_id: {link.get('model_id')}")
        if link.get("interface_id") not in interface_ids:
            failures.append(
                f"model interface has an unknown interface_id: {link.get('interface_id')}"
            )
        if link.get("evidence_id") not in evidence_ids:
            failures.append(f"model interface lacks known evidence: {link.get('model_id')}")
        elif (link.get("evidence_id"), link.get("model_id")) not in evidence_item_models:
            failures.append(
                "model interface evidence does not support an item of model: "
                f"{link.get('model_id')}"
            )
        if link.get("role") not in {"provides", "requires", "accepts"}:
            failures.append(f"model interface has an invalid role: {link.get('role')}")

    sequences: set[int] = set()
    for event in rows["inventory_events"]:
        sequence = event.get("sequence")
        if type(sequence) is not int or sequence < 1:
            failures.append(f"inventory event has an invalid sequence: {event.get('event_id')}")
        elif sequence in sequences:
            failures.append(f"duplicate inventory event sequence: {sequence}")
        else:
            sequences.add(sequence)
    return failures


def media_asset_failures(assets: list[dict], media_root: Path | None) -> list[str]:
    """Verify every referenced immutable media byte."""
    if media_root is None:
        return (
            ["media assets exist but --media-root was not supplied"]
            if assets
            else []
        )

    failures: list[str] = []
    for asset in assets:
        asset_id = asset.get("asset_id")
        digest = asset.get("sha256")
        if not isinstance(asset_id, str) or not isinstance(digest, str):
            # Source-level validation reports this more precisely.
            continue
        path = media_root / "sha256" / digest[:2] / digest
        managed_components = (
            (media_root, "media root"),
            (media_root / "sha256", "sha256 directory"),
            (media_root / "sha256" / digest[:2], "sha256 prefix directory"),
            (path, "media asset"),
        )
        unsafe_component = next(
            (
                (component, description)
                for component, description in managed_components
                if component.is_symlink()
            ),
            None,
        )
        if unsafe_component is not None:
            component, description = unsafe_component
            failures.append(
                f"media asset {asset_id} uses an unsafe symlinked {description}: {component}"
            )
            continue
        try:
            file_status = path.lstat()
        except OSError:
            failures.append(f"media asset {asset_id} is missing or not a regular file: {path}")
            continue
        if not stat.S_ISREG(file_status.st_mode):
            failures.append(f"media asset {asset_id} is missing or not a regular file: {path}")
            continue
        byte_size = asset.get("byte_size")
        actual_size = file_status.st_size
        if actual_size != byte_size:
            failures.append(
                f"media asset {asset_id} byte_size mismatch at {path}: "
                f"expected {byte_size}, found {actual_size}"
            )
            continue
        hasher = hashlib.sha256()
        with path.open("rb") as media_file:
            for chunk in iter(lambda: media_file.read(1024 * 1024), b""):
                hasher.update(chunk)
        actual_digest = hasher.hexdigest()
        if actual_digest != digest:
            failures.append(
                f"media asset {asset_id} sha256 mismatch at {path}: "
                f"expected {digest}, found {actual_digest}"
            )
    return failures


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:72] or "record"


def seed_states(
    con: sqlite3.Connection,
    source_items: list[dict],
    account_candidates: list[dict],
    state_overrides: dict[str, str],
) -> dict[str, str]:
    item_ids = {row[0] for row in con.execute("SELECT item_id FROM items")}
    source_state = {
        "claimed_owned": "confirmed",
        "owned_explicitly": "confirmed",
        "purchase_history_only": "candidate",
        "not_owned_explicitly": "not_owned",
    }
    baseline: dict[str, str] = {}
    for source_item in source_items:
        prefix = f"itm-{source_item['id']}"
        for item_id in item_ids:
            if item_id == prefix or item_id.startswith(f"{prefix}-"):
                baseline[item_id] = source_state[source_item["ownership_status"]]
    for candidate in account_candidates:
        if candidate.get("matches_source_id"):
            continue
        item_id = f"itm-account-{slug(candidate['name'])}"
        if item_id in item_ids:
            baseline[item_id] = "candidate"
    baseline.update(state_overrides)
    return baseline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=HERE / ".local" / "inventory.sqlite")
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument(
        "--catalogue-scope",
        choices=("public", "personal", "private"),
        default="personal",
    )
    parser.add_argument("--installation-id")
    parser.add_argument("--source-inventory", type=Path)
    parser.add_argument("--account-candidates", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--media-root", type=Path)
    args = parser.parse_args()

    policy = json.loads(args.policy.read_text()) if args.policy else {}
    state_overrides = policy.get("state_overrides", {})
    minimums = policy.get("acceptance_minimums", {})

    try:
        source_tables = load_store_current(args.store)
    except SchemaVersionError as error:
        print(json.dumps({"status": "fail", "failures": [str(error)]}, indent=2))
        raise SystemExit(1) from error

    source_failures = current_store_failures(source_tables)
    source_failures.extend(semantic_failures(source_tables))
    source_failures.extend(
        media_asset_failures(source_tables["media_assets"], args.media_root)
    )
    if source_failures:
        print(json.dumps({"status": "fail", "failures": source_failures}, indent=2))
        raise SystemExit(1)

    con = sqlite3.connect(args.database)
    con.row_factory = sqlite3.Row
    failures: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    require(not con.execute("PRAGMA foreign_key_check").fetchall(), "foreign-key check failed")

    projection_tables = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    missing_projection_tables = [table for table in TABLES if table not in projection_tables]
    require(
        not missing_projection_tables,
        "SQLite projection is missing canonical tables: " + ", ".join(missing_projection_tables),
    )
    if missing_projection_tables:
        con.close()
        print(json.dumps({"status": "fail", "failures": failures}, indent=2))
        raise SystemExit(1)

    for table_name in TABLES:
        source_rows = source_tables[table_name]
        database_rows = [dict(row) for row in con.execute(f"SELECT * FROM {table_name}")]
        source_view = (
            ordered_rows(source_rows)
            if table_name == "inventory_events"
            else canonical_rows(source_rows)
        )
        database_view = (
            ordered_rows(database_rows)
            if table_name == "inventory_events"
            else canonical_rows(database_rows)
        )
        require(
            source_view == database_view,
            f"SQLite is stale or divergent from store/{table_name}.jsonl",
        )

    require(
        scalar(con, "SELECT count(*) FROM projection_state") == 1
        and scalar(con, "SELECT store_digest FROM projection_state")
        == canonical_store_digest(args.store),
        "SQLite projection is not attested to the current canonical generation",
    )
    require(
        scalar(
            con,
            "SELECT count(*) FROM media_assets WHERE "
            "length(sha256) != 64 OR sha256 GLOB '*[^0-9a-f]*' "
            "OR byte_size < 0 OR uri != 'media://sha256/' || sha256",
        )
        == 0,
        "a media asset fails hash, size or logical-URI integrity",
    )
    require(
        scalar(
            con,
            "SELECT count(*) FROM items i JOIN locations c ON c.location_id=i.container_id "
            "WHERE c.kind NOT IN ('container','vehicle','asset')",
        )
        == 0,
        "an item container_id is not a container, vehicle or asset",
    )
    require(
        scalar(
            con,
            """
            WITH RECURSIVE container_chain(item_id, location_id, parent_location_id, trail) AS (
              SELECT i.item_id, c.location_id, c.parent_location_id,
                     ',' || c.location_id || ','
              FROM items i
              JOIN locations c ON c.location_id=i.container_id
              UNION ALL
              SELECT chain.item_id, parent.location_id, parent.parent_location_id,
                     chain.trail || parent.location_id || ','
              FROM container_chain chain
              JOIN locations parent ON parent.location_id=chain.parent_location_id
              WHERE instr(chain.trail, ',' || parent.location_id || ',')=0
            )
            SELECT count(*)
            FROM items i
            JOIN locations stable ON stable.location_id=i.location_id
            WHERE i.container_id IS NOT NULL
              AND stable.kind != 'unknown'
              AND NOT EXISTS (
                SELECT 1 FROM container_chain chain
                WHERE chain.item_id=i.item_id AND chain.location_id=i.location_id
              )
            """,
        )
        == 0,
        "an item container is not within its stable location",
    )
    require(
        scalar(
            con,
            "SELECT count(*) FROM evidence_assets ea "
            "LEFT JOIN evidence e ON e.evidence_id=ea.evidence_id "
            "LEFT JOIN media_assets ma ON ma.asset_id=ea.asset_id "
            "WHERE e.evidence_id IS NULL OR ma.asset_id IS NULL",
        )
        == 0,
        "an evidence asset link has a missing endpoint",
    )
    require(
        scalar(
            con,
            """
            WITH evidence_item(evidence_id, item_id) AS (
              SELECT evidence_id, item_id FROM item_evidence
              UNION SELECT evidence_id, item_id FROM inventory_events WHERE evidence_id IS NOT NULL
              UNION SELECT evidence_id, subject_item_id FROM relationships
              UNION SELECT evidence_id, object_item_id FROM relationships
              UNION SELECT evidence_id, serves_item_id FROM kits
              UNION SELECT evidence_id, tool_item_id FROM torque_paths
              UNION SELECT evidence_id, item_id FROM kit_requirements WHERE item_id IS NOT NULL
              UNION
              SELECT mi.evidence_id, i.item_id
              FROM model_interfaces mi JOIN items i ON i.model_id=mi.model_id
            )
            SELECT count(*)
            FROM evidence_assets ea
            JOIN media_assets ma ON ma.asset_id=ea.asset_id
            JOIN evidence_item ei ON ei.evidence_id=ea.evidence_id
            JOIN items i ON i.item_id=ei.item_id
            WHERE CASE ma.sensitivity WHEN 'low' THEN 0 WHEN 'personal' THEN 1 ELSE 2 END
                < CASE i.sensitivity WHEN 'low' THEN 0 WHEN 'personal' THEN 1 ELSE 2 END
            """,
        )
        == 0,
        "a media asset is less sensitive than an item its evidence supports",
    )
    require(
        scalar(
            con,
            "SELECT count(*) FROM model_interfaces mi "
            "LEFT JOIN models m ON m.model_id=mi.model_id "
            "LEFT JOIN interfaces i ON i.interface_id=mi.interface_id "
            "LEFT JOIN evidence e ON e.evidence_id=mi.evidence_id "
            "WHERE m.model_id IS NULL OR i.interface_id IS NULL OR e.evidence_id IS NULL",
        )
        == 0,
        "a model interface claim lacks a model, interface or evidence endpoint",
    )
    require(
        scalar(
            con,
            "SELECT count(*) FROM inventory_events "
            "WHERE sequence IS NULL OR sequence < 1",
        )
        == 0,
        "an inventory event has no positive stable sequence",
    )
    require(
        scalar(
            con,
            "SELECT count(*) FROM (SELECT sequence, count(*) c FROM inventory_events "
            "GROUP BY sequence HAVING c != 1)",
        )
        == 0,
        "inventory event sequences are not unique",
    )

    require(
        scalar(
            con,
            "SELECT count(*) FROM items i WHERE NOT EXISTS "
            "(SELECT 1 FROM item_evidence ie WHERE ie.item_id=i.item_id)",
        )
        == 0,
        "an item has no evidence",
    )
    require(
        scalar(
            con,
            "SELECT count(*) FROM items i WHERE "
            "(SELECT count(*) FROM item_evidence ie WHERE ie.item_id=i.item_id AND role='primary') != 1",
        )
        == 0,
        "an item does not have exactly one primary evidence record",
    )
    require(
        scalar(
            con,
            """
            SELECT count(*) FROM items i
            WHERE NOT EXISTS (
              SELECT 1 FROM item_evidence ie
              WHERE ie.item_id=i.item_id
                AND ie.evidence_id=i.primary_evidence_id
                AND ie.role='primary'
            )
            """,
        )
        == 0,
        "items.primary_evidence_id disagrees with the primary item_evidence link",
    )
    require(
        scalar(
            con,
            "SELECT count(*) FROM items i WHERE NOT EXISTS "
            "(SELECT 1 FROM inventory_events ev WHERE ev.item_id=i.item_id)",
        )
        == 0,
        "an item has no lifecycle event",
    )
    require(
        scalar(con, "SELECT count(*) FROM relationships WHERE evidence_id IS NULL") == 0,
        "a relationship has no evidence",
    )
    # A label and a generic spec are not a product identity. Match the writer's
    # Unicode-aware, case-insensitive text semantics and keep canonical
    # structured identity exact.
    model_identities = [
        (
            str(model.get("name") or "").casefold(),
            str(model.get("brand") or "").casefold(),
            str(model.get("model") or "").casefold(),
            str(model.get("category") or "").casefold(),
            model.get("specs_json"),
            model.get("interfaces_json"),
            model.get("identifiers_json"),
        )
        for model in source_tables["models"]
    ]
    require(
        len(model_identities) == len(set(model_identities)),
        "exact product-identity duplicate models remain",
    )
    require(
        scalar(
            con,
            """
            SELECT count(*) FROM inventory_events ev
            LEFT JOIN evidence e ON e.evidence_id=ev.evidence_id
            WHERE ev.event_type='physically_verified'
              AND (
                e.evidence_id IS NULL OR
                e.evidence_type != 'physical_check' OR
                e.claim_strength != 'explicit_current'
              )
            """,
        )
        == 0,
        "a physical-verification event lacks physical_check evidence",
    )
    for event_type, claim_strength in EVENT_CLAIM_REQUIREMENTS.items():
        require(
            scalar(
                con,
                """
                SELECT count(*) FROM inventory_events ev
                LEFT JOIN evidence e ON e.evidence_id=ev.evidence_id
                WHERE ev.event_type=?
                  AND (e.evidence_id IS NULL OR e.claim_strength != ?)
                """,
                (event_type, claim_strength),
            )
            == 0,
            f"a {event_type} event has invalid ownership evidence",
        )
    for event_type, evidence_type in EVENT_EVIDENCE_TYPE_REQUIREMENTS.items():
        require(
            scalar(
                con,
                """
                SELECT count(*) FROM inventory_events ev
                LEFT JOIN evidence e ON e.evidence_id=ev.evidence_id
                WHERE ev.event_type=?
                  AND (e.evidence_id IS NULL OR e.evidence_type != ?)
                """,
                (event_type, evidence_type),
            )
            == 0,
            f"a {event_type} event has invalid evidence type",
        )
    require(
        scalar(
            con,
            """
            SELECT count(*) FROM items i
            WHERE (
                i.verified_on IS NOT NULL OR
                EXISTS (
                    SELECT 1 FROM inventory_events ev
                    WHERE ev.item_id=i.item_id
                      AND ev.event_type='physically_verified'
                )
              )
              AND (
                NOT EXISTS (
                    SELECT 1 FROM inventory_events ev
                    WHERE ev.item_id=i.item_id
                      AND ev.event_type='physically_verified'
                ) OR
                i.verified_on IS NULL OR
                i.verified_on != (
                    SELECT ev.occurred_on FROM inventory_events ev
                    WHERE ev.item_id=i.item_id
                      AND ev.event_type='physically_verified'
                    ORDER BY ev.sequence DESC LIMIT 1
                )
              )
            """,
        )
        == 0,
        "verified_on does not match the latest physical-verification event",
    )

    source_items = (
        json.loads(args.source_inventory.read_text()) if args.source_inventory else []
    )
    expected_source_items = policy.get("expected_source_items")
    if expected_source_items is not None:
        require(
            len(source_items) == expected_source_items,
            "configured source coverage changed",
        )
    for source_item in source_items:
        item_prefix = f"itm-{source_item['id']}"
        require(
            scalar(
                con,
                "SELECT count(*) FROM items WHERE item_id=? OR item_id LIKE ?",
                (item_prefix, f"{item_prefix}-%"),
            )
            > 0,
            f"source item is no longer represented: {source_item['id']}",
        )
        if source_item.get("source_url"):
            require(
                scalar(
                    con,
                    """
                    SELECT count(*) FROM items i JOIN models m ON m.model_id=i.model_id
                    WHERE (i.item_id=? OR i.item_id LIKE ?) AND m.reference_url=?
                    """,
                    (item_prefix, f"{item_prefix}-%", source_item["source_url"]),
                )
                > 0,
                f"source URL is no longer represented: {source_item['id']}",
            )

    account_candidates = (
        json.loads(args.account_candidates.read_text())
        if args.account_candidates
        else []
    )
    for candidate in account_candidates:
        matched = candidate.get("matches_source_id")
        if not matched:
            continue
        account_model_id = "mdl-account-" + re.sub(
            r"[^a-z0-9]+", "-", candidate["name"].casefold()
        ).strip("-")[:72]
        require(
            scalar(con, "SELECT count(*) FROM models WHERE model_id=?", (account_model_id,)) == 0,
            f"matched account record created a duplicate model: {candidate['name']}",
        )
        require(
            scalar(
                con,
                "SELECT count(*) FROM item_evidence WHERE item_id LIKE ? AND role='supporting'",
                (f"itm-{matched}%",),
            )
            >= 1,
            f"matched account record is not supporting its source item: {candidate['name']}",
        )

    baseline = seed_states(con, source_items, account_candidates, state_overrides)
    require(
        set(baseline).issubset(
            {row[0] for row in con.execute("SELECT item_id FROM items")}
        ),
        "a seed-baseline item disappeared",
    )
    state_events = {
        "candidate": {"ordered", "ingested"},
        "confirmed": {
            "received",
            "ingested",
            "reacquired",
            "ownership_corrected",
            "loan_returned",
        },
        "lent": {"lent"},
        "disposed": {"sold", "gifted", "disposed", "lost"},
        "refunded": {"returned", "cancelled", "refunded"},
        "planned": {"planned"},
        "unknown": {"ownership_unresolved", "ingested"},
        "not_owned": {"ownership_excluded", "ingested"},
    }
    transition_events = {
        event_type
        for allowed in state_events.values()
        for event_type in allowed
    }
    placeholders = ",".join("?" for _ in transition_events)
    for row in con.execute("SELECT item_id, ownership_state FROM items"):
        item_id, current_state = row
        event_types = [
            event[0]
            for event in con.execute(
                f"SELECT event_type FROM inventory_events "
                f"WHERE item_id=? AND event_type IN ({placeholders}) ORDER BY sequence",
                (item_id, *sorted(transition_events)),
            )
        ]
        grandfathered_seed = (
            baseline.get(item_id) == current_state
            and event_types == ["ingested"]
        )
        if grandfathered_seed:
            continue
        allowed_events = state_events[current_state]
        if item_id in baseline and baseline[item_id] != current_state:
            allowed_events = allowed_events - {"ingested"}
        require(
            bool(event_types) and event_types[-1] in allowed_events,
            f"item state disagrees with its latest lifecycle event: {item_id} -> {current_state}",
        )

    acceptance = {
        "torque_paths": scalar(con, "SELECT count(*) FROM torque_paths"),
        "kit_rows": scalar(con, "SELECT count(*) FROM kit_requirements"),
        "overlap_rows": scalar(
            con,
            "SELECT count(*) FROM relationships "
            "WHERE predicate IN ('overlaps_function','replaces','unknown')",
        ),
        "ownership_states": scalar(con, "SELECT count(DISTINCT ownership_state) FROM items"),
        "known_move_rows": scalar(
            con,
            """
            WITH RECURSIVE place_tree(location_id) AS (
              SELECT location_id FROM locations
              WHERE kind='place' AND parent_location_id IS NULL
              UNION ALL
              SELECT l.location_id FROM locations l
              JOIN place_tree pt ON l.parent_location_id=pt.location_id
            )
            SELECT count(*) FROM items i
            WHERE i.ownership_state='confirmed'
              AND EXISTS (
                SELECT 1 FROM place_tree pt
                WHERE pt.location_id=i.location_id OR pt.location_id=i.container_id
              )
            """,
        ),
        "unallocated_confirmed_rows": scalar(
            con,
            """
            WITH RECURSIVE place_tree(location_id) AS (
              SELECT location_id FROM locations
              WHERE kind='place' AND parent_location_id IS NULL
              UNION ALL
              SELECT l.location_id FROM locations l
              JOIN place_tree pt ON l.parent_location_id=pt.location_id
            )
            SELECT count(*) FROM items i
            WHERE i.ownership_state='confirmed'
              AND NOT EXISTS (
                SELECT 1 FROM place_tree pt
                WHERE pt.location_id=i.location_id OR pt.location_id=i.container_id
              )
            """,
        ),
        "tracked_containers": scalar(
            con, "SELECT count(*) FROM locations WHERE kind='container'"
        ),
    }
    require(
        acceptance["torque_paths"] >= minimums.get("torque_paths", 0),
        "configured torque path baseline was lost",
    )
    require(
        acceptance["kit_rows"] >= minimums.get("kit_rows", 0),
        "configured kit requirement baseline was lost",
    )
    require(
        acceptance["overlap_rows"] >= minimums.get("overlap_rows", 0),
        "configured functional-overlap baseline was lost",
    )
    require(
        acceptance["tracked_containers"] >= minimums.get("tracked_containers", 0),
        "configured container baseline was lost",
    )
    require(
        acceptance["known_move_rows"] + acceptance["unallocated_confirmed_rows"]
        == scalar(con, "SELECT count(*) FROM items WHERE ownership_state='confirmed'"),
        "move classification drops one or more confirmed items",
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        expected = Path(temp_dir) / "Inventory.md"
        if args.installation_id is not None:
            owner_arguments = ("--installation-id", args.installation_id)
        else:
            owner_matches = re.findall(
                r"^<!-- canonical-inventory-owner-sha256:([0-9a-f]{64}) -->$",
                args.markdown.read_text() if args.markdown.exists() else "",
                re.MULTILINE,
            )
            owner_arguments = (
                ("--owner-digest", owner_matches[0])
                if len(owner_matches) == 1
                else ()
            )
        created_on = catalogue_created_on(
            args.markdown.read_text() if args.markdown.exists() else ""
        )
        created_arguments = (
            ("--created-on", created_on) if created_on is not None else ()
        )
        rendered = subprocess.run(
            [
                sys.executable,
                str(HERE / "render.py"),
                "--database",
                str(args.database),
                "--output",
                str(expected),
                "--scope",
                args.catalogue_scope,
                *owner_arguments,
                *created_arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        require(rendered.returncode == 0, f"catalogue renderer failed: {rendered.stderr.strip()}")
        require(args.markdown.exists(), "generated catalogue is missing")
        if rendered.returncode == 0 and args.markdown.exists():
            require(
                args.markdown.read_bytes() == expected.read_bytes(),
                "generated catalogue is stale or hand-edited",
            )

    eligible = scalar(con, "SELECT count(*) FROM items WHERE ownership_state='confirmed'")
    insurance_gaps = scalar(
        con,
        """
        SELECT count(*) FROM items i
        WHERE i.ownership_state='confirmed' AND (
          i.replacement_value IS NULL OR
          NOT EXISTS (
            SELECT 1 FROM item_documents d
            WHERE d.item_id=i.item_id AND d.document_type='photo'
          ) OR
          NOT EXISTS (
            SELECT 1 FROM item_documents d
            WHERE d.item_id=i.item_id AND d.document_type IN ('receipt','appraisal')
          )
        )
        """,
    )
    insurance_ready = eligible > 0 and insurance_gaps == 0
    result = {
        "status": "pass" if not failures else "fail",
        "counts": {
            table_name: scalar(con, f"SELECT count(*) FROM {table_name}")
            for table_name in TABLES
        },
        "acceptance": acceptance,
        "insurance_ready": insurance_ready,
        "insurance_note": (
            "Schema supports values, receipts and documents; the catalogue is not "
            "insurance-ready until physical evidence is captured."
        ),
        "failures": failures,
    }
    con.close()
    print(json.dumps(result, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
