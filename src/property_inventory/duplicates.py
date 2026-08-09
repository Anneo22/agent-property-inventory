"""Deterministic duplicate candidate ranking, intentionally without linking."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .capture import CaptureError


class DuplicateError(CaptureError):
    """Raised when candidate-ranking inputs are malformed."""


def _normal_identifier(value: str, field: str) -> str:
    normalized = "".join(character for character in value.casefold() if character.isalnum())
    if not normalized:
        raise DuplicateError(f"{field} must retain at least one alphanumeric character")
    return normalized


def _tokens(value: str) -> frozenset[str]:
    return frozenset(token for token in re.findall(r"[a-z0-9]+", value.casefold()) if token)


def _identifiers(values: object, field: str) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise DuplicateError(f"{field} must be an exact tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise DuplicateError(f"{field} values must be non-empty strings")
    for value in values:
        _normal_identifier(value, field)
    return tuple(values)


@dataclass(frozen=True)
class DuplicateSubject:
    """Identity signals from an observation or canonical candidate, never a link instruction."""

    identifier: str
    serials: tuple[str, ...] = ()
    barcodes: tuple[str, ...] = ()
    model_identifiers: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    display_text: str = ""
    perceptual_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identifier, str) or not self.identifier.strip():
            raise DuplicateError("duplicate subject identifier must be non-empty")
        _normal_identifier(self.identifier, "identifier")
        for field in ("serials", "barcodes", "model_identifiers", "aliases"):
            object.__setattr__(self, field, _identifiers(getattr(self, field), field))
        if not isinstance(self.display_text, str):
            raise DuplicateError("display_text must be a string")
        if self.perceptual_hash is not None:
            if not isinstance(self.perceptual_hash, str) or not re.fullmatch(
                r"[0-9a-fA-F]+", self.perceptual_hash
            ):
                raise DuplicateError("perceptual_hash must be hexadecimal when supplied")
            object.__setattr__(self, "perceptual_hash", self.perceptual_hash.casefold())


@dataclass(frozen=True)
class DuplicateEvidence:
    """A human-reviewable score contribution with no mutable effect."""

    kind: str
    score: float
    detail: str


@dataclass(frozen=True)
class DuplicateCandidate:
    """A ranked candidate to review, not an update instruction."""

    candidate_id: str
    score: float
    evidence: tuple[DuplicateEvidence, ...]

    def __post_init__(self) -> None:
        _normal_identifier(self.candidate_id, "candidate_id")
        if type(self.evidence) is not tuple or any(
            not isinstance(item, DuplicateEvidence) for item in self.evidence
        ):
            raise DuplicateError("candidate evidence must be an immutable evidence tuple")


@dataclass(frozen=True)
class DuplicateRanking:
    """Deterministic review order only. Linking belongs to a separate explicit proposal."""

    candidates: tuple[DuplicateCandidate, ...]
    match_count: int

    def __post_init__(self) -> None:
        if type(self.candidates) is not tuple or any(
            not isinstance(item, DuplicateCandidate) for item in self.candidates
        ):
            raise DuplicateError("duplicate ranking candidates must be an immutable candidate tuple")
        if (
            isinstance(self.match_count, bool)
            or not isinstance(self.match_count, int)
            or self.match_count < len(self.candidates)
        ):
            raise DuplicateError("match_count must cover every returned candidate")


def _intersection(left: Sequence[str], right: Sequence[str], field: str) -> set[str]:
    return {_normal_identifier(value, field) for value in left} & {
        _normal_identifier(value, field) for value in right
    }


def _hamming_distance(left: str, right: str) -> int | None:
    """Return bit-level distance for equal-width hexadecimal perceptual hashes."""
    if len(left) != len(right):
        return None
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _candidate_evidence(observation: DuplicateSubject, candidate: DuplicateSubject) -> tuple[DuplicateEvidence, ...]:
    evidence: list[DuplicateEvidence] = []
    for kind, score, observed, known in (
        ("exact_serial", 1000.0, observation.serials, candidate.serials),
        ("exact_barcode", 900.0, observation.barcodes, candidate.barcodes),
        ("exact_model_identifier", 700.0, observation.model_identifiers, candidate.model_identifiers),
    ):
        matches = sorted(_intersection(observed, known, kind))
        if matches:
            evidence.append(DuplicateEvidence(kind, score, matches[0]))
    exact_aliases = sorted(_intersection(observation.aliases, candidate.aliases, "exact_alias"))
    if exact_aliases:
        evidence.append(DuplicateEvidence("exact_alias", 300.0, exact_aliases[0]))
    observed_tokens = _tokens(" ".join((observation.display_text, *observation.aliases)))
    candidate_tokens = _tokens(" ".join((candidate.display_text, *candidate.aliases)))
    if observed_tokens and candidate_tokens:
        overlap = observed_tokens & candidate_tokens
        if overlap:
            score = 100.0 * len(overlap) / len(observed_tokens | candidate_tokens)
            evidence.append(DuplicateEvidence("token_overlap", score, " ".join(sorted(overlap))))
    if observation.perceptual_hash and candidate.perceptual_hash:
        distance = _hamming_distance(observation.perceptual_hash, candidate.perceptual_hash)
        if distance is not None:
            score = max(0.0, 20.0 - float(distance))
            if score:
                evidence.append(DuplicateEvidence("perceptual_hash_candidate", score, str(distance)))
    return tuple(evidence)


def rank_duplicate_candidates(
    *,
    observation: DuplicateSubject,
    candidates: Iterable[DuplicateSubject],
    limit: int | None = None,
) -> DuplicateRanking:
    """Rank evidence-backed possible duplicates deterministically, never linking them."""
    if not isinstance(observation, DuplicateSubject):
        raise DuplicateError("observation must be a DuplicateSubject")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        raise DuplicateError("duplicate candidate limit must be a positive integer")
    ranked: list[DuplicateCandidate] = []
    match_count = 0
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, DuplicateSubject):
            raise DuplicateError("candidates must be DuplicateSubject values")
        if candidate.identifier in seen:
            raise DuplicateError("candidate identifiers must be unique")
        seen.add(candidate.identifier)
        evidence = _candidate_evidence(observation, candidate)
        score = sum(item.score for item in evidence)
        if score > 0:
            match_count += 1
            ranked.append(
                DuplicateCandidate(candidate_id=candidate.identifier, score=score, evidence=evidence)
            )
            ranked.sort(key=lambda result: (-result.score, result.candidate_id))
            if limit is not None and len(ranked) > limit:
                ranked.pop()
    if limit is None:
        ranked.sort(key=lambda result: (-result.score, result.candidate_id))
    return DuplicateRanking(candidates=tuple(ranked), match_count=match_count)
