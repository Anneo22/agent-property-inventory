"""Pure, evidence-aware spatial reasoning primitives.

This module deliberately has no dependency on the inventory store, filesystem, or
network.  Callers supply only checked measurements and may use the ``unknown``
results to avoid turning an omitted measurement into a negative conclusion.

All comparisons use one relative, scale-aware tolerance derived from interval
lengths, never absolute coordinates. Arithmetic is never rounded wholesale:
only residuals within that tolerance are canonicalised to zero, preventing
binary-decimal slivers from becoming invented free space.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import permutations
from math import isfinite
from typing import Literal


class SpatialValidationError(ValueError):
    """Raised when supplied geometry is malformed or cannot be safely compared."""


_UNIT_TO_METRES = {
    "mm": 0.001,
    "cm": 0.01,
    "m": 1.0,
    "in": 0.0254,
    "ft": 0.3048,
}
_AXES = ("width", "depth", "height")
_ROTATIONS = tuple(permutations(_AXES))
# All geometric comparisons use this one scale-aware policy.  It permits normal
# binary-decimal residue (for example 0.1 + 0.2) but is small enough not to
# conceal a physically meaningful discrepancy at the measurement's own scale.
_RELATIVE_TOLERANCE = 1e-9


def _finite_number(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpatialValidationError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise SpatialValidationError(f"{label} must be a finite number") from error
    if not isfinite(number):
        raise SpatialValidationError(f"{label} must be a finite number")
    if positive and number <= 0:
        raise SpatialValidationError(f"{label} must be positive")
    return number


def _derived(value: float, label: str) -> float:
    """Reject an overflow or NaN produced by geometry arithmetic."""
    if not isfinite(value):
        raise SpatialValidationError(f"{label} is non-finite")
    return value


def _add(left: float, right: float, label: str) -> float:
    return _derived(left + right, label)


def _subtract(left: float, right: float, label: str) -> float:
    return _derived(left - right, label)


def _product(values: Iterable[float], label: str) -> float:
    result = 1.0
    for value in values:
        result = _derived(result * value, label)
    if result == 0.0:
        raise SpatialValidationError(f"{label} underflows to zero")
    return result


def _tolerance(*lengths: float) -> float:
    """Return the tolerance from interval lengths, never world coordinates."""
    scale = max(1e-12, *(abs(_derived(length, "comparison length")) for length in lengths))
    return _derived(scale * _RELATIVE_TOLERANCE, "comparison tolerance")


def _nonnegative_margin(margin: float, *lengths: float) -> bool:
    return margin >= -_tolerance(*lengths)


def _positive_margin(margin: float, *lengths: float) -> bool:
    return margin > _tolerance(*lengths)


def _contains_interval(
    container_start: float,
    container_length: float,
    subject_start: float,
    subject_length: float,
) -> bool:
    lower_margin = _subtract(subject_start, container_start, "lower containment margin")
    upper_margin = _subtract(
        _add(container_start, container_length, "container interval end"),
        _add(subject_start, subject_length, "subject interval end"),
        "upper containment margin",
    )
    return _nonnegative_margin(lower_margin, container_length, subject_length) and _nonnegative_margin(
        upper_margin, container_length, subject_length
    )


def _intervals_overlap(
    first_start: float,
    first_length: float,
    second_start: float,
    second_length: float,
) -> bool:
    first_before_second_end = _subtract(
        _add(second_start, second_length, "second interval end"),
        first_start,
        "first overlap margin",
    )
    second_before_first_end = _subtract(
        _add(first_start, first_length, "first interval end"),
        second_start,
        "second overlap margin",
    )
    return _positive_margin(first_before_second_end, first_length, second_length) and _positive_margin(
        second_before_first_end, first_length, second_length
    )


def _residual(value: float, *scale: float) -> float:
    value = _derived(value, "residual")
    return 0.0 if abs(value) <= _tolerance(value, *scale) else value


def _unit(value: object) -> str:
    if not isinstance(value, str) or value not in _UNIT_TO_METRES:
        supported = ", ".join(sorted(_UNIT_TO_METRES))
        raise SpatialValidationError(f"unit must be one of: {supported}")
    return value


def convert_length(value: object, from_unit: str, to_unit: str) -> float:
    """Convert one positive checked length without exposing internal unit tables."""
    number = _finite_number(value, "length", positive=True)
    source = _unit(from_unit)
    target = _unit(to_unit)
    return _derived(
        number * _UNIT_TO_METRES[source] / _UNIT_TO_METRES[target],
        "converted length",
    )


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpatialValidationError(f"{label} must be a non-empty string")
    return value


@dataclass(frozen=True)
class Dimensions:
    """A checked three-dimensional measurement in one explicit length unit."""

    width: float
    height: float
    depth: float
    unit: str
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "width", _finite_number(self.width, "width", positive=True))
        object.__setattr__(self, "height", _finite_number(self.height, "height", positive=True))
        object.__setattr__(self, "depth", _finite_number(self.depth, "depth", positive=True))
        object.__setattr__(self, "unit", _unit(self.unit))
        if self.evidence_id is not None:
            _identifier(self.evidence_id, "evidence_id")
        _product((self.width, self.height, self.depth), "dimensions volume")

    def in_unit(self, unit: str) -> Dimensions:
        """Return the same physical dimensions in ``unit``."""
        unit = _unit(unit)
        return Dimensions(
            width=convert_length(self.width, self.unit, unit),
            height=convert_length(self.height, self.unit, unit),
            depth=convert_length(self.depth, self.unit, unit),
            unit=unit,
            evidence_id=self.evidence_id,
        )


@dataclass(frozen=True)
class Rectangle:
    """An axis-aligned 2D rectangle, useful for floor-plan regions."""

    x: float
    y: float
    width: float
    height: float
    unit: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _finite_number(self.x, "x"))
        object.__setattr__(self, "y", _finite_number(self.y, "y"))
        object.__setattr__(self, "width", _finite_number(self.width, "width", positive=True))
        object.__setattr__(self, "height", _finite_number(self.height, "height", positive=True))
        object.__setattr__(self, "unit", _unit(self.unit))
        _add(self.x, self.width, "rectangle right edge")
        _add(self.y, self.height, "rectangle top edge")
        _product((self.width, self.height), "rectangle area")

    @property
    def right(self) -> float:
        return _add(self.x, self.width, "rectangle right edge")

    @property
    def top(self) -> float:
        return _add(self.y, self.height, "rectangle top edge")

@dataclass(frozen=True)
class Box:
    """An axis-aligned 3D box with dimensions in one explicit length unit."""

    x: float
    y: float
    z: float
    width: float
    height: float
    depth: float
    unit: str

    def __post_init__(self) -> None:
        for name in ("x", "y", "z"):
            object.__setattr__(self, name, _finite_number(getattr(self, name), name))
        for name in ("width", "height", "depth"):
            object.__setattr__(self, name, _finite_number(getattr(self, name), name, positive=True))
        object.__setattr__(self, "unit", _unit(self.unit))
        _add(self.x, self.width, "box x edge")
        _add(self.y, self.depth, "box y edge")
        _add(self.z, self.height, "box z edge")
        _product((self.width, self.height, self.depth), "box volume")

    def in_unit(self, unit: str) -> Box:
        """Return the same positioned box in ``unit``."""
        unit = _unit(unit)
        factor = _UNIT_TO_METRES[self.unit] / _UNIT_TO_METRES[unit]
        return Box(
            _derived(self.x * factor, "converted box x"),
            _derived(self.y * factor, "converted box y"),
            _derived(self.z * factor, "converted box z"),
            _derived(self.width * factor, "converted box width"),
            _derived(self.height * factor, "converted box height"),
            _derived(self.depth * factor, "converted box depth"),
            unit,
        )

    @property
    def volume(self) -> float:
        return _product((self.width, self.height, self.depth), "box volume")

    @property
    def max_x(self) -> float:
        return _add(self.x, self.width, "box x edge")

    @property
    def max_y(self) -> float:
        return _add(self.y, self.depth, "box y edge")

    @property
    def max_z(self) -> float:
        return _add(self.z, self.height, "box z edge")


def normalize_spatial_profile(profile: object) -> dict[str, object]:
    """Validate and canonicalise one persisted spatial-profile shape."""
    if not isinstance(profile, Mapping):
        raise SpatialValidationError("spatial profile must be an object")
    kind = profile.get("kind")
    if kind == "floor_rectangle":
        required = {"kind", "x", "y", "width", "height", "unit"}
        if set(profile) != required:
            raise SpatialValidationError(
                "floor_rectangle must contain exactly kind, x, y, width, height, and unit"
            )
        rectangle = Rectangle(
            profile["x"],
            profile["y"],
            profile["width"],
            profile["height"],
            profile["unit"],
        )
        return {
            "height": rectangle.height,
            "kind": kind,
            "unit": rectangle.unit,
            "width": rectangle.width,
            "x": rectangle.x,
            "y": rectangle.y,
        }
    if kind == "container_box":
        required = {"kind", "x", "y", "z", "width", "height", "depth", "unit"}
        if set(profile) != required:
            raise SpatialValidationError(
                "container_box must contain exactly kind, x, y, z, width, height, depth, and unit"
            )
        box = Box(
            profile["x"],
            profile["y"],
            profile["z"],
            profile["width"],
            profile["height"],
            profile["depth"],
            profile["unit"],
        )
        return {
            "depth": box.depth,
            "height": box.height,
            "kind": kind,
            "unit": box.unit,
            "width": box.width,
            "x": box.x,
            "y": box.y,
            "z": box.z,
        }
    raise SpatialValidationError("spatial profile kind must be floor_rectangle or container_box")


@dataclass(frozen=True)
class FloorPlanFeature:
    """One evidence-backed rectangular area in a floor plan."""

    feature_id: str
    rectangle: Rectangle
    evidence_id: str

    def __post_init__(self) -> None:
        _identifier(self.feature_id, "feature_id")
        _identifier(self.evidence_id, "evidence_id")


@dataclass(frozen=True)
class FloorPlan:
    """A homogeneous-unit collection of checked, rectangular floor-plan areas."""

    features: tuple[FloorPlanFeature, ...]
    unit: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "unit", _unit(self.unit))
        if not self.features:
            raise SpatialValidationError("floor plan must contain at least one feature")
        ids = [feature.feature_id for feature in self.features]
        if len(ids) != len(set(ids)):
            raise SpatialValidationError("floor plan feature IDs must be unique")
        if any(feature.rectangle.unit != self.unit for feature in self.features):
            raise SpatialValidationError("floor plan feature units must match")
        ordered = tuple(sorted(self.features, key=lambda feature: feature.feature_id))
        object.__setattr__(self, "features", ordered)


@dataclass(frozen=True)
class FitResult:
    status: Literal["fits", "does_not_fit", "unknown"]
    reason: str
    rotation: tuple[str, str, str] | None = None


@dataclass(frozen=True)
class VolumeResult:
    status: Literal["known", "unknown"]
    volume: float | None
    unit: str | None
    reason: str


@dataclass(frozen=True)
class PackItem:
    item_id: str
    dimensions: Dimensions | None

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")


@dataclass(frozen=True)
class Placement:
    item_id: str
    box: Box
    rotation: tuple[str, str, str]


@dataclass(frozen=True)
class PackingResult:
    status: Literal["packed", "partial", "unknown"]
    placements: tuple[Placement, ...]
    unplaced_item_ids: tuple[str, ...]
    reason: str


def _dimensions_from(value: Dimensions | Mapping[str, object] | None) -> Dimensions | None:
    """Coerce an external measurement, returning unknown (``None``) when incomplete.

    Present-but-invalid values remain errors.  This distinction is intentional:
    omitted evidence is unknown, while a corrupt measurement must be fixed.
    """
    if value is None or isinstance(value, Dimensions):
        return value
    if not isinstance(value, Mapping):
        raise SpatialValidationError("dimensions must be an object")
    required = ("width", "height", "depth", "unit")
    for field in ("width", "height", "depth"):
        if value.get(field) is not None:
            _finite_number(value[field], field, positive=True)
    if value.get("unit") is not None:
        _unit(value["unit"])
    evidence = value.get("evidence_id")
    if evidence is not None:
        _identifier(evidence, "evidence_id")
    if any(value.get(field) is None for field in required):
        return None
    return Dimensions(
        width=value["width"],
        height=value["height"],
        depth=value["depth"],
        unit=value["unit"],
        evidence_id=evidence,
    )


def parse_geojson_floor_plan(
    document: Mapping[str, object], *, expected_unit: str | None = None
) -> FloorPlan:
    """Parse rectangular GeoJSON Polygon features with explicit unit and evidence.

    Every feature needs a stable ID, ``properties.unit`` and
    ``properties.evidence_id``.  All features must use the same unit, so a
    coordinate is never silently interpreted in a different scale.
    """
    if not isinstance(document, Mapping) or document.get("type") != "FeatureCollection":
        raise SpatialValidationError("floor plan must be a GeoJSON FeatureCollection")
    features = document.get("features")
    if not isinstance(features, list) or not features:
        raise SpatialValidationError("GeoJSON FeatureCollection must have features")
    requested_unit = _unit(expected_unit) if expected_unit is not None else None
    parsed: list[FloorPlanFeature] = []
    for feature in features:
        if not isinstance(feature, Mapping) or feature.get("type") != "Feature":
            raise SpatialValidationError("floor plan entries must be GeoJSON Features")
        properties = feature.get("properties")
        if not isinstance(properties, Mapping):
            raise SpatialValidationError("floor plan feature properties must be an object")
        feature_id = feature.get("id", properties.get("feature_id"))
        evidence_id = properties.get("evidence_id")
        unit = properties.get("unit")
        feature_id = _identifier(feature_id, "feature ID")
        evidence_id = _identifier(evidence_id, "evidence_id")
        unit = _unit(unit)
        if requested_unit is None:
            requested_unit = unit
        if unit != requested_unit:
            raise SpatialValidationError("floor plan feature units must match")
        rectangle = _rectangle_from_geojson_geometry(feature.get("geometry"), unit)
        parsed.append(FloorPlanFeature(feature_id, rectangle, evidence_id))
    return FloorPlan(tuple(parsed), requested_unit)

def _rectangle_from_geojson_geometry(geometry: object, unit: str) -> Rectangle:
    if not isinstance(geometry, Mapping) or geometry.get("type") != "Polygon":
        raise SpatialValidationError("floor plan geometry must be a Polygon")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) != 1:
        raise SpatialValidationError("Polygon must have exactly one outer ring and no holes")
    ring = coordinates[0]
    if not isinstance(ring, list) or len(ring) != 5:
        raise SpatialValidationError("rectangular Polygon must have four sides and a closing point")
    points: list[tuple[float, float]] = []
    for index, position in enumerate(ring):
        if not isinstance(position, list) or len(position) != 2:
            raise SpatialValidationError("Polygon coordinates must be two-number positions")
        points.append(
            (
                _finite_number(position[0], f"coordinate {index} x"),
                _finite_number(position[1], f"coordinate {index} y"),
            )
        )
    if points[0] != points[-1]:
        raise SpatialValidationError("Polygon ring must be closed")
    corners = points[:-1]
    if len(set(corners)) != 4:
        raise SpatialValidationError("Polygon is degenerate or self-intersecting")
    for first, second in zip(corners, corners[1:] + corners[:1]):
        dx = _subtract(second[0], first[0], "polygon x edge")
        dy = _subtract(second[1], first[1], "polygon y edge")
        if (dx == 0) == (dy == 0):
            raise SpatialValidationError("Polygon must have non-zero axis-aligned edges")
    xs = {point[0] for point in corners}
    ys = {point[1] for point in corners}
    if len(xs) != 2 or len(ys) != 2:
        raise SpatialValidationError("Polygon must be an axis-aligned rectangle")
    expected_corners = {(x, y) for x in xs for y in ys}
    if set(corners) != expected_corners:
        raise SpatialValidationError("Polygon is self-intersecting or not rectangular")
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    return Rectangle(
        min_x,
        min_y,
        _subtract(max_x, min_x, "polygon width"),
        _subtract(max_y, min_y, "polygon height"),
        unit,
    )


def contains(container: Rectangle | Box, subject: Rectangle | Box) -> bool:
    """Return whether an axis-aligned subject is wholly inside its container."""
    if type(container) is not type(subject):
        raise SpatialValidationError("containment requires two rectangles or two boxes")
    if container.unit != subject.unit:
        raise SpatialValidationError("containment requires matching units")
    if isinstance(container, Rectangle):
        return (
            _contains_interval(container.x, container.width, subject.x, subject.width)
            and _contains_interval(container.y, container.height, subject.y, subject.height)
        )
    return (
        _contains_interval(container.x, container.width, subject.x, subject.width)
        and _contains_interval(container.y, container.depth, subject.y, subject.depth)
        and _contains_interval(container.z, container.height, subject.z, subject.height)
    )


def free_volume(
    container: Box | Dimensions | Mapping[str, object] | None,
    occupied: Iterable[Box | Placement | Mapping[str, object] | None],
) -> VolumeResult:
    """Calculate free volume from checked, positioned, non-overlapping boxes.

    A ``Dimensions`` container is the box rooted at ``(0, 0, 0)``.  Occupied
    values must be ``Box``/``Placement`` geometry: dimensions alone are not
    enough to prove which volumes overlap, so they return ``unknown`` rather
    than a fabricated capacity.
    """
    container_box = _container_box_from(container)
    if container_box is None:
        return VolumeResult("unknown", None, None, "container_dimensions_unknown")
    occupied_boxes = [_occupied_box_from(value) for value in occupied]
    if any(box is None for box in occupied_boxes):
        return VolumeResult("unknown", None, container_box.unit, "occupied_geometry_unknown")
    checked_boxes = [box.in_unit(container_box.unit) for box in occupied_boxes if box is not None]
    for box in checked_boxes:
        if not contains(container_box, box):
            raise SpatialValidationError("occupied box is outside the container")
    for index, first in enumerate(checked_boxes):
        for second in checked_boxes[index + 1 :]:
            if _boxes_overlap(first, second):
                raise SpatialValidationError("occupied boxes overlap")
    occupied_volume = 0.0
    for box in checked_boxes:
        occupied_volume = _add(occupied_volume, box.volume, "occupied volume")
    remaining = _residual(
        _subtract(container_box.volume, occupied_volume, "free volume"),
        container_box.volume,
        occupied_volume,
    )
    if remaining < 0:
        raise SpatialValidationError("positioned occupied volume exceeds container")
    return VolumeResult(
        "known",
        remaining,
        container_box.unit,
        "positioned_non_overlapping_boxes",
    )


def _container_box_from(value: Box | Dimensions | Mapping[str, object] | None) -> Box | None:
    if isinstance(value, Box):
        return value
    if isinstance(value, Dimensions):
        return Box(0, 0, 0, value.width, value.height, value.depth, value.unit)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SpatialValidationError("container geometry must be an object")
    if any(field in value for field in ("x", "y", "z")):
        return _box_from_mapping(value)
    dimensions = _dimensions_from(value)
    if dimensions is None:
        return None
    return Box(0, 0, 0, dimensions.width, dimensions.height, dimensions.depth, dimensions.unit)


def _occupied_box_from(value: Box | Placement | Mapping[str, object] | None) -> Box | None:
    if isinstance(value, Box):
        return value
    if isinstance(value, Placement):
        return value.box
    if isinstance(value, Dimensions):
        return None
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SpatialValidationError("occupied geometry must be an object")
    return _box_from_mapping(value)


def _box_from_mapping(value: Mapping[str, object]) -> Box | None:
    fields = ("x", "y", "z", "width", "height", "depth", "unit")
    for field in ("x", "y", "z"):
        if value.get(field) is not None:
            _finite_number(value[field], field)
    for field in ("width", "height", "depth"):
        if value.get(field) is not None:
            _finite_number(value[field], field, positive=True)
    if value.get("unit") is not None:
        _unit(value["unit"])
    if any(value.get(field) is None for field in fields):
        return None
    return Box(
        value["x"],
        value["y"],
        value["z"],
        value["width"],
        value["height"],
        value["depth"],
        value["unit"],
    )


def _boxes_overlap(first: Box, second: Box) -> bool:
    return (
        _intervals_overlap(first.x, first.width, second.x, second.width)
        and _intervals_overlap(first.y, first.depth, second.y, second.depth)
        and _intervals_overlap(first.z, first.height, second.z, second.height)
    )


def fit(
    item: Dimensions | Mapping[str, object] | None,
    container: Dimensions | Mapping[str, object] | None,
    *,
    allow_rotation: bool = True,
) -> FitResult:
    """Check a measured item against a measured container deterministically."""
    item_dimensions = _dimensions_from(item)
    container_dimensions = _dimensions_from(container)
    if item_dimensions is None or container_dimensions is None:
        return FitResult("unknown", "dimensions_or_units_unknown")
    item_in_container_unit = item_dimensions.in_unit(container_dimensions.unit)
    rotations = _ROTATIONS if allow_rotation else (_AXES,)
    for rotation in rotations:
        width, depth, height = _rotated_values(item_in_container_unit, rotation)
        if (
            _nonnegative_margin(
                _subtract(container_dimensions.width, width, "fit width margin"),
                container_dimensions.width,
                width,
            )
            and _nonnegative_margin(
                _subtract(container_dimensions.depth, depth, "fit depth margin"),
                container_dimensions.depth,
                depth,
            )
            and _nonnegative_margin(
                _subtract(container_dimensions.height, height, "fit height margin"),
                container_dimensions.height,
                height,
            )
        ):
            return FitResult("fits", "measured_dimensions_fit", rotation)
    return FitResult("does_not_fit", "measured_dimensions_do_not_fit")


def pack(
    container: Dimensions | Mapping[str, object] | None,
    items: Iterable[PackItem | Mapping[str, object]],
    *,
    allow_rotation: bool = True,
) -> PackingResult:
    """Pack measured rectangular items with deterministic 3D first-fit.

    Items are ordered by their stable IDs, independent of input iteration order.
    Each placement takes the first free box ordered by ``z, y, x`` and the first
    viable rotation in a fixed axis order.  This is intentionally a stable
    first-fit heuristic, not a claim of globally optimal packing.
    """
    container_box = _container_box_from(container)
    if container_box is None:
        return PackingResult("unknown", (), (), "container_dimensions_or_unit_unknown")
    normalised = [_pack_item_from(item) for item in items]
    ids = [item.item_id for item in normalised]
    if len(ids) != len(set(ids)):
        raise SpatialValidationError("packing item IDs must be unique")
    if any(item.dimensions is None for item in normalised):
        return PackingResult("unknown", (), tuple(sorted(ids)), "item_dimensions_or_units_unknown")
    free = [
        Box(
            container_box.x,
            container_box.y,
            container_box.z,
            container_box.width,
            container_box.height,
            container_box.depth,
            container_box.unit,
        )
    ]
    placements: list[Placement] = []
    unplaced: list[str] = []
    rotations = _ROTATIONS if allow_rotation else (_AXES,)
    for item in sorted(normalised, key=lambda candidate: candidate.item_id):
        dimensions = item.dimensions
        assert dimensions is not None  # narrowed by the unknown guard above
        dimensions = dimensions.in_unit(container_box.unit)
        chosen: tuple[int, tuple[str, str, str], tuple[float, float, float]] | None = None
        for index, space in sorted(enumerate(free), key=lambda entry: _box_sort_key(entry[1])):
            for rotation in rotations:
                width, depth, height = _rotated_values(dimensions, rotation)
                if (
                    _nonnegative_margin(
                        _subtract(space.width, width, "packing width margin"), space.width, width
                    )
                    and _nonnegative_margin(
                        _subtract(space.depth, depth, "packing depth margin"), space.depth, depth
                    )
                    and _nonnegative_margin(
                        _subtract(space.height, height, "packing height margin"), space.height, height
                    )
                ):
                    chosen = (index, rotation, (width, depth, height))
                    break
            if chosen is not None:
                break
        if chosen is None:
            unplaced.append(item.item_id)
            continue
        index, rotation, (width, depth, height) = chosen
        space = free.pop(index)
        placed = Box(space.x, space.y, space.z, width, height, depth, space.unit)
        placements.append(Placement(item.item_id, placed, rotation))
        free.extend(_split_free_box(space, placed))
    status: Literal["packed", "partial"] = "packed" if not unplaced else "partial"
    reason = "all_items_packed" if not unplaced else "one_or_more_items_do_not_fit_remaining_space"
    return PackingResult(status, tuple(placements), tuple(unplaced), reason)


def _pack_item_from(value: PackItem | Mapping[str, object]) -> PackItem:
    if isinstance(value, PackItem):
        return value
    if not isinstance(value, Mapping):
        raise SpatialValidationError("packing item must be an object")
    return PackItem(_identifier(value.get("item_id"), "item_id"), _dimensions_from(value.get("dimensions")))


def _rotated_values(
    dimensions: Dimensions, rotation: tuple[str, str, str]
) -> tuple[float, float, float]:
    values = {"width": dimensions.width, "depth": dimensions.depth, "height": dimensions.height}
    return values[rotation[0]], values[rotation[1]], values[rotation[2]]


def _box_sort_key(box: Box) -> tuple[float, float, float, float, float, float]:
    return box.z, box.y, box.x, box.height, box.depth, box.width


def _split_free_box(space: Box, placed: Box) -> tuple[Box, ...]:
    """Partition the remainder of one free box after an origin placement."""
    candidates = (
        (
            placed.max_x,
            space.y,
            space.z,
            _residual(_subtract(space.max_x, placed.max_x, "right free width"), space.width),
            space.height,
            space.depth,
        ),
        (
            space.x,
            placed.max_y,
            space.z,
            placed.width,
            space.height,
            _residual(_subtract(space.max_y, placed.max_y, "front free depth"), space.depth),
        ),
        (
            space.x,
            space.y,
            placed.max_z,
            placed.width,
            _residual(_subtract(space.max_z, placed.max_z, "above free height"), space.height),
            placed.depth,
        ),
    )
    boxes = []
    for x, y, z, width, height, depth in candidates:
        if width > 0 and height > 0 and depth > 0:
            boxes.append(Box(x, y, z, width, height, depth, space.unit))
    return tuple(boxes)


__all__ = [
    "Box",
    "Dimensions",
    "FitResult",
    "FloorPlan",
    "FloorPlanFeature",
    "PackItem",
    "PackingResult",
    "Placement",
    "Rectangle",
    "SpatialValidationError",
    "VolumeResult",
    "contains",
    "fit",
    "free_volume",
    "normalize_spatial_profile",
    "pack",
    "parse_geojson_floor_plan",
]
