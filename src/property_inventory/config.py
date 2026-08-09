"""Resolve and validate one Property Inventory instance configuration."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

CONFIG_VERSION = 1
DEFAULT_CATALOGUE_SCOPE = "personal"
VALID_CATALOGUE_SCOPES = frozenset({"public", "personal", "private"})


class ConfigError(ValueError):
    """Raised when an inventory instance configuration is unsafe or incomplete."""


@dataclass(frozen=True)
class InstanceConfig:
    """Resolved absolute paths and visibility scope for one inventory instance."""

    inventory_root: Path
    runtime_dir: Path
    media_root: Path | None
    catalogue_output: Path
    catalogue_scope: str
    forbidden_roots: tuple[Path, ...]


def default_config_path(*, home: Path | None = None) -> Path:
    """Return the conventional per-user configuration location."""
    base = home if home is not None else Path.home()
    return base / "Library" / "Application Support" / "property-inventory" / "config.json"


def resolve_instance_config(
    *,
    config_path: str | Path | None = None,
    inventory_root: str | Path | None = None,
    runtime_dir: str | Path | None = None,
    media_root: str | Path | None = None,
    catalogue_output: str | Path | None = None,
    catalogue_scope: str | None = None,
    forbidden_roots: Sequence[str | Path] | None = None,
    instance: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> InstanceConfig:
    """Load config, applying explicit values before environment and file values.

    A missing default config file is valid so callers can supply a fully explicit
    configuration. An explicitly selected config file must exist and be valid.
    """
    environment = os.environ if environ is None else environ
    chosen_config_path = _choose(config_path, environment.get("PROPERTY_INVENTORY_CONFIG"))
    explicit_config = chosen_config_path is not None
    path = (
        _resolved_path(chosen_config_path, "config_path")
        if explicit_config
        else default_config_path()
    )
    selected_instance = _choose(instance, environment.get("PROPERTY_INVENTORY_INSTANCE"))
    explicit_inventory_root = _choose(
        inventory_root, environment.get("PROPERTY_INVENTORY_ROOT")
    )
    explicit_runtime_dir = _choose(
        runtime_dir, environment.get("PROPERTY_INVENTORY_RUNTIME")
    )
    legacy_standalone_topology = (
        not explicit_config
        and selected_instance is None
        and explicit_inventory_root is not None
        and explicit_runtime_dir is not None
    )
    file_values = _read_config(path, required=explicit_config)
    values = _select_instance(file_values, selected_instance)
    path_values = {} if legacy_standalone_topology else values

    configured_inventory_root = _lexical_path(
        _choose(explicit_inventory_root, path_values.get("inventory_root")),
        "inventory_root",
    )
    resolved_inventory_root = _resolve_path(configured_inventory_root, "inventory_root")
    configured_runtime_dir = _lexical_path(
        _choose(
            explicit_runtime_dir,
            path_values.get("runtime_dir"),
            Path.cwd() / ".local",
        ),
        "runtime_dir",
    )
    resolved_runtime_dir = _resolve_path(configured_runtime_dir, "runtime_dir")
    configured_media_root = _optional_lexical_path(
        _choose(
            media_root,
            environment.get("PROPERTY_INVENTORY_MEDIA_ROOT"),
            path_values.get("media_root"),
        ),
        "media_root",
    )
    resolved_media_root = (
        None
        if configured_media_root is None
        else _resolve_path(configured_media_root, "media_root")
    )
    configured_catalogue_output = _lexical_path(
        _choose(
            catalogue_output,
            environment.get("PROPERTY_INVENTORY_CATALOGUE_OUTPUT"),
            path_values.get("catalogue_output"),
            resolved_inventory_root / "Inventory.md",
        ),
        "catalogue_output",
    )
    resolved_catalogue_output = _resolve_path(
        configured_catalogue_output, "catalogue_output"
    )
    if resolved_catalogue_output == path:
        raise ConfigError("catalogue_output must not overwrite the instance config file")
    resolved_scope = _choose(
        catalogue_scope,
        environment.get("PROPERTY_INVENTORY_CATALOGUE_SCOPE"),
        path_values.get("catalogue_scope"),
        DEFAULT_CATALOGUE_SCOPE,
    )
    if not isinstance(resolved_scope, str) or resolved_scope not in VALID_CATALOGUE_SCOPES:
        choices = ", ".join(sorted(VALID_CATALOGUE_SCOPES))
        raise ConfigError(f"catalogue_scope must be one of {choices}: {resolved_scope!r}")

    raw_forbidden_roots: list[str | Path] = []
    for source in (
        values.get("forbidden_roots", ()),
        _environment_roots(environment.get("PROPERTY_INVENTORY_FORBIDDEN_ROOTS")) or (),
        forbidden_roots or (),
    ):
        raw_forbidden_roots.extend(_raw_paths(source, "forbidden_roots"))
    lexical_forbidden_roots = tuple(
        dict.fromkeys(_lexical_paths(raw_forbidden_roots, "forbidden_roots"))
    )
    resolved_forbidden_roots = tuple(
        dict.fromkeys(
            _resolve_path(forbidden_root, "forbidden_roots")
            for forbidden_root in lexical_forbidden_roots
        )
    )
    result = InstanceConfig(
        inventory_root=resolved_inventory_root,
        runtime_dir=resolved_runtime_dir,
        media_root=resolved_media_root,
        catalogue_output=resolved_catalogue_output,
        catalogue_scope=resolved_scope,
        forbidden_roots=resolved_forbidden_roots,
    )
    _validate_roots(result)
    _validate_data_roots_against_forbidden(
        (
            ("inventory_root", configured_inventory_root),
            ("runtime_dir", configured_runtime_dir),
            ("media_root", configured_media_root),
        ),
        lexical_forbidden_roots,
    )
    _validate_catalogue_output_path(configured_catalogue_output)
    return result


load_instance_config = resolve_instance_config


def _choose(*values: object) -> object | None:
    return next((value for value in values if value is not None), None)


def _read_config(path: Path, *, required: bool) -> dict[str, object]:
    if not path.exists():
        if required:
            raise ConfigError(f"config file does not exist: {path}")
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read config file {path}: {error}") from error
    if not isinstance(loaded, dict):
        raise ConfigError("config file must contain a JSON object")
    version = loaded.get("version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        raise ConfigError(f"unsupported config version: {version!r}")
    return loaded


def _select_instance(values: dict[str, object], instance: object | None) -> dict[str, object]:
    instances = values.get("instances")
    if instances is None:
        if instance is not None:
            raise ConfigError("config file does not define instances")
        return values
    if not isinstance(instances, dict):
        raise ConfigError("config instances must be a JSON object")
    selected = instance if instance is not None else values.get("default_instance")
    if not isinstance(selected, str) or not selected:
        raise ConfigError("config instances require a default_instance")
    chosen = instances.get(selected)
    if not isinstance(chosen, dict):
        raise ConfigError(f"config instance is missing or invalid: {selected!r}")
    return chosen


def _resolved_path(value: object, name: str) -> Path:
    return _resolve_path(_lexical_path(value, name), name)


def _resolve_path(path: Path, name: str) -> Path:
    try:
        resolved = path.resolve()
        # Python 3.13 stopped raising for symlink loops in non-strict
        # resolution and instead leaves the unresolved component in the
        # result.  A managed root must never retain such a component: later
        # filesystem operations could resolve it differently or recurse
        # forever.  Inspect the returned path without following links so the
        # rule is identical on every supported Python version.
        current = resolved
        while True:
            try:
                mode = os.lstat(current).st_mode
            except FileNotFoundError:
                mode = None
            if mode is not None and stat.S_ISLNK(mode):
                raise OSError(f"unresolved symlink component: {current}")
            if current == current.parent:
                break
            current = current.parent
        return resolved
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigError(f"cannot resolve {name}: {error}") from error


def _lexical_path(value: object, name: str) -> Path:
    """Return an absolute path without following its symlink components."""
    if isinstance(value, Path):
        candidate = value
    elif isinstance(value, str) and value:
        candidate = Path(value)
    else:
        raise ConfigError(f"{name} is required and must be a non-empty path")
    return candidate.expanduser().absolute()


def _optional_lexical_path(value: object, name: str) -> Path | None:
    return None if value is None else _lexical_path(value, name)


def _lexical_paths(value: object, name: str) -> tuple[Path, ...]:
    if isinstance(value, str):
        raise ConfigError(f"{name} must be an array of paths")
    if not isinstance(value, Sequence):
        raise ConfigError(f"{name} must be an array of paths")
    return tuple(_lexical_path(path, name) for path in value)


def _raw_paths(value: object, name: str) -> tuple[str | Path, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ConfigError(f"{name} must be an array of paths")
    if not all(isinstance(path, (str, Path)) for path in value):
        raise ConfigError(f"{name} must be an array of paths")
    return tuple(value)


def _environment_roots(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not value:
        return ()
    return tuple(part for part in value.split(os.pathsep) if part)


def _validate_catalogue_output_path(path: Path) -> None:
    """Reject symlinks at the catalogue file boundary without walking ancestors."""
    if path.is_symlink():
        raise ConfigError("catalogue_output must not be a symlink")
    if path.parent.is_symlink():
        raise ConfigError("catalogue_output parent must not be a symlink")


def _validate_roots(config: InstanceConfig) -> None:
    named_roots = tuple(
        (name, path)
        for name, path in (
        ("inventory_root", config.inventory_root),
        ("runtime_dir", config.runtime_dir),
        ("media_root", config.media_root),
        ("catalogue_output", config.catalogue_output),
        )
        if path is not None
    )
    for index, (left_name, left_path) in enumerate(named_roots):
        for right_name, right_path in named_roots[index + 1 :]:
            if (
                {left_name, right_name} == {"inventory_root", "catalogue_output"}
                and config.catalogue_output == config.inventory_root / "Inventory.md"
            ):
                continue
            if _overlap(left_path, right_path):
                raise ConfigError(f"{left_name} and {right_name} must not overlap")

    _validate_data_roots_against_forbidden(named_roots, config.forbidden_roots)
    for forbidden_root in config.forbidden_roots:
        if config.catalogue_output == forbidden_root or config.catalogue_output in forbidden_root.parents:
            raise ConfigError(
                "catalogue_output may be inside a forbidden root, but cannot contain or equal one: "
                f"{forbidden_root}"
            )


def _validate_data_roots_against_forbidden(
    named_roots: Sequence[tuple[str, Path | None]], forbidden_roots: Sequence[Path]
) -> None:
    for forbidden_root in forbidden_roots:
        for name, path in named_roots:
            if name == "catalogue_output" or path is None:
                continue
            if _overlap(path, forbidden_root):
                raise ConfigError(f"{name} must not overlap forbidden root: {forbidden_root}")


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents
