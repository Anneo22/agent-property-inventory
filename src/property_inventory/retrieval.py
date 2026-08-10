"""Scope-safe, deterministic inventory retrieval shared by every read surface."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

SENSITIVITY_RANK = {"low": 0, "personal": 1, "high": 2}
SCOPE_MAX_SENSITIVITY = {"public": 0, "personal": 1, "private": 2}
EMPTY_MEANING = "unknown, not absent"
ITEM_DETAIL_FIELDS = frozenset(
    {
        "acquired_on",
        "condition",
        "purchase_currency",
        "purchase_price",
        "receipt_ref",
        "serial_or_lot",
    }
)


class RetrievalError(RuntimeError):
    """Raised when a scope-safe retrieval request cannot be resolved."""


def page_fingerprint(kind: str, request: dict[str, Any], identities: list[str]) -> str:
    payload = json.dumps(
        {"identities": identities, "kind": kind, "request": request},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def encode_page_cursor(kind: str, fingerprint: str, after: str) -> str:
    payload = json.dumps(
        {"after": after, "fingerprint": fingerprint, "kind": kind},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_page_cursor(cursor: str | None, kind: str, fingerprint: str) -> str | None:
    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
        value = json.loads(payload)
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise RetrievalError("pagination cursor is invalid") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"after", "fingerprint", "kind"}
        or value.get("kind") != kind
        or value.get("fingerprint") != fingerprint
        or not isinstance(value.get("after"), str)
        or not value["after"]
    ):
        raise RetrievalError("pagination cursor does not match this inventory query")
    return value["after"]


def scope_allows(scope: str, sensitivity: str) -> bool:
    return SENSITIVITY_RANK[sensitivity] <= SCOPE_MAX_SENSITIVITY[scope]


def identity_scope_allows(scope: str, item: dict[str, Any]) -> bool:
    sensitivity = item.get("identity_sensitivity") or item["sensitivity"]
    return scope_allows(scope, sensitivity)


def scope_visible_item_details(
    store: Any | Mapping[str, list[dict[str, Any]]],
    item: dict[str, Any],
    scope: str,
    *,
    evidence_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return current detail fields without materializing hidden amendments.

    `items` is the current projection of an append-only detail-amendment history.
    A low-sensitivity item can therefore hold a value last changed with high-
    sensitivity evidence. Lower scopes must report that current field as unknown,
    rather than treating the projection row as its provenance.
    """
    rows = store.rows if hasattr(store, "rows") else store
    evidence_by_id = evidence_by_id or {
        row["evidence_id"]: row for row in rows["evidence"]
    }
    current = {field: item.get(field) for field in ITEM_DETAIL_FIELDS}
    latest_changes: dict[str, dict[str, Any]] = {}
    amendments = sorted(
        (
            amendment
            for amendment in rows.get("item_detail_amendments", ())
            if amendment["item_id"] == item["item_id"]
        ),
        key=lambda amendment: (
            amendment["amended_on"],
            amendment["recorded_at"],
            amendment["detail_amendment_id"],
        ),
    )
    for amendment in amendments:
        try:
            changes = json.loads(amendment["changes_json"])
        except (TypeError, json.JSONDecodeError):
            # A malformed canonical row is rejected by verification. Fail closed
            # here too, so a direct reader never exposes its projected details.
            return {field: None for field in ITEM_DETAIL_FIELDS}
        if not isinstance(changes, dict):
            return {field: None for field in ITEM_DETAIL_FIELDS}
        for field in changes:
            if field in ITEM_DETAIL_FIELDS:
                latest_changes[field] = amendment
    for field, amendment in latest_changes.items():
        evidence = evidence_by_id.get(amendment["evidence_id"])
        if (
            evidence is None
            or not scope_allows(scope, amendment["sensitivity"])
            or not scope_allows(scope, evidence["sensitivity"])
        ):
            current[field] = None
    return current


def normalized(value: object) -> str:
    """Make lexical matching case- and punctuation-insensitive."""
    return " ".join(re.findall(r"[^\W_]+", str(value).casefold(), flags=re.UNICODE))


def tokens(values: Iterable[str]) -> list[str]:
    return [part for value in values for part in normalized(value).split()]


