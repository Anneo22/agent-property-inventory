"""Pure, scope-safe insurance preparation primitives.

This module deliberately reads only supplied rows and bytes.  It never opens
or changes the canonical inventory, and it does not turn an absent observation
into a negative fact.  Integrations may use these functions to prepare a
reviewable export, but are responsible for supplying a validated store.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import stat
import zipfile
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from .json_codec import StrictJSONError
from .json_codec import loads as strict_json_loads
from .media_validation import MediaValidationError, validate_declared_media_bytes
from .retrieval import scope_visible_item_details
from .valuation_policy import valuation_evidence_supports_basis

SENSITIVITY_RANK = {"low": 0, "personal": 1, "high": 2}
SCOPE_MAX_SENSITIVITY = {"public": 0, "personal": 1, "private": 2}
PACKAGE_FORMAT = "property-inventory-insurance"
PACKAGE_VERSION = 1
_PACKAGE_FILES = ("items.csv", "items.json")
MAX_INSURANCE_PACKAGE_BYTES = 64 * 1024 * 1024
# Three fixed members plus a bounded set of evidence assets. 4096 keeps the
# central directory and canonical manifest comfortably below their byte caps
# while supporting photo-heavy inventories.
_MAX_MEMBERS = 4096
MAX_INSURANCE_MEMBER_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100
_MAX_MANIFEST_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CSV_DANGEROUS_PREFIXES = ("=", "+", "-", "@")


class InsuranceError(ValueError):
    """Raised when insurance evidence or an insurance package is unsafe."""


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _json_loads(value: bytes) -> object:
    try:
        return strict_json_loads(value, label="insurance package JSON")
    except StrictJSONError as error:
        raise InsuranceError("insurance package contains malformed JSON") from error


def _sha256(value: object) -> str | None:
    text = _text(value)
    return text if text is not None and _SHA256.fullmatch(text) else None


def _rows(rows: Mapping[str, Sequence[Mapping[str, Any]]], table: str) -> list[dict[str, Any]]:
    value = rows.get(table, ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise InsuranceError(f"{table} must be a sequence of records")
    result: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, Mapping):
            raise InsuranceError(f"{table} contains a non-record")
        result.append(dict(row))
    return result


def _scope(scope: object) -> int:
    if not isinstance(scope, str):
        raise InsuranceError("scope must be a string")
    try:
        return SCOPE_MAX_SENSITIVITY[scope]
    except KeyError as error:
        raise InsuranceError(f"unknown scope: {scope}") from error


def _visible(row: Mapping[str, Any], maximum_sensitivity: int, table: str) -> bool:
    sensitivity = row.get("sensitivity")
    try:
        return SENSITIVITY_RANK[sensitivity] <= maximum_sensitivity
    except KeyError as error:
        raise InsuranceError(f"{table} has invalid sensitivity") from error


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _valid_date(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return text if date.fromisoformat(text).isoformat() == text else None
    except ValueError:
        return None


def _assessment(present: bool, **details: object) -> dict[str, object]:
    """Represent unavailable evidence as unknown, never as a false assertion."""
    return {"state": "present", **details} if present else {"state": "unknown"}


def _location_assessment(
    item: Mapping[str, Any], locations: Mapping[str, Mapping[str, Any]], maximum: int
) -> dict[str, object]:
    # A container is the more useful insurance location when both references
    # are visible: "drawer in study" beats the broader "study" alone.
    references = (item.get("container_id"), item.get("location_id"))
    visible_paths: list[tuple[str, ...]] = []
    visible_names: list[tuple[str, ...]] = []
    for reference in references:
        if reference is None:
            continue
        current_id = reference
        path: list[str] = []
        names: list[str] = []
        visited: set[str] = set()
        while current_id is not None:
            if not isinstance(current_id, str) or current_id in visited:
                return _assessment(False)
            current = locations.get(current_id)
            if current is None or not _visible(current, maximum, "locations"):
                return _assessment(False)
            visited.add(current_id)
            if current.get("kind") == "unknown":
                break
            path.append(current_id)
            name = _text(current.get("name"))
            if name is None:
                return _assessment(False)
            names.append(name)
            current_id = current.get("parent_location_id")
        if path:
            visible_paths.append(tuple(path))
            visible_names.append(tuple(names))
    if not visible_paths:
        return _assessment(False)
    # The first location is more specific than its container.  Its IDs are all
    # already scope-filtered, so exposing them cannot reveal a hidden place.
    return _assessment(
        True,
        location_ids=list(visible_paths[0]),
        location_names=list(visible_names[0]),
    )


def _supported_evidence(
    item_id: str,
    links: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Mapping[str, Any]],
    maximum: int,
) -> dict[str, dict[str, Any]]:
    visible: dict[str, dict[str, Any]] = {}
    for link in links:
        if link.get("item_id") != item_id:
            continue
        evidence_id = link.get("evidence_id")
        if not isinstance(evidence_id, str):
            continue
        record = evidence.get(evidence_id)
        if record is not None and _visible(record, maximum, "evidence"):
            visible[evidence_id] = dict(record)
    return visible


def _evidence_quality(evidence: Mapping[str, Mapping[str, Any]]) -> dict[str, object]:
    if not evidence:
        return _assessment(False)
    observations = [
        {
            "evidence_id": evidence_id,
            "evidence_type": record.get("evidence_type"),
            "claim_strength": record.get("claim_strength"),
        }
        for evidence_id, record in sorted(evidence.items())
    ]
    # This is deliberately an observation, not an invented quality score.
    return _assessment(True, observations=observations)


def _valid_media_asset(asset: Mapping[str, Any]) -> bool:
    return (
        _text(asset.get("asset_id")) is not None
        and _sha256(asset.get("sha256")) is not None
        and isinstance(asset.get("byte_size"), int)
        and not isinstance(asset.get("byte_size"), bool)
        and asset["byte_size"] >= 0
        and _text(asset.get("media_type")) is not None
    )


def _visible_document(
    document: Mapping[str, Any], item: Mapping[str, Any], maximum: int
) -> dict[str, object] | None:
    """Return a concrete document fact, never an empty label masquerading as one."""
    # Canonical item_documents have no sensitivity field. Treat their URIs as
    # private context instead of silently inheriting the owning item's scope.
    if maximum < SENSITIVITY_RANK["high"]:
        return None
    document_type = _text(document.get("document_type"))
    uri = _text(document.get("uri"))
    if document_type is None or uri is None:
        return None
    fact: dict[str, object] = {"document_type": document_type, "uri": uri}
    document_id = _text(document.get("document_id"))
    if document_id is not None:
        fact["document_id"] = document_id
    return fact


def _visible_valuations(
    item_id: str,
    valuations: Sequence[Mapping[str, Any]],
    supported_evidence: Mapping[str, Mapping[str, Any]],
    maximum: int,
) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for valuation in valuations:
        if valuation.get("item_id") != item_id or not _visible(valuation, maximum, "valuations"):
            continue
        evidence_id = valuation.get("evidence_id")
        amount = valuation.get("amount")
        basis = valuation.get("basis")
        if (
            not isinstance(evidence_id, str)
            or evidence_id not in supported_evidence
            or isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(amount)
            or amount < 0
            or _text(valuation.get("currency")) is None
            or _text(basis) is None
            or _valid_date(valuation.get("valued_on")) is None
            or not valuation_evidence_supports_basis(supported_evidence[evidence_id], basis)
        ):
            continue
        valuation_id = _text(valuation.get("valuation_id"))
        if valuation_id is None:
            continue
        values.append(
            {
                "valuation_id": valuation_id,
                "amount": amount,
                "currency": valuation["currency"],
                "basis": valuation["basis"],
                "valued_on": valuation["valued_on"],
                "evidence_id": evidence_id,
            }
        )
    return sorted(values, key=lambda value: str(value["valuation_id"]))


def _insurance_document_media_type(asset: Mapping[str, Any]) -> bool:
    media_type = _text(asset.get("media_type"))
    if media_type is None:
        return False
    normalized = media_type.split(";", 1)[0].strip().casefold()
    return normalized == "application/pdf" or normalized.startswith("image/")


def _insurance_ready_valuations(values: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Replacement/appraisal values are usable; the rest remains audit context."""
    return [dict(value) for value in values if value.get("basis") in {"replacement", "appraisal"}]


