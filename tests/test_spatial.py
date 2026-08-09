"""Focused tests for the pure spatial reasoning core."""

from __future__ import annotations

import math
import unittest
from itertools import combinations

from property_inventory.spatial import (
    Box,
    Dimensions,
    PackItem,
    Rectangle,
    SpatialValidationError,
    _dimensions_from,
    contains,
    fit,
    free_volume,
    pack,
    parse_geojson_floor_plan,
)


def feature(feature_id: str, ring: list[list[float]], unit: str = "cm") -> dict[str, object]:
    return {
        "type": "Feature",
        "id": feature_id,
        "properties": {"unit": unit, "evidence_id": f"ev-{feature_id}"},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


class SpatialGeometryTests(unittest.TestCase):
    def test_geojson_parser_canonicalises_feature_order(self) -> None:
        second = feature("b", [[10, 0], [20, 0], [20, 5], [10, 5], [10, 0]])
        first = feature("a", [[0, 0], [0, 5], [10, 5], [10, 0], [0, 0]])
        plan = parse_geojson_floor_plan({"type": "FeatureCollection", "features": [second, first]})
        self.assertEqual(plan.unit, "cm")
        self.assertEqual([entry.feature_id for entry in plan.features], ["a", "b"])
        self.assertEqual(plan.features[0].rectangle, Rectangle(0, 0, 10, 5, "cm"))

    def test_geojson_rejects_non_rectilinear_self_crossing_and_bad_metadata(self) -> None:
        invalid_documents = [
            {"type": "FeatureCollection", "features": [feature("a", [[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]])]},
            {"type": "FeatureCollection", "features": [feature("a", [[0, 0], [2, 0], [1, 1], [0, 2], [0, 0]])]},
            {"type": "FeatureCollection", "features": [feature("a", [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]), feature("b", [[3, 0], [4, 0], [4, 1], [3, 1], [3, 0]], "m")]},
        ]
        for document in invalid_documents:
            with self.subTest(document=document):
                with self.assertRaises(SpatialValidationError):
                    parse_geojson_floor_plan(document)
        missing_evidence = feature("a", [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]])
        missing_evidence["properties"] = {"unit": "cm"}
        with self.assertRaises(SpatialValidationError):
            parse_geojson_floor_plan({"type": "FeatureCollection", "features": [missing_evidence]})
        non_finite = feature("a", [[0, 0], [math.inf, 0], [math.inf, 2], [0, 2], [0, 0]])
        with self.assertRaises(SpatialValidationError):
            parse_geojson_floor_plan({"type": "FeatureCollection", "features": [non_finite]})

    def test_containment_requires_equivalent_shapes_and_units(self) -> None:
        self.assertTrue(contains(Rectangle(0, 0, 10, 10, "cm"), Rectangle(1, 1, 8, 8, "cm")))
        self.assertFalse(contains(Box(0, 0, 0, 10, 10, 10, "cm"), Box(0, 0, 0, 11, 10, 10, "cm")))
        with self.assertRaises(SpatialValidationError):
            contains(Rectangle(0, 0, 1, 1, "cm"), Rectangle(0, 0, 1, 1, "m"))

    def test_decimal_boundaries_are_contained_without_oversized_slivers(self) -> None:
        container = Rectangle(0, 0, 0.3, 1, "m")
        subject = Rectangle(0.1, 0, 0.2, 1, "m")
        self.assertTrue(contains(container, subject))
        self.assertEqual(
            fit(Dimensions(0.1 + 0.2, 1, 1, "m"), Dimensions(0.3, 1, 1, "m")).status,
            "fits",
        )

    def test_large_coordinate_offsets_do_not_relax_bounds_or_overlap(self) -> None:
        offset = 1_000_000_000_000.0
        container = Box(offset, offset, offset, 1, 1, 1, "m")
        outside = Box(offset + 1, offset, offset, 0.01, 1, 1, "m")
        self.assertFalse(contains(container, outside))
        with self.assertRaisesRegex(SpatialValidationError, "outside"):
            free_volume(container, [outside])
        first = Box(offset, offset, offset, 0.75, 1, 1, "m")
        second = Box(offset + 0.5, offset, offset, 0.5, 1, 1, "m")
        with self.assertRaisesRegex(SpatialValidationError, "overlap"):
            free_volume(container, [first, second])


