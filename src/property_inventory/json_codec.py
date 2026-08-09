"""One duplicate-free, finite JSON contract for canonical and external data."""

from __future__ import annotations

import json
import math
from collections.abc import Callable


class StrictJSONError(ValueError):
    """Raised when JSON is ambiguous, non-finite, malformed, or too deep."""


def _validate_tree(
    value: object,
    label: str,
    *,
    max_depth: int | None,
    max_nodes: int | None,
) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if (max_nodes is not None and nodes > max_nodes) or (
            max_depth is not None and depth > max_depth
        ):
            raise StrictJSONError(f"{label} exceeds structural limits")
        if isinstance(current, str):
            try:
                current.encode("utf-8")
            except UnicodeEncodeError as error:
                raise StrictJSONError(f"{label} contains invalid Unicode text") from error
        elif isinstance(current, float) and not math.isfinite(current):
            raise StrictJSONError(f"{label} contains a non-finite number")
        elif isinstance(current, dict):
            for key, child in current.items():
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError as error:
                    raise StrictJSONError(
                        f"{label} contains an invalid Unicode key"
                    ) from error
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)


def loads(
    value: str | bytes,
    *,
    label: str = "JSON",
    parse_float: Callable[[str], object] | None = None,
    max_depth: int | None = 128,
    max_nodes: int | None = 1_000_000,
) -> object:
    """Decode strict UTF-8 JSON with shared ambiguity and safety checks."""

    def reject_constant(constant: str) -> None:
        raise StrictJSONError(f"invalid JSON constant {constant}")

    def reject_duplicate_pairs(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, child in pairs:
            if key in result:
                raise StrictJSONError(f"duplicate JSON key {key}")
            result[key] = child
        return result

    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="strict")
        options: dict[str, object] = {}
        if parse_float is not None:
            options["parse_float"] = parse_float
        result = json.loads(
            value,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_pairs,
            **options,
        )
        _validate_tree(
            result,
            label,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
        return result
    except (
        RecursionError,
        TypeError,
        UnicodeDecodeError,
        StrictJSONError,
        json.JSONDecodeError,
    ) as error:
        raise StrictJSONError(f"{label} is malformed: {error}") from error


def dumps(value: object, *, sort_keys: bool = False) -> str:
    try:
        _validate_tree(value, "JSON", max_depth=128, max_nodes=1_000_000)
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=sort_keys,
            allow_nan=False,
        )
    except (RecursionError, TypeError, UnicodeEncodeError, ValueError) as error:
        raise StrictJSONError(f"JSON is not canonical: {error}") from error