def _item_assessment(
    item: Mapping[str, Any],
    model: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
    supported_evidence: Mapping[str, Mapping[str, Any]],
    values: list[dict[str, object]],
    valuation_context: list[dict[str, object]],
    locations: Mapping[str, Mapping[str, Any]],
    maximum: int,
    photo_evidence: list[dict[str, object]],
    receipt_evidence: bool,
    appraisal_evidence: bool,
) -> dict[str, object]:
    document_facts = [
        fact
        for document in documents
        if document.get("item_id") == item["item_id"]
        for fact in [_visible_document(document, item, maximum)]
        if fact is not None
    ]
    location_assessment = _location_assessment(item, locations, maximum)
    fields = {
        "photo": _assessment(bool(photo_evidence), evidence=photo_evidence),
        "serial": _assessment(
            _text(item.get("serial_or_lot")) is not None,
            serial_or_lot=_text(item.get("serial_or_lot")),
        )
        if _text(item.get("serial_or_lot")) is not None
        else _assessment(False),
        "value": _assessment(bool(values), valuations=values) if values else _assessment(False),
        "receipt": _assessment(receipt_evidence),
        "appraisal": _assessment(appraisal_evidence),
        "acquired_date": _assessment(
            _valid_date(item.get("acquired_on")) is not None,
            acquired_on=_valid_date(item.get("acquired_on")),
        )
        if _valid_date(item.get("acquired_on")) is not None
        else _assessment(False),
        "location": location_assessment,
        "evidence_quality": _evidence_quality(supported_evidence),
        "custody": _assessment(
            item.get("ownership_state") == "confirmed"
            or (item.get("ownership_state") == "lent" and location_assessment["state"] == "present")
        ),
    }
    gaps = [name for name, assessment in fields.items() if assessment["state"] == "unknown"]
    identity_sensitivity = item.get("identity_sensitivity") or item.get("sensitivity")
    identity_visible = (
        isinstance(identity_sensitivity, str)
        and identity_sensitivity in SENSITIVITY_RANK
        and SENSITIVITY_RANK[identity_sensitivity] <= maximum
    )
    model_name = (
        _text(model.get("name")) or _text(model.get("model")) or item["model_id"]
        if identity_visible
        else "[identity redacted]"
    )
    return {
        "item_id": item["item_id"],
        "model": model_name,
        "category": model.get("category") if identity_visible else None,
        "ownership_state": item.get("ownership_state"),
        "readiness": "ready" if not gaps else "not_ready",
        "gaps": gaps,
        "fields": fields,
        "documents": sorted(
            document_facts, key=lambda fact: (str(fact["document_type"]), str(fact["uri"]))
        ),
        "valuation_context": valuation_context,
    }


