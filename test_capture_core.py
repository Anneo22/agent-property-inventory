"""Focused regression tests for the pure Batch 7 capture core."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from property_inventory.capture import (
    MAX_CAPTURE_PIXELS,
    MAX_CAPTURE_SOURCE_BYTES,
    CaptureDependencyError,
    CaptureError,
    CaptureObservation,
    CaptureSegment,
    ImageRegion,
    OverviewCaptureSession,
    SourceManifest,
    _is_unknown,
    generate_crop,
    make_source_manifest,
    normalize_observation,
    run_capture_benchmark,
)
from property_inventory.capture_adapters import (
    MAX_ADAPTER_CONFIG_BYTES,
    MAX_ADAPTER_TIMEOUT_SECONDS,
    AdapterError,
    AdapterRegistry,
    _parse_response,
    load_adapter_registry,
    run_local_adapter,
)
from property_inventory.capture_provenance import (
    CaptureProvenanceError,
    canonical_artifact_bytes,
    decode_json_bytes,
    validate_capture_artifact,
    validate_capture_review,
)
from property_inventory.capture_service import (
    MAX_DUPLICATE_CANDIDATES_PER_OBSERVATION,
    CaptureServiceError,
    _identifier_strings,
    _item_subjects,
    _known_capture_signal,
    _observation_subject,
    _read_regular_source,
    prepare_capture,
    run_synthetic_capture_benchmark,
)
from property_inventory.duplicates import (
    DuplicateError,
    DuplicateSubject,
    rank_duplicate_candidates,
)

FIXTURES = Path(__file__).resolve().parent / "test_fixtures" / "capture"


class _FakeCrop:
    def __init__(self, box: tuple[int, int, int, int]) -> None:
        self.box = box

    def save(self, output: object, *, format: str, optimize: bool) -> None:
        assert format == "PNG"
        assert optimize is False
        output.write(b"fake-png:" + repr(self.box).encode("ascii"))


class _FakeImage:
    size = (10, 8)

    def crop(self, box: tuple[int, int, int, int]) -> _FakeCrop:
        return _FakeCrop(box)

    def close(self) -> None:
        return None


class CaptureCoreTests(unittest.TestCase):
    def source(self, *, width: int = 10, height: int = 8):
        data = b"source-overview-bytes"
        return data, make_source_manifest(
            source_id="overview-1",
            data=data,
            content_type="image/jpeg",
            image_width=width,
            image_height=height,
        )

    def request(self, *, width: int = 10, height: int = 8) -> dict[str, object]:
        _, source = self.source(width=width, height=height)
        return {"protocol_version": 1, "source": source.to_dict()}

    def registry(self, filename: str, *configured_arguments: str) -> AdapterRegistry:
        return AdapterRegistry(
            {"fixture": (sys.executable, str(FIXTURES / filename), *configured_arguments)},
            {"fixture": f"test:{filename}:1"},
        )

    def adapter(self, filename: str, *configured_arguments: str, timeout: float = 1.0):
        return run_local_adapter(
            adapter_name="fixture",
            registry=self.registry(filename, *configured_arguments),
            request=self.request(),
            source_data=self.source()[0],
            timeout_seconds=timeout,
        )

    def test_out_of_bounds_and_malformed_segments_are_rejected(self) -> None:
        _, source = self.source()
        with self.assertRaisesRegex(CaptureError, "outside overview"):
            OverviewCaptureSession(
                session_id="capture-1",
                captured_on="2026-08-06",
                source=source,
                segments=(CaptureSegment("out", ImageRegion(8, 0, 3, 1)),),
            )
        with self.assertRaisesRegex(CaptureError, "must be a non-empty tuple"):
            OverviewCaptureSession(
                session_id="capture-1",
                captured_on="2026-08-06",
                source=source,
                segments=[CaptureSegment("in", ImageRegion(0, 0, 1, 1))],  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(CaptureError, "exactly x, y, width, and height"):
            ImageRegion.from_mapping({"x": 0, "y": 0, "width": 1, "height": 1, "extra": 1})

    def test_injected_image_loader_crop_is_deterministic_and_binds_its_manifest(self) -> None:
        data, source = self.source()
        segment = CaptureSegment("camera", ImageRegion(1, 2, 4, 3))
        first = generate_crop(source=source, source_data=data, segment=segment, image_loader=lambda _: _FakeImage())
        second = generate_crop(source=source, source_data=data, segment=segment, image_loader=lambda _: _FakeImage())
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.manifest, second.manifest)
        with self.assertRaisesRegex(CaptureError, "must bind source digest"):
            replace(first.manifest, crop_id="crop-not-bound")
        with self.assertRaises(FrozenInstanceError):
            first.manifest.crop_id = "cannot-change"  # type: ignore[misc]

    def test_real_pillow_crop_is_deterministic_when_dependency_is_installed(self) -> None:
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (4, 4), color=(10, 20, 30)).save(output, format="PNG")
        data = output.getvalue()
        source = make_source_manifest(
            source_id="pillow-overview",
            data=data,
            content_type="image/png",
            image_width=4,
            image_height=4,
        )
        segment = CaptureSegment("centre", ImageRegion(1, 1, 2, 2))
        self.assertEqual(
            generate_crop(source=source, source_data=data, segment=segment),
            generate_crop(source=source, source_data=data, segment=segment),
        )

    def test_crop_requires_pillow_only_when_actual_image_decoding_is_requested(self) -> None:
        data, source = self.source()
        real_import = __import__

        def import_without_pillow(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError("simulated missing Pillow")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_pillow), self.assertRaises(
            CaptureDependencyError
        ):
            generate_crop(
                source=source,
                source_data=data,
                segment=CaptureSegment("camera", ImageRegion(1, 2, 4, 3)),
            )

    def test_capture_resource_limits_are_schema_invariants(self) -> None:
        with self.assertRaisesRegex(CaptureError, "byte limit"):
            SourceManifest(
                source_id="too-large",
                sha256="0" * 64,
                byte_length=MAX_CAPTURE_SOURCE_BYTES + 1,
                content_type="image/png",
                image_width=1,
                image_height=1,
            )

    def test_source_reader_rejects_symlinks_and_stops_at_the_byte_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.png"
            source.write_bytes(b"123456789")
            linked = root / "linked.png"
            linked.symlink_to(source)
            with self.assertRaisesRegex(CaptureServiceError, "non-symlink"):
                _read_regular_source(linked)
            with patch("property_inventory.capture_service.MAX_CAPTURE_SOURCE_BYTES", 8):
                with self.assertRaisesRegex(CaptureServiceError, "byte limit"):
                    _read_regular_source(source)
            fifo = root / "blocking-fifo"
            os.mkfifo(fifo)
            started = time.monotonic()
            with self.assertRaisesRegex(CaptureServiceError, "regular non-symlink file"):
                _read_regular_source(fifo)
            self.assertLess(time.monotonic() - started, 0.5)
        with self.assertRaisesRegex(CaptureError, "pixel limit"):
            SourceManifest(
                source_id="too-many-pixels",
                sha256="0" * 64,
                byte_length=1,
                content_type="image/png",
                image_width=MAX_CAPTURE_PIXELS + 1,
                image_height=1,
            )

    def test_named_adapter_configuration_is_exact_and_deeply_frozen(self) -> None:
        configured = {"fixture": (sys.executable, str(FIXTURES / "valid_adapter.py"))}
        revisions = {"fixture": "test:valid-adapter:1"}
        registry = AdapterRegistry(configured, revisions)
        configured["fixture"] = (sys.executable, str(FIXTURES / "nonzero_adapter.py"))
        revisions["fixture"] = "test:valid-adapter:2"
        response = run_local_adapter(
            adapter_name="fixture",
            registry=registry,
            request=self.request(),
            source_data=self.source()[0],
            timeout_seconds=1,
        )
        self.assertEqual(response.observations[0].observation_type, "ocr")
        self.assertEqual(
            registry.identity_for("fixture")["revision"], "test:valid-adapter:1"
        )
        with self.assertRaisesRegex(AdapterError, "exactly match"):
            AdapterRegistry(configured, {})
        with self.assertRaisesRegex(AdapterError, "immutable argv tuple"):
            AdapterRegistry({"fixture": [sys.executable]}, revisions)
        with self.assertRaisesRegex(AdapterError, "absolute configured path"):
            AdapterRegistry(
                {"fixture": ("python", str(FIXTURES / "valid_adapter.py"))},
                revisions,
            )
        with self.assertRaisesRegex(AdapterError, "must not use interpreter -c"):
            AdapterRegistry(
                {"fixture": (sys.executable, "-c", "print(1)")}, revisions
            )
        with self.assertRaisesRegex(AdapterError, "not configured"):
            run_local_adapter(
                adapter_name="missing",
                registry=registry,
                request=self.request(),
                source_data=self.source()[0],
                timeout_seconds=1,
            )
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / "adapters.json"
            config.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "adapters": {
                            "fixture": {
                                "command": [
                                    sys.executable,
                                    str(FIXTURES / "valid_adapter.py"),
                                ],
                                "revision": "test:valid-adapter:1",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_adapter_registry(config)
            self.assertEqual(loaded.command_for("fixture"), registry.command_for("fixture"))
            malformed_documents = (
                {"version": 2.0, "adapters": {}},
                {
                    "version": 2,
                    "adapters": {
                        "\ud800": {"command": [sys.executable], "revision": "valid"}
                    },
                },
                {
                    "version": 2,
                    "adapters": {
                        "fixture": {"command": ["/bin/\ud800"], "revision": "valid"}
                    },
                },
                {
                    "version": 2,
                    "adapters": {
                        "fixture": {"command": [sys.executable], "revision": "\ud800"}
                    },
                },
            )
            for malformed_document in malformed_documents:
                with self.subTest(document=repr(malformed_document)):
                    config.write_text(
                        json.dumps(malformed_document), encoding="utf-8"
                    )
                    with self.assertRaises(AdapterError):
                        load_adapter_registry(config)
            config.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "adapters": {
                            "fixture": {
                                "command": [
                                    sys.executable,
                                    str(FIXTURES / "valid_adapter.py"),
                                ],
                                "revision": "test:valid-adapter:1",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            linked = Path(temporary_directory) / "linked.json"
            linked.symlink_to(config)
            with self.assertRaisesRegex(AdapterError, "non-symlink"):
                load_adapter_registry(linked)
            config.write_bytes(b"x" * (MAX_ADAPTER_CONFIG_BYTES + 1))
            with self.assertRaisesRegex(AdapterError, "byte limit"):
                load_adapter_registry(config)

            original = Path(temporary_directory) / "original.json"
            replacement = Path(temporary_directory) / "replacement.json"
            config.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "adapters": {
                            "fixture": {
                                "command": [sys.executable],
                                "revision": "test:fixture:1",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            replacement.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "adapters": {
                            "other": {
                                "command": ["/bin/false"],
                                "revision": "test:other:1",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            real_read = os.read
            swapped = False

            def swapping_read(descriptor: int, byte_count: int) -> bytes:
                nonlocal swapped
                result = real_read(descriptor, byte_count)
                if not swapped:
                    config.rename(original)
                    replacement.rename(config)
                    swapped = True
                return result

            with patch(
                "property_inventory.capture_adapters.os.read", side_effect=swapping_read
            ), self.assertRaisesRegex(AdapterError, "changed while it was read"):
                load_adapter_registry(config)

    def test_adapter_receives_only_runtime_controlled_exact_source_file(self) -> None:
        request = self.request()
        source = request["source"]
        assert isinstance(source, dict)
        source["image_file"] = "/caller/chosen/image.png"
        with self.assertRaisesRegex(AdapterError, "controlled by the capture runtime"):
            run_local_adapter(
                adapter_name="fixture",
                registry=self.registry("valid_adapter.py"),
                request=request,
                source_data=self.source()[0],
            )
        with self.assertRaisesRegex(AdapterError, "do not match"):
            run_local_adapter(
                adapter_name="fixture",
                registry=self.registry("valid_adapter.py"),
                request=self.request(),
                source_data=b"different-overview-bytes",
            )

    def test_adapter_uses_minimal_environment_and_neutral_cwd(self) -> None:
        with patch.dict(os.environ, {"INVENTORY_ADAPTER_SECRET": "leak-me"}):
            response = self.adapter("environment_adapter.py")
        payload = response.observations[0].to_dict()["payload"]
        self.assertEqual(payload["text"], "absent")
        self.assertIn("property-inventory-capture-adapter-", payload["cwd"])
        self.assertNotEqual(payload["cwd"], str(Path.cwd()))

    def test_adapter_rejects_timeout_and_kills_the_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker = str(Path(temporary_directory) / "orphan.txt")
            with self.assertRaisesRegex(AdapterError, "adapter timed out"):
                self.adapter("tree_timeout_adapter.py", marker, timeout=0.05)
            time.sleep(0.5)
            self.assertFalse(Path(marker).exists(), "timeout left an orphan child process")
        with self.assertRaisesRegex(AdapterError, "adapter timed out"):
            self.adapter("timeout_adapter.py", timeout=0.01)

    def test_non_finite_timeout_never_launches_an_adapter(self) -> None:
        registry = self.registry("valid_adapter.py")
        with patch("property_inventory.capture_adapters.subprocess.Popen") as launched:
            for timeout in (
                float("nan"),
                float("inf"),
                float("-inf"),
                MAX_ADAPTER_TIMEOUT_SECONDS + 1,
            ):
                with self.subTest(timeout=timeout), self.assertRaisesRegex(
                    AdapterError, "no more than 60 seconds"
                ):
                    run_local_adapter(
                        adapter_name="fixture",
                        registry=registry,
                        request=self.request(),
                        source_data=self.source()[0],
                        timeout_seconds=timeout,
                    )
        launched.assert_not_called()

    def test_adapter_sanitizes_execution_and_strict_response_failures(self) -> None:
        for filename, message in (
            ("nonzero_adapter.py", "adapter execution failed"),
            ("malformed_json_adapter.py", "adapter returned malformed JSON"),
            ("nan_adapter.py", "adapter returned malformed JSON"),
            ("duplicate_key_adapter.py", "adapter returned malformed JSON"),
            ("bool_protocol_adapter.py", "adapter returned an invalid response schema"),
            ("malformed_schema_adapter.py", "adapter returned an invalid response schema"),
            ("invalid_observation_adapter.py", "adapter returned an invalid response schema"),
            ("out_of_bounds_adapter.py", "adapter returned an invalid response schema"),
            ("nested_adapter.py", "adapter response exceeds safety limits"),
            ("oversized_adapter.py", "adapter response exceeds safety limits"),
        ):
            with self.subTest(filename=filename), self.assertRaisesRegex(AdapterError, message):
                self.adapter(filename)

    def test_adapter_accepts_bounded_ocr_and_barcode_schema(self) -> None:
        response = self.adapter("valid_adapter.py")
        self.assertEqual([item.observation_type for item in response.observations], ["ocr", "barcode"])
        self.assertEqual(response.observations[0].confidence, 0.8)
        self.assertEqual(response.observations[1].to_dict()["payload"]["value"], "123456789")
        self.assertEqual(
            [segment.segment_id for segment in response.predicted_segments],
            ["detected-object"],
        )

    def test_predicted_segment_protocol_is_optional_bounded_and_exact(self) -> None:
        def parse(predicted_segments: object, *, include: bool = True):
            response: dict[str, object] = {
                "protocol_version": 1,
                "observations": [],
            }
            if include:
                response["predicted_segments"] = predicted_segments
            return _parse_response(
                json.dumps(response).encode("utf-8"),
                image_width=10,
                image_height=8,
            )

        self.assertEqual(parse([], include=False).predicted_segments, ())
        self.assertEqual(parse([]).predicted_segments, ())
        with self.assertRaisesRegex(AdapterError, "invalid response schema"):
            parse([{"segment_id": "missing-region"}])
        with self.assertRaisesRegex(AdapterError, "invalid response schema"):
            parse(
                [
                    {
                        "segment_id": "duplicate",
                        "region": {"x": 0, "y": 0, "width": 1, "height": 1},
                    },
                    {
                        "segment_id": "duplicate",
                        "region": {"x": 1, "y": 0, "width": 1, "height": 1},
                    },
                ]
            )
        with self.assertRaisesRegex(AdapterError, "invalid response schema"):
            parse(
                [
                    {
                        "segment_id": "outside",
                        "region": {"x": 9, "y": 0, "width": 2, "height": 1},
                    }
                ]
            )
        with self.assertRaisesRegex(AdapterError, "invalid response schema"):
            parse(
                [
                    {
                        "segment_id": f"segment-{index}",
                        "region": {"x": 0, "y": 0, "width": 1, "height": 1},
                    }
                    for index in range(257)
                ]
            )
        with self.assertRaisesRegex(AdapterError, "invalid response schema"):
            parse(
                [
                    {
                        "segment_id": f"full-image-{index}",
                        "region": {"x": 0, "y": 0, "width": 10, "height": 8},
                    }
                    for index in range(5)
                ]
            )

    def test_normalization_rejects_bad_types_and_preserves_unknowns_immutably(self) -> None:
        raw_payload = {"text": "unknown", "serial": None, "future": {"value": "unknown"}}
        normalized = normalize_observation(
            {
                "type": "ocr",
                "region": {"x": 1, "y": 2, "width": 3, "height": 4},
                "confidence": None,
                "payload": raw_payload,
            }
        )
        raw_payload["future"]["value"] = "mutated"
        self.assertEqual(normalized.to_dict()["payload"]["future"]["value"], "unknown")
        with self.assertRaisesRegex(CaptureError, "string or null"):
            normalize_observation(
                {
                    "type": "barcode",
                    "region": {"x": 0, "y": 0, "width": 1, "height": 1},
                    "confidence": 0.2,
                    "payload": {"value": 123},
                }
            )
        with self.assertRaisesRegex(CaptureError, "string or null"):
            normalize_observation(
                {
                    "type": "ocr",
                    "region": {"x": 0, "y": 0, "width": 1, "height": 1},
                    "confidence": 0.2,
                    "payload": {"text": 123},
                }
            )
        with self.assertRaisesRegex(CaptureError, "model_identifier"):
            normalize_observation(
                {
                    "type": "ocr",
                    "region": {"x": 0, "y": 0, "width": 1, "height": 1},
                    "confidence": 0.2,
                    "payload": {"text": "label", "model_identifier": 123},
                }
            )
        with self.assertRaisesRegex(CaptureError, "finite"):
            CaptureObservation("ocr", ImageRegion(0, 0, 1, 1), float("nan"), {"text": "x"})

    def test_duplicate_ranking_is_deterministic_and_contains_no_write_fields(self) -> None:
        observation = DuplicateSubject(
            identifier="observed", serials=("SN-44",), aliases=("camera body",), display_text="black camera"
        )
        ranking = rank_duplicate_candidates(
            observation=observation,
            candidates=(
                DuplicateSubject(identifier="z-alias", aliases=("camera body",), display_text="camera"),
                DuplicateSubject(identifier="a-serial", serials=("sn44",), display_text="different"),
                DuplicateSubject(identifier="b-token", display_text="black camera"),
            ),
        )
        self.assertEqual([candidate.candidate_id for candidate in ranking.candidates], ["a-serial", "z-alias", "b-token"])
        self.assertEqual(set(ranking.__dataclass_fields__), {"candidates", "match_count"})
        self.assertEqual(set(ranking.candidates[0].__dataclass_fields__), {"candidate_id", "score", "evidence"})
        with self.assertRaisesRegex(DuplicateError, "exact tuple"):
            DuplicateSubject(identifier="bad", serials=["SN-1"])  # type: ignore[arg-type]
        with self.assertRaisesRegex(DuplicateError, "alphanumeric"):
            DuplicateSubject(identifier="bad", barcodes=("---",))

    def test_perceptual_hash_distance_is_bit_level(self) -> None:
        ranking = rank_duplicate_candidates(
            observation=DuplicateSubject(identifier="observation", perceptual_hash="0f"),
            candidates=(DuplicateSubject(identifier="candidate", perceptual_hash="00"),),
        )
        evidence = ranking.candidates[0].evidence
        self.assertEqual(evidence[0].kind, "perceptual_hash_candidate")
        self.assertEqual(evidence[0].detail, "4")
        self.assertEqual(evidence[0].score, 16.0)

    def test_duplicate_subjects_use_canonical_serials_and_identifiers_and_observed_serials(self) -> None:
        class Store:
            rows = {
                "aliases": [],
                "items": [
                    {
                        "item_id": "item-camera",
                        "model_id": "model-camera",
                        "serial_or_lot": "ITEM-SERIAL-7",
                        "name": "Camera body",
                        "sensitivity": "low",
                    }
                ],
            }

            @staticmethod
            def get(table: str, record_id: str) -> dict[str, object]:
                assert (table, record_id) == ("models", "model-camera")
                return {
                    "name": "Camera body",
                    "brand": "Unrelated Brand",
                    "model": "Not-An-Identifier",
                    "identifiers_json": json.dumps(
                        {
                            "manufacturer_code": "MODEL-ID-9",
                            "nested": [
                                "ALT-ID-2",
                                {"identifiers": [{"ean": "0123456789012"}]},
                            ],
                        }
                    ),
                }

        candidate = _item_subjects(Store(), lambda _: True)[0]
        self.assertEqual(candidate.serials, ("ITEM-SERIAL-7",))
        self.assertEqual(
            candidate.model_identifiers,
            ("MODEL-ID-9", "ALT-ID-2", "0123456789012"),
        )
        self.assertEqual(candidate.barcodes, ("0123456789012",))
        self.assertNotIn("Camera body", candidate.model_identifiers)
        self.assertNotIn("Not-An-Identifier", candidate.model_identifiers)
        self.assertEqual(_known_capture_signal(" unknown "), ())
        self.assertEqual(_identifier_strings(" Unknown "), ())
        self.assertTrue(_is_unknown("  UNKNOWN  "))
        ocr = _observation_subject(
            "ocr", {"type": "ocr", "payload": {"text": "Camera label", "serial": "OCR-SERIAL-8"}}
        )
        barcode = _observation_subject(
            "barcode", {"type": "barcode", "payload": {"value": "0123456789012"}}
        )
        self.assertEqual(ocr.serials, ("OCR-SERIAL-8",))
        self.assertEqual(barcode.barcodes, ("0123456789012",))
        barcode_match = rank_duplicate_candidates(observation=barcode, candidates=(candidate,))
        self.assertEqual(barcode_match.candidates[0].evidence[0].kind, "exact_barcode")
        ranking = rank_duplicate_candidates(observation=ocr, candidates=(candidate,))
        self.assertEqual(ranking.candidates[0].evidence[0].kind, "token_overlap")
        self.assertNotIn(
            "exact_serial", [evidence.kind for evidence in ranking.candidates[0].evidence]
        )
        serial_match = rank_duplicate_candidates(
            observation=_observation_subject(
                "ocr-match",
                {"type": "ocr", "payload": {"text": "Different label", "serial": "ITEM-SERIAL-7"}},
            ),
            candidates=(candidate,),
        )
        self.assertEqual(serial_match.candidates[0].evidence[0].kind, "exact_serial")

    def test_production_candidate_output_is_top_k_with_an_honest_total(self) -> None:
        from PIL import Image

        class Store:
            rows = {
                "aliases": [],
                "items": [
                    {
                        "item_id": f"item-{index:02d}",
                        "model_id": f"model-{index:02d}",
                        "serial_or_lot": None,
                        "name": f"Model AB-1 candidate {index:02d}",
                        "sensitivity": "low",
                    }
                    for index in range(20)
                ],
            }

            @staticmethod
            def get(table: str, record_id: str) -> dict[str, object]:
                assert table == "models"
                return {
                    "name": f"Model AB-1 {record_id}",
                    "brand": None,
                    "model": None,
                    "identifiers_json": "{}",
                }

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = root / "runtime"
            runtime.mkdir()
            overview = root / "overview.png"
            Image.new("RGB", (12, 9), (1, 2, 3)).save(overview)
            artifact = prepare_capture(
                runtime_dir=runtime,
                overview=overview,
                captured_on="2026-08-06",
                segments_value=[
                    {
                        "segment_id": "label",
                        "region": {"x": 0, "y": 0, "width": 12, "height": 9},
                    }
                ],
                evidence_id=None,
                source_ref="bounded ranking fixture",
                evidence_type="user_source",
                sensitivity="low",
                adapter_name="fixture",
                adapter_registry=self.registry("valid_adapter.py"),
                timeout_seconds=1,
                store=Store(),
                base_digest="0" * 64,
                visible=lambda _: True,
            )
        candidates = artifact["duplicate_candidates"]["observation-1"]
        summary = artifact["duplicate_candidate_summaries"]["observation-1"]
        self.assertEqual(len(candidates), MAX_DUPLICATE_CANDIDATES_PER_OBSERVATION)
        self.assertEqual(summary["match_count"], 20)
        self.assertEqual(summary["returned_count"], 5)
        self.assertTrue(summary["truncated"])

        validate_capture_artifact(artifact)
        review = {
            "format": 1,
            "capture_session_id": artifact["capture_session_id"],
            "artifact_sha256": hashlib.sha256(
                canonical_artifact_bytes(artifact)
            ).hexdigest(),
            "base_digest": artifact["base_digest"],
            "created_at": "2026-08-06T00:00:00+00:00",
            "proposal_id": "proposal-00000000-0000-0000-0000-000000000001",
            "links": {},
            "manual_observations": {},
            "decisions": [],
        }
        validate_capture_review(review, artifact=artifact)
        for malformed_format in (True, 1.0):
            with self.subTest(artifact_format=malformed_format):
                malformed_artifact = {**artifact, "format": malformed_format}
                with self.assertRaises(CaptureProvenanceError):
                    validate_capture_artifact(malformed_artifact)
            with self.subTest(review_format=malformed_format):
                malformed_review = {**review, "format": malformed_format}
                with self.assertRaises(CaptureProvenanceError):
                    validate_capture_review(malformed_review, artifact=artifact)
        malformed_adapter = {
            **artifact,
            "adapter": {**artifact["adapter"], "revision": "\ud800"},
        }
        with self.assertRaises(CaptureProvenanceError):
            validate_capture_artifact(malformed_adapter)
        with self.assertRaisesRegex(CaptureProvenanceError, "canonical JSON"):
            canonical_artifact_bytes({"surrogate": "\ud800"})
        with self.assertRaisesRegex(CaptureProvenanceError, "strict UTF-8 JSON"):
            decode_json_bytes(
                b"[" * 10_000 + b"0" + b"]" * 10_000,
                "deep capture artifact",
            )
        nested: object = {}
        for _ in range(1_500):
            nested = {"nested": nested}
        deep_observation = {
            **artifact["observations"][0],
            "payload": {
                **artifact["observations"][0]["payload"],
                "nested": nested,
            },
        }
        deep_artifact = {
            **artifact,
            "observations": [deep_observation, *artifact["observations"][1:]],
        }
        with self.assertRaisesRegex(CaptureProvenanceError, "depth limit"):
            validate_capture_artifact(deep_artifact)
        with self.assertRaisesRegex(CaptureError, "depth limit"):
            normalize_observation(
                {
                    "type": "ocr",
                    "region": {"x": 0, "y": 0, "width": 1, "height": 1},
                    "confidence": None,
                    "payload": {"text": "label", "nested": nested},
                },
                image_width=10,
                image_height=8,
            )

    def test_synthetic_benchmark_executes_crops_adapter_and_duplicate_ranking(self) -> None:
        cases = json.loads((FIXTURES / "synthetic-executed-benchmark.json").read_text(encoding="utf-8"))
        self.assertNotIn("segments", cases[0])
        self.assertNotIn("predicted_segments", cases[0])
        self.assertNotIn("observed_fields", cases[0])
        self.assertNotIn("ranked_duplicate_ids", cases[0])
        report = run_synthetic_capture_benchmark(
            cases=cases,
            registry=self.registry("benchmark_adapter.py"),
            adapter_name="fixture",
        )
        self.assertEqual(report.claim, "synthetic-fixture-only")
        self.assertEqual(report.segmentation_recall.value, 0.0)
        self.assertTrue(report.segmentation_recall.errors)
        self.assertEqual(report.field_exact_match.value, 1.0)
        self.assertEqual(report.barcode_exact_match.value, 1.0)
        self.assertEqual(report.duplicate_top_1.value, 1.0)
        invalid_truth = json.loads(
            (FIXTURES / "synthetic-executed-benchmark.json").read_text(
                encoding="utf-8"
            )
        )
        invalid_truth[0]["truth_segments"] = [
            {"x": 8, "y": 0, "width": 1, "height": 1}
        ]
        with self.assertRaisesRegex(CaptureServiceError, "truth_segments"):
            run_synthetic_capture_benchmark(
                cases=invalid_truth,
                registry=self.registry("benchmark_adapter.py"),
                adapter_name="fixture",
            )
        cases[0]["predicted_segments"] = []
        with self.assertRaisesRegex(CaptureServiceError, "must not provide pipeline outputs"):
            run_synthetic_capture_benchmark(
                cases=cases,
                registry=self.registry("benchmark_adapter.py"),
                adapter_name="fixture",
            )

    def test_benchmark_uses_maximum_cardinality_matching_and_counts_abstentions(self) -> None:
        report = run_capture_benchmark(
            corpus_label="synthetic",
            top_k=2,
            cases=[
                {
                    "case_id": "fixture-1",
                    "truth_segments": [
                        {"x": 0, "y": 0, "width": 10, "height": 10},
                        {"x": 6, "y": 0, "width": 10, "height": 10},
                    ],
                    "predicted_segments": [
                        {"x": 3, "y": 0, "width": 10, "height": 10},
                        {"x": 0, "y": 0, "width": 10, "height": 10},
                    ],
                    "expected_fields": {"model": "AB-1"},
                    "observed_fields": {"model": "unknown"},
                    "expected_barcode": "123",
                    "observed_barcode": None,
                    "expected_duplicate_id": "item-1",
                    "ranked_duplicate_ids": "unknown",
                }
            ],
        )
        self.assertEqual(report.claim, "synthetic-fixture-only")
        self.assertEqual(report.segmentation_recall.value, 1.0)
        self.assertEqual(report.field_exact_match.denominator, 1)
        self.assertEqual(report.field_exact_match.correct, 0)
        self.assertEqual(report.field_exact_match.abstentions, 1)
        self.assertEqual(report.barcode_exact_match.abstentions, 1)
        self.assertEqual(report.duplicate_top_1.abstentions, 1)
        with self.assertRaisesRegex(CaptureError, "must contain at least one case"):
            run_capture_benchmark(corpus_label="synthetic", cases=[])
        with self.assertRaisesRegex(CaptureError, "manually_checked"):
            run_capture_benchmark(corpus_label="real-room", cases=[{}])
        real_room = run_capture_benchmark(
            corpus_label="real-room", provenance={"manually_checked": True}, cases=[{}]
        )
        self.assertEqual(real_room.claim, "real-room-sample-not-statistical-proof")


if __name__ == "__main__":
    unittest.main()
