"""Licence plate normalisation.

The toll matcher joins Turo trips to EZPass transactions on plate. Both sides
must be normalised by the same rule or the join silently misses and a toll goes
unbilled — a failure that looks like "no tolls this month" rather than an
error, so it is worth having in exactly one place.

The registry normalises on write too, so a plate typed with a space or a dash
still matches a toll record that carries neither.
"""

from __future__ import annotations


def normalize_plate(value: str) -> str:
    """Upper-case, and strip the separators EZPass and Turo disagree about."""
    return value.upper().replace(" ", "").replace("-", "")


def normalize_optional_plate(value: str | None) -> str | None:
    """As :func:`normalize_plate`, but an empty result becomes ``None``.

    EZPass exports carry blank plate fields on payment rows, and "" is not a
    plate that should ever match anything.
    """
    if value is None:
        return None
    return normalize_plate(value) or None
