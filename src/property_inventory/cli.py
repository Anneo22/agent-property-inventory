#!/usr/bin/env python3
"""Query and mutate the canonical Property Inventory without partial writes."""

from __future__ import annotations

import argparse
import errno
import gzip
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from filelock import FileLock, Timeout

from .capture import (
    MAX_CAPTURE_CROP_BYTES,
    MAX_CAPTURE_SEGMENTS,
    MAX_CAPTURE_SOURCE_BYTES,
    MAX_CAPTURE_TOTAL_CROP_BYTES,
    CaptureError,
)
from .capture_adapters import AdapterRegistry, load_adapter_registry
from .capture_provenance import (
    CaptureProvenanceError,
    canonical_artifact_bytes,
    canonical_review_bytes,
    validate_capture_artifact,
    validate_capture_review,
)
from .capture_service import (
    CAPTURE_REQUEST_DIGEST_FILE,
    CaptureServiceError,
    prepare_capture,
    run_synthetic_capture_benchmark,
)
from .compatibility_policy import (
    CompatibilityError,
    compatibility_matrix,
    validate_migration,
    validate_runtime,
)
from .config import ConfigError, resolve_instance_config
from .doctor import (
    CommandResult,
    DoctorError,
    plan_blank_restore,
    run_blank_restore,
)
from .importers import ImportError, normalize_import
from .insurance import (
    MAX_INSURANCE_MEMBER_BYTES,
    MAX_INSURANCE_PACKAGE_BYTES,
    InsuranceError,
    build_insurance_package,
    insurance_report,
    validate_insurance_package,
)
from .json_codec import StrictJSONError
from .json_codec import dumps as strict_json_dumps
from .json_codec import loads as strict_json_loads
from .maintenance import (
    MaintenanceError,
    maintenance_report,
    measured_elapsed_seconds,
    run_synthetic_four_week_harness,
)
from .media_validation import (
    MediaValidationError,
    declared_media_matches,
    normalized_media_type,
    validate_declared_media,
)
from .rebuild import (
    CONTAINER_LOCATION_KINDS,
    LOCATION_KINDS,
    SCHEMA_VERSION,
    capture_provenance_failures,
    location_assignment_error,
)
from .render import catalogue_created_on, write_output_atomic
from .retrieval import (
    RetrievalError,
    decode_page_cursor,
    encode_page_cursor,
    item_context,
    location_scope_allows,
    normalized,
    page_fingerprint,
    resolve_item_reference,
    scope_allows,
    scope_visible_item_details,
)
from .retrieval import (
    search as retrieve_search,
)
from .retrieval import (
    task_context as retrieve_task_context,
)
from .spatial import (
    SpatialValidationError,
    convert_length,
    normalize_spatial_profile,
    parse_geojson_floor_plan,
)
from .spatial import (
    fit as spatial_fit,
)
from .spatial import (
    free_volume as spatial_free_volume,
)
from .spatial import (
    pack as spatial_pack,
)
from .sync import (
    SyncError,
    build_replica_bundle,
    build_store_snapshot,
    plan_three_way_merge,
    receipt_data,
    resolve_conflicts,
    verify_replica_bundle,
    verify_store_snapshot,
)
from .sync import (
    store_digest as sync_store_digest,
)
from .valuation_policy import valuation_evidence_supports_basis

HERE = Path(__file__).resolve().parent
MAX_CAPTURE_BENCHMARK_BYTES = 64 * 1024 * 1024
MAX_CAPTURE_METADATA_BYTES = 8 * 1024 * 1024
MAX_STRUCTURED_INPUT_BYTES = 16 * 1024 * 1024
MAX_SYNC_MEDIA_BYTES = 64 * 1024 * 1024
MAX_SYNC_MEDIA_TOTAL_BYTES = 128 * 1024 * 1024
V1_TABLES = (
    "locations",
    "models",
    "evidence",
    "items",
    "item_evidence",
    "relationships",
    "item_documents",
    "torque_paths",
    "kits",
    "kit_requirements",
    "item_tags",
    "inventory_events",
)
V2_TABLES = (
    "metadata",
    "locations",
    "models",
    "evidence",
    "media_assets",
    "interfaces",
    "items",
    "item_evidence",
    "evidence_assets",
    "model_interfaces",
    "relationships",
    "item_documents",
    "torque_paths",
    "kits",
    "kit_requirements",
    "item_tags",
    "inventory_events",
)
V3_TABLES = (
    "metadata",
    "proposal_commits",
    "locations",
    "models",
    "evidence",
    "media_assets",
    "interfaces",
    "items",
    "item_evidence",
    "evidence_assets",
    "model_interfaces",
    "relationships",
    "item_documents",
    "torque_paths",
    "kits",
    "kit_requirements",
    "item_tags",
    "inventory_events",
)
V4_TABLES = (
    "metadata",
    "proposal_commits",
    "locations",
    "models",
    "evidence",
    "media_assets",
    "interfaces",
    "items",
    "item_evidence",
    "evidence_assets",
    "model_interfaces",
    "relationships",
    "item_documents",
    "torque_paths",
    "kits",
    "kit_requirements",
    "item_tags",
    "aliases",
    "spatial_profiles",
    "valuations",
    "capture_sessions",
    "capture_observations",
    "maintenance_sessions",
    "maintenance_session_items",
    "sync_receipts",
    "inventory_events",
)
V5_TABLES = V4_TABLES
V6_TABLES = (
    *V5_TABLES[:-1],
    "kit_reviews",
    "item_dimensions",
    "item_amendments",
    "item_detail_amendments",
    "fact_amendments",
    "inventory_events",
)
V7_TABLES = (
    *V6_TABLES[:-1],
    "parties",
    "item_party_relations",
    "location_embodiments",
    "inventory_events",
)
TABLES = V7_TABLES
TABLES_BY_SCHEMA = {
    1: V1_TABLES,
    2: V2_TABLES,
    3: V3_TABLES,
    4: V4_TABLES,
    5: V5_TABLES,
    6: V6_TABLES,
    7: V7_TABLES,
}
ID_FIELDS = {
    "metadata": "inventory_id",
    "proposal_commits": "proposal_id",
    "locations": "location_id",
    "models": "model_id",
    "evidence": "evidence_id",
    "media_assets": "asset_id",
    "interfaces": "interface_id",
    "items": "item_id",
    "relationships": "relationship_id",
    "item_documents": "document_id",
    "torque_paths": "path_id",
    "kits": "kit_id",
    "aliases": "alias_id",
    "spatial_profiles": "profile_id",
    "valuations": "valuation_id",
    "capture_sessions": "capture_session_id",
    "capture_observations": "observation_id",
    "maintenance_sessions": "maintenance_session_id",
    "sync_receipts": "sync_receipt_id",
    "item_dimensions": "dimension_id",
    "item_amendments": "amendment_id",
    "item_detail_amendments": "detail_amendment_id",
    "fact_amendments": "fact_amendment_id",
    "kit_reviews": "review_id",
    "parties": "party_id",
    "item_party_relations": "relation_id",
    "location_embodiments": "embodiment_id",
    "inventory_events": "event_id",
}
SENSITIVITY_RANK = {"low": 0, "personal": 1, "high": 2}
# Broad to narrow, so `--help` reads as the tree it describes.
LOCATION_KIND_CHOICES = (
    "site",
    "place",
    "building",
    "floor",
    "zone",
    "room",
    "furniture",
    "compartment",
    "container",
    "vehicle",
    "asset",
    "unknown",
)
if set(LOCATION_KIND_CHOICES) != LOCATION_KINDS:
    raise RuntimeError("CLI location kinds drifted from the canonical schema kinds")
SPATIAL_EVIDENCE_CLAIMS = {
    "physical_check": "explicit_current",
    "research": "research_only",
    "vault_note": "research_only",
}
SCOPE_MAX_SENSITIVITY = {"public": 0, "personal": 1, "private": 2}
FACT_SELECTOR_FIELDS = {
    "locations": ("location_id",),
    "aliases": ("alias_id",),
    "item_tags": ("item_id", "tag"),
    "relationships": ("relationship_id",),
    "torque_paths": ("path_id",),
    "spatial_profiles": ("profile_id",),
    "kits": ("kit_id",),
    "kit_requirements": ("kit_id", "requirement_key"),
    "model_interfaces": ("model_id", "interface_id", "role"),
    "valuations": ("valuation_id",),
    "item_documents": ("document_id",),
    "item_dimensions": ("dimension_id",),
    "parties": ("party_id",),
    "item_party_relations": ("relation_id",),
    "location_embodiments": ("embodiment_id",),
}
ITEM_DETAIL_FIELDS = (
    "acquired_on",
    "condition",
    "purchase_currency",
    "purchase_price",
    "receipt_ref",
    "serial_or_lot",
    "home_location_id",
    "home_container_id",
)
ENRICH_ITEM_DETAIL_FIELDS = tuple(
    field for field in ITEM_DETAIL_FIELDS if not field.startswith("home_")
)
REACQUISITION_RESET_FIELDS = (
    "acquired_on",
    "condition",
    "purchase_currency",
    "purchase_price",
    "receipt_ref",
)
CHRONOLOGICAL_EVENT_TYPES = frozenset(
    {
        "planned",
        "ordered",
        "received",
        "sold",
        "returned",
        "cancelled",
        "refunded",
        "gifted",
        "disposed",
        "lost",
        "lent",
        "loan_returned",
        "ownership_unresolved",
        "ownership_excluded",
        "quantity_changed",
        "moved",
        "physically_verified",
        "not_found_in_area",
        "reacquired",
        "ownership_corrected",
    }
)
AUXILIARY_MANIFEST = "auxiliary-manifest.json"
KNOWN_AUXILIARY_INPUTS = (
    "verification_policy.json",
    "source-inventory.json",
    "account-candidates.json",
)
RETIRED_INSTANCE_MARKER = ".property-inventory-retired.json"
RESTORE_JOURNAL = ".property-inventory-restore.json"
CAPTURE_MEDIA_JOURNAL = ".property-inventory-capture-media.json"
CAPTURE_MEDIA_WORKSPACE = ".property-inventory-capture-media"
INIT_JOURNAL = ".property-inventory-init.json"
ADOPTION_ROLLBACK_JOURNAL_SUFFIX = ".property-inventory-adoption-rollback.json"
RUNTIME_BINDING = ".property-inventory-runtime.json"
RUNTIME_OWNER = ".property-inventory-owner.json"
DEGRADED_MARKER = ".property-inventory-degraded.json"
INVENTORY_GITIGNORE = ".gitignore"
SYNC_PLAN_DIRECTORY = "sync-plans"
SYNC_SNAPSHOT_FORMAT = 1
SYNC_PLAN_ENVELOPE_FORMAT = 1
_INSTANCE_PATHS: ContextVar[tuple[Path | None, Path, str] | None] = ContextVar(
    "property_inventory_instance_paths", default=None
)
_LOCK_INSTANCE_PATHS: ContextVar[
    tuple[Path, Path, Path | None, Path, bool] | None
] = ContextVar(
    "property_inventory_lock_instance_paths", default=None
)


class InventoryError(RuntimeError):
    pass


class InventoryArgumentParser(argparse.ArgumentParser):
    """Return parse failures through the CLI's structured error contract."""

    def error(self, message: str) -> None:
        raise InventoryError(f"invalid command arguments: {message}")


class RestoreConflict(InventoryError):
    """Raised when post-crash bytes do not match a proven restore state."""


PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_directory(path: Path) -> None:
    """Create or harden one product-owned directory without following symlinks."""
    if path.is_symlink():
        raise InventoryError(f"managed private directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    os.chmod(path, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
    if stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) != PRIVATE_DIRECTORY_MODE:
        raise InventoryError(f"cannot enforce private directory permissions: {path}")


def ensure_private_file(path: Path) -> None:
    """Harden one existing product-owned regular file and verify its mode."""
    if path.is_symlink() or not path.is_file():
        raise InventoryError(f"managed private file must be regular: {path}")
    os.chmod(path, PRIVATE_FILE_MODE, follow_symlinks=False)
    if stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) != PRIVATE_FILE_MODE:
        raise InventoryError(f"cannot enforce private file permissions: {path}")


def harden_private_tree(root: Path) -> None:
    """Harden a product-owned machine-data tree, rejecting links and special files."""
    ensure_private_directory(root)
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise InventoryError(f"managed private tree contains a symlink: {entry}")
        if entry.is_dir():
            ensure_private_directory(entry)
        elif entry.is_file():
            ensure_private_file(entry)
        else:
            raise InventoryError(f"managed private tree contains a special file: {entry}")


def read_jsonl(path: Path) -> list[dict]:
    try:
        rows: list[dict] = []
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            row = strict_json_loads(line, label=f"{path.name} line {number}")
            if not isinstance(row, dict):
                raise StrictJSONError(f"{path.name} line {number} is not an object")
            rows.append(row)
        return rows
    except (OSError, UnicodeError, StrictJSONError) as error:
        raise InventoryError(f"cannot read {path}: {error}") from error


def write_jsonl(path: Path, rows: list[dict]) -> None:
    try:
        payload = "".join(strict_json_dumps(row, sort_keys=True) + "\n" for row in rows)
    except StrictJSONError as error:
        raise InventoryError(f"cannot write canonical JSONL: {error}") from error
    ensure_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.write-{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        PRIVATE_FILE_MODE,
    )
    os.fchmod(descriptor, PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    ensure_private_file(path)
    fsync_directory(path.parent)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def fsync_tree(path: Path) -> None:
    for child in path.rglob("*"):
        if child.is_file():
            fsync_file(child)
    directories = [child for child in path.rglob("*") if child.is_dir()]
    for directory in sorted(directories, key=lambda part: len(part.parts), reverse=True):
        fsync_directory(directory)
    fsync_directory(path)


def durable_copy(source: Path, destination: Path) -> None:
    shutil.copy2(source, destination)
    ensure_private_file(destination)
    fsync_file(destination)


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def path_entry_exists(path: Path) -> bool:
    """Return lexical existence so a broken symlink cannot masquerade as absence."""
    return path.exists() or path.is_symlink()


def safe_auxiliary_name(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or not path.parts
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or path.parts[0] in {"store", AUXILIARY_MANIFEST}
        or str(path) != value
    ):
        raise InventoryError(f"unsafe auxiliary-data path: {value}")
    return value


def checked_managed_path(root: Path, relative: Path, label: str) -> Path:
    """Return a controlled descendant only when no managed component is a symlink."""
    candidate = root
    if candidate.is_symlink():
        raise InventoryError(f"{label} root must not be a symlink")
    for component in relative.parts:
        candidate = candidate / component
        if candidate.is_symlink():
            raise InventoryError(f"{label} path traverses a symlink: {relative.as_posix()}")
    return candidate


def checked_data_path(data_dir: Path, relative: Path) -> Path:
    """Return a lexical Data child only when no component is a symlink."""
    return checked_managed_path(data_dir, relative, "auxiliary-data")


def auxiliary_data_path(data_dir: Path, name: str) -> Path:
    safe_auxiliary_name(name)
    return checked_data_path(data_dir, Path(name))


def auxiliary_machine_files(data_dir: Path) -> set[str]:
    """List every non-store file under Data, rejecting symlinks and special files."""
    if data_dir.is_symlink() or not data_dir.is_dir():
        raise InventoryError("Data directory is missing or is a symlink")
    store_dir = checked_data_path(data_dir, Path("store"))
    if not store_dir.is_dir():
        raise InventoryError("Data/store is missing or is not a directory")
    for store_path in store_dir.rglob("*"):
        relative_store_path = store_path.relative_to(data_dir).as_posix()
        if store_path.is_symlink():
            raise InventoryError(f"canonical store path traverses a symlink: {relative_store_path}")
        if not store_path.is_file():
            raise InventoryError(
                f"canonical store contains a non-regular file: {relative_store_path}"
            )
    files: set[str] = set()
    for path in data_dir.rglob("*"):
        relative = path.relative_to(data_dir).as_posix()
        if relative == "store" or relative.startswith("store/"):
            continue
        if path.is_symlink():
            raise InventoryError(f"Data contains a symlink outside Data/store: {relative}")
        if path.is_file():
            files.add(relative)
        elif not path.is_dir():
            raise InventoryError(f"Data contains a non-regular file: {relative}")
    return files


def load_auxiliary_manifest(manifest_path: Path) -> dict[str, str]:
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise InventoryError(f"cannot read auxiliary-data manifest: {error}") from error
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != 1
        or not isinstance(files, dict)
        or not files
    ):
        raise InventoryError("unsupported or malformed auxiliary-data manifest")
    normalized: dict[str, str] = {}
    for raw_name, record in files.items():
        name = safe_auxiliary_name(raw_name)
        expected = record.get("sha256") if isinstance(record, dict) else None
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise InventoryError(f"invalid auxiliary-data digest: {name}")
        normalized[name] = expected
    return normalized


def validate_auxiliary_data(data_dir: Path) -> dict[str, str]:
    """Return the complete auxiliary file manifest or fail closed on drift."""
    manifest_path = checked_data_path(data_dir, Path(AUXILIARY_MANIFEST))
    actual_files = auxiliary_machine_files(data_dir)
    if not manifest_path.exists():
        if actual_files:
            raise InventoryError(
                "auxiliary files exist without Data/auxiliary-manifest.json: "
                + ", ".join(sorted(actual_files))
            )
        return {}
    if not manifest_path.is_file():
        raise InventoryError("auxiliary-data manifest is not a regular file")
    normalized = load_auxiliary_manifest(manifest_path)
    undeclared = actual_files - {AUXILIARY_MANIFEST} - set(normalized)
    if undeclared:
        raise InventoryError(
            "auxiliary files are not declared in Data/auxiliary-manifest.json: "
            + ", ".join(sorted(undeclared))
        )
    for name, expected in normalized.items():
        path = auxiliary_data_path(data_dir, name)
        if not path.is_file():
            raise InventoryError(f"required auxiliary-data file is missing: {name}")
        actual = file_digest(path)
        if actual != expected:
            raise InventoryError(f"auxiliary-data hash mismatch: {name}")
    normalized[AUXILIARY_MANIFEST] = file_digest(manifest_path)
    return dict(sorted(normalized.items()))


def copy_validated_auxiliary_data(source_data: Path, destination_data: Path) -> dict[str, str]:
    """Copy the verifier's complete declared context into an isolated inventory."""
    declared = validate_auxiliary_data(source_data)
    for name in declared:
        relative = Path(name)
        source = (
            checked_data_path(source_data, relative)
            if name == AUXILIARY_MANIFEST
            else auxiliary_data_path(source_data, name)
        )
        destination = (
            checked_data_path(destination_data, relative)
            if name == AUXILIARY_MANIFEST
            else auxiliary_data_path(destination_data, name)
        )
        ensure_private_directory(destination.parent)
        shutil.copyfile(source, destination)
        ensure_private_file(destination)
    if validate_auxiliary_data(destination_data) != declared:
        raise InventoryError("copied auxiliary-data context failed validation")
    return declared


def write_json(path: Path, value: dict) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.write-{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def valid_installation_id(value: object) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            value,
        )
    )


def legacy_installation_id(inventory_root: Path) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "agent-property-inventory:" + str(inventory_root.resolve()),
        )
    )


def runtime_binding_payload(runtime_dir: Path, installation_id: str) -> dict:
    if not valid_installation_id(installation_id):
        raise InventoryError("installation_id must be a UUID")
    return {
        "format": 2,
        "installation_id": installation_id,
        "runtime_dir": str(runtime_dir.resolve()),
    }


def read_runtime_binding_record(inventory_root: Path) -> dict:
    marker = inventory_root / RUNTIME_BINDING
    if marker.is_symlink() or not marker.is_file():
        raise InventoryError("inventory runtime binding is missing or is not a regular file")
    try:
        record = json.loads(marker.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InventoryError(f"cannot read inventory runtime binding: {error}") from error
    raw_runtime = record.get("runtime_dir") if isinstance(record, dict) else None
    if (
        not isinstance(record, dict)
        or record.get("format") not in {1, 2}
        or not isinstance(raw_runtime, str)
        or not raw_runtime
        or not Path(raw_runtime).is_absolute()
        or (record.get("format") == 2 and not valid_installation_id(record.get("installation_id")))
        or (record.get("format") == 1 and set(record) != {"format", "runtime_dir"})
        or (
            record.get("format") == 2
            and set(record) != {"format", "installation_id", "runtime_dir"}
        )
    ):
        raise InventoryError("inventory runtime binding is malformed")
    return record


def read_runtime_binding(inventory_root: Path) -> Path:
    return Path(read_runtime_binding_record(inventory_root)["runtime_dir"]).resolve()


def inventory_id_if_available(inventory_root: Path) -> str | None:
    metadata = inventory_root / "Data" / "store" / "metadata.jsonl"
    if metadata.is_symlink() or not metadata.is_file():
        return None
    rows = read_jsonl(metadata)
    if len(rows) != 1 or not isinstance(rows[0].get("inventory_id"), str):
        return None
    return rows[0]["inventory_id"]


def runtime_owner_payload(
    inventory_root: Path,
    runtime_dir: Path,
    media_root: Path | None,
    catalogue_output: Path,
    installation_id: str,
    inventory_id: str | None,
) -> dict:
    if not valid_installation_id(installation_id):
        raise InventoryError("installation_id must be a UUID")
    return {
        "format": 1,
        "inventory_root": str(inventory_root.resolve()),
        "runtime_dir": str(runtime_dir.resolve()),
        "media_root": str(media_root.resolve()) if media_root is not None else None,
        "catalogue_output": str(catalogue_output.resolve()),
        "installation_id": installation_id,
        "inventory_id": inventory_id,
    }


def read_runtime_owner(runtime_dir: Path) -> dict | None:
    marker = runtime_dir / RUNTIME_OWNER
    if marker.is_symlink():
        raise InventoryError("inventory runtime owner marker must not be a symlink")
    if not marker.exists():
        return None
    if not marker.is_file():
        raise InventoryError("inventory runtime owner marker is not a regular file")
    try:
        owner = json.loads(marker.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InventoryError(f"cannot read inventory runtime owner marker: {error}") from error
    required = {
        "format",
        "inventory_root",
        "runtime_dir",
        "media_root",
        "catalogue_output",
        "installation_id",
        "inventory_id",
    }
    if (
        not isinstance(owner, dict)
        or set(owner) != required
        or owner.get("format") != 1
        or not valid_installation_id(owner.get("installation_id"))
        or any(
            not isinstance(owner.get(field), str) or not owner[field]
            for field in (
                "inventory_root",
                "runtime_dir",
                "catalogue_output",
            )
        )
        or (owner.get("media_root") is not None and not isinstance(owner.get("media_root"), str))
        or (
            owner.get("inventory_id") is not None and not isinstance(owner.get("inventory_id"), str)
        )
    ):
        raise InventoryError("inventory runtime owner marker is malformed")
    return owner


def claim_runtime_owner(
    inventory_root: Path,
    runtime_dir: Path,
    media_root: Path | None,
    catalogue_output: Path,
    installation_id: str,
    inventory_id: str | None,
) -> bool:
    """Claim a runtime once, or prove it already belongs to this exact instance."""
    expected = runtime_owner_payload(
        inventory_root,
        runtime_dir,
        media_root,
        catalogue_output,
        installation_id,
        inventory_id,
    )
    ensure_private_directory(runtime_dir)
    marker = runtime_dir / RUNTIME_OWNER
    owner = read_runtime_owner(runtime_dir)
    if owner is None:
        payload = json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            with marker.open("x", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), PRIVATE_FILE_MODE)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            fsync_directory(runtime_dir)
            return True
        except FileExistsError:
            owner = read_runtime_owner(runtime_dir)
    if owner is None:
        raise InventoryError("inventory runtime owner could not be established")
    for field in (
        "inventory_root",
        "runtime_dir",
        "media_root",
        "catalogue_output",
        "installation_id",
    ):
        if owner.get(field) != expected[field]:
            raise InventoryError(
                f"runtime is already owned by a different inventory instance: {runtime_dir}"
            )
    if (
        inventory_id is not None
        and owner.get("inventory_id") is not None
        and owner["inventory_id"] != inventory_id
    ):
        raise InventoryError("runtime inventory identity disagrees with the canonical store")
    if inventory_id is not None and owner.get("inventory_id") is None:
        write_json(marker, expected)
    return False


def validate_runtime_owner(
    inventory_root: Path,
    runtime_dir: Path,
    media_root: Path | None,
    catalogue_output: Path,
    installation_id: str,
    inventory_id: str | None,
    *,
    allow_unset_inventory_id: bool = False,
) -> None:
    """Prove an installed format-2 instance owns its writable projections."""
    owner = read_runtime_owner(runtime_dir)
    if owner is None:
        raise InventoryError("format-2 inventory binding has no reciprocal runtime owner marker")
    expected = runtime_owner_payload(
        inventory_root,
        runtime_dir,
        media_root,
        catalogue_output,
        installation_id,
        inventory_id,
    )
    for field in (
        "inventory_root",
        "runtime_dir",
        "media_root",
        "catalogue_output",
        "installation_id",
    ):
        if owner.get(field) != expected[field]:
            raise InventoryError(
                f"runtime is already owned by a different inventory instance: {runtime_dir}"
            )
    if (
        inventory_id is not None
        and owner.get("inventory_id") != inventory_id
        and not (allow_unset_inventory_id and owner.get("inventory_id") is None)
    ):
        raise InventoryError("runtime inventory identity disagrees with the canonical store")


def establish_existing_instance_ownership(
    args: argparse.Namespace,
    *,
    allow_unset_inventory_id: bool = False,
) -> tuple[str, bool]:
    """Validate format 2, or provision a legacy claim for one-time adoption.

    The caller must hold the inventory lock.  A legacy binding is intentionally
    upgraded only after a full command succeeds, so a crash cannot advertise a
    strict format-2 root without its reciprocal owner and catalogue marker.
    """
    record = read_runtime_binding_record(args.inventory_root)
    if Path(record["runtime_dir"]).resolve() != args.runtime_dir.resolve():
        raise InventoryError(
            "inventory is bound to a different runtime; recover the recorded runtime "
            f"or run runtime-rebind with the path recorded in "
            f"{args.inventory_root / RUNTIME_BINDING}"
        )
    inventory_id = inventory_id_if_available(args.inventory_root)
    if record["format"] == 2:
        installation_id = record["installation_id"]
        validate_runtime_owner(
            args.inventory_root,
            args.runtime_dir,
            args.media_root,
            args.catalogue_output,
            installation_id,
            inventory_id,
            allow_unset_inventory_id=allow_unset_inventory_id,
        )
        return installation_id, False
    installation_id = legacy_installation_id(args.inventory_root)
    claim_runtime_owner(
        args.inventory_root,
        args.runtime_dir,
        args.media_root,
        args.catalogue_output,
        installation_id,
        inventory_id,
    )
    return installation_id, True


def finish_legacy_instance_adoption(args: argparse.Namespace, installation_id: str) -> None:
    """Commit reciprocal ownership after the legacy bundle has verified."""
    inventory_id = inventory_id_if_available(args.inventory_root)
    if inventory_id is None:
        raise InventoryError("legacy adoption requires canonical inventory metadata")
    claim_runtime_owner(
        args.inventory_root,
        args.runtime_dir,
        args.media_root,
        args.catalogue_output,
        installation_id,
        inventory_id,
    )
    write_json(
        args.inventory_root / RUNTIME_BINDING,
        runtime_binding_payload(args.runtime_dir, installation_id),
    )


def prepare_explicit_legacy_adoption(args: argparse.Namespace) -> None:
    """Create the legacy bridge only for an explicit init or migrate command."""
    marker = args.inventory_root / RUNTIME_BINDING
    if path_entry_exists(marker):
        return
    if path_entry_exists(args.catalogue_output):
        raise InventoryError(
            "an existing inventory without a runtime binding requires a new catalogue "
            "output path; existing bytes were preserved"
        )
    owner = read_runtime_owner(args.runtime_dir)
    if owner is not None or runtime_has_unowned_entries(args.runtime_dir):
        raise InventoryError(
            "an existing inventory without a runtime binding requires a new empty runtime"
        )
    write_json(
        marker,
        {"format": 1, "runtime_dir": str(args.runtime_dir.resolve())},
    )


def bindingless_adoption_rollback_state(
    args: argparse.Namespace, *, inventory_id: str | None, runtime_existed: bool
) -> dict:
    """Record the exact durable state that a failed bridge is allowed to remove."""
    marker = args.inventory_root / RUNTIME_BINDING
    record = read_runtime_binding_record(args.inventory_root)
    if record != {"format": 1, "runtime_dir": str(args.runtime_dir.resolve())}:
        raise InventoryError("initialization legacy bridge is not the expected binding")
    installation_id = legacy_installation_id(args.inventory_root)
    return {
        "runtime_existed": runtime_existed,
        "installation_id": installation_id,
        "inventory_id": inventory_id,
        "binding_sha256": file_digest(marker),
        "owner_sha256": None,
        "database_sha256": None,
        "catalogue_sha256": None,
    }


def record_bindingless_adoption_owner(args: argparse.Namespace, state: dict) -> None:
    marker = args.runtime_dir / RUNTIME_OWNER
    expected = runtime_owner_payload(
        args.inventory_root,
        args.runtime_dir,
        args.media_root,
        args.catalogue_output,
        state["installation_id"],
        state["inventory_id"],
    )
    if read_runtime_owner(args.runtime_dir) != expected:
        raise InventoryError("initialization runtime owner is not the expected reciprocal owner")
    state["owner_sha256"] = file_digest(marker)


def record_bindingless_adoption_projection(state: dict, label: str, path: Path) -> None:
    state[f"{label}_sha256"] = file_digest(path)


def adoption_rollback_journal_path(runtime_dir: Path) -> Path:
    return runtime_dir.with_name(f".{runtime_dir.name}{ADOPTION_ROLLBACK_JOURNAL_SUFFIX}")


def adoption_rollback_journal_payload(args: argparse.Namespace, state: dict) -> dict:
    return {
        "format": 1,
        "phase": "catalogue",
        "inventory_root": str(args.inventory_root),
        "runtime": str(args.runtime_dir),
        "media_root": str(args.media_root) if args.media_root is not None else None,
        "catalogue": str(args.catalogue_output),
        **state,
    }


def read_adoption_rollback_journal(args: argparse.Namespace) -> tuple[Path, dict] | None:
    path = adoption_rollback_journal_path(args.runtime_dir)
    if not path_entry_exists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise InventoryError("adoption rollback journal must be a regular file")
    try:
        journal = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InventoryError("adoption rollback journal is malformed") from error
    expected = {
        "format": 1,
        "inventory_root": str(args.inventory_root),
        "runtime": str(args.runtime_dir),
        "media_root": str(args.media_root) if args.media_root is not None else None,
        "catalogue": str(args.catalogue_output),
    }
    required = {
        *expected,
        "phase",
        "runtime_existed",
        "installation_id",
        "inventory_id",
        "binding_sha256",
        "owner_sha256",
        "database_sha256",
        "catalogue_sha256",
    }
    if (
        not isinstance(journal, dict)
        or set(journal) != required
        or any(journal.get(key) != value for key, value in expected.items())
        or journal.get("phase")
        not in {"catalogue", "database", "owner", "runtime", "binding", "done"}
        or not isinstance(journal.get("runtime_existed"), bool)
        or not valid_installation_id(journal.get("installation_id"))
        or (
            journal.get("inventory_id") is not None
            and not isinstance(journal.get("inventory_id"), str)
        )
        or any(
            value is not None
            and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value))
            for value in (
                journal.get("owner_sha256"),
                journal.get("database_sha256"),
                journal.get("catalogue_sha256"),
            )
        )
        or not isinstance(journal.get("binding_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", journal["binding_sha256"])
        or path.parent != args.runtime_dir.parent
        or path.name != adoption_rollback_journal_path(args.runtime_dir).name
    ):
        raise InventoryError("adoption rollback journal is malformed or belongs elsewhere")
    return path, journal


def verify_adoption_rollback_state(args: argparse.Namespace, journal: dict) -> None:
    phase = journal["phase"]
    marker = args.inventory_root / RUNTIME_BINDING
    expected_binding = {"format": 1, "runtime_dir": str(args.runtime_dir.resolve())}
    if phase in {"binding", "done"}:
        if path_entry_exists(marker):
            if (
                marker.is_symlink()
                or not marker.is_file()
                or file_digest(marker) != journal["binding_sha256"]
                or read_runtime_binding_record(args.inventory_root) != expected_binding
            ):
                raise InventoryError(
                    "adoption rollback binding changed outside recovery; bytes were preserved"
                )
    elif (
        marker.is_symlink()
        or not marker.is_file()
        or file_digest(marker) != journal["binding_sha256"]
        or read_runtime_binding_record(args.inventory_root) != expected_binding
    ):
        raise InventoryError(
            "adoption rollback binding changed outside recovery; bytes were preserved"
        )

    catalogue_digest = journal["catalogue_sha256"]
    catalogue_present = path_entry_exists(args.catalogue_output)
    if catalogue_digest is None:
        if catalogue_present:
            raise InventoryError(
                "adoption rollback catalogue changed outside recovery; bytes were preserved"
            )
    elif phase == "catalogue":
        expected_owner_marker = (
            "<!-- canonical-inventory-owner-sha256:"
            + hashlib.sha256(journal["installation_id"].encode()).hexdigest()
            + " -->\n"
        ).encode()
        if catalogue_present:
            if (
                args.catalogue_output.is_symlink()
                or not args.catalogue_output.is_file()
                or file_digest(args.catalogue_output) != catalogue_digest
                or args.catalogue_output.read_bytes().count(expected_owner_marker) != 1
            ):
                raise InventoryError(
                    "adoption rollback catalogue changed outside recovery; bytes were preserved"
                )
    elif catalogue_present:
        raise InventoryError(
            "adoption rollback catalogue changed outside recovery; bytes were preserved"
        )

    required_entries: dict[str, str] = {}
    optional_entries: dict[str, str] = {}
    if phase in {"catalogue", "database", "owner"} and journal["owner_sha256"] is not None:
        expected_owner = runtime_owner_payload(
            args.inventory_root,
            args.runtime_dir,
            args.media_root,
            args.catalogue_output,
            journal["installation_id"],
            journal["inventory_id"],
        )
        owner_marker = args.runtime_dir / RUNTIME_OWNER
        if path_entry_exists(owner_marker):
            if (
                owner_marker.is_symlink()
                or not owner_marker.is_file()
                or file_digest(owner_marker) != journal["owner_sha256"]
                or read_runtime_owner(args.runtime_dir) != expected_owner
            ):
                raise InventoryError(
                    "adoption rollback runtime owner changed outside recovery; bytes were preserved"
                )
        elif phase != "owner":
            raise InventoryError(
                "adoption rollback runtime owner disappeared; bytes were preserved"
            )
        (optional_entries if phase == "owner" else required_entries)[RUNTIME_OWNER] = journal[
            "owner_sha256"
        ]
    if phase in {"catalogue", "database"} and journal["database_sha256"] is not None:
        (optional_entries if phase == "database" else required_entries)["inventory.sqlite"] = (
            journal["database_sha256"]
        )
    allowed_entries = {**required_entries, **optional_entries}

    runtime_present = path_entry_exists(args.runtime_dir)
    if not runtime_present:
        if (
            journal["runtime_existed"]
            or required_entries
            or phase in {"catalogue", "database", "owner"}
        ):
            raise InventoryError("adoption rollback runtime disappeared; bytes were preserved")
        return
    if args.runtime_dir.is_symlink() or not args.runtime_dir.is_dir():
        raise InventoryError("adoption rollback runtime is not a real directory")
    entries = {path.name: path for path in args.runtime_dir.iterdir()}
    if not set(required_entries).issubset(entries) or set(entries) - set(allowed_entries):
        raise InventoryError(
            "adoption rollback runtime changed outside recovery; bytes were preserved"
        )
    for name, digest in allowed_entries.items():
        if name not in entries:
            continue
        path = entries[name]
        if path.is_symlink() or not path.is_file() or file_digest(path) != digest:
            raise InventoryError(
                "adoption rollback runtime projection changed outside recovery; bytes were preserved"
            )


def continue_adoption_rollback(args: argparse.Namespace, path: Path, journal: dict) -> None:
    failure_phase = os.environ.get("PROPERTY_INVENTORY_FAIL_DURING_ADOPTION_ROLLBACK")
    while True:
        verify_adoption_rollback_state(args, journal)
        phase = journal["phase"]
        if phase == "catalogue":
            if path_entry_exists(args.catalogue_output):
                args.catalogue_output.unlink()
                fsync_directory(args.catalogue_output.parent)
            journal["phase"] = "database"
        elif phase == "database":
            database = args.runtime_dir / "inventory.sqlite"
            if path_entry_exists(database):
                database.unlink()
                fsync_directory(args.runtime_dir)
            journal["phase"] = "owner"
        elif phase == "owner":
            owner = args.runtime_dir / RUNTIME_OWNER
            if path_entry_exists(owner):
                owner.unlink()
                fsync_directory(args.runtime_dir)
            journal["phase"] = "runtime"
        elif phase == "runtime":
            if not journal["runtime_existed"] and path_entry_exists(args.runtime_dir):
                args.runtime_dir.rmdir()
                fsync_directory(args.runtime_dir.parent)
            elif journal["runtime_existed"]:
                fsync_directory(args.runtime_dir)
            journal["phase"] = "binding"
        elif phase == "binding":
            marker = args.inventory_root / RUNTIME_BINDING
            if path_entry_exists(marker):
                marker.unlink()
                fsync_directory(args.inventory_root)
            journal["phase"] = "done"
        else:
            path.unlink()
            fsync_directory(path.parent)
            return
        if failure_phase == phase:
            os._exit(97)
        write_json(path, journal)


def recover_pending_adoption_rollback(args: argparse.Namespace) -> bool:
    pending = read_adoption_rollback_journal(args)
    if pending is None:
        return False
    path, journal = pending
    continue_adoption_rollback(args, path, journal)
    return True


def rollback_bindingless_legacy_adoption(args: argparse.Namespace, state: dict) -> None:
    """Journal a failed adoption before removing any durable projection."""
    journal_path = adoption_rollback_journal_path(args.runtime_dir)
    if path_entry_exists(journal_path):
        raise InventoryError("adoption rollback journal already exists; bytes were preserved")
    journal = adoption_rollback_journal_payload(args, state)
    verify_adoption_rollback_state(args, journal)
    write_json(journal_path, journal)
    continue_adoption_rollback(args, journal_path, journal)


def rollback_bindingless_migration_if_unchanged(
    args: argparse.Namespace, state: dict, canonical_before: str
) -> bool:
    """Remove a failed migration bridge only when its canonical generation is untouched."""
    current = canonical_store_digest(args.inventory_root / "Data" / "store")
    if current != canonical_before:
        return False
    rollback_bindingless_legacy_adoption(args, state)
    return True


def runtime_has_unowned_entries(runtime_dir: Path) -> bool:
    if runtime_dir.is_symlink():
        raise InventoryError("inventory runtime root must not be a symlink")
    return runtime_dir.exists() and any(runtime_dir.iterdir())


def write_inventory_gitignore(inventory_root: Path) -> None:
    path = inventory_root / INVENTORY_GITIGNORE
    path.write_text(f"/{RUNTIME_BINDING}\n", encoding="utf-8")


def slug(value: str, limit: int = 64) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return (result or "record")[:limit].rstrip("-")


def valid_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value}") from error


def valid_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid ISO timestamp: {value}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("ISO timestamp must include a UTC offset")
    return parsed.isoformat()


def recorded_timestamp(value: str | None) -> str:
    return value or datetime.now().astimezone().isoformat()


def positive_number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be a number") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("value must be finite and greater than zero")
    return number


def non_negative_number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be a number") from error
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("value must be finite and non-negative")
    return number


def currency_code(value: str) -> str:
    if re.fullmatch(r"[A-Z]{3}", value) is None:
        raise argparse.ArgumentTypeError(
            "currency must be a three-letter uppercase code"
        )
    return value


def _strict_argument_json(value: str) -> object:
    try:
        return strict_json_loads(value)
    except StrictJSONError as error:
        raise argparse.ArgumentTypeError(f"invalid JSON: {error}") from error


def json_object(value: str) -> dict:
    try:
        parsed = _strict_argument_json(value)
    except argparse.ArgumentTypeError as error:
        raise argparse.ArgumentTypeError(f"invalid JSON object: {error}") from error
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    return parsed


def json_array(value: str) -> list:
    try:
        parsed = _strict_argument_json(value)
    except argparse.ArgumentTypeError as error:
        raise argparse.ArgumentTypeError(f"invalid JSON array: {error}") from error
    if not isinstance(parsed, list):
        raise argparse.ArgumentTypeError("value must be a JSON array")
    return parsed


def run(command: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise InventoryError(f"command failed ({' '.join(command)}): {detail}")
    return completed.stdout.strip()


class Store:
    def __init__(self, store_dir: Path, *, allow_legacy: bool = False):
        if store_dir.parent.is_symlink():
            raise InventoryError("canonical Data directory must not be a symlink")
        if store_dir.is_symlink() or not store_dir.is_dir():
            raise InventoryError("canonical store must be a real directory, not a symlink")
        self.store_dir = store_dir
        metadata_path = store_dir / "metadata.jsonl"
        if metadata_path.is_symlink():
            raise InventoryError("canonical table must not be a symlink: metadata.jsonl")
        if metadata_path.exists():
            metadata = read_jsonl(metadata_path)
            if len(metadata) != 1 or type(metadata[0].get("schema_version")) is not int:
                raise InventoryError(
                    "metadata.jsonl must contain exactly one integer schema_version"
                )
            self.schema_version = metadata[0]["schema_version"]
        else:
            self.schema_version = 1
        if self.schema_version > SCHEMA_VERSION:
            raise InventoryError(
                f"inventory schema {self.schema_version} is newer than supported schema {SCHEMA_VERSION}"
            )
        if self.schema_version < SCHEMA_VERSION and not allow_legacy:
            raise InventoryError(
                f"inventory schema {self.schema_version} requires migration to schema {SCHEMA_VERSION}"
            )
        expected_entries = {
            f"{table}.jsonl" for table in TABLES_BY_SCHEMA[self.schema_version]
        }
        actual_entries = {entry.name for entry in store_dir.iterdir()}
        if actual_entries != expected_entries:
            extras = sorted(actual_entries - expected_entries)
            missing = sorted(expected_entries - actual_entries)
            detail = []
            if extras:
                detail.append("unexpected: " + ", ".join(extras))
            if missing:
                detail.append("missing: " + ", ".join(missing))
            raise InventoryError(
                "canonical store must contain exactly its schema tables ("
                + "; ".join(detail)
                + ")"
            )
        self.rows = {}
        for table in TABLES:
            path = store_dir / f"{table}.jsonl"
            if path.is_symlink():
                raise InventoryError(f"canonical table must not be a symlink: {path.name}")
            if path.exists():
                self.rows[table] = read_jsonl(path)
            elif allow_legacy and table not in TABLES_BY_SCHEMA[self.schema_version]:
                self.rows[table] = []
            else:
                raise InventoryError(f"missing canonical table: {path.name}")

    def save(self, destination: Path) -> None:
        ensure_private_directory(destination)
        self.validate_ids()
        for table in TABLES:
            write_jsonl(destination / f"{table}.jsonl", self.rows[table])

    def ids(self, table: str) -> set[str]:
        key = ID_FIELDS[table]
        return {row[key] for row in self.rows[table]}

    def get(self, table: str, record_id: str) -> dict:
        key = ID_FIELDS[table]
        matches = [row for row in self.rows[table] if row[key] == record_id]
        if len(matches) != 1:
            raise InventoryError(f"expected one {table} row for {record_id}, found {len(matches)}")
        return matches[0]

    def allocate(self, table: str, base: str) -> str:
        existing = self.ids(table)
        if base not in existing:
            return base
        suffix = 2
        while f"{base}-{suffix}" in existing:
            suffix += 1
        return f"{base}-{suffix}"

    def validate_ids(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise InventoryError(
                f"cannot save schema {self.schema_version}; migrate to schema {SCHEMA_VERSION} first"
            )
        if len(self.rows["metadata"]) != 1:
            raise InventoryError("metadata.jsonl must contain exactly one row")
        if self.rows["metadata"][0].get("schema_version") != SCHEMA_VERSION:
            raise InventoryError(f"metadata schema_version must be {SCHEMA_VERSION}")
        for item in self.rows["items"]:
            if error := location_assignment_error(
                self.rows["locations"],
                item.get("location_id"),
                item.get("container_id"),
            ):
                raise InventoryError(
                    "invalid location/container assignment for "
                    f"{item.get('item_id')}: {error}"
                )
        for table, key in ID_FIELDS.items():
            values = [row.get(key) for row in self.rows[table]]
            if any(not value for value in values):
                raise InventoryError(f"{table} contains a blank {key}")
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if duplicates:
                raise InventoryError(f"duplicate {key} in {table}: {duplicates}")
        pairs = [(row.get("item_id"), row.get("evidence_id")) for row in self.rows["item_evidence"]]
        if len(pairs) != len(set(pairs)):
            raise InventoryError("duplicate item_evidence pair")
        evidence_assets = [
            (row.get("evidence_id"), row.get("asset_id"), row.get("role"))
            for row in self.rows["evidence_assets"]
        ]
        if len(evidence_assets) != len(set(evidence_assets)):
            raise InventoryError("duplicate evidence_assets link")
        model_interfaces = [
            (row.get("model_id"), row.get("interface_id"), row.get("role"))
            for row in self.rows["model_interfaces"]
        ]
        if len(model_interfaces) != len(set(model_interfaces)):
            raise InventoryError("duplicate model_interfaces link")
        maintenance_items = [
            (row.get("maintenance_session_id"), row.get("item_id"))
            for row in self.rows["maintenance_session_items"]
        ]
        if len(maintenance_items) != len(set(maintenance_items)):
            raise InventoryError("duplicate maintenance session item link")
        for side in ("item_id", "location_id"):
            embodiments = [row.get(side) for row in self.rows["location_embodiments"]]
            if len(embodiments) != len(set(embodiments)):
                raise InventoryError(f"duplicate location embodiment {side}")
        sequences = [row.get("sequence") for row in self.rows["inventory_events"]]
        if any(not isinstance(sequence, int) or sequence < 1 for sequence in sequences):
            raise InventoryError("inventory event sequence must be a positive integer")
        if len(sequences) != len(set(sequences)):
            raise InventoryError("duplicate inventory event sequence")


def data_paths(inventory_root: Path, runtime_dir: Path) -> dict[str, Path]:
    data_dir = inventory_root / "Data"
    instance_paths = _INSTANCE_PATHS.get()
    if instance_paths is None:
        media_root = None
        catalogue = inventory_root / "Inventory.md"
        catalogue_scope = "personal"
    else:
        media_root, catalogue, catalogue_scope = instance_paths
    if catalogue_scope not in SCOPE_MAX_SENSITIVITY:
        raise InventoryError(f"invalid catalogue scope: {catalogue_scope}")
    return {
        "inventory_root": inventory_root,
        "runtime": runtime_dir,
        "data": data_dir,
        "store": data_dir / "store",
        "source": data_dir / "source-inventory.json",
        "candidates": data_dir / "account-candidates.json",
        "policy": data_dir / "verification_policy.json",
        "schema": HERE / "schema.sql",
        "database": runtime_dir / "inventory.sqlite",
        "catalogue": catalogue,
        "catalogue_scope": catalogue_scope,
        "rebuild": HERE / "rebuild.py",
        "render": HERE / "render.py",
        "verify": HERE / "verify.py",
        "backups": runtime_dir / "backups",
        "media_root": media_root,
        "transaction_journal": runtime_dir / ".property-inventory-transaction.json",
        "transaction_workspace": runtime_dir / ".property-inventory-transaction",
        "restore_journal": runtime_dir / RESTORE_JOURNAL,
        "capture_media_journal": runtime_dir / CAPTURE_MEDIA_JOURNAL,
        "capture_media_workspace": runtime_dir / CAPTURE_MEDIA_WORKSPACE,
        "runtime_binding": inventory_root / RUNTIME_BINDING,
        "runtime_owner": runtime_dir / RUNTIME_OWNER,
        "degraded_marker": inventory_root / DEGRADED_MARKER,
    }


def degraded_reasons(paths: dict[str, Path]) -> list[str]:
    marker = paths["degraded_marker"]
    if not path_entry_exists(marker):
        return []
    if marker.is_symlink() or not marker.is_file():
        raise InventoryError("inventory degraded-state marker is not a regular file")
    try:
        record = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise InventoryError(f"cannot read inventory degraded-state marker: {error}") from error
    reasons = record.get("reasons") if isinstance(record, dict) else None
    if (
        not isinstance(record, dict)
        or record.get("format") != 1
        or not isinstance(reasons, list)
        or not reasons
        or any(not isinstance(reason, str) or not reason for reason in reasons)
    ):
        raise InventoryError("inventory degraded-state marker is malformed")
    return reasons


def canonical_lock_path(inventory_root: Path) -> Path:
    identity = hashlib.sha256(str(inventory_root.resolve()).encode()).hexdigest()
    lock_dir = Path(tempfile.gettempdir()) / "agent-property-inventory-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / f"{identity}.lock"


def validate_locked_instance_paths(inventory_root: Path) -> None:
    """Revalidate writable instance paths after acquiring the canonical lock."""
    expected = _LOCK_INSTANCE_PATHS.get()
    if expected is None:
        return
    (
        expected_root,
        runtime_dir,
        media_root,
        catalogue_output,
        allow_unset_inventory_id,
    ) = expected
    if inventory_root.resolve() != expected_root.resolve():
        return
    binding = read_runtime_binding_record(inventory_root)
    if Path(binding["runtime_dir"]).resolve() != runtime_dir.resolve():
        raise InventoryError(
            "inventory paths changed after this command started; retry with current config"
        )
    installation_id = (
        binding["installation_id"]
        if binding["format"] == 2
        else legacy_installation_id(inventory_root)
    )
    try:
        validate_runtime_owner(
            inventory_root,
            runtime_dir,
            media_root,
            catalogue_output,
            installation_id,
            inventory_id_if_available(inventory_root),
            allow_unset_inventory_id=allow_unset_inventory_id,
        )
    except InventoryError as error:
        raise InventoryError(
            "inventory paths changed after this command started; retry with current config"
        ) from error


class InventoryLock:
    """Canonical lock that revalidates the instance before protected work."""

    def __init__(self, inventory_root: Path) -> None:
        self.inventory_root = inventory_root
        self.file_lock = FileLock(canonical_lock_path(inventory_root), timeout=0)

    def acquire(self) -> object:
        acquired = self.file_lock.acquire()
        try:
            validate_locked_instance_paths(self.inventory_root)
        except BaseException:
            self.file_lock.release()
            raise
        return acquired

    def release(self) -> None:
        self.file_lock.release()

    def __enter__(self) -> InventoryLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()


def inventory_lock(inventory_root: Path) -> InventoryLock:
    return InventoryLock(inventory_root)


def media_lock(media_root: Path) -> FileLock:
    identity = hashlib.sha256(str(media_root.resolve()).encode()).hexdigest()
    lock_dir = Path(tempfile.gettempdir()) / "agent-property-inventory-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(lock_dir / f"media-{identity}.lock", timeout=0)


def verify_bundle(
    paths: dict[str, Path],
    store: Path,
    database: Path,
    catalogue: Path,
    *,
    on_projection: Callable[[str, Path], None] | None = None,
) -> dict:
    ensure_private_directory(paths["inventory_root"])
    harden_private_tree(paths["data"])
    harden_private_tree(store)
    harden_private_tree(paths["runtime"])
    if paths["media_root"] is not None:
        harden_private_tree(paths["media_root"])
    auxiliary = validate_auxiliary_data(paths["data"])
    degradation = degraded_reasons(paths)
    binding = read_runtime_binding_record(paths["inventory_root"])
    installation_id = (
        binding["installation_id"]
        if binding["format"] == 2
        else legacy_installation_id(paths["inventory_root"])
    )
    ensure_private_directory(database.parent)
    catalogue.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staged_database = database.with_name(f".{database.name}.verify-{token}")
    staged_catalogue = catalogue.with_name(f".{catalogue.name}.verify-{token}")
    try:
        try:
            created_on = (
                catalogue_created_on(catalogue.read_text(encoding="utf-8"))
                if catalogue.exists()
                else None
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise InventoryError(
                f"cannot read generated catalogue creation metadata: {error}"
            ) from error
        generation_before = canonical_store_digest(store)
        rebuild_output = run(
            [
                sys.executable,
                str(paths["rebuild"]),
                "--store",
                str(store),
                "--schema",
                str(paths["schema"]),
                "--database",
                str(staged_database),
            ]
        )
        render_command = [
            sys.executable,
            str(paths["render"]),
            "--database",
            str(staged_database),
            "--output",
            str(staged_catalogue),
            "--scope",
            paths["catalogue_scope"],
            "--installation-id",
            installation_id,
        ]
        if created_on is not None:
            render_command.extend(("--created-on", created_on))
        render_output = run(render_command)
        verify_command = [
            sys.executable,
            str(paths["verify"]),
            "--store",
            str(store),
            "--database",
            str(staged_database),
            "--markdown",
            str(staged_catalogue),
            "--catalogue-scope",
            paths["catalogue_scope"],
            "--installation-id",
            installation_id,
        ]
        for flag, key in (
            ("--source-inventory", "source"),
            ("--account-candidates", "candidates"),
            ("--policy", "policy"),
        ):
            if paths[key].exists():
                verify_command.extend((flag, str(paths[key])))
        if paths["media_root"] is not None:
            verify_command.extend(("--media-root", str(paths["media_root"])))
        verify_output = run(verify_command)
        verification = json.loads(verify_output)
        if verification.get("status") != "pass" or verification.get("failures"):
            raise InventoryError(f"verification failed: {verification}")
        generation_after = canonical_store_digest(store)
        if generation_after != generation_before:
            raise InventoryError("canonical generation changed during verification")
        try:
            with sqlite3.connect(staged_database) as connection:
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise InventoryError("foreign-key check failed before attestation")
                connection.execute(
                    "INSERT INTO verification_state(store_digest) VALUES (?)",
                    (generation_after,),
                )
                connection.commit()
        except sqlite3.Error as error:
            raise InventoryError("cannot attest verified canonical generation") from error
        fsync_file(staged_database)

        try:
            write_output_atomic(
                catalogue, staged_catalogue.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise InventoryError(f"cannot publish verified catalogue: {error}") from error
        if on_projection is not None:
            on_projection("catalogue", catalogue)
        if database.is_symlink() or database.parent.is_symlink():
            raise InventoryError("database path must not contain a managed symlink")
        os.replace(staged_database, database)
        ensure_private_file(database)
        fsync_directory(database.parent)
        if on_projection is not None:
            on_projection("database", database)

        projected_rebuild = json.loads(rebuild_output)
        projected_rebuild["database"] = str(database)
        projected_render = render_output.replace(
            str(staged_catalogue), str(catalogue)
        ).splitlines()
        result = {
            "status": "degraded_unsafe_legacy" if degradation else "pass",
            "rebuild": projected_rebuild,
            "render": projected_render,
            "verification": verification,
            "foreign_key_failures": 0,
            "auxiliary_files": len(auxiliary),
        }
        if degradation:
            result["verification"]["status"] = "degraded_unsafe_legacy"
            result["degraded_reasons"] = degradation
        return result
    finally:
        staged_database.unlink(missing_ok=True)
        staged_catalogue.unlink(missing_ok=True)


def verify_rebind_source(paths: dict[str, Path]) -> dict:
    """Verify a quiescent source without requiring its migration first."""
    source = Store(paths["store"], allow_legacy=True)
    if source.schema_version == SCHEMA_VERSION:
        return verify_bundle(
            paths,
            paths["store"],
            paths["database"],
            paths["catalogue"],
        )
    if degradation := degraded_reasons(paths):
        raise InventoryError(
            "cannot rebind an inventory quarantined by a degraded restore: "
            + "; ".join(degradation)
        )
    try:
        migration = validate_migration(source.schema_version)
    except CompatibilityError as error:
        raise InventoryError(f"cannot rebind legacy inventory: {error}") from error
    ensure_private_directory(paths["inventory_root"])
    harden_private_tree(paths["data"])
    harden_private_tree(paths["store"])
    harden_private_tree(paths["runtime"])
    if paths["media_root"] is not None:
        harden_private_tree(paths["media_root"])
    auxiliary = validate_auxiliary_data(paths["data"])
    generation_before = canonical_store_digest(paths["store"])
    with tempfile.TemporaryDirectory(
        prefix="property-inventory-rebind-preflight-"
    ) as temporary_name:
        temporary = Path(temporary_name)
        preflight_root = temporary / "inventory"
        preflight_runtime = temporary / "runtime"
        preflight_catalogue = temporary / "Inventory.md"
        shutil.copytree(paths["data"], preflight_root / "Data", symlinks=True)
        command = [
            sys.executable,
            "-m",
            "property_inventory.cli",
            "--inventory-root",
            str(preflight_root),
            "--runtime-dir",
            str(preflight_runtime),
            "--catalogue-output",
            str(preflight_catalogue),
            "--catalogue-scope",
            paths["catalogue_scope"],
            "--scope",
            "private",
        ]
        if paths["media_root"] is not None:
            command.extend(("--media-root", str(paths["media_root"])))
        command.append("migrate")
        try:
            preflight = json.loads(run(command))
        except (InventoryError, json.JSONDecodeError) as error:
            raise InventoryError(
                f"legacy inventory cannot complete its declared migration: {error}"
            ) from error
        if (
            preflight.get("result", {}).get("from_schema") != source.schema_version
            or preflight.get("result", {}).get("to_schema") != SCHEMA_VERSION
            or preflight.get("checks", {}).get("verification", {}).get("failures")
        ):
            raise InventoryError("legacy inventory migration preflight did not pass")
    generation_after = canonical_store_digest(paths["store"])
    if generation_after != generation_before:
        raise InventoryError("canonical generation changed during rebind verification")
    return {
        "status": "migration_required",
        "schema_version": source.schema_version,
        "target_schema_version": SCHEMA_VERSION,
        "migration_action": migration.action,
        "store_digest": generation_before,
        "auxiliary_files": len(auxiliary),
    }


def git_store_is_clean(data_dir: Path, *, continue_batch: bool) -> None:
    probe = subprocess.run(
        ["git", "-C", str(data_dir), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode:
        return
    root = Path(probe.stdout.strip())
    relative_store = (data_dir / "store").resolve().relative_to(root.resolve())
    status = run(["git", "-C", str(root), "status", "--porcelain", "--", str(relative_store)])
    if status and not continue_batch:
        raise InventoryError(
            "canonical store has uncommitted changes; another writer may be active. "
            "After confirming this is the same single-writer batch, rerun with "
            "--continue-batch before the subcommand:\n" + status
        )


def backup_store(paths: dict[str, Path], operation: str) -> Path:
    backups = checked_managed_path(paths["runtime"], Path("backups"), "runtime")
    ensure_private_directory(backups)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    destination = backups / f"{stamp}-{slug(operation, 32)}"
    counter = 2
    while path_entry_exists(destination):
        destination = backups / f"{stamp}-{slug(operation, 28)}-{counter}"
        counter += 1
    checked_managed_path(paths["runtime"], destination.relative_to(paths["runtime"]), "runtime")
    shutil.copytree(paths["store"], destination)
    harden_private_tree(destination)
    fsync_tree(destination)
    fsync_directory(destination.parent)
    return destination


def changed_store_files(staged: Path, live: Path) -> list[str]:
    return [
        source.name
        for source in sorted(staged.glob("*.jsonl"))
        if not (live / source.name).exists()
        or source.read_bytes() != (live / source.name).read_bytes()
    ]


def canonical_generation(store_dir: Path) -> dict[str, str]:
    return {path.name: file_digest(path) for path in sorted(store_dir.glob("*.jsonl"))}


def remove_transaction_state(paths: dict[str, Path]) -> None:
    if paths["transaction_workspace"].exists():
        shutil.rmtree(paths["transaction_workspace"])
        fsync_directory(paths["runtime"])
    paths["transaction_journal"].unlink(missing_ok=True)
    fsync_directory(paths["runtime"])


def prepare_transaction(paths: dict[str, Path], staged: Path, changed: list[str]) -> dict:
    workspace = paths["transaction_workspace"]
    if path_entry_exists(workspace) or path_entry_exists(paths["transaction_journal"]):
        raise InventoryError("an unrecovered inventory transaction already exists")
    old_store = workspace / "old"
    new_store = workspace / "new"
    ensure_private_directory(old_store)
    ensure_private_directory(new_store)
    fsync_directory(workspace)
    fsync_directory(paths["runtime"])
    files = []
    for name in changed:
        old = paths["store"] / name
        new = staged / name
        old_sha256 = None
        if old.exists():
            durable_copy(old, old_store / name)
            old_sha256 = file_digest(old)
        durable_copy(new, new_store / name)
        files.append(
            {
                "name": name,
                "old_sha256": old_sha256,
                "new_sha256": file_digest(new),
            }
        )
    fsync_directory(old_store)
    fsync_directory(new_store)
    fsync_directory(workspace)
    live_metadata_path = paths["store"] / "metadata.jsonl"
    source_schema_version = 1
    if live_metadata_path.exists():
        live_metadata = read_jsonl(live_metadata_path)
        if len(live_metadata) != 1 or type(live_metadata[0].get("schema_version")) is not int:
            raise InventoryError("live metadata has no valid schema version")
        source_schema_version = live_metadata[0]["schema_version"]
    metadata_path = staged / "metadata.jsonl"
    target_schema_version = 1
    if metadata_path.exists():
        metadata = read_jsonl(metadata_path)
        if len(metadata) != 1 or type(metadata[0].get("schema_version")) is not int:
            raise InventoryError("staged metadata has no valid schema version")
        target_schema_version = metadata[0]["schema_version"]
    journal = {
        "format": 2,
        "schema_version": target_schema_version,
        "source_schema_version": source_schema_version,
        "source_generation": canonical_generation(paths["store"]),
        "target_generation": canonical_generation(staged),
        "phase": "prepared",
        "files": files,
    }
    workspace_journal = workspace / "journal.json"
    write_json(workspace_journal, journal)
    os.replace(workspace_journal, paths["transaction_journal"])
    fsync_directory(paths["runtime"])
    if os.environ.get("PROPERTY_INVENTORY_FAIL_AFTER_PREPARE") == "1":
        os._exit(96)
    return journal


def replace_prepared_store(paths: dict[str, Path], journal: dict) -> None:
    new_store = paths["transaction_workspace"] / "new"
    fail_after = os.environ.get("PROPERTY_INVENTORY_FAIL_AFTER_REPLACE")
    for index, record in enumerate(journal["files"], start=1):
        source = new_store / record["name"]
        destination = paths["store"] / record["name"]
        if not source.exists() or file_digest(source) != record["new_sha256"]:
            raise InventoryError(
                f"prepared transaction file is missing or corrupt: {record['name']}"
            )
        os.replace(source, destination)
        fsync_directory(paths["store"])
        if fail_after and int(fail_after) == index:
            os._exit(97)


def restore_prepared_store(paths: dict[str, Path], journal: dict) -> None:
    old_store = paths["transaction_workspace"] / "old"
    for record in journal["files"]:
        destination = paths["store"] / record["name"]
        if record["old_sha256"] is None:
            destination.unlink(missing_ok=True)
            continue
        source = old_store / record["name"]
        temporary = paths["store"] / f".{record['name']}.restore-{os.getpid()}"
        durable_copy(source, temporary)
        os.replace(temporary, destination)
    fsync_directory(paths["store"])


def _recover_store_transaction(paths: dict[str, Path]) -> str | None:
    journal_path = paths["transaction_journal"]
    workspace = paths["transaction_workspace"]
    live_metadata = paths["store"] / "metadata.jsonl"
    if live_metadata.exists():
        metadata = read_jsonl(live_metadata)
        if len(metadata) != 1 or type(metadata[0].get("schema_version")) is not int:
            raise InventoryError("metadata.jsonl has no valid schema version")
        version = metadata[0]["schema_version"]
        if version > SCHEMA_VERSION:
            raise InventoryError(
                f"inventory schema {version} is newer than supported schema {SCHEMA_VERSION}"
            )
    if journal_path.is_symlink() or workspace.is_symlink():
        raise InventoryError("inventory transaction state must not be a symlink")
    journal_present = path_entry_exists(journal_path)
    workspace_present = path_entry_exists(workspace)
    if not journal_present and not workspace_present:
        return None
    if not journal_present:
        raise InventoryError(
            "transaction workspace exists without a journal; preserved for inspection"
        )
    journal = json.loads(journal_path.read_text())
    journal_schema = journal.get("schema_version")
    if type(journal_schema) is not int:
        raise InventoryError("inventory transaction journal has no integer schema_version")
    if journal_schema > SCHEMA_VERSION:
        raise InventoryError(
            f"pending transaction schema {journal_schema} is newer than supported schema {SCHEMA_VERSION}"
        )
    if journal.get("format") != 2:
        raise InventoryError(
            "pending transaction journal lacks complete generation manifests; "
            "restore its named backup before retrying"
        )
    valid_names = {f"{table}.jsonl" for tables in TABLES_BY_SCHEMA.values() for table in tables}
    generations: dict[str, dict[str, str]] = {}
    for generation in ("source", "target"):
        manifest = journal.get(f"{generation}_generation")
        if (
            not isinstance(manifest, dict)
            or not manifest
            or any(
                name not in valid_names
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                for name, digest in manifest.items()
            )
        ):
            raise InventoryError(
                f"pending transaction has an invalid {generation} generation manifest"
            )
        generations[generation] = manifest
    source_schema = journal.get("source_schema_version")
    if source_schema is None:
        metadata_record = next(
            (
                record
                for record in journal.get("files", [])
                if isinstance(record, dict) and record.get("name") == "metadata.jsonl"
            ),
            None,
        )
        old_metadata_path = workspace / "old" / "metadata.jsonl"
        if metadata_record and metadata_record.get("old_sha256") is None:
            source_schema = 1
        elif old_metadata_path.exists():
            old_metadata = read_jsonl(old_metadata_path)
            if len(old_metadata) != 1 or type(old_metadata[0].get("schema_version")) is not int:
                raise InventoryError("pending transaction has invalid source schema metadata")
            source_schema = old_metadata[0].get("schema_version")
        elif (
            metadata_record
            and live_metadata.exists()
            and file_digest(live_metadata) == metadata_record.get("old_sha256")
        ):
            source_schema = read_jsonl(live_metadata)[0].get("schema_version")
        else:
            source_schema = journal_schema
    if type(source_schema) is not int or source_schema not in TABLES_BY_SCHEMA:
        raise InventoryError("pending transaction has an unsupported source schema")
    if journal_schema not in TABLES_BY_SCHEMA:
        raise InventoryError("pending transaction has an unsupported target schema")
    for metadata_path in (
        workspace / "old" / "metadata.jsonl",
        workspace / "new" / "metadata.jsonl",
    ):
        if not metadata_path.exists():
            continue
        metadata = read_jsonl(metadata_path)
        if len(metadata) != 1 or type(metadata[0].get("schema_version")) is not int:
            raise InventoryError(f"invalid schema metadata in pending transaction: {metadata_path}")
        version = metadata[0]["schema_version"]
        if version > SCHEMA_VERSION:
            raise InventoryError(
                f"inventory schema {version} is newer than supported schema {SCHEMA_VERSION}"
            )
    if not isinstance(journal.get("files"), list):
        raise InventoryError("unsupported or malformed inventory transaction journal")
    for record in journal["files"]:
        if (
            not isinstance(record, dict)
            or set(record) != {"name", "old_sha256", "new_sha256"}
            or record.get("name") not in valid_names
            or (
                record.get("old_sha256") is not None
                and not re.fullmatch(r"[0-9a-f]{64}", str(record["old_sha256"]))
            )
            or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("new_sha256", "")))
        ):
            raise InventoryError("malformed inventory transaction file record")
    source_tables = TABLES_BY_SCHEMA[source_schema]
    target_tables = TABLES_BY_SCHEMA[journal_schema]
    expected_source_names = {f"{table}.jsonl" for table in source_tables}
    if set(generations["source"]) != expected_source_names:
        raise InventoryError("pending transaction source manifest has the wrong table set")
    if set(generations["target"]) != {f"{table}.jsonl" for table in target_tables}:
        raise InventoryError("pending transaction target manifest has the wrong table set")
    records_by_name = {record["name"]: record for record in journal["files"]}
    if len(records_by_name) != len(journal["files"]):
        raise InventoryError("pending transaction has duplicate file records")
    expected_changed = {
        name
        for name, digest in generations["target"].items()
        if generations["source"].get(name) != digest
    }
    if set(records_by_name) != expected_changed:
        raise InventoryError("pending transaction file records do not match its manifests")
    for name, record in records_by_name.items():
        if (
            record["old_sha256"] != generations["source"].get(name)
            or record["new_sha256"] != generations["target"][name]
        ):
            raise InventoryError("pending transaction file hashes disagree with its manifests")
        if workspace.exists():
            old_backup = workspace / "old" / name
            if record["old_sha256"] is None:
                if path_entry_exists(old_backup):
                    raise InventoryError(f"transaction has an unexpected old backup: {name}")
            elif (
                old_backup.is_symlink()
                or not old_backup.is_file()
                or file_digest(old_backup) != record["old_sha256"]
            ):
                raise InventoryError(f"transaction backup is missing or corrupt: {name}")
            new_backup = workspace / "new" / name
            if path_entry_exists(new_backup) and (
                new_backup.is_symlink()
                or not new_backup.is_file()
                or file_digest(new_backup) != record["new_sha256"]
            ):
                raise InventoryError(f"prepared transaction file is corrupt: {name}")

    def matches(record: dict, generation: str) -> bool:
        path = paths["store"] / record["name"]
        expected = record[f"{generation}_sha256"]
        if expected is None:
            return not path_entry_exists(path)
        return path.is_file() and not path.is_symlink() and file_digest(path) == expected

    def complete_generation_matches(generation: str) -> bool:
        manifest = generations[generation]
        return canonical_generation(paths["store"]) == manifest

    def verify_recovered_generation(schema_version: int) -> None:
        if schema_version == SCHEMA_VERSION:
            verify_bundle(
                paths,
                paths["store"],
                paths["database"],
                paths["catalogue"],
            )
            return
        recovered = Store(paths["store"], allow_legacy=True)
        if recovered.schema_version != schema_version:
            raise InventoryError("recovered canonical generation has an unexpected schema version")

    live_generation = canonical_generation(paths["store"])
    allowed_names = set(generations["source"]) | set(generations["target"])
    if not set(live_generation) <= allowed_names:
        raise InventoryError("canonical store contains files outside the pending transaction")
    for name, digest in live_generation.items():
        allowed_hashes = {manifest[name] for manifest in generations.values() if name in manifest}
        if digest not in allowed_hashes:
            raise InventoryError(f"canonical file changed outside the pending transaction: {name}")
    for name in set(generations["source"]) & set(generations["target"]):
        if name not in live_generation:
            raise InventoryError(
                f"canonical file disappeared during the pending transaction: {name}"
            )
    for record in journal["files"]:
        if not matches(record, "old") and not matches(record, "new"):
            raise InventoryError(
                f"canonical file changed outside the pending transaction: {record['name']}"
            )
    all_new = complete_generation_matches("target")
    all_old = complete_generation_matches("source")
    if not workspace.exists():
        if not all_new and not all_old:
            raise InventoryError(
                "transaction workspace is missing while the canonical store is mixed"
            )
        verify_recovered_generation(journal_schema if all_new else source_schema)
        journal_path.unlink()
        fsync_directory(paths["runtime"])
        return "completed" if all_new else "rolled_back"
    if all_new:
        try:
            verify_recovered_generation(journal_schema)
        except Exception:
            all_new = False
    if not all_new:
        old_store = workspace / "old"
        for record in journal["files"]:
            if record["old_sha256"] is None:
                continue
            source = old_store / record["name"]
            if not source.exists() or file_digest(source) != record["old_sha256"]:
                raise InventoryError(f"transaction backup is missing or corrupt: {record['name']}")
        restore_prepared_store(paths, journal)
        if not complete_generation_matches("source"):
            raise InventoryError("rollback did not restore the exact source generation")
        verify_recovered_generation(source_schema)
    result = "completed" if all_new else "rolled_back"
    remove_transaction_state(paths)
    return result


def recover_pending_capture_media(paths: dict[str, Path]) -> str | None:
    """Finish or roll back a journaled proposal media installation."""
    journal_path = paths["capture_media_journal"]
    workspace = paths["capture_media_workspace"]
    journal_present = path_entry_exists(journal_path)
    workspace_present = path_entry_exists(workspace)
    if not journal_present and not workspace_present:
        return None
    if journal_path.is_symlink() or workspace.is_symlink():
        raise InventoryError("capture media transaction state must not be a symlink")
    if not journal_present:
        if not workspace.is_dir():
            raise InventoryError("capture media transaction workspace is malformed")
        # Media is installed only after the journal is durable. A lone workspace
        # therefore proves that the process died while staging private copies and
        # can be removed without touching either the canonical store or media root.
        shutil.rmtree(workspace)
        fsync_directory(paths["runtime"])
        return "rolled_back_staging"
    if not workspace_present or not workspace.is_dir():
        raise InventoryError("capture media transaction is incomplete; preserved for inspection")
    if paths["media_root"] is None:
        raise InventoryError("capture media recovery requires the configured media root")
    try:
        journal = strict_json_value(
            read_bounded_regular_input(
                journal_path,
                maximum_bytes=MAX_CAPTURE_METADATA_BYTES,
                label="capture media journal",
            ).decode("utf-8"),
            "capture media journal",
        )
    except (OSError, UnicodeDecodeError) as error:
        raise InventoryError(f"cannot read capture media journal: {error}") from error
    entries = journal.get("entries") if isinstance(journal, dict) else None
    if (
        not isinstance(journal, dict)
        or set(journal) != {"entries", "format", "inventory_id", "media_root", "proposal_id"}
        or journal.get("format") != 1
        or not re.fullmatch(r"proposal-[0-9a-f-]{36}", str(journal.get("proposal_id", "")))
        or journal.get("media_root") != str(paths["media_root"])
        or not isinstance(entries, list)
        or not entries
    ):
        raise InventoryError("capture media journal is malformed or belongs elsewhere")
    store = Store(paths["store"])
    if journal.get("inventory_id") != store.rows["metadata"][0]["inventory_id"]:
        raise InventoryError("capture media journal belongs to another inventory")
    committed = any(
        row["proposal_id"] == journal["proposal_id"] for row in store.rows["proposal_commits"]
    )
    normalized: list[tuple[str, bool, Path, Path]] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"digest", "preexisting"}
            or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("digest", "")))
            or type(entry.get("preexisting")) is not bool
        ):
            raise InventoryError("capture media journal entry is malformed")
        digest = entry["digest"]
        source = checked_managed_path(workspace, Path(digest), "capture media workspace")
        destination = media_asset_path(paths["media_root"], digest)
        normalized.append((digest, entry["preexisting"], source, destination))
    if len({digest for digest, _, _, _ in normalized}) != len(normalized):
        raise InventoryError("capture media journal has duplicate digests")
    for digest, _, _, _ in normalized:
        cleanup_media_write_temps(paths["media_root"], digest)
    if committed:
        for digest, _, source, destination in normalized:
            if destination.is_symlink() or (
                destination.exists()
                and (not destination.is_file() or file_digest(destination) != digest)
            ):
                raise InventoryError("committed capture media is corrupt")
            if not destination.exists():
                if source.is_symlink() or not source.is_file() or file_digest(source) != digest:
                    raise InventoryError("committed capture media recovery source is unavailable")
                install_media(source, paths["media_root"], digest)
        outcome = "completed"
    else:
        for digest, preexisting, _, destination in normalized:
            if preexisting or not destination.exists():
                continue
            referenced = media_digest_is_referenced(paths["inventory_root"], digest)
            if referenced is None:
                raise InventoryError("cannot prove orphan capture media is safe to remove")
            if not referenced:
                destination.unlink()
                fsync_directory(destination.parent)
        outcome = "rolled_back"
    shutil.rmtree(workspace)
    journal_path.unlink()
    fsync_directory(paths["runtime"])
    return outcome


def recover_pending_transaction(paths: dict[str, Path]) -> str | None:
    """Recover the canonical generation, then its journaled external media."""
    store_result = _recover_store_transaction(paths)
    media_result = recover_pending_capture_media(paths)
    return store_result or media_result


def transaction(
    inventory_root: Path,
    runtime_dir: Path,
    operation: str,
    mutate: Callable[[Store], dict],
    *,
    continue_batch: bool = False,
    allow_legacy: bool = False,
    finalize_locked: Callable[[], None] | None = None,
) -> dict:
    paths = data_paths(inventory_root, runtime_dir)
    if degradation := degraded_reasons(paths):
        raise InventoryError(
            "inventory is quarantined by a degraded restore: " + "; ".join(degradation)
        )
    for required in ("store", "schema", "rebuild", "render", "verify"):
        if not paths[required].exists():
            raise InventoryError(f"missing inventory component: {paths[required]}")
    ensure_private_directory(
        checked_managed_path(paths["runtime"], Path("backups"), "runtime")
    )
    try:
        lock = inventory_lock(inventory_root)
        lock.acquire()
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error
    try:
        recover_pending_transaction(paths)
        git_store_is_clean(paths["data"], continue_batch=continue_batch)
        with tempfile.TemporaryDirectory(prefix="property-inventory-") as temp_name:
            temp = Path(temp_name)
            staged_store = temp / "store"
            store = Store(paths["store"], allow_legacy=allow_legacy)
            result = mutate(store)
            store.save(staged_store)
            staged_checks = verify_bundle(
                paths,
                staged_store,
                temp / "inventory.sqlite",
                temp / "Inventory.md",
            )
            changed = changed_store_files(staged_store, paths["store"])
            if not changed:
                return {
                    "status": "no_change",
                    "operation": operation,
                    "backup": None,
                    "result": result,
                    "checks": staged_checks,
                }
            backup = backup_store(paths, operation)
            journal = prepare_transaction(paths, staged_store, changed)
            try:
                replace_prepared_store(paths, journal)
                live_checks = verify_bundle(
                    paths,
                    paths["store"],
                    paths["database"],
                    paths["catalogue"],
                )
                if os.environ.get("PROPERTY_INVENTORY_FAIL_AFTER_VERIFY") == "1":
                    os._exit(98)
            except Exception as commit_error:
                restore_prepared_store(paths, journal)
                try:
                    verify_bundle(
                        paths,
                        paths["store"],
                        paths["database"],
                        paths["catalogue"],
                    )
                except Exception as rollback_error:
                    raise InventoryError(
                        f"transaction failed ({commit_error}); rollback verification "
                        f"also failed ({rollback_error})"
                    ) from commit_error
                remove_transaction_state(paths)
                raise
            remove_transaction_state(paths)
            if os.environ.get("PROPERTY_INVENTORY_RAISE_AFTER_COMMIT") == "1":
                raise InventoryError("injected failure after canonical commit")
        return {
            "status": "committed_to_store",
            "operation": operation,
            "backup": str(backup),
            "changed_store_files": changed,
            "result": result,
            "checks": live_checks,
            "next": "commit and push the verified private-data changes",
        }
    finally:
        try:
            if finalize_locked is not None:
                finalize_locked()
        finally:
            lock.release()


def add_evidence(
    store: Store,
    *,
    item_id: str,
    base: str,
    evidence_type: str,
    source_ref: str,
    captured_on: str,
    claim_strength: str,
    notes: str | None,
    minimum_sensitivity: str | None = None,
) -> str:
    evidence_id = store.allocate("evidence", f"ev-{base}")
    sensitivity = store.get("items", item_id)["sensitivity"]
    if minimum_sensitivity is not None:
        sensitivity = max(
            (sensitivity, minimum_sensitivity), key=SENSITIVITY_RANK.__getitem__
        )
    store.rows["evidence"].append(
        {
            "captured_on": captured_on,
            "claim_strength": claim_strength,
            "evidence_id": evidence_id,
            "evidence_type": evidence_type,
            "notes": notes,
            "sensitivity": sensitivity,
            "source_ref": source_ref,
        }
    )
    store.rows["item_evidence"].append(
        {"evidence_id": evidence_id, "item_id": item_id, "role": "supporting"}
    )
    return evidence_id


def add_event(
    store: Store,
    *,
    item_id: str,
    event_type: str,
    occurred_on: str | None,
    actor: str,
    evidence_id: str,
    notes: str | None,
    location_id: str | None = None,
    container_id: str | None = None,
    area_location_id: str | None = None,
    details: dict | None = None,
    occurred_on_precision: str = "exact",
    observed_on: str | None = None,
) -> str:
    if occurred_on_precision not in {"exact", "unknown"}:
        raise InventoryError("event date precision must be exact or unknown")
    if observed_on is None:
        observed_on = occurred_on
    if observed_on is None:
        raise InventoryError("event observation date is required")
    if (occurred_on_precision == "exact") != (occurred_on is not None):
        raise InventoryError("exact events need an occurrence date; unknown events must omit it")
    if (
        occurred_on_precision == "exact"
        and occurred_on is not None
        and observed_on < occurred_on
    ):
        raise InventoryError(
            f"{event_type} cannot be observed before it occurred "
            f"({observed_on} before {occurred_on})"
        )
    if event_type in CHRONOLOGICAL_EVENT_TYPES:
        previous = [
            row
            for row in store.rows["inventory_events"]
            if row["item_id"] == item_id
            and row["event_type"] in CHRONOLOGICAL_EVENT_TYPES
        ]
        previous_exact = [
            row
            for row in previous
            if row.get("occurred_on_precision", "exact") == "exact"
            and row.get("occurred_on") is not None
        ]
        if occurred_on_precision == "exact" and previous_exact:
            latest_exact = max(previous_exact, key=lambda row: row["sequence"])
            if occurred_on is not None and occurred_on < latest_exact["occurred_on"]:
                raise InventoryError(
                    f"{event_type} on {occurred_on} predates the latest lifecycle "
                    "event with an exact date "
                    f"({latest_exact['event_type']} on {latest_exact['occurred_on']})"
                )
        elif occurred_on_precision == "unknown" and previous_exact:
            latest_exact = max(previous_exact, key=lambda row: row["sequence"])
            if observed_on < latest_exact["occurred_on"]:
                raise InventoryError(
                    f"unknown-date {event_type} observed on {observed_on} predates "
                    "the latest lifecycle event with an exact date "
                    f"({latest_exact['event_type']} on {latest_exact['occurred_on']})"
                )
    event_id = store.allocate(
        "inventory_events",
        f"evt-{event_type}-{slug(item_id.removeprefix('itm-'), 34)}-"
        f"{occurred_on_precision}-{occurred_on or observed_on}",
    )
    store.rows["inventory_events"].append(
        {
            "actor": actor,
            "event_id": event_id,
            "event_type": event_type,
            "evidence_id": evidence_id,
            "location_id": location_id,
            "container_id": container_id,
            "area_location_id": area_location_id,
            "context_quality": "bound",
            "details_json": (
                strict_json_dumps(details, sort_keys=True)
                if details is not None
                else None
            ),
            "item_id": item_id,
            "notes": notes,
            "occurred_on": occurred_on,
            "occurred_on_precision": occurred_on_precision,
            "observed_on": observed_on,
            "sequence": max(
                (row["sequence"] for row in store.rows["inventory_events"]),
                default=0,
            )
            + 1,
        }
    )
    return event_id


def resolved_event_date(
    *, exact_date: str | None, date_unknown: bool, observed_on: str | None, label: str
) -> tuple[str | None, str, str]:
    """Represent an unknown historical date without inventing one.

    Unknown occurrence and known observation are separate fields, so recording a
    later observation never invents a historical lifecycle date.
    """
    if date_unknown:
        if observed_on is None:
            raise InventoryError(f"unknown {label} date requires --observed-on")
        return None, observed_on, "unknown"
    if exact_date is None:
        raise InventoryError(f"exact {label} date is required")
    effective_observation = observed_on or exact_date
    if effective_observation < exact_date:
        raise InventoryError(
            f"{label} cannot be observed before it occurred "
            f"({effective_observation} before {exact_date})"
        )
    return exact_date, effective_observation, "exact"


def append_item_detail_amendment(
    store: Store,
    *,
    item: dict,
    changes: dict[str, object],
    amended_on: str,
    actor: str,
    evidence_id: str,
    notes: str | None,
    recorded_at: str | None = None,
    record_all_fields: bool = False,
) -> str | None:
    """Apply changed item details and preserve the complete predecessor state."""
    actual_changes = {
        field: value
        for field, value in changes.items()
        if field in ITEM_DETAIL_FIELDS
        and (record_all_fields or item.get(field) != value)
    }
    if not actual_changes:
        return None
    previous = {field: item.get(field) for field in ITEM_DETAIL_FIELDS}
    resulting = {**previous, **actual_changes}
    if (resulting["purchase_price"] is None) != (
        resulting["purchase_currency"] is None
    ):
        raise InventoryError("purchase price and currency must remain paired")
    recorded = recorded_timestamp(recorded_at)
    previous_json = strict_json_dumps(previous, sort_keys=True)
    changes_json = strict_json_dumps(actual_changes, sort_keys=True)
    identity = strict_json_dumps(
        {
            "amended_on": amended_on,
            "changes": actual_changes,
            "evidence_id": evidence_id,
            "item_id": item["item_id"],
            "previous": previous,
            "recorded_at": recorded,
        },
        sort_keys=True,
    )
    amendment_id = "detail-amend-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    item.update(actual_changes)
    evidence = store.get("evidence", evidence_id)
    store.rows["item_detail_amendments"].append(
        {
            "actor": actor,
            "amended_on": amended_on,
            "changes_json": changes_json,
            "detail_amendment_id": amendment_id,
            "evidence_id": evidence_id,
            "item_id": item["item_id"],
            "notes": notes,
            "previous_json": previous_json,
            "recorded_at": recorded,
            "sensitivity": max(
                (item["sensitivity"], evidence["sensitivity"]),
                key=SENSITIVITY_RANK.__getitem__,
            ),
        }
    )
    return amendment_id


def apply_quantity_change(
    store: Store,
    *,
    item: dict,
    quantity: float,
    unit: str,
    occurred_on: str | None,
    actor: str,
    evidence_id: str,
    notes: str | None,
    occurred_on_precision: str = "exact",
    observed_on: str | None = None,
    record_unchanged: bool = False,
) -> str | None:
    """Apply a quantity change with a structured, replayable lifecycle payload."""
    previous_quantity = item.get("quantity")
    previous_unit = item.get("unit")
    if (
        previous_quantity == quantity
        and previous_unit == unit
        and not record_unchanged
    ):
        return None
    item["quantity"] = quantity
    item["unit"] = unit
    return add_event(
        store,
        item_id=item["item_id"],
        event_type="quantity_changed",
        occurred_on=occurred_on,
        occurred_on_precision=occurred_on_precision,
        observed_on=observed_on,
        actor=actor,
        evidence_id=evidence_id,
        notes=notes,
        location_id=item.get("location_id"),
        container_id=item.get("container_id"),
        details={
            "previous_quantity": previous_quantity,
            "previous_unit": previous_unit,
            "quantity": quantity,
            "unit": unit,
        },
    )


def tree_contains_manifest(root: Path, expected: dict[str, str]) -> bool:
    if root.is_symlink() or not root.is_dir():
        return False
    for name, digest in expected.items():
        path = root / Path(name)
        if path.is_symlink() or not path.is_file() or file_digest(path) != digest:
            return False
    data = root / "Data"
    if data.exists():
        for path in data.rglob("*"):
            if path.is_file() and path.relative_to(root).as_posix() not in expected:
                return False
            if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                return False
    return True


def recover_pending_init(args: argparse.Namespace) -> dict | None:
    journal_path = args.runtime_dir / INIT_JOURNAL
    if not path_entry_exists(journal_path):
        return None
    if journal_path.is_symlink() or not journal_path.is_file():
        raise InventoryError("initialization journal must be a regular file")
    try:
        journal = json.loads(journal_path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InventoryError("initialization journal is malformed") from error
    expected = {
        "format": 1,
        "inventory_root": str(args.inventory_root),
        "runtime": str(args.runtime_dir),
        "media_root": str(args.media_root) if args.media_root is not None else None,
        "catalogue": str(args.catalogue_output),
    }
    if (
        not isinstance(journal, dict)
        or any(journal.get(key) != value for key, value in expected.items())
        or journal.get("phase") not in {"prepared", "installed"}
        or not valid_installation_id(journal.get("installation_id"))
        or not isinstance(journal.get("inventory_id"), str)
        or not isinstance(journal.get("workspace"), str)
        or not valid_tree_manifest(journal.get("inventory_tree"))
        or not valid_tree_manifest(journal.get("workspace_tree"))
        or not isinstance(journal.get("catalogue_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", journal["catalogue_sha256"])
    ):
        raise InventoryError("initialization journal is malformed or belongs elsewhere")
    workspace = Path(journal["workspace"])
    staged_root = workspace / args.inventory_root.name
    if (
        workspace.parent != args.inventory_root.parent
        or not workspace.name.startswith(f".{args.inventory_root.name}-init-")
        or workspace.is_symlink()
    ):
        raise InventoryError("initialization journal workspace is unsafe")
    claim_runtime_owner(
        args.inventory_root,
        args.runtime_dir,
        args.media_root,
        args.catalogue_output,
        journal["installation_id"],
        journal["inventory_id"],
    )
    if path_entry_exists(args.inventory_root):
        if not tree_contains_manifest(args.inventory_root, journal["inventory_tree"]):
            raise InventoryError(
                "published initialization root changed in a managed path; bytes were preserved"
            )
    else:
        if not tree_matches(staged_root, journal["inventory_tree"]):
            raise InventoryError(
                "initialization staging changed before publication; bytes were preserved"
            )
        os.replace(staged_root, args.inventory_root)
        fsync_directory(args.inventory_root.parent)
    journal["phase"] = "installed"
    write_json(journal_path, journal)
    live_paths = data_paths(args.inventory_root, args.runtime_dir)
    checks = verify_bundle(
        live_paths,
        live_paths["store"],
        live_paths["database"],
        live_paths["catalogue"],
    )
    if file_digest(args.catalogue_output) != journal["catalogue_sha256"]:
        raise InventoryError("recovered initialization catalogue disagrees with staging")
    remove_partial_owned_tree(
        workspace,
        journal["workspace_tree"],
        "initialization workspace",
    )
    journal_path.unlink()
    fsync_directory(args.runtime_dir)
    return {
        "status": "recovered_initialized",
        "inventory_root": str(args.inventory_root),
        "runtime_dir": str(args.runtime_dir),
        "checks": checks,
    }


def command_init(args: argparse.Namespace) -> dict:
    inventory_root = args.inventory_root
    try:
        lock = inventory_lock(inventory_root)
        lock.acquire()
    except Timeout as error:
        raise InventoryError("another inventory writer holds the initialization lock") from error
    try:
        recover_pending_adoption_rollback(args)
        recovered_init = recover_pending_init(args)
        if recovered_init is not None:
            return recovered_init
        owner = read_runtime_owner(args.runtime_dir)
        if inventory_root.exists():
            inventory_id = inventory_id_if_available(inventory_root)
            if inventory_id is None:
                raise InventoryError(f"refusing to initialize an existing path: {inventory_root}")
            bindingless_adoption = not path_entry_exists(inventory_root / RUNTIME_BINDING)
            runtime_existed = args.runtime_dir.exists()
            prepare_explicit_legacy_adoption(args)
            rollback_state = (
                bindingless_adoption_rollback_state(
                    args,
                    inventory_id=inventory_id,
                    runtime_existed=runtime_existed,
                )
                if bindingless_adoption
                else None
            )
            try:
                installation_id, is_legacy = establish_existing_instance_ownership(args)
                if rollback_state is not None:
                    record_bindingless_adoption_owner(args, rollback_state)
                live_paths = data_paths(inventory_root, args.runtime_dir)
                live_checks = verify_bundle(
                    live_paths,
                    live_paths["store"],
                    live_paths["database"],
                    live_paths["catalogue"],
                    on_projection=(
                        lambda label, path: (
                            record_bindingless_adoption_projection(rollback_state, label, path)
                        if rollback_state is not None
                        else None
                        )
                    ),
                )
                if is_legacy:
                    finish_legacy_instance_adoption(args, installation_id)
            except BaseException:
                if bindingless_adoption:
                    rollback_bindingless_legacy_adoption(args, rollback_state)
                raise
            return {
                "status": "recovered_initialized",
                "inventory_root": str(inventory_root),
                "runtime_dir": str(args.runtime_dir),
                "checks": live_checks,
            }
        if owner is not None:
            expected_root = str(inventory_root.resolve())
            entries = [entry for entry in args.runtime_dir.iterdir() if entry.name != RUNTIME_OWNER]
            if owner.get("inventory_root") == expected_root and not entries:
                (args.runtime_dir / RUNTIME_OWNER).unlink()
                fsync_directory(args.runtime_dir)
                owner = None
            else:
                raise InventoryError(
                    f"runtime is already owned by another or incomplete instance: {args.runtime_dir}"
                )
        if runtime_has_unowned_entries(args.runtime_dir):
            raise InventoryError("initialization requires an empty runtime directory")
        if path_entry_exists(args.catalogue_output):
            raise InventoryError(
                "initialization requires a new catalogue output path; existing bytes were preserved"
            )
        catalogue_parent = args.catalogue_output.parent
        if path_entry_exists(catalogue_parent) and not catalogue_parent.is_dir():
            raise InventoryError(f"catalogue parent is not a directory: {catalogue_parent}")
        inventory_root.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{inventory_root.name}-init-", dir=inventory_root.parent
        ) as temp_name:
            staged_root = Path(temp_name) / inventory_root.name
            staged_store = staged_root / "Data" / "store"
            staged_store.mkdir(parents=True)
            inventory_id = f"inv-{uuid.uuid4()}"
            installation_id = str(uuid.uuid4())
            for table in TABLES:
                rows = []
                if table == "metadata":
                    rows = [
                        {
                            "inventory_id": inventory_id,
                            "schema_version": SCHEMA_VERSION,
                        }
                    ]
                elif table == "locations":
                    rows = [
                        {
                            "kind": "unknown",
                            "location_id": "loc-unknown",
                            "name": "Location unknown",
                            "notes": "Use only until a stable physical location is checked.",
                            "parent_location_id": None,
                            "sensitivity": "personal",
                        }
                    ]
                write_jsonl(staged_store / f"{table}.jsonl", rows)
            write_json(
                staged_root / RUNTIME_BINDING,
                runtime_binding_payload(args.runtime_dir, installation_id),
            )
            write_inventory_gitignore(staged_root)
            harden_private_tree(staged_root)
            staged_paths = data_paths(staged_root, Path(temp_name) / "runtime")
            staged_paths["catalogue"] = Path(temp_name) / "staged-Inventory.md"
            checks = verify_bundle(
                staged_paths,
                staged_store,
                staged_paths["database"],
                staged_paths["catalogue"],
            )
            expected_catalogue_hash = file_digest(staged_paths["catalogue"])
            fsync_tree(staged_root)
            ensure_private_directory(args.runtime_dir)
            init_journal = {
                "format": 1,
                "phase": "prepared",
                "installation_id": installation_id,
                "inventory_id": inventory_id,
                "inventory_root": str(inventory_root),
                "runtime": str(args.runtime_dir),
                "media_root": (str(args.media_root) if args.media_root is not None else None),
                "catalogue": str(args.catalogue_output),
                "workspace": str(Path(temp_name)),
                "inventory_tree": restore_tree_manifest(staged_root),
                "workspace_tree": restore_tree_manifest(Path(temp_name)),
                "catalogue_sha256": expected_catalogue_hash,
            }
            init_journal_path = args.runtime_dir / INIT_JOURNAL
            write_json(init_journal_path, init_journal)
            claimed = claim_runtime_owner(
                inventory_root,
                args.runtime_dir,
                args.media_root,
                args.catalogue_output,
                installation_id,
                inventory_id,
            )
            if os.environ.get("PROPERTY_INVENTORY_FAIL_INIT_AFTER_OWNER") == "1":
                os._exit(82)
            installed = False
            try:
                os.replace(staged_root, inventory_root)
                fsync_directory(inventory_root.parent)
                installed = True
                if os.environ.get("PROPERTY_INVENTORY_FAIL_INIT_AFTER_INSTALL") == "1":
                    os._exit(83)
                init_journal["phase"] = "installed"
                write_json(init_journal_path, init_journal)
                live_paths = data_paths(inventory_root, args.runtime_dir)
                ensure_private_directory(
                    checked_managed_path(
                        live_paths["runtime"], Path("backups"), "runtime"
                    )
                )
                live_checks = verify_bundle(
                    live_paths,
                    live_paths["store"],
                    live_paths["database"],
                    live_paths["catalogue"],
                )
            except BaseException:
                if installed:
                    # The canonical root is now public.  Preserve it and recover
                    # forward on a later explicit init; never delete bytes that may
                    # have appeared after publication.
                    fsync_tree(inventory_root)
                    fsync_directory(inventory_root.parent)
                elif claimed:
                    if (
                        path_entry_exists(args.catalogue_output)
                        and args.catalogue_output.is_file()
                        and not args.catalogue_output.is_symlink()
                        and file_digest(args.catalogue_output) == expected_catalogue_hash
                    ):
                        args.catalogue_output.unlink()
                        fsync_directory(args.catalogue_output.parent)
                    database = args.runtime_dir / "inventory.sqlite"
                    if database.is_file() and not database.is_symlink():
                        database.unlink()
                    backups = args.runtime_dir / "backups"
                    if backups.is_dir() and not backups.is_symlink() and not any(backups.iterdir()):
                        backups.rmdir()
                    (args.runtime_dir / RUNTIME_OWNER).unlink(missing_ok=True)
                    init_journal_path.unlink(missing_ok=True)
                    if args.runtime_dir.is_dir() and not any(args.runtime_dir.iterdir()):
                        args.runtime_dir.rmdir()
                    else:
                        fsync_directory(args.runtime_dir)
                raise
            remove_partial_owned_tree(
                Path(temp_name),
                init_journal["workspace_tree"],
                "initialization workspace",
            )
            init_journal_path.unlink()
            fsync_directory(args.runtime_dir)
        return {
            "status": "initialized",
            "inventory_root": str(inventory_root),
            "runtime_dir": str(args.runtime_dir),
            "staged_checks": checks,
            "checks": live_checks,
        }
    finally:
        lock.release()


def command_migrate(args: argparse.Namespace) -> dict:
    paths = data_paths(args.inventory_root, args.runtime_dir)

    def finalize_owner_identity() -> None:
        inventory_id = inventory_id_if_available(args.inventory_root)
        if inventory_id is None:
            return
        binding = read_runtime_binding_record(args.inventory_root)
        installation_id = (
            binding["installation_id"]
            if binding["format"] == 2
            else legacy_installation_id(args.inventory_root)
        )
        claim_runtime_owner(
            args.inventory_root,
            args.runtime_dir,
            args.media_root,
            args.catalogue_output,
            installation_id,
            inventory_id,
        )

    try:
        with inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error
    metadata_path = paths["store"] / "metadata.jsonl"
    if metadata_path.exists():
        metadata = read_jsonl(metadata_path)
        if len(metadata) != 1 or type(metadata[0].get("schema_version")) is not int:
            raise InventoryError("metadata.jsonl has no valid schema version")
        source_version = metadata[0]["schema_version"]
        if source_version == SCHEMA_VERSION:
            try:
                lock = inventory_lock(args.inventory_root)
                lock.acquire()
            except Timeout as error:
                raise InventoryError(
                    "another inventory writer holds the transaction lock"
                ) from error
            try:
                checks = verify_bundle(
                    paths,
                    paths["store"],
                    paths["database"],
                    paths["catalogue"],
                )
                current = load_verified_store(paths)
                finalize_owner_identity()
            finally:
                lock.release()
            return {
                "status": "already_current",
                "schema_version": current.schema_version,
                "inventory_id": current.rows["metadata"][0]["inventory_id"],
                "checks": checks,
            }
        if source_version > SCHEMA_VERSION:
            raise InventoryError(
                f"inventory schema {source_version} is newer than supported schema {SCHEMA_VERSION}"
            )
        if source_version not in {1, 2, 3, 4, 5, 6}:
            raise InventoryError(f"unsupported migration source schema: {source_version}")
    else:
        source_version = 1
    if not metadata_path.exists():
        partial = [
            table
        for table in TABLES
            if table not in V1_TABLES
            and table != "metadata"
            and (paths["store"] / f"{table}.jsonl").exists()
        ]
        if partial:
            raise InventoryError(
                "schema metadata is missing but versioned tables exist: " + ", ".join(partial)
            )

    def mutate(store: Store) -> dict:
        if store.schema_version not in {1, 2, 3, 4, 5, 6}:
            raise InventoryError(f"unsupported migration source schema: {store.schema_version}")
        from_schema = store.schema_version
        if from_schema == 1:
            owner = read_runtime_owner(args.runtime_dir)
            inventory_id = (
                owner["inventory_id"]
                if owner is not None and owner.get("inventory_id") is not None
                else f"inv-{uuid.uuid4()}"
            )
            store.rows["metadata"] = [
                {"inventory_id": inventory_id, "schema_version": SCHEMA_VERSION}
            ]
            for sequence, event in enumerate(store.rows["inventory_events"], start=1):
                event["sequence"] = sequence
        else:
            inventory_id = store.rows["metadata"][0]["inventory_id"]
            store.rows["metadata"][0]["schema_version"] = SCHEMA_VERSION
        if from_schema == 4:
            observation_indexes: dict[str, int] = {}
            for session in store.rows["capture_sessions"]:
                session.update(
                    {
                        "artifact_json": None,
                        "artifact_sha256": None,
                        "provenance_state": "legacy_unbound",
                        "review_json": None,
                        "review_sha256": None,
                    }
                )
            for observation in store.rows["capture_observations"]:
                session_id = observation["capture_session_id"]
                observation_indexes[session_id] = observation_indexes.get(session_id, 0) + 1
                observation["observation_index"] = observation_indexes[session_id]
                observation["validation_state"] = "legacy_unknown"
        item_sensitivity = {row["item_id"]: row["sensitivity"] for row in store.rows["items"]}
        supported_items: dict[str, set[str]] = {}

        def support(evidence_id: str | None, *item_ids: str | None) -> None:
            if evidence_id is not None:
                supported_items.setdefault(evidence_id, set()).update(
                    item_id for item_id in item_ids if item_id is not None
                )

        for link in store.rows["item_evidence"]:
            support(link["evidence_id"], link["item_id"])
        for event in store.rows["inventory_events"]:
            support(event.get("evidence_id"), event["item_id"])
        for relationship in store.rows["relationships"]:
            support(
                relationship["evidence_id"],
                relationship["subject_item_id"],
                relationship["object_item_id"],
            )
        for kit in store.rows["kits"]:
            support(kit["evidence_id"], kit["serves_item_id"])
        for path in store.rows["torque_paths"]:
            support(path["evidence_id"], path["tool_item_id"])
        for requirement in store.rows["kit_requirements"]:
            support(requirement["evidence_id"], requirement.get("item_id"))
        items_by_model: dict[str, set[str]] = {}
        for item in store.rows["items"]:
            items_by_model.setdefault(item["model_id"], set()).add(item["item_id"])
        for link in store.rows["model_interfaces"]:
            support(link["evidence_id"], *items_by_model.get(link["model_id"], set()))
        for evidence in store.rows["evidence"]:
            if "sensitivity" not in evidence:
                sensitivities = [
                    item_sensitivity[item_id]
                    for item_id in supported_items.get(evidence["evidence_id"], set())
                    if item_id in item_sensitivity
                ]
                evidence["sensitivity"] = max(
                    sensitivities, key=SENSITIVITY_RANK.__getitem__, default="high"
                )
        evidence_by_id = {
            row["evidence_id"]: row for row in store.rows["evidence"]
        }
        if from_schema < 6:
            for item in store.rows["items"]:
                item["identity_sensitivity"] = item["sensitivity"]
            for event in store.rows["inventory_events"]:
                event.setdefault("location_id", None)
                event.setdefault("container_id", None)
                event.setdefault("area_location_id", None)
                event.setdefault("details_json", None)
                event["observed_on"] = event["occurred_on"]
                event["occurred_on_precision"] = "exact"
                event["context_quality"] = "legacy_unknown"
            for tag in store.rows["item_tags"]:
                item = store.get("items", tag["item_id"])
                evidence_id = item["primary_evidence_id"]
                evidence = evidence_by_id[evidence_id]
                tag.update(
                    {
                        "evidence_id": evidence_id,
                        "sensitivity": max(
                            (item["sensitivity"], evidence["sensitivity"]),
                            key=SENSITIVITY_RANK.__getitem__,
                        ),
                        "notes": "Migrated legacy classification; it makes no ownership claim.",
                    }
                )

            linked = {
                (row["item_id"], row["evidence_id"])
                for row in store.rows["item_evidence"]
            }

            def ensure_support(evidence_id: str, *item_ids: str | None) -> None:
                for item_id in item_ids:
                    if item_id is None or (item_id, evidence_id) in linked:
                        continue
                    store.rows["item_evidence"].append(
                        {
                            "evidence_id": evidence_id,
                            "item_id": item_id,
                            "role": "supporting",
                        }
                    )
                    linked.add((item_id, evidence_id))

            for relationship in store.rows["relationships"]:
                ensure_support(
                    relationship["evidence_id"],
                    relationship["subject_item_id"],
                    relationship["object_item_id"],
                )
            for kit in store.rows["kits"]:
                ensure_support(kit["evidence_id"], kit["serves_item_id"])
            kits_by_id = {row["kit_id"]: row for row in store.rows["kits"]}
            for requirement in store.rows["kit_requirements"]:
                ensure_support(
                    requirement["evidence_id"],
                    kits_by_id[requirement["kit_id"]]["serves_item_id"],
                    requirement.get("item_id"),
                )
                evidence = evidence_by_id[requirement["evidence_id"]]
                item = (
                    store.get("items", requirement["item_id"])
                    if requirement.get("item_id") is not None
                    else None
                )
                if requirement["status"] == "source_present" and (
                    item is None
                    or item["ownership_state"] != "confirmed"
                    or evidence["evidence_type"] != "physical_check"
                    or evidence["claim_strength"] != "explicit_current"
                ):
                    requirement["status"] = "needs_verification"
                if requirement["status"] == "exists_unassigned" and (
                    item is None
                    or item["ownership_state"] != "confirmed"
                    or evidence["claim_strength"] != "explicit_current"
                ):
                    requirement["status"] = "needs_verification"
                matching_events = [
                    event
                    for event in store.rows["inventory_events"]
                    if event["item_id"] == requirement.get("item_id")
                    and event.get("evidence_id") == requirement["evidence_id"]
                    and (
                        requirement["status"] != "source_present"
                        or event["event_type"] == "physically_verified"
                    )
                ]
                if (
                    requirement["status"]
                    in {"source_present", "exists_unassigned"}
                    and not matching_events
                ):
                    requirement["status"] = "needs_verification"
                requirement["verified_event_sequence"] = (
                    max(event["sequence"] for event in matching_events)
                    if requirement["status"]
                    in {"source_present", "exists_unassigned"}
                    else None
                )
                requirement["recorded_at"] = (
                    f"{evidence['captured_on']}T00:00:00+00:00"
                )
            for path in store.rows["torque_paths"]:
                ensure_support(path["evidence_id"], path["tool_item_id"])
        corrected_loans = 0
        if from_schema < 7:
            item_evidence_links = {
                (row["item_id"], row["evidence_id"]) for row in store.rows["item_evidence"]
            }
            loan_evidence: dict[str, str] = {}
            for event in sorted(
                store.rows["inventory_events"], key=lambda row: row.get("sequence", 0)
            ):
                if event["event_type"] == "lent" and event.get("evidence_id") is not None:
                    loan_evidence[event["item_id"]] = event["evidence_id"]
            for item in store.rows["items"]:
                # A home is a separate, checked fact. Migration cannot know it,
                # and the current placement is not evidence for it.
                item["home_location_id"] = None
                item["home_container_id"] = None
                if item["ownership_state"] != "lent":
                    continue
                # v6 spent ownership to express a loan. v7 keeps the item owned
                # and records custody as elsewhere, with nobody invented.
                item_id = item["item_id"]
                evidence_id = loan_evidence.get(item_id, item["primary_evidence_id"])
                if (item_id, evidence_id) not in item_evidence_links:
                    evidence_id = item["primary_evidence_id"]
                item["ownership_state"] = "confirmed"
                store.rows["item_party_relations"].append(
                    {
                        "custody_kind": "loan",
                        "due_on": None,
                        "ended_on": None,
                        "ended_evidence_id": None,
                        "evidence_id": evidence_id,
                        "item_id": item_id,
                        "notes": (
                            "Migrated legacy loan; ownership was never transferred and "
                            "the custodian was never recorded."
                        ),
                        "party_id": None,
                        "relation_id": store.allocate(
                            "item_party_relations", f"rel-custody-{item_id}"
                        ),
                        "role": "custodian",
                        "quantity": None,
                        "sensitivity": max(
                            (
                                item["sensitivity"],
                                evidence_by_id[evidence_id]["sensitivity"],
                            ),
                            key=SENSITIVITY_RANK.__getitem__,
                        ),
                        "started_on": None,
                        "status": "active",
                        "unit": None,
                    }
                )
                corrected_loans += 1
            for amendment in store.rows["item_detail_amendments"]:
                previous = strict_json_loads(amendment["previous_json"], label="legacy item detail amendment")
                if not isinstance(previous, dict):
                    raise InventoryError("legacy item detail amendment is malformed")
                previous.setdefault("home_location_id", None)
                previous.setdefault("home_container_id", None)
                amendment["previous_json"] = strict_json_dumps(previous, sort_keys=True)
        for relation in store.rows["item_party_relations"]:
            # v7 beta rows did not record episode detail. Preserve unknown rather
            # than fabricating a loan, party, date, amount, or unit.
            relation.setdefault("custody_kind", "unknown" if relation.get("role") == "custodian" else None)
            relation.setdefault("due_on", None)
            relation.setdefault("ended_evidence_id", None)
            relation.setdefault("quantity", None)
            relation.setdefault("unit", None)
        store.schema_version = SCHEMA_VERSION
        return {
            "from_schema": from_schema,
            "to_schema": SCHEMA_VERSION,
            "inventory_id": inventory_id,
            "corrected_loan_custody_rows": corrected_loans,
            "preserved_v1_rows": {table: len(store.rows[table]) for table in V1_TABLES},
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"migrate-v{source_version}-to-v{SCHEMA_VERSION}",
        mutate,
        continue_batch=args.continue_batch,
        allow_legacy=True,
        finalize_locked=finalize_owner_identity,
    )


def media_asset_path(media_root: Path, digest: str) -> Path:
    return checked_managed_path(
        media_root,
        Path("sha256") / digest[:2] / digest,
        "media",
    )


def materialize_verified_existing_media(
    source_root: Path, destination_root: Path, asset: dict
) -> Path:
    """Expose immutable existing media cheaply, with a cross-device copy fallback."""
    digest = asset.get("sha256")
    size = asset.get("byte_size")
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        raise InventoryError("canonical media row is malformed")
    source = media_asset_path(source_root, digest)
    if (
        source.is_symlink()
        or not source.is_file()
        or source.stat().st_size != size
        or file_digest(source) != digest
    ):
        raise InventoryError(
            "existing canonical media is missing or corrupt before proposal application"
        )
    destination = media_asset_path(destination_root, digest)
    ensure_private_directory(destination.parent)
    try:
        os.link(source, destination, follow_symlinks=False)
        fsync_directory(destination.parent)
    except OSError as error:
        if error.errno not in {
            errno.EXDEV,
            errno.EPERM,
            errno.EACCES,
            getattr(errno, "ENOTSUP", errno.EINVAL),
        }:
            raise InventoryError("cannot materialize existing proposal media") from error
        durable_copy(source, destination)
        fsync_directory(destination.parent)
    if (
        destination.is_symlink()
        or not destination.is_file()
        or destination.stat().st_size != size
        or file_digest(destination) != digest
    ):
        raise InventoryError("proposal media materialization failed verification")
    return destination


def cleanup_media_write_temps(media_root: Path, digest: str) -> None:
    """Remove only digest-owned abandoned atomic-write files under the media lock."""
    destination = media_asset_path(media_root, digest)
    parent = destination.parent
    if not parent.exists():
        return
    if parent.is_symlink() or not parent.is_dir():
        raise InventoryError("content-addressed media directory is unsafe")
    pattern = re.compile(
        rf"\.{re.escape(digest)}\.write-(?:[0-9]+|[0-9a-f-]{{36}})"
    )
    removed = False
    for candidate in parent.iterdir():
        if pattern.fullmatch(candidate.name) is None:
            continue
        if candidate.is_symlink() or not candidate.is_file():
            raise InventoryError("abandoned media write entry is unsafe")
        candidate.unlink()
        removed = True
    if removed:
        fsync_directory(parent)


def install_media(source: Path, media_root: Path, digest: str) -> tuple[Path, bool]:
    destination = media_asset_path(media_root, digest)
    cleanup_media_write_temps(media_root, digest)
    if destination.exists():
        if (
            destination.is_symlink()
            or not destination.is_file()
            or file_digest(destination) != digest
        ):
            raise InventoryError(f"content-addressed media collision or corruption: {destination}")
        return destination, False
    ensure_private_directory(destination.parent)
    temporary = destination.with_name(f".{destination.name}.write-{uuid.uuid4()}")
    temporary_created = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        temporary_created = True
        os.fchmod(descriptor, 0o600)
        with source.open("rb") as source_handle, os.fdopen(
            descriptor, "wb"
        ) as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle, 1024 * 1024)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        if file_digest(temporary) != digest:
            raise InventoryError(f"media changed while being copied: {source}")
        os.link(temporary, destination, follow_symlinks=False)
        fsync_directory(destination.parent)
        temporary.unlink()
        temporary_created = False
        fsync_directory(destination.parent)
        return destination, True
    finally:
        if temporary_created:
            temporary.unlink(missing_ok=True)


def remove_new_media(path: Path, media_root: Path) -> None:
    path.unlink(missing_ok=True)
    for directory in (path.parent, path.parent.parent):
        if directory != media_root:
            try:
                directory.rmdir()
            except OSError:
                pass


def media_digest_is_referenced(inventory_root: Path, digest: str) -> bool | None:
    assets_path = inventory_root / "Data" / "store" / "media_assets.jsonl"
    try:
        assets = read_jsonl(assets_path)
    except (InventoryError, OSError):
        return None
    return any(row.get("sha256") == digest for row in assets)


def cleanup_unreferenced_media(
    inventory_root: Path, media_root: Path, path: Path, digest: str
) -> None:
    try:
        with inventory_lock(inventory_root):
            if media_digest_is_referenced(inventory_root, digest) is False:
                remove_new_media(path, media_root)
    except (OSError, Timeout):
        # Uncertainty retains immutable content; deleting a referenced byte is worse.
        return


def command_attach_media(args: argparse.Namespace) -> dict:
    if args.media_root is None:
        raise InventoryError("attach-media requires --media-root or PROPERTY_INVENTORY_MEDIA_ROOT")
    try:
        with media_lock(args.media_root):
            return attach_media_under_lock(args)
    except Timeout as error:
        raise InventoryError("another writer holds the media-root lock") from error


def attach_media_under_lock(args: argparse.Namespace) -> dict:
    source = args.file.expanduser().resolve()
    if not source.is_file():
        raise InventoryError(f"media source is not a regular file: {source}")
    digest = file_digest(source)
    byte_size = source.stat().st_size
    installed: Path | None = None
    installed_new = False
    media_type = normalized_media_type(
        args.media_type or mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    )

    def mutate(store: Store) -> dict:
        nonlocal installed, installed_new
        evidence = store.get("evidence", args.evidence_id)
        if args.role == "receipt" and (
            evidence["evidence_type"] not in {"merchant_account", "user_source"}
            or evidence["claim_strength"] != "purchase_only"
        ):
            raise InventoryError(
                "receipt media requires merchant-account or user-source purchase evidence"
            )
        if args.role == "appraisal" and (
            evidence["evidence_type"] not in {"user_source", "vault_note"}
            or evidence["claim_strength"] != "research_only"
        ):
            raise InventoryError(
                "appraisal media requires reviewed user-source or vault-note evidence"
            )
        linked_item_ids = {
            row["item_id"]
            for row in store.rows["item_evidence"]
            if row["evidence_id"] == args.evidence_id
        }
        linked_item_ids.update(
            row["item_id"]
            for row in store.rows["inventory_events"]
            if row.get("evidence_id") == args.evidence_id
        )
        for relationship in store.rows["relationships"]:
            if relationship["evidence_id"] == args.evidence_id:
                linked_item_ids.update(
                    (relationship["subject_item_id"], relationship["object_item_id"])
                )
        linked_model_ids = {
            row["model_id"]
            for row in store.rows["model_interfaces"]
            if row["evidence_id"] == args.evidence_id
        }
        linked_item_ids.update(
            row["item_id"] for row in store.rows["items"] if row["model_id"] in linked_model_ids
        )
        for kit in store.rows["kits"]:
            if kit["evidence_id"] == args.evidence_id:
                linked_item_ids.add(kit["serves_item_id"])
        for torque_path in store.rows["torque_paths"]:
            if torque_path["evidence_id"] == args.evidence_id:
                linked_item_ids.add(torque_path["tool_item_id"])
        for requirement in store.rows["kit_requirements"]:
            if requirement["evidence_id"] == args.evidence_id and requirement.get("item_id"):
                linked_item_ids.add(requirement["item_id"])
        item_sensitivities = {row["item_id"]: row["sensitivity"] for row in store.rows["items"]}
        sensitivity = max(
            (args.sensitivity, *(item_sensitivities[item_id] for item_id in linked_item_ids)),
            key=SENSITIVITY_RANK.__getitem__,
        )
        installed, installed_new = install_media(source, args.media_root, digest)
        try:
            validate_declared_media(
                installed,
                media_type,
                document_only=args.role in {"receipt", "appraisal"},
            )
        except MediaValidationError as error:
            raise InventoryError(f"media bytes do not support their declared role: {error}") from error
        if os.environ.get("PROPERTY_INVENTORY_FAIL_AFTER_MEDIA_INSTALL") == "1":
            raise InventoryError("injected failure after media install")
        matches = [row for row in store.rows["media_assets"] if row["sha256"] == digest]
        if matches:
            asset = matches[0]
            if asset["byte_size"] != byte_size:
                raise InventoryError(f"media asset size disagrees with digest: {asset['asset_id']}")
            if normalized_media_type(asset["media_type"]) != media_type:
                raise InventoryError(
                    f"media asset type disagrees with existing digest: {asset['asset_id']}"
                )
            asset_id = asset["asset_id"]
            if SENSITIVITY_RANK[sensitivity] > SENSITIVITY_RANK[asset["sensitivity"]]:
                asset["sensitivity"] = sensitivity
            reused = True
        else:
            asset_id = store.allocate("media_assets", f"asset-{digest[:24]}")
            store.rows["media_assets"].append(
                {
                    "asset_id": asset_id,
                    "byte_size": byte_size,
                    "captured_on": args.captured_on,
                    "media_type": media_type,
                    "original_name": source.name,
                    "sensitivity": sensitivity,
                    "sha256": digest,
                    "uri": f"media://sha256/{digest}",
                }
            )
            reused = False
        link = {
            "asset_id": asset_id,
            "evidence_id": args.evidence_id,
            "region_json": (
                json.dumps(args.region, ensure_ascii=False, sort_keys=True)
                if args.region is not None
                else None
            ),
            "role": args.role,
        }
        existing_links = [
            row
            for row in store.rows["evidence_assets"]
            if row["asset_id"] == asset_id
            and row["evidence_id"] == args.evidence_id
            and row["role"] == args.role
        ]
        linked = bool(existing_links)
        if linked and existing_links[0].get("region_json") != link["region_json"]:
            raise InventoryError("evidence asset link already exists with a different image region")
        if not linked:
            store.rows["evidence_assets"].append(link)
        return {
            "asset_id": asset_id,
            "evidence_id": args.evidence_id,
            "sha256": digest,
            "byte_size": byte_size,
            "media_path": str(installed),
            "sensitivity": sensitivity,
            "asset_reused": reused,
            "link_reused": linked,
        }

    try:
        return transaction(
            args.inventory_root,
            args.runtime_dir,
            f"attach-media-{args.evidence_id}",
            mutate,
            continue_batch=args.continue_batch,
        )
    except BaseException:
        if installed_new and installed is not None:
            cleanup_unreferenced_media(args.inventory_root, args.media_root, installed, digest)
        raise


def add_tar_file(archive: tarfile.TarFile, source: Path, name: str) -> None:
    def normalized(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        info.mode = 0o644
        return info

    archive.add(source, arcname=name, recursive=False, filter=normalized)


def command_auxiliary_manifest(args: argparse.Namespace) -> dict:
    if args.scope != "private":
        raise InventoryError("auxiliary-manifest requires private scope")
    paths = data_paths(args.inventory_root, args.runtime_dir)
    manifest_path = checked_data_path(paths["data"], Path(AUXILIARY_MANIFEST))
    try:
        lock = inventory_lock(args.inventory_root)
        lock.acquire()
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error
    previous = manifest_path.read_bytes() if manifest_path.exists() else None
    try:
        if previous is not None and not args.replace:
            raise InventoryError(
                "auxiliary-data manifest already exists; pass --replace after reviewing changed inputs"
            )
        names = set(load_auxiliary_manifest(manifest_path)) if previous is not None else set()
        removals = {safe_auxiliary_name(name) for name in args.remove}
        additions = {safe_auxiliary_name(name) for name in args.include}
        overlap = removals & additions
        if overlap:
            raise InventoryError(
                "auxiliary files cannot be included and removed together: "
                + ", ".join(sorted(overlap))
            )
        names.update(additions)
        names.update(name for name in KNOWN_AUXILIARY_INPUTS if (paths["data"] / name).exists())
        names.difference_update(removals)
        files: dict[str, dict[str, str]] = {}
        for raw_name in sorted(names):
            name = safe_auxiliary_name(raw_name)
            path = auxiliary_data_path(paths["data"], name)
            if not path.is_file():
                raise InventoryError(f"auxiliary-data file is not a regular file: {name}")
            files[name] = {"sha256": file_digest(path)}
        if files:
            write_json(manifest_path, {"format": 1, "files": files})
        else:
            manifest_path.unlink(missing_ok=True)
            fsync_directory(manifest_path.parent)
        declared = validate_auxiliary_data(paths["data"])
        checks = verify_bundle(
            paths,
            paths["store"],
            paths["database"],
            paths["catalogue"],
        )
        return {
            "status": "written",
            "manifest": str(manifest_path),
            "declared_files": sorted(name for name in declared if name != AUXILIARY_MANIFEST),
            "checks": checks,
        }
    except BaseException:
        if previous is None:
            manifest_path.unlink(missing_ok=True)
        else:
            temporary = manifest_path.with_name(f".{manifest_path.name}.rollback-{os.getpid()}")
            temporary.write_bytes(previous)
            fsync_file(temporary)
            os.replace(temporary, manifest_path)
            fsync_directory(manifest_path.parent)
        raise
    finally:
        lock.release()


def command_export(args: argparse.Namespace) -> dict:
    if args.scope != "private":
        raise InventoryError("export requires private scope")
    if args.media_root is None:
        raise InventoryError("export requires --media-root or PROPERTY_INVENTORY_MEDIA_ROOT")
    lexical_output = Path(os.path.abspath(args.output.expanduser()))
    if lexical_output.is_symlink() or lexical_output.parent.is_symlink():
        raise InventoryError("export output must not traverse a managed symlink")
    try:
        output = lexical_output.resolve()
    except (OSError, RuntimeError) as error:
        raise InventoryError(f"cannot resolve export output: {error}") from error
    if output == args.catalogue_output:
        raise InventoryError("export output and catalogue output must be different files")
    for label, protected_root in (
        ("inventory", args.inventory_root),
        ("runtime", args.runtime_dir),
        ("media", args.media_root),
    ):
        if (
            output == protected_root
            or output in protected_root.parents
            or protected_root in output.parents
        ):
            raise InventoryError(
                f"export output must be outside the {label} namespace: {protected_root}"
            )
    for forbidden_root in args.forbidden_roots:
        if any(
            candidate == forbidden_root
            or candidate in forbidden_root.parents
            or forbidden_root in candidate.parents
            for candidate in (lexical_output, output)
        ):
            raise InventoryError(f"export output must be outside forbidden root: {forbidden_root}")
    if path_entry_exists(lexical_output) or path_entry_exists(output):
        raise InventoryError(f"refusing to overwrite export: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    paths = data_paths(args.inventory_root, args.runtime_dir)
    if degradation := degraded_reasons(paths):
        raise InventoryError(
            "refusing to export a degraded inventory as trusted: " + "; ".join(degradation)
        )
    try:
        lock = inventory_lock(args.inventory_root)
        lock.acquire()
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error
    temporary_output = output.with_name(f".{output.name}.write-{uuid.uuid4()}")
    temporary_created = False
    temporary_descriptor: int | None = None
    temporary_identity: tuple[int, int] | None = None
    publication_complete = False
    try:
        recovery = recover_pending_transaction(paths)
        store = Store(paths["store"])
        checks = verify_bundle(
            paths,
            paths["store"],
            paths["database"],
            paths["catalogue"],
        )
        auxiliary_hashes = validate_auxiliary_data(paths["data"])
        store_hashes = {
            f"{table}.jsonl": file_digest(paths["store"] / f"{table}.jsonl") for table in TABLES
        }
        media = {
            row["sha256"]: {
                "byte_size": row["byte_size"],
                "path": f"media/{row['sha256']}",
            }
            for row in store.rows["media_assets"]
        }
        manifest = {
            "format": 2,
            "inventory_id": store.rows["metadata"][0]["inventory_id"],
            "schema_version": SCHEMA_VERSION,
            "store": store_hashes,
            "media": media,
            "auxiliary": auxiliary_hashes,
        }
        with tempfile.TemporaryDirectory(prefix="property-inventory-export-") as temp_name:
            manifest_path = Path(temp_name) / "manifest.json"
            write_json(manifest_path, manifest)
            temporary_descriptor = os.open(
                temporary_output,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            temporary_created = True
            os.fchmod(temporary_descriptor, 0o600)
            opened_stat = os.fstat(temporary_descriptor)
            temporary_identity = (opened_stat.st_dev, opened_stat.st_ino)
            with os.fdopen(temporary_descriptor, "wb", closefd=False) as raw_export:
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=raw_export, mtime=0
                ) as compressed:
                    with tarfile.open(fileobj=compressed, mode="w") as archive:
                        add_tar_file(archive, manifest_path, "manifest.json")
                        for name in sorted(store_hashes):
                            add_tar_file(archive, paths["store"] / name, f"store/{name}")
                        for name in sorted(auxiliary_hashes):
                            add_tar_file(
                                archive,
                                paths["data"] / Path(name),
                                f"auxiliary/{name}",
                            )
                        for digest in sorted(media):
                            add_tar_file(
                                archive,
                                media_asset_path(paths["media_root"], digest),
                                f"media/{digest}",
                            )
        os.fsync(temporary_descriptor)
        descriptor_stat = os.fstat(temporary_descriptor)
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) != temporary_identity:
            raise InventoryError("export staging identity changed while writing")
        try:
            named_stat = os.stat(temporary_output, follow_symlinks=False)
        except OSError as error:
            raise InventoryError(
                "export staging changed before publication"
            ) from error
        if (
            not stat.S_ISREG(named_stat.st_mode)
            or (named_stat.st_dev, named_stat.st_ino) != temporary_identity
        ):
            raise InventoryError("export staging changed before publication")
        try:
            os.link(temporary_output, output, follow_symlinks=False)
        except FileExistsError as error:
            raise InventoryError(f"refusing to overwrite export: {output}") from error
        try:
            published_stat = os.stat(output, follow_symlinks=False)
        except OSError as error:
            raise InventoryError("export publication changed before verification") from error
        if (
            not stat.S_ISREG(published_stat.st_mode)
            or (published_stat.st_dev, published_stat.st_ino) != temporary_identity
            or stat.S_IMODE(published_stat.st_mode) != 0o600
        ):
            raise InventoryError("export publication changed before verification")
        fsync_directory(output.parent)
        temporary_output.unlink()
        temporary_created = False
        fsync_directory(output.parent)
        publication_complete = True
        return {
            "status": "exported",
            "archive": str(output),
            "inventory_id": manifest["inventory_id"],
            "store_files": len(store_hashes),
            "auxiliary_files": len(auxiliary_hashes),
            "media_assets": len(media),
            "recovery": recovery,
            "checks": checks,
        }
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_created:
            try:
                current = os.stat(temporary_output, follow_symlinks=False)
            except OSError:
                current = None
            if (
                current is not None
                and temporary_identity is not None
                and (current.st_dev, current.st_ino) == temporary_identity
            ):
                temporary_output.unlink(missing_ok=True)
        if not publication_complete and temporary_identity is not None:
            try:
                current = os.stat(output, follow_symlinks=False)
            except OSError:
                current = None
            if (
                current is not None
                and stat.S_ISREG(current.st_mode)
                and (current.st_dev, current.st_ino) == temporary_identity
            ):
                output.unlink(missing_ok=True)
        lock.release()


def safe_archive_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if (
            member.name in members
            or path.is_absolute()
            or ".." in path.parts
            or not member.isfile()
        ):
            raise InventoryError(f"unsafe or duplicate export member: {member.name}")
        members[member.name] = member
    return members


def archive_bytes(
    archive: tarfile.TarFile, members: dict[str, tarfile.TarInfo], name: str
) -> bytes:
    member = members.get(name)
    if member is None:
        raise InventoryError(f"export is missing member: {name}")
    if member.size > 10 * 1024 * 1024:
        raise InventoryError(f"export manifest is unreasonably large: {name}")
    handle = archive.extractfile(member)
    if handle is None:
        raise InventoryError(f"cannot read export member: {name}")
    return handle.read()


def restore_archive_file(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
    destination: Path,
) -> tuple[str, int]:
    member = members.get(name)
    if member is None:
        raise InventoryError(f"export is missing member: {name}")
    source = archive.extractfile(member)
    if source is None:
        raise InventoryError(f"cannot read export member: {name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or (path_entry_exists(destination) and not destination.is_file()):
        raise RestoreConflict(f"restore staging path is unsafe: {destination}")
    destination_exists = destination.exists()
    existing_size = destination.stat().st_size if destination_exists else 0
    if existing_size > member.size:
        raise RestoreConflict(f"restore staging file is larger than its source: {name}")
    digest = hashlib.sha256()
    byte_size = 0
    existing = destination.open("rb") if destination_exists else None
    mode = "ab" if destination_exists else "xb"
    try:
        with destination.open(mode) as target:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                if byte_size < existing_size:
                    expected_existing = block[: min(len(block), existing_size - byte_size)]
                    actual_existing = existing.read(len(expected_existing)) if existing else b""
                    if actual_existing != expected_existing:
                        raise RestoreConflict(
                            f"restore staging bytes are not a source prefix: {name}"
                        )
                    remainder = block[len(expected_existing) :]
                else:
                    remainder = block
                if remainder:
                    target.write(remainder)
                digest.update(block)
                byte_size += len(block)
            target.flush()
            os.fsync(target.fileno())
    finally:
        if existing is not None:
            existing.close()
    if existing_size and existing_size != min(existing_size, byte_size):
        raise RestoreConflict(f"restore staging prefix exceeds archive member: {name}")
    return digest.hexdigest(), byte_size


def archive_member_digest(archive: tarfile.TarFile, member: tarfile.TarInfo) -> tuple[str, int]:
    source = archive.extractfile(member)
    if source is None:
        raise InventoryError(f"cannot read export member: {member.name}")
    digest = hashlib.sha256()
    byte_size = 0
    for block in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(block)
        byte_size += len(block)
    return digest.hexdigest(), byte_size


def inspect_restore_archive(archive_path: Path, *, allow_unsafe_legacy: bool) -> dict:
    """Validate every archive byte before creating durable ownership or staging."""
    with tarfile.open(archive_path, "r:gz") as archive:
        members = safe_archive_members(archive)
        try:
            manifest = json.loads(archive_bytes(archive, members, "manifest.json"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise InventoryError("property inventory export manifest is invalid JSON") from error
        if not isinstance(manifest, dict):
            raise InventoryError("property inventory export manifest must be an object")
        export_format = manifest.get("format")
        if (
            export_format not in {1, 2}
            or manifest.get("schema_version") != SCHEMA_VERSION
            or not isinstance(manifest.get("inventory_id"), str)
            or not isinstance(manifest.get("store"), dict)
            or not isinstance(manifest.get("media"), dict)
            or (export_format == 2 and not isinstance(manifest.get("auxiliary"), dict))
        ):
            raise InventoryError("unsupported or malformed property inventory export")
        if export_format == 1 and not allow_unsafe_legacy:
            raise InventoryError(
                "legacy format-1 export has no auxiliary-data manifest; rerun with "
                "--allow-unsafe-legacy only for a deliberate degraded restore"
            )
        expected_store = {f"{table}.jsonl" for table in TABLES}
        if set(manifest["store"]) != expected_store:
            raise InventoryError("export canonical store file set is incomplete or unexpected")
        auxiliary = manifest.get("auxiliary", {})
        expected_members = {"manifest.json"}
        for name, expected_hash in manifest["store"].items():
            if not isinstance(expected_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_hash
            ):
                raise InventoryError(f"export has invalid store digest: {name}")
            member_name = f"store/{name}"
            member = members.get(member_name)
            if member is None:
                raise InventoryError(f"export is missing member: {member_name}")
            actual_hash, _ = archive_member_digest(archive, member)
            if actual_hash != expected_hash:
                raise InventoryError(f"export store hash mismatch: {name}")
            expected_members.add(member_name)
        media_asset_bytes = archive_bytes(archive, members, "store/media_assets.jsonl")
        try:
            media_rows = [
                json.loads(line)
                for line in media_asset_bytes.decode("utf-8").splitlines()
                if line.strip()
            ]
            expected_media = {
                row["sha256"]: {
                    "byte_size": row["byte_size"],
                    "path": f"media/{row['sha256']}",
                }
                for row in media_rows
            }
        except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
            raise InventoryError("canonical media assets are malformed") from error
        if len(expected_media) != len(media_rows) or manifest["media"] != expected_media:
            raise InventoryError("export media manifest disagrees with canonical media_assets")
        for name, expected_hash in auxiliary.items():
            if name != AUXILIARY_MANIFEST:
                safe_auxiliary_name(name)
            if not isinstance(expected_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_hash
            ):
                raise InventoryError(f"export has invalid auxiliary digest: {name}")
            member_name = f"auxiliary/{name}"
            member = members.get(member_name)
            if member is None:
                raise InventoryError(f"export is missing member: {member_name}")
            actual_hash, _ = archive_member_digest(archive, member)
            if actual_hash != expected_hash:
                raise InventoryError(f"export auxiliary hash mismatch: {name}")
            expected_members.add(member_name)
        for digest, record in manifest["media"].items():
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(record, dict):
                raise InventoryError(f"export has malformed media record: {digest}")
            member_name = record.get("path")
            if member_name != f"media/{digest}":
                raise InventoryError(f"export has invalid media path: {digest}")
            member = members.get(member_name)
            if member is None:
                raise InventoryError(f"export is missing member: {member_name}")
            if member.size != record.get("byte_size"):
                raise InventoryError(f"export media size mismatch: {digest}")
            actual_hash, actual_size = archive_member_digest(archive, member)
            if actual_hash != digest or actual_size != record["byte_size"]:
                raise InventoryError(f"export media content mismatch: {digest}")
            expected_members.add(member_name)
        if set(members) != expected_members:
            unexpected = sorted(set(members) - expected_members)
            raise InventoryError(f"export contains unexpected members: {unexpected}")
    return manifest


def validate_extracting_workspace(
    journal: dict, manifest: dict, staged_inventory: Path, staged_media: Path
) -> None:
    """Reject every unjournalled path before resuming archive extraction."""
    auxiliary = manifest.get("auxiliary", {})
    inventory_files = {
        *(f"Data/store/{name}" for name in manifest["store"]),
        *(f"Data/{name}" for name in auxiliary),
        RUNTIME_BINDING,
        INVENTORY_GITIGNORE,
    }
    if manifest["format"] == 1:
        inventory_files.add(DEGRADED_MARKER)
    media_files = {f"sha256/{digest[:2]}/{digest}" for digest in manifest["media"]}
    for root, allowed_files, label in (
        (staged_inventory, inventory_files, "inventory extraction"),
        (staged_media, media_files, "media extraction"),
    ):
        allowed_directories = manifest_directories({name: "0" * 64 for name in allowed_files})
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise RestoreConflict(f"{label} contains a symlink: {relative}")
            if path.is_file() and relative not in allowed_files:
                raise RestoreConflict(f"{label} contains an unexpected file: {relative}")
            if path.is_dir() and relative not in allowed_directories:
                raise RestoreConflict(f"{label} contains an unexpected directory: {relative}")
            if not path.is_file() and not path.is_dir():
                raise RestoreConflict(f"{label} contains a special file: {relative}")
    generated = {
        RUNTIME_BINDING: (
            json.dumps(
                runtime_binding_payload(Path(journal["runtime"]), journal["installation_id"]),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode(),
        INVENTORY_GITIGNORE: f"/{RUNTIME_BINDING}\n".encode(),
    }
    if manifest["format"] == 1:
        generated[DEGRADED_MARKER] = (
            json.dumps(
                {
                    "format": 1,
                    "reasons": ["legacy format-1 export had no auxiliary-data manifest"],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()
    for name, expected in generated.items():
        path = staged_inventory / name
        if path_entry_exists(path) and (
            path.is_symlink() or not path.is_file() or path.read_bytes() != expected
        ):
            raise RestoreConflict(f"generated restore staging changed during extraction: {name}")
    preimage = Path(journal["workspace"]) / "catalogue.before"
    if path_entry_exists(preimage) and (
        not journal["catalogue_existed"]
        or preimage.is_symlink()
        or not preimage.is_file()
        or file_digest(preimage) != journal["catalogue_before_sha256"]
    ):
        raise RestoreConflict("restore catalogue preimage changed during extraction")


def target_is_empty(path: Path) -> bool:
    return not path_entry_exists(path) or (
        not path.is_symlink() and path.is_dir() and not any(path.iterdir())
    )


def inventory_tree_exclusions(args: argparse.Namespace) -> frozenset[str]:
    try:
        relative = args.catalogue_output.relative_to(args.inventory_root)
    except ValueError:
        return frozenset()
    return frozenset({relative.as_posix()})


def restore_tree_manifest(root: Path, *, excluded: frozenset[str] = frozenset()) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise RestoreConflict(f"restore tree is missing or unsafe: {root}")
    files: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        if path.is_symlink():
            raise RestoreConflict(f"restore tree contains a symlink: {relative}")
        if path.is_file():
            files[relative] = file_digest(path)
        elif not path.is_dir():
            raise RestoreConflict(f"restore tree contains a special file: {relative}")
    return dict(sorted(files.items()))


def valid_tree_manifest(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for name, digest in value.items():
        path = PurePosixPath(name) if isinstance(name, str) else None
        if (
            path is None
            or not name
            or path.is_absolute()
            or ".." in path.parts
            or str(path) != name
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            return False
    return True


def tree_matches(
    root: Path,
    expected: dict[str, str],
    *,
    excluded: frozenset[str] = frozenset(),
) -> bool:
    try:
        return restore_tree_manifest(root, excluded=excluded) == expected
    except RestoreConflict:
        return False


def restore_rollback_stash(target: Path, restore_id: str, label: str) -> Path:
    """Return a deterministic same-filesystem quarantine for one installed tree."""
    token = hashlib.sha256(restore_id.encode()).hexdigest()[:16]
    return target.with_name(f".{target.name}.property-inventory-rollback-{label}-{token}")


def restore_transfer_stage(target: Path, restore_id: str, label: str) -> Path:
    """Return a target-sibling stage so final installation never crosses filesystems."""
    token = hashlib.sha256(restore_id.encode()).hexdigest()[:16]
    return target.with_name(f".{target.name}.property-inventory-transfer-{label}-{token}")


def restore_transfer_build(target: Path, restore_id: str, label: str) -> Path:
    """Return the owned incomplete-copy path used before transfer publication."""
    return restore_transfer_stage(target, restore_id, label).with_name(
        restore_transfer_stage(target, restore_id, label).name + ".building"
    )


def install_restore_tree(
    staged: Path,
    target: Path,
    expected: dict[str, str],
    restore_id: str,
    label: str,
    *,
    excluded: frozenset[str] = frozenset(),
) -> None:
    """Copy to a same-filesystem sibling, then atomically install the exact tree."""
    if tree_matches(target, expected, excluded=excluded):
        remove_partial_owned_tree(staged, expected, f"restore {label} staging")
        transfer = restore_transfer_stage(target, restore_id, label)
        remove_partial_owned_tree(transfer, expected, f"restore {label} transfer staging")
        return
    if path_entry_exists(staged):
        if not tree_matches(staged, expected):
            raise RestoreConflict(f"restore staging changed outside recovery: {staged}")
    else:
        raise RestoreConflict(f"restore staging or proven target is missing for {label}")
    transfer = restore_transfer_stage(target, restore_id, label)
    build = restore_transfer_build(target, restore_id, label)
    if path_entry_exists(build):
        remove_partial_copy_tree(build, expected, f"restore {label} incomplete transfer", staged)
    if path_entry_exists(transfer):
        if not tree_matches(transfer, expected):
            raise RestoreConflict(f"restore {label} published transfer changed outside recovery")
    if not path_entry_exists(transfer):
        shutil.copytree(staged, build)
        fsync_tree(build)
        if not tree_matches(build, expected):
            raise RestoreConflict(f"restore transfer copy is incomplete for {label}")
        os.replace(build, transfer)
        fsync_directory(transfer.parent)
    if not tree_matches(transfer, expected):
        raise RestoreConflict(f"restore transfer staging is incomplete for {label}")
    if os.environ.get("PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_TRANSFER") == label:
        os._exit(84 if label == "media" else 85)
    if path_entry_exists(target):
        if not target_is_empty(target):
            raise InventoryError(f"restore target became non-empty: {target}")
        target.rmdir()
    os.replace(transfer, target)
    fsync_directory(target.parent)
    remove_partial_owned_tree(staged, expected, f"restore {label} staging")


def restore_root_original(target: Path, existed: bool, mode: int | None = None) -> bool:
    """Prove the exact blank state accepted before a restore began."""
    if existed:
        return (
            path_entry_exists(target)
            and not target.is_symlink()
            and target.is_dir()
            and not any(target.iterdir())
            and (mode is None or target.stat().st_mode & 0o7777 == mode)
        )
    return not path_entry_exists(target)


def manifest_directories(expected: dict[str, str]) -> set[str]:
    directories: set[str] = set()
    for name in expected:
        parent = PurePosixPath(name).parent
        while str(parent) != ".":
            directories.add(str(parent))
            parent = parent.parent
    return directories


def validate_partial_owned_tree(root: Path, expected: dict[str, str], label: str) -> None:
    """Accept only a digest-valid subset, so interrupted cleanup can resume safely."""
    if not path_entry_exists(root):
        return
    if root.is_symlink() or not root.is_dir():
        raise RestoreConflict(f"{label} is not a real directory: {root}")
    allowed_directories = manifest_directories(expected)
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise RestoreConflict(f"{label} contains a symlink: {relative}")
        if path.is_file():
            digest = expected.get(relative)
            if digest is None or file_digest(path) != digest:
                raise RestoreConflict(f"{label} contains unexpected bytes: {relative}")
        elif path.is_dir():
            if relative not in allowed_directories:
                raise RestoreConflict(f"{label} contains an unexpected directory: {relative}")
        else:
            raise RestoreConflict(f"{label} contains a special file: {relative}")


def remove_partial_owned_tree(root: Path, expected: dict[str, str], label: str) -> None:
    """Delete only manifest-owned remaining bytes, with journal-led resumability."""
    validate_partial_owned_tree(root, expected, label)
    if not path_entry_exists(root):
        return
    files = [path for path in root.rglob("*") if path.is_file()]
    for path in files:
        path.unlink()
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda part: len(part.parts), reverse=True):
        directory.rmdir()
    root.rmdir()
    fsync_directory(root.parent)


def validate_partial_copy_tree(
    root: Path, expected: dict[str, str], label: str, source_root: Path
) -> None:
    """Accept only exact files or genuine byte prefixes of their staged sources."""
    if not path_entry_exists(root):
        return
    if root.is_symlink() or not root.is_dir():
        raise RestoreConflict(f"{label} is not a real directory: {root}")
    allowed_directories = manifest_directories(expected)
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise RestoreConflict(f"{label} contains a symlink: {relative}")
        if path.is_file():
            if relative not in expected:
                raise RestoreConflict(f"{label} contains an unexpected file: {relative}")
            if file_digest(path) != expected[relative]:
                source = source_root / Path(relative)
                if source.is_symlink() or not source.is_file():
                    raise RestoreConflict(f"{label} has no source for partial file: {relative}")
                if path.stat().st_size > source.stat().st_size:
                    raise RestoreConflict(
                        f"{label} partial file is larger than its source: {relative}"
                    )
                remaining = path.stat().st_size
                with path.open("rb") as partial, source.open("rb") as complete:
                    while remaining:
                        size = min(1024 * 1024, remaining)
                        if partial.read(size) != complete.read(size):
                            raise RestoreConflict(
                                f"{label} bytes are not a source prefix: {relative}"
                            )
                        remaining -= size
        elif path.is_dir():
            if relative not in allowed_directories:
                raise RestoreConflict(f"{label} contains an unexpected directory: {relative}")
        else:
            raise RestoreConflict(f"{label} contains a special file: {relative}")


def remove_partial_copy_tree(
    root: Path, expected: dict[str, str], label: str, source_root: Path
) -> None:
    """Remove an interrupted system-owned copy while preserving unknown paths."""
    validate_partial_copy_tree(root, expected, label, source_root)
    if not path_entry_exists(root):
        return
    for path in [path for path in root.rglob("*") if path.is_file()]:
        path.unlink()
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda part: len(part.parts), reverse=True):
        directory.rmdir()
    root.rmdir()
    fsync_directory(root.parent)


def validate_restore_workspace_entries(journal: dict, *, allow_missing_preimage: bool) -> None:
    workspace = Path(journal["workspace"])
    if not path_entry_exists(workspace):
        return
    if workspace.is_symlink() or not workspace.is_dir():
        raise RestoreConflict("restore workspace is not a real directory")
    allowed = {"inventory", "media"}
    if journal["catalogue_before_sha256"] is not None:
        allowed.add("catalogue.before")
    unexpected = sorted(path.name for path in workspace.iterdir() if path.name not in allowed)
    if unexpected:
        raise RestoreConflict(
            "restore workspace contains unexpected entries: " + ", ".join(unexpected)
        )
    preimage = workspace / "catalogue.before"
    if path_entry_exists(preimage):
        if (
            preimage.is_symlink()
            or not preimage.is_file()
            or file_digest(preimage) != journal["catalogue_before_sha256"]
        ):
            raise RestoreConflict("restore catalogue preimage changed during cleanup")
    elif journal["catalogue_before_sha256"] is not None and not allow_missing_preimage:
        raise RestoreConflict("restore catalogue preimage disappeared before cleanup")


def remove_restore_workspace_preimage(journal: dict) -> None:
    workspace = Path(journal["workspace"])
    if not path_entry_exists(workspace):
        return
    preimage = workspace / "catalogue.before"
    if path_entry_exists(preimage):
        preimage.unlink()
    try:
        workspace.rmdir()
    except OSError as error:
        raise RestoreConflict("restore workspace still contains unowned recovery state") from error
    fsync_directory(workspace.parent)


def finish_restore_cleanup(paths: dict[str, Path], journal: dict) -> None:
    """Remove committed recovery state, deleting the journal last."""
    workspace = Path(journal["workspace"])
    validate_restore_workspace_entries(journal, allow_missing_preimage=True)
    if path_entry_exists(workspace / "inventory") or path_entry_exists(workspace / "media"):
        raise RestoreConflict("committed restore retained an unexpected staged tree")
    remove_restore_workspace_preimage(journal)
    paths["restore_journal"].unlink(missing_ok=True)
    fsync_directory(paths["runtime"])


def catalogue_restore_state(journal: dict) -> str:
    catalogue = Path(journal["catalogue"])
    before = journal["catalogue_before_sha256"]
    after = journal["catalogue_after_sha256"]
    if not path_entry_exists(catalogue):
        if before is None:
            return "before"
        raise RestoreConflict("restore catalogue disappeared after the journal was prepared")
    if catalogue.is_symlink() or not catalogue.is_file():
        raise RestoreConflict("restore catalogue became a non-regular file")
    current = file_digest(catalogue)
    if current == after:
        return "after"
    if before is not None and current == before:
        return "before"
    raise RestoreConflict("restore catalogue changed outside the pending restore")


def restore_catalogue_preimage(journal: dict, state: str) -> None:
    if state == "before":
        return
    catalogue = Path(journal["catalogue"])
    if journal["catalogue_before_sha256"] is not None:
        preimage = Path(journal["workspace"]) / "catalogue.before"
        if (
            preimage.is_symlink()
            or not preimage.is_file()
            or file_digest(preimage) != journal["catalogue_before_sha256"]
        ):
            raise RestoreConflict("restore catalogue preimage is missing or corrupt")
        temporary = catalogue.with_name(f".{catalogue.name}.restore-{os.getpid()}")
        catalogue.parent.mkdir(parents=True, exist_ok=True)
        durable_copy(preimage, temporary)
        os.replace(temporary, catalogue)
        fsync_directory(catalogue.parent)
    else:
        catalogue.unlink(missing_ok=True)
        if catalogue.parent.exists():
            fsync_directory(catalogue.parent)


def validate_restore_journal(
    args: argparse.Namespace, paths: dict[str, Path], journal: dict
) -> None:
    expected = {
        "inventory_root": str(args.inventory_root),
        "media_root": str(args.media_root),
        "catalogue": str(args.catalogue_output),
        "runtime": str(args.runtime_dir),
        "installation_id": args.installation_id,
    }
    if (
        not isinstance(journal, dict)
        or journal.get("format") not in {1, 2}
        or any(journal.get(key) != value for key, value in expected.items())
    ):
        raise InventoryError("restore journal is malformed or belongs to another instance")
    phase = journal.get("phase")
    if (
        not isinstance(journal.get("workspace"), str)
        or not isinstance(journal.get("restore_id"), str)
        or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            journal.get("restore_id", ""),
        )
        or not isinstance(journal.get("catalogue_existed"), bool)
        or not isinstance(journal.get("inventory_root_existed"), bool)
        or not isinstance(journal.get("media_root_existed"), bool)
        or not isinstance(journal.get("inventory_id"), str)
        or not valid_installation_id(journal.get("installation_id"))
        or not isinstance(journal.get("unsafe_legacy"), bool)
        or phase not in {"extracting", "prepared", "rolling_back", "rolled_back", "committed"}
        or (journal["format"] == 1 and phase == "extracting")
        or (
            journal.get("catalogue_before_sha256") is not None
            and (
                not isinstance(journal.get("catalogue_before_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", journal["catalogue_before_sha256"])
                )
            )
        or journal.get("catalogue_existed") != (journal.get("catalogue_before_sha256") is not None)
    ):
        raise InventoryError("restore journal is malformed")
    if phase == "extracting":
        archive = journal.get("archive")
        if (
            journal["format"] != 2
            or not isinstance(archive, str)
            or archive != str(args.archive.expanduser().resolve())
            or not isinstance(journal.get("archive_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", journal["archive_sha256"])
            or not isinstance(journal.get("owner_claimed"), bool)
            or journal.get("inventory_tree") is not None
            or journal.get("media_tree") is not None
            or journal.get("catalogue_after_sha256") is not None
            or any(
                journal.get(field) is not None
                and (not isinstance(journal.get(field), int) or not 0 <= journal[field] <= 0o7777)
                for field in ("inventory_root_mode", "media_root_mode")
            )
        ):
            raise InventoryError("extracting restore journal is malformed")
    elif (
        not valid_tree_manifest(journal.get("inventory_tree"))
        or not valid_tree_manifest(journal.get("media_tree"))
        or not isinstance(journal.get("catalogue_after_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", journal["catalogue_after_sha256"])
    ):
        raise InventoryError("prepared restore journal is malformed")
    workspace = Path(journal["workspace"])
    if (
        workspace.parent != paths["runtime"]
        or not workspace.name.startswith(".property-inventory-restore-")
        or workspace.is_symlink()
    ):
        raise InventoryError("restore journal has an unsafe workspace")
    if phase in {"prepared", "rolling_back"} and not workspace.is_dir():
        raise InventoryError("restore journal workspace is missing before rollback completed")
    if phase in {"prepared", "rolling_back"} and journal["catalogue_before_sha256"] is not None:
        preimage = workspace / "catalogue.before"
        if (
            preimage.is_symlink()
            or not preimage.is_file()
            or file_digest(preimage) != journal["catalogue_before_sha256"]
        ):
            raise InventoryError("restore journal has a missing or corrupt catalogue preimage")
    if phase == "committed" and not isinstance(journal.get("checks"), dict):
        raise InventoryError("committed restore journal is missing checks")


def rollback_pending_restore(
    args: argparse.Namespace, paths: dict[str, Path], journal: dict
) -> None:
    if journal["phase"] not in {"prepared", "rolling_back", "rolled_back"}:
        raise RestoreConflict("a committed restore must never be rolled back")
    workspace = Path(journal["workspace"])
    stages = (
        (
            "inventory",
            workspace / "inventory",
            args.inventory_root,
            journal["inventory_root_existed"],
            journal["inventory_tree"],
            journal.get("inventory_root_mode"),
        ),
        (
            "media",
            workspace / "media",
            args.media_root,
            journal["media_root_existed"],
            journal["media_tree"],
            journal.get("media_root_mode"),
        ),
    )
    failure_step = os.environ.get("PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_ROLLBACK_STEP")

    def fail_after(step: str, code: int) -> None:
        if failure_step == step:
            os._exit(code)

    if journal["phase"] == "prepared":
        inventory_excluded = inventory_tree_exclusions(args)
        catalogue_restore_state(journal)
        validate_restore_workspace_entries(journal, allow_missing_preimage=False)
        for label, staged, target, existed, expected_tree, original_mode in stages:
            stash = restore_rollback_stash(target, journal["restore_id"], label)
            transfer = restore_transfer_stage(target, journal["restore_id"], label)
            build = restore_transfer_build(target, journal["restore_id"], label)
            if path_entry_exists(stash):
                raise RestoreConflict(f"restore rollback quarantine already exists: {stash}")
            validate_partial_owned_tree(
                transfer, expected_tree, f"restore {label} transfer staging"
            )
            validate_partial_copy_tree(
                build,
                expected_tree,
                f"restore {label} incomplete transfer",
                staged,
            )
            excluded = inventory_excluded if label == "inventory" else frozenset()
            if path_entry_exists(staged):
                if not tree_matches(staged, expected_tree):
                    raise RestoreConflict(f"restore staging changed outside recovery: {staged}")
                if not restore_root_original(target, existed, original_mode) and not tree_matches(
                    target, expected_tree, excluded=excluded
                ):
                    raise RestoreConflict(f"restore target changed outside recovery: {target}")
            elif not tree_matches(target, expected_tree, excluded=excluded):
                raise RestoreConflict(f"restore target changed outside recovery: {target}")
        journal["phase"] = "rolling_back"
        write_json(paths["restore_journal"], journal)
        fail_after("phase", 86)

    if journal["phase"] == "rolling_back":
        catalogue_state = catalogue_restore_state(journal)
        restore_catalogue_preimage(journal, catalogue_state)
        fail_after("catalogue", 87)
        for index, (label, staged, target, existed, expected_tree, original_mode) in enumerate(
            stages, start=1
        ):
            stash = restore_rollback_stash(target, journal["restore_id"], label)
            transfer = restore_transfer_stage(target, journal["restore_id"], label)
            build = restore_transfer_build(target, journal["restore_id"], label)
            staged_present = path_entry_exists(staged)
            target_installed = tree_matches(target, expected_tree)
            stash_present = path_entry_exists(stash)
            if staged_present and not tree_matches(staged, expected_tree):
                raise RestoreConflict(f"restore staging changed outside rollback: {staged}")
            validate_partial_owned_tree(
                transfer, expected_tree, f"restore {label} transfer staging"
            )
            validate_partial_copy_tree(
                build,
                expected_tree,
                f"restore {label} incomplete transfer",
                staged,
            )
            if target_installed:
                if stash_present:
                    raise RestoreConflict(f"restore rollback has two installed copies for {label}")
                os.replace(target, stash)
                fsync_directory(target.parent)
                if os.environ.get("PROPERTY_INVENTORY_FAIL_DURING_RESTORE_ROLLBACK") == label:
                    os._exit(88 + index)
                fail_after(f"{label}-quarantined", 88 + index)
                if existed:
                    target.mkdir()
                    if original_mode is not None:
                        os.chmod(target, original_mode)
                    fsync_directory(target.parent)
            elif stash_present:
                if not tree_matches(stash, expected_tree):
                    raise RestoreConflict(
                        f"restore rollback quarantine changed outside recovery: {stash}"
                    )
                if existed and not path_entry_exists(target):
                    target.mkdir()
                    if original_mode is not None:
                        os.chmod(target, original_mode)
                    fsync_directory(target.parent)
                if not restore_root_original(target, existed, original_mode):
                    raise RestoreConflict(f"restore target changed outside rollback: {target}")
            elif staged_present:
                if not restore_root_original(target, existed, original_mode):
                    raise RestoreConflict(f"restore target changed outside rollback: {target}")
            else:
                raise RestoreConflict(
                    f"restore rollback lost the staged, installed, and quarantined {label} tree"
                )
            if not restore_root_original(target, existed, original_mode):
                raise RestoreConflict(f"restore did not restore the {label} preimage")
            fail_after(f"{label}-preimage", 90 + index)
        if catalogue_restore_state(journal) != "before":
            raise RestoreConflict("restore did not restore the catalogue preimage")
        journal["phase"] = "rolled_back"
        write_json(paths["restore_journal"], journal)
        fail_after("rolled-back", 93)

    for label, staged, target, existed, expected_tree, original_mode in stages:
        if not restore_root_original(target, existed, original_mode):
            raise RestoreConflict(f"rolled-back {label} target changed before cleanup")
        stash = restore_rollback_stash(target, journal["restore_id"], label)
        transfer = restore_transfer_stage(target, journal["restore_id"], label)
        build = restore_transfer_build(target, journal["restore_id"], label)
        validate_partial_owned_tree(staged, expected_tree, f"restore {label} staging")
        validate_partial_owned_tree(transfer, expected_tree, f"restore {label} transfer staging")
        validate_partial_copy_tree(
            build,
            expected_tree,
            f"restore {label} incomplete transfer",
            staged,
        )
        validate_partial_owned_tree(stash, expected_tree, f"restore {label} quarantine")
    if catalogue_restore_state(journal) != "before":
        raise RestoreConflict("rolled-back catalogue changed before cleanup")
    validate_restore_workspace_entries(journal, allow_missing_preimage=True)
    for label, staged, target, _existed, expected_tree, _original_mode in stages:
        stash = restore_rollback_stash(target, journal["restore_id"], label)
        transfer = restore_transfer_stage(target, journal["restore_id"], label)
        build = restore_transfer_build(target, journal["restore_id"], label)
        remove_partial_owned_tree(staged, expected_tree, f"restore {label} staging")
        remove_partial_owned_tree(transfer, expected_tree, f"restore {label} transfer staging")
        remove_partial_copy_tree(
            build,
            expected_tree,
            f"restore {label} incomplete transfer",
            staged,
        )
        remove_partial_owned_tree(stash, expected_tree, f"restore {label} quarantine")
        fail_after(f"cleanup-{label}", 94 if label == "inventory" else 95)
    remove_restore_workspace_preimage(journal)
    paths["restore_journal"].unlink(missing_ok=True)
    fsync_directory(paths["runtime"])


def restore_result(journal: dict, *, recovered: bool) -> dict:
    legacy_degraded = journal["unsafe_legacy"]
    return {
        "status": (
            "recovered_restored_unsafe_legacy"
            if recovered and legacy_degraded
            else "recovered_restored"
            if recovered
            else "restored_unsafe_legacy"
            if legacy_degraded
            else "restored"
        ),
        "inventory_root": journal["inventory_root"],
        "media_root": journal["media_root"],
        "inventory_id": journal["inventory_id"],
        "checks": journal["checks"],
        "degraded_reasons": (
            ["legacy format-1 export had no auxiliary-data manifest"] if legacy_degraded else []
        ),
    }


def complete_pending_restore(
    args: argparse.Namespace, paths: dict[str, Path], journal: dict, *, recovered: bool
) -> dict:
    workspace = Path(journal["workspace"])
    inventory_excluded = inventory_tree_exclusions(args)
    if journal["phase"] == "committed":
        if not tree_matches(
            args.inventory_root,
            journal["inventory_tree"],
            excluded=inventory_excluded,
        ):
            raise RestoreConflict("committed inventory tree changed before cleanup")
        if not tree_matches(args.media_root, journal["media_tree"]):
            raise RestoreConflict("committed media tree changed before cleanup")
        if catalogue_restore_state(journal) != "after":
            raise RestoreConflict("committed catalogue changed before cleanup")
        result = restore_result(journal, recovered=recovered)
        finish_restore_cleanup(paths, journal)
        return result
    if journal["phase"] != "prepared":
        raise RestoreConflict(f"restore phase cannot continue forward: {journal['phase']}")
    for label, staged, target, expected_tree, excluded in (
        (
            "media",
            workspace / "media",
            args.media_root,
            journal["media_tree"],
            frozenset(),
        ),
        (
            "inventory",
            workspace / "inventory",
            args.inventory_root,
            journal["inventory_tree"],
            inventory_excluded,
        ),
    ):
        install_restore_tree(
            staged,
            target,
            expected_tree,
            journal["restore_id"],
            label,
            excluded=excluded,
        )
        failure_point = os.environ.get("PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_REPLACEMENT")
        if failure_point == label:
            os._exit(94 if label == "media" else 95)
    catalogue_restore_state(journal)
    live_paths = data_paths(args.inventory_root, args.runtime_dir)
    checks = verify_bundle(
        live_paths,
        live_paths["store"],
        live_paths["database"],
        live_paths["catalogue"],
    )
    if os.environ.get("PROPERTY_INVENTORY_FAIL_RESTORE_AFTER_CATALOGUE_REPLACE") == "1":
        raise InventoryError("injected failure after external catalogue replacement")
    if file_digest(args.catalogue_output) != journal["catalogue_after_sha256"]:
        raise RestoreConflict("rendered catalogue disagrees with the prepared restore")
    if journal["unsafe_legacy"]:
        checks["verification"]["status"] = "degraded_unsafe_legacy"
    journal["phase"] = "committed"
    journal["checks"] = checks
    write_json(paths["restore_journal"], journal)
    if os.environ.get("PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_COMMIT") == "1":
        os._exit(96)
    result = restore_result(journal, recovered=recovered)
    finish_restore_cleanup(paths, journal)
    return result


def recover_pending_restore(args: argparse.Namespace) -> dict | None:
    paths = data_paths(args.inventory_root, args.runtime_dir)
    journal_path = paths["restore_journal"]
    if journal_path.is_symlink():
        raise InventoryError("restore journal must not be a symlink")
    if not path_entry_exists(journal_path):
        return None
    validated = False
    try:
        journal = json.loads(journal_path.read_text())
        validate_restore_journal(args, paths, journal)
        validated = True
        if journal["phase"] == "extracting":
            return restore_under_lock(args, journal)
        if journal["phase"] in {"rolling_back", "rolled_back"}:
            rollback_pending_restore(args, paths, journal)
            return None
        return complete_pending_restore(args, paths, journal, recovered=True)
    except RestoreConflict:
        raise
    except BaseException:
        if validated and journal["phase"] in {
            "prepared",
            "rolling_back",
            "rolled_back",
        }:
            rollback_pending_restore(args, paths, journal)
        raise


def reject_untracked_restore_workspaces(runtime_dir: Path) -> None:
    """Never ignore private extraction bytes that lack a recovery journal."""
    journal = runtime_dir / RESTORE_JOURNAL
    if path_entry_exists(journal) or not runtime_dir.exists():
        return
    for workspace in sorted(runtime_dir.glob(".property-inventory-restore-*")):
        if workspace.is_symlink() or not workspace.is_dir():
            raise InventoryError(
                f"untracked restore workspace is unsafe and was preserved: {workspace}"
            )
        if any(workspace.iterdir()):
            raise InventoryError(
                "untracked restore workspace contains private bytes and was preserved for "
                f"inspection: {workspace}"
            )
        workspace.rmdir()
        fsync_directory(runtime_dir)


def command_restore(args: argparse.Namespace) -> dict:
    if args.media_root is None:
        raise InventoryError("restore requires --media-root or PROPERTY_INVENTORY_MEDIA_ROOT")
    try:
        with media_lock(args.media_root), inventory_lock(args.inventory_root):
            journal_path = args.runtime_dir / RESTORE_JOURNAL
            if path_entry_exists(journal_path):
                if journal_path.is_symlink() or not journal_path.is_file():
                    raise InventoryError("restore journal must be a regular file")
                try:
                    pending = json.loads(journal_path.read_text())
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise InventoryError("restore journal is malformed") from error
                installation_id = (
                    pending.get("installation_id") if isinstance(pending, dict) else None
                )
                if not valid_installation_id(installation_id):
                    raise InventoryError("restore journal is malformed")
                args.installation_id = installation_id
                recovered = recover_pending_restore(args)
                if recovered is not None:
                    return recovered
            reject_untracked_restore_workspaces(args.runtime_dir)
            owner = read_runtime_owner(args.runtime_dir)
            if owner is None and runtime_has_unowned_entries(args.runtime_dir):
                raise InventoryError(
                    "restore runtime is non-empty and has no inventory owner marker"
                )
            if owner is not None:
                args.installation_id = owner["installation_id"]
                claim_runtime_owner(
                    args.inventory_root,
                    args.runtime_dir,
                    args.media_root,
                    args.catalogue_output,
                    args.installation_id,
                    None,
                )
            else:
                args.installation_id = str(uuid.uuid4())
            return restore_under_lock(args)
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error


def restore_under_lock(args: argparse.Namespace, journal: dict | None = None) -> dict:
    resuming_extraction = journal is not None
    if args.scope != "private":
        raise InventoryError("restore requires private scope")
    if args.media_root is None:
        raise InventoryError("restore requires --media-root or PROPERTY_INVENTORY_MEDIA_ROOT")
    archive_path = args.archive.expanduser().resolve()
    if not archive_path.is_file():
        raise InventoryError(f"export archive is not a regular file: {archive_path}")
    if archive_path == args.catalogue_output:
        raise InventoryError("restore archive and catalogue output must be different files")
    inventory_root = args.inventory_root
    media_root = args.media_root
    inventory_root_existed = inventory_root.exists()
    media_root_existed = media_root.exists()
    if (
        inventory_root == media_root
        or inventory_root in media_root.parents
        or media_root in inventory_root.parents
    ):
        raise InventoryError("inventory root and media root must be separate directories")
    if not target_is_empty(inventory_root):
        raise InventoryError(f"restore target is not empty: {inventory_root}")
    if not target_is_empty(media_root):
        raise InventoryError(f"restore media target is not empty: {media_root}")
    manifest = inspect_restore_archive(archive_path, allow_unsafe_legacy=args.allow_unsafe_legacy)
    export_format = manifest["format"]
    inventory_root.parent.mkdir(parents=True, exist_ok=True)
    media_root.parent.mkdir(parents=True, exist_ok=True)
    ensure_private_directory(args.runtime_dir)
    paths = data_paths(inventory_root, args.runtime_dir)
    if journal is None:
        restore_id = str(uuid.uuid4())
        workspace = args.runtime_dir / f".property-inventory-restore-{restore_id}"
        catalogue = args.catalogue_output
        if catalogue.exists() and not catalogue.is_file():
            raise InventoryError(f"catalogue output is not a regular file: {catalogue}")
        catalogue_existed = catalogue.exists()
        catalogue_before_hash = file_digest(catalogue) if catalogue_existed else None
        journal = {
            "format": 2,
            "restore_id": restore_id,
            "installation_id": args.installation_id,
            "inventory_root": str(inventory_root),
            "media_root": str(media_root),
            "catalogue": str(catalogue),
            "runtime": str(args.runtime_dir),
            "workspace": str(workspace),
            "phase": "extracting",
            "archive": str(archive_path),
            "archive_sha256": file_digest(archive_path),
            "owner_claimed": False,
            "catalogue_existed": catalogue_existed,
            "catalogue_before_sha256": catalogue_before_hash,
            "catalogue_after_sha256": None,
            "inventory_root_existed": inventory_root_existed,
            "media_root_existed": media_root_existed,
            "inventory_root_mode": (
                inventory_root.stat().st_mode & 0o7777 if inventory_root_existed else None
            ),
            "media_root_mode": (media_root.stat().st_mode & 0o7777 if media_root_existed else None),
            "inventory_tree": None,
            "media_tree": None,
            "inventory_id": manifest["inventory_id"],
            "unsafe_legacy": export_format == 1,
        }
        for label, target in (("inventory", inventory_root), ("media", media_root)):
            for recovery_path in (
                restore_transfer_stage(target, restore_id, label),
                restore_transfer_build(target, restore_id, label),
                restore_rollback_stash(target, restore_id, label),
            ):
                if path_entry_exists(recovery_path):
                    raise RestoreConflict(f"restore recovery path already exists: {recovery_path}")
        write_json(paths["restore_journal"], journal)
    else:
        workspace = Path(journal["workspace"])
        if (
            journal.get("archive") != str(archive_path)
            or journal.get("archive_sha256") != file_digest(archive_path)
            or journal.get("inventory_id") != manifest["inventory_id"]
        ):
            raise RestoreConflict("pending restore archive changed before extraction completed")
        inventory_root_existed = journal["inventory_root_existed"]
        media_root_existed = journal["media_root_existed"]
    claim_runtime_owner(
        inventory_root,
        args.runtime_dir,
        media_root,
        args.catalogue_output,
        args.installation_id,
        manifest["inventory_id"],
    )
    if not journal["owner_claimed"]:
        journal["owner_claimed"] = True
        write_json(paths["restore_journal"], journal)
    ensure_private_directory(workspace)
    staged_inventory = workspace / "inventory"
    staged_media = workspace / "media"
    ensure_private_directory(staged_inventory)
    ensure_private_directory(staged_media)
    validate_extracting_workspace(journal, manifest, staged_inventory, staged_media)
    journal_created = True
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = safe_archive_members(archive)
            manifest = json.loads(archive_bytes(archive, members, "manifest.json"))
            if not isinstance(manifest, dict):
                raise InventoryError("property inventory export manifest must be an object")
            export_format = manifest.get("format")
            if (
                export_format not in {1, 2}
                or manifest.get("schema_version") != SCHEMA_VERSION
                or not isinstance(manifest.get("inventory_id"), str)
                or not isinstance(manifest.get("store"), dict)
                or not isinstance(manifest.get("media"), dict)
                or (export_format == 2 and not isinstance(manifest.get("auxiliary"), dict))
            ):
                raise InventoryError("unsupported or malformed property inventory export")
            if export_format == 1 and not args.allow_unsafe_legacy:
                raise InventoryError(
                    "legacy format-1 export has no auxiliary-data manifest; "
                    "rerun with --allow-unsafe-legacy only for a deliberate degraded restore"
                )
            claim_runtime_owner(
                inventory_root,
                args.runtime_dir,
                media_root,
                args.catalogue_output,
                args.installation_id,
                manifest["inventory_id"],
            )
            auxiliary_manifest = manifest.get("auxiliary", {})
            expected_store = {f"{table}.jsonl" for table in TABLES}
            if set(manifest["store"]) != expected_store:
                raise InventoryError("export canonical store file set is incomplete or unexpected")
            expected_members = {"manifest.json"}
            store_dir = staged_inventory / "Data" / "store"
            for name, expected_hash in manifest["store"].items():
                member_name = f"store/{name}"
                actual_hash, _ = restore_archive_file(
                    archive, members, member_name, store_dir / name
                )
                if actual_hash != expected_hash:
                    raise InventoryError(f"export store hash mismatch: {name}")
                expected_members.add(member_name)
            try:
                media_rows = read_jsonl(store_dir / "media_assets.jsonl")
                expected_media = {
                    row["sha256"]: {
                        "byte_size": row["byte_size"],
                        "path": f"media/{row['sha256']}",
                    }
                    for row in media_rows
                }
            except (KeyError, TypeError) as error:
                raise InventoryError("canonical media assets are malformed") from error
            if len(expected_media) != len(media_rows) or manifest["media"] != expected_media:
                raise InventoryError("export media manifest disagrees with canonical media_assets")
            for name, expected_hash in auxiliary_manifest.items():
                if name != AUXILIARY_MANIFEST:
                    safe_auxiliary_name(name)
                if not isinstance(expected_hash, str) or not re.fullmatch(
                    r"[0-9a-f]{64}", expected_hash
                ):
                    raise InventoryError(f"export has invalid auxiliary digest: {name}")
                member_name = f"auxiliary/{name}"
                actual_hash, _ = restore_archive_file(
                    archive,
                    members,
                    member_name,
                    staged_inventory / "Data" / Path(name),
                )
                if actual_hash != expected_hash:
                    raise InventoryError(f"export auxiliary hash mismatch: {name}")
                expected_members.add(member_name)
            for digest, record in manifest["media"].items():
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise InventoryError(f"export has invalid media digest: {digest}")
                if not isinstance(record, dict):
                    raise InventoryError(f"export has malformed media record: {digest}")
                member_name = record.get("path")
                if member_name != f"media/{digest}":
                    raise InventoryError(f"export has invalid media path: {digest}")
                member = members.get(member_name)
                if member is None:
                    raise InventoryError(f"export is missing member: {member_name}")
                if member.size != record.get("byte_size"):
                    raise InventoryError(f"export media size mismatch: {digest}")
                actual_hash, actual_size = restore_archive_file(
                    archive,
                    members,
                    member_name,
                    media_asset_path(staged_media, digest),
                )
                if actual_hash != digest:
                    raise InventoryError(f"export media hash mismatch: {digest}")
                if actual_size != record.get("byte_size"):
                    raise InventoryError(f"export media size mismatch: {digest}")
                expected_members.add(member_name)
            if set(members) != expected_members:
                unexpected = sorted(set(members) - expected_members)
                raise InventoryError(f"export contains unexpected members: {unexpected}")
        restored_auxiliary = validate_auxiliary_data(staged_inventory / "Data")
        if restored_auxiliary != auxiliary_manifest:
            raise InventoryError("restored auxiliary-data manifest disagrees with export")
        metadata = read_jsonl(staged_inventory / "Data" / "store" / "metadata.jsonl")
        if len(metadata) != 1 or metadata[0].get("inventory_id") != manifest["inventory_id"]:
            raise InventoryError("export manifest inventory_id disagrees with canonical metadata")
        write_json(
            staged_inventory / RUNTIME_BINDING,
            runtime_binding_payload(args.runtime_dir, args.installation_id),
        )
        write_inventory_gitignore(staged_inventory)
        if export_format == 1:
            write_json(
                staged_inventory / DEGRADED_MARKER,
                {
                    "format": 1,
                    "reasons": ["legacy format-1 export had no auxiliary-data manifest"],
                },
            )
        staged_catalogue_hash: str
        with tempfile.TemporaryDirectory(prefix="property-inventory-restore-runtime-") as runtime:
            staged_paths = data_paths(staged_inventory, Path(runtime))
            staged_paths["media_root"] = staged_media
            staged_paths["catalogue"] = Path(runtime) / "staged-Inventory.md"
            verify_bundle(
                staged_paths,
                staged_paths["store"],
                staged_paths["database"],
                staged_paths["catalogue"],
            )
            staged_catalogue_hash = file_digest(staged_paths["catalogue"])
        catalogue = args.catalogue_output
        if catalogue.exists() and not catalogue.is_file():
            raise InventoryError(f"catalogue output is not a regular file: {catalogue}")
        current_catalogue_hash = file_digest(catalogue) if catalogue.exists() else None
        if current_catalogue_hash != journal["catalogue_before_sha256"]:
            raise RestoreConflict("catalogue changed while restore extraction was pending")
        preimage = workspace / "catalogue.before"
        if journal["catalogue_existed"]:
            if path_entry_exists(preimage):
                if (
                    preimage.is_symlink()
                    or not preimage.is_file()
                    or file_digest(preimage) != journal["catalogue_before_sha256"]
                ):
                    raise RestoreConflict("restore catalogue preimage changed during extraction")
            else:
                durable_copy(catalogue, preimage)
        fsync_tree(workspace)
        journal["catalogue_after_sha256"] = staged_catalogue_hash
        journal["inventory_tree"] = restore_tree_manifest(staged_inventory)
        journal["media_tree"] = restore_tree_manifest(staged_media)
        journal["phase"] = "prepared"
        try:
            write_json(paths["restore_journal"], journal)
            if os.environ.get("PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_JOURNAL_WRITE") == "1":
                raise OSError("injected failure after restore journal write")
        except BaseException:
            journal_created = path_entry_exists(paths["restore_journal"])
            raise
        else:
            journal_created = True
        return complete_pending_restore(args, paths, journal, recovered=resuming_extraction)
    except BaseException:
        if journal_created and journal["phase"] in {
            "prepared",
            "rolling_back",
            "rolled_back",
        }:
            rollback_pending_restore(args, paths, journal)
        raise
    finally:
        # A durable extracting journal owns every staging byte.  Failures preserve
        # it for exact-prefix retry instead of deleting uncertain private data.
        pass


def canonical_store_digest(store_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(store_dir.glob("*.jsonl")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def load_verified_store(paths: dict[str, Path]) -> Store:
    """Serve facts only from a generation that passed the full projection gate."""
    store = Store(paths["store"])
    database = paths["database"]
    if database.is_symlink() or not database.is_file():
        raise InventoryError(
            "canonical generation is not verified; run status before querying"
        )
    try:
        uri = f"file:{database.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                "SELECT store_digest FROM verification_state"
            ).fetchall()
    except sqlite3.Error as error:
        raise InventoryError(
            "canonical generation is not verified; run status before querying"
        ) from error
    current = canonical_store_digest(paths["store"])
    if rows != [(current,)]:
        raise InventoryError(
            "canonical generation changed since verification; run status before querying"
        )
    return store


def command_add_interface(args: argparse.Namespace) -> dict:
    def mutate(store: Store) -> dict:
        store.get("models", args.model_id)
        store.get("evidence", args.evidence_id)
        supported_item_ids = {
            row["item_id"]
            for row in store.rows["item_evidence"]
            if row["evidence_id"] == args.evidence_id
        }
        if not any(
            item["item_id"] in supported_item_ids and item["model_id"] == args.model_id
            for item in store.rows["items"]
        ):
            raise InventoryError(
                "interface evidence must already support an item of the selected model"
            )
        matches = [
            row
            for row in store.rows["interfaces"]
            if row["family"].casefold() == args.family.casefold()
            and (row.get("standard") or "").casefold() == (args.standard or "").casefold()
            and (row.get("variant") or "").casefold() == (args.variant or "").casefold()
            and row["direction"] == args.direction
            and json.loads(row["properties_json"]) == args.properties
        ]
        if len(matches) > 1:
            raise InventoryError("multiple normalized interfaces have the same definition")
        if matches:
            interface_id = matches[0]["interface_id"]
            interface_reused = True
        else:
            base = "-".join(
                filter(None, (args.family, args.standard, args.variant, args.direction))
            )
            interface_id = store.allocate("interfaces", f"if-{slug(base)}")
            store.rows["interfaces"].append(
                {
                    "direction": args.direction,
                    "family": args.family,
                    "interface_id": interface_id,
                    "notes": args.notes,
                    "properties_json": json.dumps(
                        args.properties, ensure_ascii=False, sort_keys=True
                    ),
                    "standard": args.standard,
                    "variant": args.variant,
                }
            )
            interface_reused = False
        same_link = [
            row
            for row in store.rows["model_interfaces"]
            if row["model_id"] == args.model_id
            and row["interface_id"] == interface_id
            and row["role"] == args.role
        ]
        if same_link:
            if same_link[0]["evidence_id"] != args.evidence_id:
                raise InventoryError("model interface claim already exists with different evidence")
            link_reused = True
        else:
            store.rows["model_interfaces"].append(
                {
                    "evidence_id": args.evidence_id,
                    "interface_id": interface_id,
                    "model_id": args.model_id,
                    "notes": args.notes,
                    "role": args.role,
                }
            )
            link_reused = False
        return {
            "interface_id": interface_id,
            "model_id": args.model_id,
            "interface_reused": interface_reused,
            "link_reused": link_reused,
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"add-interface-{args.model_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_add_alias(args: argparse.Namespace) -> dict:
    """Add a retrieval alias only when existing item evidence supports it."""
    alias = args.alias.strip()
    alias_kind = args.alias_kind.strip()
    if not alias or not alias_kind:
        raise InventoryError("alias and alias_kind must not be blank")

    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        evidence = store.get("evidence", args.evidence_id)
        if args.evidence_id not in {
            row["evidence_id"]
            for row in store.rows["item_evidence"]
            if row["item_id"] == args.item_id
        }:
            raise InventoryError("alias evidence must already support the selected item")
        required_sensitivity = max(
            (item["sensitivity"], evidence["sensitivity"]),
            key=SENSITIVITY_RANK.__getitem__,
        )
        if SENSITIVITY_RANK[args.sensitivity] < SENSITIVITY_RANK[required_sensitivity]:
            raise InventoryError(
                "alias sensitivity must be at least the selected item and evidence sensitivity"
            )
        existing = [
            row
            for row in store.rows["aliases"]
            if row["item_id"] == args.item_id and row["alias"].casefold() == alias.casefold()
        ]
        if existing:
            row = existing[0]
            if (
                row["alias_kind"] != alias_kind
                or row["evidence_id"] != args.evidence_id
                or row["sensitivity"] != args.sensitivity
                or row.get("notes") != args.notes
            ):
                raise InventoryError("alias already exists with different supported metadata")
            return {"alias_id": row["alias_id"], "alias_reused": True}
        alias_id = store.allocate("aliases", f"alias-{slug(alias)}")
        store.rows["aliases"].append(
            {
                "alias_id": alias_id,
                "item_id": args.item_id,
                "alias": alias,
                "alias_kind": alias_kind,
                "evidence_id": args.evidence_id,
                "sensitivity": args.sensitivity,
                "notes": args.notes,
            }
        )
        return {"alias_id": alias_id, "alias_reused": False}

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"add-alias-{args.item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_add_valuation(args: argparse.Namespace) -> dict:
    """Record a value fact without turning it into an ownership assertion."""
    if (
        isinstance(args.amount, bool)
        or not isinstance(args.amount, (int, float))
        or not math.isfinite(args.amount)
        or args.amount < 0
    ):
        raise InventoryError("valuation amount must be a finite non-negative number")
    currency = args.currency.strip()
    if re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise InventoryError("valuation currency must be a three-letter uppercase code")
    using_existing = args.evidence_id is not None
    using_source = args.source_ref is not None or args.captured_on is not None
    if using_existing == using_source:
        raise InventoryError(
            "provide either --evidence-id or both --source-ref and --captured-on"
        )
    if using_source and (args.source_ref is None or args.captured_on is None):
        raise InventoryError("--source-ref and --captured-on must be supplied together")
    if using_existing and args.evidence_type is not None:
        raise InventoryError("--evidence-type is only valid with --source-ref")

    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        if SENSITIVITY_RANK[args.sensitivity] < SENSITIVITY_RANK[item["sensitivity"]]:
            raise InventoryError(
                "valuation sensitivity must be at least the selected item and evidence sensitivity"
            )
        evidence_created = False
        if using_existing:
            evidence_id = args.evidence_id
            evidence = store.get("evidence", evidence_id)
            if not any(
                row["item_id"] == args.item_id
                and row["evidence_id"] == evidence_id
                for row in store.rows["item_evidence"]
            ):
                raise InventoryError(
                    "valuation evidence must already support the selected item"
                )
        else:
            source_ref = args.source_ref.strip()
            if not source_ref:
                raise InventoryError("--source-ref must not be blank")
            evidence_type = args.evidence_type or "research"
            evidence_digest = hashlib.sha256(
                "\0".join(
                    (
                        args.item_id,
                        evidence_type,
                        source_ref,
                        args.captured_on,
                        args.sensitivity,
                    )
                ).encode("utf-8")
            ).hexdigest()[:24]
            evidence_id = f"ev-valuation-{evidence_digest}"
            evidence_row = {
                "captured_on": args.captured_on,
                "claim_strength": "research_only",
                "evidence_id": evidence_id,
                "evidence_type": evidence_type,
                "notes": args.notes,
                "sensitivity": args.sensitivity,
                "source_ref": source_ref,
            }
            existing_evidence = [
                row
                for row in store.rows["evidence"]
                if row["evidence_id"] == evidence_id
            ]
            if existing_evidence:
                if len(existing_evidence) != 1 or existing_evidence[0] != evidence_row:
                    raise InventoryError(
                        "valuation source evidence already exists with different content"
                    )
            else:
                store.rows["evidence"].append(evidence_row)
                evidence_created = True
            if not any(
                row["item_id"] == args.item_id
                and row["evidence_id"] == evidence_id
                for row in store.rows["item_evidence"]
            ):
                store.rows["item_evidence"].append(
                    {
                        "item_id": args.item_id,
                        "evidence_id": evidence_id,
                        "role": "supporting",
                    }
                )
            evidence = evidence_row
        required_sensitivity = max(
            (item["sensitivity"], evidence["sensitivity"]),
            key=SENSITIVITY_RANK.__getitem__,
        )
        if SENSITIVITY_RANK[args.sensitivity] < SENSITIVITY_RANK[required_sensitivity]:
            raise InventoryError(
                "valuation sensitivity must be at least the selected item and evidence sensitivity"
            )
        if not valuation_evidence_supports_basis(evidence, args.basis):
            raise InventoryError(
                f"{args.basis} valuation evidence has incompatible type or claim strength"
            )
        valuation_digest = hashlib.sha256(
            "\0".join(
                (
                    args.item_id,
                    format(args.amount, ".17g"),
                    currency,
                    args.valued_on,
                    args.basis,
                    evidence_id,
                )
            ).encode("utf-8")
        ).hexdigest()[:24]
        valuation_id = f"valuation-{valuation_digest}"
        valuation_row = {
            "valuation_id": valuation_id,
            "item_id": args.item_id,
            "amount": args.amount,
            "currency": currency,
            "valued_on": args.valued_on,
            "basis": args.basis,
            "evidence_id": evidence_id,
            "sensitivity": args.sensitivity,
            "notes": args.notes,
        }
        existing = [
            row
            for row in store.rows["valuations"]
            if row["valuation_id"] == valuation_id
        ]
        if existing:
            if len(existing) != 1 or existing[0] != valuation_row:
                raise InventoryError(
                    "valuation already exists with different supported metadata"
                )
            reused = True
        else:
            store.rows["valuations"].append(valuation_row)
            reused = False
        return {
            "valuation_id": valuation_id,
            "evidence_id": evidence_id,
            "evidence_created": evidence_created,
            "valuation_reused": reused,
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"add-valuation-{args.item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def evidence_supports_item(store: Store, evidence_id: str, item_id: str) -> bool:
    return any(
        row["evidence_id"] == evidence_id and row["item_id"] == item_id
        for row in store.rows["item_evidence"]
    )


def command_add_tag(args: argparse.Namespace) -> dict:
    """Add evidence-bound metadata visible only at its declared sensitivity."""
    tag = args.tag.strip()
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", tag) is None or len(tag) > 64:
        raise InventoryError(
            "classification tag must be 1-64 lowercase letters, numbers, or single hyphens"
        )

    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        evidence = store.get("evidence", args.evidence_id)
        if not evidence_supports_item(store, args.evidence_id, args.item_id):
            raise InventoryError("tag evidence must already support the item")
        required_sensitivity = max(
            (item["sensitivity"], evidence["sensitivity"]),
            key=SENSITIVITY_RANK.__getitem__,
        )
        if SENSITIVITY_RANK[args.sensitivity] < SENSITIVITY_RANK[required_sensitivity]:
            raise InventoryError("tag sensitivity is lower than its item or evidence")
        requested = {
            "evidence_id": args.evidence_id,
            "item_id": args.item_id,
            "notes": args.notes,
            "sensitivity": args.sensitivity,
            "tag": tag,
        }
        matches = [
            row
            for row in store.rows["item_tags"]
            if row["item_id"] == args.item_id
            and row["tag"].casefold() == tag.casefold()
        ]
        if matches:
            if len(matches) != 1 or matches[0] != requested:
                raise InventoryError("same classification tag exists with different provenance")
            return {"item_id": args.item_id, "tag": matches[0]["tag"], "reused": True}
        store.rows["item_tags"].append(requested)
        return {"item_id": args.item_id, "tag": tag, "reused": False}

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"add-tag-{args.item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_add_kit(args: argparse.Namespace) -> dict:
    """Create one evidence-backed operational kit for an item."""
    name = args.name.strip()
    if not name:
        raise InventoryError("kit name must not be blank")

    def mutate(store: Store) -> dict:
        store.get("items", args.serves_item_id)
        store.get("evidence", args.evidence_id)
        if not evidence_supports_item(store, args.evidence_id, args.serves_item_id):
            raise InventoryError("kit evidence must already support the served item")
        requested_id = args.kit_id or f"kit-{slug(name)}"
        requested = {
            "kit_id": requested_id,
            "name": name,
            "serves_item_id": args.serves_item_id,
            "evidence_id": args.evidence_id,
            "notes": args.notes,
        }
        same_id = [row for row in store.rows["kits"] if row["kit_id"] == requested_id]
        if same_id:
            if len(same_id) != 1 or same_id[0] != requested:
                raise InventoryError(f"kit id collision with different content: {requested_id}")
            return {"kit_id": requested_id, "reused": True}
        same_name = [
            row
            for row in store.rows["kits"]
            if row["serves_item_id"] == args.serves_item_id
            and row["name"].casefold() == name.casefold()
        ]
        if same_name:
            raise InventoryError(
                "same-name kit exists with different evidence or metadata: "
                + ", ".join(row["kit_id"] for row in same_name)
            )
        store.rows["kits"].append(requested)
        return {"kit_id": requested_id, "reused": False}

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"add-kit-{name}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_set_kit_requirement(args: argparse.Namespace) -> dict:
    """Set one current evidence-backed requirement result within a kit."""
    key = args.requirement_key.strip()
    if not key:
        raise InventoryError("kit requirement key must not be blank")
    if args.status == "source_present" and args.item_id is None:
        raise InventoryError("source_present requires --item-id")
    if args.status == "exists_unassigned" and args.item_id is None:
        raise InventoryError("exists_unassigned requires --item-id")
    if args.status == "not_recorded" and args.item_id is not None:
        raise InventoryError("not_recorded must not assign --item-id")

    def mutate(store: Store) -> dict:
        store.get("kits", args.kit_id)
        evidence = store.get("evidence", args.evidence_id)
        if args.item_id is not None:
            assigned_item = store.get("items", args.item_id)
            if not evidence_supports_item(store, args.evidence_id, args.item_id):
                raise InventoryError(
                    "requirement evidence must also support the assigned item"
                )
            if args.status in {"source_present", "exists_unassigned"} and (
                assigned_item["ownership_state"] != "confirmed"
                or evidence["claim_strength"] != "explicit_current"
            ):
                raise InventoryError(
                    "a current kit requirement needs a confirmed item and explicit-current evidence"
                )
            if args.status == "source_present" and evidence["evidence_type"] != "physical_check":
                raise InventoryError("source_present requires physical_check evidence")
        verified_event_sequence = None
        if args.status in {"source_present", "exists_unassigned"}:
            matching_events = [
                row
                for row in store.rows["inventory_events"]
                if row["item_id"] == args.item_id
                and row["evidence_id"] == args.evidence_id
                and (
                    args.status != "source_present"
                    or row["event_type"] == "physically_verified"
                )
            ]
            if not matching_events:
                raise InventoryError(
                    "a current kit requirement needs a matching ownership event"
                )
            verified_event_sequence = max(
                row["sequence"] for row in matching_events
            )
        requested = {
            "kit_id": args.kit_id,
            "requirement_key": key,
            "item_id": args.item_id,
            "status": args.status,
            "evidence_id": args.evidence_id,
            "notes": args.notes,
            "recorded_at": None,
            "verified_event_sequence": verified_event_sequence,
        }
        existing = [
            row
            for row in store.rows["kit_requirements"]
            if row["kit_id"] == args.kit_id and row["requirement_key"] == key
        ]
        if existing:
            comparable = {
                **requested,
                "recorded_at": existing[0].get("recorded_at"),
            }
            if existing[0] == comparable:
                return {
                    "kit_id": args.kit_id,
                    "requirement_key": key,
                    "reused": True,
                }
        requested["recorded_at"] = recorded_timestamp(args.recorded_at)
        previous = dict(existing[0]) if existing else None
        if existing:
            store.rows["kit_requirements"].remove(existing[0])
        store.rows["kit_requirements"].append(requested)
        fact_amendment_id = None
        if previous is not None:
            selector_json = strict_json_dumps(
                {"kit_id": args.kit_id, "requirement_key": key}, sort_keys=True
            )
            previous_json = strict_json_dumps(previous, sort_keys=True)
            replacement_json = strict_json_dumps(requested, sort_keys=True)
            identity = strict_json_dumps(
                {
                    "action": "replace",
                    "amended_on": evidence["captured_on"],
                    "evidence_id": args.evidence_id,
                    "previous_json": previous_json,
                    "recorded_at": requested["recorded_at"],
                    "replacement_json": replacement_json,
                    "selector_json": selector_json,
                    "table_name": "kit_requirements",
                },
                sort_keys=True,
            )
            fact_amendment_id = "fact-amend-" + hashlib.sha256(
                identity.encode()
            ).hexdigest()[:24]
            affected_ids = {
                item_id
                for item_id in (previous.get("item_id"), requested.get("item_id"))
                if item_id is not None
            }
            store.rows["fact_amendments"].append(
                {
                    "action": "replace",
                    "actor": args.actor,
                    "amended_on": evidence["captured_on"],
                    "evidence_id": args.evidence_id,
                    "fact_amendment_id": fact_amendment_id,
                    "notes": args.notes,
                    "previous_json": previous_json,
                    "reason": "kit_requirement_update",
                    "recorded_at": requested["recorded_at"],
                    "replacement_json": replacement_json,
                    "selector_json": selector_json,
                    "sensitivity": max(
                        (
                            evidence["sensitivity"],
                            *(
                                store.get("items", item_id)["sensitivity"]
                                for item_id in affected_ids
                            ),
                        ),
                        key=SENSITIVITY_RANK.__getitem__,
                    ),
                    "table_name": "kit_requirements",
                }
            )
        return {
            "fact_amendment_id": fact_amendment_id,
            "kit_id": args.kit_id,
            "requirement_key": key,
            "reused": False,
            "previous": previous,
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"set-kit-requirement-{args.kit_id}-{key}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_review_kit(args: argparse.Namespace) -> dict:
    """Seal whether the currently named requirement list is complete."""
    source_ref = args.source_ref.strip()
    if not source_ref:
        raise InventoryError("kit review source reference must not be blank")

    def mutate(store: Store) -> dict:
        kit = store.get("kits", args.kit_id)
        served_item = store.get("items", kit["serves_item_id"])
        requirements = [
            row
            for row in store.rows["kit_requirements"]
            if row["kit_id"] == args.kit_id
        ]
        requirement_keys = sorted(row["requirement_key"] for row in requirements)
        if args.completeness == "complete":
            if not requirement_keys:
                raise InventoryError("a complete kit review requires at least one requirement")
        keys_json = strict_json_dumps(requirement_keys, sort_keys=True)
        evidence_identity = strict_json_dumps(
            {
                "completeness": args.completeness,
                "kit_id": args.kit_id,
                "requirement_keys": requirement_keys,
                "reviewed_on": args.reviewed_on,
                "source_ref": source_ref,
            },
            sort_keys=True,
        )
        evidence_id = "ev-kit-review-" + hashlib.sha256(
            evidence_identity.encode()
        ).hexdigest()[:24]
        sensitivity = max(
            (
                served_item["sensitivity"],
                *(
                    store.get("items", row["item_id"])["sensitivity"]
                    for row in requirements
                    if row["item_id"] is not None
                ),
            ),
            key=SENSITIVITY_RANK.__getitem__,
        )
        evidence_row = {
            "captured_on": args.reviewed_on,
            "claim_strength": "research_only",
            "evidence_id": evidence_id,
            "evidence_type": "user_source",
            "notes": args.notes,
            "sensitivity": sensitivity,
            "source_ref": source_ref,
        }
        existing_evidence = [
            row for row in store.rows["evidence"] if row["evidence_id"] == evidence_id
        ]
        if existing_evidence:
            if len(existing_evidence) != 1 or existing_evidence[0] != evidence_row:
                raise InventoryError("kit-review evidence identity collides")
        else:
            store.rows["evidence"].append(evidence_row)
        if not evidence_supports_item(store, evidence_id, kit["serves_item_id"]):
            store.rows["item_evidence"].append(
                {
                    "evidence_id": evidence_id,
                    "item_id": kit["serves_item_id"],
                    "role": "supporting",
                }
            )
        identity = "\0".join(
            (
                args.kit_id,
                args.reviewed_on,
                args.completeness,
                keys_json,
                evidence_id,
            )
        )
        review_id = "kit-review-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        requested = {
            "actor": args.actor,
            "completeness": args.completeness,
            "evidence_id": evidence_id,
            "kit_id": args.kit_id,
            "notes": args.notes,
            "recorded_at": None,
            "requirement_keys_json": keys_json,
            "review_id": review_id,
            "reviewed_on": args.reviewed_on,
            "sensitivity": sensitivity,
        }
        existing = [
            row
            for row in store.rows["kit_reviews"]
            if row["review_id"] == review_id
        ]
        if existing:
            comparable = {
                **requested,
                "recorded_at": existing[0].get("recorded_at"),
            }
            if len(existing) != 1 or existing[0] != comparable:
                raise InventoryError("kit review identity collides with different metadata")
            return {"evidence_id": evidence_id, "review_id": review_id, "reused": True}
        requested["recorded_at"] = recorded_timestamp(args.recorded_at)
        store.rows["kit_reviews"].append(requested)
        return {"evidence_id": evidence_id, "review_id": review_id, "reused": False}

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"review-kit-{args.kit_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_add_torque_path(args: argparse.Namespace) -> dict:
    """Create one evidence-backed tool output/range path."""
    output_drive = args.output_drive.strip()
    if not output_drive:
        raise InventoryError("torque output drive must not be blank")
    if (args.min_torque_nm is None) != (args.max_torque_nm is None):
        raise InventoryError("minimum and maximum torque must be supplied together")
    if (
        args.min_torque_nm is not None
        and args.min_torque_nm > args.max_torque_nm
    ):
        raise InventoryError("minimum torque must not exceed maximum torque")
    adapter_description = (
        args.adapter_description.strip()
        if isinstance(args.adapter_description, str)
        else None
    )
    if adapter_description == "":
        raise InventoryError("adapter description must not be blank")
    if args.status == "direct" and (
        args.min_torque_nm is None
        or adapter_description is not None
        or args.adapter_max_torque_nm is not None
    ):
        raise InventoryError(
            "direct torque paths require a complete range and no adapter fields"
        )
    if args.status == "adapter_rating_unknown" and (
        args.min_torque_nm is None
        or adapter_description is None
        or args.adapter_max_torque_nm is not None
    ):
        raise InventoryError(
            "adapter_rating_unknown requires a tool range and adapter description, with no rating"
        )
    if args.status == "attachment_only" and (
        args.min_torque_nm is not None or adapter_description is None
    ):
        raise InventoryError(
            "attachment_only requires an adapter description and no tool range"
        )
    if args.adapter_max_torque_nm is not None and adapter_description is None:
        raise InventoryError("an adapter torque rating requires an adapter description")

    def mutate(store: Store) -> dict:
        store.get("items", args.tool_item_id)
        store.get("evidence", args.evidence_id)
        if not evidence_supports_item(store, args.evidence_id, args.tool_item_id):
            raise InventoryError("torque-path evidence must support the tool item")
        requested_id = args.path_id or (
            f"torque-{slug(args.tool_item_id.removeprefix('itm-'), 36)}-"
            f"{slug(output_drive, 24)}"
        )
        requested = {
            "path_id": requested_id,
            "tool_item_id": args.tool_item_id,
            "output_drive": output_drive,
            "min_torque_nm": args.min_torque_nm,
            "max_torque_nm": args.max_torque_nm,
            "adapter_description": adapter_description,
            "adapter_max_torque_nm": args.adapter_max_torque_nm,
            "status": args.status,
            "evidence_id": args.evidence_id,
            "notes": args.notes,
        }
        same_id = [
            row for row in store.rows["torque_paths"] if row["path_id"] == requested_id
        ]
        if same_id:
            if len(same_id) != 1 or same_id[0] != requested:
                raise InventoryError(
                    f"torque path id collision with different content: {requested_id}"
                )
            return {"path_id": requested_id, "reused": True}
        same_output = [
            row
            for row in store.rows["torque_paths"]
            if row["tool_item_id"] == args.tool_item_id
            and row["output_drive"].casefold() == output_drive.casefold()
        ]
        if same_output:
            raise InventoryError(
                "tool already has a different path for this output drive: "
                + ", ".join(row["path_id"] for row in same_output)
            )
        store.rows["torque_paths"].append(requested)
        return {"path_id": requested_id, "reused": False}

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"add-torque-path-{args.tool_item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_kit_status(args: argparse.Namespace) -> dict:
    """Return conservative, scope-safe readiness for one or more operational kits."""
    store = read_retrieval_store(args)
    visible_items = {
        row["item_id"]: row
        for row in store.rows["items"]
        if scope_allows(args.scope, row["sensitivity"])
    }
    all_evidence = {row["evidence_id"]: row for row in store.rows["evidence"]}
    visible_evidence = {
        evidence_id
        for evidence_id, evidence in all_evidence.items()
        if scope_allows(args.scope, evidence["sensitivity"])
    }
    kits = [
        row
        for row in store.rows["kits"]
        if row["serves_item_id"] in visible_items
        and row["evidence_id"] in visible_evidence
        and (args.kit_id is None or row["kit_id"] == args.kit_id)
        and (
            args.serves_item_id is None
            or row["serves_item_id"] == args.serves_item_id
        )
    ]
    results = []
    for kit in sorted(kits, key=lambda row: row["kit_id"]):
        all_requirements = [
            row
            for row in store.rows["kit_requirements"]
            if row["kit_id"] == kit["kit_id"]
        ]
        requirements = [
            row
            for row in all_requirements
            if row["evidence_id"] in visible_evidence
            and (row["item_id"] is None or row["item_id"] in visible_items)
        ]
        reviews = [
            row for row in store.rows["kit_reviews"] if row["kit_id"] == kit["kit_id"]
        ]
        latest_review = (
            max(
                reviews,
                key=lambda row: (
                    row["reviewed_on"],
                    row["recorded_at"],
                    row["review_id"],
                ),
            )
            if reviews
            else None
        )
        review_visible = (
            latest_review is not None
            and scope_allows(args.scope, latest_review["sensitivity"])
            and latest_review["evidence_id"] in visible_evidence
        )
        current_keys = sorted(row["requirement_key"] for row in all_requirements)
        review_current = review_visible and strict_json_loads(
            latest_review["requirement_keys_json"]
        ) == current_keys
        requirement_views = []
        current_requirements_valid = True
        for requirement in sorted(
            requirements, key=lambda row: row["requirement_key"]
        ):
            view = dict(requirement)
            assigned = visible_items.get(requirement["item_id"])
            availability = (
                operational_availability(store, assigned, args.scope)
                if assigned is not None
                else None
            )
            view["assigned_item"] = (
                {
                    "availability": availability,
                    "item_id": assigned["item_id"],
                    "ownership_state": assigned["ownership_state"],
                }
                if assigned is not None
                else None
            )
            if requirement["status"] in {"source_present", "exists_unassigned"}:
                latest_sequence = max(
                    (
                        event["sequence"]
                        for event in store.rows["inventory_events"]
                        if event["item_id"] == requirement["item_id"]
                    ),
                    default=None,
                )
                current = (
                    assigned is not None
                    and availability is not None
                    and availability["available"] is True
                    and latest_sequence == requirement["verified_event_sequence"]
                )
                view["current_evidence_state"] = (
                    "current" if current else "stale_or_unavailable"
                )
                current_requirements_valid = current_requirements_valid and current
            else:
                view["current_evidence_state"] = "not_applicable"
            if args.scope != "private":
                view["evidence_id"] = None
                view["notes"] = None
            requirement_views.append(view)
        statuses = {row["status"] for row in requirements}
        if (
            not requirements
            or len(requirements) != len(all_requirements)
            or not current_requirements_valid
            or not review_current
            or latest_review["completeness"] != "complete"
            or "needs_verification" in statuses
        ):
            readiness = "unknown"
        elif statuses <= {"source_present"}:
            readiness = "verified_ready"
        else:
            readiness = "action_required"
        kit_view = dict(kit)
        if args.scope != "private":
            kit_view["evidence_id"] = None
            kit_view["notes"] = None
        results.append(
            {
                "kit": kit_view,
                "readiness": readiness,
                "review": (
                    {
                        **latest_review,
                        "actor": (
                            latest_review["actor"]
                            if args.scope == "private"
                            else None
                        ),
                        "evidence_id": (
                            latest_review["evidence_id"]
                            if args.scope == "private"
                            else None
                        ),
                        "notes": (
                            latest_review["notes"]
                            if args.scope == "private"
                            else None
                        ),
                    }
                    if review_visible
                    else None
                ),
                "requirements": requirement_views,
            }
        )
    return {
        "kits": results,
        "meaning_if_empty": "unknown, not absent",
        "recorded": bool(results),
    }


def torque_path_decision(path: dict, requested_nm: float) -> tuple[str, str]:
    """Make one conservative safety decision from a verifier-safe torque path."""
    minimum = path["min_torque_nm"]
    maximum = path["max_torque_nm"]
    adapter_maximum = path["adapter_max_torque_nm"]
    if adapter_maximum is not None and requested_nm > adapter_maximum:
        return "unsafe", "requested torque exceeds the recorded adapter rating"
    if (
        minimum is not None
        and maximum is not None
        and (requested_nm < minimum or requested_nm > maximum)
    ):
        return "unsafe", "requested torque is outside the recorded tool range"
    if path["status"] == "attachment_only":
        return "conditional", "attachment requires a compatible torque wrench"
    if path["status"] == "needs_verification":
        return "unknown", "path limits need verification"
    if minimum is None or maximum is None:
        return "unknown", "tool range is unknown"
    if path["status"] == "adapter_rating_unknown":
        return "unknown", "adapter rating is unknown"
    return "safe", "requested torque is within every recorded direct-path limit"


def possession_availability(store: Store, item: dict, scope: str) -> dict[str, object]:
    """Say whether a recorded unit is currently in the owner's available custody."""
    state = item["ownership_state"]
    active = [
        row
        for row in store.rows["item_party_relations"]
        if row["item_id"] == item["item_id"]
        and row["role"] == "custodian"
        and row["status"] == "active"
    ]
    hidden = [row for row in active if not scope_allows(scope, row["sensitivity"])]
    visible = [row for row in active if scope_allows(scope, row["sensitivity"])]
    possession = [row for row in visible if row.get("custody_kind") == "possession"]
    external = [
        row
        for row in visible
        if row.get("custody_kind") in {"loan", "storage", "service", "transit"}
    ]
    unknown = [row for row in visible if row.get("custody_kind") == "unknown"]
    available_quantity: float | None = None
    if hidden or unknown:
        available: bool | None = None
        reason = "current custody is hidden or unresolved at this scope"
    elif state == "not_owned" and possession:
        available = True
        reason = "not owned, but explicit current custody is recorded"
    elif state != "confirmed":
        available = False
        reason = f"item is not operationally available in ownership state {state}"
    elif any(row.get("quantity") is None for row in external):
        available = False
        reason = "the item is in active external custody"
    elif external:
        allocated = sum(float(row["quantity"]) for row in external)
        if item.get("quantity") is None:
            available = None
            reason = "external custody is quantified but the item quantity is unknown"
        else:
            available_quantity = max(0.0, float(item["quantity"]) - allocated)
            available = available_quantity > 0
            reason = (
                "some recorded quantity remains in current custody"
                if available
                else "the full recorded quantity is in active external custody"
            )
    else:
        available = state == "confirmed"
        available_quantity = float(item["quantity"]) if item.get("quantity") is not None else None
        reason = "recorded as owned with no active external custody"
    return {
        "available": available,
        "available_quantity": available_quantity,
        "ownership_state": state,
        "reason": reason,
    }


def operational_availability(
    store: Store, item: dict, scope: str
) -> dict[str, object]:
    """Conservatively combine present custody with explicit usable condition."""
    scoped_item = {**item, **scope_visible_item_details(store, item, scope)}
    possession = possession_availability(store, scoped_item, scope)
    normalized_condition = normalized(scoped_item.get("condition") or "")
    usable_conditions = {
        "excellent",
        "functional",
        "good",
        "like new",
        "new",
        "operational",
        "serviceable",
        "working",
    }
    unusable_conditions = {
        "broken",
        "damaged",
        "defective",
        "for parts",
        "needs repair",
        "not working",
        "unserviceable",
        "unusable",
    }
    if normalized_condition in usable_conditions:
        condition_state = "usable"
    elif normalized_condition in unusable_conditions:
        condition_state = "unusable"
    else:
        condition_state = "unknown"
    if possession["available"] is None:
        available: bool | None = None
        reason = str(possession["reason"])
    elif possession["available"] is False:
        available = False
        reason = str(possession["reason"])
    elif condition_state == "usable":
        available = True
        reason = "item is currently possessed and explicitly recorded as usable"
    elif condition_state == "unusable":
        available = False
        reason = "item is currently possessed but explicitly recorded as unusable"
    else:
        available = None
        reason = "item is currently possessed but usable condition is unknown"
    return {
        "available": available,
        "condition": scoped_item.get("condition"),
        "condition_state": condition_state,
        "ownership_state": scoped_item["ownership_state"],
        "possession_available": possession["available"],
        "reason": reason,
    }


def command_torque_check(args: argparse.Namespace) -> dict:
    """Evaluate a requested torque without treating missing limits as safe."""
    store = read_retrieval_store(args)
    visible_items = {
        row["item_id"]: row
        for row in store.rows["items"]
        if scope_allows(args.scope, row["sensitivity"])
    }
    visible_evidence = {
        row["evidence_id"]
        for row in store.rows["evidence"]
        if scope_allows(args.scope, row["sensitivity"])
    }
    paths = [
        row
        for row in store.rows["torque_paths"]
        if row["tool_item_id"] in visible_items
        and row["evidence_id"] in visible_evidence
        and (args.path_id is None or row["path_id"] == args.path_id)
        and (args.tool_item_id is None or row["tool_item_id"] == args.tool_item_id)
    ]
    decisions = []
    for path in sorted(paths, key=lambda row: row["path_id"]):
        physical_outcome, reason = torque_path_decision(path, args.requested_nm)
        availability = operational_availability(
            store, visible_items[path["tool_item_id"]], args.scope
        )
        outcome = physical_outcome if availability["available"] is True else "unknown"
        if availability["available"] is not True:
            reason = str(availability["reason"])
        view = dict(path)
        if args.scope != "private":
            view["evidence_id"] = None
            view["notes"] = None
        decisions.append(
            {
                "availability": availability,
                "outcome": outcome,
                "path": view,
                "physical_outcome": physical_outcome,
                "reason": reason,
            }
        )
    return {
        "decisions": decisions,
        "meaning_if_empty": "unknown, not absent",
        "recorded": bool(decisions),
        "requested_nm": args.requested_nm,
    }


def command_compatibility(args: argparse.Namespace) -> dict:
    if args.first_item_id == args.second_item_id:
        raise InventoryError("compatibility requires two different items")
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
            store = load_verified_store(paths)
            visible = {
                row["item_id"]: row
                for row in store.rows["items"]
                if scope_allows(args.scope, row["sensitivity"])
            }
            if args.first_item_id not in visible or args.second_item_id not in visible:
                raise InventoryError(f"one or both items were not found in {args.scope} scope")
            first = visible[args.first_item_id]
            second = visible[args.second_item_id]
            visible_evidence_ids = {
                row["evidence_id"]
                for row in store.rows["evidence"]
                if scope_allows(args.scope, row["sensitivity"])
            }
            relationships = [
                row
                for row in store.rows["relationships"]
                if {row["subject_item_id"], row["object_item_id"]}
                == {args.first_item_id, args.second_item_id}
                and row["predicate"]
                in {"works_with", "not_compatible", "compatible_if", "requires"}
                and row["evidence_id"] in visible_evidence_ids
            ]
            predicates = {row["predicate"] for row in relationships}
            evidence_ids = [row["evidence_id"] for row in relationships]
            insufficient = [
                row for row in relationships if row["confidence"] not in {"verified", "high"}
            ]
            positive = predicates & {"works_with"}
            negative = predicates & {"not_compatible"}
            conditional = predicates & {"compatible_if", "requires"}
            if insufficient:
                outcome = "unknown"
                reason = "explicit compatibility evidence has insufficient confidence"
            elif sum(bool(group) for group in (positive, negative, conditional)) > 1:
                outcome = "unknown"
                reason = "conflicting explicit compatibility claims"
            elif negative:
                outcome = "incompatible"
                reason = "explicit evidence says the items are not compatible"
            elif positive:
                outcome = "compatible"
                reason = "explicit evidence says the items work together"
            elif conditional:
                outcome = "conditional"
                reason = "explicit evidence records a condition or requirement"
            else:
                claims = [
                    row
                    for row in store.rows["model_interfaces"]
                    if row["model_id"] in {first["model_id"], second["model_id"]}
                    and row["evidence_id"] in visible_evidence_ids
                ]
                interfaces = {row["interface_id"]: row for row in store.rows["interfaces"]}
                first_claims = [row for row in claims if row["model_id"] == first["model_id"]]
                second_claims = [row for row in claims if row["model_id"] == second["model_id"]]
                matches = []
                for first_claim in first_claims:
                    for second_claim in second_claims:
                        first_interface = interfaces[first_claim["interface_id"]]
                        second_interface = interfaces[second_claim["interface_id"]]
                        first_properties = json.loads(first_interface["properties_json"])
                        second_properties = json.loads(second_interface["properties_json"])
                        sufficiently_specific = bool(
                            first_interface.get("standard") or first_interface.get("variant")
                        ) and bool(
                            second_interface.get("standard") or second_interface.get("variant")
                        )
                        same_standard = (
                            sufficiently_specific
                            and all(
                                (first_interface.get(field) or "").casefold()
                            == (second_interface.get(field) or "").casefold()
                            for field in ("family", "standard", "variant")
                            )
                            and first_properties == second_properties
                        )
                        directions = {
                            first_interface["direction"],
                            second_interface["direction"],
                        }
                        complementary_direction = directions == {"plug", "socket"} or (
                            "bidirectional" in directions and "unknown" not in directions
                        )
                        if not same_standard or not complementary_direction:
                            continue
                        roles = {first_claim["role"], second_claim["role"]}
                        if "provides" in roles and roles & {"accepts", "requires"}:
                            matches.append((first_claim, second_claim))
                if matches:
                    outcome = "compatible"
                    reason = "normalized evidence-bearing interfaces match"
                    evidence_ids.extend(
                        evidence_id
                        for pair in matches
                        for evidence_id in (pair[0]["evidence_id"], pair[1]["evidence_id"])
                    )
                else:
                    outcome = "unknown"
                    reason = (
                        "no sufficient normalized compatibility evidence; legacy text "
                        "or similar purpose does not prove interchangeability"
                    )
            first_availability = operational_availability(store, first, args.scope)
            second_availability = operational_availability(store, second, args.scope)
            return {
                "availability": {
                    "first": first_availability,
                    "second": second_availability,
                },
                "first_item_id": args.first_item_id,
                "second_item_id": args.second_item_id,
                "outcome": outcome,
                "operational_outcome": (
                    outcome
                    if first_availability["available"] is True
                    and second_availability["available"] is True
                    else "unknown"
                ),
                "reason": reason,
                "evidence_ids": sorted(set(evidence_ids)) if args.scope == "private" else [],
            }
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error


def capture_staging_path(runtime_dir: Path, capture_session_id: str) -> Path:
    if not re.fullmatch(r"capture-[0-9a-f-]{36}", capture_session_id):
        raise InventoryError("invalid capture session id")
    return checked_managed_path(
        runtime_dir, Path("capture-staging") / capture_session_id, "runtime"
    )


def retire_applied_capture_staging(
    runtime_dir: Path,
    capture_session_id: str,
    *,
    artifact_sha256: str,
    review_sha256: str,
    artifact_json: str,
    review_json: str,
) -> str:
    """Retire only exact canonical staging bytes, including after a partial cleanup."""
    staging = capture_staging_path(runtime_dir, capture_session_id)
    parent = staging.parent
    retired = parent / f".applied-{capture_session_id}"
    try:
        if parent.is_symlink():
            return "unsafe_retained"
        artifact_value = strict_json_value(
            artifact_json, "bound capture cleanup artifact"
        )
        review_value = strict_json_value(review_json, "bound capture cleanup review")
        artifact = validate_capture_artifact(
            artifact_value, expected_session_id=capture_session_id
        )
        review = validate_capture_review(
            review_value,
            artifact=artifact,
            expected_session_id=capture_session_id,
        )
        artifact_payload = artifact_json.encode("utf-8")
        review_payload = review_json.encode("utf-8")
        if (
            artifact_payload != canonical_artifact_bytes(artifact)
            or review_payload != canonical_review_bytes(review)
            or hashlib.sha256(artifact_payload).hexdigest() != artifact_sha256
            or hashlib.sha256(review_payload).hexdigest() != review_sha256
        ):
            return "unsafe_retained"
        source = artifact["source"]
        contract: dict[str, tuple[str, int, int]] = {
            "overview": (
                source["sha256"],
                source["byte_length"],
                MAX_CAPTURE_SOURCE_BYTES,
            ),
            "artifact.json": (
                artifact_sha256,
                len(artifact_payload),
                MAX_CAPTURE_METADATA_BYTES,
            ),
            "review.json": (
                review_sha256,
                len(review_payload),
                MAX_CAPTURE_METADATA_BYTES,
            ),
        }
        request_payload = f"{artifact['request_digest']}\n".encode("ascii")
        contract[CAPTURE_REQUEST_DIGEST_FILE] = (
            hashlib.sha256(request_payload).hexdigest(),
            len(request_payload),
            len(request_payload),
        )
        for crop in artifact["crops"]:
            name = crop["file"]
            if name in contract:
                return "unsafe_retained"
            contract[name] = (
                crop["sha256"],
                crop["byte_length"],
                MAX_CAPTURE_CROP_BYTES,
            )
        if path_entry_exists(retired):
            if retired.is_symlink() or not retired.is_dir():
                return "unsafe_retained"
            if path_entry_exists(staging):
                return "unsafe_retained"
            candidate = retired
            allow_partial = True
        elif path_entry_exists(staging):
            if staging.is_symlink() or not staging.is_dir():
                return "unsafe_retained"
            candidate = staging
            allow_partial = False
        else:
            return "already_absent"
        actual = {entry.name for entry in candidate.iterdir()}
        if (allow_partial and not actual.issubset(contract)) or (
            not allow_partial and actual != set(contract)
        ):
            return "unsafe_retained"
        for name in actual:
            digest, size, maximum = contract[name]
            _validated_capture_file(
                candidate / name,
                expected_digest=digest,
                expected_size=size,
                maximum_bytes=maximum,
                label=f"applied capture cleanup {name}",
            )
        if candidate == staging:
            os.rename(staging, retired)
            fsync_directory(parent)
        remaining = {entry.name for entry in retired.iterdir()}
        if not remaining.issubset(contract):
            return "unsafe_retained"
        for index, name in enumerate(sorted(remaining), start=1):
            digest, size, maximum = contract[name]
            _validated_capture_file(
                retired / name,
                expected_digest=digest,
                expected_size=size,
                maximum_bytes=maximum,
                label=f"applied capture cleanup {name}",
            )
            (retired / name).unlink()
            fsync_directory(retired)
            if (
                index == 1
                and os.environ.get("PROPERTY_INVENTORY_FAIL_CAPTURE_CLEANUP")
                == "after-first-file"
            ):
                os._exit(93)
        retired.rmdir()
        fsync_directory(parent)
        return "removed"
    except (CaptureProvenanceError, InventoryError):
        return "unsafe_retained"
    except OSError:
        return "deferred"


def _read_capture_artifact_from_staging(
    staging: Path, capture_session_id: str
) -> dict:
    artifact_path = staging / "artifact.json"
    if (
        staging.is_symlink()
        or not staging.is_dir()
        or artifact_path.is_symlink()
        or not artifact_path.is_file()
    ):
        raise InventoryError("capture staging artifact is missing or unsafe")
    try:
        artifact = strict_json_value(
            read_bounded_regular_input(
                artifact_path,
                maximum_bytes=MAX_CAPTURE_METADATA_BYTES,
                label="capture artifact",
            ).decode("utf-8"),
            "capture artifact",
        )
    except (OSError, UnicodeDecodeError) as error:
        raise InventoryError(f"cannot read capture artifact: {error}") from error
    try:
        artifact = validate_capture_artifact(
            artifact, expected_session_id=capture_session_id
        )
    except CaptureProvenanceError as error:
        raise InventoryError(str(error)) from error
    return artifact


def read_capture_artifact(runtime_dir: Path, capture_session_id: str) -> tuple[Path, dict]:
    staging = capture_staging_path(runtime_dir, capture_session_id)
    artifact = _read_capture_artifact_from_staging(staging, capture_session_id)
    return staging, artifact


def _capture_rectangle(value: object) -> bool:
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


def _validated_capture_file(
    path: Path,
    *,
    expected_digest: str,
    expected_size: int,
    maximum_bytes: int,
    label: str,
) -> bytes:
    if (
        not isinstance(expected_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        or isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 1
        or expected_size > maximum_bytes
    ):
        raise InventoryError(f"{label} manifest is malformed")
    payload = read_bounded_regular_input(
        path, maximum_bytes=maximum_bytes, label=label
    )
    if len(payload) != expected_size or hashlib.sha256(payload).hexdigest() != expected_digest:
        raise InventoryError(f"{label} bytes disagree with the capture manifest")
    return payload


def validate_capture_staging(
    staging: Path,
    artifact: dict,
    *,
    review_present: bool,
) -> dict[str, tuple[str, int, int]]:
    """Validate and allowlist every byte in one capture staging directory."""
    source = artifact.get("source")
    crops = artifact.get("crops")
    source_keys = {
        "byte_length",
        "content_type",
        "coordinate_space",
        "file",
        "image_height",
        "image_width",
        "original_name",
        "sha256",
        "source_id",
    }
    crop_keys = {
        "byte_length",
        "content_type",
        "crop_id",
        "file",
        "region",
        "segment_id",
        "sha256",
        "source_id",
        "source_sha256",
    }
    if (
        not isinstance(source, dict)
        or set(source) != source_keys
        or source.get("file") != "overview"
        or source.get("coordinate_space") != "exif_transposed_pixels"
        or not isinstance(source.get("content_type"), str)
        or not source["content_type"].startswith("image/")
        or not isinstance(crops, list)
        or len(crops) > MAX_CAPTURE_SEGMENTS
        or artifact.get("segmentation_source") not in {"supplied", "adapter"}
        or not isinstance(artifact.get("segments"), list)
        or len(artifact["segments"]) != len(crops)
    ):
        raise InventoryError("capture media manifest is malformed")
    contract: dict[str, tuple[str, int, int]] = {
        "overview": (
            source["sha256"],
            source["byte_length"],
            MAX_CAPTURE_SOURCE_BYTES,
        )
    }
    total_crop_bytes = 0
    segment_ids: set[str] = set()
    for index, crop in enumerate(crops):
        segment = artifact["segments"][index]
        if (
            not isinstance(crop, dict)
            or set(crop) != crop_keys
            or crop.get("content_type") != "image/png"
            or not re.fullmatch(r"crop-[0-9a-f]{64}", str(crop.get("crop_id", "")))
            or crop.get("file") != f"{crop.get('crop_id')}.png"
            or crop.get("source_id") != source.get("source_id")
            or crop.get("source_sha256") != source.get("sha256")
            or not _capture_rectangle(crop.get("region"))
            or not isinstance(segment, dict)
            or set(segment) != {"segment_id", "region"}
            or not isinstance(segment.get("segment_id"), str)
            or not segment["segment_id"].strip()
            or segment["segment_id"] in segment_ids
            or segment["segment_id"] != crop.get("segment_id")
            or segment.get("region") != crop.get("region")
        ):
            raise InventoryError("capture crop manifest is malformed")
        segment_ids.add(segment["segment_id"])
        name = crop["file"]
        if name in contract:
            raise InventoryError("capture crop file names must be unique")
        size = crop.get("byte_length")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise InventoryError("capture crop manifest is malformed")
        total_crop_bytes += size
        contract[name] = (crop["sha256"], size, MAX_CAPTURE_CROP_BYTES)
    if total_crop_bytes > MAX_CAPTURE_TOTAL_CROP_BYTES:
        raise InventoryError("capture crops exceed the total byte limit")

    artifact_path = staging / "artifact.json"
    artifact_payload = read_bounded_regular_input(
        artifact_path,
        maximum_bytes=MAX_CAPTURE_METADATA_BYTES,
        label="capture artifact",
    )
    contract["artifact.json"] = (
        hashlib.sha256(artifact_payload).hexdigest(),
        len(artifact_payload),
        MAX_CAPTURE_METADATA_BYTES,
    )
    request_payload = f"{artifact['request_digest']}\n".encode("ascii")
    contract[CAPTURE_REQUEST_DIGEST_FILE] = (
        hashlib.sha256(request_payload).hexdigest(),
        len(request_payload),
        len(request_payload),
    )
    if review_present:
        review_path = staging / "review.json"
        review_payload = read_bounded_regular_input(
            review_path,
            maximum_bytes=MAX_CAPTURE_METADATA_BYTES,
            label="capture review",
        )
        contract["review.json"] = (
            hashlib.sha256(review_payload).hexdigest(),
            len(review_payload),
            MAX_CAPTURE_METADATA_BYTES,
        )
    actual: set[str] = set()
    for index, entry in enumerate(staging.iterdir()):
        if index >= len(contract) + 1:
            raise InventoryError("capture staging contains unexpected entries")
        actual.add(entry.name)
    if actual != set(contract):
        raise InventoryError("capture staging contains missing or unexpected entries")
    for name, (digest, size, maximum) in contract.items():
        _validated_capture_file(
            staging / name,
            expected_digest=digest,
            expected_size=size,
            maximum_bytes=maximum,
            label=f"capture staging {name}",
        )
    return contract


def copy_validated_capture_staging(
    source: Path, destination: Path, artifact: dict
) -> None:
    contract = validate_capture_staging(source, artifact, review_present=True)
    destination.mkdir(mode=0o700)
    try:
        for name, (digest, size, maximum) in contract.items():
            payload = _validated_capture_file(
                source / name,
                expected_digest=digest,
                expected_size=size,
                maximum_bytes=maximum,
                label=f"capture staging {name}",
            )
            target = destination / name
            descriptor = os.open(
                target,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        fsync_directory(destination)
        validate_capture_staging(destination, artifact, review_present=True)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def capture_review_path(runtime_dir: Path, capture_session_id: str) -> Path:
    return checked_managed_path(
        capture_staging_path(runtime_dir, capture_session_id),
        Path("review.json"),
        "capture staging",
    )


def read_capture_review(runtime_dir: Path, capture_session_id: str) -> tuple[Path, dict]:
    path = capture_review_path(runtime_dir, capture_session_id)
    if path.is_symlink() or not path.is_file():
        raise InventoryError("capture review is missing or unsafe")
    try:
        review = strict_json_value(
            read_bounded_regular_input(
                path,
                maximum_bytes=MAX_CAPTURE_METADATA_BYTES,
                label="capture review",
            ).decode("utf-8"),
            "capture review",
        )
    except UnicodeDecodeError as error:
        raise InventoryError("capture review is not UTF-8") from error
    if (
        not isinstance(review, dict)
        or set(review)
        != {
            "artifact_sha256",
            "base_digest",
            "capture_session_id",
            "created_at",
            "decisions",
            "format",
            "links",
            "manual_observations",
            "proposal_id",
        }
        or review.get("format") != 1
        or review.get("capture_session_id") != capture_session_id
        or not isinstance(review.get("links"), dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in review["links"].items()
        )
        or not isinstance(review.get("manual_observations"), dict)
        or not isinstance(review.get("decisions"), list)
        or not isinstance(review.get("artifact_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", review["artifact_sha256"])
        or not isinstance(review.get("base_digest"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", review["base_digest"])
        or not isinstance(review.get("proposal_id"), str)
        or not re.fullmatch(r"proposal-[0-9a-f-]{36}", review["proposal_id"])
        or not isinstance(review.get("created_at"), str)
        or not review["created_at"]
    ):
        raise InventoryError("capture review is malformed")
    try:
        _, artifact = read_capture_artifact(runtime_dir, capture_session_id)
        validate_capture_review(
            review,
            artifact=artifact,
            expected_session_id=capture_session_id,
        )
    except CaptureProvenanceError as error:
        raise InventoryError(str(error)) from error
    return path, review


def capture_proposal_from_review(review: dict, review_digest: str) -> dict:
    return {
        "base_digest": review["base_digest"],
        "capture": {
            "artifact_sha256": review["artifact_sha256"],
            "capture_session_id": review["capture_session_id"],
            "review_sha256": review_digest,
        },
        "created_at": review["created_at"],
        "operations": [
            [
                "capture-commit",
                "--capture-session-id",
                review["capture_session_id"],
            ]
        ],
        "proposal_id": review["proposal_id"],
        "schema_version": SCHEMA_VERSION,
        "status": "prepared",
    }


def command_capture_prepare(args: argparse.Namespace) -> dict:
    if args.scope != "private":
        raise InventoryError("capture preparation requires private scope")
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
            store = load_verified_store(paths)
            if args.evidence_id is not None:
                evidence = store.get("evidence", args.evidence_id)
                if (
                    evidence["evidence_type"] == "physical_check"
                    or evidence["claim_strength"] == "explicit_current"
                ):
                    raise InventoryError(
                        "passive capture preparation cannot reuse current-possession "
                        "or physical-check evidence"
                    )
                if (
                    SENSITIVITY_RANK[args.sensitivity]
                    > SENSITIVITY_RANK[evidence["sensitivity"]]
                ):
                    raise InventoryError(
                        "existing evidence sensitivity is lower than the capture; "
                        "use source-ref to create separate evidence"
                    )
                if (
                    args.evidence_type != evidence["evidence_type"]
                    or args.captured_on != evidence["captured_on"]
                ):
                    raise InventoryError(
                        "existing capture evidence type and date must match the canonical evidence; "
                        "use source-ref to create separate evidence"
                    )
            try:
                adapter_registry = getattr(args, "capture_adapter_registry", None)
                if (
                    adapter_registry is None
                    and args.adapter_name is not None
                    and args.capture_adapters_config is not None
                ):
                    adapter_registry = load_adapter_registry(
                        args.capture_adapters_config
                    )
                artifact = prepare_capture(
                    runtime_dir=args.runtime_dir,
                    overview=args.overview,
                    captured_on=args.captured_on,
                    segments_value=args.segments,
                    evidence_id=args.evidence_id,
                    source_ref=args.source_ref,
                    evidence_type=args.evidence_type,
                    sensitivity=args.sensitivity,
                    adapter_name=args.adapter_name,
                    adapter_registry=adapter_registry,
                    timeout_seconds=args.adapter_timeout,
                    store=store,
                    base_digest=canonical_store_digest(paths["store"]),
                    visible=lambda sensitivity: (
                        SENSITIVITY_RANK.get(sensitivity, -1) <= SCOPE_MAX_SENSITIVITY[args.scope]
                    ),
                    links=None,
                )
            except (CaptureError, CaptureServiceError) as error:
                raise InventoryError(f"capture preparation failed: {error}") from error
            staging = capture_staging_path(
                args.runtime_dir, artifact["capture_session_id"]
            )
            persisted_staging, persisted_artifact = read_capture_artifact(
                args.runtime_dir, artifact["capture_session_id"]
            )
            if persisted_staging != staging or persisted_artifact != artifact:
                raise InventoryError(
                    "capture preparation disagrees with its published artifact"
                )
            review_path = capture_review_path(
                args.runtime_dir, artifact["capture_session_id"]
            )
            review_exists = path_entry_exists(review_path)
            validate_capture_staging(
                staging,
                artifact,
                review_present=review_exists,
            )
            artifact_digest = file_digest(staging / "artifact.json")
            result = {
                "status": "awaiting_review",
                "capture": {
                    "capture_session_id": artifact["capture_session_id"],
                    "artifact_sha256": artifact_digest,
                    "observations": artifact["observations"],
                    "duplicate_candidates": artifact["duplicate_candidates"],
                    "duplicate_candidate_summaries": artifact[
                        "duplicate_candidate_summaries"
                    ],
                    "segments": artifact["segments"],
                    "segmentation_source": artifact["segmentation_source"],
                },
            }
            if review_exists:
                _, review = read_capture_review(
                    args.runtime_dir, artifact["capture_session_id"]
                )
                if (
                    review["artifact_sha256"] != artifact_digest
                    or review["base_digest"] != artifact["base_digest"]
                ):
                    raise InventoryError(
                        "existing capture review disagrees with deterministic preparation"
                    )
                proposal = capture_proposal_from_review(
                    review, file_digest(review_path)
                )
                destination = proposal_path(
                    args.runtime_dir, proposal["proposal_id"]
                )
                ensure_private_directory(destination.parent)
                if path_entry_exists(destination):
                    existing = strict_json_value(
                        read_bounded_regular_input(
                            destination,
                            maximum_bytes=MAX_CAPTURE_METADATA_BYTES,
                            label="capture proposal",
                        ).decode("utf-8"),
                        "capture proposal",
                    )
                    if existing != proposal:
                        raise InventoryError(
                            "existing capture proposal disagrees with sealed review"
                        )
                else:
                    write_json(destination, proposal)
                result["status"] = "prepared"
                result["proposal"] = proposal
            return result
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error


def _capture_review_physical(value: object) -> dict:
    required = {
        "actor",
        "checked_on",
        "condition",
        "container_id",
        "location_id",
        "notes",
        "quantity",
        "serial_or_lot",
        "unit",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise InventoryError("capture decision physical details are malformed")
    if (
        not isinstance(value["actor"], str)
        or not value["actor"].strip()
        or not isinstance(value["checked_on"], str)
        or not value["checked_on"].strip()
        or not isinstance(value["location_id"], str)
        or not value["location_id"].strip()
        or (value["container_id"] is not None and not isinstance(value["container_id"], str))
        or (value["condition"] is not None and not isinstance(value["condition"], str))
        or (value["serial_or_lot"] is not None and not isinstance(value["serial_or_lot"], str))
        or (value["notes"] is not None and not isinstance(value["notes"], str))
        or (value["unit"] is not None and not isinstance(value["unit"], str))
        or (
            value["quantity"] is not None
            and (
                isinstance(value["quantity"], bool)
                or not isinstance(value["quantity"], (int, float))
                or not math.isfinite(float(value["quantity"]))
                or value["quantity"] <= 0
            )
        )
    ):
        raise InventoryError("capture decision physical details are malformed")
    try:
        checked_on = date.fromisoformat(value["checked_on"])
    except ValueError as error:
        raise InventoryError("capture decision checked_on is malformed") from error
    if checked_on.isoformat() != value["checked_on"]:
        raise InventoryError("capture decision checked_on is malformed")
    return value


def _validate_capture_discovery(value: object, store: Store) -> dict:
    allowed = {
        "brand", "category", "existing_model_id", "identifiers", "model", "name",
        "new_model", "new_unit", "reference_url", "sensitivity", "specs",
    }
    if not isinstance(value, dict) or set(value) - allowed or not {
        "category", "name", "new_unit"
    }.issubset(value):
        raise InventoryError("capture discovery details are malformed")
    if (
        not isinstance(value["name"], str)
        or not value["name"].strip()
        or not isinstance(value["category"], str)
        or not value["category"].strip()
        or value["new_unit"] is not True
        or (value.get("new_model", False) is not True and value.get("new_model", False) is not False)
        or (value.get("existing_model_id") is not None and not isinstance(value.get("existing_model_id"), str))
        or (value.get("existing_model_id") is not None and value.get("new_model", False))
        or value.get("sensitivity", "personal") not in {"low", "personal", "high"}
        or not isinstance(value.get("specs", {}), dict)
        or not isinstance(value.get("identifiers", {}), dict)
    ):
        raise InventoryError("capture discovery details are malformed")
    if value.get("existing_model_id") is not None:
        store.get("models", value["existing_model_id"])
    return value


def _validate_capture_review_decisions(store: Store, decisions: object) -> None:
    if not isinstance(decisions, list):
        raise InventoryError("capture decisions must be an array")
    for decision in decisions:
        if not isinstance(decision, dict):
            raise InventoryError("capture decisions are malformed")
        physical = _capture_review_physical(decision.get("physical"))
        stable_location_and_container(
            store, physical["location_id"], physical["container_id"]
        )
        if decision.get("item_id") is not None:
            store.get("items", decision["item_id"])
        if decision.get("discovery") is not None:
            _validate_capture_discovery(decision["discovery"], store)


def command_capture_review(args: argparse.Namespace) -> dict:
    """Seal explicit links into one immutable capture proposal without rerunning adapters."""
    if args.scope != "private":
        raise InventoryError("capture review requires private scope")
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
            staging, artifact = read_capture_artifact(
                args.runtime_dir, args.capture_session_id
            )
            artifact_digest = file_digest(staging / "artifact.json")
            if (
                not isinstance(args.artifact_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", args.artifact_sha256)
                or args.artifact_sha256 != artifact_digest
            ):
                raise InventoryError(
                    "capture artifact changed after preparation; prepare it again"
                )
            store = load_verified_store(paths)
            current_store_digest = canonical_store_digest(paths["store"])
            if artifact.get("base_digest") != current_store_digest:
                raise InventoryError(
                    "canonical inventory changed after capture preparation; prepare it again"
                )
            if any(
                row["capture_session_id"] == args.capture_session_id
                for row in store.rows["capture_sessions"]
            ):
                raise InventoryError("capture session is already applied")
            links = args.links
            if not isinstance(links, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in links.items()
            ):
                raise InventoryError("capture links must map observation IDs to item IDs")
            observation_ids = {
                f"observation-{index}"
                for index in range(1, len(artifact["observations"]) + 1)
            }
            if not set(links).issubset(observation_ids):
                raise InventoryError("capture links name an unknown observation")
            visible_item_ids = {
                row["item_id"]
                for row in store.rows["items"]
                if scope_allows(args.scope, row["sensitivity"])
            }
            if any(item_id not in visible_item_ids for item_id in links.values()):
                raise InventoryError("capture links name an item outside the selected scope")
            _validate_capture_review_decisions(store, args.decisions)
            effective_sensitivity = artifact["sensitivity"]
            if artifact.get("evidence_id") is not None:
                evidence = store.get("evidence", artifact["evidence_id"])
                if evidence["claim_strength"] != "research_only":
                    supported_items = {
                        row["item_id"]
                        for row in store.rows["item_evidence"]
                        if row["evidence_id"] == evidence["evidence_id"]
                    }
                    if any(
                        item_id not in supported_items for item_id in links.values()
                    ):
                        raise InventoryError(
                            "capture cannot extend state-bearing evidence to another item; "
                            "use an explicit lifecycle or physical verification workflow"
                        )
                if (
                    SENSITIVITY_RANK[evidence["sensitivity"]]
                    > SENSITIVITY_RANK[effective_sensitivity]
                ):
                    effective_sensitivity = evidence["sensitivity"]
            items_by_id = {row["item_id"]: row for row in store.rows["items"]}
            if any(
                SENSITIVITY_RANK[items_by_id[item_id]["sensitivity"]]
                > SENSITIVITY_RANK[effective_sensitivity]
                for item_id in links.values()
            ):
                raise InventoryError(
                    "capture link sensitivity exceeds capture evidence"
                )
            validate_capture_media_collisions(store, artifact)
            review_path = capture_review_path(
                args.runtime_dir, args.capture_session_id
            )
            review_exists = path_entry_exists(review_path)
            validate_capture_staging(
                staging,
                artifact,
                review_present=review_exists,
            )
            if review_exists:
                _, review = read_capture_review(
                    args.runtime_dir, args.capture_session_id
                )
                if (
                    review["links"] != links
                    or review["artifact_sha256"] != artifact_digest
                    or review["manual_observations"] != args.manual_observations
                    or review["decisions"] != args.decisions
                ):
                    raise InventoryError(
                        "capture review is already sealed with different content"
                    )
            else:
                review = {
                    "artifact_sha256": artifact_digest,
                    "base_digest": artifact["base_digest"],
                    "capture_session_id": args.capture_session_id,
                    "created_at": datetime.now().astimezone().isoformat(),
                    "format": 1,
                    "links": links,
                    "manual_observations": args.manual_observations,
                    "decisions": args.decisions,
                    "proposal_id": f"proposal-{uuid.uuid4()}",
                }
                try:
                    validate_capture_review(
                        review,
                        artifact=artifact,
                        expected_session_id=args.capture_session_id,
                    )
                except CaptureProvenanceError as error:
                    raise InventoryError(str(error)) from error
                write_json(review_path, review)
                validate_capture_staging(staging, artifact, review_present=True)
            proposal = capture_proposal_from_review(
                review, file_digest(review_path)
            )
            destination = proposal_path(args.runtime_dir, proposal["proposal_id"])
            ensure_private_directory(destination.parent)
            if path_entry_exists(destination):
                existing = strict_json_value(
                    read_bounded_regular_input(
                        destination,
                        maximum_bytes=MAX_CAPTURE_METADATA_BYTES,
                        label="capture proposal",
                    ).decode("utf-8"),
                    "capture proposal",
                )
                if existing != proposal:
                    raise InventoryError(
                        "capture proposal disagrees with the sealed review"
                    )
            else:
                write_json(destination, proposal)
            return {
                "status": "prepared",
                "proposal": proposal,
                "capture": {
                    "capture_session_id": args.capture_session_id,
                    "links": links,
                },
            }
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error


def _strict_capture_regions(value: object) -> list[dict] | None:
    def rectangle(candidate: object) -> bool:
        return (
            isinstance(candidate, dict)
            and set(candidate) == {"x", "y", "width", "height"}
            and all(
                isinstance(candidate[field], int)
                and not isinstance(candidate[field], bool)
                and candidate[field] >= (1 if field in {"width", "height"} else 0)
                for field in ("x", "y", "width", "height")
            )
        )

    if rectangle(value):
        return [value]
    if (
        isinstance(value, dict)
        and set(value) == {"regions"}
        and isinstance(value["regions"], list)
        and value["regions"]
        and all(rectangle(region) for region in value["regions"])
    ):
        return value["regions"]
    return None


def validate_capture_media_collisions(store: Store, artifact: dict) -> None:
    """Reject pre-existing generic annotations that capture cannot merge losslessly."""
    evidence_id = artifact.get("evidence_id")
    if evidence_id is None:
        return
    assets_by_digest = {row["sha256"]: row for row in store.rows["media_assets"]}
    for entry in [artifact["source"], *artifact["crops"]]:
        asset = assets_by_digest.get(entry.get("sha256"))
        if asset is None:
            continue
        role = "source" if entry is artifact["source"] else "crop"
        matches = [
            row
            for row in store.rows["evidence_assets"]
            if row.get("asset_id") == asset["asset_id"]
            and row.get("evidence_id") == evidence_id
            and row.get("role") == role
        ]
        if not matches:
            continue
        region_json = matches[0].get("region_json")
        if role == "source" and region_json is None:
            continue
        if role == "crop" and isinstance(region_json, str):
            try:
                if _strict_capture_regions(json.loads(region_json)) is not None:
                    continue
            except json.JSONDecodeError:
                pass
        raise InventoryError(
            "existing evidence asset annotation cannot be merged with capture; "
            "use source-ref to create separate evidence"
        )


def _capture_media(
    store: Store,
    artifact: dict,
    staging: Path,
    media_root: Path,
    *,
    minimum_sensitivity: str | None = None,
) -> list[str]:
    evidence_id = artifact.get("evidence_id")
    sensitivity = artifact["sensitivity"]
    if minimum_sensitivity is not None:
        sensitivity = max(
            (sensitivity, minimum_sensitivity), key=SENSITIVITY_RANK.__getitem__
        )
    sources = [artifact["source"], *artifact["crops"]]
    existing_assets = {row["sha256"]: row for row in store.rows["media_assets"]}
    referenced_asset_sensitivities = [
        existing_assets[entry["sha256"]]["sensitivity"]
        for entry in sources
        if entry.get("sha256") in existing_assets
    ]
    sensitivity = max(
        [sensitivity, *referenced_asset_sensitivities],
        key=SENSITIVITY_RANK.__getitem__,
    )
    if evidence_id is None:
        evidence_id = store.allocate(
            "evidence", f"ev-capture-{artifact['capture_session_id'][8:20]}"
        )
        store.rows["evidence"].append(
            {
                "captured_on": artifact["captured_on"],
                "claim_strength": "research_only",
                "evidence_id": evidence_id,
                "evidence_type": artifact["evidence_type"],
                "notes": "Overview capture; it does not assert possession, condition, or location.",
                "sensitivity": sensitivity,
                "source_ref": artifact["source_ref"],
            }
        )
    else:
        evidence = store.get("evidence", evidence_id)
        if SENSITIVITY_RANK[evidence["sensitivity"]] > SENSITIVITY_RANK[sensitivity]:
            sensitivity = evidence["sensitivity"]
    added: list[str] = []
    for entry in sources:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            raise InventoryError("capture artifact media entry is malformed")
        source = staging / entry["file"]
        digest = entry.get("sha256")
        if (
            source.is_symlink()
            or not source.is_file()
            or not isinstance(digest, str)
            or file_digest(source) != digest
        ):
            raise InventoryError("capture staging media digest disagrees with artifact")
        installed, installed_new = install_media(source, media_root, digest)
        if installed_new:
            added.append(digest)
        matches = [row for row in store.rows["media_assets"] if row["sha256"] == digest]
        if matches:
            asset = matches[0]
            asset_id = asset["asset_id"]
            if asset["byte_size"] != source.stat().st_size:
                raise InventoryError("capture media asset size disagrees with digest")
            if SENSITIVITY_RANK[sensitivity] > SENSITIVITY_RANK[asset["sensitivity"]]:
                asset["sensitivity"] = sensitivity
        else:
            asset_id = store.allocate("media_assets", f"asset-{digest[:24]}")
            store.rows["media_assets"].append(
                {
                    "asset_id": asset_id,
                    "byte_size": source.stat().st_size,
                    "captured_on": artifact["captured_on"],
                    "media_type": entry.get("content_type", artifact["source"].get("content_type")),
                    "original_name": entry.get("original_name", entry["file"]),
                    "sensitivity": sensitivity,
                    "sha256": digest,
                    "uri": f"media://sha256/{digest}",
                }
            )
        role = "source" if entry is artifact["source"] else "crop"
        link = {
            "asset_id": asset_id,
            "evidence_id": evidence_id,
            "role": role,
            "region_json": _capture_region_json(entry.get("region")),
        }
        matching_links = [
            row
            for row in store.rows["evidence_assets"]
            if row.get("asset_id") == asset_id
            and row.get("evidence_id") == evidence_id
            and row.get("role") == role
        ]
        if not matching_links:
            store.rows["evidence_assets"].append(link)
        elif role == "crop":
            existing = matching_links[0]
            try:
                current = json.loads(existing["region_json"])
                incoming = json.loads(link["region_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise InventoryError("capture crop region link is malformed") from error
            regions = _strict_capture_regions(current)
            if regions is None:
                raise InventoryError("capture crop region link is malformed")
            if incoming not in regions:
                regions.append(incoming)
                regions.sort(
                    key=lambda value: json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                existing["region_json"] = json.dumps(
                    {"regions": regions},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
        elif matching_links[0] != link:
            raise InventoryError("capture source evidence link conflicts with existing data")
    artifact["_resolved_evidence_id"] = evidence_id
    artifact["_resolved_sensitivity"] = sensitivity
    return added


def _capture_region_json(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InventoryError("capture crop region is malformed")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _capture_crop_asset_ids(store: Store, artifact: dict) -> dict[str, str]:
    assets_by_digest = {row["sha256"]: row["asset_id"] for row in store.rows["media_assets"]}
    result: dict[str, str] = {}
    for crop in artifact["crops"]:
        asset_id = assets_by_digest.get(crop["sha256"])
        if asset_id is None:
            raise InventoryError("capture crop media was not materialized")
        result[crop["crop_id"]] = asset_id
    return result


def _capture_decisions_sensitivity(store: Store, decisions: list[dict]) -> str:
    """Return the privacy level required by every sealed physical decision."""
    sensitivities = ["low"]
    for decision in decisions:
        physical = _capture_review_physical(decision["physical"])
        sensitivities.append(
            location_context_sensitivity(
                store, physical["location_id"], physical["container_id"]
            )
        )
        if decision["item_id"] is not None:
            sensitivities.append(store.get("items", decision["item_id"])["sensitivity"])
        else:
            sensitivities.append(
                _validate_capture_discovery(decision["discovery"], store).get(
                    "sensitivity", "personal"
                )
            )
    return max(sensitivities, key=SENSITIVITY_RANK.__getitem__)


def _attach_capture_crop_evidence(
    store: Store, *, evidence_id: str, crop: dict, asset_id: str
) -> None:
    link = {
        "asset_id": asset_id,
        "evidence_id": evidence_id,
        "role": "crop",
        "region_json": _capture_region_json(crop["region"]),
    }
    matching = [
        row
        for row in store.rows["evidence_assets"]
        if row["evidence_id"] == evidence_id
        and row["asset_id"] == asset_id
        and row["role"] == "crop"
    ]
    if not matching:
        store.rows["evidence_assets"].append(link)
    elif len(matching) != 1 or matching[0] != link:
        raise InventoryError("physical capture evidence disagrees with its selected crop")


def _capture_decision_item(
    store: Store, *, decision: dict, artifact: dict, crop: dict, crop_asset_id: str
) -> tuple[str, str, bool]:
    """Apply one sealed crop decision and return item, evidence, and creation state."""
    physical = _capture_review_physical(decision["physical"])
    stable_location_and_container(store, physical["location_id"], physical["container_id"])
    source_ref = artifact.get("source_ref") or f"capture:{artifact['capture_session_id']}"
    notes = physical["notes"] or f"Physical verification from crop {crop['crop_id']}."
    if decision["item_id"] is not None:
        item = store.get("items", decision["item_id"])
        if item["ownership_state"] in {"disposed", "refunded", "not_owned"}:
            raise InventoryError(
                "capture reconciliation cannot contradict terminal ownership; restore explicitly"
            )
        item["location_id"] = physical["location_id"]
        item["container_id"] = physical["container_id"]
        evidence_id = add_evidence(
            store,
            item_id=item["item_id"],
            base=f"capture-{crop['crop_id'][5:29]}-{item['item_id'][4:28]}-{physical['checked_on']}",
            evidence_type="physical_check",
            source_ref=source_ref,
            captured_on=physical["checked_on"],
            claim_strength="explicit_current",
            notes=notes,
            minimum_sensitivity=location_context_sensitivity(
                store, item["location_id"], item["container_id"]
            ),
        )
        _attach_capture_crop_evidence(
            store, evidence_id=evidence_id, crop=crop, asset_id=crop_asset_id
        )
        if physical["quantity"] is not None:
            unit = physical["unit"] or item.get("unit")
            if not isinstance(unit, str) or not unit.strip():
                raise InventoryError(
                    "capture quantity requires a known unit for the existing item"
                )
            apply_quantity_change(
                store,
                item=item,
                quantity=physical["quantity"],
                unit=unit,
                occurred_on=physical["checked_on"],
                actor=physical["actor"],
                evidence_id=evidence_id,
                notes=notes,
            )
        detail_changes = {
            field: physical[field]
            for field in ("condition", "serial_or_lot")
            if physical[field] is not None
        }
        append_item_detail_amendment(
            store,
            item=item,
            changes=detail_changes,
            amended_on=physical["checked_on"],
            actor=physical["actor"],
            evidence_id=evidence_id,
            notes=notes,
        )
        if item["ownership_state"] not in {"confirmed", "lent"}:
            item["ownership_state"] = "confirmed"
            add_event(
                store, item_id=item["item_id"], event_type="received",
                occurred_on=physical["checked_on"], actor=physical["actor"],
                evidence_id=evidence_id, notes=notes, location_id=item["location_id"],
                container_id=item["container_id"],
            )
        item["verified_on"] = physical["checked_on"]
        add_event(
            store, item_id=item["item_id"], event_type="physically_verified",
            occurred_on=physical["checked_on"], actor=physical["actor"],
            evidence_id=evidence_id, notes=notes, location_id=item["location_id"],
            container_id=item["container_id"],
        )
        return item["item_id"], evidence_id, False

    discovery = _validate_capture_discovery(decision["discovery"], store)
    model_args = argparse.Namespace(
        actor="capture-review",
        brand=discovery.get("brand"), category=discovery["category"],
        existing_model_id=discovery.get("existing_model_id"), identifiers=discovery.get("identifiers", {}),
        interface=[], model=discovery.get("model"), name=discovery["name"],
        new_model=discovery.get("new_model", False), reference_url=discovery.get("reference_url"),
        specs=discovery.get("specs", {}),
    )
    model, _model_created = resolve_or_create_model(store, model_args)
    if any(row["model_id"] == model["model_id"] for row in store.rows["items"]):
        if discovery.get("new_unit") is not True:
            raise InventoryError("capture discovery may duplicate a recorded unit; confirm new_unit")
    sensitivity = max(
        (discovery.get("sensitivity", "personal"), location_context_sensitivity(
            store, physical["location_id"], physical["container_id"]
        )),
        key=SENSITIVITY_RANK.__getitem__,
    )
    item_id = store.allocate("items", f"itm-{slug(discovery['name'])}")
    evidence_id = store.allocate(
        "evidence", f"ev-capture-{crop['crop_id'][5:29]}-{physical['checked_on']}"
    )
    store.rows["evidence"].append(
        {
            "captured_on": physical["checked_on"], "claim_strength": "explicit_current",
            "evidence_id": evidence_id, "evidence_type": "physical_check", "notes": notes,
            "sensitivity": sensitivity, "source_ref": source_ref,
        }
    )
    store.rows["items"].append(
        {
            "acquired_on": None, "condition": physical["condition"],
            "container_id": physical["container_id"], "home_container_id": None,
            "home_location_id": None, "identity_sensitivity": sensitivity,
            "item_id": item_id, "location_id": physical["location_id"], "model_id": model["model_id"],
            "notes": None, "ownership_state": "confirmed", "primary_evidence_id": evidence_id,
            "purchase_currency": None, "purchase_price": None, "quantity": physical["quantity"],
            "receipt_ref": None, "replacement_value": None, "sensitivity": sensitivity,
            "serial_or_lot": physical["serial_or_lot"], "unit": physical["unit"] or "item",
            "value_currency": None, "verified_on": physical["checked_on"],
        }
    )
    store.rows["item_evidence"].append(
        {"evidence_id": evidence_id, "item_id": item_id, "role": "primary"}
    )
    _attach_capture_crop_evidence(
        store, evidence_id=evidence_id, crop=crop, asset_id=crop_asset_id
    )
    add_event(
        store, item_id=item_id, event_type="received", occurred_on=physical["checked_on"],
        actor=physical["actor"], evidence_id=evidence_id, notes=notes,
        location_id=physical["location_id"], container_id=physical["container_id"],
    )
    add_event(
        store, item_id=item_id, event_type="physically_verified", occurred_on=physical["checked_on"],
        actor=physical["actor"], evidence_id=evidence_id, notes=notes,
        location_id=physical["location_id"], container_id=physical["container_id"],
    )
    return item_id, evidence_id, True


def command_capture_commit(args: argparse.Namespace) -> dict:
    if not getattr(args, "_proposal_materialization", False):
        raise InventoryError("capture commit is internal to capture proposal application")
    if args.scope != "private" or args.media_root is None:
        raise InventoryError("capture commit requires private scope and a media root")
    proposal_id = getattr(args, "_proposal_id", None)
    proposal_base_digest = getattr(args, "_proposal_base_digest", None)
    proposal_operations_digest = getattr(args, "_proposal_operations_digest", None)
    if (
        not isinstance(proposal_id, str)
        or not re.fullmatch(r"proposal-[0-9a-f-]{36}", proposal_id)
        or not isinstance(proposal_base_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", proposal_base_digest)
        or not isinstance(proposal_operations_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", proposal_operations_digest)
    ):
        raise InventoryError("capture commit requires its sealed proposal lineage")
    staging, artifact = read_capture_artifact(args.runtime_dir, args.capture_session_id)
    review_path, review = read_capture_review(
        args.runtime_dir, args.capture_session_id
    )
    artifact_wire = read_bounded_regular_input(
        staging / "artifact.json",
        maximum_bytes=MAX_CAPTURE_METADATA_BYTES,
        label="capture artifact",
    )
    review_wire = read_bounded_regular_input(
        review_path,
        maximum_bytes=MAX_CAPTURE_METADATA_BYTES,
        label="capture review",
    )
    if (
        artifact_wire != canonical_artifact_bytes(artifact)
        or review_wire != canonical_review_bytes(review)
        or hashlib.sha256(artifact_wire).hexdigest() != review["artifact_sha256"]
        or review_path.parent != staging
    ):
        raise InventoryError("capture review does not bind the staged artifact")
    try:
        validate_capture_review(
            review,
            artifact=artifact,
            expected_session_id=args.capture_session_id,
        )
    except CaptureProvenanceError as error:
        raise InventoryError(str(error)) from error
    artifact_sha256 = hashlib.sha256(artifact_wire).hexdigest()
    review_sha256 = hashlib.sha256(review_wire).hexdigest()
    artifact_json = artifact_wire.decode("utf-8")
    review_json = review_wire.decode("utf-8")
    artifact = {**artifact, "links": review["links"]}
    installed: list[tuple[Path, str]] = []

    def mutate(store: Store) -> dict:
        if any(
            row["capture_session_id"] == args.capture_session_id
            for row in store.rows["capture_sessions"]
        ):
            receipts = [
                row
                for row in store.rows["proposal_commits"]
                if row["proposal_id"] == proposal_id
            ]
            if len(receipts) != 1:
                raise InventoryError("existing capture session has no proposal receipt")
            return {"capture_session_id": args.capture_session_id, "reused": True}
        before = {row["sha256"] for row in store.rows["media_assets"]}
        decision_sensitivity = _capture_decisions_sensitivity(
            store, review["decisions"]
        )
        _capture_media(
            store,
            artifact,
            staging,
            args.media_root,
            minimum_sensitivity=decision_sensitivity,
        )
        for row in store.rows["media_assets"]:
            if row["sha256"] not in before:
                installed.append((media_asset_path(args.media_root, row["sha256"]), row["sha256"]))
        evidence_id = artifact["_resolved_evidence_id"]
        sensitivity = max(
            (artifact["_resolved_sensitivity"], decision_sensitivity),
            key=SENSITIVITY_RANK.__getitem__,
        )
        crop_by_id = {crop["crop_id"]: crop for crop in artifact["crops"]}
        crop_asset_ids = _capture_crop_asset_ids(store, artifact)
        decision_items: dict[str, str] = {}
        decision_results: list[dict[str, object]] = []
        for decision in review["decisions"]:
            crop = crop_by_id[decision["crop_id"]]
            item_id, physical_evidence_id, created = _capture_decision_item(
                store,
                decision=decision,
                artifact=artifact,
                crop=crop,
                crop_asset_id=crop_asset_ids[crop["crop_id"]],
            )
            observation_id = decision.get("observation_id")
            if observation_id is not None and observation_id.startswith("observation-"):
                decision_items[observation_id] = item_id
            decision_results.append(
                {
                    "crop_id": crop["crop_id"], "created": created,
                    "evidence_id": physical_evidence_id, "item_id": item_id,
                }
            )
        store.rows["capture_sessions"].append(
            {
                "capture_session_id": args.capture_session_id,
                "captured_on": artifact["captured_on"],
                "evidence_id": evidence_id,
                "sensitivity": sensitivity,
                "provenance_state": "bound",
                "artifact_sha256": artifact_sha256,
                "artifact_json": artifact_json,
                "review_sha256": review_sha256,
                "review_json": review_json,
                "notes": "Prepared overview capture; observations require separate review for identity claims.",
            }
        )
        for index, observation in enumerate(artifact["observations"], start=1):
            observation_id = f"observation-{index}"
            item_id = decision_items.get(observation_id) or artifact["links"].get(observation_id)
            if item_id is not None:
                item = store.get("items", item_id)
                if SENSITIVITY_RANK[item["sensitivity"]] > SENSITIVITY_RANK[sensitivity]:
                    raise InventoryError("capture link sensitivity exceeds capture evidence")
                if not any(
                    row["item_id"] == item_id and row["evidence_id"] == evidence_id
                    for row in store.rows["item_evidence"]
                ):
                    store.rows["item_evidence"].append(
                        {
                            "item_id": item_id,
                            "evidence_id": evidence_id,
                            "role": "supporting",
                        }
                    )
            store.rows["capture_observations"].append(
                {
                    "observation_id": store.allocate(
                        "capture_observations",
                        f"obs-{args.capture_session_id[8:20]}-{index}",
                    ),
                    "capture_session_id": args.capture_session_id,
                    "observation_index": index,
                    "item_id": item_id,
                    "observation_json": json.dumps(
                        observation, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                    "evidence_id": evidence_id,
                    "sensitivity": sensitivity,
                    "validation_state": "validated",
                    "notes": "Unreviewed capture observation"
                    if item_id is None
                    else "Explicit reviewer link; supporting evidence only.",
                }
            )
        store.rows["proposal_commits"].append(
            {
                "applied_at": datetime.now().astimezone().isoformat(),
                "base_digest": proposal_base_digest,
                "operations_digest": proposal_operations_digest,
                "proposal_id": proposal_id,
            }
        )
        return {
            "capture_session_id": args.capture_session_id,
            "decisions": decision_results,
            "observation_count": len(artifact["observations"]),
            "reused": False,
        }

    try:
        with media_lock(args.media_root):
            return transaction(
                args.inventory_root,
                args.runtime_dir,
                f"capture-{args.capture_session_id}",
                mutate,
                continue_batch=args.continue_batch,
            )
    except BaseException:
        for path, digest in installed:
            cleanup_unreferenced_media(args.inventory_root, args.media_root, path, digest)
        raise


def command_capture_status(args: argparse.Namespace) -> dict:
    if args.scope != "private":
        raise InventoryError("capture status requires private scope")
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        lock = inventory_lock(args.inventory_root)
        lock.acquire()
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error
    try:
        recover_pending_transaction(paths)
        store = load_verified_store(paths)
        sessions = [
            row
            for row in store.rows["capture_sessions"]
            if row["capture_session_id"] == args.capture_session_id
        ]
        if sessions:
            session = sessions[0]
            provenance_failures = capture_provenance_failures(
                store.rows, capture_session_id=args.capture_session_id
            )
            if provenance_failures:
                raise InventoryError("; ".join(provenance_failures))
            provenance_state = session.get("provenance_state")
            provenance: dict[str, object] = {
                "provenance_state": provenance_state,
            }
            if provenance_state == "bound":
                artifact_text = session.get("artifact_json")
                review_text = session.get("review_json")
                if not isinstance(artifact_text, str) or not isinstance(review_text, str):
                    raise InventoryError("bound capture provenance is incomplete")
                artifact_value = strict_json_value(artifact_text, "bound capture artifact")
                review_value = strict_json_value(review_text, "bound capture review")
                try:
                    artifact_value = validate_capture_artifact(
                        artifact_value, expected_session_id=args.capture_session_id
                    )
                    review_value = validate_capture_review(
                        review_value,
                        artifact=artifact_value,
                        expected_session_id=args.capture_session_id,
                    )
                except CaptureProvenanceError as error:
                    raise InventoryError(str(error)) from error
                artifact_wire = artifact_text.encode("utf-8")
                review_wire = review_text.encode("utf-8")
                if (
                    artifact_wire != canonical_artifact_bytes(artifact_value)
                    or review_wire != canonical_review_bytes(review_value)
                    or hashlib.sha256(artifact_wire).hexdigest()
                    != session.get("artifact_sha256")
                    or hashlib.sha256(review_wire).hexdigest()
                    != session.get("review_sha256")
                ):
                    raise InventoryError("bound capture provenance digest is invalid")
                provenance.update(
                    {
                        "artifact_sha256": session["artifact_sha256"],
                        "review_sha256": session["review_sha256"],
                        "source_count": 1,
                        "crop_count": len(artifact_value["crops"]),
                    }
                )
            elif provenance_state != "legacy_unbound":
                raise InventoryError("capture provenance state is invalid")
            staging = capture_staging_path(args.runtime_dir, args.capture_session_id)
            return {
                "capture_session_id": args.capture_session_id,
                "status": "applied",
                "staging": str(staging) if staging.is_dir() and not staging.is_symlink() else None,
                "observation_count": sum(
                    row["capture_session_id"] == args.capture_session_id
                    for row in store.rows["capture_observations"]
                ),
                **provenance,
            }
        staging, artifact = read_capture_artifact(
            args.runtime_dir, args.capture_session_id
        )
        review_path = capture_review_path(args.runtime_dir, args.capture_session_id)
        reviewed = path_entry_exists(review_path)
        validate_capture_staging(staging, artifact, review_present=reviewed)
        if reviewed:
            _, review = read_capture_review(args.runtime_dir, args.capture_session_id)
            proposal_args = argparse.Namespace(**vars(args))
            proposal_args.proposal_id = review["proposal_id"]
            read_proposal(proposal_args)
        return {
            "capture_session_id": args.capture_session_id,
            "status": "prepared" if reviewed else "awaiting_review",
            "staging": str(staging),
            "observation_count": len(artifact["observations"]),
        }
    finally:
        lock.release()


def command_capture_cleanup(args: argparse.Namespace) -> dict:
    """Explicitly retire redundant runtime staging for one applied bound capture."""
    if args.scope != "private":
        raise InventoryError("capture cleanup requires private scope")
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
            store = load_verified_store(paths)
            failures = capture_provenance_failures(
                store.rows, capture_session_id=args.capture_session_id
            )
            if failures:
                raise InventoryError("; ".join(failures))
            sessions = [
                row
                for row in store.rows["capture_sessions"]
                if row["capture_session_id"] == args.capture_session_id
            ]
            if len(sessions) != 1:
                raise InventoryError("applied capture session was not found")
            session = sessions[0]
            if session.get("provenance_state") != "bound":
                raise InventoryError("legacy-unbound capture staging cannot be cleaned")
            cleanup = retire_applied_capture_staging(
                args.runtime_dir,
                args.capture_session_id,
                artifact_sha256=session["artifact_sha256"],
                review_sha256=session["review_sha256"],
                artifact_json=session["artifact_json"],
                review_json=session["review_json"],
            )
            return {
                "capture_session_id": args.capture_session_id,
                "staging_cleanup": cleanup,
            }
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error


def command_capture_benchmark(args: argparse.Namespace) -> dict:
    try:
        payload = read_bounded_regular_input(
            args.input,
            maximum_bytes=MAX_CAPTURE_BENCHMARK_BYTES,
            label="capture benchmark input",
        )
        cases = strict_json_value(
            payload.decode("utf-8"), "capture benchmark input"
        )
        if not isinstance(cases, list):
            raise InventoryError("capture benchmark input must be a JSON array")
        if args.capture_adapters_config is None:
            raise InventoryError("capture benchmark requires a server-owned adapter config")
        registry = load_adapter_registry(args.capture_adapters_config)
        report = run_synthetic_capture_benchmark(
            cases=cases,
            registry=registry,
            adapter_name=args.adapter_name,
            top_k=args.top_k,
            timeout_seconds=args.adapter_timeout,
        )
    except (OSError, UnicodeDecodeError, CaptureError, CaptureServiceError) as error:
        raise InventoryError(f"capture benchmark failed: {error}") from error
    return report.to_dict()


def sync_plan_path(runtime_dir: Path, plan_id: str) -> Path:
    if not re.fullmatch(r"sync-plan-[0-9a-f-]{36}", plan_id):
        raise InventoryError(f"invalid sync plan id: {plan_id}")
    return checked_managed_path(
        runtime_dir, Path(SYNC_PLAN_DIRECTORY) / f"{plan_id}.json", "runtime"
    )


def sync_bundle_media_path(bundle_path: Path) -> Path:
    """Use one deterministic, sibling sidecar without accepting a path from input."""
    return bundle_path.with_name(f"{bundle_path.name}.media")


def sync_plan_media_path(runtime_dir: Path, plan_id: str) -> Path:
    return checked_managed_path(
        runtime_dir, Path(SYNC_PLAN_DIRECTORY) / f"{plan_id}.media", "runtime"
    )


def validate_sync_media_records(records: object) -> list[dict]:
    if not isinstance(records, list) or len(records) > 1024:
        raise InventoryError("replica media manifest is malformed or too large")
    normalized: list[dict] = []
    total = 0
    for record in records:
        if not isinstance(record, dict) or set(record) != {"sha256", "byte_size", "media_type"}:
            raise InventoryError("replica media manifest record is malformed")
        digest, size, media_type = record["sha256"], record["byte_size"], record["media_type"]
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > MAX_SYNC_MEDIA_BYTES
            or not isinstance(media_type, str)
            or not normalized_media_type(media_type)
        ):
            raise InventoryError("replica media manifest record is invalid")
        total += size
        if total > MAX_SYNC_MEDIA_TOTAL_BYTES:
            raise InventoryError("replica media sidecar exceeds the total limit")
        normalized.append(
            {"sha256": digest, "byte_size": size, "media_type": normalized_media_type(media_type)}
        )
    if normalized != sorted(normalized, key=lambda row: row["sha256"]):
        raise InventoryError("replica media manifest is not deterministic")
    if len({row["sha256"] for row in normalized}) != len(normalized):
        raise InventoryError("replica media manifest has duplicate digests")
    return normalized


def verify_sync_media_sidecar(bundle_path: Path, records: object) -> tuple[Path, list[dict]]:
    """Fail closed unless a sidecar has exactly the signed immutable bytes."""
    normalized = validate_sync_media_records(records)
    sidecar = sync_bundle_media_path(bundle_path)
    if not normalized:
        if path_entry_exists(sidecar):
            raise InventoryError("empty replica bundle has an unexpected media sidecar")
        return sidecar, normalized
    if sidecar.is_symlink() or not sidecar.is_dir():
        raise InventoryError("replica media sidecar is missing or unsafe")
    expected = {
        (Path("sha256") / row["sha256"][:2] / row["sha256"]).as_posix()
        for row in normalized
    }
    actual: set[str] = set()
    for entry in sidecar.rglob("*"):
        relative = entry.relative_to(sidecar).as_posix()
        if entry.is_symlink():
            raise InventoryError(f"replica media sidecar traverses a symlink: {relative}")
        if entry.is_dir():
            continue
        if not entry.is_file():
            raise InventoryError(f"replica media sidecar contains a special entry: {relative}")
        actual.add(relative)
    if actual != expected:
        raise InventoryError("replica media sidecar has missing or extra entries")
    for record in normalized:
        digest = record["sha256"]
        path = checked_managed_path(sidecar, Path("sha256") / digest[:2] / digest, "replica media")
        status = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(status.st_mode) or status.st_size != record["byte_size"]:
            raise InventoryError(f"replica media bytes have an invalid size: {digest}")
        if file_digest(path) != digest:
            raise InventoryError(f"replica media bytes have an invalid digest: {digest}")
        try:
            validate_declared_media(path, record["media_type"])
        except MediaValidationError as error:
            raise InventoryError(f"replica media bytes have an invalid MIME claim: {digest}: {error}") from error
    return sidecar, normalized


def copy_verified_sync_media(source_root: Path, destination_root: Path, records: list[dict]) -> None:
    """Copy a signed sidecar to a private staging tree, never merging stale entries."""
    if path_entry_exists(destination_root):
        raise InventoryError("sync media staging destination already exists")
    if not records:
        return
    ensure_private_directory(destination_root)
    for record in records:
        digest = record["sha256"]
        source = checked_managed_path(source_root, Path("sha256") / digest[:2] / digest, "replica media")
        destination = checked_managed_path(
            destination_root, Path("sha256") / digest[:2] / digest, "sync media staging"
        )
        ensure_private_directory(destination.parent)
        durable_copy(source, destination)
        ensure_private_file(destination)
    verify_sync_media_sidecar_from_root(destination_root, records, "staged sync media")


def verify_sync_media_sidecar_from_root(root: Path, records: list[dict], label: str) -> None:
    """Reuse the content checks after server-owned staging or before installation."""
    for record in records:
        digest = record["sha256"]
        path = checked_managed_path(root, Path("sha256") / digest[:2] / digest, label)
        if path.is_symlink() or not path.is_file() or path.stat().st_size != record["byte_size"]:
            raise InventoryError(f"{label} is missing or corrupt: {digest}")
        if file_digest(path) != digest:
            raise InventoryError(f"{label} digest mismatch: {digest}")
        try:
            validate_declared_media(path, record["media_type"])
        except MediaValidationError as error:
            raise InventoryError(f"{label} MIME mismatch: {digest}: {error}") from error


def sync_private_input(path: Path, label: str) -> Path:
    """Accept a bounded regular private artifact, never a symlinked path."""
    lexical = Path(os.path.abspath(path.expanduser()))
    if lexical.is_symlink() or not lexical.is_file():
        raise InventoryError(f"{label} must be a regular file, not a symlink")
    if lexical.stat().st_size > 64 * 1024 * 1024:
        raise InventoryError(f"{label} is too large")
    return lexical


def sync_private_output(args: argparse.Namespace, output: Path, label: str) -> Path:
    """Keep portable private artifacts outside every managed namespace."""
    lexical = Path(os.path.abspath(output.expanduser()))
    if lexical.is_symlink() or lexical.parent.is_symlink():
        raise InventoryError(f"{label} must not traverse a symlink")
    if path_entry_exists(lexical):
        raise InventoryError(f"refusing to overwrite {label}: {lexical}")
    try:
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise InventoryError(f"cannot resolve {label}: {error}") from error
    protected = [args.inventory_root, args.runtime_dir, args.catalogue_output]
    if args.media_root is not None:
        protected.append(args.media_root)
    protected.extend(args.forbidden_roots)
    for root in protected:
        resolved_root = root.resolve()
        if (
            resolved == resolved_root
            or resolved in resolved_root.parents
            or resolved_root in resolved.parents
        ):
            raise InventoryError(f"{label} must be outside managed root: {root}")
    return lexical


def validated_sync_snapshot(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise InventoryError(f"{label} must be a snapshot object")
    tables = value.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(TABLES):
        raise InventoryError(f"{label} must contain exactly the canonical table set")
    for table in TABLES:
        rows = tables[table]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise InventoryError(f"{label} has malformed {table} rows")
    try:
        return verify_store_snapshot(value)
    except SyncError as error:
        raise InventoryError(f"{label} is not a valid canonical snapshot: {error}") from error


def sync_jsonl_rows(payload: bytes, table: str, label: str) -> list[dict]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise InventoryError(f"{label} {table}.jsonl is not UTF-8") from error
    rows: list[dict] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise InventoryError(f"{label} {table}.jsonl has a blank row")
        row = strict_json_value(line, f"{label} {table}.jsonl row {line_number}")
        if not isinstance(row, dict):
            raise InventoryError(f"{label} {table}.jsonl row {line_number} is not an object")
        rows.append(row)
    return rows


def read_sync_snapshot(path: Path, label: str) -> dict:
    artifact = sync_private_input(path, label)
    if artifact.suffixes[-2:] == [".tar", ".gz"]:
        # Reuse the export preflight before touching an archive member.
        manifest = inspect_restore_archive(artifact, allow_unsafe_legacy=False)
        try:
            with tarfile.open(artifact, "r:gz") as archive:
                members = safe_archive_members(archive)
                table_members = [members[f"store/{table}.jsonl"] for table in TABLES]
                if sum(member.size for member in table_members) > 64 * 1024 * 1024:
                    raise InventoryError(f"{label} canonical tables are too large")
                tables = {}
                for table, member in zip(TABLES, table_members, strict=True):
                    handle = archive.extractfile(member)
                    if handle is None:
                        raise InventoryError(f"cannot read {label} table: {table}")
                    payload = handle.read(member.size + 1)
                    if len(payload) != member.size:
                        raise InventoryError(f"cannot read complete {label} table: {table}")
                    tables[table] = sync_jsonl_rows(payload, table, label)
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            tarfile.TarError,
            InventoryError,
        ) as error:
            raise InventoryError(f"cannot read {label}: {error}") from error
        try:
            snapshot = build_store_snapshot(tables)
        except SyncError as error:
            raise InventoryError(f"{label} canonical tables are malformed: {error}") from error
        if snapshot["inventory_id"] != manifest["inventory_id"]:
            raise InventoryError(f"{label} manifest and canonical metadata disagree")
        return validated_sync_snapshot(snapshot, label)
    try:
        return validated_sync_snapshot(
            strict_json_value(artifact.read_text(encoding="utf-8"), label), label
        )
    except (OSError, UnicodeError) as error:
        raise InventoryError(f"cannot read {label}: {error}") from error


def sync_sandbox_validator(
    args: argparse.Namespace,
    *,
    replica_media_root: Path | None = None,
    replica_media: list[dict] | None = None,
) -> Callable[[dict[str, list[dict]]], None]:
    """Run the real rebuild, rendering, semantic and FK verification on every ready plan."""
    paths = data_paths(args.inventory_root, args.runtime_dir)
    replica_by_digest = {
        row["sha256"]: row for row in (replica_media or [])
    }

    def validate(tables: dict[str, list[dict]]) -> None:
        with tempfile.TemporaryDirectory(prefix="property-inventory-sync-verify-") as temp_name:
            temporary = Path(temp_name)
            sandbox_store = temporary / "store"
            staged = Store(paths["store"])
            staged.rows = tables
            staged.save(sandbox_store)
            sandbox_paths = dict(paths)
            if paths["media_root"] is not None:
                sandbox_media = temporary / "media"
                ensure_private_directory(sandbox_media)
                for asset in tables.get("media_assets", []):
                    digest = asset.get("sha256")
                    if not isinstance(digest, str):
                        raise InventoryError("sync sandbox media asset is malformed")
                    source_root = (
                        replica_media_root if digest in replica_by_digest else paths["media_root"]
                    )
                    if source_root is None:
                        raise InventoryError("sync sandbox has no media source")
                    source = media_asset_path(source_root, digest)
                    destination = media_asset_path(sandbox_media, digest)
                    ensure_private_directory(destination.parent)
                    durable_copy(source, destination)
                sandbox_paths["media_root"] = sandbox_media
            verify_bundle(
                sandbox_paths,
                sandbox_store,
                temporary / "inventory.sqlite",
                temporary / "Inventory.md",
            )

    return validate


def read_sync_plan(args: argparse.Namespace) -> tuple[Path, dict]:
    path = sync_plan_path(args.runtime_dir, args.plan_id)
    if not path.is_file() or path.is_symlink():
        raise InventoryError(f"sync plan not found: {args.plan_id}")
    try:
        envelope = strict_json_value(path.read_text(encoding="utf-8"), "sync plan")
    except (OSError, UnicodeError) as error:
        raise InventoryError(f"cannot read sync plan: {error}") from error
    if (
        not isinstance(envelope, dict)
        or envelope.get("format") != SYNC_PLAN_ENVELOPE_FORMAT
        or envelope.get("plan_id") != args.plan_id
        or envelope.get("status") not in {"prepared", "applied"}
        or not isinstance(envelope.get("plan"), dict)
        or "media" not in envelope
    ):
        raise InventoryError("sync plan envelope is malformed")
    try:
        envelope["media"] = validate_sync_media_records(envelope["media"])
    except InventoryError as error:
        raise InventoryError(f"sync plan media manifest is malformed: {error}") from error
    plan = envelope["plan"]
    try:
        if plan.get("status") == "ready":
            receipt_data(plan)
        elif plan.get("status") != "needs_resolution":
            raise SyncError("unsupported sync plan status")
    except SyncError as error:
        raise InventoryError(f"sync plan is malformed: {error}") from error
    return path, envelope


def command_sync_snapshot(args: argparse.Namespace) -> dict:
    if args.scope != "private":
        raise InventoryError("sync snapshots require private scope")
    output = sync_private_output(args, args.output, "sync snapshot")
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
            store = load_verified_store(paths)
            # The same sandbox verifier used for plans proves this source snapshot.
            sync_sandbox_validator(args)(store.rows)
            payload = build_store_snapshot(store.rows)
            output.parent.mkdir(parents=True, exist_ok=True)
            write_json(output, payload)
            return {
                "status": "snapshot_created",
                "snapshot": str(output),
                "digest": payload["digest"],
            }
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error


def command_sync_bundle(args: argparse.Namespace) -> dict:
    if args.scope != "private":
        raise InventoryError("replica bundles require private scope")
    output = sync_private_output(args, args.output, "replica bundle")
    base = read_sync_snapshot(args.base, "replica base")
    head = read_sync_snapshot(args.head, "replica head")
    inventory_ids = {base["inventory_id"], head["inventory_id"]}
    if len(inventory_ids) != 1:
        raise InventoryError("replica base and head belong to different inventories")
    try:
        bundle = build_replica_bundle(
            inventory_id=next(iter(inventory_ids)),
            replica_ref=args.replica_ref,
            base=base["tables"],
            head=head["tables"],
        )
    except SyncError as error:
        raise InventoryError(f"cannot create replica bundle: {error}") from error
    records = validate_sync_media_records(bundle["media"])
    sidecar = sync_bundle_media_path(output)
    if path_entry_exists(sidecar):
        raise InventoryError(f"refusing to overwrite replica media sidecar: {sidecar}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if records:
        if args.media_root is None:
            raise InventoryError("replica bundle media requires --media-root")
        source_root = args.media_root
        # The local replica must prove every sidecar byte before this transport artifact exists.
        verify_sync_media_sidecar_from_root(source_root, records, "replica media root")
        temporary_parent = Path(tempfile.mkdtemp(prefix=f".{output.name}.media-", dir=output.parent))
        temporary = temporary_parent / "sidecar"
        try:
            copy_verified_sync_media(source_root, temporary, records)
            os.replace(temporary, sidecar)
            fsync_directory(sidecar.parent)
            temporary_parent.rmdir()
        except Exception:
            shutil.rmtree(temporary_parent, ignore_errors=True)
            raise
    write_json(output, bundle)
    return {
        "status": "bundle_created",
        "bundle": str(output),
        "bundle_digest": bundle["bundle_digest"],
        "media_assets": len(records),
    }


def command_sync_prepare(args: argparse.Namespace) -> dict:
    if args.scope != "private":
        raise InventoryError("sync preparation requires private scope")
    bundle_path = sync_private_input(args.bundle, "replica bundle")
    trusted_base = read_sync_snapshot(args.trusted_base, "trusted canonical base")
    try:
        bundle = strict_json_value(bundle_path.read_text(encoding="utf-8"), "replica bundle")
        if not isinstance(bundle, dict):
            raise InventoryError("replica bundle must be a JSON object")
    except (OSError, UnicodeError) as error:
        raise InventoryError(f"cannot read replica bundle: {error}") from error
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
            store = load_verified_store(paths)
            try:
                verified_bundle = verify_replica_bundle(bundle)
                sidecar, records = verify_sync_media_sidecar(bundle_path, verified_bundle["media"])
                plan = plan_three_way_merge(
                    base=trusted_base["tables"],
                    canonical_head=store.rows,
                    bundle=bundle,
                    merged_store_validator=sync_sandbox_validator(
                        args, replica_media_root=sidecar, replica_media=records
                    ),
                )
            except (SyncError, InventoryError) as error:
                raise InventoryError(f"cannot prepare replica sync: {error}") from error
            plan_id = f"sync-plan-{uuid.uuid4()}"
            destination = sync_plan_path(args.runtime_dir, plan_id)
            ensure_private_directory(destination.parent)
            media_stage = sync_plan_media_path(args.runtime_dir, plan_id)
            copy_verified_sync_media(sidecar, media_stage, records)
            write_json(
                destination,
                {
                    "format": SYNC_PLAN_ENVELOPE_FORMAT,
                    "plan_id": plan_id,
                    "status": "prepared",
                    "plan": plan,
                    "media": records,
                },
            )
            return {"status": plan["status"], "plan_id": plan_id, "plan": plan}
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error


def command_sync_show(args: argparse.Namespace) -> dict:
    if args.scope != "private":
        raise InventoryError("sync plan inspection requires private scope")
    _path, envelope = read_sync_plan(args)
    return {"plan_id": args.plan_id, "status": envelope["status"], "plan": envelope["plan"]}


def command_sync_resolve(args: argparse.Namespace) -> dict:
    if args.scope != "private":
        raise InventoryError("sync resolution requires private scope")
    path, envelope = read_sync_plan(args)
    if envelope["status"] == "applied":
        raise InventoryError("an applied sync plan cannot be resolved")
    resolutions_path = sync_private_input(args.resolutions, "sync resolutions")
    try:
        resolutions = strict_json_value(
            resolutions_path.read_text(encoding="utf-8"), "sync resolutions"
        )
        if not isinstance(resolutions, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in resolutions.items()
        ):
            raise InventoryError("sync resolutions must be a JSON object of string choices")
        ready = resolve_conflicts(
            envelope["plan"],
            resolutions,
            merged_store_validator=sync_sandbox_validator(
                args,
                replica_media_root=sync_plan_media_path(args.runtime_dir, args.plan_id),
                replica_media=envelope["media"],
            ),
        )
    except (OSError, UnicodeError, SyncError) as error:
        raise InventoryError(f"cannot resolve sync plan: {error}") from error
    envelope["plan"] = ready
    write_json(path, envelope)
    return {"status": ready["status"], "plan_id": args.plan_id, "plan": ready}


def sync_receipt_evidence(plan: dict) -> dict:
    receipt = receipt_data(plan)
    evidence_id = "ev-sync-" + receipt["sync_receipt_id"].removeprefix("sync-")
    return {
        "captured_on": datetime.now().date().isoformat(),
        "claim_strength": "research_only",
        "evidence_id": evidence_id,
        "evidence_type": "research",
        "notes": "Replica transport receipt only. It makes no lifecycle, possession, location, quantity, or condition claim.",
        "sensitivity": "high",
        "source_ref": f"Offline replica payload {receipt['payload_digest']}",
    }


def sync_plan_media_for_result(plan: dict, records: list[dict]) -> list[dict]:
    """Keep transport bytes only when their exact asset metadata survives the plan."""
    assets = plan.get("tables", {}).get("media_assets")
    if not isinstance(assets, list):
        raise InventoryError("sync plan media assets are malformed")
    by_digest: dict[str, dict] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("sha256"), str):
            raise InventoryError("sync plan media assets are malformed")
        digest = asset["sha256"]
        if digest in by_digest:
            raise InventoryError("sync plan has duplicate media digests")
        by_digest[digest] = asset
    selected: list[dict] = []
    for record in records:
        asset = by_digest.get(record["sha256"])
        if asset is None:
            continue
        if (
            asset.get("byte_size") != record["byte_size"]
            or normalized_media_type(str(asset.get("media_type", ""))) != record["media_type"]
        ):
            raise InventoryError("sync plan media metadata conflicts with its signed sidecar")
        selected.append(record)
    return selected


def install_sync_plan_media(args: argparse.Namespace, plan_id: str, plan: dict, records: list[dict]) -> list[Path]:
    if not records:
        return []
    if args.media_root is None:
        raise InventoryError("sync plan media requires --media-root")
    selected = sync_plan_media_for_result(plan, records)
    if not selected:
        return []
    stage = sync_plan_media_path(args.runtime_dir, plan_id)
    verify_sync_media_sidecar_from_root(stage, selected, "staged sync media")
    installed_new: list[Path] = []
    try:
        with media_lock(args.media_root):
            for record in selected:
                source = checked_managed_path(
                    stage, Path("sha256") / record["sha256"][:2] / record["sha256"], "staged sync media"
                )
                destination, created = install_media(source, args.media_root, record["sha256"])
                if destination.stat().st_size != record["byte_size"]:
                    raise InventoryError("existing-digest-conflicting media byte size")
                try:
                    validate_declared_media(destination, record["media_type"])
                except MediaValidationError as error:
                    raise InventoryError("existing-digest-conflicting media MIME type") from error
                if created:
                    installed_new.append(destination)
    except Timeout as error:
        raise InventoryError("another writer holds the media-root lock") from error
    return installed_new


def command_sync_apply(args: argparse.Namespace) -> dict:
    if args.scope != "private":
        raise InventoryError("sync application requires private scope")
    path, envelope = read_sync_plan(args)
    plan = envelope["plan"]
    if plan.get("status") != "ready":
        raise InventoryError("sync plan requires explicit conflict resolution before application")
    try:
        receipt = receipt_data(plan)
        sync_sandbox_validator(
            args,
            replica_media_root=sync_plan_media_path(args.runtime_dir, args.plan_id),
            replica_media=envelope["media"],
        )(plan["tables"])
    except (KeyError, SyncError, InventoryError) as error:
        raise InventoryError(f"sync plan cannot be applied: {error}") from error
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
            current = load_verified_store(paths)
            existing = [
                row
                for row in current.rows["sync_receipts"]
                if row["sync_receipt_id"] == receipt["sync_receipt_id"]
            ]
            competing = [
                row
                for row in current.rows["sync_receipts"]
                if row["replica_ref"] == receipt["replica_ref"]
                and row["payload_digest"] == receipt["payload_digest"]
                and row["sync_receipt_id"] != receipt["sync_receipt_id"]
            ]
            if envelope["status"] == "applied" and not existing:
                raise InventoryError("applied sync plan has no canonical receipt")
            if competing:
                raise InventoryError(
                    "replica payload was already applied with a different resolved result"
                )
            if existing:
                if (
                    existing[0]["replica_ref"] != receipt["replica_ref"]
                    or existing[0]["payload_digest"] != receipt["payload_digest"]
                ):
                    raise InventoryError("canonical sync receipt disagrees with plan")
                envelope["status"] = "applied"
                write_json(path, envelope)
                return {
                    "status": "recovered_applied",
                    "plan_id": args.plan_id,
                    "recovered_as_already_applied": True,
                }
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error

    evidence = sync_receipt_evidence(plan)
    installed_new = install_sync_plan_media(args, args.plan_id, plan, envelope["media"])

    def mutate(store: Store) -> dict:
        existing = [
            row
            for row in store.rows["sync_receipts"]
            if row["sync_receipt_id"] == receipt["sync_receipt_id"]
        ]
        competing = [
            row
            for row in store.rows["sync_receipts"]
            if row["replica_ref"] == receipt["replica_ref"]
            and row["payload_digest"] == receipt["payload_digest"]
            and row["sync_receipt_id"] != receipt["sync_receipt_id"]
        ]
        if competing:
            raise InventoryError(
                "replica payload was already applied with a different resolved result"
            )
        if existing:
            if (
                existing[0]["replica_ref"] != receipt["replica_ref"]
                or existing[0]["payload_digest"] != receipt["payload_digest"]
            ):
                raise InventoryError("canonical sync receipt disagrees with plan")
            return {"plan_id": args.plan_id, "recovered_as_already_applied": True}
        if sync_store_digest(store.rows) != plan["canonical_head_digest"]:
            raise InventoryError("sync plan is stale because the canonical head changed")
        for record in sync_plan_media_for_result(plan, envelope["media"]):
            for asset in store.rows["media_assets"]:
                if asset.get("sha256") != record["sha256"]:
                    continue
                if (
                    asset.get("byte_size") != record["byte_size"]
                    or normalized_media_type(str(asset.get("media_type", ""))) != record["media_type"]
                ):
                    raise InventoryError("existing-digest-conflicting media metadata")
        if any(
            row["sync_receipt_id"] == receipt["sync_receipt_id"]
            for row in plan["tables"]["sync_receipts"]
        ):
            raise InventoryError("replica payload illegally predeclares this sync receipt")
        store.rows = plan["tables"]
        existing_evidence = [
            row for row in store.rows["evidence"] if row["evidence_id"] == evidence["evidence_id"]
        ]
        if existing_evidence and existing_evidence[0] != evidence:
            raise InventoryError("sync receipt evidence disagrees with plan")
        if not existing_evidence:
            store.rows["evidence"].append(evidence)
        store.rows["sync_receipts"].append(
            {
                **receipt,
                "recorded_at": datetime.now().astimezone().isoformat(),
                "evidence_id": evidence["evidence_id"],
                "sensitivity": "high",
                "notes": "Offline replica merge receipt; no lifecycle or possession inference.",
            }
        )
        return {
            "plan_id": args.plan_id,
            "sync_receipt_id": receipt["sync_receipt_id"],
            "recovered_as_already_applied": False,
        }

    try:
        result = transaction(
            args.inventory_root,
            args.runtime_dir,
            f"apply-{args.plan_id}",
            mutate,
            continue_batch=args.continue_batch,
        )
    except Exception:
        for path in installed_new:
            cleanup_unreferenced_media(args.inventory_root, args.media_root, path, path.name)
        raise
    if os.environ.get("PROPERTY_INVENTORY_FAIL_SYNC_AFTER_COMMIT") == "1":
        os._exit(97)
    envelope["status"] = "applied"
    write_json(path, envelope)
    return result


PROPOSAL_COMMANDS = {
    "add-location",
    "add-alias",
    "add-valuation",
    "add-tag",
    "add-kit",
    "set-kit-requirement",
    "review-kit",
    "add-torque-path",
    "add-item-dimensions",
    "correct-item-identity",
    "record-evidence",
    "amend-fact",
    "enrich-item",
    "add-space",
    "import-floorplan",
    "order",
    "plan",
    "receive",
    "discover",
    "sell",
    "relate",
    "not-found",
    "physical-check",
    "restore-current-ownership",
    "return-loan",
    "add-party",
    "set-home",
    "custody-start",
    "custody-end",
    "access-grant",
    "access-revoke",
    "ownership-start",
    "ownership-end",
    "embody-location",
    "move",
    "change",
    "add-interface",
    "capture-commit",
}


def proposal_path(runtime_dir: Path, proposal_id: str) -> Path:
    if not re.fullmatch(r"proposal-[0-9a-f-]{36}", proposal_id):
        raise InventoryError(f"invalid proposal id: {proposal_id}")
    return checked_managed_path(
        runtime_dir,
        Path("proposals") / f"{proposal_id}.json",
        "runtime",
    )


def validate_proposal_operations(value: object) -> list[list[str]]:
    if not isinstance(value, list) or not value:
        raise InventoryError("proposal operations must be a non-empty JSON array")
    operations: list[list[str]] = []
    for number, operation in enumerate(value, start=1):
        if (
            not isinstance(operation, list)
            or not operation
            or any(not isinstance(part, str) for part in operation)
            or operation[0] not in PROPOSAL_COMMANDS
        ):
            raise InventoryError(f"invalid or unsupported proposal operation {number}")
        operations.append(operation)
    return operations


def strict_json_value(value: str, label: str) -> object:
    try:
        return _strict_argument_json(value)
    except (argparse.ArgumentTypeError, TypeError) as error:
        raise InventoryError(f"{label} is malformed: {error}") from error


def canonical_floorplan_proposal_document(document: object) -> dict:
    """Validate a floor plan and retain only fields the importer consumes."""
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise InventoryError("proposal floor-plan input must be a GeoJSON FeatureCollection")
    features = document.get("features")
    if not isinstance(features, list) or not features:
        raise InventoryError("proposal floor-plan input must contain features")
    prepared = json.loads(json.dumps(document))
    source_by_id: dict[str, dict] = {}
    supplied_evidence_ids: set[str] = set()
    for feature in prepared["features"]:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise InventoryError("proposal floor-plan entries must be GeoJSON Features")
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            raise InventoryError("proposal floor-plan properties must be objects")
        feature_id = feature.get("id", properties.get("feature_id"))
        if not isinstance(feature_id, str) or not feature_id.strip():
            raise InventoryError("proposal floor-plan feature IDs must be non-empty")
        if feature_id in source_by_id:
            raise InventoryError("proposal floor-plan feature IDs must be unique")
        source_by_id[feature_id] = feature
        if "evidence_id" in properties:
            supplied_evidence_ids.add(feature_id)
        properties.setdefault("evidence_id", "ev-proposal-floorplan-validation")
    try:
        plan = parse_geojson_floor_plan(prepared)
    except SpatialValidationError as error:
        raise InventoryError(f"invalid proposal floor plan: {error}") from error

    canonical_features: list[dict] = []
    for feature in plan.features:
        source = source_by_id[feature.feature_id]
        source_properties = source["properties"]
        location_id = source_properties.get("location_id")
        sensitivity = source_properties.get("sensitivity")
        if not isinstance(location_id, str) or not location_id.strip():
            raise InventoryError(
                f"proposal floor-plan feature {feature.feature_id} needs a location_id"
            )
        if sensitivity not in SENSITIVITY_RANK:
            raise InventoryError(
                f"proposal floor-plan feature {feature.feature_id} needs a valid sensitivity"
            )
        rectangle = feature.rectangle
        right = rectangle.x + rectangle.width
        top = rectangle.y + rectangle.height
        properties = {
            "location_id": location_id,
            "sensitivity": sensitivity,
            "unit": rectangle.unit,
        }
        if feature.feature_id in supplied_evidence_ids:
            properties["evidence_id"] = source_properties["evidence_id"]
        canonical_features.append(
            {
                "type": "Feature",
                "id": feature.feature_id,
                "properties": properties,
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [rectangle.x, rectangle.y],
                            [right, rectangle.y],
                            [right, top],
                            [rectangle.x, top],
                            [rectangle.x, rectangle.y],
                        ]
                    ],
                },
            }
        )
    return {"type": "FeatureCollection", "features": canonical_features}


def require_input_outside_forbidden_roots(
    source: Path, forbidden_roots: Sequence[Path], label: str
) -> None:
    """Prevent proposal preparation from echoing files in protected namespaces."""
    lexical = Path(os.path.abspath(source.expanduser()))
    try:
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise InventoryError(f"cannot resolve {label}: {error}") from error
    for forbidden_root in forbidden_roots:
        root = forbidden_root.resolve()
        if any(candidate == root or root in candidate.parents for candidate in (lexical, resolved)):
            raise InventoryError(f"{label} must be outside forbidden root: {forbidden_root}")


def freeze_proposal_inputs(
    operations: list[list[str]], *, forbidden_roots: Sequence[Path] = ()
) -> list[list[str]]:
    """Bind external mutation inputs into the immutable proposal document."""
    frozen: list[list[str]] = []
    for number, operation in enumerate(operations, start=1):
        current = list(operation)
        if current[0] == "import-floorplan":
            if "--document-json" in current or current.count("--input") != 1:
                raise InventoryError(
                    f"proposal import-floorplan operation {number} needs exactly one --input"
                )
            input_index = current.index("--input")
            if input_index + 1 >= len(current):
                raise InventoryError(
                    f"proposal import-floorplan operation {number} has no input path"
                )
            source = Path(current[input_index + 1])
            require_input_outside_forbidden_roots(
                source, forbidden_roots, "proposal floor-plan input"
            )
            document = read_strict_json_file(source, "proposal floor-plan input")
            document = canonical_floorplan_proposal_document(document)
            canonical = json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            current[input_index : input_index + 2] = ["--document-json", canonical]
        frozen.append(current)
    return frozen


def command_propose(args: argparse.Namespace) -> dict:
    if args.scope != "private":
        raise InventoryError("proposal preparation requires private scope")
    operations = freeze_proposal_inputs(
        validate_proposal_operations(
            read_strict_json_file(args.operations, "proposal operations input")
        ),
        forbidden_roots=args.forbidden_roots,
    )
    return prepare_proposal(args, operations)


def prepare_proposal(
    args: argparse.Namespace, operations: list[list[str]], *, import_details: dict | None = None
) -> dict:
    """Write one ordinary immutable proposal without executing its operations."""
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
            return write_prepared_proposal(paths, args.runtime_dir, operations, import_details)
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error


def write_prepared_proposal(
    paths: dict[str, Path],
    runtime_dir: Path,
    operations: list[list[str]],
    import_details: dict | None,
) -> dict:
    """Persist a proposal while the caller owns the canonical inventory lock."""
    load_verified_store(paths)
    proposal_id = f"proposal-{uuid.uuid4()}"
    proposal = {
        "base_digest": canonical_store_digest(paths["store"]),
        "created_at": datetime.now().astimezone().isoformat(),
        "operations": operations,
        "proposal_id": proposal_id,
        "schema_version": SCHEMA_VERSION,
        "status": "prepared",
    }
    if import_details is not None:
        proposal["import"] = import_details
    destination = proposal_path(runtime_dir, proposal_id)
    ensure_private_directory(destination.parent)
    write_json(destination, proposal)
    return {"status": "prepared", "proposal": proposal, "path": str(destination)}


def known_import_external_keys(store: Store) -> set[tuple[str, str]]:
    """Read legacy model provenance only, for backwards-compatible collision checks."""
    keys: set[tuple[str, str]] = set()
    for model in store.rows["models"]:
        try:
            specs = strict_json_value(model["specs_json"], f"model {model['model_id']} specs")
        except (KeyError, InventoryError) as error:
            raise InventoryError("canonical model provenance is malformed") from error
        if not isinstance(specs, dict):
            raise InventoryError(f"model {model['model_id']} specs must be an object")
        provenances: list[object] = []
        if "import" in specs:
            provenances.append(specs["import"])
        history = specs.get("import_history", [])
        if not isinstance(history, list) or any(
            not isinstance(entry, dict) or set(entry) != {"import", "raw_fields"}
            for entry in history
        ):
            raise InventoryError(f"model {model['model_id']} import history is malformed")
        provenances.extend(entry["import"] for entry in history)
        for provenance in provenances:
            if not isinstance(provenance, dict):
                raise InventoryError(
                    f"model {model['model_id']} import provenance is malformed"
                )
            namespace = provenance.get("source_namespace")
            external_id = provenance.get("external_id")
            if external_id is None:
                continue
            if (
                not isinstance(namespace, str)
                or not namespace.strip()
                or not isinstance(external_id, str)
                or not external_id.strip()
            ):
                raise InventoryError(
                    f"model {model['model_id']} import provenance is malformed"
                )
            key = (namespace.strip(), external_id.strip())
            if key in keys:
                raise InventoryError("canonical model provenance contains duplicate external keys")
            keys.add(key)
    return keys


def generic_import_evidence_identity(store: Store, evidence_id: object) -> str | None:
    """Read unit identity only from structured evidence written by generic-import."""
    if not isinstance(evidence_id, str) or not any(
        event.get("evidence_id") == evidence_id
        and event.get("actor") == "generic-import"
        and event.get("event_type") in {"planned", "ordered"}
        for event in store.rows["inventory_events"]
    ):
        return None
    evidence = store.get("evidence", evidence_id)
    notes = evidence.get("notes")
    if not isinstance(notes, str):
        return None
    try:
        document = strict_json_value(notes, "generic import evidence")
    except InventoryError:
        return None
    if not isinstance(document, dict) or set(document) != {"generic_import"}:
        return None
    provenance = document["generic_import"]
    if not isinstance(provenance, dict) or set(provenance) != {
        "import",
        "raw_fields",
    }:
        return None
    import_record = provenance["import"]
    if not isinstance(import_record, dict):
        return None
    identity = import_record.get("source_unit_identity")
    if identity is None:
        return None
    if not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{64}", identity) is None:
        raise InventoryError("generic import evidence unit identity is malformed")
    return identity


def known_import_external_key_digests(store: Store) -> set[str]:
    """Read privacy-preserving external identities from structured evidence."""
    return {
        identity
        for evidence in store.rows["evidence"]
        if (identity := generic_import_evidence_identity(store, evidence["evidence_id"]))
        is not None
    }


def generic_import_unit_identity(args: argparse.Namespace) -> str | None:
    """Return the explicit per-source unit identity for a generic import operation."""
    if args.actor != "generic-import":
        return None
    identity = args.import_unit_identity
    if identity is None:
        return None
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise InventoryError("generic import source unit identity is malformed")
    return identity


def generic_import_item_unit_identity(store: Store, item: dict) -> str | None:
    """Read the persisted import unit identity from one candidate's primary evidence."""
    return generic_import_evidence_identity(store, item.get("primary_evidence_id"))


def stable_location_and_container(
    store: Store, location_id: str, container_id: str | None
) -> None:
    """Reject an unstable or incoherent physical location before staging a write."""
    location = store.get("locations", location_id)
    if location["kind"] == "unknown":
        raise InventoryError("physical discovery requires a stable, non-unknown location")
    if container_id is None:
        return
    container = store.get("locations", container_id)
    if container["kind"] not in CONTAINER_LOCATION_KINDS:
        raise InventoryError("physical discovery container has a non-container location kind")
    seen: set[str] = set()
    current: dict | None = container
    while current is not None:
        current_id = current["location_id"]
        if current_id in seen:
            raise InventoryError("location hierarchy contains a cycle")
        seen.add(current_id)
        if current_id == location_id:
            return
        parent = current.get("parent_location_id")
        current = store.get("locations", parent) if parent is not None else None
    raise InventoryError("physical discovery container is not within the stable location")


def location_context_sensitivity(
    store: Store, *location_ids: str | None
) -> str:
    """Return the maximum sensitivity across selected locations and their ancestors."""
    by_id = {row["location_id"]: row for row in store.rows["locations"]}
    sensitivities = ["low"]
    for location_id in location_ids:
        current = by_id.get(location_id)
        visited: set[str] = set()
        while current is not None:
            current_id = current["location_id"]
            if current_id in visited:
                raise InventoryError("location hierarchy contains a cycle")
            visited.add(current_id)
            sensitivities.append(current["sensitivity"])
            current = by_id.get(current.get("parent_location_id"))
    return max(sensitivities, key=SENSITIVITY_RANK.__getitem__)


def merge_generic_import_provenance(model: dict, args: argparse.Namespace) -> None:
    """Reject source provenance in model specs, which may be less sensitive than evidence."""
    if args.actor != "generic-import":
        return
    if args.specs != {}:
        raise InventoryError("generic import model provenance is malformed")


def read_bounded_regular_input(source: Path, *, maximum_bytes: int, label: str) -> bytes:
    """Read one stable regular-file generation through a no-follow descriptor."""
    lexical = Path(os.path.abspath(source.expanduser()))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(lexical, flags)
    except OSError as error:
        raise InventoryError(f"{label} must be a readable regular file: {error}") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise InventoryError(f"{label} must be a regular file")
        if details.st_size > maximum_bytes:
            raise InventoryError(f"{label} is too large")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum_bytes:
            raise InventoryError(f"{label} is too large")
        after = os.fstat(descriptor)
        try:
            named = os.stat(lexical, follow_symlinks=False)
        except OSError as error:
            raise InventoryError(f"{label} changed while it was read") from error
        identity = (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns)
        if (
            identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (named.st_dev, named.st_ino) != (details.st_dev, details.st_ino)
            or not stat.S_ISREG(named.st_mode)
        ):
            raise InventoryError(f"{label} changed while it was read")
        return payload
    except OSError as error:
        raise InventoryError(f"cannot read {label}: {error}") from error
    finally:
        os.close(descriptor)


def read_strict_json_file(source: Path, label: str) -> object:
    """Read one bounded stable file and decode duplicate-free finite JSON."""
    payload = read_bounded_regular_input(
        source,
        maximum_bytes=MAX_STRUCTURED_INPUT_BYTES,
        label=label,
    )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InventoryError(f"{label} is not UTF-8") from error
    return strict_json_value(text, label)


def command_import_propose(args: argparse.Namespace) -> dict:
    """Normalize a bounded source file into an ordinary, review-only proposal."""
    if args.scope != "private":
        raise InventoryError("import proposal preparation requires private scope")
    source = Path(os.path.abspath(args.input.expanduser()))
    payload = read_bounded_regular_input(
        source, maximum_bytes=16 * 1024 * 1024, label="import input"
    )
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
            try:
                store = load_verified_store(paths)
                normalized = normalize_import(
                    payload,
                    source_format=args.format,
                    source_name=args.source_name,
                    source_namespace=args.source_namespace,
                    imported_on=args.source_date,
                    known_external_keys=known_import_external_keys(store),
                    known_external_key_digests=known_import_external_key_digests(
                        store
                    ),
                    sensitivity=args.sensitivity,
                )
            except ImportError as error:
                raise InventoryError(f"cannot normalize import: {error}") from error
            return write_prepared_proposal(
                paths,
                args.runtime_dir,
                normalized.operation_lists(),
                {
                    "format": normalized.source_format,
                    "imported_on": normalized.imported_on,
                    "source_name": normalized.source_name,
                    "source_namespace": normalized.source_namespace,
                    "source_sha256": normalized.source_sha256,
                },
            )
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error


def validate_capture_proposal_input(args: argparse.Namespace, proposal: dict) -> None:
    capture = proposal.get("capture")
    if capture is None:
        return
    staging, artifact = read_capture_artifact(
        args.runtime_dir, capture["capture_session_id"]
    )
    artifact_path = (
        capture_staging_path(args.runtime_dir, artifact["capture_session_id"]) / "artifact.json"
    )
    if file_digest(artifact_path) != capture["artifact_sha256"]:
        raise InventoryError("capture staging artifact changed after proposal preparation")
    validate_capture_staging(staging, artifact, review_present=True)
    review_path, review = read_capture_review(
        args.runtime_dir, capture["capture_session_id"]
    )
    if (
        file_digest(review_path) != capture["review_sha256"]
        or review["artifact_sha256"] != capture["artifact_sha256"]
        or review["proposal_id"] != proposal["proposal_id"]
        or review["base_digest"] != proposal["base_digest"]
    ):
        raise InventoryError("capture review changed after proposal preparation")


def read_proposal(
    args: argparse.Namespace, *, require_prepared_inputs: bool = True
) -> tuple[Path, dict]:
    path = proposal_path(args.runtime_dir, args.proposal_id)
    if not path.is_file():
        raise InventoryError(f"proposal not found: {args.proposal_id}")
    proposal = strict_json_value(path.read_text(encoding="utf-8"), "proposal")
    if (
        not isinstance(proposal, dict)
        or proposal.get("proposal_id") != args.proposal_id
        or proposal.get("schema_version") != SCHEMA_VERSION
        or not isinstance(proposal.get("base_digest"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", proposal["base_digest"])
        or proposal.get("status") not in {"prepared", "applied"}
    ):
        raise InventoryError("proposal metadata is malformed or unsupported")
    validate_proposal_operations(proposal.get("operations"))
    capture = proposal.get("capture")
    if capture is not None:
        if (
            not isinstance(capture, dict)
            or set(capture)
            != {"artifact_sha256", "capture_session_id", "review_sha256"}
            or not isinstance(capture["artifact_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", capture["artifact_sha256"])
            or not isinstance(capture["review_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", capture["review_sha256"])
        ):
            raise InventoryError("capture proposal binding is malformed")
        if proposal["operations"] != [
            ["capture-commit", "--capture-session-id", capture["capture_session_id"]]
        ]:
            raise InventoryError("capture proposal operations disagree with staged artifact")
        if require_prepared_inputs:
            validate_capture_proposal_input(args, proposal)
    return path, proposal


def command_proposal_show(args: argparse.Namespace) -> dict:
    if args.scope != "private":
        raise InventoryError("proposal inspection requires private scope")
    path, proposal = read_proposal(args, require_prepared_inputs=False)
    if proposal.get("status") == "prepared":
        validate_capture_proposal_input(args, proposal)
    return {"path": str(path), "proposal": proposal}


def prepare_capture_media_transaction(
    paths: dict[str, Path],
    *,
    proposal_id: str,
    inventory_id: str,
    sources: list[tuple[str, Path]],
) -> None:
    """Journal exact media bytes before any file becomes visible in the live root."""
    if not sources:
        return
    media_root = paths["media_root"]
    if media_root is None:
        raise InventoryError("capture proposal requires a media root")
    journal_path = paths["capture_media_journal"]
    workspace = paths["capture_media_workspace"]
    if path_entry_exists(journal_path) or path_entry_exists(workspace):
        raise InventoryError("another capture media transaction requires recovery")
    digests = [digest for digest, _ in sources]
    if len(set(digests)) != len(digests):
        raise InventoryError("capture proposal has duplicate media digests")
    ensure_private_directory(workspace)
    entries: list[dict[str, object]] = []
    try:
        for digest, source in sources:
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise InventoryError("capture proposal has an invalid media digest")
            staged = checked_managed_path(workspace, Path(digest), "capture media workspace")
            durable_copy(source, staged)
            if file_digest(staged) != digest:
                raise InventoryError("capture media changed while entering the transaction")
            destination = media_asset_path(media_root, digest)
            preexisting = path_entry_exists(destination)
            if preexisting and (
                destination.is_symlink()
                or not destination.is_file()
                or file_digest(destination) != digest
            ):
                raise InventoryError("capture media destination is corrupt")
            entries.append({"digest": digest, "preexisting": preexisting})
        if os.environ.get("PROPERTY_INVENTORY_FAIL_CAPTURE_BEFORE_JOURNAL") == "1":
            os._exit(96)
        write_json(
            journal_path,
            {
                "entries": entries,
                "format": 1,
                "inventory_id": inventory_id,
                "media_root": str(media_root),
                "proposal_id": proposal_id,
            },
        )
    except BaseException:
        if not path_entry_exists(journal_path):
            shutil.rmtree(workspace, ignore_errors=True)
        raise
    created_count = 0
    for entry in entries:
        digest = str(entry["digest"])
        source = checked_managed_path(workspace, Path(digest), "capture media workspace")
        _, created = install_media(source, media_root, digest)
        if created:
            created_count += 1
            if (
                created_count == 1
                and os.environ.get("PROPERTY_INVENTORY_FAIL_CAPTURE_AFTER_MEDIA") == "1"
            ):
                os._exit(97)


def command_proposal_apply(args: argparse.Namespace) -> dict:
    """Apply a proposal while capture media, if any, has one outer lock owner."""
    if args.scope != "private":
        raise InventoryError("proposal application requires private scope")
    _, proposal = read_proposal(args, require_prepared_inputs=False)
    if args.media_root is None:
        if proposal.get("capture") is not None:
            raise InventoryError("capture proposal application requires a media root")
        return _command_proposal_apply_under_media_guard(args)
    try:
        with media_lock(args.media_root):
            return _command_proposal_apply_under_media_guard(args)
    except Timeout as error:
        raise InventoryError("another writer holds the media-root lock") from error


def _command_proposal_apply_under_media_guard(args: argparse.Namespace) -> dict:
    if args.scope != "private":
        raise InventoryError("proposal application requires private scope")
    path, proposal = read_proposal(args, require_prepared_inputs=False)
    if proposal.get("status") != "prepared":
        raise InventoryError(f"proposal is not prepared: {proposal.get('status')}")
    operations = validate_proposal_operations(proposal["operations"])
    operations_digest = hashlib.sha256(
        json.dumps(operations, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()

    def finish_runtime(result: dict) -> dict:
        capture = proposal.get("capture")
        if isinstance(capture, dict):
            canonical = Store(paths["store"])
            sessions = [
                row
                for row in canonical.rows["capture_sessions"]
                if row["capture_session_id"] == capture["capture_session_id"]
            ]
            if len(sessions) != 1 or sessions[0].get("provenance_state") != "bound":
                result["capture_staging_cleanup"] = "unsafe_retained"
            else:
                session = sessions[0]
                result["capture_staging_cleanup"] = retire_applied_capture_staging(
                    args.runtime_dir,
                    capture["capture_session_id"],
                    artifact_sha256=session["artifact_sha256"],
                    review_sha256=session["review_sha256"],
                    artifact_json=session["artifact_json"],
                    review_json=session["review_json"],
                )
        proposal["status"] = "applied"
        proposal["applied_at"] = datetime.now().astimezone().isoformat()
        proposal["result"] = result["result"]
        write_json(path, proposal)
        return result

    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
            current = load_verified_store(paths)
            committed = [
                row
                for row in current.rows["proposal_commits"]
                if row["proposal_id"] == args.proposal_id
            ]
            if committed:
                marker = committed[0]
                if (
                    marker["base_digest"] != proposal["base_digest"]
                    or marker["operations_digest"] != operations_digest
                ):
                    raise InventoryError("canonical proposal receipt disagrees with proposal")
                checks = verify_bundle(
                    paths,
                    paths["store"],
                    paths["database"],
                    paths["catalogue"],
                )
                return finish_runtime(
                    {
                        "status": "recovered_applied",
                        "operation": f"apply-{args.proposal_id}",
                        "backup": None,
                        "result": {
                            "proposal_id": args.proposal_id,
                            "operations": [],
                            "recovered_as_already_applied": True,
                        },
                        "checks": checks,
                    }
                )
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error

    def mutate(store: Store) -> dict:
        committed = [
            row for row in store.rows["proposal_commits"] if row["proposal_id"] == args.proposal_id
        ]
        if committed:
            marker = committed[0]
            if (
                marker["base_digest"] != proposal["base_digest"]
                or marker["operations_digest"] != operations_digest
            ):
                raise InventoryError("canonical proposal receipt disagrees with proposal")
            return {
                "proposal_id": args.proposal_id,
                "operations": [],
                "recovered_as_already_applied": True,
            }
        validate_capture_proposal_input(args, proposal)
        live_digest = canonical_store_digest(store.store_dir)
        if live_digest != proposal["base_digest"]:
            raise InventoryError("proposal is stale because the canonical store changed")
        outputs = []
        with tempfile.TemporaryDirectory(prefix="property-inventory-proposal-") as temp_name:
            temp = Path(temp_name)
            sandbox_root = temp / "inventory"
            sandbox_store = sandbox_root / "Data" / "store"
            store.save(sandbox_store)
            copy_validated_auxiliary_data(paths["data"], sandbox_root / "Data")
            sandbox_runtime = temp / "runtime"
            has_capture = proposal.get("capture") is not None
            sandbox_media = temp / "media" if has_capture else args.media_root
            if has_capture:
                assert args.media_root is not None
                for asset in store.rows["media_assets"]:
                    materialize_verified_existing_media(
                        args.media_root, sandbox_media, asset
                    )
            sandbox_catalogue = temp / "catalogue" / "Inventory.md"
            sandbox_installation_id = str(uuid.uuid4())
            write_json(
                sandbox_root / RUNTIME_BINDING,
                runtime_binding_payload(sandbox_runtime, sandbox_installation_id),
            )
            write_inventory_gitignore(sandbox_root)
            claim_runtime_owner(
                sandbox_root,
                sandbox_runtime,
                sandbox_media if args.media_root is not None else None,
                sandbox_catalogue,
                sandbox_installation_id,
                store.rows["metadata"][0]["inventory_id"],
            )
            sandbox_environment = dict(os.environ)
            for name in (
                "PROPERTY_INVENTORY_CATALOGUE_OUTPUT",
                "PROPERTY_INVENTORY_CATALOGUE_SCOPE",
                "PROPERTY_INVENTORY_CONFIG",
                "PROPERTY_INVENTORY_INSTANCE",
                "PROPERTY_INVENTORY_ROOT",
                "PROPERTY_INVENTORY_RUNTIME",
                "PROPERTY_INVENTORY_MEDIA_ROOT",
                "PROPERTY_INVENTORY_SCOPE",
            ):
                sandbox_environment.pop(name, None)
            for number, operation in enumerate(operations, start=1):
                sandbox_operation = list(operation)
                if sandbox_operation[0] == "capture-commit":
                    capture = proposal.get("capture")
                    if not isinstance(capture, dict):
                        raise InventoryError("capture operation requires a bound capture proposal")
                    source_staging, source_artifact = read_capture_artifact(
                        args.runtime_dir, capture["capture_session_id"]
                    )
                    target_staging = capture_staging_path(
                        sandbox_runtime, capture["capture_session_id"]
                    )
                    ensure_private_directory(target_staging.parent)
                    copy_validated_capture_staging(
                        source_staging,
                        target_staging,
                        source_artifact,
                    )
                    if (
                        file_digest(target_staging / "artifact.json")
                        != capture["artifact_sha256"]
                        or file_digest(target_staging / "review.json")
                        != capture["review_sha256"]
                    ):
                        raise InventoryError(
                            "capture staging changed while proposal inputs were materialized"
                        )
                    instance_token = _INSTANCE_PATHS.set(
                        (sandbox_media, sandbox_catalogue, args.catalogue_scope)
                    )
                    try:
                        outputs.append(
                            command_capture_commit(
                                argparse.Namespace(
                                    _proposal_materialization=True,
                                    _proposal_id=args.proposal_id,
                                    _proposal_base_digest=proposal["base_digest"],
                                    _proposal_operations_digest=operations_digest,
                                    capture_session_id=capture["capture_session_id"],
                                    continue_batch=False,
                                    inventory_root=sandbox_root,
                                    media_root=sandbox_media,
                                    runtime_dir=sandbox_runtime,
                                    scope="private",
                                )
                            )
                        )
                    finally:
                        _INSTANCE_PATHS.reset(instance_token)
                    continue
                if (
                    sandbox_operation[0] == "import-floorplan"
                    and "--document-json" in sandbox_operation
                ):
                    document_index = sandbox_operation.index("--document-json")
                    if document_index + 1 >= len(sandbox_operation):
                        raise InventoryError(
                            f"proposal operation {number} has malformed inline floor-plan content"
                        )
                    document = strict_json_value(
                        sandbox_operation[document_index + 1],
                        f"proposal operation {number} floor plan",
                    )
                    input_path = temp / "proposal-inputs" / f"floorplan-{number}.geojson"
                    input_path.parent.mkdir()
                    write_json(input_path, document)
                    sandbox_operation[document_index : document_index + 2] = [
                        "--input",
                        str(input_path),
                    ]
                command = [
                    sys.executable,
                    "-m",
                    "property_inventory.cli",
                    "--inventory-root",
                    str(sandbox_root),
                    "--runtime-dir",
                    str(sandbox_runtime),
                    "--catalogue-output",
                    str(sandbox_catalogue),
                    "--catalogue-scope",
                    args.catalogue_scope,
                    "--scope",
                    args.scope,
                ]
                if args.media_root is not None:
                    command.extend(("--media-root", str(sandbox_media)))
                command.extend(sandbox_operation)
                completed = subprocess.run(
                    command,
                    cwd=temp,
                    text=True,
                    capture_output=True,
                    check=False,
                    env=sandbox_environment,
                )
                if completed.returncode:
                    detail = completed.stderr.strip() or completed.stdout.strip()
                    raise InventoryError(f"proposal operation {number} failed: {detail}")
                outputs.append(json.loads(completed.stdout))
            applied = Store(sandbox_store)
            if args.media_root is not None:
                before = {row["sha256"] for row in store.rows["media_assets"]}
                media_sources: list[tuple[str, Path]] = []
                for asset in applied.rows["media_assets"]:
                    digest = asset["sha256"]
                    if digest in before:
                        continue
                    source = media_asset_path(sandbox_media, digest)
                    media_sources.append((digest, source))
                if not has_capture and media_sources:
                    raise InventoryError(
                        "non-capture proposal changed canonical media rows"
                    )
                prepare_capture_media_transaction(
                    paths,
                    proposal_id=args.proposal_id,
                    inventory_id=store.rows["metadata"][0]["inventory_id"],
                    sources=media_sources,
                )
            store.rows = applied.rows
        receipts = [
            row
            for row in store.rows["proposal_commits"]
            if row["proposal_id"] == args.proposal_id
        ]
        if not receipts:
            store.rows["proposal_commits"].append(
                {
                    "applied_at": datetime.now().astimezone().isoformat(),
                    "base_digest": proposal["base_digest"],
                    "operations_digest": operations_digest,
                    "proposal_id": args.proposal_id,
                }
            )
        elif len(receipts) != 1 or (
            receipts[0]["base_digest"] != proposal["base_digest"]
            or receipts[0]["operations_digest"] != operations_digest
        ):
            raise InventoryError("capture proposal receipt disagrees with application")
        return {
            "proposal_id": args.proposal_id,
            "operations": outputs,
            "recovered_as_already_applied": False,
        }

    result = transaction(
            args.inventory_root,
            args.runtime_dir,
            f"apply-{args.proposal_id}",
            mutate,
            continue_batch=args.continue_batch,
            finalize_locked=(
                lambda: recover_pending_capture_media(paths)
                if proposal.get("capture") is not None
                else None
            ),
        )
    if os.environ.get("PROPERTY_INVENTORY_FAIL_PROPOSAL_AFTER_COMMIT") == "1":
        os._exit(99)
    return finish_runtime(result)


def canonical_spatial_profile(profile: object) -> tuple[str, str, dict]:
    """Validate and canonicalise the two persisted, measured space shapes."""
    try:
        canonical = normalize_spatial_profile(profile)
    except SpatialValidationError as error:
        raise InventoryError(f"invalid spatial profile: {error}") from error
    kind = canonical["kind"]
    return (
        kind,
        json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        canonical,
    )


def spatial_profile_id(*parts: str) -> str:
    """Make a stable profile key without exposing source path or geometry bytes."""
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"spatial-{digest}"


def require_spatial_parent_sensitivity(
    store: Store, location_id: str, evidence_id: str, sensitivity: str
) -> None:
    store.get("locations", location_id)
    evidence = store.get("evidence", evidence_id)
    expected_claim = SPATIAL_EVIDENCE_CLAIMS.get(evidence["evidence_type"])
    if expected_claim is None or evidence["claim_strength"] != expected_claim:
        raise InventoryError(
            "spatial profiles require physical_check/explicit_current or "
            "research-or-vault_note/research_only evidence"
        )
    required = max(
        (location_context_sensitivity(store, location_id), evidence["sensitivity"]),
        key=SENSITIVITY_RANK.__getitem__,
    )
    if SENSITIVITY_RANK[sensitivity] < SENSITIVITY_RANK[required]:
        raise InventoryError(
            "spatial profile sensitivity must be at least the selected location and evidence sensitivity"
        )


def spatial_source_evidence_id(
    *, source_ref: str, captured_on: str, evidence_type: str, sensitivity: str
) -> str:
    digest = hashlib.sha256(
        "\0".join((source_ref, captured_on, evidence_type, sensitivity)).encode("utf-8")
    ).hexdigest()[:24]
    return f"ev-space-{digest}"


def resolve_spatial_evidence(store: Store, args: argparse.Namespace) -> tuple[str, bool]:
    """Use an existing record or create one source record without item coupling."""
    has_existing = args.evidence_id is not None
    has_source = args.source_ref is not None or args.captured_on is not None
    if has_existing and has_source:
        raise InventoryError(
            "use either --evidence-id or --source-ref with --captured-on, not both"
        )
    if has_existing:
        store.get("evidence", args.evidence_id)
        return args.evidence_id, False
    if args.source_ref is None or args.captured_on is None:
        raise InventoryError("provide --evidence-id or both --source-ref and --captured-on")
    source_ref = args.source_ref.strip()
    if not source_ref:
        raise InventoryError("--source-ref must not be blank")
    evidence_id = spatial_source_evidence_id(
        source_ref=source_ref,
        captured_on=args.captured_on,
        evidence_type=args.evidence_type,
        sensitivity=args.sensitivity,
    )
    row = {
        "captured_on": args.captured_on,
        "claim_strength": "research_only",
        "evidence_id": evidence_id,
        "evidence_type": args.evidence_type,
        "notes": args.evidence_notes,
        "sensitivity": args.sensitivity,
        "source_ref": source_ref,
    }
    existing = [
        candidate for candidate in store.rows["evidence"] if candidate["evidence_id"] == evidence_id
    ]
    if existing:
        if existing[0] != row:
            raise InventoryError("spatial source evidence already exists with different content")
        return evidence_id, False
    store.rows["evidence"].append(row)
    return evidence_id, True


def upsert_spatial_profile(
    store: Store,
    *,
    profile_id: str,
    location_id: str,
    profile_json: str,
    evidence_id: str,
    sensitivity: str,
    notes: str | None,
) -> bool:
    """Reuse an exact deterministic row and reject an asserted-space conflict."""
    require_spatial_parent_sensitivity(store, location_id, evidence_id, sensitivity)
    row = {
        "evidence_id": evidence_id,
        "location_id": location_id,
        "notes": notes,
        "profile_id": profile_id,
        "profile_json": profile_json,
        "sensitivity": sensitivity,
    }
    existing = [
        candidate
        for candidate in store.rows["spatial_profiles"]
        if candidate["profile_id"] == profile_id
    ]
    if existing:
        if existing[0] != row:
            raise InventoryError("spatial profile already exists with different checked content")
        return True
    store.rows["spatial_profiles"].append(row)
    return False


def command_add_space(args: argparse.Namespace) -> dict:
    kind, profile_json, _profile = canonical_spatial_profile(args.profile)
    if kind != "container_box":
        raise InventoryError("add-space accepts only a checked container_box profile")
    profile_id = spatial_profile_id("container_box", args.location_id)

    def mutate(store: Store) -> dict:
        evidence_id, evidence_created = resolve_spatial_evidence(store, args)
        reused = upsert_spatial_profile(
            store,
            profile_id=profile_id,
            location_id=args.location_id,
            profile_json=profile_json,
            evidence_id=evidence_id,
            sensitivity=args.sensitivity,
            notes=args.notes,
        )
        return {
            "evidence_created": evidence_created,
            "evidence_id": evidence_id,
            "profile_id": profile_id,
            "reused": reused,
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"add-space-{args.location_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_import_floorplan(args: argparse.Namespace) -> dict:
    if args.document_json is not None:
        document = strict_json_value(args.document_json, "inline floor plan")
        operation_label = "inline"
    else:
        document = read_strict_json_file(args.input, "floor-plan input")
        operation_label = args.input.stem
    has_source = args.source_ref is not None or args.captured_on is not None
    if args.evidence_id is not None and has_source:
        raise InventoryError(
            "use either --evidence-id or --source-ref with --captured-on, not both"
        )
    if has_source and (args.source_ref is None or args.captured_on is None):
        raise InventoryError("--source-ref and --captured-on must be supplied together")
    if has_source:
        source_ref = args.source_ref.strip()
        if not source_ref:
            raise InventoryError("--source-ref must not be blank")
        shared_evidence_id = spatial_source_evidence_id(
            source_ref=source_ref,
            captured_on=args.captured_on,
            evidence_type=args.evidence_type,
            sensitivity=args.sensitivity,
        )
    else:
        shared_evidence_id = args.evidence_id
    if not isinstance(document, dict) or not isinstance(document.get("features"), list):
        raise InventoryError("invalid floor plan: floor plan must be a GeoJSON FeatureCollection")
    # Source-created evidence is deliberately shared by the batch. Existing per-feature
    # evidence remains supported, but it cannot be silently mixed with this mode.
    prepared_document = json.loads(json.dumps(document))
    for feature in prepared_document["features"]:
        if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
            continue
        properties = feature["properties"]
        supplied = properties.get("evidence_id")
        if shared_evidence_id is not None:
            if supplied is not None and supplied != shared_evidence_id:
                raise InventoryError(
                    "floor plan feature evidence_id conflicts with the shared evidence mode"
                )
            properties["evidence_id"] = shared_evidence_id
    try:
        plan = parse_geojson_floor_plan(prepared_document)
    except SpatialValidationError as error:
        raise InventoryError(f"invalid floor plan: {error}") from error
    features_by_id = {
        str(feature.get("id", feature.get("properties", {}).get("feature_id"))): feature
        for feature in prepared_document["features"]
        if isinstance(feature, dict) and isinstance(feature.get("properties"), dict)
    }
    specifications: list[tuple[str, str, str, str, str]] = []
    for feature in plan.features:
        source = features_by_id.get(feature.feature_id)
        properties = source.get("properties") if source is not None else None
        if not isinstance(properties, dict):
            raise InventoryError(
                f"floor plan feature {feature.feature_id} properties are malformed"
            )
        location_id = properties.get("location_id")
        sensitivity = properties.get("sensitivity")
        if not isinstance(location_id, str) or not location_id:
            raise InventoryError(
                f"floor plan feature {feature.feature_id} needs an explicit location_id"
            )
        if sensitivity not in SENSITIVITY_RANK:
            raise InventoryError(
                f"floor plan feature {feature.feature_id} needs a valid explicit sensitivity"
            )
        _kind, profile_json, _profile = canonical_spatial_profile(
            {
                "kind": "floor_rectangle",
                "x": feature.rectangle.x,
                "y": feature.rectangle.y,
                "width": feature.rectangle.width,
                "height": feature.rectangle.height,
                "unit": feature.rectangle.unit,
            }
        )
        specifications.append(
            (
                spatial_profile_id("floor_rectangle", location_id, feature.feature_id),
                location_id,
                profile_json,
                feature.evidence_id,
                sensitivity,
            )
        )

    def mutate(store: Store) -> dict:
        if has_source or args.evidence_id is not None:
            shared_evidence, evidence_created = resolve_spatial_evidence(store, args)
        else:
            shared_evidence, evidence_created = None, False
        imported = []
        for profile_id, location_id, profile_json, evidence_id, sensitivity in specifications:
            if shared_evidence is not None and evidence_id != shared_evidence:
                raise InventoryError(
                    "floor plan feature evidence_id conflicts with the shared evidence mode"
                )
            reused = upsert_spatial_profile(
                store,
                profile_id=profile_id,
                location_id=location_id,
                profile_json=profile_json,
                evidence_id=evidence_id,
                sensitivity=sensitivity,
                notes=None,
            )
            imported.append({"profile_id": profile_id, "reused": reused})
        result = {"profiles": imported}
        if shared_evidence is not None:
            result["evidence_id"] = shared_evidence
            result["evidence_created"] = evidence_created
        return result

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"import-floorplan-{operation_label}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_add_item_dimensions(args: argparse.Namespace) -> dict:
    """Append one evidence-backed item measurement while retaining older readings."""
    measurements = {
        "depth": args.depth,
        "height": args.height,
        "width": args.width,
    }
    if not any(value is not None for value in measurements.values()):
        raise InventoryError("at least one item dimension must be supplied")

    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        evidence = store.get("evidence", args.evidence_id)
        if not evidence_supports_item(store, args.evidence_id, args.item_id):
            raise InventoryError("dimension evidence must already support the item")
        required_sensitivity = max(
            (item["sensitivity"], evidence["sensitivity"]),
            key=SENSITIVITY_RANK.__getitem__,
        )
        if SENSITIVITY_RANK[args.sensitivity] < SENSITIVITY_RANK[required_sensitivity]:
            raise InventoryError("dimension sensitivity is lower than its item or evidence")
        identity = strict_json_dumps(
            {
                "evidence_id": args.evidence_id,
                "item_id": args.item_id,
                "measured_on": args.measured_on,
                "measurements": measurements,
                "unit": args.unit,
            },
            sort_keys=True,
        )
        dimension_id = "dim-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        requested = {
            "depth": args.depth,
            "dimension_id": dimension_id,
            "evidence_id": args.evidence_id,
            "height": args.height,
            "item_id": args.item_id,
            "measured_on": args.measured_on,
            "notes": args.notes,
            "recorded_at": None,
            "sensitivity": args.sensitivity,
            "unit": args.unit,
            "width": args.width,
        }
        existing = [
            row
            for row in store.rows["item_dimensions"]
            if row["dimension_id"] == dimension_id
        ]
        if existing:
            comparable = {**requested, "recorded_at": existing[0].get("recorded_at")}
            if len(existing) != 1 or existing[0] != comparable:
                raise InventoryError("dimension identity collides with different metadata")
            return {"dimension_id": dimension_id, "reused": True}
        requested["recorded_at"] = recorded_timestamp(args.recorded_at)
        store.rows["item_dimensions"].append(requested)
        return {"dimension_id": dimension_id, "reused": False}

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"add-item-dimensions-{args.item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def visible_spatial_profiles(store: Store, location_id: str, scope: str) -> list[dict]:
    locations = {row["location_id"]: row for row in store.rows["locations"]}
    location = locations.get(location_id)
    if location is None or not location_scope_allows(locations, location, scope):
        return []
    evidence = {row["evidence_id"]: row for row in store.rows["evidence"]}
    visible = []
    for row in store.rows["spatial_profiles"]:
        if row["location_id"] != location_id or not scope_allows(scope, row["sensitivity"]):
            continue
        supporting = evidence.get(row["evidence_id"])
        if supporting is None or not scope_allows(scope, supporting["sensitivity"]):
            continue
        _kind, _profile_json, profile = canonical_spatial_profile(json.loads(row["profile_json"]))
        profile.update(
            {
                "evidence": {
                    key: supporting[key]
                    for key in (
                        "captured_on",
                        "claim_strength",
                        "evidence_id",
                        "evidence_type",
                        "sensitivity",
                        "source_ref",
                    )
                },
                "notes": row["notes"],
                "profile_id": row["profile_id"],
                "sensitivity": row["sensitivity"],
            }
        )
        if scope != "private":
            profile["evidence"] = {
                **profile["evidence"],
                "evidence_id": None,
                "source_ref": None,
            }
            profile["notes"] = None
        visible.append(profile)
    return sorted(visible, key=lambda profile: profile["profile_id"])


def command_space(args: argparse.Namespace) -> dict:
    profiles = visible_spatial_profiles(read_retrieval_store(args), args.location_id, args.scope)
    if not profiles:
        return {"status": "unknown", "reason": "space_not_visible_or_not_recorded"}
    return {
        "status": "known",
        "location_id": args.location_id,
        "profiles": profiles,
        "reason": "checked_visible_spatial_profiles",
    }


def visible_container_box(store: Store, location_id: str, scope: str) -> dict | None:
    boxes = [
        profile
        for profile in visible_spatial_profiles(store, location_id, scope)
        if profile["kind"] == "container_box"
    ]
    if len(boxes) != 1:
        return None
    return boxes[0]


def current_visible_item_dimensions(
    store: Store, item_id: str, scope: str
) -> dict | None:
    """Compose the latest visible value for each axis with per-axis provenance."""
    items = {
        row["item_id"]: row
        for row in store.rows["items"]
        if scope_allows(scope, row["sensitivity"])
    }
    if item_id not in items:
        return None
    evidence = {row["evidence_id"]: row for row in store.rows["evidence"]}
    readings = []
    for row in store.rows["item_dimensions"]:
        if row["item_id"] != item_id or not scope_allows(scope, row["sensitivity"]):
            continue
        supporting = evidence.get(row["evidence_id"])
        if supporting is None or not scope_allows(scope, supporting["sensitivity"]):
            continue
        readings.append(row)
    if not readings:
        return None
    selected: dict[str, dict] = {}
    for axis in ("width", "height", "depth"):
        candidates = [row for row in readings if row[axis] is not None]
        if candidates:
            latest = max(
                candidates,
                key=lambda row: (
                    row["measured_on"],
                    row["recorded_at"],
                    row["dimension_id"],
                ),
            )
            selected[axis] = latest
    if not selected:
        return None
    newest = max(
        selected.values(),
        key=lambda row: (
            row["measured_on"],
            row["recorded_at"],
            row["dimension_id"],
        ),
    )
    target_unit = newest["unit"]
    provenance = {
        axis: {
            "dimension_id": row["dimension_id"],
            "evidence_id": row["evidence_id"] if scope == "private" else None,
            "measured_on": row["measured_on"],
            "unit": row["unit"],
        }
        for axis, row in selected.items()
    }
    return {
        "depth": (
            convert_length(selected["depth"]["depth"], selected["depth"]["unit"], target_unit)
            if "depth" in selected
            else None
        ),
        "evidence_id": None,
        "height": (
            convert_length(
                selected["height"]["height"], selected["height"]["unit"], target_unit
            )
            if "height" in selected
            else None
        ),
        "item_id": item_id,
        "measured_on": newest["measured_on"],
        "provenance": provenance,
        "unit": target_unit,
        "width": (
            convert_length(
                selected["width"]["width"], selected["width"]["unit"], target_unit
            )
            if "width" in selected
            else None
        ),
    }


def command_fit(args: argparse.Namespace) -> dict:
    store = read_retrieval_store(args)
    dimensions = args.item_dimensions
    if args.item_id is not None:
        visible_item = next(
            (
                item
                for item in store.rows["items"]
                if item["item_id"] == args.item_id
                and scope_allows(args.scope, item["sensitivity"])
            ),
            None,
        )
        if visible_item is None:
            return {"status": "unknown", "reason": "item_not_visible_or_not_recorded"}
        availability = possession_availability(store, visible_item, args.scope)
        if not availability["available"]:
            return {
                "status": "unknown",
                "reason": "item_not_operationally_available",
                "item_id": args.item_id,
                "availability": availability,
            }
        dimensions = current_visible_item_dimensions(store, args.item_id, args.scope)
        if dimensions is None or any(
            dimensions[field] is None for field in ("width", "height", "depth")
        ):
            return {
                "status": "unknown",
                "reason": "item_dimensions_not_visible_or_incomplete",
                "item_id": args.item_id,
            }
    # Parse caller data first. A malformed supplied measurement remains an error even
    # when a protected container cannot be disclosed.
    try:
        spatial_fit(dimensions, None)
    except SpatialValidationError as error:
        raise InventoryError(f"invalid item dimensions: {error}") from error
    container = visible_container_box(store, args.location_id, args.scope)
    if container is None:
        return {"status": "unknown", "reason": "container_box_not_visible_or_not_recorded"}
    try:
        result = spatial_fit(dimensions, container, allow_rotation=not args.no_rotation)
    except SpatialValidationError as error:
        raise InventoryError(f"invalid spatial fit input: {error}") from error
    return {
        "status": result.status,
        "reason": result.reason,
        "rotation": list(result.rotation) if result.rotation is not None else None,
        "container_profile": container,
        "item_dimensions": dimensions if args.item_id is not None else None,
    }


def command_pack(args: argparse.Namespace) -> dict:
    store = read_retrieval_store(args)
    items = args.items
    if args.item_ids:
        items = []
        unknown_item_ids = []
        unavailable_items = []
        for item_id in args.item_ids:
            visible_item = next(
                (
                    item
                    for item in store.rows["items"]
                    if item["item_id"] == item_id
                    and scope_allows(args.scope, item["sensitivity"])
                ),
                None,
            )
            if visible_item is None:
                unknown_item_ids.append(item_id)
                continue
            availability = possession_availability(store, visible_item, args.scope)
            if not availability["available"]:
                unavailable_items.append(
                    {"item_id": item_id, "availability": availability}
                )
                continue
            dimensions = current_visible_item_dimensions(store, item_id, args.scope)
            if dimensions is None or any(
                dimensions[field] is None for field in ("width", "height", "depth")
            ):
                unknown_item_ids.append(item_id)
                continue
            items.append(
                {
                    "dimensions": {
                        key: dimensions[key]
                        for key in ("depth", "evidence_id", "height", "unit", "width")
                    },
                    "item_id": item_id,
                }
            )
        if unavailable_items:
            return {
                "status": "unknown",
                "reason": "items_not_operationally_available",
                "unavailable_items": unavailable_items,
            }
        if unknown_item_ids:
            return {
                "status": "unknown",
                "reason": "item_dimensions_not_visible_or_incomplete",
                "unknown_item_ids": unknown_item_ids,
            }
    # This validates every supplied item, including partial measurements, before a
    # hidden container can select the unknown path.
    try:
        spatial_pack({"width": 1, "height": 1, "depth": 1, "unit": "m"}, items)
    except SpatialValidationError as error:
        raise InventoryError(f"invalid packing items: {error}") from error
    container = visible_container_box(store, args.location_id, args.scope)
    if container is None:
        return {"status": "unknown", "reason": "container_box_not_visible_or_not_recorded"}
    try:
        result = spatial_pack(container, items, allow_rotation=not args.no_rotation)
    except SpatialValidationError as error:
        raise InventoryError(f"invalid spatial packing input: {error}") from error
    return {
        "status": result.status,
        "reason": result.reason,
        "placements": [
            {
                "box": {
                    "depth": placement.box.depth,
                    "height": placement.box.height,
                    "unit": placement.box.unit,
                    "width": placement.box.width,
                    "x": placement.box.x,
                    "y": placement.box.y,
                    "z": placement.box.z,
                },
                "item_id": placement.item_id,
                "rotation": list(placement.rotation),
            }
            for placement in result.placements
        ],
        "unplaced_item_ids": list(result.unplaced_item_ids),
        "container_profile": container,
        "item_dimensions": {
            item_id: current_visible_item_dimensions(store, item_id, args.scope)
            for item_id in args.item_ids
        }
        if args.item_ids
        else None,
    }


def command_free_volume(args: argparse.Namespace) -> dict:
    """Calculate remaining checked container volume from positioned occupied boxes."""
    store = read_retrieval_store(args)
    container = visible_container_box(store, args.location_id, args.scope)
    if container is None:
        return {
            "status": "unknown",
            "reason": "container_box_not_visible_or_not_recorded",
        }
    try:
        result = spatial_free_volume(container, args.occupied_box)
    except SpatialValidationError as error:
        raise InventoryError(f"invalid occupied geometry: {error}") from error
    return {
        "status": result.status,
        "reason": result.reason,
        "free_volume": result.volume,
        "unit": result.unit,
        "container_profile": container,
        "occupied_boxes": args.occupied_box,
    }


def retrieval_filters(args: argparse.Namespace) -> dict[str, str | None]:
    return {
        "category": args.category,
        "ownership_state": args.ownership_state,
        "condition": args.condition,
        "location": args.location,
        "tag": args.tag,
        "alias_kind": args.alias_kind,
        "interface_family": args.interface_family,
        "interface_standard": args.interface_standard,
        "interface_variant": args.interface_variant,
        "interface_direction": args.interface_direction,
        "location_known": args.location_known,
    }


def read_retrieval_store(args: argparse.Namespace) -> Store:
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        lock = inventory_lock(args.inventory_root)
        lock.acquire()
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error
    try:
        recover_pending_transaction(paths)
        return load_verified_store(paths)
    finally:
        lock.release()


def command_search(args: argparse.Namespace) -> dict:
    result = retrieve_search(
        read_retrieval_store(args),
        query=args.query,
        scope=args.scope,
        limit=args.limit,
        filters=retrieval_filters(args),
        cursor=args.cursor,
    )
    if not args.summary:
        return result
    summary = {
        "matching_record_found": result["recorded"],
        "count": result["count"],
        "matches": [
            {
                "name": match["model"]["name"],
                "ownership": match["item"]["ownership_state"],
                "condition": match["item"]["condition"],
                "location": match["location"],
                "location_path": " / ".join(
                    step["name"] for step in match["location_path"]
                ),
                "last_physical_check_on": match["item"]["verified_on"],
                "evidence_types": sorted(
                    {evidence["evidence_type"] for evidence in match["evidence"]}
                ),
            }
            for match in result["matches"]
        ],
        "next_cursor": result["next_cursor"],
        "page_count": result["page_count"],
        "truncated": result["truncated"],
    }
    if not result["recorded"]:
        summary = {"meaning": result["meaning_if_empty"], **summary}
    return summary


def command_list_items(args: argparse.Namespace) -> dict:
    """Enumerate every scope-visible item satisfying explicit typed filters."""
    return retrieve_search(
        read_retrieval_store(args),
        query=(),
        scope=args.scope,
        limit=args.limit,
        filters=retrieval_filters(args),
        cursor=args.cursor,
    )


def command_locations(args: argparse.Namespace) -> dict:
    """List or resolve visible physical areas without leaking protected locations."""
    if args.limit < 1 or args.limit > 500:
        raise InventoryError("limit must be between 1 and 500")
    store = read_retrieval_store(args)
    locations = {row["location_id"]: row for row in store.rows["locations"]}
    query_tokens = [
        token for token in re.findall(r"[^\W_]+", (args.query or "").casefold()) if token
    ]
    visible = []
    for row in sorted(store.rows["locations"], key=lambda value: value["location_id"]):
        if not location_scope_allows(locations, row, args.scope):
            continue
        if args.kind is not None and row["kind"] != args.kind:
            continue
        if args.parent_location_id is not None and row.get(
            "parent_location_id"
        ) != args.parent_location_id:
            continue
        chain = []
        current: dict | None = row
        visited: set[str] = set()
        while current is not None and current["location_id"] not in visited:
            visited.add(current["location_id"])
            chain.append(
                {
                    "kind": current["kind"],
                    "location_id": current["location_id"],
                    "name": current["name"],
                }
            )
            current = locations.get(current.get("parent_location_id"))
        path = list(reversed(chain))
        path_text = " / ".join(step["name"] for step in path)
        # Matching reads the whole root-to-leaf path, so "riverside third drawer"
        # finds the drawer without the caller knowing the intermediate names.
        searchable = " ".join(
            (
                row["location_id"],
                row["name"],
                row["kind"],
                *(step["location_id"] for step in path),
                *(step["name"] for step in path),
            )
        ).casefold()
        if query_tokens and not all(token in searchable for token in query_tokens):
            continue
        record = {
            "chain": chain,
            "kind": row["kind"],
            "location_id": row["location_id"],
            "name": row["name"],
            "parent_location_id": row.get("parent_location_id"),
            "path": path,
            "path_text": path_text,
        }
        if args.scope == "private":
            record["notes"] = row.get("notes")
        visible.append(record)
    total_count = len(visible)
    fingerprint = page_fingerprint(
        "locations",
        {
            "kind": args.kind,
            "parent_location_id": args.parent_location_id,
            "query": query_tokens,
            "scope": args.scope,
        },
        [row["location_id"] for row in visible],
    )
    after = decode_page_cursor(args.cursor, "locations", fingerprint)
    remaining = [
        row for row in visible if after is None or row["location_id"] > after
    ]
    matches = remaining[: args.limit]
    next_cursor = (
        encode_page_cursor("locations", fingerprint, matches[-1]["location_id"])
        if len(remaining) > args.limit and matches
        else None
    )
    return {
        "count": total_count,
        "matches": matches,
        "meaning_if_empty": "unknown, not absent",
        "next_cursor": next_cursor,
        "page_count": len(matches),
        "query": query_tokens,
        "truncated": next_cursor is not None,
    }


def command_context(args: argparse.Namespace) -> dict:
    return retrieve_task_context(
        read_retrieval_store(args),
        task=args.task,
        scope=args.scope,
        limit=args.limit,
        filters=retrieval_filters(args),
        cursor=args.cursor,
    )


def command_show(args: argparse.Namespace) -> dict:
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        lock = inventory_lock(args.inventory_root)
        lock.acquire()
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error
    try:
        recover_pending_transaction(paths)
        store = load_verified_store(paths)
        item = resolve_item_reference(store, args.item_id, scope=args.scope)
        return item_context(store, item, scope=args.scope)
    finally:
        lock.release()


def command_status(args: argparse.Namespace) -> dict:
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        lock = inventory_lock(args.inventory_root)
        lock.acquire()
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error
    try:
        recovery: object = "unknown"
        try:
            recovery = recover_pending_transaction(paths)
            result = verify_bundle(
                paths,
                paths["store"],
                paths["database"],
                paths["catalogue"],
            )
        except (
            InventoryError,
            OSError,
            UnicodeError,
            sqlite3.Error,
            json.JSONDecodeError,
        ):
            if args.scope == "private":
                raise
            if args.summary:
                return {
                    "integrity_gate": "unhealthy",
                    "scope": args.scope,
                    "verification_failures": None,
                    "foreign_key_failures": None,
                }
            return {
                "status": "unhealthy",
                "scope": args.scope,
                "store_valid": False,
                "recovery": "unknown",
            }
        result["recovery"] = recovery
        if args.scope != "private":
            if args.summary:
                return {
                    "integrity_gate": result["status"],
                    "scope": args.scope,
                    "verification_failures": None,
                    "foreign_key_failures": None,
                }
            return {
                "status": result["status"],
                "scope": args.scope,
                "store_valid": True,
                "recovery": recovery,
            }
        if args.summary:
            return {
                "integrity_gate": result["status"],
                "verification_failures": result["verification"]["failures"],
                "foreign_key_failures": result["foreign_key_failures"],
            }
        return result
    finally:
        lock.release()


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def maintenance_marker_path(runtime_dir: Path, session_id: str) -> Path:
    if not re.fullmatch(r"maintenance-[0-9a-f-]{36}", session_id):
        raise InventoryError("invalid maintenance session id")
    return checked_managed_path(
        runtime_dir, Path("maintenance-sessions") / f"{session_id}.json", "runtime"
    )


def maintenance_marker_digest(record: dict) -> str:
    unsigned = {key: value for key, value in record.items() if key != "record_digest"}
    return hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def write_maintenance_marker(marker: Path, record: dict) -> None:
    record["record_digest"] = maintenance_marker_digest(record)
    write_json(marker, record)


def prepare_maintenance_marker_parent(runtime_dir: Path, marker: Path) -> None:
    ensure_private_directory(marker.parent)
    # The marker fsync cannot make a newly-created parent entry durable by itself.
    fsync_directory(runtime_dir)
    if marker.parent.is_symlink() or not marker.parent.is_dir() or path_entry_exists(marker):
        raise InventoryError("maintenance timer marker path is unsafe or already exists")


def read_maintenance_marker(args: argparse.Namespace, session_id: str) -> tuple[Path, dict]:
    marker = maintenance_marker_path(args.runtime_dir, session_id)
    if marker.is_symlink() or not marker.is_file():
        raise InventoryError("maintenance timer marker is missing or unsafe")
    try:
        record = strict_json_value(marker.read_text(encoding="utf-8"), "maintenance timer marker")
    except OSError as error:
        raise InventoryError(f"cannot read maintenance timer marker: {error}") from error
    required = {
        "activity",
        "evidence",
        "format",
        "finish_request",
        "installation_id",
        "inventory_id",
        "notes",
        "performed_on",
        "record_digest",
        "result",
        "sensitivity",
        "session_id",
        "status",
        "started_at",
        "started_monotonic_ns",
    }
    if (
        not isinstance(record, dict)
        or set(record) != required
        or record.get("format") != 2
        or record.get("session_id") != session_id
        or record.get("status") not in {"active", "completed"}
        or not valid_installation_id(record.get("installation_id"))
        or not isinstance(record.get("inventory_id"), str)
        or not record["inventory_id"]
        or not isinstance(record.get("record_digest"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", record["record_digest"])
        or record["record_digest"] != maintenance_marker_digest(record)
        or not isinstance(record.get("activity"), str)
        or not record["activity"].strip()
        or not isinstance(record.get("performed_on"), str)
        or not isinstance(record.get("started_at"), str)
        or type(record.get("started_monotonic_ns")) is not int
        or record.get("started_monotonic_ns") < 0
        or record.get("sensitivity") not in SENSITIVITY_RANK
        or record.get("notes") is not None and not isinstance(record.get("notes"), str)
        or not isinstance(record.get("evidence"), dict)
        or record.get("finish_request") is not None
        and not isinstance(record.get("finish_request"), dict)
        or record.get("result") is not None and not isinstance(record.get("result"), dict)
    ):
        raise InventoryError("maintenance timer marker is malformed")
    try:
        date.fromisoformat(record["performed_on"])
        datetime.fromisoformat(record["started_at"])
    except ValueError as error:
        raise InventoryError("maintenance timer marker has invalid timestamps") from error
    evidence = record["evidence"]
    if set(evidence) == {"evidence_id"}:
        if not isinstance(evidence["evidence_id"], str) or not evidence["evidence_id"]:
            raise InventoryError("maintenance timer marker evidence is malformed")
    elif set(evidence) == {"evidence_type", "source_ref"}:
        if (
            evidence["evidence_type"] not in {"user_source", "research", "vault_note"}
            or not isinstance(evidence["source_ref"], str)
            or not evidence["source_ref"].strip()
        ):
            raise InventoryError("maintenance timer marker source provenance is malformed")
    else:
        raise InventoryError("maintenance timer marker evidence is malformed")
    finish_request = record["finish_request"]
    if finish_request is not None and (
        set(finish_request)
        != {"correction_count", "elapsed_seconds", "item_ids", "review_count"}
        or any(
            type(finish_request[field]) is not int or finish_request[field] < 0
            for field in ("correction_count", "elapsed_seconds", "review_count")
        )
        or not isinstance(finish_request["item_ids"], list)
        or any(
            not isinstance(item_id, str) or not item_id
            for item_id in finish_request["item_ids"]
        )
        or finish_request["item_ids"] != sorted(set(finish_request["item_ids"]))
    ):
        raise InventoryError("maintenance timer finish request is malformed")
    completion = record["result"]
    if completion is not None and (
        set(completion)
        != {
            "correction_count",
            "elapsed_seconds",
            "evidence_id",
            "item_ids",
            "maintenance_session_id",
            "review_count",
        }
        or completion.get("maintenance_session_id") != session_id
        or not isinstance(completion.get("evidence_id"), str)
        or not completion["evidence_id"]
        or any(
            type(completion.get(field)) is not int or completion[field] < 0
            for field in ("correction_count", "elapsed_seconds", "review_count")
        )
        or completion.get("item_ids") != (finish_request or {}).get("item_ids")
    ):
        raise InventoryError("maintenance timer completion is malformed")
    if (
        record["status"] == "active" and completion is not None
        or record["status"] == "completed"
        and (finish_request is None or completion is None)
    ):
        raise InventoryError("maintenance timer state is inconsistent")
    binding = read_runtime_binding_record(args.inventory_root)
    current_installation_id = (
        binding["installation_id"]
        if binding["format"] == 2
        else legacy_installation_id(args.inventory_root)
    )
    if record["installation_id"] != current_installation_id:
        raise InventoryError("maintenance timer marker belongs to another installation")
    return marker, record


def command_maintenance_start(args: argparse.Namespace) -> dict:
    """Persist a runtime-only, conservative timer marker for one upkeep session."""
    if args.scope != "private":
        raise InventoryError("maintenance start requires private scope")
    if not args.activity.strip():
        raise InventoryError("maintenance activity must not be blank")
    if args.source_ref is not None and not args.source_ref.strip():
        raise InventoryError("maintenance source provenance must not be blank")
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
            store = load_verified_store(paths)
            inventory_id = store.rows["metadata"][0]["inventory_id"]
            binding = read_runtime_binding_record(args.inventory_root)
            installation_id = (
                binding["installation_id"]
                if binding["format"] == 2
                else legacy_installation_id(args.inventory_root)
            )
            if args.evidence_id:
                store.get("evidence", args.evidence_id)
                evidence = {"evidence_id": args.evidence_id}
            else:
                evidence = {"evidence_type": args.evidence_type, "source_ref": args.source_ref}
            session_id = f"maintenance-{uuid.uuid4()}"
            marker = maintenance_marker_path(args.runtime_dir, session_id)
            prepare_maintenance_marker_parent(args.runtime_dir, marker)
            record = {
                "activity": args.activity,
                "evidence": evidence,
                "finish_request": None,
                "format": 2,
                "installation_id": installation_id,
                "inventory_id": inventory_id,
                "notes": args.notes,
                "performed_on": args.performed_on,
                "result": None,
                "sensitivity": args.sensitivity,
                "session_id": session_id,
                "status": "active",
                "started_at": datetime.now().astimezone().isoformat(),
                "started_monotonic_ns": time.monotonic_ns(),
            }
            write_maintenance_marker(marker, record)
            return {
                "status": "started",
                "maintenance_session_id": session_id,
                "marker": str(marker),
                "performed_on": args.performed_on,
            }
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error


def command_maintenance_finish(args: argparse.Namespace) -> dict:
    """Commit one measured session and retain a retryable runtime receipt."""
    if args.scope != "private":
        raise InventoryError("maintenance finish requires private scope")
    if args.correction_count is None or args.review_count is None:
        raise InventoryError(
            "maintenance finish requires explicit correction and review counts; unknown is not zero"
        )
    requested_item_ids = sorted(set(args.item_ids))
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
            marker, record = read_maintenance_marker(args, args.maintenance_session_id)
            request = record["finish_request"]
            if request is None:
                try:
                    elapsed_seconds = measured_elapsed_seconds(
                        started_at=record["started_at"],
                        started_monotonic_ns=record["started_monotonic_ns"],
                        finished_at=datetime.now().astimezone().isoformat(),
                        finished_monotonic_ns=time.monotonic_ns(),
                        explicit_elapsed_seconds=args.elapsed_seconds,
                    )
                except MaintenanceError as error:
                    raise InventoryError(str(error)) from error
                request = {
                    "correction_count": args.correction_count,
                    "elapsed_seconds": elapsed_seconds,
                    "item_ids": requested_item_ids,
                    "review_count": args.review_count,
                }
                validation_store = load_verified_store(paths)
                if (
                    validation_store.rows["metadata"][0]["inventory_id"]
                    != record["inventory_id"]
                ):
                    raise InventoryError(
                        "maintenance timer marker belongs to another inventory"
                    )
                for item_id in requested_item_ids:
                    validation_store.get("items", item_id)
                evidence_record = record["evidence"]
                if "evidence_id" in evidence_record:
                    evidence_id = evidence_record["evidence_id"]
                    validation_store.get("evidence", evidence_id)
                    supported = {
                        (row["item_id"], row["evidence_id"])
                        for row in validation_store.rows["item_evidence"]
                    }
                    if any(
                        (item_id, evidence_id) not in supported
                        for item_id in requested_item_ids
                    ):
                        raise InventoryError(
                            "maintenance evidence must already support every linked item"
                        )
                record["finish_request"] = request
                write_maintenance_marker(marker, record)
            elif (
                request["correction_count"] != args.correction_count
                or request["review_count"] != args.review_count
                or request["item_ids"] != requested_item_ids
                or args.elapsed_seconds is not None
                and request["elapsed_seconds"] != args.elapsed_seconds
            ):
                raise InventoryError(
                    "maintenance finish retry disagrees with the durable finish request"
                )
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error

    def mutate(store: Store) -> dict:
        if store.rows["metadata"][0]["inventory_id"] != record["inventory_id"]:
            raise InventoryError("maintenance timer marker belongs to another inventory")
        item_ids = request["item_ids"]
        items = [store.get("items", item_id) for item_id in item_ids]
        existing = [
            row
            for row in store.rows["maintenance_sessions"]
            if row["maintenance_session_id"] == args.maintenance_session_id
        ]
        if existing:
            session = existing[0]
            evidence = store.get("evidence", session["evidence_id"])
            expected_sensitivity = max(
                [record["sensitivity"], evidence["sensitivity"], *(item["sensitivity"] for item in items)],
                key=SENSITIVITY_RANK.__getitem__,
            )
            expected_session = {
                "maintenance_session_id": args.maintenance_session_id,
                "performed_on": record["performed_on"],
                "activity": record["activity"],
                "elapsed_seconds": request["elapsed_seconds"],
                "correction_count": request["correction_count"],
                "review_count": request["review_count"],
                "evidence_id": evidence["evidence_id"],
                "sensitivity": expected_sensitivity,
                "notes": record["notes"],
            }
            actual_links = sorted(
                row["item_id"]
                for row in store.rows["maintenance_session_items"]
                if row["maintenance_session_id"] == args.maintenance_session_id
            )
            evidence_record = record["evidence"]
            if "evidence_id" in evidence_record:
                evidence_matches = evidence["evidence_id"] == evidence_record["evidence_id"]
            else:
                evidence_matches = evidence == {
                    "captured_on": record["performed_on"],
                    "claim_strength": "research_only",
                    "evidence_id": evidence["evidence_id"],
                    "evidence_type": evidence_record["evidence_type"],
                    "notes": record["notes"],
                    "sensitivity": max(
                        [record["sensitivity"], *(item["sensitivity"] for item in items)],
                        key=SENSITIVITY_RANK.__getitem__,
                    ),
                    "source_ref": evidence_record["source_ref"],
                }
            supported = {
                (row["item_id"], row["evidence_id"])
                for row in store.rows["item_evidence"]
            }
            if (
                session != expected_session
                or actual_links != item_ids
                or not evidence_matches
                or any((item_id, evidence["evidence_id"]) not in supported for item_id in item_ids)
            ):
                raise InventoryError(
                    "canonical maintenance session disagrees with the durable finish request"
                )
            return {
                "maintenance_session_id": args.maintenance_session_id,
                "evidence_id": evidence["evidence_id"],
                "elapsed_seconds": request["elapsed_seconds"],
                "recovered": True,
            }
        evidence_record = record["evidence"]
        if "evidence_id" in evidence_record:
            evidence_id = evidence_record["evidence_id"]
            evidence = store.get("evidence", evidence_id)
            for item_id in item_ids:
                if (item_id, evidence_id) not in {
                    (row["item_id"], row["evidence_id"])
                    for row in store.rows["item_evidence"]
                }:
                    raise InventoryError("maintenance evidence must already support every linked item")
        else:
            required_sensitivity = max(
                [record["sensitivity"], *(item["sensitivity"] for item in items)],
                key=SENSITIVITY_RANK.__getitem__,
            )
            evidence_id = store.allocate("evidence", f"ev-maintenance-{args.maintenance_session_id[-12:]}")
            evidence = {
                "captured_on": record["performed_on"],
                "claim_strength": "research_only",
                "evidence_id": evidence_id,
                "evidence_type": evidence_record["evidence_type"],
                "notes": record["notes"],
                "sensitivity": required_sensitivity,
                "source_ref": evidence_record["source_ref"],
            }
            store.rows["evidence"].append(evidence)
            for item_id in item_ids:
                store.rows["item_evidence"].append(
                    {"evidence_id": evidence_id, "item_id": item_id, "role": "supporting"}
                )
        sensitivity = max(
            [record["sensitivity"], evidence["sensitivity"], *(item["sensitivity"] for item in items)],
            key=SENSITIVITY_RANK.__getitem__,
        )
        store.rows["maintenance_sessions"].append(
            {
                "maintenance_session_id": args.maintenance_session_id,
                "performed_on": record["performed_on"],
                "activity": record["activity"],
                "elapsed_seconds": request["elapsed_seconds"],
                "correction_count": request["correction_count"],
                "review_count": request["review_count"],
                "evidence_id": evidence_id,
                "sensitivity": sensitivity,
                "notes": record["notes"],
            }
        )
        store.rows["maintenance_session_items"].extend(
            {"maintenance_session_id": args.maintenance_session_id, "item_id": item_id}
            for item_id in item_ids
        )
        return {
            "maintenance_session_id": args.maintenance_session_id,
            "evidence_id": evidence_id,
            "elapsed_seconds": request["elapsed_seconds"],
            "recovered": False,
        }

    result = transaction(
        args.inventory_root,
        args.runtime_dir,
        f"maintenance-{args.maintenance_session_id}",
        mutate,
        continue_batch=args.continue_batch,
    )
    current_marker, current_record = read_maintenance_marker(
        args, args.maintenance_session_id
    )
    if current_marker != marker or current_record["record_digest"] != record["record_digest"]:
        raise InventoryError("maintenance timer marker changed during finish")
    canonical = result["result"]
    record["status"] = "completed"
    record["result"] = {
        "correction_count": request["correction_count"],
        "elapsed_seconds": request["elapsed_seconds"],
        "evidence_id": canonical["evidence_id"],
        "item_ids": request["item_ids"],
        "maintenance_session_id": args.maintenance_session_id,
        "review_count": request["review_count"],
    }
    write_maintenance_marker(marker, record)
    if os.environ.get("PROPERTY_INVENTORY_FAIL_MAINTENANCE_AFTER_MARKER") == "1":
        os._exit(96)
    return result


def command_maintenance_report(args: argparse.Namespace) -> dict:
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
            try:
                return maintenance_report(load_verified_store(paths).rows, scope=args.scope)
            except MaintenanceError as error:
                raise InventoryError(f"cannot report upkeep: {error}") from error
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error


def command_maintenance_harness(args: argparse.Namespace) -> dict:
    fixture = sync_private_input(args.input, "synthetic upkeep fixture")
    if fixture.stat().st_size > 1024 * 1024:
        raise InventoryError("synthetic upkeep fixture is too large")
    try:
        value = strict_json_value(fixture.read_text(encoding="utf-8"), "synthetic upkeep fixture")
        return run_synthetic_four_week_harness(value)
    except (OSError, MaintenanceError) as error:
        raise InventoryError(f"cannot run upkeep harness: {error}") from error


def command_compatibility_status(args: argparse.Namespace) -> dict:
    """Expose the declared historical/current migration and runtime contract."""
    try:
        runtime = validate_runtime()
        matrix = compatibility_matrix(runtime)
        migrations = [
            validate_migration(entry.schema_version, python_version=runtime)
            for entry in matrix.entries
        ]
    except CompatibilityError as error:
        raise InventoryError(f"compatibility policy is invalid: {error}") from error
    return {
        "status": "pass",
        "current_schema_version": matrix.current_schema_version,
        "minimum_python": list(matrix.minimum_python),
        "runtime_python": list(matrix.runtime_python),
        "entries": [
            {
                "schema_version": entry.schema_version,
                "action": entry.action,
                "supported": entry.supported,
            }
            for entry in migrations
        ],
    }


def command_doctor(args: argparse.Namespace) -> dict:
    """Prove portability by restoring one fresh export outside every live root."""
    if args.scope != "private":
        raise InventoryError("doctor requires private scope")
    if args.media_root is None:
        raise InventoryError("doctor requires --media-root or PROPERTY_INVENTORY_MEDIA_ROOT")
    archive = sync_private_output(args, args.output, "doctor archive")
    try:
        with tempfile.TemporaryDirectory(prefix="property-inventory-doctor-") as temporary_name:
            temporary = Path(temporary_name)
            plan = plan_blank_restore(
                executable=(sys.executable, "-m", "property_inventory.cli", "--scope", "private"),
                source_inventory_root=args.inventory_root,
                source_runtime_dir=args.runtime_dir,
                source_media_root=args.media_root,
                source_catalogue_output=args.catalogue_output,
                archive=archive,
                restored_inventory_root=temporary / "inventory",
                restored_runtime_dir=temporary / "runtime",
                restored_media_root=temporary / "media",
                restored_catalogue_output=temporary / "Inventory.md",
                catalogue_scope=args.catalogue_scope,
                forbidden_roots=args.forbidden_roots,
            )

            def runner(arguments: tuple[str, ...]) -> CommandResult:
                completed = subprocess.run(arguments, text=True, capture_output=True, check=False)
                return CommandResult(
                    completed.returncode, stdout=completed.stdout, stderr=completed.stderr
                )

            report = run_blank_restore(plan, runner=runner)
    except DoctorError as error:
        raise InventoryError(f"doctor drill failed: {error}") from error
    return {
        "status": "pass",
        "archive": str(archive),
        "commands": [
            {
                "label": command.label,
                "returncode": result.returncode,
            }
            for command, result in report.results
        ],
        "restored_roots_cleaned": True,
    }


def command_insurance_status(args: argparse.Namespace) -> dict:
    """Read the evidence-backed readiness report without changing canonical rows."""
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with (
            media_lock(args.media_root) if args.media_root is not None else nullcontext(),
            inventory_lock(args.inventory_root),
        ):
            recover_pending_transaction(paths)
            try:
                rows = load_verified_store(paths).rows
                preliminary = insurance_report(rows, scope=args.scope)
                verified = verified_insurance_media_ids(preliminary, args.media_root)
                return insurance_report(
                    rows,
                    scope=args.scope,
                    verified_media_asset_ids=verified,
                )
            except InsuranceError as error:
                raise InventoryError(f"cannot assess insurance readiness: {error}") from error
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error


def insurance_output_path(args: argparse.Namespace) -> Path:
    """Resolve a fresh insurance-package destination outside managed roots."""
    lexical_output = Path(os.path.abspath(args.output.expanduser()))
    if lexical_output.is_symlink() or lexical_output.parent.is_symlink():
        raise InventoryError("insurance export output must not traverse a managed symlink")
    try:
        output = lexical_output.resolve()
    except (OSError, RuntimeError) as error:
        raise InventoryError(f"cannot resolve insurance export output: {error}") from error
    if output == args.catalogue_output:
        raise InventoryError("insurance export output and catalogue output must be different files")
    for label, protected_root in (
        ("inventory", args.inventory_root),
        ("runtime", args.runtime_dir),
        ("media", args.media_root),
    ):
        if protected_root is not None and (
            output == protected_root
            or output in protected_root.parents
            or protected_root in output.parents
        ):
            raise InventoryError(
                f"insurance export output must be outside the {label} namespace: {protected_root}"
            )
    for forbidden_root in args.forbidden_roots:
        if any(
            candidate == forbidden_root
            or candidate in forbidden_root.parents
            or forbidden_root in candidate.parents
            for candidate in (lexical_output, output)
        ):
            raise InventoryError(
                f"insurance export output must be outside forbidden root: {forbidden_root}"
            )
    if path_entry_exists(lexical_output) or path_entry_exists(output):
        raise InventoryError(f"refusing to overwrite insurance export: {output}")
    return output


def canonical_insurance_media(report: dict, media_root: Path) -> dict[str, bytes]:
    """Read only report-declared media and reject missing, linked, or altered bytes."""
    result: dict[str, bytes] = {}
    if sum(asset["byte_size"] for asset in report["media_assets"]) > MAX_INSURANCE_PACKAGE_BYTES:
        raise InventoryError("insurance media exceeds the package byte limit")
    for asset in report["media_assets"]:
        asset_id = asset["asset_id"]
        digest = asset["sha256"]
        if asset["byte_size"] > MAX_INSURANCE_MEMBER_BYTES:
            raise InventoryError(f"insurance media asset exceeds the byte limit: {asset_id}")
        path = media_asset_path(media_root, digest)
        if not path.is_file() or path.is_symlink():
            raise InventoryError(
                f"insurance media asset is missing or not a regular file: {asset_id}"
            )
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise InventoryError(
                f"cannot read insurance media asset {asset_id}: {error}"
            ) from error
        if len(payload) != asset["byte_size"] or hashlib.sha256(payload).hexdigest() != digest:
            raise InventoryError(f"insurance media asset is missing or tampered: {asset_id}")
        if not declared_media_matches(path, asset["media_type"]):
            raise InventoryError(
                f"insurance media asset disagrees with its declared media type: {asset_id}"
            )
        result[asset_id] = payload
    return result


def verified_insurance_media_ids(report: dict, media_root: Path | None) -> set[str]:
    """Return only assets whose current canonical bytes match their declarations."""
    if media_root is None:
        return set()
    verified: set[str] = set()
    for asset in report["media_assets"]:
        if asset["byte_size"] > MAX_INSURANCE_MEMBER_BYTES:
            continue
        try:
            path = media_asset_path(media_root, asset["sha256"])
            if (
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_size == asset["byte_size"]
                and file_digest(path) == asset["sha256"]
                and declared_media_matches(path, asset["media_type"])
            ):
                verified.add(asset["asset_id"])
        except (InventoryError, OSError):
            continue
    return verified


def command_insurance_export(args: argparse.Namespace) -> dict:
    """Write one deterministic, self-validating private insurance ZIP package."""
    if args.scope != "private":
        raise InventoryError("insurance-export requires private scope")
    if args.media_root is None:
        raise InventoryError(
            "insurance-export requires --media-root or PROPERTY_INVENTORY_MEDIA_ROOT"
        )
    output = insurance_output_path(args)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_name(f".{output.name}.write-{uuid.uuid4()}")
    temporary_created = False
    temporary_descriptor: int | None = None
    temporary_identity: tuple[int, int] | None = None
    paths = data_paths(args.inventory_root, args.runtime_dir)
    try:
        with media_lock(args.media_root), inventory_lock(args.inventory_root):
            recover_pending_transaction(paths)
            try:
                rows = load_verified_store(paths).rows
                preliminary = insurance_report(rows, scope="private")
                media = canonical_insurance_media(preliminary, args.media_root)
                report = insurance_report(
                    rows,
                    scope="private",
                    verified_media_asset_ids=set(media),
                )
                package = build_insurance_package(report, media)
                validated = validate_insurance_package(package)
            except InsuranceError as error:
                raise InventoryError(f"cannot export insurance package: {error}") from error
            try:
                temporary_descriptor = os.open(
                    temporary_output,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    PRIVATE_FILE_MODE,
                )
                temporary_created = True
                os.fchmod(temporary_descriptor, PRIVATE_FILE_MODE)
                opened = os.fstat(temporary_descriptor)
                temporary_identity = (opened.st_dev, opened.st_ino)
                with os.fdopen(temporary_descriptor, "wb", closefd=False) as handle:
                    handle.write(package)
                    handle.flush()
                    os.fsync(handle.fileno())
                named = os.stat(temporary_output, follow_symlinks=False)
                if (
                    not stat.S_ISREG(named.st_mode)
                    or (named.st_dev, named.st_ino) != temporary_identity
                    or stat.S_IMODE(named.st_mode) != PRIVATE_FILE_MODE
                ):
                    raise InventoryError("insurance export staging changed before publication")
                os.link(temporary_output, output, follow_symlinks=False)
                published = os.stat(output, follow_symlinks=False)
                if (
                    not stat.S_ISREG(published.st_mode)
                    or (published.st_dev, published.st_ino) != temporary_identity
                    or stat.S_IMODE(published.st_mode) != PRIVATE_FILE_MODE
                ):
                    raise InventoryError("insurance export publication changed before verification")
                temporary_output.unlink()
                temporary_created = False
                fsync_directory(output.parent)
            except OSError as error:
                raise InventoryError(f"cannot write insurance package: {error}") from error
            return {
                "status": "exported",
                "package": str(output),
                "report": validated,
            }
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_created and temporary_identity is not None:
            try:
                current = os.stat(temporary_output, follow_symlinks=False)
            except OSError:
                current = None
            if current is not None and (current.st_dev, current.st_ino) == temporary_identity:
                temporary_output.unlink(missing_ok=True)


def command_insurance_validate(args: argparse.Namespace) -> dict:
    """Validate an existing insurance ZIP without extracting or changing inventory data."""
    if args.scope != "private":
        raise InventoryError("insurance-validate requires private scope")
    package_path = args.package.expanduser()
    if package_path.is_symlink() or not package_path.is_file():
        raise InventoryError("insurance package must be a regular file")
    if package_path.stat().st_size > MAX_INSURANCE_PACKAGE_BYTES:
        raise InventoryError("insurance package exceeds the byte limit")
    try:
        report = validate_insurance_package(package_path.read_bytes())
    except (OSError, InsuranceError) as error:
        raise InventoryError(f"cannot validate insurance package: {error}") from error
    return {"status": "valid", "package": str(package_path.resolve()), "report": report}


def command_runtime_rebind(args: argparse.Namespace) -> dict:
    """Rebind verified instance paths only after the old runtime proves quiescent."""
    if args.scope != "private":
        raise InventoryError("runtime-rebind requires private scope")
    requested_old_runtime = args.from_runtime.expanduser().resolve()
    requested_old_catalogue = (
        args.from_catalogue_output.expanduser().resolve()
        if args.from_catalogue_output is not None
        else args.catalogue_output.resolve()
    )
    requested_old_media = (
        args.from_media_root.expanduser().resolve()
        if args.from_media_root is not None
        else (args.media_root.resolve() if args.media_root is not None else None)
    )
    new_runtime = args.runtime_dir.resolve()
    try:
        lock = inventory_lock(args.inventory_root)
        lock.acquire()
    except Timeout as error:
        raise InventoryError("another inventory writer holds the transaction lock") from error
    try:
        binding = read_runtime_binding_record(args.inventory_root)
        bound_runtime = Path(binding["runtime_dir"]).resolve()
        installation_id = (
            binding["installation_id"]
            if binding["format"] == 2
            else legacy_installation_id(args.inventory_root)
        )
        if bound_runtime != requested_old_runtime:
            raise InventoryError("--from-runtime does not match the inventory binding")
        inventory_id = inventory_id_if_available(args.inventory_root)
        old_expected_owner = runtime_owner_payload(
            args.inventory_root,
            requested_old_runtime,
            requested_old_media,
            requested_old_catalogue,
            installation_id,
            inventory_id,
        )
        new_expected_owner = runtime_owner_payload(
            args.inventory_root,
            new_runtime,
            args.media_root,
            args.catalogue_output,
            installation_id,
            inventory_id,
        )
        existing_owner = read_runtime_owner(requested_old_runtime)
        if existing_owner is not None and (
            existing_owner != old_expected_owner
            and existing_owner != new_expected_owner
        ):
            raise InventoryError(
                "--from-catalogue-output or --from-media-root does not match "
                "the runtime owner marker"
            )
        already_bound = existing_owner == new_expected_owner
        protected_paths = (
            args.inventory_root,
            args.media_root,
            args.catalogue_output,
            requested_old_media,
            requested_old_catalogue,
            *args.forbidden_roots,
        )
        for protected in (path for path in protected_paths if path is not None):
            if (
                requested_old_runtime == protected
                or requested_old_runtime in protected.parents
                or protected in requested_old_runtime.parents
            ):
                raise InventoryError(f"bound runtime overlaps a protected path: {protected}")
        old_paths = data_paths(args.inventory_root, requested_old_runtime)
        pending = [
            path
            for path in (
                old_paths["transaction_journal"],
                old_paths["transaction_workspace"],
                old_paths["restore_journal"],
                old_paths["capture_media_journal"],
                old_paths["capture_media_workspace"],
                requested_old_runtime / INIT_JOURNAL,
                adoption_rollback_journal_path(requested_old_runtime),
            )
            if path_entry_exists(path)
        ]
        pending.extend(
            path
            for path in sorted(
                requested_old_runtime.glob(".property-inventory-restore-*")
            )
            if path_entry_exists(path)
        )
        if pending:
            raise InventoryError(
                "cannot rebind a runtime with pending recovery state: "
                + ", ".join(str(path) for path in pending)
            )
        proposal_directory = requested_old_runtime / "proposals"
        prepared_proposals: list[Path] = []
        if path_entry_exists(proposal_directory):
            if proposal_directory.is_symlink() or not proposal_directory.is_dir():
                raise InventoryError("runtime proposal directory is not a real directory")
            for proposal_file in proposal_directory.iterdir():
                if proposal_file.is_symlink() or not proposal_file.is_file():
                    raise InventoryError(
                        f"runtime proposal entry is not a regular file: {proposal_file}"
                    )
                try:
                    proposal = json.loads(proposal_file.read_text())
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise InventoryError(
                        f"cannot read runtime proposal before rebinding: {proposal_file}"
                    ) from error
                if not isinstance(proposal, dict) or proposal.get("status") not in {
                    "prepared",
                    "applied",
                }:
                    raise InventoryError(f"runtime proposal is malformed: {proposal_file}")
                if proposal["status"] == "prepared":
                    prepared_proposals.append(proposal_file)
        if prepared_proposals:
            raise InventoryError(
                "cannot rebind a runtime with prepared proposals: "
                + ", ".join(str(path) for path in prepared_proposals)
            )
        if not requested_old_runtime.is_dir():
            raise InventoryError(f"bound runtime is missing: {requested_old_runtime}")
        checks = verify_rebind_source(old_paths)
        if new_runtime != requested_old_runtime:
            new_owner = read_runtime_owner(new_runtime)
            if new_owner is None and runtime_has_unowned_entries(new_runtime):
                raise InventoryError(f"new runtime is not empty: {new_runtime}")
            claim_runtime_owner(
                args.inventory_root,
                new_runtime,
                args.media_root,
                args.catalogue_output,
                installation_id,
                inventory_id,
            )
            write_json(
                args.inventory_root / RUNTIME_BINDING,
                runtime_binding_payload(new_runtime, installation_id),
            )
        elif not already_bound:
            write_json(requested_old_runtime / RUNTIME_OWNER, new_expected_owner)
        if binding["format"] == 1:
            write_json(
                args.inventory_root / RUNTIME_BINDING,
                runtime_binding_payload(new_runtime, installation_id),
            )
        return {
            "status": "already_bound" if already_bound else "rebound",
            "old_runtime_retained": str(requested_old_runtime),
            "runtime_dir": str(new_runtime),
            "media_root": str(args.media_root.resolve()) if args.media_root is not None else None,
            "catalogue_output": str(args.catalogue_output.resolve()),
            "checks": checks,
        }
    finally:
        lock.release()


def command_add_location(args: argparse.Namespace) -> dict:
    def mutate(store: Store) -> dict:
        if args.parent_location_id:
            store.get("locations", args.parent_location_id)
        requested_content = {
            "kind": args.kind,
            "name": args.name,
            "notes": args.notes,
            "parent_location_id": args.parent_location_id,
            "sensitivity": args.sensitivity,
        }
        same_name = [
            row
            for row in store.rows["locations"]
            if row["name"].casefold() == args.name.casefold()
            and row.get("parent_location_id") == args.parent_location_id
        ]
        if args.location_id:
            same_id = [
                row
                for row in store.rows["locations"]
                if row["location_id"] == args.location_id
            ]
            if same_id:
                if len(same_id) != 1 or {
                    key: same_id[0].get(key) for key in requested_content
                } != requested_content:
                    raise InventoryError(
                        f"location id collision with different content: {args.location_id}"
                    )
                return {"location_id": args.location_id, "reused": True}
            if same_name:
                identifiers = ", ".join(row["location_id"] for row in same_name)
                raise InventoryError(
                    "a same-name location already exists; explicitly reuse or resolve "
                    + identifiers
                )
            location_id = args.location_id
        else:
            exact = [
                row
                for row in same_name
                if {key: row.get(key) for key in requested_content}
                == requested_content
            ]
            if len(exact) == 1 and len(same_name) == 1:
                return {"location_id": exact[0]["location_id"], "reused": True}
            if same_name:
                identifiers = ", ".join(row["location_id"] for row in same_name)
                raise InventoryError(
                    "same-name location has different kind, sensitivity, or notes; "
                    "pass an explicit resolved location id: "
                    + identifiers
                )
            location_id = store.allocate("locations", f"loc-{slug(args.name)}")
        store.rows["locations"].append(
            {
                "kind": args.kind,
                "location_id": location_id,
                "name": args.name,
                "notes": args.notes,
                "parent_location_id": args.parent_location_id,
                "sensitivity": args.sensitivity,
            }
        )
        return {"location_id": location_id, "reused": False}

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"add-location-{args.name}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_import_locations(args: argparse.Namespace) -> dict:
    specifications = read_strict_json_file(args.input, "location batch input")
    if not isinstance(specifications, list) or not specifications:
        raise InventoryError("location batch must be a non-empty JSON array")

    def mutate(store: Store) -> dict:
        imported: list[dict] = []
        seen_location_ids: set[str] = set()
        for index, specification in enumerate(specifications):
            if not isinstance(specification, dict):
                raise InventoryError(f"location batch row {index} is not an object")
            required = {"location_id", "name", "kind", "sensitivity"}
            allowed = required | {"parent_location_id", "notes"}
            missing = sorted(required - set(specification))
            if missing:
                raise InventoryError(f"location batch row {index} is missing {missing}")
            unexpected = sorted(set(specification) - allowed)
            if unexpected:
                raise InventoryError(
                    f"location batch row {index} has unexpected fields {unexpected}"
                )
            if (
                not isinstance(specification["location_id"], str)
                or not specification["location_id"].strip()
                or not isinstance(specification["name"], str)
                or not specification["name"].strip()
                or (
                    specification.get("notes") is not None
                    and not isinstance(specification.get("notes"), str)
                )
            ):
                raise InventoryError(f"location batch row {index} has invalid text fields")
            if specification["location_id"] in seen_location_ids:
                raise InventoryError(
                    f"location batch has duplicate location_id {specification['location_id']}"
                )
            seen_location_ids.add(specification["location_id"])
            if specification["kind"] not in {
                "place",
                "room",
                "container",
                "vehicle",
                "asset",
                "unknown",
            }:
                raise InventoryError(f"invalid location kind in row {index}")
            if specification["sensitivity"] not in {"low", "personal", "high"}:
                raise InventoryError(f"invalid location sensitivity in row {index}")
            parent = specification.get("parent_location_id")
            if parent is not None and (
                not isinstance(parent, str) or not parent.strip()
            ):
                raise InventoryError(
                    f"location batch row {index} has invalid parent_location_id"
                )
            if parent:
                store.get("locations", parent)
            same_id = [
                row
                for row in store.rows["locations"]
                if row["location_id"] == specification["location_id"]
            ]
            if same_id:
                comparable = {
                    "kind": specification["kind"],
                    "location_id": specification["location_id"],
                    "name": specification["name"],
                    "notes": specification.get("notes"),
                    "parent_location_id": parent,
                    "sensitivity": specification["sensitivity"],
                }
                if same_id[0] != comparable:
                    raise InventoryError(
                        f"location id collision with different content: {specification['location_id']}"
                    )
                imported.append({"location_id": specification["location_id"], "reused": True})
                continue
            store.rows["locations"].append(
                {
                    "kind": specification["kind"],
                    "location_id": specification["location_id"],
                    "name": specification["name"],
                    "notes": specification.get("notes"),
                    "parent_location_id": parent,
                    "sensitivity": specification["sensitivity"],
                }
            )
            imported.append({"location_id": specification["location_id"], "reused": False})
        return {"locations": imported}

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"import-locations-{args.input.stem}",
        mutate,
        continue_batch=args.continue_batch,
    )


def requested_model_matches(model: dict, args: argparse.Namespace) -> bool:
    """Compare every identity field the caller actually supplied."""
    try:
        specs = strict_json_value(
            model["specs_json"], f"model {model['model_id']} specs"
        )
        interfaces = strict_json_value(
            model["interfaces_json"], f"model {model['model_id']} interfaces"
        )
        identifiers = strict_json_value(
            model["identifiers_json"], f"model {model['model_id']} identifiers"
        )
    except (KeyError, InventoryError) as error:
        raise InventoryError("canonical model identity is malformed") from error
    requested_specs = {} if args.actor == "generic-import" else args.specs
    return (
        (model.get("name") or "").casefold() == args.name.casefold()
        and (model.get("brand") or "").casefold() == (args.brand or "").casefold()
        and (model.get("model") or "").casefold() == (args.model or "").casefold()
        and (model.get("category") or "").casefold() == args.category.casefold()
        and (not requested_specs or specs == requested_specs)
        and (not args.interface or interfaces == args.interface)
        and (not args.identifiers or identifiers == args.identifiers)
        and (args.reference_url is None or model.get("reference_url") == args.reference_url)
    )


def resolve_or_create_model(store: Store, args: argparse.Namespace) -> tuple[dict, bool]:
    """Reuse only a compatible automatic identity, or require an explicit decision."""
    existing_model_id = getattr(args, "existing_model_id", None)
    new_model = bool(getattr(args, "new_model", False))
    if existing_model_id and new_model:
        raise InventoryError("--existing-model-id and --new-model are mutually exclusive")
    if existing_model_id:
        model = store.get("models", existing_model_id)
        if not requested_model_matches(model, args):
            raise InventoryError(
                "requested model facts disagree with --existing-model-id"
            )
        merge_generic_import_provenance(model, args)
        return model, False

    label_matches = [
        row
        for row in store.rows["models"]
        if (row.get("name") or "").casefold() == args.name.casefold()
        and (row.get("brand") or "").casefold() == (args.brand or "").casefold()
        and (row.get("model") or "").casefold() == (args.model or "").casefold()
    ]
    requested_specs = {} if args.actor == "generic-import" else args.specs
    exact = [row for row in label_matches if requested_model_matches(row, args)]
    if not new_model:
        if len(exact) == 1 and len(label_matches) == 1:
            merge_generic_import_provenance(exact[0], args)
            return exact[0], False
        if label_matches:
            identifiers = ", ".join(row["model_id"] for row in label_matches)
            raise InventoryError(
                "same-label model identity differs; pass --existing-model-id to reuse "
                "deliberately or --new-model to create a distinct model: "
                + identifiers
            )

    model = {
        "brand": args.brand,
        "category": args.category,
        "identifiers_json": json.dumps(
            args.identifiers, ensure_ascii=False, sort_keys=True, allow_nan=False
        ),
        "interfaces_json": json.dumps(
            args.interface, ensure_ascii=False, allow_nan=False
        ),
        "model": args.model,
        "model_id": store.allocate(
            "models",
            f"mdl-{slug(' '.join(filter(None, [args.brand, args.model, args.name])))}",
        ),
        "name": args.name,
        "reference_url": args.reference_url,
        "specs_json": json.dumps(
            requested_specs,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ),
    }
    store.rows["models"].append(model)
    return model, True


def command_correct_item_identity(args: argparse.Namespace) -> dict:
    """Reassign one item to an immutable corrected model and retain the old identity."""

    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        evidence = store.get("evidence", args.evidence_id)
        if not evidence_supports_item(store, args.evidence_id, args.item_id):
            raise InventoryError("identity-correction evidence must support the item")
        item["identity_sensitivity"] = max(
            (
                item.get("identity_sensitivity") or item["sensitivity"],
                evidence["sensitivity"],
            ),
            key=SENSITIVITY_RANK.__getitem__,
        )
        current_model = store.get("models", item["model_id"])
        completed_retries = sorted(
            (
                row
                for row in store.rows["item_amendments"]
                if row["item_id"] == args.item_id
                and row["amended_on"] == args.amended_on
                and row["evidence_id"] == args.evidence_id
                and row["reason"] == args.reason
                and row["target_model_id"] == item["model_id"]
                and requested_model_matches(current_model, args)
            ),
            key=lambda row: (
                row["amended_on"],
                row["recorded_at"],
                row["amendment_id"],
            ),
        )
        if completed_retries:
            latest = completed_retries[-1]
            return {
                "amendment_id": latest["amendment_id"],
                "item_id": args.item_id,
                "model_created": False,
                "reused": True,
                "target_model_id": current_model["model_id"],
            }
        target, model_created = resolve_or_create_model(store, args)
        previous_model_id = item["model_id"]
        if previous_model_id == target["model_id"]:
            matching_history = sorted(
                (
                    row
                    for row in store.rows["item_amendments"]
                    if row["item_id"] == args.item_id
                    and row["amended_on"] == args.amended_on
                    and row["evidence_id"] == args.evidence_id
                    and row["reason"] == args.reason
                    and row["target_model_id"] == target["model_id"]
                ),
                key=lambda row: (
                    row["amended_on"],
                    row["recorded_at"],
                    row["amendment_id"],
                ),
            )
            if not matching_history:
                raise InventoryError("identity correction must select a different model")
            latest = matching_history[-1]
            return {
                "amendment_id": latest["amendment_id"],
                "item_id": args.item_id,
                "model_created": False,
                "reused": True,
                "target_model_id": target["model_id"],
            }
        recorded_at = recorded_timestamp(args.recorded_at)
        amendment_identity = "\0".join(
            (
                args.item_id,
                previous_model_id,
                target["model_id"],
                args.amended_on,
                recorded_at,
                args.evidence_id,
                args.reason,
            )
        )
        amendment_id = (
            "amend-" + hashlib.sha256(amendment_identity.encode()).hexdigest()[:24]
        )
        item["model_id"] = target["model_id"]
        store.rows["item_amendments"].append(
            {
                "actor": args.actor,
                "amended_on": args.amended_on,
                "amendment_id": amendment_id,
                "evidence_id": args.evidence_id,
                "item_id": args.item_id,
                "notes": args.notes,
                "previous_model_id": previous_model_id,
                "recorded_at": recorded_at,
                "reason": args.reason,
                "target_model_id": target["model_id"],
            }
        )
        return {
            "amendment_id": amendment_id,
            "item_id": args.item_id,
            "model_created": model_created,
            "previous_model_id": previous_model_id,
            "reused": False,
            "target_model_id": target["model_id"],
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"correct-item-identity-{args.item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_record_evidence(args: argparse.Namespace) -> dict:
    """Create a reusable evidence row without smuggling in an ownership transition."""
    item_ids = sorted(set(args.item_id or []))
    if args.evidence_type == "physical_check":
        raise InventoryError(
            "record-evidence cannot create physical_check evidence; use physical-check "
            "or discover so location and lifecycle are updated atomically"
        )
    if args.evidence_type in {"research", "vault_note"} and (
        args.claim_strength != "research_only"
    ):
        raise InventoryError("research and vault_note evidence require research_only")
    if args.claim_strength in {
        "explicit_current",
        "explicit_not_owned",
        "area_not_found",
    }:
        raise InventoryError(
            "state-bearing evidence must be created by its lifecycle command"
        )

    def mutate(store: Store) -> dict:
        items = [store.get("items", item_id) for item_id in item_ids]
        required_sensitivity = max(
            (args.sensitivity, *(item["sensitivity"] for item in items)),
            key=SENSITIVITY_RANK.__getitem__,
        )
        identity = strict_json_dumps(
            {
                "captured_on": args.captured_on,
                "claim_strength": args.claim_strength,
                "evidence_type": args.evidence_type,
                "item_ids": item_ids,
                "notes": args.notes,
                "sensitivity": required_sensitivity,
                "source_ref": args.source_ref,
            },
            sort_keys=True,
        )
        evidence_id = "ev-recorded-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        requested = {
            "captured_on": args.captured_on,
            "claim_strength": args.claim_strength,
            "evidence_id": evidence_id,
            "evidence_type": args.evidence_type,
            "notes": args.notes,
            "sensitivity": required_sensitivity,
            "source_ref": args.source_ref,
        }
        existing = [
            row for row in store.rows["evidence"] if row["evidence_id"] == evidence_id
        ]
        if existing:
            if len(existing) != 1 or existing[0] != requested:
                raise InventoryError("recorded evidence identity collides")
            for item_id in item_ids:
                if not evidence_supports_item(store, evidence_id, item_id):
                    raise InventoryError("recorded evidence retry has missing item support")
            return {"evidence_id": evidence_id, "reused": True}
        store.rows["evidence"].append(requested)
        store.rows["item_evidence"].extend(
            {
                "evidence_id": evidence_id,
                "item_id": item_id,
                "role": "supporting",
            }
            for item_id in item_ids
        )
        return {"evidence_id": evidence_id, "reused": False}

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        "record-evidence",
        mutate,
        continue_batch=args.continue_batch,
    )


def fact_item_ids(store: Store, table: str, row: dict) -> set[str]:
    if table in {"aliases", "item_tags", "valuations", "item_documents", "item_dimensions"}:
        return {row["item_id"]}
    if table == "relationships":
        return {row["subject_item_id"], row["object_item_id"]}
    if table == "torque_paths":
        return {row["tool_item_id"]}
    if table == "kits":
        return {row["serves_item_id"]}
    if table == "kit_requirements":
        return {row["item_id"]} if row.get("item_id") else set()
    if table == "model_interfaces":
        return {
            item["item_id"]
            for item in store.rows["items"]
            if item["model_id"] == row["model_id"]
        }
    if table in {"item_party_relations", "location_embodiments"}:
        return {row["item_id"]}
    return set()


def command_amend_fact(args: argparse.Namespace) -> dict:
    """Replace or retract one current fact while retaining an evidence-backed chain."""
    selector_fields = FACT_SELECTOR_FIELDS[args.table]
    if set(args.selector) != set(selector_fields) or any(
        not isinstance(args.selector[field], str) or not args.selector[field]
        for field in selector_fields
    ):
        raise InventoryError(
            "fact selector must contain exactly: " + ", ".join(selector_fields)
        )
    if args.action == "replace" and args.replacement is None:
        raise InventoryError("replace requires --replacement")
    if args.action == "retract" and args.replacement is not None:
        raise InventoryError("retract does not accept --replacement")
    reason = args.reason.strip()
    if not reason:
        raise InventoryError("fact amendment reason must not be blank")
    selector_json = strict_json_dumps(args.selector, sort_keys=True)
    replacement_json = (
        strict_json_dumps(args.replacement, sort_keys=True)
        if args.replacement is not None
        else None
    )

    def mutate(store: Store) -> dict:
        evidence = store.get("evidence", args.evidence_id)
        matching_history = sorted(
            (
            row
            for row in store.rows["fact_amendments"]
            if row["table_name"] == args.table
            and row["selector_json"] == selector_json
            and row["amended_on"] == args.amended_on
            and row["evidence_id"] == args.evidence_id
            and row["action"] == args.action
            and row["replacement_json"] == replacement_json
            and row["reason"] == reason
            ),
            key=lambda row: (
                row["amended_on"],
                row["recorded_at"],
                row["fact_amendment_id"],
            ),
        )
        current = [
            row
            for row in store.rows[args.table]
            if all(row.get(field) == args.selector[field] for field in selector_fields)
        ]
        expected = args.replacement if args.action == "replace" else None
        if (expected is None and not current) or (
            expected is not None and len(current) == 1 and current[0] == expected
        ):
            if not matching_history:
                raise InventoryError("fact amendment would make no change")
            return {
                "fact_amendment_id": matching_history[-1]["fact_amendment_id"],
                "reused": True,
            }
        if len(current) != 1:
            raise InventoryError("fact selector must resolve exactly one current row")
        previous = current[0]
        replacement = args.replacement
        if replacement is not None:
            if set(replacement) != set(previous):
                raise InventoryError("replacement must contain exactly the current row fields")
            if any(replacement.get(field) != previous.get(field) for field in selector_fields):
                raise InventoryError("replacement cannot change fact identity fields")
            if "evidence_id" in replacement and replacement["evidence_id"] != args.evidence_id:
                raise InventoryError("replacement evidence_id must equal amendment evidence")
            if replacement == previous:
                raise InventoryError("fact amendment would make no change")
        affected_item_ids = fact_item_ids(store, args.table, previous)
        if replacement is not None:
            affected_item_ids |= fact_item_ids(store, args.table, replacement)
        evidence_item_ids = fact_item_ids(
            store,
            args.table,
            replacement if replacement is not None else previous,
        )
        for item_id in affected_item_ids:
            store.get("items", item_id)
        for item_id in evidence_item_ids:
            if not evidence_supports_item(store, args.evidence_id, item_id):
                raise InventoryError("fact-amendment evidence must support every affected item")
        sensitivity_candidates = [evidence["sensitivity"]]
        for row in (previous, replacement):
            if isinstance(row, dict) and row.get("sensitivity") in SENSITIVITY_RANK:
                sensitivity_candidates.append(row["sensitivity"])
        sensitivity_candidates.extend(
            store.get("items", item_id)["sensitivity"]
            for item_id in affected_item_ids
        )
        if args.table == "locations":
            sensitivity_candidates.append(
                location_context_sensitivity(store, previous["location_id"])
            )
        if args.table == "spatial_profiles":
            sensitivity_candidates.append(
                location_context_sensitivity(store, previous["location_id"])
            )
        previous_json = strict_json_dumps(previous, sort_keys=True)
        recorded_at = recorded_timestamp(args.recorded_at)
        identity = strict_json_dumps(
            {
                "action": args.action,
                "amended_on": args.amended_on,
                "evidence_id": args.evidence_id,
                "previous_json": previous_json,
                "recorded_at": recorded_at,
                "reason": reason,
                "replacement_json": replacement_json,
                "selector_json": selector_json,
                "table_name": args.table,
            },
            sort_keys=True,
        )
        amendment_id = "fact-amend-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        if replacement is None:
            store.rows[args.table].remove(previous)
        else:
            previous.clear()
            previous.update(replacement)
        store.rows["fact_amendments"].append(
            {
                "action": args.action,
                "actor": args.actor,
                "amended_on": args.amended_on,
                "evidence_id": args.evidence_id,
                "fact_amendment_id": amendment_id,
                "notes": args.notes,
                "previous_json": previous_json,
                "reason": reason,
                "recorded_at": recorded_at,
                "replacement_json": replacement_json,
                "selector_json": selector_json,
                "sensitivity": max(
                    sensitivity_candidates, key=SENSITIVITY_RANK.__getitem__
                ),
                "table_name": args.table,
            }
        )
        return {"fact_amendment_id": amendment_id, "reused": False}

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"amend-fact-{args.table}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_enrich_item(args: argparse.Namespace) -> dict:
    """Add receipt/acquisition metadata without claiming current possession."""
    changes = {
        field: getattr(args, field)
        for field in ENRICH_ITEM_DETAIL_FIELDS
        if getattr(args, field) is not None
    }
    clear_fields = set(args.clear_field or [])
    if clear_fields & changes.keys():
        raise InventoryError("a detail cannot be both supplied and cleared")
    changes.update({field: None for field in clear_fields})
    if not changes:
        raise InventoryError("enrich-item requires at least one supplied or cleared field")
    if ("purchase_price" in changes) != ("purchase_currency" in changes):
        raise InventoryError("purchase price and currency must be changed together")

    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        evidence = store.get("evidence", args.evidence_id)
        if not evidence_supports_item(store, args.evidence_id, args.item_id):
            raise InventoryError("item-detail evidence must support the item")
        changes_json = strict_json_dumps(changes, sort_keys=True)
        matching_history = sorted(
            (
            row
            for row in store.rows["item_detail_amendments"]
            if row["item_id"] == args.item_id
            and row["amended_on"] == args.amended_on
            and row["evidence_id"] == args.evidence_id
            and row["changes_json"] == changes_json
            ),
            key=lambda row: (
                row["amended_on"],
                row["recorded_at"],
                row["detail_amendment_id"],
            ),
        )
        if all(item.get(field) == value for field, value in changes.items()):
            if not matching_history:
                raise InventoryError("item-detail amendment would make no change")
            return {
                "detail_amendment_id": matching_history[-1]["detail_amendment_id"],
                "reused": True,
            }
        previous = {field: item.get(field) for field in ITEM_DETAIL_FIELDS}
        previous_json = strict_json_dumps(previous, sort_keys=True)
        recorded_at = recorded_timestamp(args.recorded_at)
        identity = strict_json_dumps(
            {
                "amended_on": args.amended_on,
                "changes": changes,
                "evidence_id": args.evidence_id,
                "item_id": args.item_id,
                "previous": previous,
                "recorded_at": recorded_at,
            },
            sort_keys=True,
        )
        amendment_id = "detail-amend-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        item.update(changes)
        store.rows["item_detail_amendments"].append(
            {
                "actor": args.actor,
                "amended_on": args.amended_on,
                "changes_json": changes_json,
                "detail_amendment_id": amendment_id,
                "evidence_id": args.evidence_id,
                "item_id": args.item_id,
                "notes": args.notes,
                "previous_json": previous_json,
                "recorded_at": recorded_at,
                "sensitivity": max(
                    (item["sensitivity"], evidence["sensitivity"]),
                    key=SENSITIVITY_RANK.__getitem__,
                ),
            }
        )
        return {"detail_amendment_id": amendment_id, "reused": False}

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"enrich-item-{args.item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_order(args: argparse.Namespace) -> dict:
    if (args.purchase_price is None) != (args.purchase_currency is None):
        raise InventoryError(
            "--purchase-price and --purchase-currency must be supplied together"
        )

    def mutate(store: Store) -> dict:
        if not args.order_placed:
            raise InventoryError("a cart is not an order; pass --order-placed only after checkout")
        if args.location_id:
            store.get("locations", args.location_id)
        if args.existing_item_id:
            item = store.get("items", args.existing_item_id)
            if getattr(args, "new_model", False):
                raise InventoryError("--new-model cannot be used with --existing-item-id")
            if args.existing_model_id and args.existing_model_id != item["model_id"]:
                raise InventoryError(
                    "--existing-model-id does not match --existing-item-id"
                )
            if not requested_model_matches(
                store.get("models", item["model_id"]), args
            ):
                raise InventoryError(
                    "requested model facts disagree with --existing-item-id"
                )
            if item["ownership_state"] not in {"candidate", "planned", "unknown"}:
                raise InventoryError(
                    "--existing-item-id is only for an unverified candidate, planned, or unknown row"
                )
            item_id = item["item_id"]
            merge_generic_import_provenance(store.get("models", item["model_id"]), args)
            previous_quantity = item.get("quantity")
            previous_unit = item.get("unit")
            item["ownership_state"] = "candidate"
            if args.location_id:
                item["location_id"] = args.location_id
                item["container_id"] = None
            created = False
        else:
            model, _ = resolve_or_create_model(store, args)
            matching_candidates = [
                row
                for row in store.rows["items"]
                if row["model_id"] == model["model_id"]
                and row["ownership_state"] in {"candidate", "planned", "unknown"}
            ]
            if matching_candidates:
                incoming_unit_identity = generic_import_unit_identity(args)
                existing_unit_identities = [
                    generic_import_item_unit_identity(store, row)
                    for row in matching_candidates
                ]
                if (
                    incoming_unit_identity is not None
                    and all(identity is not None for identity in existing_unit_identities)
                    and incoming_unit_identity not in existing_unit_identities
                ):
                    # Explicit source-unit identities prove these are separate
                    # physical units despite sharing a model.  Do not collapse
                    # the later source row onto the first candidate.
                    matching_candidates = []
            if matching_candidates:
                identifiers = ", ".join(row["item_id"] for row in matching_candidates)
                raise InventoryError(
                    "matching unverified item already exists; rerun with --existing-item-id "
                    + identifiers
                )
            item_id = store.allocate("items", f"itm-{slug(args.name)}")
            evidence_id = store.allocate(
                "evidence", f"ev-order-{slug(args.name, 40)}-{args.ordered_on}"
            )
            store.rows["evidence"].append(
                {
                    "captured_on": args.ordered_on,
                    "claim_strength": "purchase_only",
                    "evidence_id": evidence_id,
                    "evidence_type": args.evidence_type,
                    "notes": args.notes,
                    "sensitivity": max(
                        (
                            args.sensitivity,
                            location_context_sensitivity(
                                store, args.location_id or "loc-unknown"
                            ),
                        ),
                        key=SENSITIVITY_RANK.__getitem__,
                    ),
                    "source_ref": args.source_ref,
                }
            )
            store.rows["items"].append(
                {
                    "acquired_on": None,
                    "condition": None,
                    "container_id": None,
                    "home_container_id": None,
                    "home_location_id": None,
                    "item_id": item_id,
                    "location_id": args.location_id or "loc-unknown",
                    "model_id": model["model_id"],
                    "notes": args.notes,
                    "ownership_state": "candidate",
                    "primary_evidence_id": evidence_id,
                    "purchase_currency": args.purchase_currency,
                    "purchase_price": args.purchase_price,
                    "quantity": args.quantity,
                    "receipt_ref": args.receipt_ref,
                    "replacement_value": None,
                    "sensitivity": args.sensitivity,
                    "identity_sensitivity": args.sensitivity,
                    "serial_or_lot": None,
                    "unit": args.unit,
                    "value_currency": None,
                    "verified_on": None,
                }
            )
            store.rows["item_evidence"].append(
                {"evidence_id": evidence_id, "item_id": item_id, "role": "primary"}
            )
            created = True
            previous_quantity = None
            previous_unit = None
        if not created:
            evidence_id = add_evidence(
                store,
                item_id=item_id,
                base=f"order-{slug(item_id.removeprefix('itm-'), 40)}-{args.ordered_on}",
                evidence_type=args.evidence_type,
                source_ref=args.source_ref,
                captured_on=args.ordered_on,
                claim_strength="purchase_only",
                notes=args.notes,
                minimum_sensitivity=location_context_sensitivity(
                    store, item.get("location_id"), item.get("container_id")
                ),
            )
        quantity_details = None
        detail_amendment_id = None
        if not created:
            current_item = store.get("items", item_id)
            if args.quantity is not None and args.quantity != previous_quantity:
                current_item["quantity"] = args.quantity
                quantity_details = {
                    "previous_quantity": previous_quantity,
                    "previous_unit": previous_unit,
                    "quantity": args.quantity,
                    "unit": current_item.get("unit"),
                }
            detail_amendment_id = append_item_detail_amendment(
                store,
                item=current_item,
                changes={
                    **(
                        {
                            "purchase_price": args.purchase_price,
                            "purchase_currency": args.purchase_currency,
                        }
                        if args.purchase_price is not None
                        else {}
                    ),
                    **({"receipt_ref": args.receipt_ref} if args.receipt_ref else {}),
                },
                amended_on=args.ordered_on,
                actor=args.actor,
                evidence_id=evidence_id,
                notes=args.notes,
            )
        event_id = add_event(
            store,
            item_id=item_id,
            event_type="ordered",
            occurred_on=args.ordered_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=args.notes,
            location_id=store.get("items", item_id).get("location_id"),
            container_id=store.get("items", item_id).get("container_id"),
            details=quantity_details,
        )
        return {
            "item_id": item_id,
            "evidence_id": evidence_id,
            "event_id": event_id,
            "detail_amendment_id": detail_amendment_id,
            "created": created,
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"order-{args.name}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_plan(args: argparse.Namespace) -> dict:
    def mutate(store: Store) -> dict:
        if args.location_id:
            store.get("locations", args.location_id)
        if args.existing_item_id:
            item = store.get("items", args.existing_item_id)
            if getattr(args, "new_model", False):
                raise InventoryError("--new-model cannot be used with --existing-item-id")
            if args.existing_model_id and args.existing_model_id != item["model_id"]:
                raise InventoryError(
                    "--existing-model-id does not match --existing-item-id"
                )
            if not requested_model_matches(
                store.get("models", item["model_id"]), args
            ):
                raise InventoryError(
                    "requested model facts disagree with --existing-item-id"
                )
            if item["ownership_state"] not in {"planned", "unknown"}:
                raise InventoryError("--existing-item-id for a plan must be planned or unknown")
            item_id = item["item_id"]
            merge_generic_import_provenance(store.get("models", item["model_id"]), args)
            previous_quantity = item.get("quantity")
            previous_unit = item.get("unit")
            item["ownership_state"] = "planned"
            if args.location_id:
                item["location_id"] = args.location_id
                item["container_id"] = None
            if args.quantity is not None:
                item["quantity"] = args.quantity
            evidence_id = add_evidence(
                store,
                item_id=item_id,
                base=f"planned-{slug(item_id.removeprefix('itm-'), 40)}-{args.planned_on}",
                evidence_type="user_source",
                source_ref=args.source_ref,
                captured_on=args.planned_on,
                claim_strength="purchase_only",
                notes=args.notes,
            )
            created = False
        else:
            model, _ = resolve_or_create_model(store, args)
            matching_items = [
                row
                for row in store.rows["items"]
                if row["model_id"] == model["model_id"]
                and row["ownership_state"] in {"candidate", "planned", "unknown"}
            ]
            if matching_items:
                incoming_unit_identity = generic_import_unit_identity(args)
                existing_unit_identities = [
                    generic_import_item_unit_identity(store, row)
                    for row in matching_items
                ]
                if (
                    incoming_unit_identity is not None
                    and all(identity is not None for identity in existing_unit_identities)
                    and incoming_unit_identity not in existing_unit_identities
                ):
                    matching_items = []
            if matching_items:
                identifiers = ", ".join(row["item_id"] for row in matching_items)
                raise InventoryError(
                    "matching unverified item already exists; rerun with --existing-item-id "
                    + identifiers
                )
            item_id = store.allocate("items", f"itm-{slug(args.name)}")
            evidence_id = store.allocate(
                "evidence", f"ev-planned-{slug(args.name, 40)}-{args.planned_on}"
            )
            store.rows["evidence"].append(
                {
                    "captured_on": args.planned_on,
                    "claim_strength": "purchase_only",
                    "evidence_id": evidence_id,
                    "evidence_type": "user_source",
                    "notes": args.notes,
                    "sensitivity": max(
                        (
                            args.sensitivity,
                            location_context_sensitivity(
                                store, args.location_id or "loc-unknown"
                            ),
                        ),
                        key=SENSITIVITY_RANK.__getitem__,
                    ),
                    "source_ref": args.source_ref,
                }
            )
            store.rows["items"].append(
                {
                    "acquired_on": None,
                    "condition": None,
                    "container_id": None,
                    "home_container_id": None,
                    "home_location_id": None,
                    "item_id": item_id,
                    "location_id": args.location_id or "loc-unknown",
                    "model_id": model["model_id"],
                    "notes": args.notes,
                    "ownership_state": "planned",
                    "primary_evidence_id": evidence_id,
                    "purchase_currency": None,
                    "purchase_price": None,
                    "quantity": args.quantity,
                    "receipt_ref": None,
                    "replacement_value": None,
                    "sensitivity": args.sensitivity,
                    "identity_sensitivity": args.sensitivity,
                    "serial_or_lot": None,
                    "unit": args.unit,
                    "value_currency": None,
                    "verified_on": None,
                }
            )
            store.rows["item_evidence"].append(
                {"evidence_id": evidence_id, "item_id": item_id, "role": "primary"}
            )
            created = True
            previous_quantity = None
            previous_unit = None
        planned_quantity = store.get("items", item_id).get("quantity")
        planned_unit = store.get("items", item_id).get("unit")
        quantity_details = None
        if not created and (
            previous_quantity != planned_quantity or previous_unit != planned_unit
        ):
            quantity_details = {
                "previous_quantity": previous_quantity,
                "previous_unit": previous_unit,
                "quantity": planned_quantity,
                "unit": planned_unit,
            }
        event_id = add_event(
            store,
            item_id=item_id,
            event_type="planned",
            occurred_on=args.planned_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=args.notes,
            location_id=store.get("items", item_id).get("location_id"),
            container_id=store.get("items", item_id).get("container_id"),
            details=quantity_details,
        )
        return {
            "item_id": item_id,
            "evidence_id": evidence_id,
            "event_id": event_id,
            "created": created,
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"plan-{args.name}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_receive(args: argparse.Namespace) -> dict:
    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        if item["ownership_state"] not in {"candidate", "planned", "unknown"}:
            raise InventoryError(f"cannot receive item in state {item['ownership_state']}")
        if args.location_id:
            item["location_id"] = args.location_id
            item["container_id"] = args.container_id
        if args.container_id:
            item["container_id"] = args.container_id
        if args.location_id is None and not args.location_unchanged:
            raise InventoryError(
                "receipt requires --location-id or --location-unchanged"
            )
        stable_location_and_container(
            store, item.get("location_id"), item.get("container_id")
        )
        item["ownership_state"] = "confirmed"
        evidence_type = "physical_check" if args.physical_check else "user_source"
        evidence_id = add_evidence(
            store,
            item_id=args.item_id,
            base=f"received-{slug(args.item_id.removeprefix('itm-'), 36)}-{args.received_on}",
            evidence_type=evidence_type,
            source_ref=args.source_ref,
            captured_on=args.received_on,
            claim_strength="explicit_current",
            notes=args.notes,
            minimum_sensitivity=location_context_sensitivity(
                store, item.get("location_id"), item.get("container_id")
            ),
        )
        received_event_id = add_event(
            store,
            item_id=args.item_id,
            event_type="received",
            occurred_on=args.received_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=args.notes,
            location_id=item.get("location_id"),
            container_id=item.get("container_id"),
        )
        quantity_event_id = None
        if args.quantity is not None:
            quantity_event_id = apply_quantity_change(
                store,
                item=item,
                quantity=args.quantity,
                unit=item["unit"],
                occurred_on=args.received_on,
                actor=args.actor,
                evidence_id=evidence_id,
                notes=args.notes,
            )
        detail_amendment_id = append_item_detail_amendment(
            store,
            item=item,
            changes={
                **(
                    {"acquired_on": args.received_on}
                    if item.get("acquired_on") is None
                    else {}
                ),
                **({"condition": args.condition} if args.condition is not None else {}),
                **(
                    {"serial_or_lot": args.serial_or_lot}
                    if args.serial_or_lot is not None
                    else {}
                ),
            },
            amended_on=args.received_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=args.notes,
        )
        physical_event_id = None
        if args.physical_check:
            item["verified_on"] = args.received_on
            physical_event_id = add_event(
                store,
                item_id=args.item_id,
                event_type="physically_verified",
                occurred_on=args.received_on,
                actor=args.actor,
                evidence_id=evidence_id,
                notes=args.notes,
                location_id=item.get("location_id"),
                container_id=item.get("container_id"),
            )
        return {
            "item_id": args.item_id,
            "evidence_id": evidence_id,
            "received_event_id": received_event_id,
            "quantity_event_id": quantity_event_id,
            "detail_amendment_id": detail_amendment_id,
            "physical_event_id": physical_event_id,
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"receive-{args.item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_discover(args: argparse.Namespace) -> dict:
    """Record one physically shown unit without inventing its ownership."""
    if args.existing_item_id and args.new_model:
        raise InventoryError("--new-model cannot be used with --existing-item-id")
    if args.existing_item_id and args.ownership_state != "confirmed":
        raise InventoryError(
            "non-confirmed ownership is only valid for a newly distinguished unit"
        )

    def mutate(store: Store) -> dict:
        stable_location_and_container(store, args.location_id, args.container_id)
        model, model_created = resolve_or_create_model(store, args)

        existing_items = [
            row
            for row in store.rows["items"]
            if row["model_id"] == model["model_id"]
        ]
        if args.existing_item_id:
            item = store.get("items", args.existing_item_id)
            if item["model_id"] != model["model_id"]:
                raise InventoryError("--existing-item-id does not belong to the discovered model")
            if item["ownership_state"] in {"disposed", "refunded", "not_owned"}:
                raise InventoryError(
                    f"physical discovery would contradict state {item['ownership_state']}"
                )
            item["location_id"] = args.location_id
            item["container_id"] = args.container_id
            evidence_id = add_evidence(
                store,
                item_id=item["item_id"],
                base=f"discovered-{slug(item['item_id'].removeprefix('itm-'), 36)}-{args.checked_on}",
                evidence_type="physical_check",
                source_ref=args.source_ref,
                captured_on=args.checked_on,
                claim_strength="explicit_current",
                notes=args.notes,
                minimum_sensitivity=location_context_sensitivity(
                    store, args.location_id, args.container_id
                ),
            )
            received_event_id = None
            if item["ownership_state"] not in {"confirmed", "lent"}:
                item["ownership_state"] = "confirmed"
                received_event_id = add_event(
                    store,
                    item_id=item["item_id"],
                    event_type="received",
                    occurred_on=args.checked_on,
                    actor=args.actor,
                    evidence_id=evidence_id,
                    notes="Current possession established by physical discovery. " + (args.notes or ""),
                    location_id=args.location_id,
                    container_id=args.container_id,
                )
            quantity_event_id = None
            if args.quantity is not None:
                quantity_event_id = apply_quantity_change(
                    store,
                    item=item,
                    quantity=args.quantity,
                    unit=args.unit,
                    occurred_on=args.checked_on,
                    actor=args.actor,
                    evidence_id=evidence_id,
                    notes=args.notes,
                )
            detail_amendment_id = append_item_detail_amendment(
                store,
                item=item,
                changes={
                    **(
                        {"condition": args.condition}
                        if args.condition is not None
                        else {}
                    ),
                    **(
                        {"serial_or_lot": args.serial_or_lot}
                        if args.serial_or_lot is not None
                        else {}
                    ),
                },
                amended_on=args.checked_on,
                actor=args.actor,
                evidence_id=evidence_id,
                notes=args.notes,
            )
            item["verified_on"] = args.checked_on
            physical_event_id = add_event(
                store,
                item_id=item["item_id"],
                event_type="physically_verified",
                occurred_on=args.checked_on,
                actor=args.actor,
                evidence_id=evidence_id,
                notes=args.notes,
                location_id=args.location_id,
                container_id=args.container_id,
            )
            return {
                "item_id": item["item_id"],
                "model_id": model["model_id"],
                "model_created": model_created,
                "item_reused": True,
                "evidence_id": evidence_id,
                "received_event_id": received_event_id,
                "quantity_event_id": quantity_event_id,
                "detail_amendment_id": detail_amendment_id,
                "physical_event_id": physical_event_id,
            }
        if existing_items and not args.new_unit:
            identifiers = ", ".join(
                f"{row['item_id']} ({row['ownership_state']})" for row in existing_items
            )
            raise InventoryError(
                "matching physical unit may already exist; rerun with --existing-item-id "
                f"({identifiers}) or --new-unit after confirming this is another unit"
            )

        item_id = store.allocate("items", f"itm-{slug(args.name)}")
        evidence_id = store.allocate(
            "evidence", f"ev-discovered-{slug(args.name, 40)}-{args.checked_on}"
        )
        discovery_sensitivity = max(
            (
                args.sensitivity,
                location_context_sensitivity(
                    store, args.location_id, args.container_id
                ),
            ),
            key=SENSITIVITY_RANK.__getitem__,
        )
        store.rows["evidence"].append(
            {
                "captured_on": args.checked_on,
                "claim_strength": "explicit_current",
                "evidence_id": evidence_id,
                "evidence_type": "physical_check",
                "notes": args.notes,
                "sensitivity": discovery_sensitivity,
                "source_ref": args.source_ref,
            }
        )
        store.rows["items"].append(
            {
                "acquired_on": None,
                "condition": args.condition,
                "container_id": args.container_id,
                "home_container_id": None,
                "home_location_id": None,
                "item_id": item_id,
                "location_id": args.location_id,
                "model_id": model["model_id"],
                "notes": args.notes,
                "ownership_state": args.ownership_state,
                "primary_evidence_id": evidence_id,
                "purchase_currency": None,
                "purchase_price": None,
                "quantity": args.quantity,
                "receipt_ref": None,
                "replacement_value": None,
                "sensitivity": args.sensitivity,
                "identity_sensitivity": args.sensitivity,
                "serial_or_lot": args.serial_or_lot,
                "unit": args.unit,
                "value_currency": None,
                "verified_on": args.checked_on,
            }
        )
        store.rows["item_evidence"].append(
            {"evidence_id": evidence_id, "item_id": item_id, "role": "primary"}
        )
        possession_event_id = add_event(
            store,
            item_id=item_id,
            event_type=("received" if args.ownership_state == "confirmed" else "ingested"),
            occurred_on=args.checked_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=(
                "Current ownership and possession established by physical discovery. "
                if args.ownership_state == "confirmed"
                else "Physical presence established; ownership remains "
                f"{args.ownership_state}. "
            )
            + (args.notes or ""),
            location_id=(args.location_id if args.ownership_state == "confirmed" else None),
            container_id=(args.container_id if args.ownership_state == "confirmed" else None),
        )
        physical_event_id = add_event(
            store,
            item_id=item_id,
            event_type="physically_verified",
            occurred_on=args.checked_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=args.notes,
            location_id=args.location_id,
            container_id=args.container_id,
        )
        return {
            "item_id": item_id,
            "model_id": model["model_id"],
            "model_created": model_created,
            "item_reused": False,
            "evidence_id": evidence_id,
            "received_event_id": (
                possession_event_id if args.ownership_state == "confirmed" else None
            ),
            "ingested_event_id": (
                possession_event_id if args.ownership_state != "confirmed" else None
            ),
            "physical_event_id": physical_event_id,
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"discover-{args.name}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_sell(args: argparse.Namespace) -> dict:
    occurred_on, observed_on, date_precision = resolved_event_date(
        exact_date=args.sold_on,
        date_unknown=args.sold_date_unknown,
        observed_on=args.observed_on,
        label="sale",
    )

    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        if item["ownership_state"] not in {"confirmed", "lent"}:
            raise InventoryError(f"cannot sell item in state {item['ownership_state']}")
        previous_location = item.get("location_id")
        previous_container = item.get("container_id")
        item["ownership_state"] = "disposed"
        item["location_id"] = None
        item["container_id"] = None
        notes = args.notes or "Sold; current possession ended."
        if date_precision == "unknown":
            notes += f" Exact sale date unknown; fact observed on {observed_on}."
        if previous_location or previous_container:
            notes += f" Previous location={previous_location or 'none'}, container={previous_container or 'none'}."
        evidence_id = add_evidence(
            store,
            item_id=args.item_id,
            base=(
                f"sold-{slug(args.item_id.removeprefix('itm-'), 32)}-"
                f"{date_precision}-{occurred_on or observed_on}"
            ),
            evidence_type="user_source",
            source_ref=args.source_ref,
            captured_on=observed_on,
            claim_strength="explicit_not_owned",
            notes=notes,
            minimum_sensitivity=location_context_sensitivity(
                store, previous_location, previous_container
            ),
        )
        event_id = add_event(
            store,
            item_id=args.item_id,
            event_type="sold",
            occurred_on=occurred_on,
            occurred_on_precision=date_precision,
            observed_on=observed_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=notes,
            location_id=previous_location,
            container_id=previous_container,
        )
        return {
            "item_id": args.item_id,
            "evidence_id": evidence_id,
            "event_id": event_id,
            "occurred_on": occurred_on,
            "occurred_on_precision": date_precision,
            "observed_on": observed_on,
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"sell-{args.item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_relate(args: argparse.Namespace) -> dict:
    def mutate(store: Store) -> dict:
        store.get("items", args.subject_item_id)
        store.get("items", args.object_item_id)
        if args.subject_item_id == args.object_item_id:
            raise InventoryError("a relationship cannot point an item at itself")
        same = [
            row
            for row in store.rows["relationships"]
            if row["subject_item_id"] == args.subject_item_id
            and row["predicate"] == args.predicate
            and row["object_item_id"] == args.object_item_id
        ]
        if same:
            raise InventoryError(f"relationship already exists: {same[0]['relationship_id']}")
        if args.evidence_type == "physical_check" and args.claim_strength != "explicit_current":
            raise InventoryError("physical_check evidence must use explicit_current claim strength")
        if args.evidence_type == "research" and args.claim_strength != "research_only":
            raise InventoryError("research evidence must use research_only claim strength")
        subject_slug = slug(args.subject_item_id.removeprefix("itm-"), 24)
        object_slug = slug(args.object_item_id.removeprefix("itm-"), 24)
        relationship_id = store.allocate(
            "relationships", f"rel-{subject_slug}-{args.predicate}-{object_slug}"
        )
        evidence_id = store.allocate(
            "evidence", f"ev-{relationship_id.removeprefix('rel-')}-{args.captured_on}"
        )
        store.rows["evidence"].append(
            {
                "captured_on": args.captured_on,
                "claim_strength": args.claim_strength,
                "evidence_id": evidence_id,
                "evidence_type": args.evidence_type,
                "notes": args.notes,
                "sensitivity": max(
                    (
                        store.get("items", item_id)["sensitivity"]
                        for item_id in (args.subject_item_id, args.object_item_id)
                    ),
                    key=SENSITIVITY_RANK.__getitem__,
                ),
                "source_ref": args.source_ref,
            }
        )
        store.rows["item_evidence"].extend(
            {
                "evidence_id": evidence_id,
                "item_id": item_id,
                "role": "supporting",
            }
            for item_id in (args.subject_item_id, args.object_item_id)
        )
        store.rows["relationships"].append(
            {
                "confidence": args.confidence,
                "evidence_id": evidence_id,
                "notes": args.notes,
                "object_item_id": args.object_item_id,
                "predicate": args.predicate,
                "relationship_id": relationship_id,
                "subject_item_id": args.subject_item_id,
            }
        )
        return {"relationship_id": relationship_id, "evidence_id": evidence_id}

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"relate-{args.subject_item_id}-{args.predicate}-{args.object_item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_not_found(args: argparse.Namespace) -> dict:
    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        area = store.get("locations", args.area_location_id)
        previous_location = item.get("location_id")
        previous_container = item.get("container_id")
        locations = {
            row["location_id"]: row for row in store.rows["locations"]
        }

        def is_within_checked_area(location_id: str | None) -> bool:
            current = locations.get(location_id)
            visited: set[str] = set()
            while current is not None and current["location_id"] not in visited:
                current_id = current["location_id"]
                if current_id == args.area_location_id:
                    return True
                visited.add(current_id)
                current = locations.get(current.get("parent_location_id"))
            return False

        if is_within_checked_area(previous_location) or is_within_checked_area(
            previous_container
        ):
            item["location_id"] = "loc-unknown"
            item["container_id"] = None
        notes = args.notes or "Expected item was not found during this area check."
        notes += (
            f" Checked area={area['name']} ({args.area_location_id}); "
            f"previous location={previous_location or 'none'}, container={previous_container or 'none'}. "
            "This does not prove sale, loss, disposal, or absence from ownership."
        )
        evidence_id = add_evidence(
            store,
            item_id=args.item_id,
            base=f"not-found-{slug(args.item_id.removeprefix('itm-'), 32)}-{args.checked_on}",
            evidence_type="physical_check",
            source_ref=args.source_ref,
            captured_on=args.checked_on,
            claim_strength="area_not_found",
            notes=notes,
            minimum_sensitivity=location_context_sensitivity(
                store, args.area_location_id, previous_location, previous_container
            ),
        )
        event_id = add_event(
            store,
            item_id=args.item_id,
            event_type="not_found_in_area",
            occurred_on=args.checked_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=notes,
            location_id=item.get("location_id"),
            area_location_id=args.area_location_id,
        )
        return {
            "item_id": args.item_id,
            "evidence_id": evidence_id,
            "event_id": event_id,
            "ownership_state": item["ownership_state"],
            "follow_up_required": True,
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"not-found-{args.item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_physical_check(args: argparse.Namespace) -> dict:
    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        if item["ownership_state"] in {"disposed", "refunded"}:
            raise InventoryError(
                f"physical confirmation would contradict state {item['ownership_state']}; correct or reacquire explicitly"
            )
        if args.location_id is None and not args.location_unchanged:
            raise InventoryError(
                "physical check requires --location-id or --location-unchanged"
            )
        if args.location_id:
            item["location_id"] = args.location_id
            item["container_id"] = args.container_id
        if args.container_id:
            item["container_id"] = args.container_id
        stable_location_and_container(
            store, item.get("location_id"), item.get("container_id")
        )
        evidence_id = add_evidence(
            store,
            item_id=args.item_id,
            base=f"physical-{slug(args.item_id.removeprefix('itm-'), 36)}-{args.checked_on}",
            evidence_type="physical_check",
            source_ref=args.source_ref,
            captured_on=args.checked_on,
            claim_strength="explicit_current",
            notes=args.notes,
            minimum_sensitivity=location_context_sensitivity(
                store, item.get("location_id"), item.get("container_id")
            ),
        )
        received_event_id = None
        previous_state = item["ownership_state"]
        if previous_state in {"candidate", "planned"}:
            item["ownership_state"] = "confirmed"
            received_event_id = add_event(
                store,
                item_id=args.item_id,
                event_type="received",
                occurred_on=args.checked_on,
                actor=args.actor,
                evidence_id=evidence_id,
                notes="Current possession established by physical check. " + (args.notes or ""),
                location_id=item.get("location_id"),
                container_id=item.get("container_id"),
            )
        quantity_event_id = None
        if args.quantity is not None:
            quantity_event_id = apply_quantity_change(
                store,
                item=item,
                quantity=args.quantity,
                unit=item["unit"],
                occurred_on=args.checked_on,
                actor=args.actor,
                evidence_id=evidence_id,
                notes=args.notes,
            )
        detail_amendment_id = append_item_detail_amendment(
            store,
            item=item,
            changes={
                **({"condition": args.condition} if args.condition is not None else {}),
                **(
                    {"serial_or_lot": args.serial_or_lot}
                    if args.serial_or_lot is not None
                    else {}
                ),
            },
            amended_on=args.checked_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=args.notes,
        )
        item["verified_on"] = args.checked_on
        physical_event_id = add_event(
            store,
            item_id=args.item_id,
            event_type="physically_verified",
            occurred_on=args.checked_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=args.notes,
            location_id=item.get("location_id"),
            container_id=item.get("container_id"),
        )
        return {
            "item_id": args.item_id,
            "evidence_id": evidence_id,
            "received_event_id": received_event_id,
            "quantity_event_id": quantity_event_id,
            "detail_amendment_id": detail_amendment_id,
            "physical_event_id": physical_event_id,
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"physical-check-{args.item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_return_loan(args: argparse.Namespace) -> dict:
    """Record an owned item returning from a loan to one stable location."""

    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        active_loans = [row for row in store.rows["item_party_relations"] if row["item_id"] == args.item_id
                        and row["role"] == "custodian" and row.get("custody_kind") == "loan" and row["status"] == "active"]
        if len(active_loans) != 1:
            raise InventoryError("return-loan requires exactly one active loan custody relation")
        stable_location_and_container(store, args.location_id, args.container_id)
        item["location_id"] = args.location_id
        item["container_id"] = args.container_id
        evidence_id = add_evidence(
            store,
            item_id=args.item_id,
            base=(
                f"loan-returned-{slug(args.item_id.removeprefix('itm-'), 34)}-"
                f"{args.returned_on}"
            ),
            evidence_type="user_source",
            source_ref=args.source_ref,
            captured_on=args.returned_on,
            claim_strength="explicit_current",
            notes=args.notes,
            minimum_sensitivity=location_context_sensitivity(
                store, args.location_id, args.container_id
            ),
        )
        event_id = add_event(
            store,
            item_id=args.item_id,
            event_type="loan_returned",
            occurred_on=args.returned_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=args.notes,
            location_id=args.location_id,
            container_id=args.container_id,
            details={"relation_id": active_loans[0]["relation_id"]},
        )
        active_loans[0].update(
            {
                "status": "ended",
                "ended_on": args.returned_on,
                "ended_evidence_id": evidence_id,
            }
        )
        return {
            "evidence_id": evidence_id,
            "event_id": event_id,
            "item_id": args.item_id,
            "ownership_state": "confirmed",
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"loan-returned-{args.item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_restore_current_ownership(args: argparse.Namespace) -> dict:
    """Correct or reacquire one terminal item without creating a duplicate record."""

    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        if item["ownership_state"] not in {"disposed", "refunded", "not_owned"}:
            raise InventoryError(
                "ownership restoration is only for disposed, refunded, or not-owned records"
            )
        if (
            args.reason == "reacquired"
            and item.get("quantity") is not None
            and args.quantity is None
        ):
            raise InventoryError(
                "reacquiring an item with a previous quantity requires --quantity; "
                "the prior ownership episode is not current quantity evidence"
            )
        stable_location_and_container(store, args.location_id, args.container_id)
        previous_state = item["ownership_state"]
        item.update(
            {
                "container_id": args.container_id,
                "location_id": args.location_id,
                "ownership_state": "confirmed",
                "verified_on": args.checked_on,
            }
        )
        evidence_id = add_evidence(
            store,
            item_id=args.item_id,
            base=(
                f"{args.reason}-{slug(args.item_id.removeprefix('itm-'), 34)}-"
                f"{args.checked_on}"
            ),
            evidence_type="physical_check",
            source_ref=args.source_ref,
            captured_on=args.checked_on,
            claim_strength="explicit_current",
            notes=args.notes,
            minimum_sensitivity=location_context_sensitivity(
                store, args.location_id, args.container_id
            ),
        )
        ownership_event_id = add_event(
            store,
            item_id=args.item_id,
            event_type=args.reason,
            occurred_on=args.checked_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=args.notes,
            location_id=args.location_id,
            container_id=args.container_id,
            details=(
                {
                    "condition_checked": args.condition,
                    "quantity_checked": args.quantity,
                    "reset_fields": list(REACQUISITION_RESET_FIELDS),
                    "unit": item.get("unit") if args.quantity is not None else None,
                }
                if args.reason == "reacquired"
                else None
            ),
        )
        quantity_event_id = None
        if args.quantity is not None:
            quantity_event_id = apply_quantity_change(
                store,
                item=item,
                quantity=args.quantity,
                unit=item["unit"],
                occurred_on=args.checked_on,
                actor=args.actor,
                evidence_id=evidence_id,
                notes=args.notes,
                record_unchanged=args.reason == "reacquired",
            )
        reset_amendment_id = None
        if args.reason == "reacquired":
            reset_amendment_id = append_item_detail_amendment(
                store,
                item=item,
                changes={
                    field: None for field in REACQUISITION_RESET_FIELDS
                },
                amended_on=args.checked_on,
                actor=args.actor,
                evidence_id=evidence_id,
                notes=args.notes,
                record_all_fields=True,
            )
        current_detail_amendment_id = append_item_detail_amendment(
            store,
            item=item,
            changes={
                **(
                    {"condition": args.condition}
                    if args.condition is not None
                    else {}
                ),
                **(
                    {"serial_or_lot": args.serial_or_lot}
                    if args.serial_or_lot is not None
                    else {}
                ),
            },
            amended_on=args.checked_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=args.notes,
        )
        detail_amendment_id = current_detail_amendment_id or reset_amendment_id
        physical_event_id = add_event(
            store,
            item_id=args.item_id,
            event_type="physically_verified",
            occurred_on=args.checked_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=args.notes,
            location_id=args.location_id,
            container_id=args.container_id,
        )
        return {
            "evidence_id": evidence_id,
            "item_id": args.item_id,
            "ownership_event_id": ownership_event_id,
            "ownership_state": "confirmed",
            "quantity_event_id": quantity_event_id,
            "detail_amendment_id": detail_amendment_id,
            "detail_amendment_ids": [
                amendment_id
                for amendment_id in (
                    reset_amendment_id,
                    current_detail_amendment_id,
                )
                if amendment_id is not None
            ],
            "physical_event_id": physical_event_id,
            "previous_state": previous_state,
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"{args.reason}-{args.item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def command_move(args: argparse.Namespace) -> dict:
    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        borrowed_in_possession = any(
            row["item_id"] == args.item_id
            and row["role"] == "custodian"
            and row["status"] == "active"
            and row.get("custody_kind") == "possession"
            for row in store.rows["item_party_relations"]
        )
        if item["ownership_state"] != "confirmed" and not (
            item["ownership_state"] == "not_owned" and borrowed_in_possession
        ):
            raise InventoryError(f"cannot move item in state {item['ownership_state']}")
        stable_location_and_container(store, args.location_id, args.container_id)
        item["location_id"] = args.location_id
        item["container_id"] = args.container_id
        evidence_id = add_evidence(
            store,
            item_id=args.item_id,
            base=f"moved-{slug(args.item_id.removeprefix('itm-'), 40)}-{args.moved_on}",
            evidence_type="user_source",
            source_ref=args.source_ref,
            captured_on=args.moved_on,
            claim_strength="explicit_current",
            notes=args.notes,
            minimum_sensitivity=location_context_sensitivity(
                store, args.location_id, args.container_id
            ),
        )
        event_id = add_event(
            store,
            item_id=args.item_id,
            event_type="moved",
            occurred_on=args.moved_on,
            actor=args.actor,
            evidence_id=evidence_id,
            notes=args.notes,
            location_id=args.location_id,
            container_id=args.container_id,
        )
        embodiment = next(
            (row for row in store.rows["location_embodiments"] if row["item_id"] == args.item_id),
            None,
        )
        if embodiment is not None:
            location = store.get("locations", embodiment["location_id"])
            previous = dict(location)
            location["parent_location_id"] = args.container_id or args.location_id
            replacement = dict(location)
            recorded_at = recorded_timestamp(None)
            amendment_id = store.allocate(
                "fact_amendments", f"fact-amend-embodiment-move-{args.item_id}-{args.moved_on}"
            )
            store.rows["fact_amendments"].append(
                {
                    "action": "replace",
                    "actor": args.actor,
                    "amended_on": args.moved_on,
                    "evidence_id": evidence_id,
                    "fact_amendment_id": amendment_id,
                    "notes": args.notes,
                    "previous_json": strict_json_dumps(previous, sort_keys=True),
                    "reason": "embodied location follows its item move",
                    "recorded_at": recorded_at,
                    "replacement_json": strict_json_dumps(replacement, sort_keys=True),
                    "selector_json": strict_json_dumps(
                        {"location_id": location["location_id"]}, sort_keys=True
                    ),
                    "sensitivity": max(
                        (
                            item["sensitivity"],
                            store.get("evidence", evidence_id)["sensitivity"],
                            location["sensitivity"],
                        ),
                        key=SENSITIVITY_RANK.__getitem__,
                    ),
                    "table_name": "locations",
                }
            )
        return {
            "item_id": args.item_id,
            "evidence_id": evidence_id,
            "event_id": event_id,
            "embodied_location_reparented": embodiment is not None,
            "location_amendment_id": amendment_id if embodiment is not None else None,
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"move-{args.item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def _add_party_evidence(store: Store, *, name: str, source_ref: str, captured_on: str,
                        sensitivity: str, notes: str | None) -> str:
    identity = strict_json_dumps({"captured_on": captured_on, "name": name,
                                  "source_ref": source_ref}, sort_keys=True)
    evidence_id = "ev-party-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    existing = [row for row in store.rows["evidence"] if row["evidence_id"] == evidence_id]
    if not existing:
        store.rows["evidence"].append({"evidence_id": evidence_id, "evidence_type": "user_source",
            "source_ref": source_ref, "captured_on": captured_on, "claim_strength": "explicit_current",
            "sensitivity": sensitivity, "notes": notes})
    return evidence_id


def command_add_party(args: argparse.Namespace) -> dict:
    def mutate(store: Store) -> dict:
        matches = [
            row
            for row in store.rows["parties"]
            if normalized(row["name"]) == normalized(args.name)
            and row["party_kind"] == args.party_kind
        ]
        if len(matches) > 1:
            raise InventoryError("party match is ambiguous; correct the existing records first")
        if matches:
            return {
                "party_id": matches[0]["party_id"],
                "evidence_id": matches[0]["evidence_id"],
                "reused": True,
            }
        evidence_id = _add_party_evidence(store, name=args.name, source_ref=args.source_ref,
            captured_on=args.captured_on, sensitivity=args.sensitivity, notes=args.notes)
        party_id = "party-" + hashlib.sha256(
            strict_json_dumps({"name": args.name, "kind": args.party_kind, "evidence": evidence_id}, sort_keys=True).encode()
        ).hexdigest()[:24]
        row = {"party_id": party_id, "name": args.name, "party_kind": args.party_kind,
               "evidence_id": evidence_id, "sensitivity": args.sensitivity, "notes": args.notes}
        store.rows["parties"].append(row)
        return {"party_id": party_id, "evidence_id": evidence_id, "reused": False}
    return transaction(args.inventory_root, args.runtime_dir, f"add-party-{slug(args.name, 32)}", mutate,
                       continue_batch=args.continue_batch)


def _relation_evidence(store: Store, args: argparse.Namespace, *, item_id: str, date_value: str) -> str:
    return add_evidence(store, item_id=item_id, base=f"custody-{slug(item_id, 28)}-{date_value}",
        evidence_type="user_source", source_ref=args.source_ref, captured_on=date_value,
        claim_strength="explicit_current", notes=args.notes,
        minimum_sensitivity=location_context_sensitivity(store, store.get("items", item_id).get("location_id"), store.get("items", item_id).get("container_id")))


def command_custody_start(args: argparse.Namespace) -> dict:
    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        if item["ownership_state"] not in {"confirmed", "unknown", "not_owned"}:
            raise InventoryError("custody requires a current item, not a planned or terminal record")
        party = None
        if args.party_id is not None:
            party = store.get("parties", args.party_id)
        if (args.quantity is None) != (args.unit is None):
            raise InventoryError("custody quantity and unit must be supplied together")
        if args.quantity is not None and args.unit != item["unit"]:
            raise InventoryError("custody unit must match the item unit")
        active = [
            row
            for row in store.rows["item_party_relations"]
            if row["item_id"] == args.item_id
            and row["role"] == "custodian"
            and row["status"] == "active"
        ]
        if args.quantity is None and active:
            raise InventoryError("unknown custody quantity cannot overlap another active allocation")
        if args.quantity is not None and any(row.get("quantity") is None for row in active):
            raise InventoryError("known custody quantity cannot overlap an unknown allocation")
        if args.quantity is not None:
            allocated = sum(float(row["quantity"]) for row in active)
            if item.get("quantity") is not None and allocated + args.quantity > float(item["quantity"]):
                raise InventoryError("active custody allocation would exceed item quantity")
        previous_placement = (item.get("location_id"), item.get("container_id"))
        if args.location_id is not None:
            stable_location_and_container(store, args.location_id, args.container_id)
            item["location_id"] = args.location_id
            item["container_id"] = args.container_id
        elif args.container_id is not None:
            raise InventoryError("custody container requires a custody location")
        evidence_id = _relation_evidence(store, args, item_id=args.item_id, date_value=args.started_on)
        relation_id = store.allocate("item_party_relations", f"rel-custody-{slug(args.item_id, 28)}-{args.started_on}")
        store.rows["item_party_relations"].append({"relation_id": relation_id, "item_id": args.item_id,
            "party_id": args.party_id, "role": "custodian", "custody_kind": args.custody_kind,
            "status": "active", "started_on": args.started_on, "ended_on": None,
            "ended_evidence_id": None, "due_on": args.due_on,
            "quantity": args.quantity, "unit": args.unit, "evidence_id": evidence_id,
            "sensitivity": max((item["sensitivity"], store.get("evidence", evidence_id)["sensitivity"],
                party["sensitivity"] if party is not None else "low"), key=SENSITIVITY_RANK.__getitem__),
            "notes": args.notes})
        event_ids = []
        if args.location_id is not None and previous_placement != (args.location_id, args.container_id):
            event_ids.append(add_event(store, item_id=args.item_id, event_type="moved",
                occurred_on=args.started_on, actor=args.actor, evidence_id=evidence_id, notes=args.notes,
                location_id=item.get("location_id"), container_id=item.get("container_id")))
        event_ids.append(add_event(store, item_id=args.item_id,
            event_type="lent" if args.custody_kind == "loan" else "custody_started",
            occurred_on=args.started_on, actor=args.actor, evidence_id=evidence_id, notes=args.notes,
            location_id=(item.get("location_id") if args.custody_kind == "loan" else None),
            container_id=(item.get("container_id") if args.custody_kind == "loan" else None),
            details={"relation_id": relation_id}))
        return {"relation_id": relation_id, "evidence_id": evidence_id,
                "event_id": event_ids[-1], "event_ids": event_ids}
    return transaction(args.inventory_root, args.runtime_dir, f"custody-start-{args.item_id}", mutate,
                       continue_batch=args.continue_batch)


def command_custody_end(args: argparse.Namespace) -> dict:
    def mutate(store: Store) -> dict:
        relation = store.get("item_party_relations", args.relation_id)
        if relation["role"] != "custodian" or relation["status"] != "active":
            raise InventoryError("custody-end requires one active custody relation")
        evidence_id = _relation_evidence(store, args, item_id=relation["item_id"], date_value=args.ended_on)
        relation.update({"status": "ended", "ended_on": args.ended_on,
                         "ended_evidence_id": evidence_id})
        item = store.get("items", relation["item_id"])
        if args.location_id is not None:
            stable_location_and_container(store, args.location_id, args.container_id)
            item["location_id"] = args.location_id
            item["container_id"] = args.container_id
        elif args.container_id is not None:
            raise InventoryError("custody container requires a custody location")
        event_ids = []
        if args.location_id is not None:
            event_ids.append(add_event(store, item_id=relation["item_id"], event_type="moved",
                occurred_on=args.ended_on, actor=args.actor, evidence_id=evidence_id,
                notes=args.notes, location_id=args.location_id, container_id=args.container_id))
        event_ids.append(add_event(store, item_id=relation["item_id"],
            event_type="loan_returned" if relation["custody_kind"] == "loan" else "custody_ended",
            occurred_on=args.ended_on, actor=args.actor, evidence_id=evidence_id, notes=args.notes,
            location_id=(item.get("location_id") if relation["custody_kind"] == "loan" else None),
            container_id=(item.get("container_id") if relation["custody_kind"] == "loan" else None),
            details={"relation_id": relation["relation_id"]}))
        return {"relation_id": relation["relation_id"], "evidence_id": evidence_id,
                "event_id": event_ids[-1], "event_ids": event_ids}
    return transaction(args.inventory_root, args.runtime_dir, f"custody-end-{args.relation_id}", mutate,
                       continue_batch=args.continue_batch)


def command_set_home(args: argparse.Namespace) -> dict:
    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        if args.clear:
            if args.container_id is not None:
                raise InventoryError("clearing home cannot name a container")
            location_id = None
            container_id = None
        else:
            location_id = args.location_id
            container_id = args.container_id
            stable_location_and_container(store, location_id, container_id)
        evidence_id = add_evidence(store, item_id=args.item_id, base=f"home-{slug(args.item_id, 28)}-{args.set_on}",
            evidence_type="user_source", source_ref=args.source_ref, captured_on=args.set_on,
            claim_strength="explicit_current", notes=args.notes,
            minimum_sensitivity=location_context_sensitivity(store, location_id, container_id))
        amendment_id = append_item_detail_amendment(store, item=item, changes={"home_location_id": location_id,
            "home_container_id": container_id}, amended_on=args.set_on, actor=args.actor,
            evidence_id=evidence_id, notes=args.notes)
        if amendment_id is None:
            raise InventoryError("home is already recorded with this exact placement")
        return {"item_id": args.item_id, "evidence_id": evidence_id, "detail_amendment_id": amendment_id}
    return transaction(args.inventory_root, args.runtime_dir, f"set-home-{args.item_id}", mutate,
                       continue_batch=args.continue_batch)


def _named_relation_change(
    args: argparse.Namespace, *, role: str, ending: bool
) -> dict:
    start_event = "access_granted" if role == "access" else "ownership_started"
    end_event = "access_revoked" if role == "access" else "ownership_ended"
    start_date_argument = "granted_on" if role == "access" else "started_on"
    end_date_argument = "revoked_on" if role == "access" else "ended_on"

    def mutate(store: Store) -> dict:
        if ending:
            relation = store.get("item_party_relations", args.relation_id)
            if relation["role"] != role or relation["status"] != "active":
                raise InventoryError(
                    f"{role} end requires one active {role} relation"
                )
            item_id = relation["item_id"]
            party_id = relation["party_id"]
            event_date = getattr(args, end_date_argument)
        else:
            party = store.get("parties", args.party_id)
            store.get("items", args.item_id)
            item_id = args.item_id
            party_id = args.party_id
            event_date = getattr(args, start_date_argument)
            if any(
                row["item_id"] == item_id
                and row["party_id"] == party_id
                and row["role"] == role
                and row["status"] == "active"
                for row in store.rows["item_party_relations"]
            ):
                raise InventoryError(
                    f"item already has this party as an active {role}"
                )
        evidence_id = _relation_evidence(store, args, item_id=item_id, date_value=event_date)
        if ending:
            relation.update({"status": "ended", "ended_on": event_date,
                             "ended_evidence_id": evidence_id})
            event_id = add_event(store, item_id=item_id, event_type=end_event,
                occurred_on=event_date, actor=args.actor, evidence_id=evidence_id,
                notes=args.notes, details={"relation_id": relation["relation_id"]})
            return {"relation_id": relation["relation_id"], "evidence_id": evidence_id,
                    "event_id": event_id}
        relation_id = store.allocate(
            "item_party_relations",
            f"rel-{role}-{slug(item_id, 28)}-{slug(party_id, 20)}-{event_date}",
        )
        store.rows["item_party_relations"].append({"relation_id": relation_id, "item_id": item_id, "party_id": party_id,
            "role": role, "custody_kind": None, "status": "active", "started_on": event_date, "ended_on": None,
            "ended_evidence_id": None, "due_on": None, "quantity": None, "unit": None,
            "evidence_id": evidence_id,
            "sensitivity": max((store.get("items", item_id)["sensitivity"],
                store.get("evidence", evidence_id)["sensitivity"], party["sensitivity"]),
                key=SENSITIVITY_RANK.__getitem__), "notes": args.notes})
        event_id = add_event(store, item_id=item_id, event_type=start_event,
            occurred_on=event_date, actor=args.actor, evidence_id=evidence_id, notes=args.notes,
            details={"relation_id": relation_id})
        return {"relation_id": relation_id, "evidence_id": evidence_id, "event_id": event_id}
    action = f"{role}-end" if ending else f"{role}-start"
    return transaction(
        args.inventory_root,
        args.runtime_dir,
        action,
        mutate,
        continue_batch=args.continue_batch,
    )


def command_access_grant(args: argparse.Namespace) -> dict:
    return _named_relation_change(args, role="access", ending=False)


def command_access_revoke(args: argparse.Namespace) -> dict:
    return _named_relation_change(args, role="access", ending=True)


def command_ownership_start(args: argparse.Namespace) -> dict:
    return _named_relation_change(args, role="owner", ending=False)


def command_ownership_end(args: argparse.Namespace) -> dict:
    return _named_relation_change(args, role="owner", ending=True)


def command_embody_location(args: argparse.Namespace) -> dict:
    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        location = store.get("locations", args.location_id)
        placement = item.get("container_id") or item.get("location_id")
        if item["ownership_state"] != "confirmed" or placement != location.get("parent_location_id"):
            raise InventoryError("embodied location must be owned/current and parented at the item's current placement")
        evidence_id = add_evidence(store, item_id=args.item_id, base=f"embody-{slug(args.item_id, 28)}-{args.recorded_on}",
            evidence_type="user_source", source_ref=args.source_ref, captured_on=args.recorded_on,
            claim_strength="explicit_current", notes=args.notes,
            minimum_sensitivity=location_context_sensitivity(store, placement))
        embodiment_id = store.allocate("location_embodiments", f"emb-{slug(args.item_id, 28)}")
        store.rows["location_embodiments"].append({"embodiment_id": embodiment_id, "item_id": args.item_id,
            "location_id": args.location_id, "evidence_id": evidence_id,
            "sensitivity": max((item["sensitivity"], location["sensitivity"]), key=SENSITIVITY_RANK.__getitem__), "notes": args.notes})
        return {"embodiment_id": embodiment_id, "evidence_id": evidence_id}
    return transaction(args.inventory_root, args.runtime_dir, f"embody-location-{args.item_id}", mutate,
                       continue_batch=args.continue_batch)


def command_change(args: argparse.Namespace) -> dict:
    state_by_event = {
        "returned": "refunded",
        "cancelled": "refunded",
        "refunded": "refunded",
        "gifted": "disposed",
        "disposed": "disposed",
        "lost": "disposed",
        "ownership_unresolved": "unknown",
        "ownership_excluded": "not_owned",
    }
    claim_by_event = {
        "returned": "explicit_not_owned",
        "cancelled": "explicit_not_owned",
        "refunded": "explicit_not_owned",
        "gifted": "explicit_not_owned",
        "disposed": "explicit_not_owned",
        "lost": "explicit_not_owned",
        "lent": "explicit_current",
        "ownership_unresolved": "claimed_owned",
        "ownership_excluded": "explicit_not_owned",
        "quantity_changed": "explicit_current",
    }
    allowed_states_by_event = {
        "returned": {"confirmed"},
        "cancelled": {"candidate", "planned"},
        "refunded": {"candidate", "planned", "confirmed"},
        "gifted": {"confirmed"},
        "disposed": {"confirmed"},
        "lost": {"confirmed"},
        "lent": {"confirmed"},
        "ownership_unresolved": {"candidate", "planned", "confirmed", "unknown"},
        "ownership_excluded": {"candidate", "planned", "unknown"},
        "quantity_changed": {"confirmed"},
    }

    occurred_on, observed_on, date_precision = resolved_event_date(
        exact_date=args.occurred_on,
        date_unknown=args.date_unknown,
        observed_on=args.observed_on,
        label="lifecycle event",
    )

    def mutate(store: Store) -> dict:
        item = store.get("items", args.item_id)
        previous_state = item["ownership_state"]
        previous_location = item.get("location_id")
        previous_container = item.get("container_id")
        if previous_state not in allowed_states_by_event[args.event_type]:
            raise InventoryError(
                f"cannot apply {args.event_type} to item in state {previous_state}"
            )
        if args.event_type != "lent" and (
            args.location_id is not None or args.container_id is not None
        ):
            raise InventoryError("location options are only valid for a lent event")
        if args.event_type == "quantity_changed":
            if args.quantity is None:
                raise InventoryError("quantity_changed requires --quantity")
        elif args.event_type != "lent":
            item["ownership_state"] = state_by_event[args.event_type]
        loan_relation_id = None
        if args.event_type == "lent":
            if args.location_id is None:
                item["location_id"] = "loc-unknown"
                item["container_id"] = None
            else:
                stable_location_and_container(
                    store, args.location_id, args.container_id
                )
                item["location_id"] = args.location_id
                item["container_id"] = args.container_id
        if item["ownership_state"] in {"disposed", "refunded", "not_owned"}:
            item["location_id"] = None
            item["container_id"] = None
        evidence_id = add_evidence(
            store,
            item_id=args.item_id,
            base=(
                f"{args.event_type}-{slug(args.item_id.removeprefix('itm-'), 26)}-"
                f"{date_precision}-{occurred_on or observed_on}"
            ),
            evidence_type="user_source",
            source_ref=args.source_ref,
            captured_on=observed_on,
            claim_strength=claim_by_event[args.event_type],
            notes=args.notes,
            minimum_sensitivity=location_context_sensitivity(
                store,
                previous_location,
                previous_container,
                item.get("location_id"),
                item.get("container_id"),
            ),
        )
        if args.event_type == "lent":
            if any(row["item_id"] == args.item_id and row["role"] == "custodian" and row["status"] == "active"
                   for row in store.rows["item_party_relations"]):
                raise InventoryError("item already has active custody; use custody-end or return-loan first")
            loan_relation_id = store.allocate(
                "item_party_relations",
                f"rel-custody-{slug(args.item_id, 28)}-{occurred_on or observed_on}",
            )
            store.rows["item_party_relations"].append({
                "relation_id": loan_relation_id,
                "item_id": args.item_id, "party_id": None, "role": "custodian", "custody_kind": "loan",
                "status": "active", "started_on": occurred_on, "ended_on": None,
                "ended_evidence_id": None, "due_on": None,
                "quantity": None, "unit": None, "evidence_id": evidence_id,
                "sensitivity": max(
                    (
                        item["sensitivity"],
                        store.get("evidence", evidence_id)["sensitivity"],
                    ),
                    key=SENSITIVITY_RANK.__getitem__,
                ),
                "notes": args.notes,
            })
        terminal = item["ownership_state"] in {"disposed", "refunded", "not_owned"}
        if args.event_type == "quantity_changed":
            event_id = apply_quantity_change(
                store,
                item=item,
                quantity=args.quantity,
                unit=args.unit or item["unit"],
                occurred_on=occurred_on,
                occurred_on_precision=date_precision,
                observed_on=observed_on,
                actor=args.actor,
                evidence_id=evidence_id,
                notes=args.notes,
            )
            if event_id is None:
                raise InventoryError("quantity_changed would make no change")
        else:
            event_id = add_event(
                store,
                item_id=args.item_id,
                event_type=args.event_type,
                occurred_on=occurred_on,
                occurred_on_precision=date_precision,
                observed_on=observed_on,
                actor=args.actor,
                evidence_id=evidence_id,
                notes=args.notes,
                location_id=(previous_location if terminal else item.get("location_id")),
                container_id=(previous_container if terminal else item.get("container_id")),
                details=(
                    {"relation_id": loan_relation_id}
                    if loan_relation_id is not None
                    else None
                ),
            )
        return {
            "item_id": args.item_id,
            "event_id": event_id,
            "evidence_id": evidence_id,
            "previous_state": previous_state,
            "ownership_state": item["ownership_state"],
            "quantity": item.get("quantity"),
            "unit": item.get("unit"),
            "occurred_on": occurred_on,
            "occurred_on_precision": date_precision,
            "observed_on": observed_on,
        }

    return transaction(
        args.inventory_root,
        args.runtime_dir,
        f"{args.event_type}-{args.item_id}",
        mutate,
        continue_batch=args.continue_batch,
    )


def add_actor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", required=True, help="Agent or person recording the change")


def add_source(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source-ref", required=True, help="Concrete receipt, message, photo, or check reference"
    )
    parser.add_argument("--notes")


def add_retrieval_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--category")
    parser.add_argument("--ownership-state")
    parser.add_argument("--condition")
    parser.add_argument(
        "--location", help="Visible location ID or name, including a visible ancestor"
    )
    parser.add_argument("--tag")
    parser.add_argument("--alias-kind")
    parser.add_argument("--interface-family")
    parser.add_argument("--interface-standard")
    parser.add_argument("--interface-variant")
    parser.add_argument(
        "--interface-direction",
        choices=("plug", "socket", "bidirectional", "unknown"),
    )
    parser.add_argument("--location-known", choices=("known", "unknown"))


def build_parser() -> argparse.ArgumentParser:
    parser = InventoryArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        help="Versioned JSON instance configuration; defaults to Application Support",
    )
    parser.add_argument("--instance", help="Named instance from the configuration file")
    parser.add_argument(
        "--inventory-root",
        type=Path,
        help="Directory containing the private Data/store canonical ledger",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="Directory for SQLite, rollback backups, locks, and other generated runtime files",
    )
    parser.add_argument(
        "--media-root",
        type=Path,
        help="Independent content-addressed durable media directory",
    )
    parser.add_argument(
        "--catalogue-output",
        type=Path,
        help="Independent generated Markdown catalogue path",
    )
    parser.add_argument(
        "--catalogue-scope",
        choices=("public", "personal", "private"),
        help="Sensitivity scope of the generated catalogue",
    )
    parser.add_argument(
        "--capture-adapters-config",
        type=Path,
        default=(
            Path(os.environ["PROPERTY_INVENTORY_CAPTURE_ADAPTERS"])
            if os.environ.get("PROPERTY_INVENTORY_CAPTURE_ADAPTERS")
            else None
        ),
        help="Server-owned named capture adapter registry",
    )
    parser.add_argument(
        "--forbidden-root",
        type=Path,
        action="append",
        dest="forbidden_roots",
        help="Root which canonical data, media, and runtime must not overlap",
    )
    parser.add_argument(
        "--continue-batch",
        action="store_true",
        help=(
            "Allow an already-dirty canonical store only after confirming its changes "
            "belong to this same single-writer batch"
        ),
    )
    parser.add_argument(
        "--scope",
        choices=("public", "personal", "private"),
        default=os.environ.get("PROPERTY_INVENTORY_SCOPE", "private"),
        help="Read sensitivity scope; private is intended only for the trusted local owner",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create and verify a new empty inventory root")
    init_parser.set_defaults(function=command_init)

    migrate_parser = subparsers.add_parser(
        "migrate", help="Back up and migrate a supported inventory to the current schema"
    )
    migrate_parser.set_defaults(function=command_migrate)

    media_parser = subparsers.add_parser(
        "attach-media", help="Content-address a real file and link it to existing evidence"
    )
    media_parser.add_argument("--evidence-id", required=True)
    media_parser.add_argument("--file", type=Path, required=True)
    media_parser.add_argument(
        "--role",
        choices=("source", "crop", "receipt", "appraisal", "manual", "other"),
        required=True,
    )
    media_parser.add_argument("--region", type=json_object)
    media_parser.add_argument("--captured-on", type=valid_date)
    media_parser.add_argument("--media-type")
    media_parser.add_argument(
        "--sensitivity", choices=("low", "personal", "high"), default="personal"
    )
    media_parser.set_defaults(function=command_attach_media)

    export_parser = subparsers.add_parser(
        "export", help="Create a verified portable archive of canonical rows and media"
    )
    export_parser.add_argument("--output", type=Path, required=True)
    export_parser.set_defaults(function=command_export)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Export, restore into fresh blank roots, and verify the restored inventory",
    )
    doctor_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New retained export archive outside all managed roots",
    )
    doctor_parser.set_defaults(function=command_doctor)

    restore_parser = subparsers.add_parser(
        "restore", help="Restore a verified export into blank inventory and media roots"
    )
    restore_parser.add_argument("--archive", type=Path, required=True)
    restore_parser.add_argument(
        "--allow-unsafe-legacy",
        action="store_true",
        help="Allow a deliberately degraded restore of a format-1 export without auxiliaries",
    )
    restore_parser.set_defaults(function=command_restore)

    auxiliary_parser = subparsers.add_parser(
        "auxiliary-manifest",
        help="Hash required policy and source inputs so verification and export fail closed",
    )
    auxiliary_parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Additional Data-relative file to include; repeat for several files",
    )
    auxiliary_parser.add_argument(
        "--remove",
        action="append",
        default=[],
        help="Remove a deleted Data-relative file from the manifest; repeat as needed",
    )
    auxiliary_parser.add_argument("--replace", action="store_true")
    auxiliary_parser.set_defaults(function=command_auxiliary_manifest)

    status_parser = subparsers.add_parser(
        "status", help="Rebuild and verify the current canonical store"
    )
    status_parser.add_argument(
        "--summary",
        action="store_true",
        help="Return the integrity-gate result; failure details stay hidden outside private scope",
    )
    status_parser.set_defaults(function=command_status)

    maintenance_start_parser = subparsers.add_parser(
        "maintenance-start", help="Start one runtime-only measured upkeep session"
    )
    maintenance_start_parser.add_argument("--performed-on", type=valid_date, required=True)
    maintenance_start_parser.add_argument("--activity", required=True)
    maintenance_evidence = maintenance_start_parser.add_mutually_exclusive_group(required=True)
    maintenance_evidence.add_argument("--evidence-id")
    maintenance_evidence.add_argument("--source-ref")
    maintenance_start_parser.add_argument(
        "--evidence-type", choices=("user_source", "research", "vault_note"), default="user_source"
    )
    maintenance_start_parser.add_argument(
        "--sensitivity", choices=("low", "personal", "high"), default="personal"
    )
    maintenance_start_parser.add_argument("--notes")
    maintenance_start_parser.set_defaults(function=command_maintenance_start)

    maintenance_finish_parser = subparsers.add_parser(
        "maintenance-finish", help="Commit one measured upkeep session and resolve its timer"
    )
    maintenance_finish_parser.add_argument("maintenance_session_id")
    maintenance_finish_parser.add_argument("--elapsed-seconds", type=nonnegative_int)
    maintenance_finish_parser.add_argument("--correction-count", type=nonnegative_int)
    maintenance_finish_parser.add_argument("--review-count", type=nonnegative_int)
    maintenance_finish_parser.add_argument("--item-id", action="append", dest="item_ids", default=[])
    maintenance_finish_parser.set_defaults(function=command_maintenance_finish)

    maintenance_report_parser = subparsers.add_parser(
        "maintenance-report", help="Return scope-safe observed upkeep records and counts"
    )
    maintenance_report_parser.set_defaults(function=command_maintenance_report)

    maintenance_harness_parser = subparsers.add_parser(
        "maintenance-harness", help="Run the explicitly synthetic four-week upkeep fixture"
    )
    maintenance_harness_parser.add_argument("--input", type=Path, required=True)
    maintenance_harness_parser.set_defaults(function=command_maintenance_harness)

    compatibility_status_parser = subparsers.add_parser(
        "compatibility-status",
        aliases=("compatibility-matrix",),
        help="Show the executable Python and schema migration compatibility matrix",
    )
    compatibility_status_parser.set_defaults(function=command_compatibility_status)

    insurance_status_parser = subparsers.add_parser(
        "insurance-status",
        help="Return scope-safe, evidence-backed insurance readiness and gaps",
    )
    insurance_status_parser.set_defaults(function=command_insurance_status)

    insurance_export_parser = subparsers.add_parser(
        "insurance-export",
        help="Write a deterministic private JSON/CSV/media insurance ZIP package",
    )
    insurance_export_parser.add_argument("--output", type=Path, required=True)
    insurance_export_parser.set_defaults(function=command_insurance_export)

    insurance_validate_parser = subparsers.add_parser(
        "insurance-validate",
        help="Validate an existing private insurance ZIP package without extracting it",
    )
    insurance_validate_parser.add_argument("--package", type=Path, required=True)
    insurance_validate_parser.set_defaults(function=command_insurance_validate)

    rebind_parser = subparsers.add_parser(
        "runtime-rebind",
        help="Bind a verified quiescent inventory to a new empty runtime directory",
    )
    rebind_parser.add_argument(
        "--from-runtime",
        type=Path,
        required=True,
        help="Current runtime path recorded in the inventory binding",
    )
    rebind_parser.add_argument(
        "--from-catalogue-output",
        type=Path,
        help="Previous catalogue path recorded in the runtime owner marker",
    )
    rebind_parser.add_argument(
        "--from-media-root",
        type=Path,
        help="Previous media root recorded in the runtime owner marker",
    )
    rebind_parser.set_defaults(function=command_runtime_rebind)

    search_parser = subparsers.add_parser(
        "search", help="Search canonical property records before deciding or buying"
    )
    search_parser.add_argument("query", nargs="+")
    search_parser.add_argument("--limit", type=int, default=50)
    search_parser.add_argument("--cursor")
    search_parser.add_argument(
        "--summary",
        action="store_true",
        help="Return names, ownership, condition, location, last physical-check date, evidence types, and empty-result meaning",
    )
    add_retrieval_filters(search_parser)
    search_parser.set_defaults(function=command_search)

    list_items_parser = subparsers.add_parser(
        "list-items",
        help="Enumerate all scope-visible items matching typed filters, including one area",
    )
    list_items_parser.add_argument("--limit", type=int, default=500)
    list_items_parser.add_argument("--cursor")
    add_retrieval_filters(list_items_parser)
    list_items_parser.set_defaults(function=command_list_items)

    locations_read_parser = subparsers.add_parser(
        "locations",
        help=(
            "List or resolve scope-visible spatial nodes with their full root-to-leaf path; "
            "the query matches the whole path, not only the leaf name"
        ),
    )
    locations_read_parser.add_argument("--query")
    locations_read_parser.add_argument("--parent-location-id")
    locations_read_parser.add_argument(
        "--kind", choices=LOCATION_KIND_CHOICES
    )
    locations_read_parser.add_argument("--limit", type=int, default=500)
    locations_read_parser.add_argument("--cursor")
    locations_read_parser.set_defaults(function=command_locations)

    context_parser = subparsers.add_parser(
        "context",
        help="Return scope-safe inventory context and explicit unknowns for one task",
    )
    context_parser.add_argument("--task", required=True)
    context_parser.add_argument("--limit", type=int, default=50)
    context_parser.add_argument("--cursor")
    add_retrieval_filters(context_parser)
    context_parser.set_defaults(function=command_context)

    show_parser = subparsers.add_parser(
        "show", help="Show one item with evidence, lifecycle, and relationships"
    )
    show_parser.add_argument("item_id")
    show_parser.set_defaults(function=command_show)

    compatibility_parser = subparsers.add_parser(
        "compatibility",
        help="Return only evidence-supported compatibility between two recorded items",
    )
    compatibility_parser.add_argument("first_item_id")
    compatibility_parser.add_argument("second_item_id")
    compatibility_parser.set_defaults(function=command_compatibility)

    kit_status_parser = subparsers.add_parser(
        "kit-status", help="Return conservative evidence-backed readiness for operational kits"
    )
    kit_selector = kit_status_parser.add_mutually_exclusive_group()
    kit_selector.add_argument("--kit-id")
    kit_selector.add_argument("--serves-item-id")
    kit_status_parser.set_defaults(function=command_kit_status)

    torque_check_parser = subparsers.add_parser(
        "torque-check", help="Check requested torque against every recorded limiting fact"
    )
    torque_selector = torque_check_parser.add_mutually_exclusive_group(required=True)
    torque_selector.add_argument("--path-id")
    torque_selector.add_argument("--tool-item-id")
    torque_check_parser.add_argument(
        "--requested-nm", type=non_negative_number, required=True
    )
    torque_check_parser.set_defaults(function=command_torque_check)

    space_parser = subparsers.add_parser(
        "space", help="Return only checked spatial profiles visible for one location"
    )
    space_parser.add_argument("location_id")
    space_parser.set_defaults(function=command_space)

    fit_parser = subparsers.add_parser(
        "fit", help="Check caller-supplied measured item dimensions against a visible container box"
    )
    fit_parser.add_argument("location_id")
    fit_subject = fit_parser.add_mutually_exclusive_group(required=True)
    fit_subject.add_argument("--item-dimensions", type=json_object)
    fit_subject.add_argument("--item-id")
    fit_parser.add_argument("--no-rotation", action="store_true")
    fit_parser.set_defaults(function=command_fit)

    pack_parser = subparsers.add_parser(
        "pack",
        help="Plan deterministic packing of supplied measured items into a visible container box",
    )
    pack_parser.add_argument("location_id")
    pack_subject = pack_parser.add_mutually_exclusive_group(required=True)
    pack_subject.add_argument("--items", type=json_array)
    pack_subject.add_argument("--item-id", action="append", dest="item_ids")
    pack_parser.add_argument("--no-rotation", action="store_true")
    pack_parser.set_defaults(function=command_pack)

    free_volume_parser = subparsers.add_parser(
        "free-volume",
        help="Calculate free volume from a checked container and positioned occupied boxes",
    )
    free_volume_parser.add_argument("location_id")
    free_volume_parser.add_argument(
        "--occupied-box", type=json_object, action="append", default=[]
    )
    free_volume_parser.set_defaults(function=command_free_volume)

    item_dimensions_parser = subparsers.add_parser(
        "add-item-dimensions",
        help="Append evidence-backed owned-item measurements without replacing history",
    )
    item_dimensions_parser.add_argument("--item-id", required=True)
    item_dimensions_parser.add_argument("--width", type=positive_number)
    item_dimensions_parser.add_argument("--height", type=positive_number)
    item_dimensions_parser.add_argument("--depth", type=positive_number)
    item_dimensions_parser.add_argument(
        "--unit", choices=("mm", "cm", "m", "in", "ft"), required=True
    )
    item_dimensions_parser.add_argument("--measured-on", type=valid_date, required=True)
    item_dimensions_parser.add_argument("--recorded-at", type=valid_timestamp)
    item_dimensions_parser.add_argument("--evidence-id", required=True)
    item_dimensions_parser.add_argument(
        "--sensitivity", choices=("low", "personal", "high"), default="personal"
    )
    item_dimensions_parser.add_argument("--notes")
    item_dimensions_parser.set_defaults(function=command_add_item_dimensions)

    interface_parser = subparsers.add_parser(
        "add-interface",
        help="Add an evidence-bearing normalized interface claim to a model",
    )
    interface_parser.add_argument("--model-id", required=True)
    interface_parser.add_argument("--evidence-id", required=True)
    interface_parser.add_argument("--family", required=True)
    interface_parser.add_argument("--standard")
    interface_parser.add_argument("--variant")
    interface_parser.add_argument(
        "--direction",
        choices=("plug", "socket", "bidirectional", "unknown"),
        required=True,
    )
    interface_parser.add_argument(
        "--role", choices=("provides", "requires", "accepts"), required=True
    )
    interface_parser.add_argument("--properties", type=json_object, default={})
    interface_parser.add_argument("--notes")
    interface_parser.set_defaults(function=command_add_interface)

    alias_parser = subparsers.add_parser(
        "add-alias",
        help="Add an evidence-backed retrieval alias to an existing item",
    )
    alias_parser.add_argument("--item-id", required=True)
    alias_parser.add_argument("--alias", required=True)
    alias_parser.add_argument("--alias-kind", required=True)
    alias_parser.add_argument("--evidence-id", required=True)
    alias_parser.add_argument("--sensitivity", choices=("low", "personal", "high"), required=True)
    alias_parser.add_argument("--notes")
    alias_parser.set_defaults(function=command_add_alias)

    valuation_parser = subparsers.add_parser(
        "add-valuation",
        help="Add an evidence-backed value fact without asserting ownership",
    )
    valuation_parser.add_argument("--item-id", required=True)
    valuation_parser.add_argument("--amount", type=non_negative_number, required=True)
    valuation_parser.add_argument("--currency", type=currency_code, required=True)
    valuation_parser.add_argument("--valued-on", type=valid_date, required=True)
    valuation_parser.add_argument(
        "--basis",
        choices=("replacement", "appraisal", "purchase", "receipt", "market", "other"),
        required=True,
    )
    valuation_parser.add_argument("--evidence-id")
    valuation_parser.add_argument("--source-ref")
    valuation_parser.add_argument("--captured-on", type=valid_date)
    valuation_parser.add_argument(
        "--evidence-type",
        choices=("user_source", "vault_note", "research"),
    )
    valuation_parser.add_argument(
        "--sensitivity",
        choices=("low", "personal", "high"),
        required=True,
    )
    valuation_parser.add_argument("--notes")
    valuation_parser.set_defaults(function=command_add_valuation)

    tag_parser = subparsers.add_parser(
        "add-tag", help="Add an item-scoped classification tag"
    )
    tag_parser.add_argument("--item-id", required=True)
    tag_parser.add_argument("--tag", required=True)
    tag_parser.add_argument("--evidence-id", required=True)
    tag_parser.add_argument(
        "--sensitivity", choices=("low", "personal", "high"), default="personal"
    )
    tag_parser.add_argument("--notes")
    tag_parser.set_defaults(function=command_add_tag)

    kit_parser = subparsers.add_parser(
        "add-kit", help="Add one evidence-backed operational kit"
    )
    kit_parser.add_argument("--name", required=True)
    kit_parser.add_argument("--serves-item-id", required=True)
    kit_parser.add_argument("--evidence-id", required=True)
    kit_parser.add_argument("--kit-id")
    kit_parser.add_argument("--notes")
    kit_parser.set_defaults(function=command_add_kit)

    requirement_parser = subparsers.add_parser(
        "set-kit-requirement",
        help="Set one evidence-backed current kit requirement result",
    )
    add_actor(requirement_parser)
    requirement_parser.add_argument("--kit-id", required=True)
    requirement_parser.add_argument("--requirement-key", required=True)
    requirement_parser.add_argument("--item-id")
    requirement_parser.add_argument(
        "--status",
        choices=(
            "source_present",
            "exists_unassigned",
            "purchase_candidate",
            "not_recorded",
            "needs_verification",
        ),
        required=True,
    )
    requirement_parser.add_argument("--evidence-id", required=True)
    requirement_parser.add_argument("--recorded-at", type=valid_timestamp)
    requirement_parser.add_argument("--notes")
    requirement_parser.set_defaults(function=command_set_kit_requirement)

    kit_review_parser = subparsers.add_parser(
        "review-kit",
        help="Seal whether the current named requirement list is complete",
    )
    add_actor(kit_review_parser)
    kit_review_parser.add_argument("--kit-id", required=True)
    kit_review_parser.add_argument("--reviewed-on", type=valid_date, required=True)
    kit_review_parser.add_argument("--recorded-at", type=valid_timestamp)
    kit_review_parser.add_argument(
        "--completeness", choices=("complete", "incomplete"), required=True
    )
    kit_review_parser.add_argument(
        "--source-ref",
        required=True,
        help="Concrete statement or check that this is the complete current requirement list",
    )
    kit_review_parser.add_argument("--notes")
    kit_review_parser.set_defaults(function=command_review_kit)

    torque_parser = subparsers.add_parser(
        "add-torque-path", help="Add one evidence-backed tool torque path"
    )
    torque_parser.add_argument("--tool-item-id", required=True)
    torque_parser.add_argument("--output-drive", required=True)
    torque_parser.add_argument("--min-torque-nm", type=non_negative_number)
    torque_parser.add_argument("--max-torque-nm", type=non_negative_number)
    torque_parser.add_argument("--adapter-description")
    torque_parser.add_argument("--adapter-max-torque-nm", type=non_negative_number)
    torque_parser.add_argument(
        "--status",
        choices=(
            "direct",
            "adapter_rating_unknown",
            "attachment_only",
            "needs_verification",
        ),
        required=True,
    )
    torque_parser.add_argument("--evidence-id", required=True)
    torque_parser.add_argument("--path-id")
    torque_parser.add_argument("--notes")
    torque_parser.set_defaults(function=command_add_torque_path)

    space_mutation_parser = subparsers.add_parser(
        "add-space", help="Store one checked container-box profile without locating any item"
    )
    space_mutation_parser.add_argument("--location-id", required=True)
    space_mutation_parser.add_argument("--evidence-id")
    space_mutation_parser.add_argument("--source-ref")
    space_mutation_parser.add_argument("--captured-on", type=valid_date)
    space_mutation_parser.add_argument(
        "--evidence-type", choices=("research", "vault_note"), default="research"
    )
    space_mutation_parser.add_argument("--evidence-notes")
    space_mutation_parser.add_argument("--profile", type=json_object, required=True)
    space_mutation_parser.add_argument(
        "--sensitivity", choices=("low", "personal", "high"), required=True
    )
    space_mutation_parser.add_argument("--notes")
    space_mutation_parser.set_defaults(function=command_add_space)

    floorplan_parser = subparsers.add_parser(
        "import-floorplan",
        help="Import checked rectangular GeoJSON as location profiles without locating any item",
    )
    floorplan_source = floorplan_parser.add_mutually_exclusive_group(required=True)
    floorplan_source.add_argument("--input", type=Path)
    floorplan_source.add_argument("--document-json", help=argparse.SUPPRESS)
    floorplan_parser.add_argument("--evidence-id")
    floorplan_parser.add_argument("--source-ref")
    floorplan_parser.add_argument("--captured-on", type=valid_date)
    floorplan_parser.add_argument(
        "--evidence-type", choices=("research", "vault_note"), default="research"
    )
    floorplan_parser.add_argument("--evidence-notes")
    floorplan_parser.add_argument(
        "--sensitivity",
        choices=("low", "personal", "high"),
        default="personal",
        help="Sensitivity for a source-created shared evidence row",
    )
    floorplan_parser.set_defaults(function=command_import_floorplan)

    capture_prepare_parser = subparsers.add_parser(
        "capture-prepare",
        help="Stage an overview image and immutable artifact for candidate review",
    )
    capture_prepare_parser.add_argument("--overview", type=Path, required=True)
    capture_prepare_parser.add_argument("--captured-on", type=valid_date, required=True)
    capture_prepare_parser.add_argument("--segments", type=json_array, required=True)
    capture_evidence = capture_prepare_parser.add_mutually_exclusive_group(required=True)
    capture_evidence.add_argument("--evidence-id")
    capture_evidence.add_argument("--source-ref")
    capture_prepare_parser.add_argument(
        "--evidence-type",
        choices=("user_source", "physical_check", "research", "vault_note"),
        default="user_source",
    )
    capture_prepare_parser.add_argument(
        "--sensitivity", choices=("low", "personal", "high"), default="personal"
    )
    capture_prepare_parser.add_argument("--adapter-name")
    capture_prepare_parser.add_argument("--adapter-timeout", type=float, default=10.0)
    capture_prepare_parser.set_defaults(function=command_capture_prepare)

    capture_review_parser = subparsers.add_parser(
        "capture-review",
        help="Seal explicit observation links into a reviewable capture proposal",
    )
    capture_review_parser.add_argument("capture_session_id")
    capture_review_parser.add_argument("--artifact-sha256", required=True)
    capture_review_parser.add_argument("--links", type=json_object, default={})
    capture_review_parser.add_argument("--manual-observations", type=json_object, default={})
    capture_review_parser.add_argument("--decisions", type=json_array, default=[])
    capture_review_parser.set_defaults(function=command_capture_review)

    capture_status_parser = subparsers.add_parser(
        "capture-status", help="Inspect a staged or applied capture session"
    )
    capture_status_parser.add_argument("capture_session_id")
    capture_status_parser.set_defaults(function=command_capture_status)

    capture_cleanup_parser = subparsers.add_parser(
        "capture-cleanup",
        help="Explicitly retire redundant staging for one applied bound capture",
    )
    capture_cleanup_parser.add_argument("capture_session_id")
    capture_cleanup_parser.set_defaults(function=command_capture_cleanup)

    capture_benchmark_parser = subparsers.add_parser(
        "capture-benchmark",
        help="Execute crops, a named adapter and duplicate ranking on a synthetic corpus",
    )
    capture_benchmark_parser.add_argument("--input", type=Path, required=True)
    capture_benchmark_parser.add_argument("--adapter-name", required=True)
    capture_benchmark_parser.add_argument("--adapter-timeout", type=float, default=10.0)
    capture_benchmark_parser.add_argument("--top-k", type=int, default=3)
    capture_benchmark_parser.set_defaults(function=command_capture_benchmark)

    sync_snapshot_parser = subparsers.add_parser(
        "sync-snapshot",
        help="Write a verified private canonical snapshot for an offline replica base or head",
    )
    sync_snapshot_parser.add_argument("--output", type=Path, required=True)
    sync_snapshot_parser.set_defaults(function=command_sync_snapshot)

    sync_bundle_parser = subparsers.add_parser(
        "sync-bundle",
        help="Build a self-verifying offline replica bundle from a base export/snapshot and replica head",
    )
    sync_bundle_parser.add_argument("--replica-ref", required=True)
    sync_bundle_parser.add_argument("--base", type=Path, required=True)
    sync_bundle_parser.add_argument("--head", type=Path, required=True)
    sync_bundle_parser.add_argument("--output", type=Path, required=True)
    sync_bundle_parser.set_defaults(function=command_sync_bundle)

    sync_prepare_parser = subparsers.add_parser(
        "sync-prepare",
        help="Inspect a private replica bundle against the current canonical head and prepare a plan",
    )
    sync_prepare_parser.add_argument("--bundle", type=Path, required=True)
    sync_prepare_parser.add_argument(
        "--trusted-base",
        type=Path,
        required=True,
        help="Independent retained canonical snapshot or verified export used to create the replica",
    )
    sync_prepare_parser.set_defaults(function=command_sync_prepare)

    sync_show_parser = subparsers.add_parser(
        "sync-show", help="Inspect one private prepared, resolved, or applied sync plan"
    )
    sync_show_parser.add_argument("plan_id")
    sync_show_parser.set_defaults(function=command_sync_show)

    sync_resolve_parser = subparsers.add_parser(
        "sync-resolve", help="Record every explicit choice required by one sync plan"
    )
    sync_resolve_parser.add_argument("plan_id")
    sync_resolve_parser.add_argument("--resolutions", type=Path, required=True)
    sync_resolve_parser.set_defaults(function=command_sync_resolve)

    sync_apply_parser = subparsers.add_parser(
        "sync-apply",
        help="Apply one ready private sync plan through the canonical transaction writer",
    )
    sync_apply_parser.add_argument("plan_id")
    sync_apply_parser.set_defaults(function=command_sync_apply)

    propose_parser = subparsers.add_parser(
        "propose",
        help="Prepare a digest-bound batch of mutation command argument arrays",
    )
    propose_parser.add_argument("--operations", type=Path, required=True)
    propose_parser.set_defaults(function=command_propose)

    import_propose_parser = subparsers.add_parser(
        "import-propose",
        help="Normalize a bounded CSV or JSON source into a review-only proposal",
    )
    import_propose_parser.add_argument("--input", type=Path, required=True)
    import_propose_parser.add_argument("--format", choices=("csv", "json"), required=True)
    import_propose_parser.add_argument("--source-name", required=True)
    import_propose_parser.add_argument("--source-namespace", required=True)
    import_propose_parser.add_argument("--source-date", type=valid_date, required=True)
    import_propose_parser.add_argument(
        "--sensitivity",
        choices=("low", "personal", "high"),
        default="personal",
        help="Sensitivity for source-row evidence; defaults to personal",
    )
    import_propose_parser.set_defaults(function=command_import_propose)

    proposal_show_parser = subparsers.add_parser(
        "proposal-show", help="Inspect one prepared or applied proposal"
    )
    proposal_show_parser.add_argument("proposal_id")
    proposal_show_parser.set_defaults(function=command_proposal_show)

    proposal_apply_parser = subparsers.add_parser(
        "proposal-apply", help="Apply every prepared operation in one transaction"
    )
    proposal_apply_parser.add_argument("proposal_id")
    proposal_apply_parser.set_defaults(function=command_proposal_apply)

    location_parser = subparsers.add_parser(
        "add-location",
        help="Add or reuse one node of the spatial tree, from a site down to a compartment",
    )
    location_parser.add_argument("--name", required=True)
    location_parser.add_argument("--location-id")
    location_parser.add_argument("--parent-location-id")
    location_parser.add_argument(
        "--kind",
        choices=LOCATION_KIND_CHOICES,
        required=True,
    )
    location_parser.add_argument(
        "--sensitivity", choices=("low", "personal", "high"), default="personal"
    )
    location_parser.add_argument("--notes")
    location_parser.set_defaults(function=command_add_location)

    locations_parser = subparsers.add_parser(
        "import-locations", help="Add a checked location hierarchy in one verified transaction"
    )
    locations_parser.add_argument("--input", type=Path, required=True)
    locations_parser.set_defaults(function=command_import_locations)

    correction_parser = subparsers.add_parser(
        "correct-item-identity",
        help="Reassign an item to a corrected immutable model while retaining identity history",
    )
    add_actor(correction_parser)
    correction_parser.add_argument("--item-id", required=True)
    correction_parser.add_argument("--evidence-id", required=True)
    correction_parser.add_argument("--amended-on", type=valid_date, required=True)
    correction_parser.add_argument("--recorded-at", type=valid_timestamp)
    correction_parser.add_argument(
        "--reason",
        choices=("identity_correction", "reclassification", "model_merge", "model_split"),
        required=True,
    )
    correction_parser.add_argument("--name", required=True)
    correction_parser.add_argument("--category", required=True)
    correction_parser.add_argument("--brand")
    correction_parser.add_argument("--model")
    correction_model = correction_parser.add_mutually_exclusive_group()
    correction_model.add_argument("--existing-model-id")
    correction_model.add_argument("--new-model", action="store_true")
    correction_parser.add_argument("--reference-url")
    correction_parser.add_argument("--interface", action="append", default=[])
    correction_parser.add_argument("--specs", type=json_object, default={})
    correction_parser.add_argument("--identifiers", type=json_object, default={})
    correction_parser.add_argument("--notes")
    correction_parser.set_defaults(function=command_correct_item_identity)

    evidence_parser = subparsers.add_parser(
        "record-evidence",
        help="Record non-lifecycle evidence for later fact enrichment or correction",
    )
    evidence_parser.add_argument("--item-id", action="append", default=[])
    evidence_parser.add_argument("--source-ref", required=True)
    evidence_parser.add_argument("--captured-on", type=valid_date, required=True)
    evidence_parser.add_argument(
        "--evidence-type",
        choices=(
            "user_source",
            "merchant_account",
            "finance_sheet",
            "vault_note",
            "research",
        ),
        required=True,
    )
    evidence_parser.add_argument(
        "--claim-strength",
        choices=("claimed_owned", "purchase_only", "research_only"),
        required=True,
    )
    evidence_parser.add_argument(
        "--sensitivity", choices=("low", "personal", "high"), required=True
    )
    evidence_parser.add_argument("--notes")
    evidence_parser.set_defaults(function=command_record_evidence)

    amend_fact_parser = subparsers.add_parser(
        "amend-fact",
        help="Evidence-back replacement or retraction of one current durable fact",
    )
    add_actor(amend_fact_parser)
    amend_fact_parser.add_argument(
        "--table", choices=tuple(sorted(FACT_SELECTOR_FIELDS)), required=True
    )
    amend_fact_parser.add_argument("--selector", type=json_object, required=True)
    amend_fact_parser.add_argument(
        "--action", choices=("replace", "retract"), required=True
    )
    amend_fact_parser.add_argument("--replacement", type=json_object)
    amend_fact_parser.add_argument("--evidence-id", required=True)
    amend_fact_parser.add_argument("--amended-on", type=valid_date, required=True)
    amend_fact_parser.add_argument("--recorded-at", type=valid_timestamp)
    amend_fact_parser.add_argument("--reason", required=True)
    amend_fact_parser.add_argument("--notes")
    amend_fact_parser.set_defaults(function=command_amend_fact)

    enrich_item_parser = subparsers.add_parser(
        "enrich-item",
        help="Add or correct receipt, acquisition, condition, or serial facts without asserting possession",
    )
    add_actor(enrich_item_parser)
    enrich_item_parser.add_argument("--item-id", required=True)
    enrich_item_parser.add_argument("--evidence-id", required=True)
    enrich_item_parser.add_argument("--amended-on", type=valid_date, required=True)
    enrich_item_parser.add_argument("--recorded-at", type=valid_timestamp)
    enrich_item_parser.add_argument("--acquired-on", type=valid_date)
    enrich_item_parser.add_argument("--condition")
    enrich_item_parser.add_argument("--purchase-price", type=non_negative_number)
    enrich_item_parser.add_argument("--purchase-currency", type=currency_code)
    enrich_item_parser.add_argument("--receipt-ref")
    enrich_item_parser.add_argument("--serial-or-lot")
    enrich_item_parser.add_argument(
        "--clear-field", action="append", choices=ENRICH_ITEM_DETAIL_FIELDS, default=[]
    )
    enrich_item_parser.add_argument("--notes")
    enrich_item_parser.set_defaults(function=command_enrich_item)

    order_parser = subparsers.add_parser(
        "order", help="Record an actual order as candidate ownership"
    )
    add_actor(order_parser)
    add_source(order_parser)
    order_parser.add_argument("--name", required=True)
    order_parser.add_argument("--category", required=True)
    order_parser.add_argument("--ordered-on", type=valid_date, required=True)
    order_parser.add_argument("--order-placed", action="store_true", required=True)
    order_parser.add_argument("--brand")
    order_parser.add_argument("--model")
    order_model = order_parser.add_mutually_exclusive_group()
    order_model.add_argument("--existing-model-id")
    order_model.add_argument("--new-model", action="store_true")
    order_parser.add_argument("--existing-item-id")
    order_parser.add_argument("--import-unit-identity")
    order_parser.add_argument("--reference-url")
    order_parser.add_argument("--interface", action="append", default=[])
    order_parser.add_argument("--specs", type=json_object, default={})
    order_parser.add_argument("--identifiers", type=json_object, default={})
    order_parser.add_argument("--quantity", type=positive_number)
    order_parser.add_argument("--unit", default="item")
    order_parser.add_argument("--location-id")
    order_parser.add_argument("--purchase-price", type=non_negative_number)
    order_parser.add_argument("--purchase-currency", type=currency_code)
    order_parser.add_argument("--receipt-ref")
    order_parser.add_argument(
        "--evidence-type", choices=("merchant_account", "user_source"), default="merchant_account"
    )
    order_parser.add_argument(
        "--sensitivity", choices=("low", "personal", "high"), default="personal"
    )
    order_parser.set_defaults(function=command_order)

    plan_parser = subparsers.add_parser(
        "plan", help="Record a cart or considered purchase without claiming an order"
    )
    add_actor(plan_parser)
    add_source(plan_parser)
    plan_parser.add_argument("--name", required=True)
    plan_parser.add_argument("--category", required=True)
    plan_parser.add_argument("--planned-on", type=valid_date, required=True)
    plan_parser.add_argument("--brand")
    plan_parser.add_argument("--model")
    plan_model = plan_parser.add_mutually_exclusive_group()
    plan_model.add_argument("--existing-model-id")
    plan_model.add_argument("--new-model", action="store_true")
    plan_parser.add_argument("--existing-item-id")
    plan_parser.add_argument("--import-unit-identity")
    plan_parser.add_argument("--reference-url")
    plan_parser.add_argument("--interface", action="append", default=[])
    plan_parser.add_argument("--specs", type=json_object, default={})
    plan_parser.add_argument("--identifiers", type=json_object, default={})
    plan_parser.add_argument("--quantity", type=positive_number)
    plan_parser.add_argument("--unit", default="item")
    plan_parser.add_argument("--location-id")
    plan_parser.add_argument(
        "--sensitivity", choices=("low", "personal", "high"), default="personal"
    )
    plan_parser.set_defaults(function=command_plan)

    receive_parser = subparsers.add_parser(
        "receive", help="Promote an ordered item only after delivery or current confirmation"
    )
    add_actor(receive_parser)
    add_source(receive_parser)
    receive_parser.add_argument("--item-id", required=True)
    receive_parser.add_argument("--received-on", type=valid_date, required=True)
    receive_parser.add_argument("--location-id")
    receive_parser.add_argument("--container-id")
    receive_parser.add_argument("--quantity", type=positive_number)
    receive_parser.add_argument("--condition")
    receive_parser.add_argument("--serial-or-lot")
    receive_parser.add_argument("--physical-check", action="store_true")
    receive_parser.add_argument(
        "--location-unchanged",
        action="store_true",
        help="Explicitly preserve the item's already-known stable location",
    )
    receive_parser.set_defaults(function=command_receive)

    discover_parser = subparsers.add_parser(
        "discover",
        help="Record one physically shown, already-owned unit with explicit current evidence",
    )
    add_actor(discover_parser)
    add_source(discover_parser)
    discover_parser.add_argument("--name", required=True)
    discover_parser.add_argument("--category", required=True)
    discover_parser.add_argument("--checked-on", type=valid_date, required=True)
    discover_parser.add_argument("--location-id", required=True)
    discover_parser.add_argument("--container-id")
    discover_parser.add_argument("--brand")
    discover_parser.add_argument("--model")
    discover_model = discover_parser.add_mutually_exclusive_group()
    discover_model.add_argument("--existing-model-id")
    discover_model.add_argument("--new-model", action="store_true")
    discovery_unit = discover_parser.add_mutually_exclusive_group()
    discovery_unit.add_argument("--existing-item-id")
    discovery_unit.add_argument(
        "--new-unit",
        action="store_true",
        help="Confirm that a same-model sighting is a distinct physical unit",
    )
    discover_parser.add_argument("--reference-url")
    discover_parser.add_argument("--interface", action="append", default=[])
    discover_parser.add_argument("--specs", type=json_object, default={})
    discover_parser.add_argument("--identifiers", type=json_object, default={})
    discover_parser.add_argument("--quantity", type=positive_number)
    discover_parser.add_argument("--unit", default="item")
    discover_parser.add_argument("--condition")
    discover_parser.add_argument("--serial-or-lot")
    discover_parser.add_argument(
        "--ownership-state",
        choices=("confirmed", "unknown", "not_owned"),
        default="confirmed",
        help=(
            "Ownership evidenced by the check. Use unknown or not_owned for a newly "
            "distinguished borrowed or otherwise non-owned unit."
        ),
    )
    discover_parser.add_argument(
        "--sensitivity", choices=("low", "personal", "high"), default="personal"
    )
    discover_parser.set_defaults(function=command_discover)

    sell_parser = subparsers.add_parser(
        "sell", help="Record sale without erasing the item or its history"
    )
    add_actor(sell_parser)
    add_source(sell_parser)
    sell_parser.add_argument("--item-id", required=True)
    sale_date = sell_parser.add_mutually_exclusive_group(required=True)
    sale_date.add_argument("--sold-on", type=valid_date)
    sale_date.add_argument(
        "--sold-date-unknown",
        action="store_true",
        help="Preserve an unknown historical sale date; requires --observed-on",
    )
    sell_parser.add_argument(
        "--observed-on",
        type=valid_date,
        help="Date the lifecycle fact was observed or reported; defaults to the occurrence date",
    )
    sell_parser.set_defaults(function=command_sell)

    relate_parser = subparsers.add_parser(
        "relate", help="Record an evidence-bearing compatibility or containment relationship"
    )
    add_source(relate_parser)
    relate_parser.add_argument("--subject-item-id", required=True)
    relate_parser.add_argument("--object-item-id", required=True)
    relate_parser.add_argument(
        "--predicate",
        choices=(
            "works_with",
            "requires",
            "contained_in",
            "replaces",
            "overlaps_function",
            "not_compatible",
            "compatible_if",
            "configured_on",
            "unknown",
        ),
        required=True,
    )
    relate_parser.add_argument(
        "--confidence", choices=("verified", "high", "medium", "low", "unknown"), required=True
    )
    relate_parser.add_argument("--captured-on", type=valid_date, required=True)
    relate_parser.add_argument(
        "--evidence-type",
        choices=("user_source", "vault_note", "physical_check", "research"),
        required=True,
    )
    relate_parser.add_argument(
        "--claim-strength",
        choices=("explicit_current", "claimed_owned", "research_only"),
        required=True,
    )
    relate_parser.set_defaults(function=command_relate)

    not_found_parser = subparsers.add_parser(
        "not-found",
        help="Flag an expected item absent from one checked area without declaring it lost or disposed",
    )
    add_actor(not_found_parser)
    add_source(not_found_parser)
    not_found_parser.add_argument("--item-id", required=True)
    not_found_parser.add_argument("--area-location-id", required=True)
    not_found_parser.add_argument("--checked-on", type=valid_date, required=True)
    not_found_parser.set_defaults(function=command_not_found)

    physical_parser = subparsers.add_parser(
        "physical-check", help="Attach physical evidence and current location"
    )
    add_actor(physical_parser)
    add_source(physical_parser)
    physical_parser.add_argument("--item-id", required=True)
    physical_parser.add_argument("--checked-on", type=valid_date, required=True)
    physical_location = physical_parser.add_mutually_exclusive_group(required=True)
    physical_location.add_argument("--location-id")
    physical_location.add_argument(
        "--location-unchanged",
        action="store_true",
        help="Explicitly confirm the item's already-known stable location",
    )
    physical_parser.add_argument("--container-id")
    physical_parser.add_argument("--quantity", type=positive_number)
    physical_parser.add_argument("--condition")
    physical_parser.add_argument("--serial-or-lot")
    physical_parser.set_defaults(function=command_physical_check)

    restore_current_parser = subparsers.add_parser(
        "restore-current-ownership",
        help="Correct or reacquire one terminal item with current physical evidence",
    )
    add_actor(restore_current_parser)
    add_source(restore_current_parser)
    restore_current_parser.add_argument("--item-id", required=True)
    restore_current_parser.add_argument("--checked-on", type=valid_date, required=True)
    restore_current_parser.add_argument("--location-id", required=True)
    restore_current_parser.add_argument("--container-id")
    restore_current_parser.add_argument("--quantity", type=positive_number)
    restore_current_parser.add_argument("--condition")
    restore_current_parser.add_argument("--serial-or-lot")
    restore_current_parser.add_argument(
        "--reason", choices=("reacquired", "ownership_corrected"), required=True
    )
    restore_current_parser.set_defaults(function=command_restore_current_ownership)

    return_loan_parser = subparsers.add_parser(
        "return-loan",
        help="Return a lent owned item to one stable current location",
    )
    add_actor(return_loan_parser)
    add_source(return_loan_parser)
    return_loan_parser.add_argument("--item-id", required=True)
    return_loan_parser.add_argument("--returned-on", type=valid_date, required=True)
    return_loan_parser.add_argument("--location-id", required=True)
    return_loan_parser.add_argument("--container-id")
    return_loan_parser.set_defaults(function=command_return_loan)

    add_party_parser = subparsers.add_parser("add-party", help="Add an evidence-backed named counterparty")
    add_source(add_party_parser)
    add_party_parser.add_argument("--name", required=True)
    add_party_parser.add_argument("--party-kind", choices=("person", "household", "organisation", "unknown"), required=True)
    add_party_parser.add_argument("--captured-on", type=valid_date, required=True)
    add_party_parser.add_argument("--sensitivity", choices=("low", "personal", "high"), default="personal")
    add_party_parser.set_defaults(function=command_add_party)

    set_home_parser = subparsers.add_parser("set-home", help="Amend an item's independent home placement")
    add_actor(set_home_parser)
    add_source(set_home_parser)
    set_home_parser.add_argument("--item-id", required=True)
    set_home_parser.add_argument("--set-on", type=valid_date, required=True)
    home_destination = set_home_parser.add_mutually_exclusive_group(required=True)
    home_destination.add_argument("--location-id")
    home_destination.add_argument(
        "--clear",
        action="store_true",
        help="Record that the item's usual home is currently unknown",
    )
    set_home_parser.add_argument("--container-id")
    set_home_parser.set_defaults(function=command_set_home)

    custody_start_parser = subparsers.add_parser("custody-start", help="Start a supported custody episode without changing ownership")
    add_actor(custody_start_parser)
    add_source(custody_start_parser)
    custody_start_parser.add_argument("--item-id", required=True)
    custody_start_parser.add_argument("--party-id")
    custody_start_parser.add_argument("--custody-kind", choices=("loan", "storage", "service", "transit", "possession", "unknown"), required=True)
    custody_start_parser.add_argument("--started-on", type=valid_date, required=True)
    custody_start_parser.add_argument("--due-on", type=valid_date)
    custody_start_parser.add_argument("--location-id")
    custody_start_parser.add_argument("--container-id")
    custody_start_parser.add_argument("--quantity", type=positive_number)
    custody_start_parser.add_argument("--unit")
    custody_start_parser.set_defaults(function=command_custody_start)

    custody_end_parser = subparsers.add_parser("custody-end", help="End a supported custody episode")
    add_actor(custody_end_parser)
    add_source(custody_end_parser)
    custody_end_parser.add_argument("--relation-id", required=True)
    custody_end_parser.add_argument("--ended-on", type=valid_date, required=True)
    custody_end_parser.add_argument("--location-id")
    custody_end_parser.add_argument("--container-id")
    custody_end_parser.set_defaults(function=command_custody_end)

    access_grant_parser = subparsers.add_parser("access-grant", help="Record independent item access")
    add_actor(access_grant_parser)
    add_source(access_grant_parser)
    access_grant_parser.add_argument("--item-id", required=True)
    access_grant_parser.add_argument("--party-id", required=True)
    access_grant_parser.add_argument("--granted-on", type=valid_date, required=True)
    access_grant_parser.set_defaults(function=command_access_grant)

    access_revoke_parser = subparsers.add_parser("access-revoke", help="End independent item access")
    add_actor(access_revoke_parser)
    add_source(access_revoke_parser)
    access_revoke_parser.add_argument("--relation-id", required=True)
    access_revoke_parser.add_argument("--revoked-on", type=valid_date, required=True)
    access_revoke_parser.set_defaults(function=command_access_revoke)

    ownership_start_parser = subparsers.add_parser(
        "ownership-start", help="Record an evidence-backed owner without changing custody"
    )
    add_actor(ownership_start_parser)
    add_source(ownership_start_parser)
    ownership_start_parser.add_argument("--item-id", required=True)
    ownership_start_parser.add_argument("--party-id", required=True)
    ownership_start_parser.add_argument("--started-on", type=valid_date, required=True)
    ownership_start_parser.set_defaults(function=command_ownership_start)

    ownership_end_parser = subparsers.add_parser(
        "ownership-end", help="End one explicit ownership episode"
    )
    add_actor(ownership_end_parser)
    add_source(ownership_end_parser)
    ownership_end_parser.add_argument("--relation-id", required=True)
    ownership_end_parser.add_argument("--ended-on", type=valid_date, required=True)
    ownership_end_parser.set_defaults(function=command_ownership_end)

    embody_parser = subparsers.add_parser("embody-location", help="Bind an owned current item to the location node it embodies")
    add_source(embody_parser)
    embody_parser.add_argument("--item-id", required=True)
    embody_parser.add_argument("--location-id", required=True)
    embody_parser.add_argument("--recorded-on", type=valid_date, required=True)
    embody_parser.set_defaults(function=command_embody_location)

    move_parser = subparsers.add_parser(
        "move", help="Change the current stable location with explicit evidence"
    )
    add_actor(move_parser)
    add_source(move_parser)
    move_parser.add_argument("--item-id", required=True)
    move_parser.add_argument("--moved-on", type=valid_date, required=True)
    move_parser.add_argument("--location-id", required=True)
    move_parser.add_argument("--container-id")
    move_parser.set_defaults(function=command_move)

    change_parser = subparsers.add_parser(
        "change", help="Record another supported lifecycle or quantity change"
    )
    add_actor(change_parser)
    add_source(change_parser)
    change_parser.add_argument("--item-id", required=True)
    change_parser.add_argument(
        "--event-type",
        choices=(
            "returned",
            "cancelled",
            "refunded",
            "gifted",
            "disposed",
            "lost",
            "lent",
            "ownership_unresolved",
            "ownership_excluded",
            "quantity_changed",
        ),
        required=True,
    )
    change_date = change_parser.add_mutually_exclusive_group(required=True)
    change_date.add_argument("--occurred-on", type=valid_date)
    change_date.add_argument(
        "--date-unknown",
        action="store_true",
        help="Preserve an unknown historical event date; requires --observed-on",
    )
    change_parser.add_argument(
        "--observed-on",
        type=valid_date,
        help="Date the lifecycle fact was observed or reported; defaults to the occurrence date",
    )
    change_parser.add_argument("--quantity", type=positive_number)
    change_parser.add_argument("--unit")
    change_parser.add_argument(
        "--location-id",
        help="Known destination for a lent item; omitted means location unknown",
    )
    change_parser.add_argument("--container-id")
    change_parser.set_defaults(function=command_change)
    return parser


def execute(
    arguments: list[str] | None = None,
    *,
    capture_adapter_registry: AdapterRegistry | None = None,
) -> dict:
    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.scope not in SCOPE_MAX_SENSITIVITY:
        parser.error("PROPERTY_INVENTORY_SCOPE must be public, personal, or private")
    try:
        instance = resolve_instance_config(
            config_path=args.config,
            instance=args.instance,
            inventory_root=args.inventory_root,
            runtime_dir=args.runtime_dir,
            media_root=args.media_root,
            catalogue_output=args.catalogue_output,
            catalogue_scope=args.catalogue_scope,
            forbidden_roots=args.forbidden_roots,
        )
    except ConfigError as error:
        raise InventoryError(str(error)) from error
    args.inventory_root = instance.inventory_root
    args.runtime_dir = instance.runtime_dir
    args.media_root = instance.media_root
    args.catalogue_output = instance.catalogue_output
    args.catalogue_scope = instance.catalogue_scope
    args.forbidden_roots = instance.forbidden_roots
    args.capture_adapter_registry = capture_adapter_registry
    if args.function is command_attach_media and args.media_root is None:
        raise InventoryError("attach-media requires --media-root or PROPERTY_INVENTORY_MEDIA_ROOT")
    retired = args.inventory_root / RETIRED_INSTANCE_MARKER
    if path_entry_exists(retired):
        if retired.is_symlink() or not retired.is_file():
            raise InventoryError("inventory retirement marker is not a regular file")
        raise InventoryError(
            f"inventory instance is retired; use the replacement named in {retired}"
        )
    if args.function is command_migrate and args.inventory_root.exists():
        try:
            with inventory_lock(args.inventory_root):
                recover_pending_adoption_rollback(args)
        except Timeout as error:
            raise InventoryError("another inventory writer holds the transaction lock") from error
    legacy_adoption: tuple[str, bool] | None = None
    migration_rollback: tuple[dict, str] | None = None
    if args.inventory_root.exists() and args.function not in {
        command_init,
        command_restore,
        command_runtime_rebind,
    }:
        try:
            with inventory_lock(args.inventory_root):
                binding_marker = args.inventory_root / RUNTIME_BINDING
                if not path_entry_exists(binding_marker):
                    if args.function is not command_migrate:
                        raise InventoryError(
                            "inventory runtime binding is missing; run init explicitly "
                            "to claim new runtime and catalogue projections"
                        )
                    inventory_id = inventory_id_if_available(args.inventory_root)
                    canonical_before = canonical_store_digest(
                        args.inventory_root / "Data" / "store"
                    )
                    runtime_existed = args.runtime_dir.exists()
                    prepare_explicit_legacy_adoption(args)
                    migration_state = bindingless_adoption_rollback_state(
                        args,
                        inventory_id=inventory_id,
                        runtime_existed=runtime_existed,
                    )
                    migration_rollback = (migration_state, canonical_before)
                binding_record = read_runtime_binding_record(args.inventory_root)
                if binding_record["format"] == 1 and args.function is not command_migrate:
                    raise InventoryError(
                        "legacy inventory ownership is ambiguous; run init explicitly "
                        "to adopt this root and its projections"
                    )
                installation_id, is_legacy = establish_existing_instance_ownership(
                    args,
                    allow_unset_inventory_id=args.function is command_migrate,
                )
                if migration_rollback is not None:
                    record_bindingless_adoption_owner(args, migration_rollback[0])
                legacy_adoption = (installation_id, is_legacy)
                restore_journal = args.runtime_dir / RESTORE_JOURNAL
                if restore_journal.is_symlink():
                    raise InventoryError("restore journal must not be a symlink")
                if path_entry_exists(restore_journal):
                    raise InventoryError(
                        "a pending restore must be recovered with the original restore command "
                        "before any inventory read or mutation"
                    )
        except Timeout as error:
            raise InventoryError("another inventory writer holds the transaction lock") from error
        except BaseException:
            if migration_rollback is not None:
                rollback_bindingless_migration_if_unchanged(
                    args, migration_rollback[0], migration_rollback[1]
                )
            raise
    instance_token = _INSTANCE_PATHS.set(
        (args.media_root, args.catalogue_output, args.catalogue_scope)
    )
    lock_instance_token = _LOCK_INSTANCE_PATHS.set(
        None
        if args.function in {command_init, command_restore, command_runtime_rebind}
        else (
            args.inventory_root,
            args.runtime_dir,
            args.media_root,
            args.catalogue_output,
            args.function is command_migrate
            or legacy_adoption is not None
            and legacy_adoption[1],
        )
    )
    try:
        result = args.function(args)
        if legacy_adoption is not None and legacy_adoption[1]:
            installation_id = legacy_adoption[0]
            try:
                with inventory_lock(args.inventory_root):
                    live_paths = data_paths(args.inventory_root, args.runtime_dir)
                    verify_bundle(
                        live_paths,
                        live_paths["store"],
                        live_paths["database"],
                        live_paths["catalogue"],
                        on_projection=(
                            lambda label, path: (
                                record_bindingless_adoption_projection(
                                migration_rollback[0], label, path
                            )
                            if migration_rollback is not None
                            else None
                            )
                        ),
                    )
                    finish_legacy_instance_adoption(args, installation_id)
            except Timeout as error:
                raise InventoryError(
                    "another inventory writer holds the legacy-adoption lock"
                ) from error
        return result
    except BaseException:
        if migration_rollback is not None:
            rollback_bindingless_migration_if_unchanged(
                args, migration_rollback[0], migration_rollback[1]
            )
        raise
    finally:
        _LOCK_INSTANCE_PATHS.reset(lock_instance_token)
        _INSTANCE_PATHS.reset(instance_token)


def main() -> int:
    requested_scope = os.environ.get("PROPERTY_INVENTORY_SCOPE", "private")
    for index, argument in enumerate(sys.argv[1:]):
        if argument == "--scope" and index + 2 <= len(sys.argv[1:]):
            requested_scope = sys.argv[1:][index + 1]
        elif argument.startswith("--scope="):
            requested_scope = argument.partition("=")[2]
    try:
        result = execute()
    except (
        InventoryError,
        RetrievalError,
        OSError,
        UnicodeError,
        sqlite3.Error,
        json.JSONDecodeError,
        tarfile.TarError,
    ) as error:
        message = (
            str(error)
            if requested_scope == "private"
            else "inventory command could not complete safely in this scope"
        )
        print(json.dumps({"status": "error", "error": message}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