def location_scope_allows(
    locations: dict[str, dict[str, Any]], location: dict[str, Any], scope: str
) -> bool:
    current: dict[str, Any] | None = location
    visited: set[str] = set()
    while current is not None:
        location_id = current["location_id"]
        if location_id in visited or not scope_allows(scope, current["sensitivity"]):
            return False
        visited.add(location_id)
        current = locations.get(current.get("parent_location_id"))
    return True


def visible_location_chain(
    locations: dict[str, dict[str, Any]], location_id: str | None, scope: str
) -> list[dict[str, Any]]:
    if location_id is None:
        return []
    location = locations.get(location_id)
    if location is None or not location_scope_allows(locations, location, scope):
        return []
    chain = []
    while location is not None:
        chain.append(location)
        location = locations.get(location.get("parent_location_id"))
    return chain


def visible_location_path(
    locations: dict[str, dict[str, Any]], location_id: str | None, scope: str
) -> list[dict[str, Any]]:
    """Return the full root-to-leaf ancestry, empty when unknown or out of scope.

    Scope is applied to the whole chain before anything is serialized, so a
    partially visible path is withheld rather than published with a gap.
    """
    return [
        {"kind": row["kind"], "location_id": row["location_id"], "name": row["name"]}
        for row in reversed(visible_location_chain(locations, location_id, scope))
    ]


def most_specific_placement(
    item: dict[str, Any], *, prefix: str = ""
) -> str | None:
    """Prefer the container over the area, because a drawer beats a room."""
    return item.get(f"{prefix}container_id") or item.get(f"{prefix}location_id")


def location_state(
    locations: dict[str, dict[str, Any]], item: dict[str, Any], scope: str
) -> str | None:
    """Return known/unknown, or None when scope redaction prevents classification."""
    references = (item.get("location_id"), item.get("container_id"))
    has_reference = any(reference is not None for reference in references)
    has_semantic_unknown = False
    has_real_visible_location = False
    has_redacted_reference = False
    for reference in references:
        if reference is None:
            continue
        location = locations.get(reference)
        chain = visible_location_chain(locations, reference, scope)
        if location is None or not chain:
            has_redacted_reference = True
            continue
        if location["kind"] == "unknown":
            has_semantic_unknown = True
        else:
            has_real_visible_location = True
    if has_redacted_reference:
        return None
    if has_real_visible_location:
        return "known"
    if has_semantic_unknown or not has_reference:
        return "unknown"
    return None


