#!/usr/bin/env python3
"""Compatibility entry point for source checkouts."""

from __future__ import annotations

import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SOURCE_ROOT))
if __name__ == "property_inventory":
    __path__ = [str(SOURCE_ROOT / "property_inventory")]

from property_inventory import cli as _CLI  # noqa: E402

InventoryError = _CLI.InventoryError
canonical_lock_path = _CLI.canonical_lock_path
git_store_is_clean = _CLI.git_store_is_clean
main = _CLI.main

__all__ = ["InventoryError", "canonical_lock_path", "git_store_is_clean", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