def insurance_report(
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    scope: str = "private",
    verified_media_asset_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, object]:
    """Return deterministic, evidence-safe readiness facts for confirmed visible items.

    No missing field is converted into a value, a boolean false, or a value in
    another currency.  Valuations are included only when their evidence is
    visibly linked to the item.
    """
    maximum = _scope(scope)
    if verified_media_asset_ids is None:
        verified_media_asset_ids = frozenset()
    if not isinstance(verified_media_asset_ids, (set, frozenset)) or any(
        not isinstance(asset_id, str) or not asset_id for asset_id in verified_media_asset_ids
    ):
        raise InsuranceError("verified media asset ids must be a string set")
    items = _rows(rows, "items")
    models = {
        row.get("model_id"): row
        for row in _rows(rows, "models")
        if isinstance(row.get("model_id"), str)
    }
    evidence = {
        row.get("evidence_id"): row
        for row in _rows(rows, "evidence")
        if isinstance(row.get("evidence_id"), str)
    }
    locations = {
        row.get("location_id"): row
        for row in _rows(rows, "locations")
        if isinstance(row.get("location_id"), str)
    }
    links = _rows(rows, "item_evidence")
    documents = _rows(rows, "item_documents")
    valuations = _rows(rows, "valuations")
    assets = {
        row.get("asset_id"): row
        for row in _rows(rows, "media_assets")
        if isinstance(row.get("asset_id"), str)
    }
    asset_links = _rows(rows, "evidence_assets")
    visible_items = sorted(
        (
            {**item, **scope_visible_item_details(rows, item, scope)}
            for item in items
            if _visible(item, maximum, "items")
            and item.get("ownership_state") in {"confirmed", "lent"}
            and isinstance(item.get("item_id"), str)
        ),
        key=lambda item: str(item["item_id"]),
    )
    report_items: list[dict[str, object]] = []
    exported_assets: dict[str, dict[str, object]] = {}
    exported_evidence: dict[str, dict[str, object]] = {}
    exported_item_evidence: set[tuple[str, str, str]] = set()
    exported_evidence_assets: set[tuple[str, str, str]] = set()
    for item in visible_items:
        model_id = item.get("model_id")
        model = models.get(model_id)
        if model is None:
            raise InsuranceError(f"visible item has no model: {item['item_id']}")
        supported = _supported_evidence(item["item_id"], links, evidence, maximum)
        for evidence_id, record in supported.items():
            exported_evidence[evidence_id] = {
                "evidence_id": evidence_id,
                "evidence_type": record.get("evidence_type"),
                "claim_strength": record.get("claim_strength"),
            }
            matching_links = [
                link
                for link in links
                if link.get("item_id") == item["item_id"] and link.get("evidence_id") == evidence_id
            ]
            if len(matching_links) != 1 or matching_links[0].get("role") not in {
                "primary",
                "supporting",
            }:
                raise InsuranceError(
                    f"visible item has invalid evidence role: {item['item_id']} / {evidence_id}"
                )
            exported_item_evidence.add((item["item_id"], evidence_id, matching_links[0]["role"]))
        for link in asset_links:
            evidence_id = link.get("evidence_id")
            asset_id = link.get("asset_id")
            if evidence_id not in supported or not isinstance(asset_id, str):
                continue
            asset = assets.get(asset_id)
            if (
                asset is not None
                and _visible(asset, maximum, "media_assets")
                and _valid_media_asset(asset)
            ):
                digest = _sha256(asset.get("sha256"))
                size = asset.get("byte_size")
                role = _text(link.get("role"))
                if (
                    digest is not None
                    and isinstance(size, int)
                    and not isinstance(size, bool)
                    and role is not None
                ):
                    exported_assets[asset_id] = {
                        "asset_id": asset_id,
                        "sha256": digest,
                        "byte_size": size,
                        "media_type": asset.get("media_type"),
                    }
                    exported_evidence_assets.add((str(evidence_id), asset_id, role))
        valuation_context = _visible_valuations(item["item_id"], valuations, supported, maximum)
        values = _insurance_ready_valuations(valuation_context)
        # Capture artifacts/reviews are not exported with an insurance package.
        # A five-field assertion could not prove a particular observation chose
        # this particular image crop after the package leaves the canonical
        # store. Until the package carries that complete immutable lineage,
        # only direct current physical-check imagery can qualify as insurance
        # photography.
        current_physical_photo_evidence = {
            evidence_id
            for evidence_id, record in supported.items()
            if record.get("evidence_type") == "physical_check"
            and record.get("claim_strength") == "explicit_current"
        }
        photo_qualifications: dict[str, dict[str, object]] = {}
        for link in asset_links:
            evidence_id = link.get("evidence_id")
            asset_id = link.get("asset_id")
            asset = assets.get(asset_id)
            role = link.get("role")
            if (
                not isinstance(evidence_id, str)
                or not isinstance(asset_id, str)
                or asset_id not in verified_media_asset_ids
                or asset is None
                or not _visible(asset, maximum, "media_assets")
                or not _valid_media_asset(asset)
                or not isinstance(asset.get("media_type"), str)
                or not asset["media_type"].startswith("image/")
            ):
                continue
            if evidence_id in current_physical_photo_evidence and role in {
                "source",
                "crop",
                "manual",
            }:
                photo_qualifications[evidence_id] = {
                    "evidence_id": evidence_id,
                    "qualification": "current_physical_check",
                }
        photo_evidence = [
            qualification for _evidence_id, qualification in sorted(photo_qualifications.items())
        ]
        receipt_evidence = any(
            link.get("role") == "receipt"
            and link.get("asset_id") in verified_media_asset_ids
            and assets.get(link.get("asset_id")) is not None
            and _visible(assets[link["asset_id"]], maximum, "media_assets")
            and _valid_media_asset(assets[link["asset_id"]])
            and _insurance_document_media_type(assets[link["asset_id"]])
            and supported[link["evidence_id"]].get("claim_strength") == "purchase_only"
            and supported[link["evidence_id"]].get("evidence_type")
            in {"merchant_account", "user_source"}
            for link in asset_links
            if link.get("evidence_id") in supported
        )
        appraisal_evidence_ids = {
            value["evidence_id"] for value in valuation_context if value.get("basis") == "appraisal"
        }
        appraisal_evidence = any(
            link.get("role") == "appraisal"
            and link.get("evidence_id") in appraisal_evidence_ids
            and link.get("asset_id") in verified_media_asset_ids
            and assets.get(link.get("asset_id")) is not None
            and _visible(assets[link["asset_id"]], maximum, "media_assets")
            and _valid_media_asset(assets[link["asset_id"]])
            and _insurance_document_media_type(assets[link["asset_id"]])
            for link in asset_links
        )
        report_items.append(
            _item_assessment(
                item,
                model,
                documents,
                supported,
                values,
                valuation_context,
                locations,
                maximum,
                photo_evidence,
                receipt_evidence,
                appraisal_evidence,
            )
        )
    report_items.sort(key=lambda item: str(item["item_id"]))
    return {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "scope": scope,
        "summary": {
            "item_count": len(report_items),
            "ready_count": sum(item["readiness"] == "ready" for item in report_items),
            "not_ready_count": sum(item["readiness"] == "not_ready" for item in report_items),
        },
        "items": report_items,
        "media_assets": sorted(exported_assets.values(), key=lambda asset: str(asset["asset_id"])),
        "evidence": sorted(
            exported_evidence.values(), key=lambda record: str(record["evidence_id"])
        ),
        "item_evidence": [
            {"item_id": item_id, "evidence_id": evidence_id, "role": role}
            for item_id, evidence_id, role in sorted(exported_item_evidence)
        ],
        "evidence_assets": [
            {"evidence_id": evidence_id, "asset_id": asset_id, "role": role}
            for evidence_id, asset_id, role in sorted(exported_evidence_assets)
        ],
        # Kept as an empty compatibility field for version-1 readers. Its
        # non-empty form was self-asserted and is rejected by the validator.
        "capture_photo_proofs": [],
    }