class SpatialReasoningTests(unittest.TestCase):
    def test_missing_dimensions_are_unknown_not_negative(self) -> None:
        self.assertIsNone(_dimensions_from({"width": 10, "height": 10, "depth": 10}))
        self.assertEqual(fit({"width": 10}, Dimensions(10, 10, 10, "cm")).status, "unknown")
        self.assertEqual(free_volume(None, []).status, "unknown")
        result = pack(Dimensions(10, 10, 10, "cm"), [PackItem("item", None)])
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.unplaced_item_ids, ("item",))

    def test_partial_input_validates_every_supplied_value_before_unknown(self) -> None:
        for corrupt in (
            {"width": "bad", "height": None, "depth": 1, "unit": "cm"},
            {"width": 1, "height": None, "depth": 1, "unit": "yards"},
            {"width": 1, "height": None, "depth": 1, "unit": "cm", "evidence_id": 3},
        ):
            with self.subTest(corrupt=corrupt):
                with self.assertRaises(SpatialValidationError):
                    _dimensions_from(corrupt)

    def test_fit_converts_known_units_and_selects_deterministic_rotation(self) -> None:
        result = fit(Dimensions(4, 2, 3, "cm"), Dimensions(3, 4, 2, "cm"))
        self.assertEqual(result.status, "fits")
        self.assertEqual(result.rotation, ("depth", "height", "width"))
        converted = fit(Dimensions(0.2, 0.1, 0.1, "m"), Dimensions(20, 10, 10, "cm"))
        self.assertEqual(converted.status, "fits")
        self.assertEqual(fit(Dimensions(4, 4, 4, "cm"), Dimensions(3, 3, 3, "cm")).status, "does_not_fit")

    def test_free_volume_requires_positioned_checked_non_overlapping_boxes(self) -> None:
        container = Box(0, 0, 0, 0.3, 1, 1, "m")
        boxes = [Box(0, 0, 0, 0.1, 1, 1, "m"), Box(0.1, 0, 0, 0.2, 1, 1, "m")]
        result = free_volume(container, boxes)
        self.assertEqual((result.status, result.volume, result.unit), ("known", 0.0, "m"))
        self.assertEqual(free_volume(container, [Dimensions(0.1, 1, 1, "m")]).status, "unknown")
        with self.assertRaisesRegex(SpatialValidationError, "outside"):
            free_volume(container, [Box(0.2, 0, 0, 0.2, 1, 1, "m")])
        with self.assertRaisesRegex(SpatialValidationError, "overlap"):
            free_volume(container, [Box(0, 0, 0, 0.2, 1, 1, "m"), Box(0.1, 0, 0, 0.2, 1, 1, "m")])
        with self.assertRaises(SpatialValidationError):
            free_volume(container, [{"x": "bad"}])

    def test_overflowing_derived_geometry_is_rejected(self) -> None:
        with self.assertRaises(SpatialValidationError):
            Dimensions(1e308, 1e308, 1e308, "m")
        conversion_source = Dimensions(1e308, 1e-308, 1e-308, "m")
        with self.assertRaises(SpatialValidationError):
            conversion_source.in_unit("mm")
        with self.assertRaises(SpatialValidationError):
            Box(1e308, 0, 0, 1e308, 1e-308, 1e-308, "m")
        with self.assertRaises(SpatialValidationError):
            Dimensions(10**400, 1, 1, "m")
        with self.assertRaisesRegex(SpatialValidationError, "underflows"):
            Dimensions(1e-308, 1e-308, 1e-308, "m")

    def test_packing_is_stable_and_reports_partial(self) -> None:
        container = Dimensions(4, 1, 1, "cm")
        items = [PackItem("b", Dimensions(3, 1, 1, "cm")), PackItem("a", Dimensions(2, 1, 1, "cm"))]
        first = pack(container, items)
        second = pack(container, list(reversed(items)))
        self.assertEqual(first, second)
        self.assertEqual(first.status, "partial")
        self.assertEqual([placement.item_id for placement in first.placements], ["a"])
        self.assertEqual(first.unplaced_item_ids, ("b",))

    def test_decimal_exact_fill_packs_without_residual_slivers(self) -> None:
        result = pack(
            Dimensions(0.3, 1, 1, "m"),
            [PackItem("a", Dimensions(0.1, 1, 1, "m")), PackItem("b", Dimensions(0.2, 1, 1, "m"))],
        )
        self.assertEqual(result.status, "packed")
        self.assertEqual(len(result.placements), 2)

    def test_packing_rotates_and_produces_non_overlapping_boxes(self) -> None:
        result = pack(
            Dimensions(2, 2, 1, "cm"),
            [PackItem("one", Dimensions(1, 1, 2, "cm")), PackItem("two", Dimensions(1, 1, 2, "cm"))],
        )
        self.assertEqual(result.status, "packed")
        self.assertEqual([placement.item_id for placement in result.placements], ["one", "two"])
        first, second = (placement.box for placement in result.placements)
        self.assertTrue(first.max_x <= second.x or second.max_x <= first.x or first.max_y <= second.y or second.max_y <= first.y or first.max_z <= second.z or second.max_z <= first.z)

    def test_asymmetric_packing_preserves_bounds_partitions_and_volume(self) -> None:
        dimensions = Dimensions(7, 4, 5, "cm")
        container = Box(0, 0, 0, dimensions.width, dimensions.height, dimensions.depth, dimensions.unit)
        items = [
            PackItem("gamma", Dimensions(1, 1, 4, "cm")),
            PackItem("alpha", Dimensions(3, 2, 2, "cm")),
            PackItem("delta", Dimensions(2, 1, 1, "cm")),
            PackItem("beta", Dimensions(2, 3, 2, "cm")),
        ]
        result = pack(dimensions, list(reversed(items)))
        self.assertEqual(result.status, "packed")
        placement_ids = [placement.item_id for placement in result.placements]
        self.assertEqual(placement_ids, sorted(placement_ids))
        self.assertEqual(
            sorted(placement_ids + list(result.unplaced_item_ids)),
            sorted(item.item_id for item in items),
        )
        for placement in result.placements:
            self.assertTrue(contains(container, placement.box))
        for first, second in combinations((placement.box for placement in result.placements), 2):
            self.assertTrue(
                first.max_x <= second.x
                or second.max_x <= first.x
                or first.max_y <= second.y
                or second.max_y <= first.y
                or first.max_z <= second.z
                or second.max_z <= first.z
            )
        free = free_volume(container, result.placements)
        self.assertEqual(free.status, "known")
        self.assertIsNotNone(free.volume)
        occupied = sum(placement.box.volume for placement in result.placements)
        self.assertTrue(math.isclose(occupied + free.volume, container.volume, rel_tol=1e-9, abs_tol=1e-12))

    def test_packing_preserves_a_checked_nonzero_container_origin(self) -> None:
        container = Box(10, 20, 30, 4, 5, 6, "cm")
        result = pack(
            {
                "x": container.x,
                "y": container.y,
                "z": container.z,
                "width": container.width,
                "height": container.height,
                "depth": container.depth,
                "unit": container.unit,
            },
            [
                {
                    "item_id": "measured-item",
                    "dimensions": {"width": 1, "height": 1, "depth": 1, "unit": "cm"},
                }
            ],
        )
        self.assertEqual(result.status, "packed")
        self.assertEqual(
            (result.placements[0].box.x, result.placements[0].box.y, result.placements[0].box.z),
            (10.0, 20.0, 30.0),
        )
        self.assertTrue(contains(container, result.placements[0].box))


if __name__ == "__main__":
    unittest.main()
