"""Kennzeichen (German plate) validation/normalization — pure logic."""
from __future__ import annotations

import pytest

from app.web.kennzeichen import (
    InvalidKennzeichen,
    normalize_kennzeichen,
    validate_kennzeichen,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("b-ab 1234", "B-AB 1234"),
        ("  B-AB   1234 ", "B-AB 1234"),
        ("B-AB1234", "B-AB 1234"),        # missing space inserted
        ("M-X 9", "M-X 9"),
        ("GT-XY 12E", "GT-XY 12E"),       # E-Kennzeichen
        ("MÜ-AB 100H", "MÜ-AB 100H"),     # umlaut district + Historic
    ],
)
def test_valid_plates_normalize(raw: str, expected: str) -> None:
    assert validate_kennzeichen(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "ABCD-AB 1234", "B-ABC 1234", "B-AB 12345", "1234", "B_AB 1234"],
)
def test_invalid_plates_rejected(raw: str) -> None:
    with pytest.raises(InvalidKennzeichen):
        validate_kennzeichen(raw)


def test_normalize_is_idempotent() -> None:
    once = normalize_kennzeichen("b-ab1234")
    assert normalize_kennzeichen(once) == once