def _csv_bytes(report: Mapping[str, Any]) -> bytes:
    def csv_value(value: object) -> object:
        if not isinstance(value, str):
            return value
        return "'" + value if value.lstrip().startswith(_CSV_DANGEROUS_PREFIXES) else value

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=(
            "item_id",
            "model",
            "category",
            "ownership_state",
            "readiness",
            "gaps",
            "photo_state",
            "serial_state",
            "serial_or_lot",
            "value_state",
            "valuations_json",
            "receipt_state",
            "appraisal_state",
            "acquired_date_state",
            "acquired_on",
            "location_state",
            "location_ids_json",
            "location_names_json",
            "evidence_quality_state",
            "custody_state",
            "evidence_observations_json",
            "item_json",
        ),
        lineterminator="\n",
    )
    writer.writeheader()
    for item in report["items"]:
        fields = item["fields"]
        writer.writerow(
            {
                "item_id": csv_value(item["item_id"]),
                "model": csv_value(item["model"]),
                "category": csv_value(item["category"] or ""),
                "ownership_state": item["ownership_state"],
                "readiness": csv_value(item["readiness"]),
                "gaps": csv_value(";".join(item["gaps"])),
                "photo_state": fields["photo"]["state"],
                "serial_state": fields["serial"]["state"],
                "serial_or_lot": csv_value(fields["serial"].get("serial_or_lot", "")),
                "value_state": fields["value"]["state"],
                "valuations_json": json.dumps(
                    fields["value"].get("valuations", []),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                "receipt_state": fields["receipt"]["state"],
                "appraisal_state": fields["appraisal"]["state"],
                "acquired_date_state": fields["acquired_date"]["state"],
                "acquired_on": csv_value(fields["acquired_date"].get("acquired_on", "")),
                "location_state": fields["location"]["state"],
                "location_ids_json": json.dumps(
                    fields["location"].get("location_ids", []),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "location_names_json": json.dumps(
                    fields["location"].get("location_names", []),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "evidence_quality_state": fields["evidence_quality"]["state"],
                "custody_state": fields["custody"]["state"],
                "evidence_observations_json": json.dumps(
                    fields["evidence_quality"].get("observations", []),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                "item_json": json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
            }
        )
    return output.getvalue().encode("utf-8")


def _digest(value: bytes) -> dict[str, object]:
    return {"sha256": hashlib.sha256(value).hexdigest(), "byte_size": len(value)}


def _zip_member(archive: zipfile.ZipFile, name: str, value: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, value)


def _require_exact_keys(value: Mapping[str, object], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise InsuranceError(f"insurance report has invalid {label} schema")


def _validate_assessment(value: object, name: str) -> None:
    if not isinstance(value, Mapping) or not isinstance(value.get("state"), str):
        raise InsuranceError(f"insurance report has invalid {name} assessment")
    state = value["state"]
    if state not in {"present", "unknown"}:
        raise InsuranceError(f"insurance report has invalid {name} assessment")
    if state == "unknown" and set(value) != {"state"}:
        raise InsuranceError(f"insurance report has invalid unknown {name} assessment")


def _validate_report(report: Mapping[str, Any]) -> None:
    """Validate the complete serialisable report schema before any archive I/O."""
    _require_exact_keys(
        report,
        {
            "format",
            "version",
            "scope",
            "summary",
            "items",
            "media_assets",
            "evidence",
            "item_evidence",
            "evidence_assets",
            "capture_photo_proofs",
        },
        "top-level",
    )
    if report.get("format") != PACKAGE_FORMAT or report.get("version") != PACKAGE_VERSION:
        raise InsuranceError("report has an unsupported format")
    _scope(report.get("scope"))
    summary = report.get("summary")
    if not isinstance(summary, Mapping):
        raise InsuranceError("insurance report has invalid summary")
    _require_exact_keys(summary, {"item_count", "ready_count", "not_ready_count"}, "summary")
    if any(
        not isinstance(summary[key], int) or isinstance(summary[key], bool) or summary[key] < 0
        for key in summary
    ):
        raise InsuranceError("insurance report has invalid summary")
    items = report.get("items")
    if not isinstance(items, list):
        raise InsuranceError("insurance report has invalid items")
    capture_proofs = report.get("capture_photo_proofs")
    if capture_proofs != []:
        raise InsuranceError("insurance report must not contain capture photo proofs")
    item_ids: set[str] = set()
    evidence_links: set[tuple[str, str, str]] = set()
    evidence_pairs: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise InsuranceError("insurance report has invalid item")
        _require_exact_keys(
            item,
            {
                "item_id",
                "model",
                "category",
                "ownership_state",
                "readiness",
                "gaps",
                "fields",
                "documents",
                "valuation_context",
            },
            "item",
        )
        item_id = _text(item.get("item_id"))
        if item_id is None or item_id in item_ids or _text(item.get("model")) is None:
            raise InsuranceError("insurance report has invalid item identity")
        item_ids.add(item_id)
        if item.get("category") is not None and not isinstance(item.get("category"), str):
            raise InsuranceError("insurance report has invalid item category")
        if item.get("ownership_state") not in {"confirmed", "lent"}:
            raise InsuranceError("insurance report has invalid item ownership state")
        if item.get("readiness") not in {"ready", "not_ready"} or not isinstance(
            item.get("gaps"), list
        ):
            raise InsuranceError("insurance report has invalid item readiness")
        fields = item.get("fields")
        field_order = (
            "photo",
            "serial",
            "value",
            "receipt",
            "appraisal",
            "acquired_date",
            "location",
            "evidence_quality",
            "custody",
        )
        expected_fields = set(field_order)
        if not isinstance(fields, Mapping) or set(fields) != expected_fields:
            raise InsuranceError("insurance report has invalid item fields")
        for field_name, assessment in fields.items():
            _validate_assessment(assessment, field_name)
        photo = fields["photo"]
        if photo["state"] == "present":
            _require_exact_keys(photo, {"state", "evidence"}, "photo assessment")
            if not isinstance(photo.get("evidence"), list) or not photo["evidence"]:
                raise InsuranceError("insurance report has invalid photo assessment")
            for observation in photo["evidence"]:
                if not isinstance(observation, Mapping):
                    raise InsuranceError("insurance report has invalid photo assessment")
                qualification = observation.get("qualification")
                _require_exact_keys(
                    observation,
                    {"evidence_id", "qualification"},
                    "photo evidence",
                )
                evidence_id = _text(observation.get("evidence_id"))
                if evidence_id is None or qualification != "current_physical_check":
                    raise InsuranceError("insurance report has invalid photo assessment")
        for field_name in ("receipt", "appraisal", "custody"):
            if fields[field_name]["state"] == "present":
                _require_exact_keys(fields[field_name], {"state"}, f"{field_name} assessment")
        gaps = item["gaps"]
        if (
            any(not isinstance(gap, str) or gap not in expected_fields for gap in gaps)
            or gaps != [name for name in field_order if fields[name]["state"] == "unknown"]
            or item["readiness"] != ("ready" if not gaps else "not_ready")
        ):
            raise InsuranceError("insurance report has inconsistent readiness")
        serial = fields["serial"]
        if serial["state"] == "present" and (
            _require_exact_keys(serial, {"state", "serial_or_lot"}, "serial")
            or _text(serial.get("serial_or_lot")) is None
        ):
            raise InsuranceError("insurance report has invalid serial")
        acquired = fields["acquired_date"]
        if acquired["state"] == "present" and (
            _require_exact_keys(acquired, {"state", "acquired_on"}, "acquired date")
            or _valid_date(acquired.get("acquired_on")) is None
        ):
            raise InsuranceError("insurance report has invalid acquired date")
        location = fields["location"]
        if location["state"] == "present":
            _require_exact_keys(location, {"state", "location_ids", "location_names"}, "location")
            if (
                type(location["location_ids"]) is not list
                or type(location["location_names"]) is not list
                or not location["location_ids"]
                or len(location["location_ids"]) != len(location["location_names"])
                or not all(isinstance(value, str) and value for value in location["location_ids"])
                or not all(isinstance(value, str) and value for value in location["location_names"])
            ):
                raise InsuranceError("insurance report has invalid location")
        value = fields["value"]
        if value["state"] == "present":
            _require_exact_keys(value, {"state", "valuations"}, "value")
            if not isinstance(value["valuations"], list):
                raise InsuranceError("insurance report has invalid valuations")
        evidence_quality = fields["evidence_quality"]
        if evidence_quality["state"] == "present":
            _require_exact_keys(evidence_quality, {"state", "observations"}, "evidence quality")
            if not isinstance(evidence_quality["observations"], list):
                raise InsuranceError("insurance report has invalid evidence quality")
            for observation in evidence_quality["observations"]:
                if not isinstance(observation, Mapping):
                    raise InsuranceError("insurance report has invalid evidence quality")
                _require_exact_keys(
                    observation,
                    {"evidence_id", "evidence_type", "claim_strength"},
                    "evidence observation",
                )
        if not isinstance(item["valuation_context"], list):
            raise InsuranceError("insurance report has invalid valuation context")
        for valuation in item["valuation_context"]:
            if not isinstance(valuation, Mapping):
                raise InsuranceError("insurance report has invalid valuation context")
            _require_exact_keys(
                valuation,
                {"valuation_id", "amount", "currency", "basis", "valued_on", "evidence_id"},
                "valuation",
            )
            if (
                _text(valuation.get("valuation_id")) is None
                or not _finite_number(valuation.get("amount"))
                or valuation["amount"] < 0
                or _text(valuation.get("currency")) is None
                or _text(valuation.get("basis")) is None
                or _valid_date(valuation.get("valued_on")) is None
                or _text(valuation.get("evidence_id")) is None
            ):
                raise InsuranceError("insurance report has invalid valuation context")
        ready_values = (
            fields["value"].get("valuations", []) if fields["value"]["state"] == "present" else []
        )
        if ready_values != [
            value
            for value in item["valuation_context"]
            if value["basis"] in {"replacement", "appraisal"}
        ]:
            raise InsuranceError("insurance report has invalid insurance-ready valuation semantics")
        if not isinstance(item["documents"], list):
            raise InsuranceError("insurance report has invalid documents")
        for document in item["documents"]:
            if not isinstance(document, Mapping) or not {"document_type", "uri"}.issubset(document):
                raise InsuranceError("insurance report has invalid document")
            if not set(document).issubset({"document_id", "document_type", "uri"}):
                raise InsuranceError("insurance report has invalid document")
            if _text(document.get("document_type")) is None or _text(document.get("uri")) is None:
                raise InsuranceError("insurance report has invalid document")
            if "document_id" in document and _text(document.get("document_id")) is None:
                raise InsuranceError("insurance report has invalid document")
    if summary != {
        "item_count": len(items),
        "ready_count": sum(item["readiness"] == "ready" for item in items),
        "not_ready_count": sum(item["readiness"] == "not_ready" for item in items),
    }:
        raise InsuranceError("insurance report has inconsistent summary")
    assets = report.get("media_assets")
    if not isinstance(assets, list):
        raise InsuranceError("insurance report has invalid media assets")
    asset_ids: set[str] = set()
    for asset in assets:
        if (
            not isinstance(asset, Mapping)
            or not _valid_media_asset(asset)
            or asset["asset_id"] in asset_ids
        ):
            raise InsuranceError("insurance report has invalid media asset")
        _require_exact_keys(asset, {"asset_id", "sha256", "byte_size", "media_type"}, "media asset")
        asset_ids.add(asset["asset_id"])
    evidence = report.get("evidence")
    if not isinstance(evidence, list):
        raise InsuranceError("insurance report has invalid evidence")
    evidence_ids: set[str] = set()
    for record in evidence:
        if not isinstance(record, Mapping):
            raise InsuranceError("insurance report has invalid evidence")
        _require_exact_keys(record, {"evidence_id", "evidence_type", "claim_strength"}, "evidence")
        evidence_id = _text(record.get("evidence_id"))
        if (
            evidence_id is None
            or evidence_id in evidence_ids
            or _text(record.get("evidence_type")) is None
            or _text(record.get("claim_strength")) is None
        ):
            raise InsuranceError("insurance report has invalid evidence")
        evidence_ids.add(evidence_id)
    links = report.get("item_evidence")
    if not isinstance(links, list):
        raise InsuranceError("insurance report has invalid item evidence links")
    for link in links:
        if not isinstance(link, Mapping):
            raise InsuranceError("insurance report has invalid item evidence link")
        _require_exact_keys(link, {"item_id", "evidence_id", "role"}, "item evidence link")
        triple = (link.get("item_id"), link.get("evidence_id"), link.get("role"))
        pair = (triple[0], triple[1])
        if (
            not all(isinstance(value, str) and value for value in triple)
            or triple[2] not in {"primary", "supporting"}
            or triple in evidence_links
            or pair in evidence_pairs
            or triple[0] not in item_ids
            or triple[1] not in evidence_ids
        ):
            raise InsuranceError("insurance report has invalid item evidence link")
        evidence_links.add(triple)
        evidence_pairs.add(pair)
    asset_links = report.get("evidence_assets")
    if not isinstance(asset_links, list):
        raise InsuranceError("insurance report has invalid evidence media links")
    seen_asset_links: set[tuple[str, str, str]] = set()
    for link in asset_links:
        if not isinstance(link, Mapping):
            raise InsuranceError("insurance report has invalid evidence media link")
        _require_exact_keys(link, {"evidence_id", "asset_id", "role"}, "evidence media link")
        triple = (link.get("evidence_id"), link.get("asset_id"), link.get("role"))
        if (
            not all(isinstance(value, str) and value for value in triple)
            or triple[2] not in {"source", "crop", "receipt", "appraisal", "manual", "other"}
            or triple in seen_asset_links
            or triple[0] not in evidence_ids
            or triple[1] not in asset_ids
        ):
            raise InsuranceError("insurance report has invalid evidence media link")
        seen_asset_links.add(triple)
    assets_by_id = {asset["asset_id"]: asset for asset in assets}
    evidence_by_id = {record["evidence_id"]: record for record in evidence}
    for item in items:
        item_id = item["item_id"]
        for valuation in item["valuation_context"]:
            valuation_evidence = evidence_by_id.get(valuation["evidence_id"])
            if (
                valuation_evidence is None
                or not valuation_evidence_supports_basis(valuation_evidence, valuation["basis"])
                or not any(
                    linked_item == item_id and linked_evidence == valuation["evidence_id"]
                    for linked_item, linked_evidence, _role in evidence_links
                )
            ):
                raise InsuranceError("insurance report has unlinked valuation evidence")
        item_evidence_ids = {
            evidence_id
            for linked_item, evidence_id, _role in evidence_links
            if linked_item == item_id
        }
        photo_observations = item["fields"]["photo"].get("evidence", [])
        valid_photo_evidence = True
        for observation in photo_observations:
            evidence_id = observation.get("evidence_id")
            qualification = observation.get("qualification")
            record = evidence_by_id.get(evidence_id)
            image_roles = {
                role
                for linked_evidence, asset_id, role in seen_asset_links
                if linked_evidence == evidence_id
                and asset_id in assets_by_id
                and assets_by_id[asset_id]["media_type"].startswith("image/")
            }
            if evidence_id not in item_evidence_ids or record is None:
                valid_photo_evidence = False
            elif qualification == "current_physical_check":
                valid_photo_evidence = valid_photo_evidence and (
                    record["evidence_type"] == "physical_check"
                    and record["claim_strength"] == "explicit_current"
                    and bool(image_roles & {"source", "crop", "manual"})
                )
            else:
                valid_photo_evidence = False
        receipt_evidence = any(
            role == "receipt"
            and _insurance_document_media_type(assets_by_id[asset_id])
            and evidence_by_id[evidence_id]["claim_strength"] == "purchase_only"
            and evidence_by_id[evidence_id]["evidence_type"] in {"merchant_account", "user_source"}
            for evidence_id, asset_id, role in seen_asset_links
            if evidence_id in item_evidence_ids
        )
        appraisal_valuation_evidence = {
            value["evidence_id"]
            for value in item["valuation_context"]
            if value["basis"] == "appraisal"
        }
        appraisal_evidence = any(
            role == "appraisal"
            and evidence_id in appraisal_valuation_evidence
            and _insurance_document_media_type(assets_by_id[asset_id])
            for evidence_id, asset_id, role in seen_asset_links
        )
        expected_states = {
            "photo": "present" if photo_observations and valid_photo_evidence else "unknown",
            "serial": "present"
            if _text(item["fields"]["serial"].get("serial_or_lot"))
            else "unknown",
            "value": "present" if item["fields"]["value"].get("valuations") else "unknown",
            "receipt": "present" if receipt_evidence else "unknown",
            "appraisal": "present" if appraisal_evidence else "unknown",
            "acquired_date": "present"
            if _valid_date(item["fields"]["acquired_date"].get("acquired_on"))
            else "unknown",
            "location": item["fields"]["location"]["state"],
            "evidence_quality": "present" if item_evidence_ids else "unknown",
            "custody": "present"
            if item["ownership_state"] == "confirmed"
            or (
                item["ownership_state"] == "lent"
                and item["fields"]["location"]["state"] == "present"
            )
            else "unknown",
        }
        if any(item["fields"][name]["state"] != state for name, state in expected_states.items()):
            raise InsuranceError("insurance report has inconsistent field state")
        expected_observations = [
            {
                "evidence_id": record["evidence_id"],
                "evidence_type": record["evidence_type"],
                "claim_strength": record["claim_strength"],
            }
            for record in sorted(evidence, key=lambda record: record["evidence_id"])
            if record["evidence_id"] in item_evidence_ids
        ]
        if item["fields"]["evidence_quality"].get("observations", []) != expected_observations:
            raise InsuranceError("insurance report has inconsistent evidence quality")
    # Canonical JSON is the final finite-number and serialisability gate.
    _canonical_json(report)


def build_insurance_package(
    report: Mapping[str, Any], media_by_asset_id: Mapping[str, bytes] | None = None
) -> bytes:
    """Create a deterministic JSON/CSV/media ZIP from one scoped report.

    ``media_by_asset_id`` is intentionally explicit: package creation refuses
    absent, altered, or ambiguously keyed media rather than reading from a live
    inventory root.
    """
    _validate_report(report)
    supplied = media_by_asset_id or {}
    files = {"items.json": _canonical_json(report), "items.csv": _csv_bytes(report)}
    for asset in report.get("media_assets", ()):
        if not isinstance(asset, Mapping):
            raise InsuranceError("report media_assets contains a non-record")
        asset_id = _text(asset.get("asset_id"))
        digest = _sha256(asset.get("sha256"))
        size = asset.get("byte_size")
        payload = supplied.get(asset_id) if asset_id is not None else None
        if (
            asset_id is None
            or digest is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(payload, bytes)
            or len(payload) != size
            or hashlib.sha256(payload).hexdigest() != digest
        ):
            raise InsuranceError(
                f"media bytes do not match report asset: {asset_id or '[unknown]'}"
            )
        try:
            validate_declared_media_bytes(payload, asset["media_type"])
        except (KeyError, MediaValidationError) as error:
            raise InsuranceError(
                f"media bytes disagree with their declared type: {asset_id}"
            ) from error
        files[f"media/{digest}"] = payload
    manifest = {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "files": {name: _digest(payload) for name, payload in sorted(files.items())},
    }
    manifest_bytes = _canonical_json(manifest)
    if len(files) + 1 > _MAX_MEMBERS:
        raise InsuranceError("insurance package exceeds the member limit")
    if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
        raise InsuranceError("insurance package manifest exceeds the byte limit")
    result = io.BytesIO()
    with zipfile.ZipFile(
        result, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True
    ) as archive:
        _zip_member(archive, "manifest.json", manifest_bytes)
        for name, payload in sorted(files.items()):
            _zip_member(archive, name, payload)
    package = result.getvalue()
    if len(package) > MAX_INSURANCE_PACKAGE_BYTES:
        raise InsuranceError("insurance package exceeds the byte limit")
    return package


def _safe_member_name(name: str) -> bool:
    """Accept only the exact POSIX namespace used by this package format."""
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:", name)
    ):
        return False
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    return (
        name == "manifest.json"
        or name in _PACKAGE_FILES
        or (len(parts) == 2 and parts[0] == "media" and _SHA256.fullmatch(parts[1]) is not None)
    )


def _bounded_member_read(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.file_size > MAX_INSURANCE_MEMBER_BYTES:
        raise InsuranceError("package member exceeds size limit")
    result = bytearray()
    with archive.open(info, "r") as source:
        while chunk := source.read(min(64 * 1024, MAX_INSURANCE_MEMBER_BYTES - len(result) + 1)):
            result.extend(chunk)
            if len(result) > MAX_INSURANCE_MEMBER_BYTES:
                raise InsuranceError("package member exceeds size limit")
    if len(result) != info.file_size:
        raise InsuranceError("package member size does not match archive metadata")
    return bytes(result)


def _read_package(package: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not isinstance(package, bytes):
        raise InsuranceError("package must be bytes")
    if len(package) > MAX_INSURANCE_PACKAGE_BYTES:
        raise InsuranceError("package exceeds size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(package), "r") as archive:
            infos = archive.infolist()
            if not infos or len(infos) > _MAX_MEMBERS:
                raise InsuranceError("package has invalid member count")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or any(not _safe_member_name(name) for name in names):
                raise InsuranceError("package contains unsafe or duplicate member names")
            if any(info.flag_bits & 0x1 for info in infos):
                raise InsuranceError("encrypted package members are not supported")
            total = 0
            for info in infos:
                mode = (info.external_attr >> 16) & 0o170000
                if mode not in {0, stat.S_IFREG}:
                    raise InsuranceError("package contains a non-regular member")
                if info.file_size < 0 or info.file_size > MAX_INSURANCE_MEMBER_BYTES:
                    raise InsuranceError("package member exceeds size limit")
                total += info.file_size
                if total > _MAX_TOTAL_BYTES:
                    raise InsuranceError("package exceeds expanded size limit")
                if info.file_size and (
                    info.compress_size <= 0
                    or info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
                ):
                    raise InsuranceError("package member exceeds compression ratio limit")
            if "manifest.json" not in names:
                raise InsuranceError("package has no manifest")
            manifest_info = archive.getinfo("manifest.json")
            if manifest_info.file_size > _MAX_MANIFEST_BYTES:
                raise InsuranceError("package manifest exceeds size limit")
            manifest_value = _json_loads(_bounded_member_read(archive, manifest_info))
            if not isinstance(manifest_value, dict):
                raise InsuranceError("package manifest must be an object")
            payloads = {
                info.filename: _bounded_member_read(archive, info)
                for info in infos
                if info.filename != "manifest.json"
            }
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise InsuranceError(f"cannot read insurance package: {error}") from error
    return manifest_value, payloads


def validate_insurance_package(package: bytes) -> dict[str, Any]:
    """Validate and load an archive without writing any canonical inventory bytes."""
    manifest, payloads = _read_package(package)
    _require_exact_keys(manifest, {"format", "version", "files"}, "package manifest")
    if manifest.get("format") != PACKAGE_FORMAT or manifest.get("version") != PACKAGE_VERSION:
        raise InsuranceError("package has an unsupported format")
    declarations = manifest.get("files")
    if not isinstance(declarations, Mapping) or set(declarations) != set(payloads):
        raise InsuranceError("package manifest does not declare exactly its payload files")
    if not set(_PACKAGE_FILES).issubset(payloads):
        raise InsuranceError("package is missing its JSON or CSV report")
    if any(not _safe_member_name(name) or name == "manifest.json" for name in payloads):
        raise InsuranceError("package manifest declares an unsupported file")
    for name, declaration in declarations.items():
        if not isinstance(declaration, Mapping):
            raise InsuranceError(f"package manifest declaration is invalid: {name}")
        payload = payloads[name]
        if declaration != _digest(payload):
            raise InsuranceError(f"package member digest mismatch: {name}")
        if name.startswith("media/"):
            digest = name.removeprefix("media/")
            if len(digest) != 64 or hashlib.sha256(payload).hexdigest() != digest:
                raise InsuranceError(f"package media digest mismatch: {name}")
    try:
        report = _json_loads(payloads["items.json"])
    except ValueError as error:
        raise InsuranceError("package JSON report is invalid") from error
    if not isinstance(report, dict):
        raise InsuranceError("package JSON report must be an object")
    _validate_report(report)
    try:
        csv_payload = _csv_bytes(report)
    except (KeyError, TypeError) as error:
        raise InsuranceError("package JSON report has invalid items") from error
    if csv_payload != payloads["items.csv"]:
        raise InsuranceError("package CSV report does not match its JSON report")
    report_assets = report["media_assets"]
    expected_media: set[str] = set()
    seen_asset_ids: set[str] = set()
    for asset in report_assets:
        if (
            not isinstance(asset, Mapping)
            or not isinstance(asset.get("asset_id"), str)
            or asset["asset_id"] in seen_asset_ids
            or _sha256(asset.get("sha256")) is None
            or not isinstance(asset.get("byte_size"), int)
            or isinstance(asset["byte_size"], bool)
            or asset["byte_size"] < 0
        ):
            raise InsuranceError("package JSON report has invalid media asset")
        seen_asset_ids.add(asset["asset_id"])
        media_name = f"media/{asset['sha256']}"
        if media_name in expected_media or media_name not in payloads:
            raise InsuranceError("package JSON report has duplicate or missing media asset")
        if len(payloads[media_name]) != asset["byte_size"]:
            raise InsuranceError("package JSON report media size does not match its asset")
        try:
            validate_declared_media_bytes(payloads[media_name], asset["media_type"])
        except MediaValidationError as error:
            raise InsuranceError(
                f"package media bytes disagree with their declared type: {asset['asset_id']}"
            ) from error
        expected_media.add(media_name)
    actual_media = {name for name in payloads if name.startswith("media/")}
    if expected_media != actual_media:
        raise InsuranceError("package media does not match its JSON report")
    return report


def load_insurance_package(package: bytes) -> dict[str, Any]:
    """Alias for callers that need a validated in-memory package load."""
    return validate_insurance_package(package)


# Names kept intentionally plain for command and MCP adapters.
assess_insurance = insurance_report
export_insurance_package = build_insurance_package