def item_context(store: Any, item: dict[str, Any], *, scope: str = "private") -> dict[str, Any]:
    """Serialize one visible item while redacting every lower-scope dependency."""
    if not scope_allows(scope, item["sensitivity"]):
        raise RetrievalError(f"item is not available in {scope} scope: {item['item_id']}")
    model = store.get("models", item["model_id"])
    identity_visible = identity_scope_allows(scope, item)
    locations = {row["location_id"]: row for row in store.rows["locations"]}
    evidence_by_id = {row["evidence_id"]: row for row in store.rows["evidence"]}
    item_evidence_links = [
        row
        for row in store.rows["item_evidence"]
        if row["item_id"] == item["item_id"]
    ]
    evidence_ids = [row["evidence_id"] for row in item_evidence_links]
    events = [row for row in store.rows["inventory_events"] if row["item_id"] == item["item_id"]]
    relationships = [
        row
        for row in store.rows["relationships"]
        if item["item_id"] in (row["subject_item_id"], row["object_item_id"])
        and row["evidence_id"] in evidence_by_id
        and scope_allows(scope, evidence_by_id[row["evidence_id"]]["sensitivity"])
    ]
    events = [
        row
        for row in events
        if row.get("evidence_id") is None
        or (
            row["evidence_id"] in evidence_by_id
            and scope_allows(scope, evidence_by_id[row["evidence_id"]]["sensitivity"])
        )
    ]
    visible_item_ids = {
        row["item_id"]
        for row in store.rows["items"]
        if scope_allows(scope, row["sensitivity"])
    }
    visible_evidence_ids = {
        evidence_id
        for evidence_id, evidence in evidence_by_id.items()
        if scope_allows(scope, evidence["sensitivity"])
    }
    item_roles = {
        row["evidence_id"]: row["role"]
        for row in item_evidence_links
        if row["evidence_id"] in visible_evidence_ids
    }
    media_by_id = {row["asset_id"]: row for row in store.rows["media_assets"]}
    asset_links_by_evidence: dict[str, list[dict[str, Any]]] = {}
    for link in store.rows["evidence_assets"]:
        asset = media_by_id.get(link["asset_id"])
        if (
            link["evidence_id"] not in item_roles
            or asset is None
            or not scope_allows(scope, asset["sensitivity"])
        ):
            continue
        asset_view = {
            **asset,
            "role": link["role"],
            "region": (
                json.loads(link["region_json"])
                if link.get("region_json") is not None
                else None
            ),
        }
        if scope != "private":
            for field in ("asset_id", "original_name", "sha256", "uri"):
                asset_view[field] = None
            asset_view["region"] = None
        asset_links_by_evidence.setdefault(link["evidence_id"], []).append(asset_view)
    evidence_records = []
    for evidence_id in sorted(item_roles):
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            continue
        evidence_view = {
            **evidence,
            "item_role": item_roles[evidence_id],
            "assets": sorted(
                asset_links_by_evidence.get(evidence_id, []),
                key=lambda row: (row["role"], row.get("asset_id") or ""),
            ),
        }
        if scope != "private":
            evidence_view["evidence_id"] = None
            evidence_view["source_ref"] = None
            evidence_view["notes"] = None
        evidence_records.append(evidence_view)
    visible_kits = {
        row["kit_id"]: row
        for row in store.rows["kits"]
        if row["serves_item_id"] in visible_item_ids
        and row["evidence_id"] in visible_evidence_ids
    }
    visible_kit_requirements = [
        row
        for row in store.rows["kit_requirements"]
        if row["kit_id"] in visible_kits
        and row["evidence_id"] in visible_evidence_ids
        and (row["item_id"] is None or row["item_id"] in visible_item_ids)
    ]
    relevant_kit_ids = {
        kit_id
        for kit_id, kit in visible_kits.items()
        if kit["serves_item_id"] == item["item_id"]
    }
    relevant_kit_ids.update(
        row["kit_id"]
        for row in visible_kit_requirements
        if row["item_id"] == item["item_id"]
    )
    kits = [
        visible_kits[kit_id]
        for kit_id in sorted(relevant_kit_ids)
    ]
    kit_requirements = sorted(
        (
            row
            for row in visible_kit_requirements
            if row["kit_id"] in relevant_kit_ids
        ),
        key=lambda row: (row["kit_id"], row["requirement_key"]),
    )
    kit_reviews = sorted(
        (
            row
            for row in store.rows["kit_reviews"]
            if row["kit_id"] in relevant_kit_ids
            and row["evidence_id"] in visible_evidence_ids
            and scope_allows(scope, row["sensitivity"])
        ),
        key=lambda row: (
            row["kit_id"],
            row["reviewed_on"],
            row["recorded_at"],
            row["review_id"],
        ),
    )
    torque_paths = sorted(
        (
            row
            for row in store.rows["torque_paths"]
            if row["tool_item_id"] == item["item_id"]
            and row["tool_item_id"] in visible_item_ids
            and row["evidence_id"] in visible_evidence_ids
        ),
        key=lambda row: row["path_id"],
    )
    item_tags = sorted(
        _visible_tags(store, item["item_id"], scope),
        key=lambda row: row["tag"].casefold(),
    )
    item_dimensions = sorted(
        (
            row
            for row in store.rows["item_dimensions"]
            if row["item_id"] == item["item_id"]
            and scope_allows(scope, row["sensitivity"])
            and row["evidence_id"] in visible_evidence_ids
        ),
        key=lambda row: (
            row["measured_on"],
            row["recorded_at"],
            row["dimension_id"],
        ),
    )
    item_amendments = sorted(
        (
            row
            for row in store.rows["item_amendments"]
            if row["item_id"] == item["item_id"]
            and row["evidence_id"] in visible_evidence_ids
        ),
        key=lambda row: (
            row["amended_on"],
            row["recorded_at"],
            row["amendment_id"],
        ),
    )
    item_detail_amendments = sorted(
        (
            row
            for row in store.rows["item_detail_amendments"]
            if row["item_id"] == item["item_id"]
            and row["evidence_id"] in visible_evidence_ids
            and scope_allows(scope, row["sensitivity"])
        ),
        key=lambda row: (
            row["amended_on"],
            row["recorded_at"],
            row["detail_amendment_id"],
        ),
    )
    item_view = dict(item)
    item_view.update(
        scope_visible_item_details(
            store, item, scope, evidence_by_id=evidence_by_id
        )
    )
    model_view = {
        **model,
        "interfaces": json.loads(model["interfaces_json"]),
        "specs": json.loads(model["specs_json"]),
        "identifiers": json.loads(model["identifiers_json"]),
    }
    location_chain = visible_location_chain(locations, item.get("location_id"), scope)
    container_chain = visible_location_chain(locations, item.get("container_id"), scope)
    location_path = visible_location_path(locations, most_specific_placement(item), scope)
    home_location_path = visible_location_path(
        locations, most_specific_placement(item, prefix="home_"), scope
    )
    parties_by_id = {row["party_id"]: row for row in store.rows.get("parties", [])}
    party_relations = [
        dict(row) for row in store.rows.get("item_party_relations", [])
        if row.get("item_id") == item["item_id"] and scope_allows(scope, row.get("sensitivity"))
    ]
    for relation in party_relations:
        party = parties_by_id.get(relation.get("party_id"))
        if party is not None and scope_allows(scope, party.get("sensitivity")):
            relation["party"] = {"party_id": party["party_id"], "name": party["name"], "party_kind": party["party_kind"]}
        else:
            relation["party"] = None
    def fact_amendment_mentions_item(row: dict[str, Any]) -> bool:
        facts: list[dict[str, Any]] = []
        for field in ("previous_json", "replacement_json"):
            if row.get(field) is None:
                continue
            try:
                value = json.loads(row[field])
            except (TypeError, json.JSONDecodeError):
                return False
            if isinstance(value, dict):
                facts.append(value)
        table = row.get("table_name")
        for fact in facts:
            if fact.get("item_id") == item["item_id"]:
                return True
            if table == "relationships" and item["item_id"] in {
                fact.get("subject_item_id"),
                fact.get("object_item_id"),
            }:
                return True
            if table == "torque_paths" and fact.get("tool_item_id") == item["item_id"]:
                return True
            if table == "kits" and fact.get("serves_item_id") == item["item_id"]:
                return True
            if table == "model_interfaces" and fact.get("model_id") == item["model_id"]:
                return True
            if table == "kit_requirements":
                kit = next(
                    (
                        candidate
                        for candidate in store.rows["kits"]
                        if candidate["kit_id"] == fact.get("kit_id")
                    ),
                    None,
                )
                if kit is not None and kit["serves_item_id"] == item["item_id"]:
                    return True
            if table in {"locations", "spatial_profiles"}:
                location_id = fact.get("location_id")
                if location_id is not None and any(
                    location.get("location_id") == location_id
                    for location in (*location_chain, *container_chain)
                ):
                    return True
        return False

    fact_amendments = sorted(
        (
            row
            for row in store.rows["fact_amendments"]
            if row["evidence_id"] in visible_evidence_ids
            and scope_allows(scope, row["sensitivity"])
            and fact_amendment_mentions_item(row)
        ),
        key=lambda row: (
            row["amended_on"],
            row["recorded_at"],
            row["fact_amendment_id"],
        ),
    )
    location_name = location_chain[0]["name"] if location_chain else None
    container_name = container_chain[0]["name"] if container_chain else None
    if item.get("location_id") is not None and not location_chain:
        item_view["location_id"] = None
        location_name = "[redacted]"
    if item.get("container_id") is not None and not container_chain:
        item_view["container_id"] = None
        container_name = "[redacted]"
    for field in ("home_location_id", "home_container_id"):
        if item.get(field) is not None and not visible_location_chain(
            locations, item[field], scope
        ):
            item_view[field] = None
    if scope != "private":
        for field in (
            "notes",
            "serial_or_lot",
            "receipt_ref",
            "purchase_price",
            "purchase_currency",
            "replacement_value",
            "value_currency",
            "primary_evidence_id",
        ):
            item_view[field] = None
        model_view["identifiers"] = {}
        model_view["identifiers_json"] = "{}"
        model_view["reference_url"] = None
        evidence_ids = []
        events = [
            {**event, "actor": None, "evidence_id": None, "notes": None}
            for event in events
        ]
        accessible_items = {
            row["item_id"]
            for row in store.rows["items"]
            if scope_allows(scope, row["sensitivity"])
        }
        relationships = [
            {**relationship, "evidence_id": None, "notes": None}
            for relationship in relationships
            if relationship["subject_item_id"] in accessible_items
            and relationship["object_item_id"] in accessible_items
        ]
        kits = [{**kit, "evidence_id": None, "notes": None} for kit in kits]
        kit_requirements = [
            {**requirement, "evidence_id": None, "notes": None}
            for requirement in kit_requirements
        ]
        kit_reviews = [
            {**review, "actor": None, "evidence_id": None, "notes": None}
            for review in kit_reviews
        ]
        torque_paths = [
            {**path, "evidence_id": None, "notes": None}
            for path in torque_paths
        ]
        item_tags = [
            {**tag, "evidence_id": None, "notes": None} for tag in item_tags
        ]
        item_dimensions = [
            {**dimension, "evidence_id": None, "notes": None}
            for dimension in item_dimensions
        ]
        item_amendments = [
            {**amendment, "actor": None, "evidence_id": None, "notes": None}
            for amendment in item_amendments
        ]
        item_detail_amendments = []
        fact_amendments = []
        party_relations = [
            {**relation, "party_id": None, "evidence_id": None,
             "ended_evidence_id": None, "notes": None,
             "party": ({"party_id": None, "name": "[redacted]", "party_kind": None} if relation.get("party") else None)}
            for relation in party_relations
        ]
    active_loans = [
        relation
        for relation in party_relations
        if relation.get("role") == "custodian"
        and relation.get("status") == "active"
        and relation.get("custody_kind") == "loan"
    ]
    custody_summary = {
        "owners": [
            relation
            for relation in party_relations
            if relation.get("role") == "owner" and relation.get("status") == "active"
        ],
        "custodians": [
            relation
            for relation in party_relations
            if relation.get("role") == "custodian" and relation.get("status") == "active"
        ],
        "access": [
            relation
            for relation in party_relations
            if relation.get("role") == "access" and relation.get("status") == "active"
        ],
        "active_loans": active_loans,
        "overdue_loans": [
            relation
            for relation in active_loans
            if isinstance(relation.get("due_on"), str)
            and relation["due_on"] < date.today().isoformat()
        ],
    }
    if not identity_visible:
        item_view["model_id"] = None
        model_view = {
            "brand": None,
            "category": None,
            "identifiers": {},
            "identifiers_json": "{}",
            "interfaces": [],
            "interfaces_json": "[]",
            "model": None,
            "model_id": None,
            "name": "[redacted]",
            "reference_url": None,
            "specs": {},
            "specs_json": "{}",
        }
        relationships = []
        item_tags = []
        kits = []
        kit_requirements = []
        kit_reviews = []
        torque_paths = []
        item_amendments = []
        item_detail_amendments = []
        fact_amendments = []
    return {
        "item": item_view,
        "model": model_view,
        "location": location_name,
        "container": container_name,
        "location_path": location_path,
        "current_location_path": location_path,
        "home_location_path": home_location_path,
        "party_relations": party_relations,
        "custody": custody_summary,
        "evidence_ids": evidence_ids,
        "evidence": evidence_records,
        "events": events,
        "relationships": relationships,
        "item_tags": item_tags,
        "kits": kits,
        "kit_requirements": kit_requirements,
        "kit_reviews": kit_reviews,
        "torque_paths": torque_paths,
        "item_dimensions": item_dimensions,
        "item_amendments": item_amendments,
        "item_detail_amendments": item_detail_amendments,
        "fact_amendments": fact_amendments,
    }


