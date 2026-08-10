"""Deterministic, offline replica-bundle validation and three-way merge planning.

This module deliberately has no knowledge of paths, locks, or transactions.  A
caller may persist a bundle or apply a ready plan through the canonical writer,
but this core never writes canonical JSONL itself.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping
from typing import Any


class SyncError(ValueError):
    """A replica bundle or requested merge is unsafe to use."""


_MISSING = object()
_TRANSPORT_FORMAT = 1
_PLAN_FORMAT = 2
_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "metadata": ("inventory_id",),
    "proposal_commits": ("proposal_id",),
    "locations": ("location_id",),
    "models": ("model_id",),
    "evidence": ("evidence_id",),
    "media_assets": ("asset_id",),
    "interfaces": ("interface_id",),
    "items": ("item_id",),
    "item_evidence": ("item_id", "evidence_id"),
    "evidence_assets": ("evidence_id", "asset_id", "role"),
    "model_interfaces": ("model_id", "interface_id", "role"),
    "relationships": ("relationship_id",),
    "item_documents": ("document_id",),
    "torque_paths": ("path_id",),
    "kits": ("kit_id",),
    "kit_requirements": ("kit_id", "requirement_key"),
    "item_tags": ("item_id", "tag"),
    "aliases": ("alias_id",),
    "spatial_profiles": ("profile_id",),
    "valuations": ("valuation_id",),
    "capture_sessions": ("capture_session_id",),
    "capture_observations": ("observation_id",),
    "maintenance_sessions": ("maintenance_session_id",),
    "maintenance_session_items": ("maintenance_session_id", "item_id"),
    "sync_receipts": ("sync_receipt_id",),
    "kit_reviews": ("review_id",),
    "item_dimensions": ("dimension_id",),
    "item_amendments": ("amendment_id",),
    "item_detail_amendments": ("detail_amendment_id",),
    "fact_amendments": ("fact_amendment_id",),
    "parties": ("party_id",),
    "item_party_relations": ("relation_id",),
    "location_embodiments": ("embodiment_id",),
    "inventory_events": ("event_id",),
}
_ITEM_SEMANTIC_FIELDS = ("ownership_state", "location_id", "container_id", "quantity")
_APPEND_ONLY_TABLES = frozenset(
    {
        "inventory_events",
        "item_amendments",
        "item_detail_amendments",
        "fact_amendments",
        "kit_reviews",
        "item_dimensions",
        "proposal_commits",
        "sync_receipts",
    }
)
# A replica is an offline client of the canonical transaction layer, not a
# second JSONL writer.  These rows are provenance or shared reference data: an
# existing row may never be rewritten by importing a bundle.  New rows remain
# possible because normal transactions can discover new models, evidence and
# capture records while offline.
_IMMUTABLE_REPLICA_BASE_TABLES = frozenset(
    {
        "metadata",
        "models",
        "evidence",
        "media_assets",
        "interfaces",
        "item_evidence",
        "evidence_assets",
        "capture_sessions",
        "capture_observations",
        "maintenance_sessions",
        "maintenance_session_items",
        # Custody, ownership, access and embodiment have no offline write path
        # yet, so a replica may discover new rows but never rewrite one.
        "parties",
        "item_party_relations",
        "location_embodiments",
    }
)
_FACT_AMENDMENT_TABLES = frozenset(
    {
        "locations",
        "aliases",
        "item_tags",
        "relationships",
        "torque_paths",
        "spatial_profiles",
        "kits",
        "kit_requirements",
        "model_interfaces",
        "valuations",
        "item_documents",
        "parties",
        "item_party_relations",
        "location_embodiments",
    }
)
_ITEM_LIFECYCLE_FIELDS = frozenset(
    {"ownership_state", "location_id", "container_id", "quantity", "unit", "verified_on"}
)
_TERMINAL_STATES = frozenset({"disposed", "refunded", "not_owned"})
_TERMINAL_EVENT_TRANSITIONS = {
    "sold": (frozenset({"confirmed", "lent"}), "disposed"),
    "gifted": (frozenset({"confirmed", "lent"}), "disposed"),
    "disposed": (frozenset({"confirmed", "lent"}), "disposed"),
    "lost": (frozenset({"confirmed", "lent"}), "disposed"),
    "returned": (frozenset({"confirmed", "lent"}), "refunded"),
    "cancelled": (frozenset({"candidate", "planned"}), "refunded"),
    "refunded": (frozenset({"candidate", "planned", "confirmed"}), "refunded"),
    "ownership_excluded": (
        frozenset({"candidate", "planned", "unknown"}),
        "not_owned",
    ),
}
_REPLICA_EVENT_TYPES = frozenset(
    {
        "planned",
        "ordered",
        "received",
        "physically_verified",
        "moved",
        "quantity_changed",
        "lent",
        "loan_returned",
        "reacquired",
        "ownership_corrected",
        "ownership_unresolved",
        "not_found_in_area",
        *_TERMINAL_EVENT_TRANSITIONS,
    }
)
_LIFECYCLE_PROJECTION_FIELDS = (
    "ownership_state",
    "location_id",
    "container_id",
    "quantity",
    "unit",
    "verified_on",
)
_ITEM_DETAIL_AMENDMENT_FIELDS = frozenset(
    {
        "acquired_on",
        "condition",
        "home_container_id",
        "home_location_id",
        "purchase_currency",
        "purchase_price",
        "receipt_ref",
        "serial_or_lot",
    }
)
_REACQUISITION_RESET_FIELDS = (
    "acquired_on",
    "condition",
    "purchase_currency",
    "purchase_price",
    "receipt_ref",
)


def _media_manifest(
    base_tables: Mapping[str, list[dict[str, Any]]],
    head_tables: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return the exact bytes a replica must carry beyond its trusted base."""
    base_digests = {
        row.get("sha256") for row in base_tables.get("media_assets", [])
    }
    records: dict[str, dict[str, Any]] = {}
    for row in head_tables.get("media_assets", []):
        digest = row.get("sha256")
        size = row.get("byte_size")
        media_type = row.get("media_type")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(media_type, str)
            or not media_type
        ):
            raise SyncError("replica media asset metadata is malformed")
        record = {"sha256": digest, "byte_size": size, "media_type": media_type}
        prior = records.setdefault(digest, record)
        if prior != record:
            raise SyncError("replica media digest has conflicting metadata")
    return [records[digest] for digest in sorted(records) if digest not in base_digests]


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise SyncError(f"sync value is not JSON-serializable: {error}") from error


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _copy_row(row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise SyncError("every replica row must be a JSON object")
    copied = json.loads(_canonical_json(dict(row)))
    if not isinstance(copied, dict):  # Defensive, although dict(row) guarantees it.
        raise SyncError("every replica row must be a JSON object")
    return copied


def _identity_fields(table: str, row: Mapping[str, Any]) -> tuple[str, ...]:
    known = _IDENTITY_FIELDS.get(table)
    if known is not None:
        return known
    raise SyncError(f"table {table!r} is not supported for replica sync")


def _identity(table: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
    fields = _identity_fields(table, row)
    values = []
    for field in fields:
        value = row.get(field)
        if not isinstance(value, str) or not value:
            raise SyncError(f"{table} row has a non-string or blank identity field: {field}")
        values.append(value)
    return tuple(values)


def _identity_json(identity: tuple[Any, ...]) -> str:
    return _canonical_json(list(identity))


def _normalise_tables(
    tables: Mapping[str, Iterable[Mapping[str, Any]]], *, validate_event_sequences: bool = True
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(tables, Mapping) or not tables:
        raise SyncError("replica tables must be a non-empty mapping")
    normalised: dict[str, list[dict[str, Any]]] = {}
    for table, rows in tables.items():
        if not isinstance(table, str) or not table:
            raise SyncError("replica table names must be non-empty strings")
        if isinstance(rows, (str, bytes)):
            raise SyncError(f"{table} rows must be an iterable of JSON objects")
        try:
            copied = [_copy_row(row) for row in rows]
        except TypeError as error:
            raise SyncError(f"{table} rows must be iterable") from error
        identities = [_identity(table, row) for row in copied]
        if len(set(_identity_json(value) for value in identities)) != len(identities):
            raise SyncError(f"{table} has duplicate row identities")
        normalised[table] = [
            row
            for _, row in sorted(
                zip(identities, copied, strict=True), key=lambda pair: _identity_json(pair[0])
            )
        ]
        if table == "locations":
            normalised[table] = _parent_first_locations(normalised[table])
    ordered = {table: normalised[table] for table in sorted(normalised)}
    if validate_event_sequences:
        _validate_inventory_event_sequences(ordered)
    return ordered


def _parent_first_locations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Serialize locations parent-first so the JSONL projection can load under FK checks."""
    by_id = {row["location_id"]: row for row in rows}
    parents: dict[str, str | None] = {}
    children: dict[str, list[str]] = {location_id: [] for location_id in by_id}
    for location_id, row in by_id.items():
        parent = row.get("parent_location_id")
        if parent is not None and (not isinstance(parent, str) or parent not in by_id):
            raise SyncError("location parent is missing from replica tables")
        parents[location_id] = parent
        if parent is not None:
            children[parent].append(location_id)
    output: list[dict[str, Any]] = []
    ready = sorted(location_id for location_id, parent in parents.items() if parent is None)
    while ready:
        location_id = ready.pop(0)
        output.append(by_id[location_id])
        ready.extend(sorted(children[location_id]))
        ready.sort()
    if len(output) != len(rows):
        raise SyncError("locations contain a parent cycle")
    return output


def _validate_inventory_event_sequences(tables: Mapping[str, list[dict[str, Any]]]) -> None:
    """Reject event streams that SQLite's UNIQUE sequence constraint would reject."""
    sequences: set[int] = set()
    for row in tables.get("inventory_events", []):
        sequence = row.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise SyncError("inventory_events rows need a positive integer sequence")
        if sequence in sequences:
            raise SyncError("inventory_events has duplicate sequence values")
        sequences.add(sequence)


def _table_bytes(rows: list[dict[str, Any]]) -> bytes:
    return "".join(_canonical_json(row) + "\n" for row in rows).encode("utf-8")


def _snapshot(tables: Mapping[str, Iterable[Mapping[str, Any]]]) -> dict[str, Any]:
    normalised = _normalise_tables(tables)
    files = {
        f"{table}.jsonl": hashlib.sha256(_table_bytes(rows)).hexdigest()
        for table, rows in normalised.items()
    }
    return {
        "digest": _digest({"files": files}),
        "files": files,
        "tables": normalised,
    }


def store_digest(tables: Mapping[str, Iterable[Mapping[str, Any]]]) -> str:
    """Return the canonical digest for an in-memory JSONL store."""
    return _snapshot(tables)["digest"]


def build_store_snapshot(
    tables: Mapping[str, Iterable[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Build the exact portable snapshot envelope accepted by replica commands."""
    snapshot = _snapshot(tables)
    inventory_id = _metadata_inventory_id(snapshot["tables"], "snapshot")
    return {
        "format": _TRANSPORT_FORMAT,
        "inventory_id": inventory_id,
        **snapshot,
    }


def verify_store_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a portable snapshot without discarding its declared digests."""
    required = {"format", "inventory_id", "digest", "files", "tables"}
    if not isinstance(snapshot, Mapping) or set(snapshot) != required:
        raise SyncError("store snapshot envelope is malformed")
    if (
        isinstance(snapshot["format"], bool)
        or not isinstance(snapshot["format"], int)
        or snapshot["format"] != _TRANSPORT_FORMAT
    ):
        raise SyncError("unsupported store snapshot format")
    if not isinstance(snapshot["inventory_id"], str) or not snapshot["inventory_id"]:
        raise SyncError("store snapshot inventory_id is malformed")
    verified = _validate_snapshot(
        {key: snapshot[key] for key in ("digest", "files", "tables")}, "store"
    )
    _assert_inventory_identity(verified["tables"], snapshot["inventory_id"], "snapshot")
    return {
        "format": _TRANSPORT_FORMAT,
        "inventory_id": snapshot["inventory_id"],
        **verified,
    }


def _metadata_inventory_id(tables: Mapping[str, list[dict[str, Any]]], label: str) -> str:
    rows = tables.get("metadata")
    if not isinstance(rows, list) or len(rows) != 1:
        raise SyncError(f"{label} must contain exactly one metadata row")
    inventory_id = rows[0].get("inventory_id")
    if not isinstance(inventory_id, str) or not inventory_id:
        raise SyncError(f"{label} metadata inventory_id is malformed")
    return inventory_id


def _assert_inventory_identity(
    tables: Mapping[str, list[dict[str, Any]]], inventory_id: str, label: str
) -> None:
    actual = _metadata_inventory_id(tables, label)
    if actual != inventory_id:
        raise SyncError(f"{label} metadata inventory_id does not match bundle manifest")


def build_replica_bundle(
    *, inventory_id: str, replica_ref: str, base: Mapping[str, Iterable[Mapping[str, Any]]],
    head: Mapping[str, Iterable[Mapping[str, Any]]]
) -> dict[str, Any]:
    """Build a self-verifying, deterministic offline replica bundle."""
    if not isinstance(inventory_id, str) or not inventory_id:
        raise SyncError("inventory_id must be a non-empty string")
    if not isinstance(replica_ref, str) or not replica_ref:
        raise SyncError("replica_ref must be a non-empty string")
    base_snapshot = _snapshot(base)
    head_snapshot = _snapshot(head)
    if set(base_snapshot["tables"]) != set(head_snapshot["tables"]):
        raise SyncError("replica base and head must have the same canonical table set")
    _assert_inventory_identity(base_snapshot["tables"], inventory_id, "replica base")
    _assert_inventory_identity(head_snapshot["tables"], inventory_id, "replica head")
    bundle: dict[str, Any] = {
        "format": _TRANSPORT_FORMAT,
        "inventory_id": inventory_id,
        "replica_ref": replica_ref,
        "base": base_snapshot,
        "head": head_snapshot,
        "media": _media_manifest(base_snapshot["tables"], head_snapshot["tables"]),
    }
    bundle["bundle_digest"] = _digest(bundle)
    return bundle


def _validate_snapshot(snapshot: Any, label: str) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping) or set(snapshot) != {"digest", "files", "tables"}:
        raise SyncError(f"replica {label} snapshot is malformed")
    recalculated = _snapshot(snapshot["tables"])
    if snapshot["files"] != recalculated["files"] or snapshot["digest"] != recalculated["digest"]:
        raise SyncError(f"replica {label} snapshot digest mismatch")
    return recalculated


def verify_replica_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Validate every declared file digest and the enclosing manifest digest."""
    required = {
        "format", "inventory_id", "replica_ref", "base", "head", "media", "bundle_digest"
    }
    if not isinstance(bundle, Mapping) or set(bundle) != required:
        raise SyncError("replica bundle manifest is malformed")
    if (
        isinstance(bundle["format"], bool)
        or not isinstance(bundle["format"], int)
        or bundle["format"] != _TRANSPORT_FORMAT
    ):
        raise SyncError("unsupported replica bundle format")
    if not isinstance(bundle["inventory_id"], str) or not bundle["inventory_id"]:
        raise SyncError("replica bundle inventory_id is malformed")
    if not isinstance(bundle["replica_ref"], str) or not bundle["replica_ref"]:
        raise SyncError("replica bundle replica_ref is malformed")
    declared = bundle["bundle_digest"]
    unsigned = {key: bundle[key] for key in required if key != "bundle_digest"}
    if not isinstance(declared, str) or declared != _digest(unsigned):
        raise SyncError("replica bundle manifest digest mismatch")
    base = _validate_snapshot(bundle["base"], "base")
    head = _validate_snapshot(bundle["head"], "head")
    if set(base["tables"]) != set(head["tables"]):
        raise SyncError("replica bundle base and head table sets differ")
    _assert_inventory_identity(base["tables"], bundle["inventory_id"], "replica base")
    _assert_inventory_identity(head["tables"], bundle["inventory_id"], "replica head")
    if bundle["media"] != _media_manifest(base["tables"], head["tables"]):
        raise SyncError("replica bundle media manifest mismatch")
    return {
        "format": _TRANSPORT_FORMAT,
        "inventory_id": bundle["inventory_id"],
        "replica_ref": bundle["replica_ref"],
        "base": base,
        "head": head,
        "media": list(bundle["media"]),
        "bundle_digest": declared,
    }


def _row_map(table: str, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {_identity_json(_identity(table, row)): row for row in rows}


def _changed(before: Any, after: Any) -> bool:
    if before is _MISSING or after is _MISSING:
        return before is not after
    return _canonical_json(before) != _canonical_json(after)


def _conflict_id(table: str, identity: str, base: Any, canonical: Any, replica: Any) -> str:
    return "conflict-" + _digest(
        {
            "table": table,
            "identity": identity,
            "base": _external_row(base),
            "canonical": _external_row(canonical),
            "replica": _external_row(replica),
        }
    )[:24]


def _external_row(value: Any) -> Any:
    return None if value is _MISSING else value


def _semantic_fields(table: str, canonical: Any, replica: Any) -> list[str]:
    if table != "items" or canonical is _MISSING or replica is _MISSING:
        return []
    return [
        field
        for field in _ITEM_SEMANTIC_FIELDS
        if canonical.get(field) != replica.get(field)
    ]


def _replica_dependency_rows(
    *,
    parent_table: str,
    parent_identity: str,
    base_tables: Mapping[str, list[dict[str, Any]]],
    replica_tables: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Find changed replica rows that bind to one colliding canonical identity."""
    identity_values = {
        value for value in json.loads(parent_identity) if isinstance(value, str)
    }
    dependencies: list[dict[str, Any]] = []
    for table in sorted(replica_tables):
        base_rows = _row_map(table, base_tables.get(table, []))
        for identity, row in _row_map(table, replica_tables.get(table, [])).items():
            if table == parent_table and identity == parent_identity:
                continue
            if not _changed(base_rows.get(identity, _MISSING), row):
                continue
            if any(
                key.endswith("_id") and isinstance(value, str) and value in identity_values
                for key, value in row.items()
            ):
                dependencies.append(
                    {"table": table, "identity": json.loads(identity)}
                )
    return dependencies


def _item_conflict_dependency_rows(
    *,
    item_id: str,
    before: Mapping[str, Any],
    replica: Mapping[str, Any],
    base_tables: Mapping[str, list[dict[str, Any]]],
    replica_tables: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Close replica-only facts that would be left incoherent by dropping one item branch."""
    seeds = {item_id}
    if before.get("model_id") != replica.get("model_id") and isinstance(replica.get("model_id"), str):
        seeds.add(replica["model_id"])
    history = {"items", "inventory_events", "item_amendments", "item_detail_amendments", "item_evidence"}
    transit_only = {"evidence", "evidence_assets", "media_assets", "models", "interfaces"}
    discovered: dict[tuple[str, str], dict[str, Any]] = {}
    changed: list[tuple[str, str, dict[str, Any]]] = []
    for table in sorted(replica_tables):
        base_rows = _row_map(table, base_tables.get(table, []))
        for identity, row in _row_map(table, replica_tables.get(table, [])).items():
            if _changed(base_rows.get(identity, _MISSING), row):
                changed.append((table, identity, row))
                if table in history and row.get("item_id") == item_id:
                    seeds.update(
                        value
                        for key, value in row.items()
                        if key.endswith("_id") and isinstance(value, str)
                    )
    # A model created solely to support the losing identity-correction branch
    # would otherwise survive as an unreferenced matcher candidate.  It is not
    # safe to prune at conflict time because a model may have transitive facts;
    # require a rebase before selecting canonical history instead.
    target_model = replica.get("model_id")
    base_model_ids = {
        row.get("model_id") for row in base_tables.get("models", []) if isinstance(row.get("model_id"), str)
    }
    if (
        isinstance(target_model, str)
        and target_model != before.get("model_id")
        and target_model not in base_model_ids
    ):
        for table, identity, row in changed:
            if table == "models" and row.get("model_id") == target_model:
                discovered[(table, identity)] = {"table": table, "identity": json.loads(identity)}
    progressed = True
    while progressed:
        progressed = False
        for table, identity, row in changed:
            if table in history or (table, identity) in discovered:
                continue
            references = {
                value for key, value in row.items() if key.endswith("_id") and isinstance(value, str)
            }
            if not references & seeds:
                continue
            if table not in transit_only:
                discovered[(table, identity)] = {"table": table, "identity": json.loads(identity)}
            before_size = len(seeds)
            seeds.update(references)
            progressed = progressed or len(seeds) != before_size
    return [discovered[key] for key in sorted(discovered)]


def _plan_digest(plan: Mapping[str, Any]) -> str:
    return _digest({key: value for key, value in plan.items() if key != "plan_digest"})


def _application_digest(
    *,
    format: int,
    inventory_id: str,
    replica_ref: str,
    base_digest: str,
    bundle_digest: str,
    canonical_head_digest: str,
    replica_head_digest: str,
    resolutions: list[dict[str, str]],
    event_sequence_rewrites: list[dict[str, Any]],
    tables: Mapping[str, Iterable[Mapping[str, Any]]],
) -> str:
    return _digest(
        {
            "format": format,
            "inventory_id": inventory_id,
            "replica_ref": replica_ref,
            "base_digest": base_digest,
            "bundle_digest": bundle_digest,
            "canonical_head_digest": canonical_head_digest,
            "replica_head_digest": replica_head_digest,
            "resolutions": resolutions,
            "event_sequence_rewrites": event_sequence_rewrites,
            "tables": tables,
        }
    )


def _receipt_for_application(
    *, replica_ref: str, replica_head_digest: str, application_digest: str
) -> dict[str, str]:
    return {
        "sync_receipt_id": "sync-" + application_digest,
        "replica_ref": replica_ref,
        "payload_digest": replica_head_digest,
    }


def _resequence_remote_event_additions(
    tables: dict[str, list[dict[str, Any]]],
    *,
    base_tables: Mapping[str, list[dict[str, Any]]],
    canonical_tables: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Keep canonical ledger ordering and make disjoint replica appends unique.

    Inventory events are append-only.  A remote event may only be new relative to
    both the replica base and the canonical head.  New events are ordered by their
    original ``(sequence, event_id)`` and receive a contiguous sequence range after
    canonical events.  The returned audit mapping is part of the signed plan.
    """
    events = tables.get("inventory_events")
    if events is None:
        return []
    base_events = _row_map("inventory_events", list(base_tables.get("inventory_events", [])))
    canonical_events = _row_map(
        "inventory_events", list(canonical_tables.get("inventory_events", []))
    )
    canonical_sequences: set[int] = set()
    remote_additions: list[dict[str, Any]] = []
    for event in events:
        event_id = _identity_json(_identity("inventory_events", event))
        canonical = canonical_events.get(event_id)
        if canonical is not None:
            if event["sequence"] != canonical["sequence"]:
                raise SyncError("a replica sync cannot change a canonical inventory event sequence")
            canonical_sequences.add(canonical["sequence"])
            continue
        if event_id in base_events:
            if _changed(base_events[event_id], event):
                raise SyncError("a replica sync cannot alter a base inventory event")
            # A resolution may have restored an event that a head deleted.  It
            # remains the original base event, never a remote reintroduction.
            canonical_sequences.add(event["sequence"])
            continue
        remote_additions.append(event)

    next_sequence = max(canonical_sequences, default=0) + 1
    rewrites: list[dict[str, Any]] = []
    for event in sorted(remote_additions, key=lambda row: (row["sequence"], row["event_id"])):
        requested = event["sequence"]
        assigned = next_sequence
        while assigned in canonical_sequences:
            assigned += 1
        if requested != assigned:
            event["sequence"] = assigned
            rewrites.append(
                {
                    "event_id": event["event_id"],
                    "replica_sequence": requested,
                    "sequence": assigned,
                }
            )
        canonical_sequences.add(assigned)
        next_sequence = assigned + 1
    _validate_inventory_event_sequences(tables)
    return rewrites


def _validate_merged_tables(
    tables: Mapping[str, Iterable[Mapping[str, Any]]],
    validator: Callable[[Mapping[str, list[dict[str, Any]]]], None] | None,
) -> None:
    """Offer an isolated merged snapshot to the canonical semantic verifier."""
    if not callable(validator):
        raise SyncError("a merged store validator is required before a sync plan is ready")
    isolated = _normalise_tables(tables)
    try:
        validator(isolated)
    except SyncError:
        raise
    except Exception as error:
        raise SyncError(f"merged store validator rejected sync plan: {error}") from error


def _fact_amendment_selector_matches(
    row: Mapping[str, Any], *, table: str, identity: tuple[Any, ...]
) -> bool:
    """Return whether one immutable amendment belongs to one conflicted fact row."""
    fields = _IDENTITY_FIELDS.get(table)
    if fields is None or row.get("table_name") != table:
        return False
    encoded = row.get("selector_json")
    if not isinstance(encoded, str):
        return False
    try:
        selector = json.loads(encoded)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(selector, dict)
        and set(selector) == set(fields)
        and all(selector.get(field) == value for field, value in zip(fields, identity, strict=True))
    )


def _new_branch_rows(
    table: str,
    *,
    base_tables: Mapping[str, list[dict[str, Any]]],
    replica_tables: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return rows appended by the replica, preserving the table's identity rule."""
    base_rows = _row_map(table, base_tables.get(table, []))
    return [
        row
        for identity, row in _row_map(table, replica_tables.get(table, [])).items()
        if identity not in base_rows
    ]


def _json_object_matches(encoded: object, expected: Mapping[str, Any]) -> bool:
    if not isinstance(encoded, str):
        return False
    try:
        decoded = json.loads(encoded)
    except json.JSONDecodeError:
        return False
    return isinstance(decoded, dict) and _canonical_json(decoded) == _canonical_json(expected)


def _has_matching_fact_amendment(
    amendments: Iterable[Mapping[str, Any]],
    *,
    table: str,
    identity: tuple[Any, ...],
    before: Mapping[str, Any],
    after: Mapping[str, Any] | object,
) -> bool:
    chain = [
        amendment
        for amendment in amendments
        if _fact_amendment_selector_matches(amendment, table=table, identity=identity)
    ]
    if not chain:
        return False
    chain.sort(
        key=lambda row: (
            str(row.get("amended_on", "")),
            str(row.get("recorded_at", "")),
            str(row.get("fact_amendment_id", "")),
        )
    )
    current: Mapping[str, Any] | object = before
    for amendment in chain:
        if current is _MISSING or not _json_object_matches(amendment.get("previous_json"), current):
            return False
        if amendment.get("action") == "retract" and amendment.get("replacement_json") is None:
            current = _MISSING
        elif amendment.get("action") == "replace":
            encoded = amendment.get("replacement_json")
            try:
                replacement = json.loads(encoded) if isinstance(encoded, str) else None
            except json.JSONDecodeError:
                return False
            if not isinstance(replacement, dict):
                return False
            current = replacement
        else:
            return False
    return current is after or (
        current is not _MISSING and after is not _MISSING and _canonical_json(current) == _canonical_json(after)
    )


def _has_matching_item_amendment(
    amendments: Iterable[Mapping[str, Any]],
    *,
    item_id: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    chain = [row for row in amendments if row.get("item_id") == item_id]
    if not chain:
        return False
    chain.sort(
        key=lambda row: (
            str(row.get("amended_on", "")),
            str(row.get("recorded_at", "")),
            str(row.get("amendment_id", "")),
        )
    )
    current = before.get("model_id")
    for amendment in chain:
        previous, target = amendment.get("previous_model_id"), amendment.get("target_model_id")
        if not isinstance(target, str) or previous != current or target == previous:
            return False
        current = target
    return current == after.get("model_id")


def _has_matching_item_detail_amendment(
    amendments: Iterable[Mapping[str, Any]],
    *,
    item_id: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    changed_fields: set[str],
) -> bool:
    if not changed_fields <= _ITEM_DETAIL_AMENDMENT_FIELDS:
        return False
    chain = [row for row in amendments if row.get("item_id") == item_id]
    if not chain:
        return False
    chain.sort(
        key=lambda row: (
            str(row.get("amended_on", "")),
            str(row.get("recorded_at", "")),
            str(row.get("detail_amendment_id", "")),
        )
    )
    current = {field: before.get(field) for field in _ITEM_DETAIL_AMENDMENT_FIELDS}
    for amendment in chain:
        if not _json_object_matches(amendment.get("previous_json"), current):
            return False
        encoded = amendment.get("changes_json")
        try:
            changes = json.loads(encoded) if isinstance(encoded, str) else None
        except json.JSONDecodeError:
            return False
        if (
            not isinstance(changes, dict)
            or not changes
            or not set(changes) <= _ITEM_DETAIL_AMENDMENT_FIELDS
        ):
            return False
        current.update(changes)
    return all(current[field] == after.get(field) for field in _ITEM_DETAIL_AMENDMENT_FIELDS)


def _event_details(event: Mapping[str, Any]) -> dict[str, Any] | None:
    encoded = event.get("details_json")
    if not isinstance(encoded, str):
        return None
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError:
        return None
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or canonical != encoded:
        return None
    return value


def _reacquisition_declaration(
    event: Mapping[str, Any],
) -> dict[str, Any] | None:
    details = _event_details(event)
    if (
        details is None
        or set(details)
        != {"condition_checked", "quantity_checked", "reset_fields", "unit"}
        or details.get("reset_fields") != list(_REACQUISITION_RESET_FIELDS)
        or not (
            details.get("condition_checked") is None
            or (
                isinstance(details.get("condition_checked"), str)
                and details["condition_checked"].strip()
            )
        )
        or (
            details.get("quantity_checked") is None
            and details.get("unit") is not None
        )
        or (
            details.get("quantity_checked") is not None
            and (
                type(details["quantity_checked"]) not in {int, float}
                or not math.isfinite(details["quantity_checked"])
                or details["quantity_checked"] <= 0
                or not isinstance(details.get("unit"), str)
                or not details["unit"].strip()
            )
        )
    ):
        return None
    return details


def _validate_new_replica_events(
    *,
    base_events: Iterable[Mapping[str, Any]],
    new_events: Iterable[Mapping[str, Any]],
    new_evidence: Iterable[Mapping[str, Any]],
    new_item_evidence: Iterable[Mapping[str, Any]],
    replica_items: Iterable[Mapping[str, Any]],
) -> None:
    """Require every replica event to be a fresh, append-only CLI-shaped fact."""
    base_rows = list(base_events)
    additions = sorted(new_events, key=lambda row: row.get("sequence", -1))
    expected_sequences = list(
        range(
            max((row.get("sequence", 0) for row in base_rows), default=0) + 1,
            max((row.get("sequence", 0) for row in base_rows), default=0)
            + len(additions)
            + 1,
        )
    )
    if [row.get("sequence") for row in additions] != expected_sequences:
        raise SyncError("replica inventory events must be appended contiguously after the base")

    evidence_by_id = {
        row.get("evidence_id"): row
        for row in new_evidence
        if isinstance(row.get("evidence_id"), str)
    }
    item_ids = {
        row.get("item_id")
        for row in replica_items
        if isinstance(row.get("item_id"), str)
    }
    evidence_links = {
        (row.get("item_id"), row.get("evidence_id"))
        for row in new_item_evidence
        if row.get("role") in {"primary", "supporting"}
    }
    for event in additions:
        event_type = event.get("event_type")
        evidence = evidence_by_id.get(event.get("evidence_id"))
        if event_type not in _REPLICA_EVENT_TYPES:
            raise SyncError(f"replica cannot append unsupported inventory event {event_type!r}")
        if event.get("item_id") not in item_ids:
            raise SyncError("replica inventory event names an unknown item")
        if (
            event.get("context_quality") != "bound"
            or evidence is None
            or evidence.get("captured_on") != event.get("observed_on")
        ):
            raise SyncError("replica inventory event lacks fresh bound evidence")
        if (
            event.get("occurred_on_precision") == "exact"
            and (
                not isinstance(event.get("occurred_on"), str)
                or not isinstance(event.get("observed_on"), str)
                or event["observed_on"] < event["occurred_on"]
            )
        ):
            raise SyncError("replica inventory event was observed before it occurred")
        if (event.get("item_id"), event.get("evidence_id")) not in evidence_links:
            raise SyncError("replica inventory event evidence is not attached to its item")

    latest_exact_by_item: dict[object, str] = {}
    for event in sorted([*base_rows, *additions], key=lambda row: row.get("sequence", -1)):
        if event.get("event_type") not in _REPLICA_EVENT_TYPES:
            continue
        item_id = event.get("item_id")
        if event.get("occurred_on_precision") == "exact":
            occurred_on = event.get("occurred_on")
            previous = latest_exact_by_item.get(item_id)
            if not isinstance(occurred_on, str) or (
                previous is not None and occurred_on < previous
            ):
                raise SyncError("replica lifecycle event chronology moves backwards")
            latest_exact_by_item[item_id] = occurred_on
        else:
            previous = latest_exact_by_item.get(item_id)
            if previous is not None and event.get("observed_on", "") < previous:
                raise SyncError(
                    "replica unknown-date lifecycle fact predates later exact reality"
                )


def _validate_new_item_creation(
    item: Mapping[str, Any],
    *,
    new_events: Iterable[Mapping[str, Any]],
    new_evidence: Iterable[Mapping[str, Any]],
    new_item_evidence: Iterable[Mapping[str, Any]],
    new_item_detail_amendments: Iterable[Mapping[str, Any]],
    locations: Iterable[Mapping[str, Any]],
) -> None:
    """Validate one replica-only item from its replayable creation transaction."""
    item_id = item.get("item_id")
    events = sorted(
        (row for row in new_events if row.get("item_id") == item_id),
        key=lambda row: row.get("sequence", -1),
    )
    evidence_by_id = {
        row.get("evidence_id"): row
        for row in new_evidence
        if isinstance(row.get("evidence_id"), str)
    }
    if not events or events[0].get("event_type") not in {"planned", "ordered", "received"}:
        raise SyncError(f"replica new item {item_id!r} lacks a replayable creation event")
    first = events[0]
    primary_evidence_id = item.get("primary_evidence_id")
    primary = evidence_by_id.get(primary_evidence_id)
    if (
        primary is None
        or first.get("evidence_id") != primary_evidence_id
        or not any(
            row.get("item_id") == item_id
            and row.get("evidence_id") == primary_evidence_id
            and row.get("role") == "primary"
            for row in new_item_evidence
        )
    ):
        raise SyncError(f"replica new item {item_id!r} lacks fresh primary evidence")

    first_type = first.get("event_type")
    detail_history = sorted(
        (
            row
            for row in new_item_detail_amendments
            if row.get("item_id") == item_id
        ),
        key=lambda row: (
            row.get("amended_on", ""),
            row.get("recorded_at", ""),
            row.get("detail_amendment_id", ""),
        ),
    )
    initial_details = {
        field: item.get(field) for field in _ITEM_DETAIL_AMENDMENT_FIELDS
    }
    if detail_history:
        encoded_previous = detail_history[0].get("previous_json")
        try:
            decoded_previous = json.loads(encoded_previous)
        except (TypeError, json.JSONDecodeError):
            decoded_previous = None
        if (
            not isinstance(decoded_previous, dict)
            or set(decoded_previous) != _ITEM_DETAIL_AMENDMENT_FIELDS
        ):
            raise SyncError(f"replica new item {item_id!r} has invalid detail history")
        initial_details = decoded_previous
    always_empty = {
        "home_container_id": initial_details.get("home_container_id"),
        "home_location_id": initial_details.get("home_location_id"),
        "replacement_value": item.get("replacement_value"),
        "value_currency": item.get("value_currency"),
    }
    if any(value is not None for value in always_empty.values()):
        raise SyncError(f"replica new item {item_id!r} has impossible creation values")
    if first_type in {"planned", "ordered"}:
        if (
            primary.get("claim_strength") != "purchase_only"
            or first.get("container_id") is not None
            or (first_type == "planned" and primary.get("evidence_type") != "user_source")
            or any(
                initial_details.get(field) is not None
                for field in (
                    ("acquired_on", "condition", "purchase_currency", "purchase_price", "receipt_ref", "serial_or_lot")
                    if first_type == "planned"
                    else ("acquired_on", "condition", "serial_or_lot")
                )
            )
        ):
            raise SyncError(f"replica new item {item_id!r} has invalid purchase creation")
    elif (
        primary.get("claim_strength") != "explicit_current"
        or primary.get("evidence_type") != "physical_check"
        or any(
            initial_details.get(field) is not None
            for field in ("acquired_on", "purchase_currency", "purchase_price", "receipt_ref")
        )
        or not any(
            row.get("event_type") == "physically_verified"
            and row.get("evidence_id") == primary_evidence_id
            for row in events
        )
    ):
        raise SyncError(f"replica new item {item_id!r} lacks physical creation proof")

    initial_quantity = item.get("quantity")
    initial_unit = item.get("unit")
    for event in events:
        details = _event_details(event)
        if event.get("event_type") in {"planned", "ordered", "quantity_changed"} and details:
            initial_quantity = details.get("previous_quantity")
            initial_unit = details.get("previous_unit")
            break
    replayed = _replay_lifecycle_events(
        events,
        evidence=new_evidence,
        locations=locations,
        item_id=str(item_id),
        before={
            "ownership_state": "unknown",
            "location_id": first.get("location_id"),
            "container_id": first.get("container_id"),
            "quantity": initial_quantity,
            "unit": initial_unit,
            "verified_on": None,
        },
    )
    if replayed is None or any(
        replayed[field] != item.get(field) for field in _LIFECYCLE_PROJECTION_FIELDS
    ):
        raise SyncError(f"replica new item {item_id!r} disagrees with its creation history")


def _location_is_within(
    locations: Mapping[str, Mapping[str, Any]],
    location_id: Any,
    area_location_id: Any,
) -> bool:
    visited: set[Any] = set()
    current = locations.get(location_id) if isinstance(location_id, str) else None
    while current is not None:
        current_id = current.get("location_id")
        if current_id in visited:
            return False
        if current_id == area_location_id:
            return True
        visited.add(current_id)
        parent = current.get("parent_location_id")
        current = locations.get(parent) if isinstance(parent, str) else None
    return False


def _replay_lifecycle_events(
    events: Iterable[Mapping[str, Any]],
    *,
    evidence: Iterable[Mapping[str, Any]],
    locations: Iterable[Mapping[str, Any]],
    item_id: str,
    before: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Replay current CLI lifecycle semantics over one replica branch."""
    item_events = sorted(
        (
            event
            for event in events
            if event.get("item_id") == item_id
        ),
        key=lambda event: event.get("sequence", -1),
    )
    evidence_by_id = {
        row.get("evidence_id"): row
        for row in evidence
        if isinstance(row.get("evidence_id"), str)
    }
    locations_by_id = {
        row.get("location_id"): row
        for row in locations
        if isinstance(row.get("location_id"), str)
    }
    current = {field: before.get(field) for field in _LIFECYCLE_PROJECTION_FIELDS}

    def bound(event: Mapping[str, Any]) -> bool:
        evidence_id = event.get("evidence_id")
        return (
            event.get("context_quality") == "bound"
            and isinstance(evidence_id, str)
            and evidence_id in evidence_by_id
            and evidence_by_id[evidence_id].get("captured_on")
            == event.get("observed_on")
        )

    def set_location(event: Mapping[str, Any]) -> bool:
        if not isinstance(event.get("location_id"), str):
            return False
        current["location_id"] = event.get("location_id")
        current["container_id"] = event.get("container_id")
        return True

    def set_purchase_location(event: Mapping[str, Any]) -> bool:
        """Replay the only two location shapes emitted by plan/order.

        With no explicit location option the commands preserve both fields.  An
        explicit location replaces the stable location and clears the container;
        purchase events cannot assign or move into a container directly.
        """
        location_id = event.get("location_id")
        container_id = event.get("container_id")
        if not isinstance(location_id, str):
            return False
        if (
            location_id == current["location_id"]
            and container_id == current["container_id"]
        ):
            return True
        if container_id is not None:
            return False
        current["location_id"] = location_id
        current["container_id"] = None
        return True

    def replay_quantity(event: Mapping[str, Any]) -> bool:
        details = _event_details(event)
        if details is None or details != {
            "previous_quantity": current["quantity"],
            "previous_unit": current["unit"],
            "quantity": details.get("quantity"),
            "unit": details.get("unit"),
        }:
            return False
        if details.get("quantity") is None or not isinstance(details.get("unit"), str):
            return False
        current["quantity"] = details["quantity"]
        current["unit"] = details["unit"]
        return True

    for event in item_events:
        event_type = event.get("event_type")
        if event_type == "corrected":
            continue
        if not bound(event):
            return None
        state = current["ownership_state"]

        if event_type in {"planned", "ordered"}:
            allowed = {"planned", "unknown"} if event_type == "planned" else {
                "candidate",
                "planned",
                "unknown",
            }
            if state not in allowed or not set_purchase_location(event):
                return None
            current["ownership_state"] = (
                "planned" if event_type == "planned" else "candidate"
            )
            if event.get("details_json") is not None:
                purchase_details = _event_details(event)
                if (
                    purchase_details is None
                    or purchase_details.get("unit") != current["unit"]
                    or purchase_details.get("previous_unit") != current["unit"]
                    or not replay_quantity(event)
                ):
                    return None
            continue

        if event_type == "received":
            if state not in {"candidate", "planned", "unknown"} or not set_location(event):
                return None
            current["ownership_state"] = "confirmed"
            continue

        if event_type == "physically_verified":
            event_evidence = evidence_by_id[event["evidence_id"]]
            occurred_on = event.get("occurred_on")
            observed_on = event.get("observed_on")
            if (
                state not in {"confirmed", "lent"}
                or event.get("occurred_on_precision") != "exact"
                or not isinstance(occurred_on, str)
                or not isinstance(observed_on, str)
                or observed_on < occurred_on
                or event_evidence.get("captured_on") != observed_on
                or event_evidence.get("evidence_type") != "physical_check"
                or event_evidence.get("claim_strength") != "explicit_current"
                or not set_location(event)
            ):
                return None
            current["verified_on"] = occurred_on
            continue

        if event_type == "moved":
            if state not in {"confirmed", "lent"} or not set_location(event):
                return None
            continue

        if event_type == "quantity_changed":
            if (
                state not in {"confirmed", "lent"}
                or event.get("location_id") != current["location_id"]
                or event.get("container_id") != current["container_id"]
                or not replay_quantity(event)
            ):
                return None
            continue

        if event_type == "lent":
            if state != "confirmed" or not set_location(event):
                return None
            current["ownership_state"] = "lent"
            continue

        if event_type == "loan_returned":
            if state != "lent" or not set_location(event):
                return None
            current["ownership_state"] = "confirmed"
            continue

        if event_type in {"reacquired", "ownership_corrected"}:
            if state not in _TERMINAL_STATES or not set_location(event):
                return None
            if event_type == "reacquired" and _reacquisition_declaration(event) is None:
                return None
            current["ownership_state"] = "confirmed"
            continue

        if event_type == "ownership_unresolved":
            if state not in {"candidate", "planned", "confirmed", "lent", "unknown"}:
                return None
            if (
                event.get("location_id") != current["location_id"]
                or event.get("container_id") != current["container_id"]
            ):
                return None
            current["ownership_state"] = "unknown"
            continue

        if event_type == "not_found_in_area":
            area_location_id = event.get("area_location_id")
            within_area = _location_is_within(
                locations_by_id, current["location_id"], area_location_id
            ) or _location_is_within(
                locations_by_id, current["container_id"], area_location_id
            )
            expected_location = "loc-unknown" if within_area else current["location_id"]
            expected_container = None if within_area else current["container_id"]
            if (
                state in _TERMINAL_STATES
                or not isinstance(area_location_id, str)
                or event.get("location_id") != expected_location
                or event.get("container_id") != expected_container
            ):
                return None
            current["location_id"] = expected_location
            current["container_id"] = expected_container
            continue

        terminal = _TERMINAL_EVENT_TRANSITIONS.get(event_type)
        if terminal is not None:
            allowed_states, target_state = terminal
            if (
                state not in allowed_states
                or event.get("location_id") != current["location_id"]
                or event.get("container_id") != current["container_id"]
            ):
                return None
            current["ownership_state"] = target_state
            current["location_id"] = None
            current["container_id"] = None
            continue

        # `ingested` is creation-only and every other event type must have an
        # explicit replay rule before it can authorize a projected row change.
        return None
    return current


def _matching_lifecycle_fields(
    events: Iterable[Mapping[str, Any]],
    *,
    evidence: Iterable[Mapping[str, Any]],
    locations: Iterable[Mapping[str, Any]],
    item_id: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    changed_fields: set[str],
) -> set[str]:
    """Return fields whose final row is the exact result of event replay."""
    replayed = _replay_lifecycle_events(
        events,
        evidence=evidence,
        locations=locations,
        item_id=item_id,
        before=before,
    )
    if replayed is None:
        return set()
    if any(replayed[field] != after.get(field) for field in _LIFECYCLE_PROJECTION_FIELDS):
        return set()
    return set(changed_fields)


def _validate_replica_delta_provenance(
    base_tables: Mapping[str, list[dict[str, Any]]],
    replica_tables: Mapping[str, list[dict[str, Any]]],
) -> None:
    """Reject a bundle that edits canonical state without transaction history.

    The canonical semantic verifier validates a snapshot's internal meaning.
    It cannot tell whether a replica changed a row by using a command or by
    opening JSONL directly.  This delta check closes that gap before merge.
    """
    new_fact_amendments = _new_branch_rows(
        "fact_amendments", base_tables=base_tables, replica_tables=replica_tables
    )
    new_item_amendments = _new_branch_rows(
        "item_amendments", base_tables=base_tables, replica_tables=replica_tables
    )
    new_item_detail_amendments = _new_branch_rows(
        "item_detail_amendments", base_tables=base_tables, replica_tables=replica_tables
    )
    new_events = _new_branch_rows(
        "inventory_events", base_tables=base_tables, replica_tables=replica_tables
    )
    new_evidence = _new_branch_rows(
        "evidence", base_tables=base_tables, replica_tables=replica_tables
    )
    new_item_evidence = _new_branch_rows(
        "item_evidence", base_tables=base_tables, replica_tables=replica_tables
    )
    if _new_branch_rows(
        "sync_receipts", base_tables=base_tables, replica_tables=replica_tables
    ):
        raise SyncError("replica cannot append canonical sync receipts")
    _validate_new_replica_events(
        base_events=base_tables.get("inventory_events", []),
        new_events=new_events,
        new_evidence=new_evidence,
        new_item_evidence=new_item_evidence,
        replica_items=replica_tables.get("items", []),
    )
    base_items = {
        row.get("item_id"): row
        for row in base_tables.get("items", [])
        if isinstance(row.get("item_id"), str)
    }
    for event in new_events:
        if event.get("event_type") != "reacquired":
            continue
        item_id = event.get("item_id")
        before = base_items.get(item_id)
        if before is None:
            continue
        declaration = _reacquisition_declaration(event)
        if declaration is None:
            raise SyncError("replica reacquisition lacks an episode-reset declaration")
        relevant_amendments: list[tuple[Mapping[str, Any], dict, dict, dict]] = []
        for amendment in new_item_detail_amendments:
            if (
                amendment.get("item_id") != item_id
                or amendment.get("evidence_id") != event.get("evidence_id")
                or amendment.get("amended_on") != event.get("occurred_on")
            ):
                continue
            try:
                previous = json.loads(amendment.get("previous_json"))
                changes = json.loads(amendment.get("changes_json"))
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(previous, dict) and isinstance(changes, dict):
                relevant_amendments.append(
                    (amendment, previous, changes, {**previous, **changes})
                )
        has_reset = any(
            changes == {field: None for field in _REACQUISITION_RESET_FIELDS}
            and all(
                resulting.get(field) is None
                for field in _REACQUISITION_RESET_FIELDS
            )
            for _amendment, _previous, changes, resulting in relevant_amendments
        )
        if not has_reset:
            raise SyncError(
                "replica reacquisition lacks an ownership-episode reset artifact"
            )
        declared_condition = declaration.get("condition_checked")
        if declared_condition is not None and not any(
            previous.get("condition") != declared_condition
            and resulting.get("condition") == declared_condition
            for _amendment, previous, _changes, resulting in relevant_amendments
        ):
            raise SyncError("replica reacquisition lacks current condition confirmation")
        declared_quantity = declaration.get("quantity_checked")
        if before.get("quantity") is not None or declared_quantity is not None:
            quantity_confirmed = False
            for candidate in new_events:
                if not (
                    candidate.get("event_type") == "quantity_changed"
                    and candidate.get("item_id") == item_id
                    and candidate.get("evidence_id") == event.get("evidence_id")
                    and candidate.get("occurred_on") == event.get("occurred_on")
                    and candidate.get("sequence", -1) > event.get("sequence", -1)
                ):
                    continue
                quantity_details = _event_details(candidate)
                if (
                    quantity_details is not None
                    and quantity_details.get("quantity") == declared_quantity
                    and quantity_details.get("unit") == declaration.get("unit")
                ):
                    quantity_confirmed = True
                    break
            if not quantity_confirmed:
                raise SyncError(
                    "replica reacquisition lacks current quantity confirmation"
                )

    base_item_ids = {
        row.get("item_id")
        for row in base_tables.get("items", [])
        if isinstance(row.get("item_id"), str)
    }
    for item in replica_tables.get("items", []):
        if item.get("item_id") not in base_item_ids:
            _validate_new_item_creation(
                item,
                new_events=new_events,
                new_evidence=new_evidence,
                new_item_evidence=new_item_evidence,
                new_item_detail_amendments=new_item_detail_amendments,
                locations=replica_tables.get("locations", []),
            )

    for table in sorted(base_tables):
        base_rows = _row_map(table, base_tables[table])
        replica_rows = _row_map(table, replica_tables[table])
        for identity, before in base_rows.items():
            after = replica_rows.get(identity, _MISSING)
            if not _changed(before, after):
                continue
            identity_values = tuple(json.loads(identity))
            if table in _IMMUTABLE_REPLICA_BASE_TABLES:
                raise SyncError(
                    f"replica cannot alter immutable {table} base row {identity_values!r}"
                )
            if table in _APPEND_ONLY_TABLES:
                # The existing conflict machinery retains the base audit row
                # and makes its only safe resolution explicit.
                continue
            if table == "items":
                if after is _MISSING:
                    raise SyncError(f"replica cannot delete current item {identity_values!r}")
                changed_fields = {
                    field
                    for field in set(before) | set(after)
                    if before.get(field) != after.get(field)
                }
                item_id = identity_values[0]
                if "model_id" in changed_fields and not _has_matching_item_amendment(
                    new_item_amendments, item_id=item_id, before=before, after=after
                ):
                    raise SyncError(
                        f"replica item model change for {item_id!r} lacks matching item_amendment"
                    )
                detail_fields = changed_fields - _ITEM_LIFECYCLE_FIELDS - {"model_id"}
                if detail_fields and not _has_matching_item_detail_amendment(
                    new_item_detail_amendments,
                    item_id=item_id,
                    before=before,
                    after=after,
                    changed_fields=detail_fields,
                ):
                    raise SyncError(
                        f"replica item detail change for {item_id!r} lacks matching item_detail_amendment"
                    )
                lifecycle_fields = changed_fields & _ITEM_LIFECYCLE_FIELDS
                covered_lifecycle_fields = _matching_lifecycle_fields(
                    new_events,
                    evidence=new_evidence,
                    locations=replica_tables.get("locations", []),
                    item_id=item_id,
                    before=before,
                    after=after,
                    changed_fields=lifecycle_fields,
                )
                if lifecycle_fields - covered_lifecycle_fields:
                    missing = ", ".join(sorted(lifecycle_fields - covered_lifecycle_fields))
                    raise SyncError(
                        f"replica item lifecycle change for {item_id!r} lacks exact "
                        f"inventory_event provenance for: {missing}"
                    )
                continue
            if table in _FACT_AMENDMENT_TABLES and not _has_matching_fact_amendment(
                new_fact_amendments,
                table=table,
                identity=identity_values,
                before=before,
                after=after,
            ):
                raise SyncError(
                    f"replica {table} change for {identity_values!r} lacks matching fact_amendment"
                )


def _apply_fact_amendment_branch_choices(
    tables: dict[str, list[dict[str, Any]]],
    *,
    conflicts: Iterable[Mapping[str, Any]],
    selected: Mapping[str, str],
    base_tables: Mapping[str, list[dict[str, Any]]],
    canonical_tables: Mapping[str, list[dict[str, Any]]],
    replica_tables: Mapping[str, list[dict[str, Any]]],
) -> None:
    """Keep the selected branch's complete chain for each conflicted mutable fact.

    `fact_amendments` is append-only within a branch, but two divergent branches
    cannot both be replayed for the same fact.  Their predecessor snapshots each
    point to the common base, so unioning them creates an impossible chain.
    Resolution therefore retains the complete chain from the chosen branch for
    that selector and preserves every unrelated amendment from the merged heads.
    """
    targets: list[tuple[str, tuple[Any, ...], str]] = []
    for conflict in conflicts:
        table = conflict.get("table")
        identity = conflict.get("identity")
        conflict_id = conflict.get("conflict_id")
        if (
            not isinstance(table, str)
            or table == "fact_amendments"
            or table not in _IDENTITY_FIELDS
            or not isinstance(identity, list)
            or not isinstance(conflict_id, str)
            or conflict_id not in selected
        ):
            continue
        fields = _IDENTITY_FIELDS[table]
        if len(identity) != len(fields):
            raise SyncError("fact conflict identity is malformed")
        targets.append((table, tuple(identity), selected[conflict_id]))
    if not targets:
        return
    if "fact_amendments" not in tables:
        return

    def belongs_to_target(row: Mapping[str, Any]) -> bool:
        return any(
            _fact_amendment_selector_matches(row, table=table, identity=identity)
            for table, identity, _ in targets
        )

    retained = [row for row in tables["fact_amendments"] if not belongs_to_target(row)]
    branches = {
        "base": base_tables,
        "canonical": canonical_tables,
        "replica": replica_tables,
    }
    for table, identity, choice in targets:
        selected_rows = branches[choice].get("fact_amendments", [])
        retained.extend(
            row
            for row in selected_rows
            if _fact_amendment_selector_matches(row, table=table, identity=identity)
        )
    tables["fact_amendments"] = retained


def _apply_item_branch_choices(
    tables: dict[str, list[dict[str, Any]]],
    *,
    conflicts: Iterable[Mapping[str, Any]],
    selected: Mapping[str, str],
    base_tables: Mapping[str, list[dict[str, Any]]],
    canonical_tables: Mapping[str, list[dict[str, Any]]],
    replica_tables: Mapping[str, list[dict[str, Any]]],
) -> None:
    """Keep one coherent item projection history for every selected item row.

    An item conflict is a branch conflict, not just a row conflict.  Retaining
    the losing lifecycle or amendment chain makes the selected projection
    unverifiable, so the chosen branch owns all item-projection history.
    """
    targets: list[tuple[str, str]] = []
    for conflict in conflicts:
        identity = conflict.get("identity")
        conflict_id = conflict.get("conflict_id")
        if (
            conflict.get("table") == "items"
            and isinstance(identity, list)
            and len(identity) == 1
            and isinstance(identity[0], str)
            and isinstance(conflict_id, str)
            and conflict_id in selected
        ):
            targets.append((identity[0], selected[conflict_id]))
    if not targets:
        return

    branches = {
        "base": base_tables,
        "canonical": canonical_tables,
        "replica": replica_tables,
    }
    history_tables = (
        "inventory_events",
        "item_amendments",
        "item_detail_amendments",
        "item_evidence",
    )
    independent_evidence_ids = {
        value
        for table, rows in tables.items()
        if table not in {*history_tables, "evidence", "evidence_assets", "media_assets"}
        for row in rows
        for key, value in row.items()
        if key in {"evidence_id", "primary_evidence_id"} and isinstance(value, str)
    }
    dropped_evidence_ids: set[str] = set()
    for item_id, choice in targets:
        lifecycle_evidence_ids = {
            row["evidence_id"]
            for table in ("inventory_events", "item_amendments", "item_detail_amendments")
            for row in branches["replica"].get(table, [])
            if row.get("item_id") == item_id and isinstance(row.get("evidence_id"), str)
        }
        for table in history_tables:
            if table not in tables:
                continue
            retained_other_items = [
                row for row in tables[table] if row.get("item_id") != item_id
            ]
            selected_rows = [
                row
                for row in branches[choice].get(table, [])
                if row.get("item_id") == item_id
            ]
            selected_identities = {
                _identity_json(_identity(table, row)) for row in selected_rows
            }
            if table == "item_evidence":
                # A relation/fact can remain valid on the selected item branch
                # while relying on a replica-created supporting-evidence link.
                # Preserve only that exact independent support row; never retain
                # lifecycle history merely because it shares an item id.
                selected_rows.extend(
                    row
                    for row in tables[table]
                    if row.get("item_id") == item_id
                    and (
                        row.get("evidence_id") in independent_evidence_ids
                        or row.get("evidence_id") not in lifecycle_evidence_ids
                    )
                    and _identity_json(_identity(table, row)) not in selected_identities
                )
            for row in tables[table]:
                if (
                    row.get("item_id") == item_id
                    and _identity_json(_identity(table, row)) not in selected_identities
                    and isinstance(row.get("evidence_id"), str)
                ):
                    dropped_evidence_ids.add(row["evidence_id"])
            tables[table] = [*retained_other_items, *selected_rows]

    if not dropped_evidence_ids or "evidence" not in tables:
        return

    def has_semantic_reference(evidence_id: str) -> bool:
        return any(
            value == evidence_id
            for table, rows in tables.items()
            if table not in {"evidence", "evidence_assets"}
            for row in rows
            for key, value in row.items()
            if key in {"evidence_id", "primary_evidence_id"}
        )

    orphaned = {
        evidence_id
        for evidence_id in dropped_evidence_ids
        if not has_semantic_reference(evidence_id)
    }
    if not orphaned:
        return
    tables["evidence"] = [
        row for row in tables["evidence"] if row.get("evidence_id") not in orphaned
    ]
    if "evidence_assets" in tables:
        tables["evidence_assets"] = [
            row
            for row in tables["evidence_assets"]
            if row.get("evidence_id") not in orphaned
        ]
    if "media_assets" not in tables:
        return

    # A rejected branch may have introduced immutable media before its item
    # history was discarded.  Remove only assets demonstrably private to that
    # rejected replica branch.  Base and canonical bytes are retained even when
    # currently unlinked: they predate this conflict and are not ours to prune.
    protected_asset_ids = {
        row.get("asset_id")
        for branch in (base_tables, canonical_tables)
        for row in branch.get("media_assets", [])
        if isinstance(row.get("asset_id"), str)
    }
    protected_digests = {
        row.get("sha256")
        for branch in (base_tables, canonical_tables)
        for row in branch.get("media_assets", [])
        if isinstance(row.get("sha256"), str)
    }
    replica_assets = {
        row.get("asset_id"): row
        for row in replica_tables.get("media_assets", [])
        if isinstance(row.get("asset_id"), str)
    }
    linked_asset_ids = {
        row.get("asset_id")
        for row in tables.get("evidence_assets", [])
        if isinstance(row.get("asset_id"), str)
    }
    removable_asset_ids: set[str] = set()
    for asset in tables["media_assets"]:
        asset_id, digest = asset.get("asset_id"), asset.get("sha256")
        if not isinstance(asset_id, str) or not isinstance(digest, str):
            raise SyncError("merged media asset is malformed")
        if asset_id in protected_asset_ids or digest in protected_digests:
            continue
        replica = replica_assets.get(asset_id)
        if replica is None:
            raise SyncError("unowned merged media asset blocks rejected-branch pruning")
        if _canonical_json(replica) != _canonical_json(asset):
            raise SyncError("replica media asset changed during rejected-branch pruning")
        if asset_id not in linked_asset_ids:
            removable_asset_ids.add(asset_id)
    if removable_asset_ids:
        tables["media_assets"] = [
            row for row in tables["media_assets"] if row.get("asset_id") not in removable_asset_ids
        ]


def _prune_rejected_replica_artifacts(
    tables: dict[str, list[dict[str, Any]]],
    *,
    base_tables: Mapping[str, list[dict[str, Any]]],
    canonical_tables: Mapping[str, list[dict[str, Any]]],
    replica_tables: Mapping[str, list[dict[str, Any]]],
    rejected_evidence_ids: set[str],
) -> None:
    """Remove only rejected replica artifacts no surviving fact can still reach."""
    branches = (base_tables, canonical_tables)
    protected_evidence = {
        row.get("evidence_id")
        for branch in branches for row in branch.get("evidence", [])
        if isinstance(row.get("evidence_id"), str)
    }
    replica_evidence = {
        row.get("evidence_id"): row for row in replica_tables.get("evidence", [])
        if isinstance(row.get("evidence_id"), str)
    }
    referenced_evidence = {
        value
        for table, rows in tables.items()
        if table not in {"evidence", "evidence_assets"}
        for row in rows for key, value in row.items()
        if key in {"evidence_id", "primary_evidence_id"} and isinstance(value, str)
    }
    retained_evidence: list[dict[str, Any]] = []
    for row in tables.get("evidence", []):
        evidence_id = row.get("evidence_id")
        if not isinstance(evidence_id, str):
            raise SyncError("merged evidence is malformed")
        if (
            evidence_id in protected_evidence
            or evidence_id in referenced_evidence
            or evidence_id not in rejected_evidence_ids
        ):
            retained_evidence.append(row)
            continue
        if evidence_id not in replica_evidence or _canonical_json(replica_evidence[evidence_id]) != _canonical_json(row):
            raise SyncError("unowned rejected evidence blocks artifact pruning")
    tables["evidence"] = retained_evidence
    retained_evidence_ids = {row["evidence_id"] for row in retained_evidence}
    tables["evidence_assets"] = [
        row for row in tables.get("evidence_assets", [])
        if row.get("evidence_id") in retained_evidence_ids
    ]
    protected_asset_ids = {
        row.get("asset_id")
        for branch in branches for row in branch.get("media_assets", [])
        if isinstance(row.get("asset_id"), str)
    }
    protected_digests = {
        row.get("sha256")
        for branch in branches for row in branch.get("media_assets", [])
        if isinstance(row.get("sha256"), str)
    }
    replica_assets = {
        row.get("asset_id"): row for row in replica_tables.get("media_assets", [])
        if isinstance(row.get("asset_id"), str)
    }
    linked_assets = {
        row.get("asset_id") for row in tables["evidence_assets"] if isinstance(row.get("asset_id"), str)
    }
    retained_assets: list[dict[str, Any]] = []
    for row in tables.get("media_assets", []):
        asset_id, digest = row.get("asset_id"), row.get("sha256")
        if not isinstance(asset_id, str) or not isinstance(digest, str):
            raise SyncError("merged media asset is malformed")
        if asset_id in protected_asset_ids or digest in protected_digests or asset_id in linked_assets:
            retained_assets.append(row)
            continue
        if asset_id not in replica_assets or _canonical_json(replica_assets[asset_id]) != _canonical_json(row):
            raise SyncError("unowned rejected media blocks artifact pruning")
    tables["media_assets"] = retained_assets


def _ready_plan(
    *, inventory_id: str, replica_ref: str, base_digest: str, canonical_head_digest: str,
    replica_head_digest: str, bundle_digest: str, tables: Mapping[str, Iterable[Mapping[str, Any]]],
    conflicts: list[dict[str, Any]],
    event_sequence_rewrites: list[dict[str, Any]],
    resolutions: list[dict[str, str]] | None = None,
    format: int = _PLAN_FORMAT,
) -> dict[str, Any]:
    merged = _normalise_tables(tables)
    selected = sorted(resolutions or [], key=lambda row: row["conflict_id"])
    merged_digest = _snapshot(merged)["digest"]
    # This is data for the canonical transaction layer, never a JSONL write instruction.
    plan: dict[str, Any] = {
        "format": format,
        "status": "ready",
        "inventory_id": inventory_id,
        "replica_ref": replica_ref,
        "base_digest": base_digest,
        "canonical_head_digest": canonical_head_digest,
        "replica_head_digest": replica_head_digest,
        "bundle_digest": bundle_digest,
        "conflicts": conflicts,
        "event_sequence_rewrites": event_sequence_rewrites,
        "merged_digest": merged_digest,
        "resolutions": selected,
        "tables": merged,
    }
    application_digest = _application_digest(
        format=format,
        inventory_id=inventory_id,
        replica_ref=replica_ref,
        base_digest=base_digest,
        bundle_digest=bundle_digest,
        canonical_head_digest=canonical_head_digest,
        replica_head_digest=replica_head_digest,
        resolutions=selected,
        event_sequence_rewrites=event_sequence_rewrites,
        tables=merged,
    )
    plan["application_digest"] = application_digest
    # The receipt id binds the transported replica, canonical head, explicit
    # choices, sequence rewrites and exact merged tables. payload_digest retains
    # the raw replica-head identity so competing applications are detectable.
    receipt = _receipt_for_application(
        replica_ref=replica_ref,
        replica_head_digest=replica_head_digest,
        application_digest=application_digest,
    )
    plan["receipt"] = receipt
    plan["plan_digest"] = _plan_digest(plan)
    return plan


def plan_three_way_merge(
    *, base: Mapping[str, Iterable[Mapping[str, Any]]], canonical_head: Mapping[str, Iterable[Mapping[str, Any]]],
    bundle: Mapping[str, Any],
    merged_store_validator: Callable[[Mapping[str, list[dict[str, Any]]]], None] | None = None,
) -> dict[str, Any]:
    """Create a write-free merge plan, refusing stale bases and competing row edits."""
    verified = verify_replica_bundle(bundle)
    base_snapshot = _snapshot(base)
    canonical_snapshot = _snapshot(canonical_head)
    if set(base_snapshot["tables"]) != set(canonical_snapshot["tables"]):
        raise SyncError("canonical base and head must have the same canonical table set")
    _assert_inventory_identity(base_snapshot["tables"], verified["inventory_id"], "canonical base")
    _assert_inventory_identity(
        canonical_snapshot["tables"], verified["inventory_id"], "canonical head"
    )
    if base_snapshot["digest"] != verified["base"]["digest"]:
        raise SyncError("replica bundle base is stale or belongs to another canonical generation")
    if set(base_snapshot["tables"]) != set(verified["head"]["tables"]):
        raise SyncError("replica bundle table set does not match canonical base")
    _validate_replica_delta_provenance(base_snapshot["tables"], verified["head"]["tables"])

    event_rebase_items: set[str] = set()
    base_items = _row_map("items", base_snapshot["tables"].get("items", []))
    canonical_items = _row_map("items", canonical_snapshot["tables"].get("items", []))
    replica_items = _row_map("items", verified["head"]["tables"].get("items", []))
    item_conflict_owned = {
        item_id
        for identity, before in base_items.items()
        if isinstance((item_id := before.get("item_id")), str)
        and (canonical_item := canonical_items.get(identity)) is not None
        and (replica_item := replica_items.get(identity)) is not None
        and _changed(before, canonical_item)
        and _changed(before, replica_item)
        and _changed(canonical_item, replica_item)
    }
    base_events = _row_map("inventory_events", base_snapshot["tables"].get("inventory_events", []))
    canonical_new_event_items = {
        row["item_id"]
        for identity, row in _row_map(
            "inventory_events", canonical_snapshot["tables"].get("inventory_events", [])
        ).items()
        if (
            identity not in base_events
            and isinstance(row.get("item_id"), str)
            and isinstance(row.get("event_type"), str)
        )
    }
    remote_events = [
        row for identity, row in _row_map("inventory_events", verified["head"]["tables"].get("inventory_events", [])).items()
        if identity not in base_events
    ]
    combined_evidence = [
        *canonical_snapshot["tables"].get("evidence", []),
        *[
            row for row in verified["head"]["tables"].get("evidence", [])
            if row.get("evidence_id") not in {entry.get("evidence_id") for entry in canonical_snapshot["tables"].get("evidence", [])}
        ],
    ]
    for item_id in {row.get("item_id") for row in remote_events if isinstance(row.get("item_id"), str)}:
        identity = _identity_json((item_id,))
        before_item, canonical_item = base_items.get(identity), canonical_items.get(identity)
        if (
            before_item is None
            or canonical_item is None
            or item_id in item_conflict_owned
        ):
            continue
        replayed = _replay_lifecycle_events(
            [row for row in remote_events if row.get("item_id") == item_id],
            evidence=combined_evidence,
            locations=canonical_snapshot["tables"].get("locations", []),
            item_id=item_id,
            before=canonical_item,
        )
        canonical_projection_changed = _changed(before_item, canonical_item)
        replay_disagrees = replayed is None or any(
            replayed[field] != canonical_item.get(field)
            for field in _LIFECYCLE_PROJECTION_FIELDS
        )
        # A canonical lifecycle append has already claimed a position after the
        # trusted base. Replica events would be resequenced after it during
        # application, which can invert their observed chronology even when
        # the item projection later returns to its base value (an ABA history).
        if item_id in canonical_new_event_items or (
            canonical_projection_changed and replay_disagrees
        ):
            event_rebase_items.add(item_id)

    merged_maps: dict[str, dict[str, dict[str, Any]]] = {}
    conflicts: list[dict[str, Any]] = []
    for table in sorted(base_snapshot["tables"]):
        base_rows = _row_map(table, base_snapshot["tables"][table])
        canonical_rows = _row_map(table, canonical_snapshot["tables"][table])
        replica_rows = _row_map(table, verified["head"]["tables"][table])
        chosen: dict[str, dict[str, Any]] = {}
        for identity in sorted(set(base_rows) | set(canonical_rows) | set(replica_rows)):
            before = base_rows.get(identity, _MISSING)
            local = canonical_rows.get(identity, _MISSING)
            remote = replica_rows.get(identity, _MISSING)
            local_changed, remote_changed = _changed(before, local), _changed(before, remote)
            if (
                table == "inventory_events" and before is _MISSING and remote is not _MISSING
                and remote.get("item_id") in event_rebase_items
            ):
                conflicts.append({
                    "conflict_id": _conflict_id(table, identity, before, local, remote),
                    "table": table, "identity": json.loads(identity),
                    "kind": "identity_collision_requires_rebase", "semantic_fields": [],
                    "base": None, "canonical": _external_row(local), "replica": _external_row(remote),
                    "choices": [], "reconciliation_required": "replay the replica event against the current canonical item, then prepare a new bundle",
                })
                continue
            if table in _APPEND_ONLY_TABLES and before is not _MISSING and (
                local_changed or remote_changed
            ):
                # Canonical audit ledgers are append-only on every branch. A
                # base row cannot be deleted or altered by accepting either
                # head, even when both heads make the same alteration.
                conflict = {
                    "conflict_id": _conflict_id(table, identity, before, local, remote),
                    "table": table,
                    "identity": json.loads(identity),
                    "kind": (
                        "inventory_event_append_only_violation"
                        if table == "inventory_events"
                        else f"{table}_append_only_violation"
                    ),
                    "semantic_fields": [],
                    "base": _external_row(before),
                    "canonical": _external_row(local),
                    "replica": _external_row(remote),
                    "choices": ["base"],
                }
                conflicts.append(conflict)
                continue
            if (
                table in _APPEND_ONLY_TABLES
                and before is _MISSING
                and local is not _MISSING
                and remote is not _MISSING
                and _changed(local, remote)
            ):
                # Two branches may append the same immutable identity only
                # when their bytes agree. The canonical head wins a collision;
                # accepting replica content would rewrite a canonical append.
                conflict = {
                    "conflict_id": _conflict_id(table, identity, before, local, remote),
                    "table": table,
                    "identity": json.loads(identity),
                    "kind": f"{table}_append_only_identity_collision",
                    "semantic_fields": [],
                    "base": None,
                    "canonical": _external_row(local),
                    "replica": _external_row(remote),
                    "choices": ["canonical"],
                }
                conflicts.append(conflict)
                continue
            if (
                before is not _MISSING
                and local is not _MISSING
                and remote is _MISSING
                and not local_changed
            ):
                conflict = {
                    "conflict_id": _conflict_id(table, identity, before, local, remote),
                    "table": table,
                    "identity": json.loads(identity),
                    "kind": "replica_deletion_requires_reconciliation",
                    "semantic_fields": [],
                    "base": _external_row(before),
                    "canonical": _external_row(local),
                    "replica": _external_row(remote),
                    "choices": ["canonical"],
                    "reconciliation_required": (
                        "accept canonical history, then re-record any still-valid replica intent "
                        "as a fresh canonical transaction"
                    ),
                }
                conflicts.append(conflict)
                continue
            if not local_changed:
                result = remote
            elif not remote_changed or not _changed(local, remote):
                result = local
            else:
                semantic_fields = _semantic_fields(table, local, remote)
                canonical_history_owned = (
                    table in _FACT_AMENDMENT_TABLES
                    and (before is not _MISSING or local is not _MISSING)
                ) or (
                    table == "items"
                    and local is not _MISSING
                    and remote is not _MISSING
                ) or (
                    table in _IMMUTABLE_REPLICA_BASE_TABLES
                    and before is _MISSING
                    and local is not _MISSING
                    and remote is not _MISSING
                )
                dependent_replica_rows = (
                    _replica_dependency_rows(
                        parent_table=table,
                        parent_identity=identity,
                        base_tables=base_snapshot["tables"],
                        replica_tables=verified["head"]["tables"],
                    )
                    if canonical_history_owned
                    and (table != "items" or before is _MISSING)
                    else []
                )
                if table == "items" and before is _MISSING:
                    dependent_replica_rows = [
                        row
                        for row in dependent_replica_rows
                        if row["table"]
                        not in {
                            "inventory_events",
                            "item_amendments",
                            "item_detail_amendments",
                            "item_evidence",
                        }
                    ]
                elif table == "items" and before is not _MISSING and isinstance(before.get("item_id"), str):
                    dependent_replica_rows = _item_conflict_dependency_rows(
                        item_id=before["item_id"],
                        before=before,
                        replica=remote,
                        base_tables=base_snapshot["tables"],
                        replica_tables=verified["head"]["tables"],
                    )
                conflict = {
                    "conflict_id": _conflict_id(table, identity, before, local, remote),
                    "table": table,
                    "identity": json.loads(identity),
                    "kind": (
                        "identity_collision_requires_rebase"
                        if dependent_replica_rows
                        else "canonical_history_conflict"
                        if canonical_history_owned
                        else "semantic_conflict"
                        if semantic_fields
                        else "divergent_row_content"
                    ),
                    "semantic_fields": semantic_fields,
                    "base": _external_row(before),
                    "canonical": _external_row(local),
                    "replica": _external_row(remote),
                    "choices": (
                        []
                        if dependent_replica_rows
                        else ["canonical"]
                        if canonical_history_owned
                        else ["canonical", "replica", "base"]
                    ),
                }
                if dependent_replica_rows:
                    conflict["dependent_replica_rows"] = dependent_replica_rows
                    conflict["reconciliation_required"] = (
                        "re-ID the replica transaction against the canonical identity, then "
                        "prepare a new bundle"
                    )
                elif canonical_history_owned:
                    conflict["reconciliation_required"] = (
                        "accept canonical history, then re-record any still-valid replica intent "
                        "as a fresh canonical transaction"
                    )
                conflicts.append(conflict)
                continue
            if result is not _MISSING:
                chosen[identity] = result
        merged_maps[table] = chosen

    common = {
        "format": _PLAN_FORMAT,
        "inventory_id": verified["inventory_id"],
        "replica_ref": verified["replica_ref"],
        "base_digest": base_snapshot["digest"],
        "canonical_head_digest": canonical_snapshot["digest"],
        "replica_head_digest": verified["head"]["digest"],
        "bundle_digest": verified["bundle_digest"],
    }
    if conflicts:
        plan = {
            **common,
            "status": "needs_resolution",
            "conflicts": sorted(conflicts, key=lambda item: item["conflict_id"]),
            # These are snapshots, not write instructions.  They make a crash-safe
            # resolution retry self-contained while their enclosing plan digest
            # makes any edit fail closed.
            "base_tables": base_snapshot["tables"],
            "canonical_tables": canonical_snapshot["tables"],
            "replica_tables": verified["head"]["tables"],
        }
        plan["plan_digest"] = _plan_digest(plan)
        return plan
    merged = {
        table: list(rows.values())
        for table, rows in merged_maps.items()
    }
    merged = _normalise_tables(merged, validate_event_sequences=False)
    rewrites = _resequence_remote_event_additions(
        merged,
        base_tables=base_snapshot["tables"],
        canonical_tables=canonical_snapshot["tables"],
    )
    _validate_merged_tables(merged, merged_store_validator)
    return _ready_plan(
        **common, tables=merged, conflicts=[], event_sequence_rewrites=rewrites
    )


def _verify_plan(plan: Mapping[str, Any]) -> None:
    if not isinstance(plan, Mapping) or not isinstance(plan.get("plan_digest"), str):
        raise SyncError("sync plan is malformed")
    if isinstance(plan.get("format"), bool) or plan.get("format") != _PLAN_FORMAT:
        raise SyncError("unsupported sync plan format")
    if not isinstance(plan.get("inventory_id"), str) or not plan["inventory_id"]:
        raise SyncError("sync plan inventory_id is malformed")
    if plan["plan_digest"] != _plan_digest(plan):
        raise SyncError("sync plan digest mismatch")


def resolve_conflicts(
    plan: Mapping[str, Any],
    resolutions: Mapping[str, str],
    merged_store_validator: Callable[[Mapping[str, list[dict[str, Any]]]], None] | None = None,
) -> dict[str, Any]:
    """Turn every explicit conflict choice into a deterministic ready-to-apply data plan."""
    _verify_plan(plan)
    if plan.get("status") != "needs_resolution" or not isinstance(plan.get("conflicts"), list):
        raise SyncError("only a conflict plan can be resolved")
    if not isinstance(resolutions, Mapping):
        raise SyncError("resolutions must map conflict ids to explicit choices")
    conflicts = plan["conflicts"]
    expected = {conflict.get("conflict_id") for conflict in conflicts}
    if None in expected or set(resolutions) != expected:
        raise SyncError("resolutions must choose exactly once for every conflict")
    selected: dict[str, str] = {}
    for conflict in conflicts:
        choice = resolutions[conflict["conflict_id"]]
        if choice not in conflict.get("choices", ()):
            raise SyncError(f"invalid resolution for {conflict['conflict_id']}")
        selected[conflict["conflict_id"]] = choice

    # Rebuild the non-conflicting state from the digest-bound snapshot material.
    base_tables = plan.get("base_tables")
    canonical_tables = plan.get("canonical_tables")
    replica_tables = plan.get("replica_tables")
    if not all(isinstance(value, Mapping) for value in (base_tables, canonical_tables, replica_tables)):
        raise SyncError("conflict plan lacks resolution material")
    rebuilt_bundle = build_replica_bundle(
        inventory_id=plan["inventory_id"],
        replica_ref=plan["replica_ref"],
        base=base_tables,
        head=replica_tables,
    )
    rebuilt = plan_three_way_merge(base=base_tables, canonical_head=canonical_tables, bundle=rebuilt_bundle)
    if rebuilt.get("status") != "needs_resolution" or rebuilt.get("plan_digest") != plan["plan_digest"]:
        raise SyncError("conflict plan resolution material does not match its digest")
    normalised_base = _normalise_tables(base_tables)
    normalised_canonical = _normalise_tables(canonical_tables)
    normalised_replica = _normalise_tables(replica_tables)
    merged: dict[str, dict[str, dict[str, Any]]] = {}
    for table in sorted(normalised_base):
        base_rows = _row_map(table, normalised_base[table])
        canonical_rows = _row_map(table, normalised_canonical[table])
        replica_rows = _row_map(table, normalised_replica[table])
        chosen: dict[str, dict[str, Any]] = {}
        for identity in sorted(set(base_rows) | set(canonical_rows) | set(replica_rows)):
            before = base_rows.get(identity, _MISSING)
            local = canonical_rows.get(identity, _MISSING)
            remote = replica_rows.get(identity, _MISSING)
            local_changed, remote_changed = _changed(before, local), _changed(before, remote)
            append_only_violation = table in _APPEND_ONLY_TABLES and before is not _MISSING and (
                local_changed or remote_changed
            )
            append_only_collision = (
                table in _APPEND_ONLY_TABLES
                and before is _MISSING
                and local is not _MISSING
                and remote is not _MISSING
                and _changed(local, remote)
            )
            special_conflict = append_only_violation or append_only_collision or (
                before is not _MISSING
                and local is not _MISSING
                and remote is _MISSING
                and not local_changed
            )
            if special_conflict:
                conflict_id = _conflict_id(table, identity, before, local, remote)
                if append_only_violation:
                    result = {"base": before}[selected[conflict_id]]
                elif append_only_collision:
                    result = {"canonical": local}[selected[conflict_id]]
                else:
                    result = {
                        "canonical": local,
                    }[selected[conflict_id]]
            elif not local_changed:
                result = remote
            elif not remote_changed or not _changed(local, remote):
                result = local
            else:
                conflict_id = _conflict_id(table, identity, before, local, remote)
                result = {"canonical": local, "replica": remote, "base": before}[selected[conflict_id]]
            if result is not _MISSING:
                chosen[identity] = result
        merged[table] = chosen
    resolved_tables = {table: list(rows.values()) for table, rows in merged.items()}
    _apply_item_branch_choices(
        resolved_tables,
        conflicts=conflicts,
        selected=selected,
        base_tables=normalised_base,
        canonical_tables=normalised_canonical,
        replica_tables=normalised_replica,
    )
    _apply_fact_amendment_branch_choices(
        resolved_tables,
        conflicts=conflicts,
        selected=selected,
        base_tables=normalised_base,
        canonical_tables=normalised_canonical,
        replica_tables=normalised_replica,
    )
    rejected_evidence_ids: set[str] = set()
    for conflict in conflicts:
        if selected.get(conflict.get("conflict_id")) != "canonical":
            continue
        if conflict.get("table") == "items" and isinstance(conflict.get("identity"), list):
            item_id = conflict["identity"][0] if len(conflict["identity"]) == 1 else None
            if isinstance(item_id, str):
                rejected_evidence_ids.update(
                    row["evidence_id"]
                    for table in ("inventory_events", "item_amendments", "item_detail_amendments")
                    for row in normalised_replica.get(table, [])
                    if row.get("item_id") == item_id and isinstance(row.get("evidence_id"), str)
                )
        elif conflict.get("table") in _FACT_AMENDMENT_TABLES and isinstance(conflict.get("identity"), list):
            identity = tuple(conflict["identity"])
            rejected_evidence_ids.update(
                row["evidence_id"] for row in normalised_replica.get("fact_amendments", [])
                if _fact_amendment_selector_matches(row, table=conflict["table"], identity=identity)
                and isinstance(row.get("evidence_id"), str)
            )
    _prune_rejected_replica_artifacts(
        resolved_tables,
        base_tables=normalised_base,
        canonical_tables=normalised_canonical,
        replica_tables=normalised_replica,
        rejected_evidence_ids=rejected_evidence_ids,
    )
    ready_tables = _normalise_tables(resolved_tables, validate_event_sequences=False)
    rewrites = _resequence_remote_event_additions(
        ready_tables,
        base_tables=normalised_base,
        canonical_tables=normalised_canonical,
    )
    _validate_merged_tables(ready_tables, merged_store_validator)
    resolution_rows = [
        {"conflict_id": conflict_id, "choice": selected[conflict_id]}
        for conflict_id in sorted(selected)
    ]
    ready = _ready_plan(
        inventory_id=plan["inventory_id"],
        replica_ref=plan["replica_ref"],
        base_digest=plan["base_digest"],
        canonical_head_digest=plan["canonical_head_digest"],
        replica_head_digest=plan["replica_head_digest"],
        bundle_digest=plan["bundle_digest"],
        tables=ready_tables,
        conflicts=[],
        event_sequence_rewrites=rewrites,
        resolutions=resolution_rows,
    )
    return ready


def receipt_data(plan: Mapping[str, Any]) -> dict[str, str]:
    """Return the stable receipt key to record after a caller durably applies a ready plan."""
    _verify_plan(plan)
    if plan.get("status") != "ready" or not isinstance(plan.get("receipt"), Mapping):
        raise SyncError("a receipt exists only for a ready sync plan")
    receipt = plan["receipt"]
    if set(receipt) != {"sync_receipt_id", "replica_ref", "payload_digest"}:
        raise SyncError("sync receipt is malformed")
    if not all(isinstance(receipt[field], str) and receipt[field] for field in receipt):
        raise SyncError("sync receipt is malformed")
    resolutions = plan.get("resolutions")
    if not isinstance(resolutions, list) or any(
        not isinstance(row, dict)
        or set(row) != {"conflict_id", "choice"}
        or not isinstance(row["conflict_id"], str)
        or not row["conflict_id"]
        or not isinstance(row["choice"], str)
        or not row["choice"]
        for row in resolutions
    ):
        raise SyncError("sync plan resolutions are malformed")
    if resolutions != sorted(resolutions, key=lambda row: row["conflict_id"]):
        raise SyncError("sync plan resolutions are not canonical")
    if len({row["conflict_id"] for row in resolutions}) != len(resolutions):
        raise SyncError("sync plan resolutions contain duplicate conflicts")
    try:
        merged_digest = _snapshot(plan["tables"])["digest"]
        application_digest = _application_digest(
            format=plan["format"],
            inventory_id=plan["inventory_id"],
            replica_ref=plan["replica_ref"],
            base_digest=plan["base_digest"],
            bundle_digest=plan["bundle_digest"],
            canonical_head_digest=plan["canonical_head_digest"],
            replica_head_digest=plan["replica_head_digest"],
            resolutions=resolutions,
            event_sequence_rewrites=plan["event_sequence_rewrites"],
            tables=plan["tables"],
        )
        expected = _receipt_for_application(
            replica_ref=plan["replica_ref"],
            replica_head_digest=plan["replica_head_digest"],
            application_digest=application_digest,
        )
    except (KeyError, TypeError) as error:
        raise SyncError("sync plan receipt material is malformed") from error
    if (
        plan.get("merged_digest") != merged_digest
        or plan.get("application_digest") != application_digest
        or dict(receipt) != expected
    ):
        raise SyncError("sync receipt does not bind the merged result")
    return dict(receipt)
