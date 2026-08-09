"""Small byte-level checks for media used as insurance evidence."""

from __future__ import annotations

import warnings
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError


class MediaValidationError(ValueError):
    """Raised when declared media semantics disagree with the actual bytes."""


_MIME_ALIASES = {"image/jpg": "image/jpeg", "image/x-png": "image/png"}
MAX_VALIDATED_MEDIA_BYTES = 64 * 1024 * 1024


def normalized_media_type(value: str) -> str:
    """Return one lowercase MIME token without optional parameters."""
    media_type = value.split(";", 1)[0].strip().casefold()
    return _MIME_ALIASES.get(media_type, media_type)


def _decoded_image_type(payload: bytes) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as image:
                image_format = image.format
                image.verify()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombWarning) as error:
        # ISO-BMFF brands are only a container hint.  In particular, a forged
        # 16-byte ``ftyp`` header must not become insurance evidence without a
        # decoder proving that the payload contains an image.  We deliberately
        # fail closed when Pillow (or an installed codec plugin) cannot decode
        # HEIF/HEIC/AVIF rather than accepting the brand alone.
        raise MediaValidationError("declared image bytes cannot be decoded and verified") from error
    detected = Image.MIME.get(image_format or "")
    if not detected:
        raise MediaValidationError("decoded image format has no supported MIME type")
    return normalized_media_type(detected)


def _validate_pdf(payload: bytes) -> None:
    if not payload[:8].startswith(b"%PDF-") or b"%%EOF" not in payload[-2048:]:
        raise MediaValidationError("declared PDF bytes have no valid PDF envelope")
    try:
        reader = PdfReader(BytesIO(payload), strict=True)
        if reader.is_encrypted or not reader.pages:
            raise MediaValidationError(
                "declared PDF must be readable and contain at least one page"
            )
        # Resolve the catalogue while the bounded in-memory source is alive.
        reader.root_object
    except (OSError, PdfReadError, TypeError, ValueError) as error:
        raise MediaValidationError("declared PDF bytes cannot be parsed") from error


def validate_declared_media_bytes(
    payload: bytes, media_type: str, *, document_only: bool = False
) -> str:
    """Validate bytes for image/PDF claims and return the normalized MIME type.

    Other media may be stored as ordinary evidence. Receipt and appraisal roles
    set ``document_only`` so an arbitrary text file cannot become readiness
    evidence merely by choosing a role.
    """
    normalized = normalized_media_type(media_type)
    if normalized.startswith("image/"):
        detected = _decoded_image_type(payload)
        compatible = detected == normalized or {
            detected,
            normalized,
        } <= {"image/heic", "image/heif"}
        if not compatible:
            raise MediaValidationError(
                f"declared media type {normalized} disagrees with decoded {detected}"
            )
        return normalized
    if normalized == "application/pdf":
        _validate_pdf(payload)
        return normalized
    if document_only:
        raise MediaValidationError("receipt and appraisal evidence must be an image or PDF")
    return normalized


def validate_declared_media(path: Path, media_type: str, *, document_only: bool = False) -> str:
    """Read one bounded file and validate its bytes against the declared role."""
    try:
        if path.stat().st_size > MAX_VALIDATED_MEDIA_BYTES:
            raise MediaValidationError("declared image or document exceeds the validation limit")
        payload = path.read_bytes()
    except OSError as error:
        raise MediaValidationError(f"cannot inspect declared media bytes: {error}") from error
    return validate_declared_media_bytes(payload, media_type, document_only=document_only)


def declared_media_matches(path: Path, media_type: str) -> bool:
    """Return whether a canonical file still matches its image/PDF declaration."""
    try:
        validate_declared_media(path, media_type)
    except (MediaValidationError, OSError):
        return False
    return True


__all__ = [
    "MediaValidationError",
    "MAX_VALIDATED_MEDIA_BYTES",
    "declared_media_matches",
    "normalized_media_type",
    "validate_declared_media",
    "validate_declared_media_bytes",
]