def _visible_aliases(store: Any, item_id: str, scope: str) -> list[dict[str, Any]]:
    evidence = {row["evidence_id"]: row for row in store.rows["evidence"]}
    return [
        row
        for row in store.rows["aliases"]
        if row["item_id"] == item_id
        and scope_allows(scope, row["sensitivity"])
        and row["evidence_id"] in evidence
        and scope_allows(scope, evidence[row["evidence_id"]]["sensitivity"])
    ]


def _visible_tags(store: Any, item_id: str, scope: str) -> list[dict[str, Any]]:
    evidence = {row["evidence_id"]: row for row in store.rows["evidence"]}
    return [
        row
        for row in store.rows["item_tags"]
        if row["item_id"] == item_id
        and scope_allows(scope, row["sensitivity"])
        and row["evidence_id"] in evidence
        and scope_allows(scope, evidence[row["evidence_id"]]["sensitivity"])
    ]


def _visible_interfaces(store: Any, model_id: str, scope: str) -> list[dict[str, Any]]:
    evidence = {row["evidence_id"]: row for row in store.rows["evidence"]}
    interfaces = {row["interface_id"]: row for row in store.rows["interfaces"]}
    return [
        interfaces[row["interface_id"]]
        for row in store.rows["model_interfaces"]
        if row["model_id"] == model_id
        and row["interface_id"] in interfaces
        and row["evidence_id"] in evidence
        and scope_allows(scope, evidence[row["evidence_id"]]["sensitivity"])
    ]


