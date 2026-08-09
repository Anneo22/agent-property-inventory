"""Executable package/schema compatibility and migration policy."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .rebuild import SCHEMA_VERSION


class CompatibilityError(ValueError):
    """Raised when a runtime or schema is outside the supported matrix."""


MINIMUM_PYTHON = (3, 11)
MIGRATION_ACTIONS = {
    1: "migrate_v1_to_v6",
    2: "migrate_v2_to_v6",
    3: "migrate_v3_to_v6",
    4: "migrate_v4_to_v6",
    5: "migrate_v5_to_v6",
    6: "read_current",
}
SUPPORTED_SCHEMA_VERSIONS = tuple(MIGRATION_ACTIONS)


def _validate_current_schema_policy() -> None:
    """Require an explicit policy update whenever the canonical schema advances."""
    if SCHEMA_VERSION not in MIGRATION_ACTIONS or SCHEMA_VERSION != max(MIGRATION_ACTIONS):
        raise CompatibilityError(
            "compatibility policy must be explicitly updated for the current schema"
        )


def _python_version(value: object) -> tuple[int, int]:
    if not isinstance(value, tuple) or len(value) < 2:
        raise CompatibilityError("python version must be a tuple containing major and minor integers")
    major, minor = value[:2]
    if isinstance(major, bool) or isinstance(minor, bool) or not isinstance(major, int) or not isinstance(minor, int):
        raise CompatibilityError("python version must contain integer major and minor values")
    return major, minor


@dataclass(frozen=True)
class CompatibilityEntry:
    schema_version: int
    action: str
    supported: bool


@dataclass(frozen=True)
class CompatibilityMatrix:
    current_schema_version: int
    minimum_python: tuple[int, int]
    runtime_python: tuple[int, int]
    entries: tuple[CompatibilityEntry, ...]

    def entry_for(self, schema_version: int) -> CompatibilityEntry:
        for entry in self.entries:
            if entry.schema_version == schema_version:
                return entry
        raise CompatibilityError(f"schema {schema_version} is not represented in the compatibility matrix")


def compatibility_matrix(
    python_version: tuple[int, ...] = sys.version_info[:2],
) -> CompatibilityMatrix:
    """Build the historical/current-runtime matrix from source metadata."""
    _validate_current_schema_policy()
    runtime = _python_version(python_version)
    runtime_supported = runtime >= MINIMUM_PYTHON
    entries = tuple(
        CompatibilityEntry(
            schema_version=version,
            action=MIGRATION_ACTIONS[version],
            supported=runtime_supported,
        )
        for version in SUPPORTED_SCHEMA_VERSIONS
    )
    return CompatibilityMatrix(
        current_schema_version=SCHEMA_VERSION,
        minimum_python=MINIMUM_PYTHON,
        runtime_python=runtime,
        entries=entries,
    )


def validate_runtime(python_version: tuple[int, ...] = sys.version_info[:2]) -> tuple[int, int]:
    """Accept the package's declared Python floor and reject older runtimes."""
    runtime = _python_version(python_version)
    if runtime < MINIMUM_PYTHON:
        raise CompatibilityError(
            f"Python {runtime[0]}.{runtime[1]} is unsupported; requires >= {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}"
        )
    return runtime


def validate_schema(schema_version: object) -> int:
    """Accept only explicitly supported historical/current canonical schemas."""
    _validate_current_schema_policy()
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise CompatibilityError("schema version must be an integer")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise CompatibilityError(
            f"schema {schema_version} is unsupported; supported schemas are {SUPPORTED_SCHEMA_VERSIONS}"
        )
    return schema_version


def validate_migration(
    source_schema_version: object,
    target_schema_version: object = SCHEMA_VERSION,
    *,
    python_version: tuple[int, ...] = sys.version_info[:2],
) -> CompatibilityEntry:
    """Validate a supported path to the current schema, never an invented downgrade."""
    _validate_current_schema_policy()
    validate_runtime(python_version)
    source = validate_schema(source_schema_version)
    target = validate_schema(target_schema_version)
    if target != SCHEMA_VERSION:
        raise CompatibilityError(f"migration target must be current schema {SCHEMA_VERSION}")
    return compatibility_matrix(python_version).entry_for(source)
