"""Plan and execute a verifiable blank-restore doctor drill.

The doctor owns no inventory data and never removes a backup.  It delegates to
the existing CLI export, restore, and status commands through an injectable
runner, so a caller cannot mistake a local checksum for a restore test.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


class DoctorError(RuntimeError):
    """Raised when the planned blank-restore drill is unsafe or fails."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.returncode, bool) or not isinstance(self.returncode, int):
            raise DoctorError("runner returncode must be an integer")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise DoctorError("runner stdout and stderr must be text")


@dataclass(frozen=True)
class DoctorCommand:
    label: str
    arguments: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.label not in {"export", "restore", "status"}:
            raise DoctorError("doctor command label is unsupported")
        if not self.arguments or any(
            not isinstance(argument, str) or not argument for argument in self.arguments
        ):
            raise DoctorError("doctor command arguments must be non-empty text")


@dataclass(frozen=True)
class BlankRestorePlan:
    executable: tuple[str, ...]
    source_inventory_root: Path
    source_runtime_dir: Path
    source_media_root: Path
    archive: Path
    restored_inventory_root: Path
    restored_runtime_dir: Path
    restored_media_root: Path
    commands: tuple[DoctorCommand, ...]
    source_catalogue_output: Path | None = None
    restored_catalogue_output: Path | None = None
    catalogue_scope: str = "personal"
    forbidden_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class DoctorReport:
    plan: BlankRestorePlan
    results: tuple[tuple[DoctorCommand, CommandResult], ...]


def _absolute(path: Path) -> Path:
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise DoctorError(f"cannot resolve doctor path: {path}") from error


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _require_separate(paths: dict[str, Path]) -> None:
    entries = tuple(paths.items())
    for index, (left_label, left) in enumerate(entries):
        for right_label, right in entries[index + 1 :]:
            if _overlaps(left, right):
                raise DoctorError(f"{left_label} overlaps {right_label}")


def _base_command(
    executable: Sequence[str],
    inventory_root: Path,
    runtime_dir: Path,
    media_root: Path,
    catalogue_output: Path | None = None,
    *,
    catalogue_scope: str = "personal",
    forbidden_roots: Sequence[Path] = (),
) -> tuple[str, ...]:
    if (
        isinstance(executable, (str, bytes))
        or not executable
        or any(not isinstance(part, str) or not part for part in executable)
    ):
        raise DoctorError("executable must be a non-empty command sequence")
    if catalogue_scope not in {"public", "personal", "private"}:
        raise DoctorError("catalogue_scope is unsupported")
    command = (
        *executable,
        "--inventory-root",
        str(inventory_root),
        "--runtime-dir",
        str(runtime_dir),
        "--media-root",
        str(media_root),
        "--catalogue-scope",
        catalogue_scope,
    )
    if catalogue_output is not None:
        command = (*command, "--catalogue-output", str(catalogue_output))
    for root in forbidden_roots:
        command = (*command, "--forbidden-root", str(root))
    return command