def _matches_exact(value: object, expected: str | None) -> bool:
    return expected is None or normalized(value) == normalized(expected)


def _matches_location(
    locations: dict[str, dict[str, Any]], item: dict[str, Any], scope: str, expected: str | None
) -> bool:
    if expected is None:
        return True
    expected_normalized = normalized(expected)
    return any(
        expected_normalized in {normalized(location["location_id"]), normalized(location["name"])}
        for location_id in (item.get("location_id"), item.get("container_id"))
        for location in visible_location_chain(locations, location_id, scope)
    )


def _matches_filters(
    store: Any,
    item: dict[str, Any],
    model: dict[str, Any],
    scope: str,
    filters: dict[str, str | None],
) -> bool:
    locations = {row["location_id"]: row for row in store.rows["locations"]}
    identity_visible = identity_scope_allows(scope, item)
    aliases = (
        _visible_aliases(store, item["item_id"], scope)
        if identity_visible
        else []
    )
    interfaces = (
        _visible_interfaces(store, item["model_id"], scope)
        if identity_visible
        else []
    )
    tags = (
        [row["tag"] for row in _visible_tags(store, item["item_id"], scope)]
        if identity_visible
        else []
    )
    item_location_state = location_state(locations, item, scope)
    item_details = scope_visible_item_details(store, item, scope)
    return (
        (filters.get("category") is None or (
            identity_visible
            and _matches_exact(model["category"], filters.get("category"))
        ))
        and _matches_exact(item["ownership_state"], filters.get("ownership_state"))
        and _matches_exact(item_details["condition"] or "", filters.get("condition"))
        and _matches_location(locations, item, scope, filters.get("location"))
        and (
            filters.get("tag") is None
            or any(_matches_exact(tag, filters["tag"]) for tag in tags)
        )
        and (
            filters.get("alias_kind") is None
            or any(_matches_exact(alias["alias_kind"], filters["alias_kind"]) for alias in aliases)
        )
        and (
            filters.get("interface_family") is None
            and filters.get("interface_standard") is None
            and filters.get("interface_variant") is None
            and filters.get("interface_direction") is None
            or any(
                _matches_exact(interface["family"], filters.get("interface_family"))
                and _matches_exact(interface.get("standard") or "", filters.get("interface_standard"))
                and _matches_exact(interface.get("variant") or "", filters.get("interface_variant"))
                and _matches_exact(interface["direction"], filters.get("interface_direction"))
                for interface in interfaces
            )
        )
        and (
            filters.get("location_known") is None
            or filters["location_known"] == item_location_state
        )
    )


