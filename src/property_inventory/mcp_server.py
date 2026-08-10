"""Least-privilege stdio MCP adapter for the Property Inventory core."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from collections.abc import Sequence
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from typing import Any, Literal

from .capture_adapters import load_adapter_registry
from .cli import execute
from .config import ConfigError, resolve_instance_config
from .rebuild import LOCATION_KINDS


def create_server(
    *,
    inventory_root: Path,
    runtime_dir: Path,
    media_root: Path | None,
    catalogue_output: Path | None = None,
    catalogue_scope: Literal["public", "personal", "private"] = "personal",
    capture_adapters_config: Path | None = None,
    forbidden_roots: Sequence[Path] = (),
    scope: Literal["public", "personal", "private"],
    profile: Literal["read", "write"],
) -> Any:
    """Create an MCP server whose exposed tools are fixed by startup profile."""
    try:
        from mcp.server import MCPServer
    except ImportError as error:  # pragma: no cover - exercised by clean base install.
        raise RuntimeError(
            "MCP support is not installed; install agent-property-inventory[mcp]"
        ) from error

    server = MCPServer(
        name="property-inventory",
        description="Evidence-backed, local-first physical property inventory",
        instructions=(
            "Search before purchase or ownership claims. Empty search means unknown, "
            "not absent. Compatibility is definitive only when supported by normalized "
            "or sufficiently confident explicit evidence."
        ),
        version="0.2.0",
    )
    frozen_capture_registry = (
        load_adapter_registry(capture_adapters_config)
        if capture_adapters_config is not None
        else None
    )

    common = [
        "--inventory-root",
        str(inventory_root),
        "--runtime-dir",
        str(runtime_dir),
        "--scope",
        scope,
    ]
    if media_root is not None:
        common.extend(("--media-root", str(media_root)))
    if catalogue_output is not None:
        common.extend(("--catalogue-output", str(catalogue_output)))
    common.extend(("--catalogue-scope", catalogue_scope))
    if capture_adapters_config is not None:
        common.extend(("--capture-adapters-config", str(capture_adapters_config)))
    for forbidden_root in forbidden_roots:
        common.extend(("--forbidden-root", str(forbidden_root)))

    def invoke(*arguments: str) -> dict[str, Any]:
        # argparse raises SystemExit after printing usage.  A bad MCP request must
        # be a normal tool error and must never expose invocation paths over stdio.
        try:
            with redirect_stderr(StringIO()):
                return execute(
                    [*common, *arguments],
                    capture_adapter_registry=frozen_capture_registry,
                )
        except SystemExit:
            raise ValueError("invalid inventory tool arguments") from None
        except Exception:
            if scope != "private":
                raise ValueError(
                    "inventory tool could not complete safely in this scope"
                ) from None
            raise

    def retrieval_options(
        *,
        limit: int,
        cursor: str | None = None,
        category: str | None = None,
        ownership_state: str | None = None,
        condition: str | None = None,
        location: str | None = None,
        tag: str | None = None,
        alias_kind: str | None = None,
        interface_family: str | None = None,
        interface_standard: str | None = None,
        interface_variant: str | None = None,
        interface_direction: str | None = None,
        location_known: str | None = None,
    ) -> list[str]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if interface_direction not in {None, "plug", "socket", "bidirectional", "unknown"}:
            raise ValueError("interface_direction must be plug, socket, bidirectional, or unknown")
        if location_known not in {None, "known", "unknown"}:
            raise ValueError("location_known must be known or unknown")
        values = {
            "--category": category,
            "--ownership-state": ownership_state,
            "--condition": condition,
            "--location": location,
            "--tag": tag,
            "--alias-kind": alias_kind,
            "--interface-family": interface_family,
            "--interface-standard": interface_standard,
            "--interface-variant": interface_variant,
            "--interface-direction": interface_direction,
            "--location-known": location_known,
        }
        options = ["--limit", str(limit)]
        if cursor is not None:
            if not cursor.strip():
                raise ValueError("cursor must not be blank")
            options.extend(("--cursor", cursor))
        for flag, value in values.items():
            if value is not None:
                options.extend((flag, value))
        return options

    @server.tool(
        name="inventory_status",
        description="Verify the canonical inventory and return only scope-safe status.",
        structured_output=True,
    )
    def inventory_status() -> dict[str, Any]:
        return invoke("status")

    @server.tool(
        name="get_insurance_readiness",
        description=(
            "Return scope-safe evidence-backed insurance readiness and explicit unknown gaps. "
            "It never writes or exports a package."
        ),
        structured_output=True,
    )
    def get_insurance_readiness() -> dict[str, Any]:
        return invoke("insurance-status")

    @server.tool(
        name="get_upkeep_report",
        description=(
            "Return scope-safe observed upkeep records and counts. Empty results do not "
            "prove that no upkeep occurred, and the report never starts or finishes a session."
        ),
        structured_output=True,
    )
    def get_upkeep_report() -> dict[str, Any]:
        return invoke("maintenance-report")

    @server.tool(
        name="search_inventory",
        description=(
            "Search before asserting ownership or recommending a purchase. "
            "No result means unknown, never absent."
        ),
        structured_output=True,
    )
    def search_inventory(
        query: str,
        limit: int = 50,
        cursor: str | None = None,
        category: str | None = None,
        ownership_state: str | None = None,
        condition: str | None = None,
        location: str | None = None,
        tag: str | None = None,
        alias_kind: str | None = None,
        interface_family: str | None = None,
        interface_standard: str | None = None,
        interface_variant: str | None = None,
        interface_direction: str | None = None,
        location_known: str | None = None,
    ) -> dict[str, Any]:
        if not query.strip():
            raise ValueError("query must not be blank")
        return invoke(
            "search",
            *retrieval_options(
                limit=limit,
                cursor=cursor,
                category=category,
                ownership_state=ownership_state,
                condition=condition,
                location=location,
                tag=tag,
                alias_kind=alias_kind,
                interface_family=interface_family,
                interface_standard=interface_standard,
                interface_variant=interface_variant,
                interface_direction=interface_direction,
                location_known=location_known,
            ),
            "--",
            query,
        )

    @server.tool(
        name="list_inventory_items",
        description=(
            "Enumerate every scope-visible item matching typed filters. Use location to "
            "retrieve the complete expected set for a room or container."
        ),
        structured_output=True,
    )
    def list_inventory_items(
        limit: int = 500,
        cursor: str | None = None,
        category: str | None = None,
        ownership_state: str | None = None,
        condition: str | None = None,
        location: str | None = None,
        tag: str | None = None,
        alias_kind: str | None = None,
        interface_family: str | None = None,
        interface_standard: str | None = None,
        interface_variant: str | None = None,
        interface_direction: str | None = None,
        location_known: str | None = None,
    ) -> dict[str, Any]:
        return invoke(
            "list-items",
            *retrieval_options(
                limit=limit,
                cursor=cursor,
                category=category,
                ownership_state=ownership_state,
                condition=condition,
                location=location,
                tag=tag,
                alias_kind=alias_kind,
                interface_family=interface_family,
                interface_standard=interface_standard,
                interface_variant=interface_variant,
                interface_direction=interface_direction,
                location_known=location_known,
            ),
        )

    @server.tool(
        name="list_inventory_locations",
        description=(
            "List or resolve scope-visible nodes of the spatial tree, from a site down to a "
            "compartment, with each match carrying its full root-to-leaf path. Search matches "
            "the whole path, so an intermediate name need not be known. Use it before choosing "
            "an area or parent location."
        ),
        structured_output=True,
    )
    def list_inventory_locations(
        query: str | None = None,
        parent_location_id: str | None = None,
        kind: str | None = None,
        limit: int = 500,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if kind is not None and kind not in LOCATION_KINDS:
            raise ValueError("invalid location kind")
        arguments = ["locations", "--limit", str(limit)]
        if cursor is not None:
            if not cursor.strip():
                raise ValueError("cursor must not be blank")
            arguments.extend(("--cursor", cursor))
        for flag, value in (
            ("--query", query),
            ("--parent-location-id", parent_location_id),
            ("--kind", kind),
        ):
            if value is not None:
                arguments.extend((flag, value))
        return invoke(*arguments)

    @server.tool(
        name="inventory_task_context",
        description=(
            "Return scope-safe inventory context for a task, with explicit unknowns. "
            "No match means unknown, never absent."
        ),
        structured_output=True,
    )
    def inventory_task_context(
        task: str,
        limit: int = 50,
        cursor: str | None = None,
        category: str | None = None,
        ownership_state: str | None = None,
        condition: str | None = None,
        location: str | None = None,
        tag: str | None = None,
        alias_kind: str | None = None,
        interface_family: str | None = None,
        interface_standard: str | None = None,
        interface_variant: str | None = None,
        interface_direction: str | None = None,
        location_known: str | None = None,
    ) -> dict[str, Any]:
        if not task.strip():
            raise ValueError("task must not be blank")
        return invoke(
            "context",
            "--task",
            task,
            *retrieval_options(
                limit=limit,
                cursor=cursor,
                category=category,
                ownership_state=ownership_state,
                condition=condition,
                location=location,
                tag=tag,
                alias_kind=alias_kind,
                interface_family=interface_family,
                interface_standard=interface_standard,
                interface_variant=interface_variant,
                interface_direction=interface_direction,
                location_known=location_known,
            ),
        )

    @server.tool(
        name="get_inventory_item",
        description="Get one scope-visible item with its evidence and lifecycle context.",
        structured_output=True,
    )
    def get_inventory_item(item_id: str) -> dict[str, Any]:
        return invoke("show", "--", item_id)

    @server.tool(
        name="check_compatibility",
        description=(
            "Check two scope-visible items. Unknown means the record does not prove "
            "interchangeability."
        ),
        structured_output=True,
    )
    def check_compatibility(
        first_item_id: str, second_item_id: str
    ) -> dict[str, Any]:
        return invoke("compatibility", "--", first_item_id, second_item_id)

    @server.tool(
        name="get_kit_status",
        description=(
            "Return conservative evidence-backed kit readiness. Unknown does not prove a "
            "requirement is absent."
        ),
        structured_output=True,
    )
    def get_kit_status(
        kit_id: str | None = None, serves_item_id: str | None = None
    ) -> dict[str, Any]:
        if kit_id is not None and serves_item_id is not None:
            raise ValueError("choose kit_id or serves_item_id, not both")
        arguments = ["kit-status"]
        if kit_id is not None:
            arguments.extend(("--kit-id", kit_id))
        if serves_item_id is not None:
            arguments.extend(("--serves-item-id", serves_item_id))
        return invoke(*arguments)

    @server.tool(
        name="check_torque_path",
        description=(
            "Check a requested torque against tool and adapter limits. Missing ratings "
            "always return unknown, never safe."
        ),
        structured_output=True,
    )
    def check_torque_path(
        requested_nm: float,
        path_id: str | None = None,
        tool_item_id: str | None = None,
    ) -> dict[str, Any]:
        if not math.isfinite(requested_nm) or requested_nm < 0:
            raise ValueError("requested_nm must be finite and non-negative")
        if (path_id is None) == (tool_item_id is None):
            raise ValueError("choose exactly one of path_id or tool_item_id")
        selector = ("--path-id", path_id) if path_id is not None else (
            "--tool-item-id",
            tool_item_id,
        )
        return invoke(
            "torque-check", selector[0], str(selector[1]), "--requested-nm", str(requested_nm)
        )

    @server.tool(
        name="get_space_context",
        description=(
            "Return only checked spatial profiles visible for a location. Missing or "
            "protected profiles remain unknown without disclosing their existence."
        ),
        structured_output=True,
    )
    def get_space_context(location_id: str) -> dict[str, Any]:
        if not location_id.strip():
            raise ValueError("location_id must not be blank")
        return invoke("space", "--", location_id)

    @server.tool(
        name="check_spatial_fit",
        description=(
            "Check supplied explicit item measurements against one visible checked "
            "container-box profile. Unknown never means it does not fit."
        ),
        structured_output=True,
    )
    def check_spatial_fit(
        location_id: str,
        item_dimensions: dict[str, Any] | None = None,
        item_id: str | None = None,
        allow_rotation: bool = True,
    ) -> dict[str, Any]:
        if not location_id.strip():
            raise ValueError("location_id must not be blank")
        if (item_dimensions is None) == (item_id is None):
            raise ValueError("choose exactly one of item_dimensions or item_id")
        arguments = ["fit", location_id]
        if item_dimensions is not None:
            if not isinstance(item_dimensions, dict):
                raise ValueError("item_dimensions must be an object")
            arguments.extend(
                ("--item-dimensions", json.dumps(item_dimensions, ensure_ascii=False))
            )
        else:
            arguments.extend(("--item-id", str(item_id)))
        if not allow_rotation:
            arguments.append("--no-rotation")
        return invoke(*arguments)

    @server.tool(
        name="calculate_free_volume",
        description=(
            "Calculate remaining volume in one visible checked container from explicit "
            "positioned, non-overlapping occupied boxes. Missing geometry stays unknown."
        ),
        structured_output=True,
    )
    def calculate_free_volume(
        location_id: str,
        occupied_boxes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not location_id.strip():
            raise ValueError("location_id must not be blank")
        if not isinstance(occupied_boxes, list) or any(
            not isinstance(box, dict) for box in occupied_boxes
        ):
            raise ValueError("occupied_boxes must be an array of objects")
        arguments = ["free-volume", location_id]
        for box in occupied_boxes:
            arguments.extend(("--occupied-box", json.dumps(box, ensure_ascii=False)))
        return invoke(*arguments)

    @server.tool(
        name="plan_spatial_packing",
        description=(
            "Plan deterministic first-fit packing from supplied explicit measurements "
            "and one visible checked container-box profile. It never assigns locations."
        ),
        structured_output=True,
    )
    def plan_spatial_packing(
        location_id: str,
        items: list[dict[str, Any]] | None = None,
        item_ids: list[str] | None = None,
        allow_rotation: bool = True,
    ) -> dict[str, Any]:
        if not location_id.strip():
            raise ValueError("location_id must not be blank")
        if (items is None) == (item_ids is None):
            raise ValueError("choose exactly one of items or item_ids")
        arguments = ["pack", location_id]
        if items is not None:
            if not isinstance(items, list):
                raise ValueError("items must be an array")
            arguments.extend(("--items", json.dumps(items, ensure_ascii=False)))
        else:
            if not isinstance(item_ids, list) or not item_ids:
                raise ValueError("item_ids must be a non-empty array")
            for item_id in item_ids:
                arguments.extend(("--item-id", item_id))
        if not allow_rotation:
            arguments.append("--no-rotation")
        return invoke(*arguments)

    if scope == "private":

        @server.tool(
            name="show_inventory_proposal",
            description="Inspect a private prepared or applied proposal without mutation.",
            structured_output=True,
        )
        def show_inventory_proposal(proposal_id: str) -> dict[str, Any]:
            return invoke("proposal-show", "--", proposal_id)

        @server.tool(
            name="capture_status",
            description=(
                "Read the state of one private staged overview capture. It exposes no "
                "lifecycle mutation and does not imply possession or a location."
            ),
            structured_output=True,
        )
        def capture_status(capture_session_id: str) -> dict[str, Any]:
            return invoke("capture-status", "--", capture_session_id)

    if profile == "write":
        if scope != "private":
            raise ValueError("the write profile requires private scope")

        @server.tool(
            name="prepare_inventory_proposal",
            description=(
                "Prepare a reviewable list of supported CLI operation argument arrays. "
                "This does not mutate the canonical store."
            ),
            structured_output=True,
        )
        def prepare_inventory_proposal(
            operations: list[list[str]],
        ) -> dict[str, Any]:
            # The generic MCP surface accepts only self-contained argument
            # arrays. File-backed operations would let a caller copy arbitrary
            # local bytes into a returned proposal. Dedicated capture and sync
            # tools have their own bounded, digest-bound file contracts.
            forbidden_commands = {"capture-commit", "import-floorplan"}
            forbidden_options = {"--document-json", "--file", "--input", "--operations"}
            for number, operation in enumerate(operations, start=1):
                if not operation or operation[0] in forbidden_commands:
                    raise ValueError(
                        f"proposal operation {number} is not available through MCP"
                    )
                if any(argument in forbidden_options for argument in operation[1:]):
                    raise ValueError(
                        f"proposal operation {number} contains a file-backed option"
                    )
            with tempfile.TemporaryDirectory(
                prefix="property-inventory-mcp-proposal-"
            ) as temporary_directory:
                operations_path = Path(temporary_directory) / "operations.json"
                operations_path.write_text(
                    json.dumps(operations, ensure_ascii=False), encoding="utf-8"
                )
                return invoke("propose", "--operations", str(operations_path))

        @server.tool(
            name="prepare_physical_discovery",
            description=(
                "Prepare, but never apply, one physically shown already-owned item. "
                "The sealed proposal creates explicit-current physical evidence and "
                "received plus physically-verified lifecycle events."
            ),
            structured_output=True,
        )
        def prepare_physical_discovery(
            actor: str,
            source_ref: str,
            name: str,
            category: str,
            checked_on: str,
            location_id: str,
            container_id: str | None = None,
            brand: str | None = None,
            model: str | None = None,
            existing_model_id: str | None = None,
            new_model: bool = False,
            existing_item_id: str | None = None,
            new_unit: bool = False,
            reference_url: str | None = None,
            quantity: float | None = None,
            unit: str = "item",
            condition: str | None = None,
            serial_or_lot: str | None = None,
            notes: str | None = None,
            sensitivity: str = "personal",
            specs: dict[str, Any] | None = None,
            identifiers: dict[str, Any] | None = None,
            interfaces: list[str] | None = None,
        ) -> dict[str, Any]:
            required = {
                "actor": actor,
                "source_ref": source_ref,
                "name": name,
                "category": category,
                "checked_on": checked_on,
                "location_id": location_id,
            }
            if any(not isinstance(value, str) or not value.strip() for value in required.values()):
                raise ValueError("physical discovery text inputs must not be blank")
            if sensitivity not in {"low", "personal", "high"}:
                raise ValueError("sensitivity must be low, personal, or high")
            if existing_item_id is not None and new_unit:
                raise ValueError("existing_item_id and new_unit are mutually exclusive")
            if existing_model_id is not None and new_model:
                raise ValueError("existing_model_id and new_model are mutually exclusive")
            if existing_item_id is not None and new_model:
                raise ValueError("existing_item_id and new_model are mutually exclusive")
            if specs is not None and not isinstance(specs, dict):
                raise ValueError("specs must be an object")
            if identifiers is not None and not isinstance(identifiers, dict):
                raise ValueError("identifiers must be an object")
            if interfaces is not None and (
                not isinstance(interfaces, list) or any(not isinstance(value, str) for value in interfaces)
            ):
                raise ValueError("interfaces must be an array of strings")
            operation = [
                "discover", "--actor", actor, "--source-ref", source_ref,
                "--name", name, "--category", category, "--checked-on", checked_on,
                "--location-id", location_id, "--unit", unit, "--sensitivity", sensitivity,
            ]
            optional = {
                "--container-id": container_id,
                "--brand": brand,
                "--model": model,
                "--existing-model-id": existing_model_id,
                "--existing-item-id": existing_item_id,
                "--reference-url": reference_url,
                "--condition": condition,
                "--serial-or-lot": serial_or_lot,
                "--notes": notes,
            }
            for flag, value in optional.items():
                if value is not None:
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError(f"{flag.removeprefix('--')} must be non-blank text")
                    operation.extend((flag, value))
            if new_unit:
                operation.append("--new-unit")
            if new_model:
                operation.append("--new-model")
            if quantity is not None:
                if (
                    isinstance(quantity, bool)
                    or not isinstance(quantity, (int, float))
                    or not math.isfinite(quantity)
                    or quantity <= 0
                ):
                    raise ValueError("quantity must be a finite positive number")
                operation.extend(("--quantity", str(quantity)))
            for interface in interfaces or []:
                operation.extend(("--interface", interface))
            operation.extend(("--specs", json.dumps(specs or {}, ensure_ascii=False)))
            operation.extend(("--identifiers", json.dumps(identifiers or {}, ensure_ascii=False)))
            with tempfile.TemporaryDirectory(
                prefix="property-inventory-mcp-discovery-"
            ) as temporary_directory:
                operations_path = Path(temporary_directory) / "operations.json"
                operations_path.write_text(
                    json.dumps([operation], ensure_ascii=False), encoding="utf-8"
                )
                return invoke("propose", "--operations", str(operations_path))

        @server.tool(
            name="prepare_overview_capture",
            description=(
                "Copy a local overview image and deterministic crops into private proposal "
                "staging. Configured adapters are trusted local programs, not sandboxed and "
                "not prevented from network access. This prepares capture evidence only; "
                "a later reviewed decision may seal explicit physical or discovery facts into "
                "a proposal, but this tool never makes a canonical assertion."
            ),
            structured_output=True,
        )
        def prepare_overview_capture(
            overview_path: str,
            captured_on: str,
            segments: list[dict[str, Any]],
            source_ref: str | None = None,
            evidence_id: str | None = None,
            evidence_type: str = "user_source",
            sensitivity: str = "personal",
            adapter_name: str | None = None,
            adapter_timeout: float = 10.0,
        ) -> dict[str, Any]:
            if (source_ref is None) == (evidence_id is None):
                raise ValueError("provide exactly one of source_ref or evidence_id")
            arguments = [
                "capture-prepare", "--overview", overview_path, "--captured-on", captured_on,
                "--segments", json.dumps(segments, ensure_ascii=False), "--evidence-type", evidence_type,
                "--sensitivity", sensitivity, "--adapter-timeout", str(adapter_timeout),
            ]
            if source_ref is not None:
                arguments.extend(("--source-ref", source_ref))
            else:
                arguments.extend(("--evidence-id", evidence_id or ""))
            if adapter_name is not None:
                arguments.extend(("--adapter-name", adapter_name))
            return invoke(*arguments)

        @server.tool(
            name="review_overview_capture",
            description=(
                "Seal explicit observation links and reviewed physical or discovery decisions "
                "for an existing staged capture into a digest-bound proposal without rerunning "
                "the adapter. It never applies canonical writes; sealed lifecycle claims take "
                "effect only if the separately reviewed proposal is applied."
            ),
            structured_output=True,
        )
        def review_overview_capture(
            capture_session_id: str,
            artifact_sha256: str,
            links: dict[str, str] | None = None,
            manual_observations: dict[str, dict[str, Any]] | None = None,
            decisions: list[dict[str, Any]] | None = None,
        ) -> dict[str, Any]:
            if not capture_session_id.strip():
                raise ValueError("capture_session_id must not be blank")
            if len(artifact_sha256) != 64:
                raise ValueError("artifact_sha256 must be the preparation digest")
            return invoke(
                "capture-review",
                capture_session_id,
                "--artifact-sha256",
                artifact_sha256,
                "--links",
                json.dumps(links or {}, ensure_ascii=False),
                "--manual-observations",
                json.dumps(manual_observations or {}, ensure_ascii=False),
                "--decisions",
                json.dumps(decisions or [], ensure_ascii=False),
            )

        @server.tool(
            name="prepare_replica_sync",
            description=(
                "Inspect one local offline replica bundle against the current private canonical "
                "head and prepare a digest-bound merge plan. It never applies a plan or contacts a network."
            ),
            structured_output=True,
        )
        def prepare_replica_sync(
            bundle_path: str, trusted_base_path: str
        ) -> dict[str, Any]:
            if not bundle_path.strip() or not trusted_base_path.strip():
                raise ValueError("bundle_path and trusted_base_path must not be blank")
            return invoke(
                "sync-prepare",
                "--bundle",
                bundle_path,
                "--trusted-base",
                trusted_base_path,
            )

        @server.tool(
            name="inspect_replica_sync",
            description=(
                "Inspect one private offline replica merge plan. This is available only to the "
                "private write profile because plans contain private replica rows."
            ),
            structured_output=True,
        )
        def inspect_replica_sync(plan_id: str) -> dict[str, Any]:
            return invoke("sync-show", "--", plan_id)

        @server.tool(
            name="resolve_replica_sync",
            description=(
                "Record explicit choices for every conflict in one private replica plan and "
                "rebuild and verify its ready result. It never applies the plan or contacts a network."
            ),
            structured_output=True,
        )
        def resolve_replica_sync(
            plan_id: str, resolutions: dict[str, str]
        ) -> dict[str, Any]:
            if not isinstance(resolutions, dict):
                raise ValueError("resolutions must be an object")
            with tempfile.TemporaryDirectory(prefix="property-inventory-mcp-sync-") as temporary_directory:
                resolution_path = Path(temporary_directory) / "resolutions.json"
                resolution_path.write_text(
                    json.dumps(resolutions, ensure_ascii=False), encoding="utf-8"
                )
                return invoke(
                    "sync-resolve", plan_id, "--resolutions", str(resolution_path)
                )

    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--instance")
    parser.add_argument(
        "--inventory-root",
        type=Path,
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
    )
    parser.add_argument(
        "--media-root",
        type=Path,
    )
    parser.add_argument("--catalogue-output", type=Path)
    parser.add_argument(
        "--capture-adapters-config",
        type=Path,
        default=(
            Path(os.environ["PROPERTY_INVENTORY_CAPTURE_ADAPTERS"])
            if os.environ.get("PROPERTY_INVENTORY_CAPTURE_ADAPTERS")
            else None
        ),
    )
    parser.add_argument(
        "--catalogue-scope",
        choices=("public", "personal", "private"),
    )
    parser.add_argument(
        "--forbidden-root", type=Path, action="append", dest="forbidden_roots"
    )
    parser.add_argument(
        "--scope",
        choices=("public", "personal", "private"),
        default=os.environ.get("PROPERTY_INVENTORY_SCOPE", "personal"),
    )
    parser.add_argument(
        "--profile",
        choices=("read", "write"),
        default=os.environ.get("PROPERTY_INVENTORY_MCP_PROFILE", "read"),
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
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
        server = create_server(
            inventory_root=instance.inventory_root,
            runtime_dir=instance.runtime_dir,
            media_root=instance.media_root,
            catalogue_output=instance.catalogue_output,
            catalogue_scope=instance.catalogue_scope,
            capture_adapters_config=args.capture_adapters_config,
            forbidden_roots=instance.forbidden_roots,
            scope=args.scope,
            profile=args.profile,
        )
    except (ConfigError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