def plan_blank_restore(
    *,
    executable: Sequence[str],
    source_inventory_root: Path,
    source_runtime_dir: Path,
    source_media_root: Path,
    archive: Path,
    restored_inventory_root: Path,
    restored_runtime_dir: Path,
    restored_media_root: Path,
    source_catalogue_output: Path | None = None,
    restored_catalogue_output: Path | None = None,
    catalogue_scope: str = "personal",
    forbidden_roots: Sequence[Path] = (),
) -> BlankRestorePlan:
    """Return export, blank-target restore, and restored-status commands.

    The source and restored namespaces must be non-overlapping.  This is a
    planner, not a retention policy: it creates neither directories nor files.
    """
    source_inventory_root = _absolute(source_inventory_root)
    source_runtime_dir = _absolute(source_runtime_dir)
    source_media_root = _absolute(source_media_root)
    archive = _absolute(archive)
    restored_inventory_root = _absolute(restored_inventory_root)
    restored_runtime_dir = _absolute(restored_runtime_dir)
    restored_media_root = _absolute(restored_media_root)
    source_catalogue_output = (
        _absolute(source_catalogue_output) if source_catalogue_output is not None else None
    )
    restored_catalogue_output = (
        _absolute(restored_catalogue_output) if restored_catalogue_output is not None else None
    )
    forbidden_roots = tuple(_absolute(root) for root in forbidden_roots)
    _require_separate(
        {
            "source inventory": source_inventory_root,
            "source runtime": source_runtime_dir,
            "source media": source_media_root,
            "archive": archive,
            "restored inventory": restored_inventory_root,
            "restored runtime": restored_runtime_dir,
            "restored media": restored_media_root,
        }
    )
    # A catalogue may deliberately live below its own inventory root. It must
    # nevertheless stay away from the other instance and the retained archive.
    if source_catalogue_output is not None:
        _require_separate(
            {
                "source catalogue": source_catalogue_output,
                "archive": archive,
                "restored inventory": restored_inventory_root,
                "restored runtime": restored_runtime_dir,
                "restored media": restored_media_root,
            }
        )
    if restored_catalogue_output is not None:
        _require_separate(
            {
                "restored catalogue": restored_catalogue_output,
                "archive": archive,
                "source inventory": source_inventory_root,
                "source runtime": source_runtime_dir,
                "source media": source_media_root,
            }
        )
    if source_catalogue_output is not None and restored_catalogue_output is not None:
        _require_separate(
            {
                "source catalogue": source_catalogue_output,
                "restored catalogue": restored_catalogue_output,
            }
        )
    export = DoctorCommand(
        "export",
        (
            *_base_command(
                executable,
                source_inventory_root,
                source_runtime_dir,
                source_media_root,
                source_catalogue_output,
                catalogue_scope=catalogue_scope,
                forbidden_roots=forbidden_roots,
            ),
            "export",
            "--output",
            str(archive),
        ),
    )
    restore = DoctorCommand(
        "restore",
        (
            *_base_command(
                executable,
                restored_inventory_root,
                restored_runtime_dir,
                restored_media_root,
                restored_catalogue_output,
                catalogue_scope=catalogue_scope,
                forbidden_roots=forbidden_roots,
            ),
            "restore",
            "--archive",
            str(archive),
        ),
    )
    status = DoctorCommand(
        "status",
        (
            *_base_command(
                executable,
                restored_inventory_root,
                restored_runtime_dir,
                restored_media_root,
                restored_catalogue_output,
                catalogue_scope=catalogue_scope,
                forbidden_roots=forbidden_roots,
            ),
            "status",
        ),
    )
    return BlankRestorePlan(
        executable=tuple(executable),
        source_inventory_root=source_inventory_root,
        source_runtime_dir=source_runtime_dir,
        source_media_root=source_media_root,
        archive=archive,
        restored_inventory_root=restored_inventory_root,
        restored_runtime_dir=restored_runtime_dir,
        restored_media_root=restored_media_root,
        commands=(export, restore, status),
        source_catalogue_output=source_catalogue_output,
        restored_catalogue_output=restored_catalogue_output,
        catalogue_scope=catalogue_scope,
        forbidden_roots=forbidden_roots,
    )


def path_is_blank(path: Path) -> bool:
    """Whether a path is absent or an empty real directory, without changing it."""
    if path.is_symlink():
        return False
    if not path.exists():
        return True
    return path.is_dir() and not any(path.iterdir())