def _haystack(store: Any, item: dict[str, Any], model: dict[str, Any], scope: str) -> str:
    locations = {row["location_id"]: row for row in store.rows["locations"]}
    identity_visible = identity_scope_allows(scope, item)
    aliases = (
        _visible_aliases(store, item["item_id"], scope)
        if identity_visible
        else []
    )
    interfaces = (
        _visible_interfaces(store, item["model_id"], scope)
        if identity_visible
        else []
    )
    location_values = [
        value
        for location_id in (
            item.get("location_id"),
            item.get("container_id"),
            item.get("home_location_id"),
            item.get("home_container_id"),
        )
        for location in visible_location_chain(locations, location_id, scope)
        for value in (location["location_id"], location["name"])
    ]
    tags = (
        [row["tag"] for row in _visible_tags(store, item["item_id"], scope)]
        if identity_visible
        else []
    )
    values: list[object] = [
        item["item_id"],
        *location_values,
        *tags,
        *(alias["alias"] for alias in aliases),
        *(
            value
            for interface in interfaces
            for value in (
                interface["family"],
                interface.get("standard"),
                interface.get("variant"),
                interface["direction"],
            )
        ),
    ]
    if identity_visible:
        values.extend(
            (
                model.get("name"),
                model.get("brand"),
                model.get("model"),
                model.get("category"),
                model.get("interfaces_json"),
                model.get("specs_json"),
            )
        )
    if scope == "private":
        values.extend(
            (
                item.get("notes"),
                item.get("serial_or_lot"),
                model.get("identifiers_json"),
            )
        )
    return normalized(" ".join(str(value) for value in values if value is not None))


