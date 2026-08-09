"""Strict, provenance-preserving generic import normalization.

This module deliberately has no access to an inventory root.  An import is
evidence to review, not proof that an item is owned, received, located, or in
any particular condition.  Its only output is a deterministic list of CLI
proposal operations which an integration layer may prepare for review.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Literal

from .json_codec import StrictJSONError
from .json_codec import loads as strict_json_loads


class ImportError(ValueError):
    """Raised when a source cannot safely become an explicit proposal."""


JsonValue = str | int | float | bool | None | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
ImportFormat = Literal["csv", "json"]

_CART_STATES = frozenset({"cart", "considering", "planned", "plan", "wishlist"})
_ORDER_STATES = frozenset({"order", "ordered", "purchased"})
_HEADER_ALIASES = {
    "name": "name",
    "title": "name",
    "item": "name",
    "product": "name",
    "status": "status",
    "state": "status",
    "lifecycle": "status",
    "category": "category",
    "type": "category",
    "external_id": "external_id",
    "externalid": "external_id",
    "id": "external_id",
    "order_id": "external_id",
    "orderid": "external_id",
    "date": "date",
    "ordered_on": "date",
    "order_date": "date",
    "purchased_on": "date",
    "planned_on": "date",
    "url": "reference_url",
    "reference_url": "reference_url",
    "product_url": "reference_url",
    "brand": "brand",
    "model": "model",
    "quantity": "quantity",
    "unit": "unit",
    "notes": "notes",
}


def _frozen_json(value: object, field: str) -> JsonValue:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ImportError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ImportError(f"{field} contains a non-finite number")
        projected = float(value)
        if not math.isfinite(projected) or Decimal(str(projected)) != value.normalize():
            raise ImportError(
                f"{field} contains a number that cannot be represented without changing it"
            )
        return projected
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ImportError(f"{field} keys must be strings")
            frozen[key] = _frozen_json(child, field)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_frozen_json(child, field) for child in value)
    raise ImportError(f"{field} must contain JSON data")


def thaw_json(value: JsonValue) -> Any:
    """Return ordinary JSON data without exposing the immutable proposal state."""
    if isinstance(value, Mapping):
        return {key: thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(child) for child in value]
    return value


def _text(value: object, field: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ImportError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ImportError(f"{field} must be text when supplied")
    if "\x00" in value:
        raise ImportError(f"{field} must not contain NUL bytes")
    normalized = value.strip()
    if not normalized:
        if required:
            raise ImportError(f"{field} is required")
        return None
    return normalized


def _date(value: object, field: str) -> str:
    text = _text(value, field, required=True)
    assert text is not None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as error:
        raise ImportError(f"{field} must be an ISO-8601 date") from error


def _quantity(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ImportError("quantity must be a finite positive number when supplied")
    if not isinstance(value, (str, int, float)):
        raise ImportError("quantity must be a finite positive number when supplied")
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ImportError("quantity must be a finite positive number when supplied") from error
    if not numeric.is_finite() or numeric <= 0:
        raise ImportError("quantity must be a finite positive number when supplied")
    projected = float(numeric)
    if not math.isfinite(projected) or Decimal(str(projected)) != numeric.normalize():
        raise ImportError("quantity cannot be represented exactly by the canonical numeric store")
    return format(numeric.normalize(), "f")


def _normalized_header(header: object) -> str:
    text = _text(header, "CSV header", required=True)
    assert text is not None
    return "".join(character for character in text.casefold() if character.isalnum() or character == "_")


def _canonical_field(key: str) -> str:
    return _HEADER_ALIASES.get(_normalized_header(key), _normalized_header(key))


def _csv_rows(payload: bytes) -> list[dict[str, JsonValue]]:
    try:
        source = io.StringIO(payload.decode("utf-8-sig", errors="strict"), newline="")
    except UnicodeDecodeError as error:
        raise ImportError("CSV must be UTF-8") from error
    reader = csv.reader(source, strict=True)
    try:
        headers = next(reader)
    except StopIteration as error:
        raise ImportError("CSV must have a header row") from error
    normalized = [_normalized_header(header) for header in headers]
    if len(normalized) != len(set(normalized)):
        raise ImportError("CSV headers collide after normalization")
    if not normalized:
        raise ImportError("CSV must have at least one header")
    rows: list[dict[str, JsonValue]] = []
    try:
        for row_number, row in enumerate(reader, start=2):
            if len(row) != len(headers):
                raise ImportError(f"CSV row {row_number} has a different number of fields")
            rows.append(
                {
                    header: (value if value != "" else None)
                    for header, value in zip(headers, row, strict=True)
                }
            )
    except csv.Error as error:
        raise ImportError(f"malformed CSV: {error}") from error
    if not rows:
        raise ImportError("CSV must contain at least one data row")
    return rows


def _json_rows(payload: bytes) -> list[dict[str, JsonValue]]:
    try:
        loaded = strict_json_loads(
            payload,
            label="generic import JSON",
            parse_float=Decimal,
        )
    except StrictJSONError as error:
        raise ImportError(f"malformed JSON: {error}") from error
    if not isinstance(loaded, list) or not loaded:
        raise ImportError("JSON import must be a non-empty array of objects")
    rows: list[dict[str, JsonValue]] = []
    for row_number, row in enumerate(loaded, start=1):
        if not isinstance(row, Mapping) or not row:
            raise ImportError(f"JSON row {row_number} must be a non-empty object")
        normalized = [_normalized_header(key) for key in row]
        if len(normalized) != len(set(normalized)):
            raise ImportError(f"JSON row {row_number} has colliding keys")
        try:
            rows.append(
                {
                    key: _frozen_json(value, f"JSON row {row_number}")
                    for key, value in row.items()
                }
            )
        except RecursionError as error:
            raise ImportError("malformed JSON: nesting is too deep") from error
    return rows


@dataclass(frozen=True)
class ImportRecord:
    """One source row, with its raw values and non-assertive proposed command."""

    row_number: int
    external_id: str | None
    operation: tuple[str, ...]
    raw: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if self.row_number <= 0:
            raise ImportError("row_number must be positive")
        if self.external_id is not None:
            _text(self.external_id, "external_id", required=True)
        if not self.operation or self.operation[0] not in {"plan", "order"}:
            raise ImportError("import proposals must be plan or order operations")


@dataclass(frozen=True)
class ImportProposal:
    """A deterministic review artifact, never a direct canonical write."""

    source_name: str
    source_namespace: str
    source_format: ImportFormat
    source_sha256: str
    imported_on: str
    records: tuple[ImportRecord, ...]

    @property
    def operations(self) -> tuple[tuple[str, ...], ...]:
        return tuple(record.operation for record in self.records)

    def operation_lists(self) -> list[list[str]]:
        """Return the JSON-compatible operation arrays accepted by ``propose``."""
        return [list(operation) for operation in self.operations]


def _row_lookup(raw: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    lookup: dict[str, JsonValue] = {}
    for key, value in raw.items():
        canonical = _canonical_field(key)
        if canonical in lookup:
            raise ImportError(f"row has colliding fields for {canonical}")
        lookup[canonical] = value
    return lookup


def _operation_for_row(
    *,
    raw: Mapping[str, JsonValue],
    row_number: int,
    source_format: ImportFormat,
    source_name: str,
    source_namespace: str,
    source_sha256: str,
    sensitivity: str,
) -> tuple[str, ...]:
    fields = _row_lookup(raw)
    name = _text(fields.get("name"), "name", required=True)
    status = _text(fields.get("status"), "status", required=True)
    assert name is not None and status is not None
    state = status.casefold().replace("-", "_").replace(" ", "_")
    if state in _CART_STATES:
        command = "plan"
        date_flag = "--planned-on"
    elif state in _ORDER_STATES:
        command = "order"
        date_flag = "--ordered-on"
    else:
        raise ImportError(
            f"row {row_number} has ambiguous status {status!r}; use a cart/planned or order/ordered value"
        )
    record_date = _date(fields.get("date"), "date")
    category = _text(fields.get("category"), "category") or "unknown"
    external_id = _text(fields.get("external_id"), "external_id")
    # Only a source's stable external ID identifies a distinct unit.  File
    # digest and row number change when an export is regenerated or reordered,
    # so they must never authorise another candidate unit.
    source_unit_identity = (
        hashlib.sha256(f"{source_namespace}\x00{external_id}".encode()).hexdigest()
        if external_id is not None
        else None
    )
    source_ref = (
        f"import:{source_namespace}:{source_name}#row-{row_number};"
        + (f"unit_sha256={source_unit_identity};" if source_unit_identity else "")
        + f"sha256={source_sha256}"
    )
    provenance = {
        "import": {
            "format": source_format,
            "row_number": row_number,
            "source_name": source_name,
            "source_namespace": source_namespace,
            "source_sha256": source_sha256,
            "external_id": external_id,
            "source_unit_identity": source_unit_identity,
        },
        "raw_fields": thaw_json(_frozen_json(raw, "raw row")),
    }
    evidence_notes = json.dumps(
        {"generic_import": provenance},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    operation = [
        command,
        "--actor",
        "generic-import",
        "--source-ref",
        source_ref,
        "--name",
        name,
        "--category",
        category,
        date_flag,
        record_date,
        # Source-instance provenance is evidence, not model identity.  Model
        # specs can be public while import rows are routinely personal.
        "--specs",
        "{}",
        "--notes",
        evidence_notes,
        "--sensitivity",
        sensitivity,
    ]
    if command == "order":
        operation.append("--order-placed")
    if source_unit_identity is not None:
        operation.extend(("--import-unit-identity", source_unit_identity))
    # Merchant URLs are source-instance provenance and can contain order/customer
    # tokens. They remain inside sensitivity-bearing evidence notes, never the
    # shared model row.
    for field, flag in (("brand", "--brand"), ("model", "--model")):
        value = _text(fields.get(field), field)
        if value is not None:
            operation.extend((flag, value))
    quantity = _quantity(fields.get("quantity"))
    if quantity is not None:
        operation.extend(("--quantity", quantity))
    unit = _text(fields.get("unit"), "unit")
    if unit is not None:
        operation.extend(("--unit", unit))
    return tuple(operation)


def normalize_import(
    payload: bytes,
    *,
    source_format: ImportFormat,
    source_name: str,
    source_namespace: str,
    imported_on: str,
    known_external_keys: Iterable[tuple[str, str]] = (),
    known_external_key_digests: Iterable[str] = (),
    sensitivity: str = "personal",
) -> ImportProposal:
    """Normalize one CSV or JSON export into reviewable non-possession proposals.

    Rows require an explicit cart/planned or order/ordered state.  Received,
    owned, location, condition, and verification claims are intentionally not
    interpreted, even if a third-party export contains columns with those names.
    """
    if source_format not in {"csv", "json"}:
        raise ImportError("source_format must be csv or json")
    name = _text(source_name, "source_name", required=True)
    namespace = _text(source_namespace, "source_namespace", required=True)
    assert name is not None and namespace is not None
    imported_date = _date(imported_on, "imported_on")
    if sensitivity not in {"low", "personal", "high"}:
        raise ImportError("sensitivity must be low, personal, or high")
    if not isinstance(payload, bytes) or not payload:
        raise ImportError("payload must be non-empty bytes")
    rows = _csv_rows(payload) if source_format == "csv" else _json_rows(payload)
    source_sha256 = hashlib.sha256(payload).hexdigest()
    known: set[tuple[str, str]] = set()
    for external_key in known_external_keys:
        if type(external_key) is not tuple or len(external_key) != 2:
            raise ImportError("known_external_keys must contain (source_namespace, external_id) tuples")
        known_namespace = _text(
            external_key[0], "known_external_keys source namespace", required=True
        )
        known_id = _text(external_key[1], "known_external_keys external id", required=True)
        assert known_namespace is not None and known_id is not None
        normalized = (known_namespace, known_id)
        if normalized in known:
            raise ImportError("known_external_keys contains a duplicate")
        known.add(normalized)
    known_digests: set[str] = set()
    for digest in known_external_key_digests:
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ImportError("known_external_key_digests must contain SHA-256 digests")
        if digest in known_digests:
            raise ImportError("known_external_key_digests contains a duplicate")
        known_digests.add(digest)
    records: list[ImportRecord] = []
    seen_external_ids: set[str] = set()
    seen_operations: set[tuple[str, ...]] = set()
    seen_raw_rows: set[str] = set()
    for row_number, raw in enumerate(rows, start=1):
        fields = _row_lookup(raw)
        raw_fingerprint = json.dumps(
            thaw_json(_frozen_json(raw, "raw row")),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if raw_fingerprint in seen_raw_rows:
            raise ImportError(f"row {row_number} duplicates an earlier source row")
        seen_raw_rows.add(raw_fingerprint)
        external_id = _text(fields.get("external_id"), "external_id")
        if external_id is not None:
            if (namespace, external_id) in known:
                raise ImportError(f"row {row_number} external_id collides with an existing record")
            external_digest = hashlib.sha256(
                f"{namespace}\x00{external_id}".encode()
            ).hexdigest()
            if external_digest in known_digests:
                raise ImportError(f"row {row_number} external_id collides with an existing record")
            if external_id in seen_external_ids:
                raise ImportError(f"row {row_number} duplicates external_id {external_id!r}")
            seen_external_ids.add(external_id)
        operation = _operation_for_row(
            raw=raw,
            row_number=row_number,
            source_format=source_format,
            source_name=name,
            source_namespace=namespace,
            source_sha256=source_sha256,
            sensitivity=sensitivity,
        )
        if operation in seen_operations:
            raise ImportError(f"row {row_number} duplicates an earlier proposed operation")
        seen_operations.add(operation)
        records.append(
            ImportRecord(
                row_number=row_number,
                external_id=external_id,
                operation=operation,
                raw=MappingProxyType(dict(raw)),
            )
        )
    return ImportProposal(
        source_name=name,
        source_namespace=namespace,
        source_format=source_format,
        source_sha256=source_sha256,
        imported_on=imported_date,
        records=tuple(records),
    )
