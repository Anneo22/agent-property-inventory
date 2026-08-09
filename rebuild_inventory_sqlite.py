#!/usr/bin/env python3
# ruff: noqa: E402, I001
"""Compatibility entry point for rebuilding from a source checkout."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from property_inventory.rebuild import main  # noqa: E402


if __name__ == "__main__":
    main()