def search(
    store: Any,
    *,
    query: Iterable[str],
    scope: str,
    limit: int,
    filters: dict[str, str | None],
    cursor: str | None = None,
) -> dict[str, Any]:
    if limit < 1 or limit > 500:
        raise RetrievalError("limit must be between 1 and 500")
    query_tokens = tokens(query)
    matches = []
    for item in sorted(store.rows["items"], key=lambda row: row["item_id"]):
        if not scope_allows(scope, item["sensitivity"]):
            continue
        model = store.get("models", item["model_id"])
        if not _matches_filters(store, item, model, scope, filters):
            continue
        haystack = _haystack(store, item, model, scope)
        if not query_tokens or all(token in haystack for token in query_tokens):
            matches.append(item)
    identities = [item["item_id"] for item in matches]
    fingerprint = page_fingerprint(
        "items",
        {"filters": filters, "query": query_tokens, "scope": scope},
        identities,
    )
    after = decode_page_cursor(cursor, "items", fingerprint)
    remaining = [item for item in matches if after is None or item["item_id"] > after]
    page = remaining[:limit]
    next_cursor = (
        encode_page_cursor("items", fingerprint, page[-1]["item_id"])
        if len(remaining) > limit and page
        else None
    )
    return {
        "recorded": bool(matches),
        "meaning_if_empty": EMPTY_MEANING,
        "query": query_tokens,
        "count": len(matches),
        "matches": [item_context(store, item, scope=scope) for item in page],
        "next_cursor": next_cursor,
        "page_count": len(page),
        "truncated": next_cursor is not None,
    }


def resolve_item_reference(store: Any, reference: str, *, scope: str) -> dict[str, Any]:
    all_items = {row["item_id"]: row for row in store.rows["items"]}
    visible_items = {
        row["item_id"]: row
        for row in store.rows["items"]
        if scope_allows(scope, row["sensitivity"])
    }
    if reference in visible_items:
        return visible_items[reference]
    if reference in all_items:
        raise RetrievalError(f"item not found in {scope} scope")
    expected = normalized(reference)
    candidates = {
        alias["item_id"]
        for alias in store.rows["aliases"]
        if alias["item_id"] in visible_items
        and identity_scope_allows(scope, visible_items[alias["item_id"]])
        and normalized(alias["alias"]) == expected
        and alias in _visible_aliases(store, alias["item_id"], scope)
    }
    if not candidates:
        raise RetrievalError(f"item not found in {scope} scope")
    if len(candidates) > 1:
        raise RetrievalError("alias is ambiguous; use an item_id")
    return visible_items[candidates.pop()]


def task_context(
    store: Any,
    *,
    task: str,
    scope: str,
    limit: int,
    filters: dict[str, str | None],
    cursor: str | None = None,
) -> dict[str, Any]:
    result = search(
        store,
        query=[task],
        scope=scope,
        limit=limit,
        filters=filters,
        cursor=cursor,
    )
    if not result["matches"]:
        unknowns: list[dict[str, Any]] = [
            {
                "field": "matching_inventory_records",
                "meaning": EMPTY_MEANING,
            }
        ]
    else:
        unknowns = []
        for match in result["matches"]:
            fields = [
                field
                for field, value in (
                    ("location", match["location"]),
                    ("container", match["container"]),
                    ("condition", match["item"].get("condition")),
                    ("serial_or_lot", match["item"].get("serial_or_lot")),
                )
                if value is None or value == "[redacted]"
            ]
            if fields:
                unknowns.append({"item_id": match["item"]["item_id"], "fields": fields})
    return {
        "task": task,
        "meaning_if_empty": EMPTY_MEANING,
        "recorded": result["recorded"],
        "count": result["count"],
        "matches": result["matches"],
        "next_cursor": result["next_cursor"],
        "page_count": result["page_count"],
        "truncated": result["truncated"],
        "unknowns": unknowns,
    }
