#!/usr/bin/env python3
"""Render a canonical SQLite inventory index into an Obsidian catalogue."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path

from filelock import FileLock

HERE = Path(__file__).resolve().parent
DATABASE = HERE / ".local" / "inventory.sqlite"
SENSITIVITY_RANK = {"low": 0, "personal": 1, "high": 2}
SCOPE_MAX_SENSITIVITY = {"public": 0, "personal": 1, "private": 2}
USABLE_CONDITIONS = frozenset(
    {
        "excellent",
        "functional",
        "good",
        "like new",
        "new",
        "operational",
        "serviceable",
        "working",
    }
)
OWNER_MARKER = re.compile(
    r"^<!-- canonical-inventory-owner-sha256:([0-9a-f]{64}) -->\n", re.MULTILINE
)
FRONTMATTER = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)
CREATED_PROPERTY = re.compile(
    r"^Created: (\d{4}-\d{2}-\d{2})$", re.MULTILINE
)


def canonical_digest(rows: list[dict]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def inventory_owner_digest(inventory_id: str) -> str:
    """Return a non-reversible marker for the inventory that generated a catalogue."""
    return hashlib.sha256(inventory_id.encode()).hexdigest()


def remove_owner_marker(note: str) -> str:
    """Return a legacy-compatible generated note without its owner marker."""
    return OWNER_MARKER.sub("", note, count=1)


def catalogue_owner(note: str) -> str:
    """Read the single owner marker embedded in a generated catalogue."""
    matches = OWNER_MARKER.findall(note)
    if len(matches) != 1:
        raise ValueError("generated catalogue must contain exactly one owner marker")
    return matches[0]


def validate_created_on(value: str) -> str:
    """Require one exact ISO calendar date."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("generated catalogue Created property must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(
            "generated catalogue Created property must be an ISO date"
        ) from error
    if parsed.isoformat() != value:
        raise ValueError("generated catalogue Created property must be an ISO date")
    return value


def catalogue_created_on(note: str) -> str | None:
    """Read one valid Obsidian creation date from initial YAML frontmatter."""
    frontmatter = FRONTMATTER.match(note)
    if frontmatter is None:
        return None
    matches = CREATED_PROPERTY.findall(frontmatter.group("body"))
    if len(matches) > 1:
        raise ValueError("generated catalogue must contain at most one Created property")
    if not matches:
        return None
    return validate_created_on(matches[0])


def remove_created_property(note: str) -> str:
    """Remove the non-authoritative creation property from YAML frontmatter."""
    frontmatter = FRONTMATTER.match(note)
    if frontmatter is None:
        return note
    body = frontmatter.group("body")
    created = CREATED_PROPERTY.search(body)
    if created is None:
        return note
    created_start, created_end = created.span()
    if created_end < len(body) and body[created_end] == "\n":
        created_end += 1
    elif created_start > 0 and body[created_start - 1] == "\n":
        created_start -= 1
    cleaned = body[:created_start] + body[created_end:]
    start, end = frontmatter.span("body")
    return note[:start] + cleaned + note[end:]


