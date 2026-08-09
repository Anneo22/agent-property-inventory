#!/usr/bin/env python3
"""Prove a clean wheel can run the core lifecycle and a real stdio MCP client."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client


def fail(message: str) -> None:
    raise RuntimeError(message)


def main() -> None:
    # Keep the venv's executable directory. Resolving the interpreter symlink
    # jumps to the base Python install and hides the wheel's console scripts.
    executable_dir = Path(sys.executable).parent
    cli = executable_dir / "property-inventory"
    mcp = executable_dir / "property-inventory-mcp"
    if not cli.is_file() or not mcp.is_file():
        fail("installed wheel did not provide both console scripts")

    with tempfile.TemporaryDirectory(prefix="property-inventory-wheel-smoke-") as raw:
        scratch = Path(raw)
        inventory = scratch / "inventory"
        runtime = scratch / "runtime"
        media = scratch / "media"
        catalogue = scratch / "notes" / "Inventory.md"
        common = [
            str(cli),
            "--inventory-root",
            str(inventory),
            "--runtime-dir",
            str(runtime),
            "--media-root",
            str(media),
            "--catalogue-output",
            str(catalogue),
            "--scope",
            "private",
        ]

        def run(*arguments: str) -> dict:
            completed = subprocess.run(
                [*common, *arguments],
                cwd=scratch,
                env={**os.environ, "PYTHONPATH": ""},
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                fail(completed.stderr or completed.stdout)
            value = json.loads(completed.stdout)
            if not isinstance(value, dict):
                fail("CLI did not return a JSON object")
            return value

        run("init")
        run(
            "add-location",
            "--location-id",
            "loc-wheel-smoke",
            "--name",
            "Wheel smoke room",
            "--kind",
            "room",
            "--sensitivity",
            "low",
        )
        run(
            "plan",
            "--actor",
            "clean wheel smoke",
            "--source-ref",
            "synthetic cart fixture",
            "--name",
            "Planned smoke object",
            "--category",
            "test fixture",
            "--planned-on",
            "2026-08-05",
            "--sensitivity",
            "low",
        )
        ordered = run(
            "order",
            "--actor",
            "clean wheel smoke",
            "--source-ref",
            "synthetic placed order fixture",
            "--name",
            "Ordered smoke object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
            "--new-model",
            "--sensitivity",
            "low",
        )["result"]
        item_id = ordered["item_id"]
        run(
            "receive",
            "--actor",
            "clean wheel smoke",
            "--source-ref",
            "synthetic delivery fixture",
            "--item-id",
            item_id,
            "--received-on",
            "2026-08-06",
            "--location-id",
            "loc-wheel-smoke",
        )
        run(
            "sell",
            "--actor",
            "clean wheel smoke",
            "--source-ref",
            "synthetic sale fixture",
            "--item-id",
            item_id,
            "--sold-on",
            "2026-08-07",
        )
        status = run("status")
        if status.get("status") != "pass":
            fail(f"clean wheel lifecycle did not verify: {status}")
        shown = run("show", item_id)
        if shown["item"]["ownership_state"] != "disposed":
            fail("purchase, delivery, and sale lifecycle did not reach disposed")

        async def exercise_mcp() -> None:
            parameters = StdioServerParameters(
                command=str(mcp),
                args=[
                    "--inventory-root",
                    str(inventory),
                    "--runtime-dir",
                    str(runtime),
                    "--media-root",
                    str(media),
                    "--catalogue-output",
                    str(catalogue),
                    "--scope",
                    "private",
                    "--profile",
                    "read",
                ],
                cwd=scratch,
                env={**os.environ, "PYTHONPATH": ""},
            )
            async with Client(stdio_client(parameters), mode="legacy") as client:
                tools = {tool.name for tool in (await client.list_tools()).tools}
                required = {
                    "inventory_status",
                    "search_inventory",
                    "get_inventory_item",
                    "list_inventory_items",
                    "list_inventory_locations",
                }
                if not required <= tools:
                    fail(f"MCP tool surface is incomplete: {sorted(required - tools)}")
                mcp_status = await client.call_tool("inventory_status", {})
                if mcp_status.is_error or mcp_status.structured_content.get("status") != "pass":
                    fail(f"MCP status failed: {mcp_status.content}")
                search = await client.call_tool(
                    "search_inventory", {"query": "Ordered smoke object"}
                )
                if search.is_error or not search.structured_content.get("recorded"):
                    fail(f"MCP could not query the clean-wheel lifecycle: {search.content}")

        asyncio.run(exercise_mcp())
        print(json.dumps({"item_id": item_id, "status": "pass"}, sort_keys=True))


if __name__ == "__main__":
    main()
