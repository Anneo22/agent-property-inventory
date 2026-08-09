"""Strict named-local-command protocol for OCR and barcode capture adapters.

Configured adapters are trusted local code. This module does not claim to
sandbox them or prevent network access; it only prevents callers from changing
their configured command at invocation time.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .capture import (
    CAPTURE_COORDINATE_SPACE,
    MAX_CAPTURE_SEGMENTS,
    MAX_CAPTURE_SOURCE_BYTES,
    CaptureError,
    CaptureObservation,
    CaptureSegment,
    ImageRegion,
    normalize_observations,
)
from .json_codec import StrictJSONError
from .json_codec import loads as strict_json_loads

# A local adapter returns one compact JSON result, never media. These caps bound
# untrusted output before it reaches JSON parsing or the observation freezer.
MAX_ADAPTER_RESPONSE_BYTES = 64 * 1024
MAX_ADAPTER_JSON_DEPTH = 32
MAX_ADAPTER_CONFIG_BYTES = 64 * 1024
MAX_ADAPTER_TIMEOUT_SECONDS = 60.0
_ADAPTER_SOURCE_FILENAME = "overview-image"


class AdapterError(CaptureError):
    """Raised when a named local adapter breaches the one-object protocol."""


def _utf8_bytes(value: object, field: str) -> bytes:
    if not isinstance(value, str):
        raise AdapterError(f"{field} must be a string")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise AdapterError(f"{field} is not valid UTF-8 text") from error


def _command(value: object, field: str) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        raise AdapterError(f"{field} must be a non-empty immutable argv tuple")
    for part in value:
        _utf8_bytes(part, field)
        if not part or "\x00" in part:
            raise AdapterError(f"{field} must contain non-empty command strings")
    if not Path(value[0]).is_absolute():
        raise AdapterError(f"{field} executable must be an absolute configured path")
    if "-c" in value:
        raise AdapterError(f"{field} must not use interpreter -c")
    return tuple(value)


@dataclass(frozen=True)
class AdapterRegistry:
    """Name-to-versioned-command configuration, frozen independently of callers."""

    commands: Mapping[str, tuple[str, ...]]
    revisions: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.commands, Mapping):
            raise AdapterError("adapter commands must be a mapping")
        if not isinstance(self.revisions, Mapping):
            raise AdapterError("adapter revisions must be a mapping")
        frozen: dict[str, tuple[str, ...]] = {}
        for name, command in self.commands.items():
            _utf8_bytes(name, "adapter name")
            if not name.strip():
                raise AdapterError("adapter names must be non-empty strings")
            frozen[name] = _command(command, f"adapter command {name!r}")
        if set(self.revisions) != set(frozen):
            raise AdapterError("adapter revisions must exactly match adapter commands")
        frozen_revisions: dict[str, str] = {}
        for name, revision in self.revisions.items():
            revision_bytes = _utf8_bytes(revision, f"adapter revision {name!r}")
            if (
                not revision.strip()
                or revision != revision.strip()
                or len(revision_bytes) > 256
                or any(ord(character) < 32 for character in revision)
            ):
                raise AdapterError(f"adapter revision {name!r} is invalid")
            frozen_revisions[name] = revision
        object.__setattr__(self, "commands", MappingProxyType(frozen))
        object.__setattr__(self, "revisions", MappingProxyType(frozen_revisions))

    def command_for(self, adapter_name: str) -> tuple[str, ...]:
        if not isinstance(adapter_name, str) or not adapter_name.strip():
            raise AdapterError("adapter name must be a non-empty string")
        try:
            return self.commands[adapter_name]
        except KeyError as error:
            raise AdapterError("adapter is not configured") from error

    def identity_for(self, adapter_name: str) -> dict[str, str]:
        """Return the durable server-owned identity for one adapter revision."""
        command = self.command_for(adapter_name)
        command_sha256 = hashlib.sha256(
            json.dumps(
                list(command),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return {
            "name": adapter_name,
            "revision": self.revisions[adapter_name],
            "command_sha256": command_sha256,
        }


def load_adapter_registry(path: Path) -> AdapterRegistry:
    """Load one server-owned exact-command registry from a bounded local file."""
    lexical = Path(os.path.abspath(path.expanduser()))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(lexical, flags)
    except OSError as error:
        raise AdapterError(
            "capture adapter config must be a regular non-symlink file"
        ) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AdapterError(
                "capture adapter config must be a regular non-symlink file"
            )
        if before.st_size > MAX_ADAPTER_CONFIG_BYTES:
            raise AdapterError("capture adapter config exceeds the byte limit")
        chunks: list[bytes] = []
        remaining = MAX_ADAPTER_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_ADAPTER_CONFIG_BYTES:
            raise AdapterError("capture adapter config exceeds the byte limit")
        after = os.fstat(descriptor)
        named = os.stat(lexical, follow_symlinks=False)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if (
            identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISREG(named.st_mode)
        ):
            raise AdapterError("capture adapter config changed while it was read")
    except OSError as error:
        raise AdapterError("cannot read capture adapter config") from error
    finally:
        os.close(descriptor)

    try:
        document = strict_json_loads(payload, label="capture adapter config")
    except StrictJSONError as error:
        raise AdapterError("capture adapter config is malformed") from error
    if (
        not isinstance(document, dict)
        or set(document) != {"adapters", "version"}
        or type(document.get("version")) is not int
        or document.get("version") != 2
        or not isinstance(document.get("adapters"), dict)
    ):
        raise AdapterError("capture adapter config has an unsupported schema")
    adapters = document["adapters"]
    assert isinstance(adapters, dict)
    commands: dict[str, tuple[str, ...]] = {}
    revisions: dict[str, str] = {}
    for name, specification in adapters.items():
        if (
            not isinstance(name, str)
            or not isinstance(specification, dict)
            or set(specification) != {"command", "revision"}
            or not isinstance(specification.get("command"), list)
        ):
            raise AdapterError(
                "capture adapter config entries require command and revision"
            )
        commands[name] = tuple(specification["command"])
        revisions[name] = specification["revision"]
    return AdapterRegistry(commands, revisions)


@dataclass(frozen=True)
class AdapterResponse:
    """Validated segmentation/OCR/barcode output from one local adapter invocation."""

    protocol_version: int
    observations: tuple[CaptureObservation, ...]
    predicted_segments: tuple[CaptureSegment, ...] = ()

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != 1:
            raise AdapterError("adapter response protocol_version must be 1")
        if type(self.observations) is not tuple or any(
            not isinstance(item, CaptureObservation) for item in self.observations
        ):
            raise AdapterError("adapter response observations must be an immutable observation tuple")
        if type(self.predicted_segments) is not tuple or any(
            not isinstance(item, CaptureSegment) for item in self.predicted_segments
        ):
            raise AdapterError(
                "adapter response segments must be an immutable segment tuple"
            )
        if len(self.predicted_segments) > MAX_CAPTURE_SEGMENTS:
            raise AdapterError("adapter response exceeds the capture segment limit")
        if len({segment.segment_id for segment in self.predicted_segments}) != len(
            self.predicted_segments
        ):
            raise AdapterError("adapter response segment IDs must be unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "predicted_segments": [
                segment.to_dict() for segment in self.predicted_segments
            ],
            "observations": [observation.to_dict() for observation in self.observations],
        }


def _request_bounds(request: Mapping[str, object]) -> tuple[int, int]:
    if type(request.get("protocol_version")) is not int or request.get("protocol_version") != 1:
        raise AdapterError("adapter request protocol_version must be 1")
    source = request.get("source")
    if not isinstance(source, Mapping):
        raise AdapterError("adapter request source must be an object with image dimensions")
    width = source.get("image_width")
    height = source.get("image_height")
    if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
        raise AdapterError("adapter request source image dimensions are invalid")
    if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        raise AdapterError("adapter request source image dimensions are invalid")
    if source.get("coordinate_space") != CAPTURE_COORDINATE_SPACE:
        raise AdapterError(
            "adapter request source coordinate_space must be exif_transposed_pixels"
        )
    return width, height


def _validate_request(
    request: Mapping[str, object], *, source_data: bytes
) -> tuple[bytes, tuple[int, int]]:
    if not isinstance(request, Mapping):
        raise AdapterError("adapter request must be one JSON object")
    bounds = _request_bounds(request)
    if not isinstance(source_data, bytes) or not source_data:
        raise AdapterError("adapter source_data must be non-empty bytes")
    if len(source_data) > MAX_CAPTURE_SOURCE_BYTES:
        raise AdapterError("adapter source_data exceeds the capture byte limit")
    source = request["source"]
    assert isinstance(source, Mapping)  # proved by _request_bounds
    if "image_file" in source:
        raise AdapterError("adapter request source.image_file is controlled by the capture runtime")
    declared_length = source.get("byte_length")
    declared_digest = source.get("sha256")
    if (
        isinstance(declared_length, bool)
        or not isinstance(declared_length, int)
        or declared_length != len(source_data)
        or not isinstance(declared_digest, str)
        or declared_digest != hashlib.sha256(source_data).hexdigest()
    ):
        raise AdapterError("adapter source bytes do not match the supplied source manifest")
    # This is deliberately the only media locator in the protocol.  A named
    # adapter is trusted local code, but callers cannot choose a path or argv:
    # it receives the exact manifest-bound bytes at this fixed relative name in
    # a neutral temporary working directory. Coordinates are always relative
    # to the image after EXIF orientation is applied. Adapters MUST decode the
    # exact source bytes with EXIF transpose before producing observations.
    request_for_adapter = dict(request)
    source_for_adapter = dict(source)
    source_for_adapter["image_file"] = _ADAPTER_SOURCE_FILENAME
    request_for_adapter["source"] = source_for_adapter
    try:
        encoded = json.dumps(
            request_for_adapter, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise AdapterError("adapter request must be JSON serializable") from error
    return encoded.encode("utf-8"), bounds


def _validate_json_depth(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > MAX_ADAPTER_JSON_DEPTH:
            raise AdapterError("adapter response exceeds safety limits")
        if isinstance(current, Mapping):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)


def _parse_response(stdout: bytes, *, image_width: int, image_height: int) -> AdapterResponse:
    if len(stdout) > MAX_ADAPTER_RESPONSE_BYTES:
        raise AdapterError("adapter response exceeds safety limits")
    try:
        parsed: Any = strict_json_loads(
            stdout,
            label="adapter response",
            max_depth=None,
        )
    except StrictJSONError as error:
        raise AdapterError("adapter returned malformed JSON") from error
    try:
        _validate_json_depth(parsed)
    except RecursionError as error:
        raise AdapterError("adapter response exceeds safety limits") from error
    if not isinstance(parsed, Mapping) or set(parsed) not in (
        {"protocol_version", "observations"},
        {"protocol_version", "observations", "predicted_segments"},
    ):
        raise AdapterError("adapter returned an invalid response schema")
    protocol_version = parsed["protocol_version"]
    if isinstance(protocol_version, bool) or not isinstance(protocol_version, int) or protocol_version != 1:
        raise AdapterError("adapter returned an invalid response schema")
    observations = parsed["observations"]
    if not isinstance(observations, list):
        raise AdapterError("adapter returned an invalid response schema")
    raw_segments = parsed.get("predicted_segments", [])
    if not isinstance(raw_segments, list) or len(raw_segments) > MAX_CAPTURE_SEGMENTS:
        raise AdapterError("adapter returned an invalid response schema")
    try:
        normalized = normalize_observations(
            observations, image_width=image_width, image_height=image_height
        )
        segments = tuple(
            CaptureSegment(
                segment_id=raw["segment_id"],
                region=ImageRegion.from_mapping(
                    raw["region"], f"segments[{index}].region"
                ),
            )
            for index, raw in enumerate(raw_segments)
            if isinstance(raw, Mapping) and set(raw) == {"segment_id", "region"}
        )
        if len(segments) != len(raw_segments):
            raise CaptureError(
                "adapter segments must contain exactly segment_id and region"
            )
        if len({segment.segment_id for segment in segments}) != len(segments):
            raise CaptureError("adapter segment IDs must be unique")
        for segment in segments:
            segment.region.validate_within(image_width, image_height)
        if sum(
            segment.region.width * segment.region.height for segment in segments
        ) > 4 * image_width * image_height:
            raise CaptureError("adapter segments exceed the total crop pixel limit")
    except (CaptureError, KeyError, TypeError) as error:
        raise AdapterError("adapter returned an invalid response schema") from error
    return AdapterResponse(
        protocol_version=1,
        observations=normalized,
        predicted_segments=segments,
    )


def _minimal_environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
    }


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _bounded_stdout(
    process: subprocess.Popen[bytes], *, timeout_seconds: float
) -> bytes:
    """Read adapter stdout incrementally and kill the group at either bound."""
    if process.stdout is None:
        raise AdapterError("adapter execution failed")
    deadline = time.monotonic() + timeout_seconds
    chunks: list[bytes] = []
    total = 0
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_group(process)
                process.wait()
                raise AdapterError("adapter timed out")
            if not selector.select(remaining):
                _kill_process_group(process)
                process.wait()
                raise AdapterError("adapter timed out")
            chunk = os.read(
                process.stdout.fileno(),
                min(8192, MAX_ADAPTER_RESPONSE_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_ADAPTER_RESPONSE_BYTES:
                _kill_process_group(process)
                process.wait()
                raise AdapterError("adapter response exceeds safety limits")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _kill_process_group(process)
        process.wait()
        raise AdapterError("adapter timed out")
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as error:
        _kill_process_group(process)
        process.wait()
        raise AdapterError("adapter timed out") from error
    return b"".join(chunks)


def run_local_adapter(
    *,
    adapter_name: str,
    registry: AdapterRegistry,
    request: Mapping[str, object],
    source_data: bytes,
    timeout_seconds: float = 10.0,
) -> AdapterResponse:
    """Run trusted configured code with exact manifest-bound image bytes in a neutral directory.

    This is a local-trusted boundary, not a sandbox.  The adapter may read the
    fixed ``source.image_file`` relative path, but cannot receive caller-chosen
    media paths, environment variables, or command arguments.
    """
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
        or timeout_seconds > MAX_ADAPTER_TIMEOUT_SECONDS
    ):
        raise AdapterError(
            "adapter timeout_seconds must be finite, positive, and no more than 60 seconds"
        )
    if not isinstance(registry, AdapterRegistry):
        raise AdapterError("adapter registry is invalid")
    command = registry.command_for(adapter_name)
    request_bytes, (image_width, image_height) = _validate_request(
        request, source_data=source_data
    )
    try:
        with tempfile.TemporaryDirectory(prefix="property-inventory-capture-adapter-") as temporary_directory:
            source_path = Path(temporary_directory) / _ADAPTER_SOURCE_FILENAME
            descriptor = os.open(source_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as source_file:
                source_file.write(source_data)
            request_path = Path(temporary_directory) / "request.json"
            request_descriptor = os.open(
                request_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(request_descriptor, "wb") as request_file:
                request_file.write(request_bytes)
            with request_path.open("rb") as request_file:
                process = subprocess.Popen(
                    command,
                    stdin=request_file,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=temporary_directory,
                    env=_minimal_environment(),
                    start_new_session=True,
                )
                try:
                    stdout = _bounded_stdout(
                        process, timeout_seconds=float(timeout_seconds)
                    )
                finally:
                    if process.stdout is not None:
                        process.stdout.close()
    except AdapterError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise AdapterError("adapter execution failed") from error
    if process.returncode != 0:
        raise AdapterError("adapter execution failed")
    return _parse_response(stdout, image_width=image_width, image_height=image_height)