def archive_is_regular(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()


def archive_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise DoctorError(f"cannot hash doctor archive: {path}") from error
    return digest.hexdigest()


def _validate_plan(plan: BlankRestorePlan) -> None:
    # Plans may be constructed directly. Rebuild the one canonical plan from
    # its bound fields so path normalization, overlap checks, and command
    # construction have exactly one implementation.
    try:
        canonical = plan_blank_restore(
            executable=plan.executable,
            source_inventory_root=plan.source_inventory_root,
            source_runtime_dir=plan.source_runtime_dir,
            source_media_root=plan.source_media_root,
            archive=plan.archive,
            restored_inventory_root=plan.restored_inventory_root,
            restored_runtime_dir=plan.restored_runtime_dir,
            restored_media_root=plan.restored_media_root,
            source_catalogue_output=plan.source_catalogue_output,
            restored_catalogue_output=plan.restored_catalogue_output,
            catalogue_scope=plan.catalogue_scope,
            forbidden_roots=plan.forbidden_roots,
        )
    except (TypeError, ValueError) as error:
        raise DoctorError("doctor plan fields are invalid") from error
    if plan != canonical:
        raise DoctorError(
            "doctor plan must exactly match its bound export, restore, and status commands"
        )


def run_blank_restore(
    plan: BlankRestorePlan,
    *,
    runner: Callable[[tuple[str, ...]], CommandResult],
    blank_validator: Callable[[Path], bool] = path_is_blank,
    archive_validator: Callable[[Path], bool] = archive_is_regular,
    archive_hasher: Callable[[Path], str] = archive_digest,
) -> DoctorReport:
    """Execute the only acceptable drill: export, restore into blanks, then status.

    Each failed command aborts immediately. A runner result cannot be converted
    into a passing report. This core does not delete targets or the archive; a
    caller may place targets in an automatically cleaned private temporary root.
    """
    if not isinstance(plan, BlankRestorePlan):
        raise DoctorError("plan must be a BlankRestorePlan")
    if (
        not callable(runner)
        or not callable(blank_validator)
        or not callable(archive_validator)
        or not callable(archive_hasher)
    ):
        raise DoctorError("doctor runner and validators must be callable")
    _validate_plan(plan)
    targets = (
        plan.restored_inventory_root,
        plan.restored_runtime_dir,
        plan.restored_media_root,
    )
    for path in targets:
        if not blank_validator(path):
            raise DoctorError(f"blank restore target is not blank: {path}")
    results: list[tuple[DoctorCommand, CommandResult]] = []
    exported_digest: str | None = None
    for command in plan.commands:
        if command.label == "restore":
            if not archive_validator(plan.archive):
                raise DoctorError(f"export archive is missing before restore: {plan.archive}")
            for path in targets:
                if not blank_validator(path):
                    raise DoctorError(f"blank restore target changed after export: {path}")
            if exported_digest is None or archive_hasher(plan.archive) != exported_digest:
                raise DoctorError("doctor export archive changed before restore")
        result = runner(command.arguments)
        if not isinstance(result, CommandResult):
            raise DoctorError("runner must return CommandResult")
        results.append((command, result))
        if result.returncode != 0:
            detail = result.stderr or result.stdout or "no output"
            raise DoctorError(
                f"doctor {command.label} failed with exit {result.returncode}: {detail}"
            )
        if command.label == "export":
            if not archive_validator(plan.archive):
                raise DoctorError(f"doctor export did not create a regular archive: {plan.archive}")
            exported_digest = archive_hasher(plan.archive)
        if command.label == "restore":
            if not archive_validator(plan.archive):
                raise DoctorError(f"export archive is missing after restore: {plan.archive}")
            if exported_digest is None or archive_hasher(plan.archive) != exported_digest:
                raise DoctorError("doctor export archive changed during restore")
        if command.label == "status":

            def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
                parsed: dict[str, object] = {}
                for key, value in pairs:
                    if key in parsed:
                        raise ValueError(f"duplicate JSON key {key}")
                    parsed[key] = value
                return parsed

            try:
                status = json.loads(result.stdout, object_pairs_hook=reject_duplicate_pairs)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise DoctorError("doctor status did not return JSON") from error
            if not isinstance(status, dict) or status.get("status") != "pass":
                raise DoctorError("doctor restored status did not pass")
    return DoctorReport(plan=plan, results=tuple(results))
