"""German licence-plate (Kennzeichen) validation and normalization.

Structure: 1–3 letter Unterscheidungszeichen (district), 1–2 letter
Erkennungsnummer, up to 4 digits, optional E (Elektro) or H (Historic) suffix.
E.g. "B-AB 1234", "M-X 9", "GT-XY 12E". We validate the shape (not the district
registry) and normalize spacing/case so duplicates collapse.
"""
from __future__ import annotations

import re

# Umlauts are legal in the district code (e.g. "MÜ" for München-area plates).
_PATTERN = re.compile(
    r"^[A-ZÄÖÜ]{1,3}-[A-Z]{1,2}\s?\d{1,4}[EH]?$"
)


class InvalidKennzeichen(ValueError):
    """The plate does not match the DIN structure."""


def normalize_kennzeichen(raw: str) -> str:
    """Uppercase, collapse internal whitespace to one space, strip ends.
    ``' b-ab   1234 '`` → ``'B-AB 1234'``."""
    collapsed = re.sub(r"\s+", " ", raw.strip().upper())
    # Ensure exactly one space before the numeric group if the user omitted it:
    # "B-AB1234" → "B-AB 1234".
    m = re.match(r"^([A-ZÄÖÜ]{1,3}-[A-Z]{1,2})\s?(\d{1,4}[EH]?)$", collapsed)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return collapsed


def validate_kennzeichen(raw: str) -> str:
    """Return the normalized plate, or raise InvalidKennzeichen."""
    normalized = normalize_kennzeichen(raw)
    if not _PATTERN.match(normalized):
        raise InvalidKennzeichen(raw)
    return normalized
