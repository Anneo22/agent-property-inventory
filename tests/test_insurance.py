"""Focused tests for the pure insurance-preparation core."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import unittest
import zipfile
from copy import deepcopy

from PIL import Image, PngImagePlugin
from pypdf import PdfWriter

from property_inventory import insurance as insurance_module
from property_inventory.insurance import (
    InsuranceError,
    build_insurance_package,
    insurance_report,
    load_insurance_package,
    validate_insurance_package,
)
from property_inventory.media_validation import (
    MediaValidationError,
    validate_declared_media_bytes,
)


def fixture_image_bytes(label: str = "physical-photo") -> bytes:
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("fixture", label)
    Image.new("RGB", (4, 3), (20, 30, 40)).save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


PHOTO_BYTES = fixture_image_bytes()


def fixture_pdf_bytes() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return output.getvalue()


class MediaValidationTests(unittest.TestCase):
    def test_pdf_claim_requires_a_parseable_document_not_only_markers(self) -> None:
        with self.assertRaisesRegex(MediaValidationError, "cannot be parsed"):
            validate_declared_media_bytes(b"%PDF-1.7\nnot a document\n%%EOF\n", "application/pdf")
        self.assertEqual(
            validate_declared_media_bytes(fixture_pdf_bytes(), "application/pdf"),
            "application/pdf",
        )

    def test_iso_bmff_brand_alone_does_not_validate_heic_or_avif(self) -> None:
        for brand, media_type in ((b"heic", "image/heic"), (b"avif", "image/avif")):
            forged = (16).to_bytes(4, "big") + b"ftyp" + brand + b"\x00\x00\x00\x00"
            with self.subTest(media_type=media_type):
                with self.assertRaisesRegex(MediaValidationError, "decoded and verified"):
                    validate_declared_media_bytes(forged, media_type)


def base_rows() -> dict[str, list[dict[str, object]]]:
    photo = PHOTO_BYTES
    return {
        "items": [
            {
                "item_id": "item-visible",
                "model_id": "model-visible",
                "ownership_state": "confirmed",
                "location_id": "place-visible",
                "container_id": None,
                "serial_or_lot": "SN-1",
                "acquired_on": "2025-01-02",
                "sensitivity": "low",
            },
            {
                "item_id": "item-canary-secret",
                "model_id": "model-secret",
                "ownership_state": "confirmed",
                "location_id": "place-secret",
                "container_id": None,
                "serial_or_lot": "SECRET-SERIAL",
                "acquired_on": "2025-01-02",
                "sensitivity": "high",
            },
        ],
        "models": [
            {"model_id": "model-visible", "name": "Visible camera", "category": "electronics"},
            {"model_id": "model-secret", "name": "Secret asset", "category": "private"},
        ],
        "locations": [
            {
                "location_id": "place-visible",
                "name": "Visible shelf",
                "parent_location_id": None,
                "kind": "place",
                "sensitivity": "low",
            },
            {
                "location_id": "place-secret",
                "name": "Secret safe",
                "parent_location_id": None,
                "kind": "place",
                "sensitivity": "high",
            },
        ],
        "evidence": [
            {
                "evidence_id": "ev-visible",
                "evidence_type": "physical_check",
                "claim_strength": "explicit_current",
                "sensitivity": "low",
            },
            {
                "evidence_id": "ev-value",
                "evidence_type": "research",
                "claim_strength": "research_only",
                "sensitivity": "low",
            },
            {
                "evidence_id": "ev-secret",
                "evidence_type": "physical_check",
                "claim_strength": "explicit_current",
                "sensitivity": "high",
            },
        ],
        "item_evidence": [
            {"item_id": "item-visible", "evidence_id": "ev-visible", "role": "primary"},
            {"item_id": "item-visible", "evidence_id": "ev-value", "role": "supporting"},
            {
                "item_id": "item-canary-secret",
                "evidence_id": "ev-secret",
                "role": "supporting",
            },
        ],
        "item_documents": [
            {"item_id": "item-visible", "document_type": "photo", "uri": "media://sha256/ignored"},
            {"item_id": "item-visible", "document_type": "receipt", "uri": "receipt://visible"},
            {"item_id": "item-visible", "document_type": "appraisal", "uri": "appraisal://visible"},
        ],
        "valuations": [
            {
                "valuation_id": "val-good",
                "item_id": "item-visible",
                "amount": 42,
                "currency": "GBP",
                "valued_on": "2025-02-03",
                "basis": "replacement",
                "evidence_id": "ev-value",
                "sensitivity": "low",
            },
            {
                "valuation_id": "val-unlinked",
                "item_id": "item-visible",
                "amount": 999,
                "currency": "GBP",
                "valued_on": "2025-02-03",
                "basis": "invented",
                "evidence_id": "ev-secret",
                "sensitivity": "low",
            },
        ],
        "media_assets": [
            {
                "asset_id": "asset-photo",
                "sha256": hashlib.sha256(photo).hexdigest(),
                "byte_size": len(photo),
                "media_type": "image/png",
                "sensitivity": "low",
            },
        ],
        "evidence_assets": [
            {"evidence_id": "ev-visible", "asset_id": "asset-photo", "role": "source"}
        ],
    }


class InsuranceReportTests(unittest.TestCase):
    def test_scope_filters_before_item_ids_or_counts_and_never_leaks_canary(self) -> None:
        rows = base_rows()
        rows["item_documents"][0]["uri"] = "PRIVATE-DOCUMENT-CANARY"
        report = insurance_report(rows, scope="public")
        serialized = json.dumps(report, sort_keys=True)
        self.assertEqual(report["summary"]["item_count"], 1)
        self.assertEqual([item["item_id"] for item in report["items"]], ["item-visible"])
        self.assertNotIn("secret", serialized.casefold())
        self.assertNotIn("999", serialized)
        self.assertNotIn("private-document-canary", serialized.casefold())

    def test_missing_data_is_unknown_not_a_false_or_invented_value(self) -> None:
        rows = base_rows()
        item = next(item for item in rows["items"] if item["item_id"] == "item-visible")
        item["serial_or_lot"] = None
        item["acquired_on"] = "not-a-date"
        rows["item_documents"] = []
        rows["valuations"] = []
        rows["media_assets"] = []
        rows["evidence_assets"] = []
        item["location_id"] = None
        report = insurance_report(rows, verified_media_asset_ids={"asset-photo"})
        visible = next(item for item in report["items"] if item["item_id"] == "item-visible")
        self.assertEqual(visible["readiness"], "not_ready")
        self.assertEqual(
            visible["gaps"],
            ["photo", "serial", "value", "receipt", "appraisal", "acquired_date", "location"],
        )
        self.assertEqual(visible["fields"]["serial"], {"state": "unknown"})
        self.assertNotIn(False, visible["fields"].values())

    def test_valuation_requires_visible_evidence_link_and_preserves_currency(self) -> None:
        report = insurance_report(base_rows())
        item = next(item for item in report["items"] if item["item_id"] == "item-visible")
        values = item["fields"]["value"]["valuations"]
        self.assertEqual(
            values,
            [
                {
                    "valuation_id": "val-good",
                    "amount": 42,
                    "currency": "GBP",
                    "basis": "replacement",
                    "valued_on": "2025-02-03",
                    "evidence_id": "ev-value",
                }
            ],
        )

    def test_exports_actual_serial_date_and_human_location_when_visible(self) -> None:
        item = next(
            item
            for item in insurance_report(base_rows(), scope="public")["items"]
            if item["item_id"] == "item-visible"
        )
        self.assertEqual(item["fields"]["serial"], {"state": "present", "serial_or_lot": "SN-1"})
        self.assertEqual(
            item["fields"]["acquired_date"], {"state": "present", "acquired_on": "2025-01-02"}
        )
        self.assertEqual(
            item["fields"]["location"],
            {
                "state": "present",
                "location_ids": ["place-visible"],
                "location_names": ["Visible shelf"],
            },
        )

    def test_visible_drawer_beats_its_broader_room_for_insurance_location(self) -> None:
        rows = base_rows()
        rows["locations"].append(
            {
                "location_id": "drawer-visible",
                "name": "Camera drawer",
                "parent_location_id": "place-visible",
                "kind": "container",
                "sensitivity": "low",
            }
        )
        rows["items"][0]["container_id"] = "drawer-visible"
        item = next(
            item
            for item in insurance_report(rows, scope="public")["items"]
            if item["item_id"] == "item-visible"
        )
        self.assertEqual(
            item["fields"]["location"],
            {
                "state": "present",
                "location_ids": ["drawer-visible", "place-visible"],
                "location_names": ["Camera drawer", "Visible shelf"],
            },
        )

    def test_export_preserves_nonblank_canonical_text_exactly(self) -> None:
        rows = base_rows()
        rows["items"][0]["serial_or_lot"] = "  SN-001  "
        rows["locations"][0]["name"] = "  Main shelf  "
        item = next(
            item
            for item in insurance_report(rows, scope="public")["items"]
            if item["item_id"] == "item-visible"
        )
        self.assertEqual(item["fields"]["serial"]["serial_or_lot"], "  SN-001  ")
        self.assertEqual(item["fields"]["location"]["location_names"], ["  Main shelf  "])

    def test_private_evidence_or_place_does_not_be_reported_missing_in_a_lower_scope(self) -> None:
        rows = base_rows()
        rows["items"][0]["location_id"] = "place-secret"
        rows["item_evidence"] = [
            {"item_id": "item-visible", "evidence_id": "ev-secret", "role": "supporting"}
        ]
        report = insurance_report(rows, scope="public")
        item = report["items"][0]
        self.assertEqual(item["fields"]["location"], {"state": "unknown"})
        self.assertEqual(item["fields"]["evidence_quality"], {"state": "unknown"})
        self.assertNotIn("ev-secret", json.dumps(report))

    def test_document_type_or_blank_uri_is_not_insurance_evidence(self) -> None:
        rows = base_rows()
        rows["media_assets"] = []
        rows["evidence_assets"] = []
        rows["item_documents"] = [
            {"item_id": "item-visible", "document_type": "photo", "uri": ""},
            {"item_id": "item-visible", "document_type": "receipt", "uri": "   "},
            {"item_id": "item-visible", "document_type": "appraisal"},
        ]
        fields = insurance_report(rows)["items"][0]["fields"]
        self.assertEqual(fields["photo"], {"state": "unknown"})
        self.assertEqual(fields["receipt"], {"state": "unknown"})
        self.assertEqual(fields["appraisal"], {"state": "unknown"})

        rows["item_documents"] = [
            {
                "document_id": "legacy-photo",
                "item_id": "item-visible",
                "document_type": "photo",
                "uri": "https://example.invalid/unlinked-photo.jpg",
                "captured_on": "2026-08-06",
                "notes": None,
            }
        ]
        unlinked = next(
            item for item in insurance_report(rows)["items"] if item["item_id"] == "item-visible"
        )
        self.assertEqual(unlinked["fields"]["photo"], {"state": "unknown"})

        rows["item_documents"][0]["evidence_id"] = "ev-visible"
        report = insurance_report(rows, scope="public")
        linked = next(item for item in report["items"] if item["item_id"] == "item-visible")
        self.assertEqual(linked["fields"]["photo"], {"state": "unknown"})
        self.assertEqual(linked["documents"], [])
        private = next(
            item
            for item in insurance_report(rows, scope="private")["items"]
            if item["item_id"] == "item-visible"
        )
        self.assertNotIn("evidence_id", private["documents"][0])

    def test_appraisal_readiness_requires_an_evidence_linked_appraisal_valuation(self) -> None:
        rows = base_rows()
        visible = next(value for value in rows["valuations"] if value["valuation_id"] == "val-good")
        visible["basis"] = "appraisal"
        next(evidence for evidence in rows["evidence"] if evidence["evidence_id"] == "ev-value")[
            "evidence_type"
        ] = "user_source"
        rows["evidence_assets"].append(
            {"evidence_id": "ev-value", "asset_id": "asset-photo", "role": "appraisal"}
        )
        item = next(
            item
            for item in insurance_report(rows, verified_media_asset_ids={"asset-photo"})["items"]
            if item["item_id"] == "item-visible"
        )
        self.assertEqual(item["fields"]["appraisal"], {"state": "present"})

    def test_blank_location_name_stays_unknown_instead_of_emitting_misaligned_lists(self) -> None:
        rows = base_rows()
        rows["locations"][0]["name"] = ""
        field = next(
            item for item in insurance_report(rows)["items"] if item["item_id"] == "item-visible"
        )["fields"]["location"]
        self.assertEqual(field, {"state": "unknown"})

    def test_item_evidence_roles_round_trip_and_locations_require_parallel_lists(self) -> None:
        rows = base_rows()
        report = insurance_report(rows, verified_media_asset_ids={"asset-photo"})
        self.assertEqual(
            {link["role"] for link in report["item_evidence"]},
            {"primary", "supporting"},
        )
        package = build_insurance_package(report, {"asset-photo": PHOTO_BYTES})
        self.assertEqual(
            validate_insurance_package(package)["item_evidence"], report["item_evidence"]
        )

        forged = insurance_report(rows, verified_media_asset_ids={"asset-photo"})
        visible = next(item for item in forged["items"] if item["item_id"] == "item-visible")
        visible["fields"]["location"]["location_ids"] = "place-visible"
        visible["fields"]["location"]["location_names"] = "Visible shelf"
        with self.assertRaisesRegex(InsuranceError, "invalid location"):
            build_insurance_package(forged, {"asset-photo": PHOTO_BYTES})

    def test_non_insurance_valuation_is_context_not_readiness(self) -> None:
        rows = base_rows()
        rows["valuations"].append(
            {
                "valuation_id": "val-sale",
                "item_id": "item-visible",
                "amount": 31,
                "currency": "GBP",
                "valued_on": "2025-02-04",
                "basis": "sale",
                "evidence_id": "ev-visible",
                "sensitivity": "low",
            }
        )
        item = next(
            item for item in insurance_report(rows)["items"] if item["item_id"] == "item-visible"
        )
        self.assertEqual(
            [value["valuation_id"] for value in item["fields"]["value"]["valuations"]], ["val-good"]
        )
        self.assertEqual(
            [value["valuation_id"] for value in item["valuation_context"]], ["val-good", "val-sale"]
        )

    def test_purchase_or_capture_imagery_needs_direct_current_physical_evidence(self) -> None:
        for evidence_type, claim_strength in (
            ("merchant_account", "purchase_only"),
            ("user_source", "claimed_owned"),
        ):
            with self.subTest(evidence_type=evidence_type):
                rows = base_rows()
                rows["evidence"][0].update(
                    {
                        "evidence_type": evidence_type,
                        "claim_strength": claim_strength,
                    }
                )
                report = insurance_report(rows, verified_media_asset_ids={"asset-photo"})
                self.assertEqual(report["items"][0]["fields"]["photo"], {"state": "unknown"})
                package = build_insurance_package(report, {"asset-photo": PHOTO_BYTES})
                self.assertEqual(validate_insurance_package(package), report)

        capture_derived = base_rows()
        capture_derived["evidence"][0].update(
            {"evidence_type": "merchant_account", "claim_strength": "purchase_only"}
        )
        capture_derived["evidence_assets"][0]["role"] = "crop"
        capture_derived["capture_sessions"] = [
            {
                "capture_session_id": "capture-reviewed",
                "evidence_id": "ev-visible",
                "provenance_state": "bound",
                "sensitivity": "low",
            }
        ]
        capture_derived["capture_observations"] = [
            {
                "capture_session_id": "capture-reviewed",
                "item_id": "item-visible",
                "evidence_id": "ev-visible",
                "validation_state": "validated",
            }
        ]
        report = insurance_report(capture_derived, verified_media_asset_ids={"asset-photo"})
        item = next(item for item in report["items"] if item["item_id"] == "item-visible")
        self.assertEqual(item["fields"]["photo"], {"state": "unknown"})
        self.assertEqual(report["capture_photo_proofs"], [])
        package = build_insurance_package(report, {"asset-photo": PHOTO_BYTES})
        self.assertEqual(validate_insurance_package(package), report)
        lower_scope = insurance_report(
            capture_derived,
            scope="public",
            verified_media_asset_ids={"asset-photo"},
        )
        lower_item = next(
            item for item in lower_scope["items"] if item["item_id"] == "item-visible"
        )
        self.assertEqual(lower_item["fields"]["photo"], {"state": "unknown"})


class InsurancePackageTests(unittest.TestCase):
    def package(self) -> tuple[dict[str, object], bytes, bytes]:
        report = insurance_report(base_rows(), verified_media_asset_ids={"asset-photo"})
        photo = PHOTO_BYTES
        return report, photo, build_insurance_package(report, {"asset-photo": photo})

    def repack_report(self, package: bytes, report: dict[str, object]) -> bytes:
        """Forge both report members and their manifest, as an external attacker could."""
        with zipfile.ZipFile(io.BytesIO(package), "r") as archive:
            entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
        entries["items.json"] = (
            json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        entries["items.csv"] = insurance_module._csv_bytes(report)
        manifest = json.loads(entries["manifest.json"])
        for name in ("items.json", "items.csv"):
            manifest["files"][name] = {
                "sha256": hashlib.sha256(entries[name]).hexdigest(),
                "byte_size": len(entries[name]),
            }
        entries["manifest.json"] = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        rewritten = io.BytesIO()
        with zipfile.ZipFile(rewritten, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        return rewritten.getvalue()

    def test_package_is_byte_deterministic_and_round_trips_without_writes(self) -> None:
        report, photo, first = self.package()
        second = build_insurance_package(report, {"asset-photo": photo})
        self.assertEqual(first, second)
        self.assertEqual(load_insurance_package(first), report)
        self.assertEqual(validate_insurance_package(first), report)
        self.assertEqual(len(report["items"]), 2)
        self.assertEqual(
            report["item_evidence"],
            [
                {
                    "item_id": "item-canary-secret",
                    "evidence_id": "ev-secret",
                    "role": "supporting",
                },
                {"item_id": "item-visible", "evidence_id": "ev-value", "role": "supporting"},
                {"item_id": "item-visible", "evidence_id": "ev-visible", "role": "primary"},
            ],
        )
        self.assertEqual(
            report["evidence_assets"],
            [{"evidence_id": "ev-visible", "asset_id": "asset-photo", "role": "source"}],
        )
        with zipfile.ZipFile(io.BytesIO(first), "r") as archive:
            rows = list(csv.DictReader(io.StringIO(archive.read("items.csv").decode("utf-8"))))
        reconstructed = [json.loads(row["item_json"]) for row in rows]
        self.assertEqual(reconstructed, report["items"])

    def test_external_custody_keeps_ownership_but_is_not_present(self) -> None:
        rows = base_rows()
        rows["item_party_relations"] = [{
            "item_id": "item-visible", "role": "custodian", "status": "active",
            "custody_kind": "loan",
        }]
        report = insurance_report(rows, verified_media_asset_ids={"asset-photo"})
        item = next(item for item in report["items"] if item["item_id"] == "item-visible")
        self.assertEqual(item["ownership_state"], "confirmed")
        self.assertEqual(item["fields"]["custody"], {"state": "unknown"})
        package = build_insurance_package(report, {"asset-photo": PHOTO_BYTES})
        self.assertEqual(validate_insurance_package(package), report)

    def test_photo_heavy_report_stays_within_the_validator_member_contract(self) -> None:
        rows = base_rows()
        payloads = {
            f"asset-{index}": fixture_image_bytes(f"distinct-photo-{index}") for index in range(254)
        }
        rows["media_assets"] = [
            {
                "asset_id": asset_id,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "byte_size": len(payload),
                "media_type": "image/png",
                "sensitivity": "low",
            }
            for asset_id, payload in payloads.items()
        ]
        rows["evidence_assets"] = [
            {
                "evidence_id": "ev-visible",
                "asset_id": asset_id,
                "role": "source",
            }
            for asset_id in payloads
        ]
        report = insurance_report(rows, verified_media_asset_ids=set(payloads))
        package = build_insurance_package(report, payloads)
        with zipfile.ZipFile(io.BytesIO(package), "r") as archive:
            self.assertEqual(len(archive.infolist()), 257)
        self.assertEqual(validate_insurance_package(package), report)

    def test_media_hash_and_tampering_are_rejected(self) -> None:
        report, photo, package = self.package()
        with self.assertRaisesRegex(InsuranceError, "media bytes"):
            build_insurance_package(report, {"asset-photo": b"changed"})
        with zipfile.ZipFile(io.BytesIO(package), "r") as archive:
            entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
        entries["media/" + hashlib.sha256(photo).hexdigest()] = b"tampered"
        rewritten = io.BytesIO()
        with zipfile.ZipFile(rewritten, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        with self.assertRaisesRegex(InsuranceError, "digest mismatch"):
            validate_insurance_package(rewritten.getvalue())

    def test_csv_tampering_is_rejected_even_when_manifest_is_rehashed(self) -> None:
        _, _, package = self.package()
        with zipfile.ZipFile(io.BytesIO(package), "r") as archive:
            entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
        entries["items.csv"] = b"forged\n"
        manifest = json.loads(entries["manifest.json"])
        manifest["files"]["items.csv"] = {
            "sha256": hashlib.sha256(entries["items.csv"]).hexdigest(),
            "byte_size": len(entries["items.csv"]),
        }
        entries["manifest.json"] = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        rewritten = io.BytesIO()
        with zipfile.ZipFile(rewritten, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        with self.assertRaisesRegex(InsuranceError, "CSV report"):
            validate_insurance_package(rewritten.getvalue())

    def test_rejects_nonfinite_reports_and_neutralizes_csv_formulas(self) -> None:
        report, photo, _ = self.package()
        visible = next(item for item in report["items"] if item["item_id"] == "item-visible")
        visible["model"] = "=formula"
        package = build_insurance_package(report, {"asset-photo": photo})
        with zipfile.ZipFile(io.BytesIO(package), "r") as archive:
            self.assertIn("'=formula", archive.read("items.csv").decode())
        visible["valuation_context"][0]["amount"] = math.nan
        with self.assertRaisesRegex(InsuranceError, "invalid valuation|finite|serialis"):
            build_insurance_package(report, {"asset-photo": photo})
        malformed, photo, _ = self.package()
        malformed["items"][0]["valuation_context"] = None
        with self.assertRaisesRegex(InsuranceError, "invalid valuation context"):
            build_insurance_package(malformed, {"asset-photo": photo})

        malformed, photo, _ = self.package()
        malformed["scope"] = []
        with self.assertRaisesRegex(InsuranceError, "scope must be a string"):
            build_insurance_package(malformed, {"asset-photo": photo})

        malformed, photo, _ = self.package()
        valued = next(item for item in malformed["items"] if item["valuation_context"])
        valued["valuation_context"][0]["amount"] = 10**310
        with self.assertRaisesRegex(InsuranceError, "invalid valuation"):
            build_insurance_package(malformed, {"asset-photo": photo})

    def test_rejects_forged_ready_field_state_before_packaging(self) -> None:
        report, photo, _ = self.package()
        secret = next(item for item in report["items"] if item["item_id"] == "item-canary-secret")
        secret["fields"]["photo"] = {"state": "present"}
        secret["gaps"].remove("photo")
        with self.assertRaisesRegex(InsuranceError, "invalid photo assessment"):
            build_insurance_package(report, {"asset-photo": photo})

    def test_rejects_forged_capture_proof_and_cross_crop_packages(self) -> None:
        report, _photo, package = self.package()
        proof_forgery = deepcopy(report)
        proof_forgery["capture_photo_proofs"] = [
            {
                "capture_session_id": "capture-forged",
                "item_id": "item-visible",
                "evidence_id": "ev-visible",
                "provenance_state": "bound",
                "validation_state": "validated",
            }
        ]
        forged_package = self.repack_report(package, proof_forgery)
        with self.assertRaisesRegex(InsuranceError, "must not contain capture photo proofs"):
            validate_insurance_package(forged_package)

        cross_crop = deepcopy(report)
        visible = next(item for item in cross_crop["items"] if item["item_id"] == "item-visible")
        visible["fields"]["photo"] = {
            "state": "present",
            "evidence": [
                {
                    "evidence_id": "ev-visible",
                    "qualification": "reviewed_capture_crop",
                    "capture_session_id": "capture-for-another-crop",
                }
            ],
        }
        forged_cross_crop_package = self.repack_report(package, cross_crop)
        with self.assertRaisesRegex(InsuranceError, "invalid photo"):
            validate_insurance_package(forged_cross_crop_package)

    def test_rejects_unsafe_and_nonregular_archive_members_before_reading(self) -> None:
        _, _, package = self.package()
        with zipfile.ZipFile(io.BytesIO(package), "r") as archive:
            entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
        rewritten = io.BytesIO()
        with zipfile.ZipFile(rewritten, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
            archive.writestr("..\\escape", b"x")
        with self.assertRaisesRegex(InsuranceError, "unsafe"):
            validate_insurance_package(rewritten.getvalue())
        rewritten = io.BytesIO()
        with zipfile.ZipFile(rewritten, "w") as archive:
            for name, value in entries.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = 0o120777 << 16
                archive.writestr(info, value)
        with self.assertRaisesRegex(InsuranceError, "non-regular"):
            validate_insurance_package(rewritten.getvalue())

    def test_rejects_compression_bombs_before_extracting(self) -> None:
        _, _, package = self.package()
        with zipfile.ZipFile(io.BytesIO(package), "r") as archive:
            entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
        rewritten = io.BytesIO()
        with zipfile.ZipFile(rewritten, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
            archive.writestr("media/" + "0" * 64, b"0" * (256 * 1024))
        with self.assertRaisesRegex(InsuranceError, "compression ratio"):
            validate_insurance_package(rewritten.getvalue())


if __name__ == "__main__":
    unittest.main()
