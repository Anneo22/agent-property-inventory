"""Evidence-backed property inventory core with lazy compatibility exports."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["InventoryError", "_CLI", "canonical_lock_path", "git_store_is_clean"]


def __getattr__(name: str) -> Any:
    """Avoid importing the CLI while ``python -m property_inventory.cli`` starts."""
    if name not in {"_CLI", "InventoryError", "canonical_lock_path", "git_store_is_clean"}:
        raise AttributeError(name)
    cli = import_module(".cli", __name__)
    if name == "_CLI":
        return cli
    return getattr(cli, name)
