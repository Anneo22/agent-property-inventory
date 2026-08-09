"""Shared evidence policy for durable valuation facts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def valuation_evidence_supports_basis(evidence: Mapping[str, Any], basis: object) -> bool:
    """Return whether evidence semantics can support the named value basis."""
    if basis in {"replacement", "market", "other"}:
        return evidence.get("claim_strength") == "research_only" and evidence.get(
            "evidence_type"
        ) in {"research", "user_source", "vault_note"}
    if basis == "appraisal":
        return evidence.get("claim_strength") == "research_only" and evidence.get(
            "evidence_type"
        ) in {"user_source", "vault_note"}
    if basis in {"purchase", "receipt"}:
        return evidence.get("claim_strength") == "purchase_only" and evidence.get(
            "evidence_type"
        ) in {"merchant_account", "user_source", "finance_sheet"}
    # Preserve unknown historical bases only as audit context. Callers decide
    # separately which named bases can affect readiness.
    return True


__all__ = ["valuation_evidence_supports_basis"]
