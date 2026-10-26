"""Undoing a name Turo sends double-encoded.

Turo's reservation feed gave one guest's first name as "Ä°" plus five letters:
the two UTF-8 bytes of "İ" (C4 B0), each read as a single-byte character and
stored that way on Turo's side. The extension posts Turo's JSON untouched and
the API decodes it correctly, so the damage arrives in the data, and showed
on the trips and invoices pages as it came.

The repair re-encodes the text as cp1252 (or Latin-1) and reads the bytes as
UTF-8, and keeps the result only when that succeeds. It succeeds only for text
that is UTF-8 read the wrong way: a real accented name such as "José" encodes
to a lone 0xE9 byte, which is not valid UTF-8, and is left alone.
"""

from __future__ import annotations


def repair_mojibake(text: str) -> str:
    """`text` with UTF-8 read as a single-byte charset undone, else unchanged."""
    if not text or text.isascii():
        return text
    for codec in ("cp1252", "latin-1"):
        try:
            fixed = text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != text:
            return fixed
    return text