def digest_rows(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    """Return the visible fields of one rendered catalogue table."""
    return [{key: row.get(key) for key in keys} for row in rows]


def _digest_fact(value: object, *, scope: str) -> object:
    """Keep only the fields of a nested fact that the catalogue renders."""
    if isinstance(value, list):
        return [_digest_fact(part, scope=scope) for part in value]
    if not isinstance(value, dict):
        return value
    if "axes" in value:
        axes = value.get("axes")
        return {
            "axes": {
                str(axis): _digest_fact(measurement, scope=scope)
                for axis, measurement in sorted(axes.items())
            }
            if isinstance(axes, dict)
            else None
        }
    visible = {
        key: value.get(key)
        for key in (
            "alias",
            "alias_kind",
            "tag",
            "width",
            "height",
            "depth",
            "unit",
            "measured_on",
            "recorded_at",
            "amount",
            "currency",
            "valued_on",
            "basis",
            "amended_on",
            "reason",
            "uri",
            "evidence_type",
            "claim_strength",
            "captured_on",
        )
    }
    if scope == "private":
        visible["source_ref"] = value.get("source_ref")
    return visible


def _digest_audit(value: object) -> object:
    """Keep every rendered private audit value, including nested predecessor facts."""
    if isinstance(value, list):
        return [_digest_audit(part) for part in value]
    if isinstance(value, dict):
        return {str(key): _digest_audit(part) for key, part in sorted(value.items())}
    return value


def inventory_digest_rows(
    inventory: list[dict], keys: tuple[str, ...], scope: str
) -> list[dict]:
    nested = {
        "aliases",
        "tags",
        "dimensions",
        "amendment",
        "receipt_documents",
        "audit_history",
    }
    return [
        {
            key: (
                _digest_audit(row.get(key))
                if key == "audit_history"
                else _digest_fact(row.get(key), scope=scope)
            )
            if key in nested else row.get(key)
            for key in keys
        }
        for row in inventory
    ]


def catalogue_digest(
    inventory: list[dict],
    states: list[dict],
    relationships: list[dict],
    kits: list[dict],
    torque_paths: list[dict],
    scope: str,
) -> str:
    """Hash every selected-scope datum rendered into the catalogue body."""
    inventory_keys = (
        "item_id",
        "name",
        "category",
        "quantity",
        "unit",
        "ownership_state",
        "location",
        "container",
        "current_location_path",
        "home_location_path",
        "verified_on",
        "evidence_type",
        "claim_strength",
        "interfaces_json",
        "specs_json",
        "aliases",
        "tags",
        "dimensions",
        "valuations",
        "amendment",
    )
    relationship_keys = ("subject", "predicate", "object", "confidence")
    kit_keys = (
        "kit",
        "serves",
        "requirement",
        "matched_item",
        "status",
        "review_completeness",
        "reviewed_on",
        "review_recorded_at",
        "review_evidence_type",
        "review_claim_strength",
    )
    torque_keys = ("tool", "output_drive", "range_nm", "adapter", "status")
    if scope == "private":
        inventory_keys += (
            "condition",
            "serial_or_lot",
            "acquired_on",
            "purchase_price",
            "purchase_currency",
            "replacement_value",
            "value_currency",
            "receipt_ref",
            "receipt_documents",
            "notes",
            "reference_url",
            "audit_history",
        )
        relationship_keys += ("source_ref", "notes")
        kit_keys += ("source_ref", "notes", "review_source_ref")
        torque_keys += ("source_ref", "notes")
    return canonical_digest(
        [
            {"inventory": inventory_digest_rows(inventory, inventory_keys, scope)},
            {"states": digest_rows(states, ("state", "rows"))},
            {"relationships": digest_rows(relationships, relationship_keys)},
            {"kits": digest_rows(kits, kit_keys)},
            {"torque_paths": digest_rows(torque_paths, torque_keys)},
        ]
    )


def sensitivity_predicate(column: str) -> str:
    """Return a SQL predicate that accepts rows visible in one supplied scope."""
    cases = " ".join(
        f"WHEN '{sensitivity}' THEN {rank}"
        for sensitivity, rank in SENSITIVITY_RANK.items()
    )
    return f"CASE {column} {cases} END <= ?"


def visible_projected_item_detail(
    con: sqlite3.Connection,
    item_id: str | None,
    field: str,
    current_value: object,
    scope_rank: int,
) -> object:
    """Hide a current item detail whose latest amendment is outside scope."""
    if item_id is None:
        return current_value
    if field not in {
        "acquired_on",
        "condition",
        "purchase_currency",
        "purchase_price",
        "receipt_ref",
        "serial_or_lot",
    }:
        raise ValueError(f"unsupported projected item detail: {field}")
    latest = con.execute(
        """
        SELECT a.sensitivity AS amendment_sensitivity,
               e.sensitivity AS evidence_sensitivity
        FROM item_detail_amendments a
        JOIN evidence e ON e.evidence_id=a.evidence_id
        WHERE a.item_id=? AND json_type(a.changes_json, ?) IS NOT NULL
        ORDER BY a.amended_on DESC, a.recorded_at DESC,
                 a.detail_amendment_id DESC
        LIMIT 1
        """,
        (item_id, f"$.{field}"),
    ).fetchone()
    if latest is not None and (
        SENSITIVITY_RANK[latest["amendment_sensitivity"]] > scope_rank
        or SENSITIVITY_RANK[latest["evidence_sensitivity"]] > scope_rank
    ):
        return None
    return current_value


def catalogue_output_lock(output: Path) -> FileLock:
    identity = hashlib.sha256(str(output.resolve()).encode()).hexdigest()
    lock_dir = Path(tempfile.gettempdir()) / "agent-property-inventory-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(lock_dir / f"catalogue-{identity}.lock")


def write_output_atomic(output: Path, note: str) -> None:
    """Serialize ownership validation and replacement for one catalogue path."""
    with catalogue_output_lock(output):
        write_output_atomic_unlocked(output, note)


def write_output_atomic_unlocked(output: Path, note: str) -> None:
    """Durably replace an output file without exposing a partial render."""
    if output.is_symlink() or output.parent.is_symlink():
        raise ValueError("catalogue output must not traverse a managed symlink")
    owner = catalogue_owner(note)
    if output.exists():
        existing_bytes = output.read_bytes()
        existing = existing_bytes.decode("utf-8")
        existing_markers = OWNER_MARKER.findall(existing)
        if existing_markers:
            if len(existing_markers) != 1 or existing_markers[0] != owner:
                raise ValueError("catalogue output belongs to a different inventory owner")
        elif remove_created_property(existing) != remove_created_property(
            remove_owner_marker(note)
        ):
            raise ValueError(
                "refusing to replace an unowned catalogue that is not an exact legacy render"
            )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.tmp-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(note)
            handle.flush()
            os.fsync(handle.fileno())
        if os.environ.get("PROPERTY_INVENTORY_FAIL_BEFORE_RENDER_REPLACE") == "1":
            raise OSError("injected failure before render output replacement")
        os.replace(temporary, output)
        if os.name != "nt":
            directory_descriptor = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def decode(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def text(value: object) -> str:
    value = decode(value)
    if value is None or value == "":
        return ""
    if isinstance(value, list):
        return "; ".join(str(part) for part in value if part not in (None, ""))
    if isinstance(value, dict):
        return "; ".join(f"{key}: {val}" for key, val in value.items())
    return str(value)


def explicitly_usable_condition(value: object) -> bool:
    """Apply the CLI's punctuation- and case-insensitive usable-condition allowlist."""
    normalized = " ".join(re.findall(r"[^\W_]+", str(value or "").casefold()))
    return normalized in USABLE_CONDITIONS


def cell(value: object) -> str:
    return text(value).replace("|", "\\|").replace("\n", " ").strip() or "Not recorded"


def quantity(row: dict) -> str:
    value = row["quantity"]
    if value is None:
        return "Unknown"
    number = f"{value:g}" if isinstance(value, (int, float)) else str(value)
    return f"{number} {row['unit']}"


def place(row: dict) -> str:
    current_path = text(row.get("current_location_path"))
    home_path = text(row.get("home_location_path"))
    if current_path or home_path:
        parts = []
        if current_path:
            parts.append(f"Current: {current_path}")
        if home_path:
            parts.append(f"Home: {home_path}")
        return "; ".join(parts)
    location = text(row["location"])
    container = text(row["container"])
    if location and container and location != container:
        return f"{container} in {location}"
    return container or location or "Not recorded"


def item_label(row: dict, *, include_reference_url: bool) -> str:
    name = cell(row["name"])
    url = text(row["reference_url"])
    return (
        f"[{name}]({url})"
        if include_reference_url and url.startswith(("http://", "https://"))
        else name
    )


def money(value: object, currency: object) -> str:
    """Format a stored monetary amount without inventing a currency or precision."""
    if value is None or currency in (None, ""):
        return "Not recorded"
    if isinstance(value, (int, float)):
        amount = f"{value:.2f}".rstrip("0").rstrip(".")
    else:
        amount = str(value)
    return f"{currency} {amount}"


def provenance_label(row: dict, *, include_source_ref: bool) -> str:
    """Render an evidence-backed fact's provenance at the permitted detail level."""
    parts = [
        text(row.get("evidence_type")).replace("_", " "),
        text(row.get("claim_strength")).replace("_", " "),
    ]
    if row.get("captured_on"):
        parts.append(f"captured {text(row['captured_on'])}")
    if row.get("recorded_at"):
        parts.append(f"recorded {text(row['recorded_at'])}")
    if include_source_ref and text(row.get("source_ref")):
        parts.append(text(row["source_ref"]))
    return "; ".join(part for part in parts if part)


def aliases_and_tags(row: dict, *, include_source_ref: bool) -> str:
    """Render only aliases and tags whose own record and evidence are visible."""
    values: list[str] = []
    for alias in row.get("aliases", []):
        provenance = provenance_label(alias, include_source_ref=include_source_ref)
        label = f"Alias: {text(alias.get('alias'))} ({text(alias.get('alias_kind'))})"
        values.append(f"{label} [{provenance}]" if provenance else label)
    for tag in row.get("tags", []):
        provenance = provenance_label(tag, include_source_ref=include_source_ref)
        label = f"Tag: #{text(tag.get('tag'))}"
        values.append(f"{label} [{provenance}]" if provenance else label)
    return "; ".join(values) or "Not recorded"


def dimensions_label(row: dict, *, include_source_ref: bool) -> str:
    """Render the current complete-or-partial measured dimensions with provenance."""
    dimension = row.get("dimensions")
    if not isinstance(dimension, dict):
        return "Not recorded"
    axes = dimension.get("axes")
    if not isinstance(axes, dict):
        return "Not recorded"
    values: list[str] = []
    for axis in ("width", "height", "depth"):
        measurement = axes.get(axis)
        if not isinstance(measurement, dict):
            continue
        value = measurement.get(axis)
        if value is None:
            continue
        number = f"{value:g}" if isinstance(value, (int, float)) else str(value)
        measured_on = text(measurement.get("measured_on"))
        provenance = provenance_label(measurement, include_source_ref=include_source_ref)
        suffix = "; ".join(
            part
            for part in (
                f"measured {measured_on}" if measured_on else "",
                provenance,
            )
            if part
        )
        label = f"{axis} {number} {text(measurement.get('unit'))}".strip()
        values.append(f"{label} [{suffix}]" if suffix else label)
    return "; ".join(values) or "Not recorded"


def valuations_label(row: dict, *, include_source_ref: bool) -> str:
    """Render current valuation facts by basis with their supporting provenance."""
    values: list[str] = []
    for valuation in row.get("valuations", []):
        if not isinstance(valuation, dict):
            continue
        label = f"{text(valuation.get('basis'))}: {money(valuation.get('amount'), valuation.get('currency'))}"
        valued_on = text(valuation.get("valued_on"))
        provenance = provenance_label(valuation, include_source_ref=include_source_ref)
        suffix = "; ".join(
            part
            for part in (
                f"valued {valued_on}" if valued_on else "",
                provenance,
            )
            if part
        )
        values.append(f"{label} [{suffix}]" if suffix else label)
    return "; ".join(values) or "Not recorded"


def amendment_label(row: dict, *, include_source_ref: bool) -> str:
    """Render the newest visible identity/fact correction without exposing hidden history."""
    amendment = row.get("amendment")
    if not isinstance(amendment, dict):
        return "Not recorded"
    description = text(amendment.get("reason")).replace("_", " ") or "fact correction"
    amended_on = text(amendment.get("amended_on"))
    provenance = provenance_label(amendment, include_source_ref=include_source_ref)
    details = "; ".join(
        part for part in (f"amended {amended_on}" if amended_on else "", provenance) if part
    )
    return f"{description} [{details}]" if details else description


def receipt_label(row: dict) -> str:
    """Render the private receipt reference and attached receipt records."""
    values = [text(row.get("receipt_ref"))]
    values.extend(text(document.get("uri")) for document in row.get("receipt_documents", []))
    return "; ".join(value for value in values if value) or "Not recorded"


def json_label(value: object) -> str:
    """Render structured audit facts deterministically without losing their shape."""
    value = decode(value)
    if value is None:
        return "Not recorded"
    return json.dumps(value, ensure_ascii=False, separators=(",", ": "), sort_keys=True)


def audit_evidence_label(row: dict) -> str:
    """Render the complete private provenance for one amendment record."""
    label = provenance_label(row, include_source_ref=True)
    evidence_id = text(row.get("evidence_id"))
    return "; ".join(part for part in (evidence_id, label) if part) or "Not recorded"


def private_audit_history_table(inventory: list[dict]) -> str:
    """Render private-only amendment history attached to visible inventory items."""
    history = [
        entry
        for item in inventory
        for entry in item.get("audit_history", [])
    ]
    if not history:
        return "No item or fact amendments recorded."
    rows = []
    for entry in history:
        table_name = text(entry.get("table_name"))
        action = text(entry.get("action"))
        reason = text(entry.get("reason"))
        rows.append(
            {
                **entry,
                "item": f"{text(entry.get('item'))} ({text(entry.get('item_id'))})",
                "record": f"{text(entry.get('record_kind'))}: {text(entry.get('record_id'))}",
                "evidence": audit_evidence_label(entry),
                "change": "; ".join(
                    part
                    for part in (
                        f"table {table_name}" if table_name else "",
                        f"action {action}" if action else "",
                        f"reason {reason}" if reason else "",
                    )
                    if part
                )
                or "Not recorded",
                "selector": json_label(entry.get("selector")),
                "previous": json_label(entry.get("previous")),
                "replacement_or_changes": json_label(
                    entry.get("replacement_or_changes")
                ),
            }
        )
    rows.sort(
        key=lambda row: (
            text(row.get("amended_on")),
            text(row.get("recorded_at")),
            text(row.get("record_kind")),
            text(row.get("record_id")),
        )
    )
    return table(
        rows,
        [
            "Item",
            "Record",
            "Amended on",
            "Recorded at",
            "Actor",
            "Evidence",
            "Table, action, and reason",
            "Selector",
            "Previous",
            "Replacement or changes",
        ],
        [
            "item",
            "record",
            "amended_on",
            "recorded_at",
            "actor",
            "evidence",
            "change",
            "selector",
            "previous",
            "replacement_or_changes",
        ],
    )


def kit_review_label(row: dict, *, include_source_ref: bool) -> str:
    """Render only a current, visible kit review; never infer readiness from parts."""
    completeness = row.get("review_completeness")
    if completeness not in {"complete", "incomplete"}:
        return "Unknown"
    provenance = provenance_label(
        {
            "claim_strength": row.get("review_claim_strength"),
            "evidence_type": row.get("review_evidence_type"),
            "recorded_at": row.get("review_recorded_at"),
            "source_ref": row.get("review_source_ref"),
        },
        include_source_ref=include_source_ref,
    )
    reviewed_on = text(row.get("reviewed_on"))
    details = "; ".join(
        part for part in (f"reviewed {reviewed_on}" if reviewed_on else "", provenance) if part
    )
    label = text(completeness).capitalize()
    return f"{label} [{details}]" if details else label


def state_label(state: str) -> str:
    labels = {
        "confirmed": "Confirmed",
        "lent": "Lent",
        "candidate": "Candidate",
        "unknown": "Unresolved",
        "not_owned": "Not owned",
        "disposed": "Disposed or sold",
        "refunded": "Refunded or cancelled",
        "planned": "Planned only",
    }
    return labels.get(state, state)


def item_tables(rows: list[dict], *, include_private_details: bool) -> str:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[text(row["category"]) or "Other"].append(row)

    blocks: list[str] = []
    for category in sorted(groups, key=str.casefold):
        blocks.extend(
            [
                f"### {category}",
                "",
                "| Item ID | Item | Qty | State | Location or container | Interfaces and specifications | Aliases and tags | Current dimensions | Current valuations | Evidence | Physical check"
                + (
                    " | Condition | Serial or lot | Acquired | Purchase value | Replacement value | Receipt | Latest correction | Notes |"
                    if include_private_details
                    else " |"
                ),
                "|---|---|---:|---|---|---|---|---|---|---|---|"
                + ("---|---|---|---|---|---|---|---|" if include_private_details else ""),
            ]
        )
        for row in sorted(groups[category], key=lambda value: (text(value["name"]).casefold(), value["item_id"])):
            details = "; ".join(
                part for part in (text(row["interfaces_json"]), text(row["specs_json"])) if part
            )
            evidence = f"{text(row['evidence_type']).replace('_', ' ')}; {text(row['claim_strength']).replace('_', ' ')}"
            blocks.append(
                "| "
                + " | ".join(
                    [
                        cell(row["item_id"]),
                        item_label(row, include_reference_url=include_private_details),
                        cell(quantity(row)),
                        cell(state_label(row["ownership_state"])),
                        cell(place(row)),
                        cell(details),
                        cell(aliases_and_tags(row, include_source_ref=include_private_details)),
                        cell(dimensions_label(row, include_source_ref=include_private_details)),
                        cell(valuations_label(row, include_source_ref=include_private_details)),
                        cell(evidence),
                        cell(row["verified_on"] or "Not physically verified"),
                    ]
                    + (
                        [
                            cell(row["condition"]),
                            cell(row["serial_or_lot"]),
                            cell(row["acquired_on"]),
                            cell(money(row["purchase_price"], row["purchase_currency"])),
                            cell(money(row["replacement_value"], row["value_currency"])),
                            cell(receipt_label(row)),
                            cell(amendment_label(row, include_source_ref=True)),
                            cell(row["notes"]),
                        ]
                        if include_private_details
                        else []
                    )
                )
                + " |"
            )
        blocks.append("")
    return "\n".join(blocks)


def table(rows: list[dict], headers: list[str], keys: list[str]) -> str:
    output = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    output.extend("| " + " | ".join(cell(row[key]) for key in keys) + " |" for row in rows)
    return "\n".join(output)


def render(
    inventory: list[dict],
    states: list[dict],
    relationships: list[dict],
    kits: list[dict],
    torque_paths: list[dict],
    owner: str,
    scope: str = "personal",
    created_on: str | None = None,
) -> str:
    include_private_details = scope == "private"
    digest = catalogue_digest(
        inventory, states, relationships, kits, torque_paths, scope
    )
    state_table = table(states, ["State", "Rows"], ["state", "rows"])
    relationship_headers = ["Subject", "Relationship", "Object", "Confidence"]
    relationship_keys = ["subject", "predicate", "object", "confidence"]
    if include_private_details:
        relationship_headers.extend(("Evidence", "Notes"))
        relationship_keys.extend(("source_ref", "notes"))
    relationship_table = table(
        relationships, relationship_headers, relationship_keys
    )
    kit_headers = [
        "Kit",
        "Serves",
        "Requirement",
        "Matched item",
        "Status",
        "Review completeness",
    ]
    kit_keys = [
        "kit",
        "serves",
        "requirement",
        "matched_item",
        "status",
        "review_completeness_label",
    ]
    if include_private_details:
        kit_headers.extend(("Evidence", "Notes"))
        kit_keys.extend(("source_ref", "notes"))
    kit_rows = [
        {
            **row,
            "review_completeness_label": kit_review_label(
                row, include_source_ref=include_private_details
            ),
        }
        for row in kits
    ]
    kit_table = table(kit_rows, kit_headers, kit_keys)
    torque_headers = ["Tool", "Output drive", "Range Nm", "Adapter", "Status"]
    torque_keys = ["tool", "output_drive", "range_nm", "adapter", "status"]
    if include_private_details:
        torque_headers.extend(("Evidence", "Notes"))
        torque_keys.extend(("source_ref", "notes"))
    torque_table = table(
        torque_paths, torque_headers, torque_keys
    )
    audit_section = (
        "## Private amendment audit\n\n"
        "This private-only history records the predecessor and replacement or change\n"
        "provenance for amendments relevant to the items above.\n\n"
        + private_audit_history_table(inventory)
        if include_private_details
        else ""
    )
    created_property = f"Created: {created_on}\n" if created_on is not None else ""
    return f'''---
type: "[[Reference Category]]"
subtype: inventory
summary: "Generated human view of the canonical physical-property inventory."
scope: "{scope}"
tags:
  - inventory
  - property
  - equipment
{created_property}---

# Property Inventory Catalogue

> [!important] Generated view
> The canonical inventory store is the sole authority for current physical-property state. This note is rebuilt from its SQLite index and must not be hand-edited. A merchant or finance record remains a candidate until delivery or explicit current confirmation.

<!-- canonical-inventory-sha256:{digest} scope:{scope} -->
<!-- canonical-inventory-owner-sha256:{owner} -->

## Live summary

This catalogue contains **{len(inventory)} item or stock rows** across **{len(states)} lifecycle states**.

{state_table}

## Before buying

1. Search by model, item ID, size, drive, valve, connector, standard, and function.
2. Check state, quantity, and location. Not recorded means unknown, not absent.
3. Check the structured relationships, kit requirements, and torque paths below.
4. Physically verify an unresolved candidate before buying a duplicate where practical.
5. Record an order as candidate evidence immediately. Promote it only after delivery or explicit current confirmation.

## Current canonical inventory

{item_tables(inventory, include_private_details=include_private_details)}

## Compatibility and configuration relationships

{relationship_table}

## Operational kits

{kit_table}

## Torque paths

{torque_table}

{audit_section}

## Provenance

This projection deliberately does not link to raw source, store, or research inputs. Canonical provenance remains in the private inventory instance.
'''


def fetch(
    con: sqlite3.Connection, query: str, parameters: tuple[object, ...] = ()
) -> list[dict]:
    return [dict(row) for row in con.execute(query, parameters)]


def inventory_rows(con: sqlite3.Connection, scope_rank: int) -> list[dict]:
    """Return visible items without revealing a location beneath a hidden ancestor."""
    def visible_location(identifier: str, name: str) -> str:
        return f"""
        CASE
          WHEN {identifier} IS NULL THEN NULL
          WHEN EXISTS (
            SELECT 1 FROM location_tree tree
            WHERE tree.origin_id={identifier}
              AND NOT ({sensitivity_predicate('tree.sensitivity')})
          ) THEN '[redacted]'
          ELSE {name}
        END
        """

    return fetch(
        con,
        f"""
        WITH RECURSIVE location_tree(
          origin_id, location_id, parent_location_id, sensitivity, trail
        ) AS (
          SELECT location_id, location_id, parent_location_id, sensitivity,
                 ',' || location_id || ','
          FROM locations
          UNION ALL
          SELECT tree.origin_id, parent.location_id, parent.parent_location_id,
                 parent.sensitivity, tree.trail || parent.location_id || ','
          FROM location_tree tree
          JOIN locations parent ON parent.location_id=tree.parent_location_id
          WHERE instr(tree.trail, ',' || parent.location_id || ',')=0
        )
        SELECT i.item_id, i.model_id, i.location_id, i.container_id,
               i.home_location_id, i.home_container_id,
               CASE WHEN {sensitivity_predicate('COALESCE(i.identity_sensitivity, i.sensitivity)')}
                    THEN m.name ELSE '[identity redacted]' END AS name,
               CASE WHEN {sensitivity_predicate('COALESCE(i.identity_sensitivity, i.sensitivity)')}
                    THEN m.category ELSE 'Identity redacted' END AS category,
               i.quantity, i.unit, i.ownership_state,
               {visible_location('i.location_id', 'location.name')} AS location,
               {visible_location('i.container_id', 'container.name')} AS container,
               {visible_location('COALESCE(i.container_id, i.location_id)', 'current_path.path_text')}
                    AS current_location_path,
               {visible_location('COALESCE(i.home_container_id, i.home_location_id)', 'home_path.path_text')}
                    AS home_location_path,
               i.verified_on, i.sensitivity,
               CASE WHEN {sensitivity_predicate('e.sensitivity')}
                    THEN e.evidence_type ELSE '[redacted]' END AS evidence_type,
               CASE WHEN {sensitivity_predicate('e.sensitivity')}
                    THEN e.claim_strength ELSE '[redacted]' END AS claim_strength,
               CASE WHEN {sensitivity_predicate('COALESCE(i.identity_sensitivity, i.sensitivity)')}
                    THEN m.interfaces_json ELSE '[]' END AS interfaces_json,
               CASE WHEN {sensitivity_predicate('COALESCE(i.identity_sensitivity, i.sensitivity)')}
                    THEN m.specs_json ELSE '{{}}' END AS specs_json,
               i.condition, i.serial_or_lot, i.acquired_on,
               i.purchase_price, i.purchase_currency,
               i.replacement_value, i.value_currency, i.receipt_ref,
               i.notes,
               CASE WHEN {sensitivity_predicate('COALESCE(i.identity_sensitivity, i.sensitivity)')}
                    THEN m.reference_url ELSE NULL END AS reference_url
        FROM items i
        JOIN models m ON m.model_id=i.model_id
        JOIN evidence e ON e.evidence_id=i.primary_evidence_id
        LEFT JOIN locations location ON location.location_id=i.location_id
        LEFT JOIN locations container ON container.location_id=i.container_id
        LEFT JOIN v_location_paths current_path
          ON current_path.location_id=COALESCE(i.container_id, i.location_id)
        LEFT JOIN v_location_paths home_path
          ON home_path.location_id=COALESCE(i.home_container_id, i.home_location_id)
        WHERE {sensitivity_predicate('i.sensitivity')}
        ORDER BY i.item_id
        """,
        (
            scope_rank,
            scope_rank,
            scope_rank,
            scope_rank,
            scope_rank,
            scope_rank,
            scope_rank,
            scope_rank,
            scope_rank,
            scope_rank,
            scope_rank,
            scope_rank,
        ),
    )


def _visible_inventory_details(
    con: sqlite3.Connection, inventory: list[dict], scope_rank: int
) -> None:
    """Attach scope-safe, evidence-backed item facts to already-visible item rows.

    This is intentionally separate from ``inventory_rows``.  A future generic
    fact/amendment ledger can replace only the current-fact queries below while
    leaving the projection, scope rules, and digest contract unchanged.
    """
    if not inventory:
        return
    placeholders = ",".join("?" for _ in inventory)
    item_ids = tuple(row["item_id"] for row in inventory)
    details = {row["item_id"]: row for row in inventory}
    for row in inventory:
        row["aliases"] = []
        row["tags"] = []
        row["dimensions"] = None
        row["valuations"] = []
        row["amendment"] = None
        row["receipt_documents"] = []

    aliases = fetch(
        con,
        f"""
        SELECT a.item_id, a.alias, a.alias_kind, e.evidence_type,
               e.claim_strength, e.captured_on, e.source_ref
        FROM aliases a
        JOIN items i ON i.item_id=a.item_id
        JOIN evidence e ON e.evidence_id=a.evidence_id
        WHERE a.item_id IN ({placeholders})
          AND {sensitivity_predicate('a.sensitivity')}
          AND {sensitivity_predicate('e.sensitivity')}
          AND {sensitivity_predicate('COALESCE(i.identity_sensitivity, i.sensitivity)')}
        ORDER BY a.item_id, a.alias COLLATE NOCASE, a.alias_id
        """,
        (*item_ids, scope_rank, scope_rank, scope_rank),
    )
    for alias in aliases:
        details[alias["item_id"]]["aliases"].append(alias)

    tags = fetch(
        con,
        f"""
        SELECT t.item_id, t.tag, e.evidence_type, e.claim_strength,
               e.captured_on, e.source_ref
        FROM item_tags t
        JOIN items i ON i.item_id=t.item_id
        JOIN evidence e ON e.evidence_id=t.evidence_id
        WHERE t.item_id IN ({placeholders})
          AND {sensitivity_predicate('t.sensitivity')}
          AND {sensitivity_predicate('e.sensitivity')}
          AND {sensitivity_predicate('COALESCE(i.identity_sensitivity, i.sensitivity)')}
        ORDER BY t.item_id, t.tag COLLATE NOCASE
        """,
        (*item_ids, scope_rank, scope_rank, scope_rank),
    )
    for tag in tags:
        details[tag["item_id"]]["tags"].append(tag)

    # Select the latest fact for each axis before applying scope.  Falling back
    # to an older visible reading would present a stale physical dimension as
    # current.  Axes retain their own unit and provenance, matching the CLI's
    # per-axis composition without inventing a common unit for partial facts.
    dimension_rows = fetch(
        con,
        f"""
        SELECT d.dimension_id, d.item_id, d.width, d.height, d.depth, d.unit,
               d.measured_on, d.recorded_at, d.sensitivity AS record_sensitivity,
               e.evidence_type, e.claim_strength, e.captured_on, e.source_ref,
               e.sensitivity AS evidence_sensitivity
        FROM item_dimensions d
        JOIN evidence e ON e.evidence_id=d.evidence_id
        WHERE d.item_id IN ({placeholders})
        ORDER BY d.item_id, d.measured_on, d.recorded_at, d.dimension_id
        """,
        item_ids,
    )
    per_item_dimensions: dict[str, list[dict]] = defaultdict(list)
    for dimension in dimension_rows:
        per_item_dimensions[dimension["item_id"]].append(dimension)
    for item_id, readings in per_item_dimensions.items():
        axes: dict[str, dict] = {}
        for axis in ("width", "height", "depth"):
            candidates = [reading for reading in readings if reading[axis] is not None]
            if not candidates:
                continue
            latest = max(
                candidates,
                key=lambda reading: (
                    reading["measured_on"],
                    reading["recorded_at"],
                    reading["dimension_id"],
                ),
            )
            if (
                SENSITIVITY_RANK[latest["record_sensitivity"]] <= scope_rank
                and SENSITIVITY_RANK[latest["evidence_sensitivity"]] <= scope_rank
            ):
                axes[axis] = latest
        if axes:
            details[item_id]["dimensions"] = {"axes": axes}

    # Select the latest valuation for each basis before applying visibility.
    # Older public values must not masquerade as the current valuation when a
    # newer private valuation exists.
    valuations = fetch(
        con,
        f"""
        WITH current_valuations AS (
          SELECT v.*
          FROM valuations v
          WHERE NOT EXISTS (
            SELECT 1 FROM valuations newer
            WHERE newer.item_id=v.item_id AND newer.basis=v.basis
              AND (newer.valued_on > v.valued_on
                   OR (newer.valued_on=v.valued_on
                       AND newer.valuation_id > v.valuation_id))
          )
        )
        SELECT v.item_id, v.amount, v.currency, v.valued_on, v.basis,
               e.evidence_type, e.claim_strength, e.captured_on, e.source_ref
        FROM current_valuations v
        JOIN evidence e ON e.evidence_id=v.evidence_id
        WHERE v.item_id IN ({placeholders})
          AND {sensitivity_predicate('v.sensitivity')}
          AND {sensitivity_predicate('e.sensitivity')}
        ORDER BY v.item_id, v.basis COLLATE NOCASE, v.valued_on, v.valuation_id
        """,
        (*item_ids, scope_rank, scope_rank),
    )
    for valuation in valuations:
        details[valuation["item_id"]]["valuations"].append(valuation)

    # The latest correction is an indicator, not a replay of hidden history.
    amendments = fetch(
        con,
        f"""
        WITH current_amendments AS (
          SELECT a.*
          FROM item_amendments a
          WHERE NOT EXISTS (
            SELECT 1 FROM item_amendments newer
            WHERE newer.item_id=a.item_id
              AND (newer.amended_on > a.amended_on
                   OR (newer.amended_on=a.amended_on
                       AND (newer.recorded_at > a.recorded_at
                            OR (newer.recorded_at=a.recorded_at
                                AND newer.amendment_id > a.amendment_id))))
          )
        )
        SELECT a.item_id, a.amended_on, a.recorded_at, a.reason, e.evidence_type,
               e.claim_strength, e.captured_on, e.source_ref
        FROM current_amendments a
        JOIN evidence e ON e.evidence_id=a.evidence_id
        WHERE a.item_id IN ({placeholders})
          AND {sensitivity_predicate('e.sensitivity')}
        ORDER BY a.item_id
        """,
        (*item_ids, scope_rank),
    )
    for amendment in amendments:
        details[amendment["item_id"]]["amendment"] = amendment

    # Item documents have no independent sensitivity field, so only the
    # private projection can reveal their opaque receipt references.
    if scope_rank == SCOPE_MAX_SENSITIVITY["private"]:
        receipts = fetch(
            con,
            f"""
            SELECT item_id, uri
            FROM item_documents
            WHERE item_id IN ({placeholders}) AND document_type='receipt'
            ORDER BY item_id, captured_on, document_id
            """,
            item_ids,
        )
        for receipt in receipts:
            details[receipt["item_id"]]["receipt_documents"].append(receipt)


def _attach_private_audit_history(
    con: sqlite3.Connection, inventory: list[dict]
) -> None:
    """Attach every amendment materially relevant to an item for the private view.

    The normal catalogue intentionally shows only a current correction indicator.
    The audit trail is private because predecessor values, actors, source references,
    and retracted facts are all sensitive historical context.
    """
    if not inventory:
        return
    item_ids = tuple(row["item_id"] for row in inventory)
    placeholders = ",".join("?" for _ in item_ids)
    items = {row["item_id"]: row for row in inventory}
    item_models = {row["item_id"]: row["model_id"] for row in inventory}
    model_ids = set(item_models.values())
    location_parents = {
        row["location_id"]: row["parent_location_id"]
        for row in fetch(con, "SELECT location_id, parent_location_id FROM locations")
    }
    item_locations: dict[str, set[str]] = {}
    for row in inventory:
        locations: set[str] = set()
        for location_id in (
            row.get("location_id"),
            row.get("container_id"),
            row.get("home_location_id"),
            row.get("home_container_id"),
        ):
            while location_id is not None and location_id not in locations:
                locations.add(location_id)
                location_id = location_parents.get(location_id)
        item_locations[row["item_id"]] = locations
    kit_serves = {
        row["kit_id"]: row["serves_item_id"]
        for row in fetch(con, "SELECT kit_id, serves_item_id FROM kits")
    }
    for item in inventory:
        item["audit_history"] = []

    detail_amendments = fetch(
        con,
        f"""
        SELECT a.detail_amendment_id, a.item_id, a.amended_on, a.recorded_at,
               a.actor, a.evidence_id, a.previous_json, a.changes_json,
               a.notes, e.evidence_type, e.claim_strength, e.captured_on,
               e.source_ref
        FROM item_detail_amendments a
        JOIN evidence e ON e.evidence_id=a.evidence_id
        WHERE a.item_id IN ({placeholders})
        ORDER BY a.amended_on, a.recorded_at, a.detail_amendment_id
        """,
        item_ids,
    )
    for amendment in detail_amendments:
        items[amendment["item_id"]]["audit_history"].append(
            {
                "item_id": amendment["item_id"],
                "item": items[amendment["item_id"]]["name"],
                "record_kind": "item_detail_amendments",
                "record_id": amendment["detail_amendment_id"],
                "table_name": "items",
                "action": "change",
                "reason": None,
                "selector": {"item_id": amendment["item_id"]},
                "amended_on": amendment["amended_on"],
                "recorded_at": amendment["recorded_at"],
                "actor": amendment["actor"],
                "evidence_id": amendment["evidence_id"],
                "evidence_type": amendment["evidence_type"],
                "claim_strength": amendment["claim_strength"],
                "captured_on": amendment["captured_on"],
                "source_ref": amendment["source_ref"],
                "previous": decode(amendment["previous_json"]),
                "replacement_or_changes": decode(amendment["changes_json"]),
            }
        )

    def relates_to_item(
        amendment: dict, previous: dict | None, replacement: dict | None
    ) -> set[str]:
        related: set[str] = set()
        table_name = amendment["table_name"]
        for fact in (previous, replacement):
            if not isinstance(fact, dict):
                continue
            item_id = fact.get("item_id")
            if item_id in items:
                related.add(item_id)
            if table_name == "relationships":
                related.update(
                    item_id
                    for item_id in (fact.get("subject_item_id"), fact.get("object_item_id"))
                    if item_id in items
                )
            elif table_name == "torque_paths" and fact.get("tool_item_id") in items:
                related.add(fact["tool_item_id"])
            elif table_name == "kits" and fact.get("serves_item_id") in items:
                related.add(fact["serves_item_id"])
            elif table_name == "model_interfaces" and fact.get("model_id") in model_ids:
                related.update(
                    item_id
                    for item_id, model_id in item_models.items()
                    if model_id == fact["model_id"]
                )
            elif table_name == "kit_requirements":
                served_item_id = kit_serves.get(fact.get("kit_id"))
                if served_item_id in items:
                    related.add(served_item_id)
            elif table_name in {"locations", "spatial_profiles"}:
                location_id = fact.get("location_id")
                if location_id is not None:
                    related.update(
                        item_id
                        for item_id, locations in item_locations.items()
                        if location_id in locations
                    )
        return related

    fact_amendments = fetch(
        con,
        """
        SELECT a.fact_amendment_id, a.table_name, a.selector_json, a.amended_on,
               a.recorded_at, a.actor, a.evidence_id, a.action, a.previous_json,
               a.replacement_json, a.reason, e.evidence_type, e.claim_strength,
               e.captured_on, e.source_ref
        FROM fact_amendments a
        JOIN evidence e ON e.evidence_id=a.evidence_id
        ORDER BY a.amended_on, a.recorded_at, a.fact_amendment_id
        """,
    )
    for amendment in fact_amendments:
        previous = decode(amendment["previous_json"])
        replacement = decode(amendment["replacement_json"])
        for item_id in relates_to_item(amendment, previous, replacement):
            items[item_id]["audit_history"].append(
                {
                    "item_id": item_id,
                    "item": items[item_id]["name"],
                    "record_kind": "fact_amendments",
                    "record_id": amendment["fact_amendment_id"],
                    "table_name": amendment["table_name"],
                    "action": amendment["action"],
                    "reason": amendment["reason"],
                    "selector": decode(amendment["selector_json"]),
                    "amended_on": amendment["amended_on"],
                    "recorded_at": amendment["recorded_at"],
                    "actor": amendment["actor"],
                    "evidence_id": amendment["evidence_id"],
                    "evidence_type": amendment["evidence_type"],
                    "claim_strength": amendment["claim_strength"],
                    "captured_on": amendment["captured_on"],
                    "source_ref": amendment["source_ref"],
                    "previous": previous,
                    "replacement_or_changes": replacement,
                }
            )


def _attach_visible_kit_reviews(
    con: sqlite3.Connection, kits: list[dict], scope_rank: int
) -> None:
    """Attach a visible canonical latest, still-current completeness review to each kit row."""
    if not kits:
        return
    kit_ids = tuple(sorted({row["kit_id"] for row in kits}))
    placeholders = ",".join("?" for _ in kit_ids)
    requirement_keys: dict[str, list[str]] = defaultdict(list)
    for requirement in fetch(
        con,
        f"""
        SELECT kit_id, requirement_key
        FROM kit_requirements
        WHERE kit_id IN ({placeholders})
        ORDER BY kit_id, requirement_key
        """,
        kit_ids,
    ):
        requirement_keys[requirement["kit_id"]].append(requirement["requirement_key"])
    # Determine the canonical latest review before visibility filtering.  A
    # lower scope must not revive an older review after a newer hidden review.
    reviews = fetch(
        con,
        f"""
        SELECT r.kit_id, r.completeness, r.reviewed_on, r.recorded_at,
               r.requirement_keys_json, r.sensitivity AS review_sensitivity,
               e.evidence_type, e.claim_strength, e.source_ref,
               e.sensitivity AS evidence_sensitivity
        FROM kit_reviews r
        JOIN evidence e ON e.evidence_id=r.evidence_id
        WHERE r.kit_id IN ({placeholders})
        ORDER BY r.kit_id, r.reviewed_on, r.recorded_at, r.review_id
        """,
        kit_ids,
    )
    latest: dict[str, dict] = {}
    for review in reviews:
        latest[review["kit_id"]] = review
    for row in kits:
        row.update(
            {
                "review_completeness": None,
                "reviewed_on": None,
                "review_recorded_at": None,
                "review_evidence_type": None,
                "review_claim_strength": None,
                "review_source_ref": None,
            }
        )
        review = latest.get(row["kit_id"])
        if (
            review is None
            or SENSITIVITY_RANK[review["review_sensitivity"]] > scope_rank
            or SENSITIVITY_RANK[review["evidence_sensitivity"]] > scope_rank
            or any(
                kit_row["kit_id"] == row["kit_id"]
                and text(kit_row["status"]).casefold() == "unknown"
                for kit_row in kits
            )
        ):
            continue
        reviewed_keys = decode(review["requirement_keys_json"])
        if (
            not isinstance(reviewed_keys, list)
            or sorted(reviewed_keys) != requirement_keys.get(row["kit_id"], [])
        ):
            continue
        row.update(
            {
                "review_completeness": review["completeness"],
                "reviewed_on": review["reviewed_on"],
                "review_recorded_at": review["recorded_at"],
                "review_evidence_type": review["evidence_type"],
                "review_claim_strength": review["claim_strength"],
                "review_source_ref": review["source_ref"],
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=DATABASE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", choices=("public", "personal", "private"), default="personal")
    parser.add_argument("--installation-id")
    parser.add_argument("--owner-digest")
    parser.add_argument("--created-on")
    args = parser.parse_args()
    scope_rank = SCOPE_MAX_SENSITIVITY[args.scope]

    con = sqlite3.connect(args.database)
    con.row_factory = sqlite3.Row
    metadata = fetch(con, "SELECT inventory_id FROM metadata")
    if len(metadata) != 1:
        raise ValueError("inventory metadata must contain exactly one inventory_id")
    if args.owner_digest is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", args.owner_digest):
            raise ValueError("owner digest must be a lowercase SHA-256 value")
        owner = args.owner_digest
    else:
        owner = inventory_owner_digest(
            args.installation_id or metadata[0]["inventory_id"]
        )
    inventory = inventory_rows(con, scope_rank)
    _visible_inventory_details(con, inventory, scope_rank)
    if args.scope == "private":
        _attach_private_audit_history(con, inventory)
    states = fetch(
        con,
        f"""
        SELECT ownership_state AS state, count(*) AS rows
        FROM items
        WHERE {sensitivity_predicate('sensitivity')}
        GROUP BY ownership_state
        ORDER BY ownership_state
        """,
        (scope_rank,),
    )
    relationships = fetch(
        con,
        f"""
        SELECT sm.name AS subject, replace(r.predicate, '_', ' ') AS predicate,
               om.name AS object, r.confidence, e.source_ref, coalesce(r.notes, '') AS notes
        FROM relationships r
        JOIN items si ON si.item_id=r.subject_item_id
        JOIN models sm ON sm.model_id=si.model_id
        JOIN items oi ON oi.item_id=r.object_item_id
        JOIN models om ON om.model_id=oi.model_id
        JOIN evidence e ON e.evidence_id=r.evidence_id
        WHERE {sensitivity_predicate('si.sensitivity')}
          AND {sensitivity_predicate('oi.sensitivity')}
          AND {sensitivity_predicate('e.sensitivity')}
          AND {sensitivity_predicate('COALESCE(si.identity_sensitivity, si.sensitivity)')}
          AND {sensitivity_predicate('COALESCE(oi.identity_sensitivity, oi.sensitivity)')}
        ORDER BY sm.name, r.predicate, om.name
        """,
        (scope_rank, scope_rank, scope_rank, scope_rank, scope_rank),
    )
    kits = fetch(
        con,
        f"""
        SELECT k.kit_id, k.name AS kit, served.name AS serves, replace(kr.requirement_key, '_', ' ') AS requirement,
               coalesce(matched.name, '') AS matched_item,
               matched_item.item_id AS matched_item_id,
               kr.status AS recorded_status, matched_item.condition AS matched_condition,
               CASE
                 WHEN kr.status IN ('source_present', 'exists_unassigned')
                      AND (
                        matched_item.item_id IS NULL
                        OR matched_item.ownership_state != 'confirmed'
                        OR COALESCE(
                          (SELECT max(event.sequence) FROM inventory_events event
                           WHERE event.item_id=matched_item.item_id),
                          -1
                        ) != kr.verified_event_sequence
                      )
                   THEN 'Unknown'
                 ELSE replace(kr.status, '_', ' ')
               END AS status,
               e.source_ref, coalesce(kr.notes, '') AS notes
        FROM kit_requirements kr
        JOIN kits k ON k.kit_id=kr.kit_id
        JOIN evidence ke ON ke.evidence_id=k.evidence_id
        JOIN items served_item ON served_item.item_id=k.serves_item_id
        JOIN models served ON served.model_id=served_item.model_id
        LEFT JOIN items matched_item ON matched_item.item_id=kr.item_id
        LEFT JOIN models matched ON matched.model_id=matched_item.model_id
        JOIN evidence e ON e.evidence_id=kr.evidence_id
        WHERE {sensitivity_predicate('served_item.sensitivity')}
          AND (matched_item.item_id IS NULL OR {sensitivity_predicate('matched_item.sensitivity')})
          AND {sensitivity_predicate('ke.sensitivity')}
          AND {sensitivity_predicate('e.sensitivity')}
          AND {sensitivity_predicate('COALESCE(served_item.identity_sensitivity, served_item.sensitivity)')}
          AND (
            matched_item.item_id IS NULL
            OR {sensitivity_predicate('COALESCE(matched_item.identity_sensitivity, matched_item.sensitivity)')}
          )
        ORDER BY k.name, kr.requirement_key
        """,
        (scope_rank, scope_rank, scope_rank, scope_rank, scope_rank, scope_rank),
    )
    for kit in kits:
        kit["matched_condition"] = visible_projected_item_detail(
            con,
            kit.pop("matched_item_id"),
            "condition",
            kit["matched_condition"],
            scope_rank,
        )
        if (
            kit["recorded_status"] == "source_present"
            and not explicitly_usable_condition(kit["matched_condition"])
        ):
            kit["status"] = "Unknown"
    _attach_visible_kit_reviews(con, kits, scope_rank)
    torque_paths = fetch(
        con,
        f"""
        SELECT m.name AS tool, tp.output_drive,
               CASE WHEN tp.min_torque_nm IS NULL OR tp.max_torque_nm IS NULL
                    THEN '' ELSE printf('%g-%g', tp.min_torque_nm, tp.max_torque_nm) END AS range_nm,
               coalesce(tp.adapter_description, '') AS adapter,
               replace(tp.status, '_', ' ') AS status, e.source_ref, coalesce(tp.notes, '') AS notes
        FROM torque_paths tp
        JOIN items i ON i.item_id=tp.tool_item_id
        JOIN models m ON m.model_id=i.model_id
        JOIN evidence e ON e.evidence_id=tp.evidence_id
        WHERE {sensitivity_predicate('i.sensitivity')}
          AND {sensitivity_predicate('e.sensitivity')}
          AND {sensitivity_predicate('COALESCE(i.identity_sensitivity, i.sensitivity)')}
        ORDER BY m.name, tp.output_drive
        """,
        (scope_rank, scope_rank, scope_rank),
    )
    con.close()

    if args.created_on is not None:
        created_on = validate_created_on(args.created_on)
    elif args.output.exists():
        created_on = catalogue_created_on(args.output.read_text())
    else:
        created_on = None
    note = render(
        inventory,
        states,
        relationships,
        kits,
        torque_paths,
        owner,
        args.scope,
        created_on or date.today().isoformat(),
    )
    write_output_atomic(args.output, note)
    print(f"rendered={args.output}")
    print(f"scope={args.scope}")
    print(f"inventory_rows={len(inventory)}")
    print(f"relationships={len(relationships)}")
    print(f"kit_requirements={len(kits)}")
    print(f"torque_paths={len(torque_paths)}")
    print(
        "canonical_digest="
        + catalogue_digest(
            inventory, states, relationships, kits, torque_paths, args.scope
        )
    )


if __name__ == "__main__":
    main()
