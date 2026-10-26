"""repair guest names Turo sent double-encoded

Revision ID: c5e7a9b1d3f6
Revises: b4d6f8a0c2e4
Create Date: 2026-10-26 12:00:00.000000

The reservation feed stores names as they arrive, and one arrived as UTF-8
read as a single-byte charset ("Ä°" for "İ"). New ones are repaired on the way
in (turonomics_api/names.py); this repairs the ones already stored, with a
copy of the same rule so that a later change to it cannot change this. Only rows
with non-ASCII names are read, and the repair changes nothing that is not
UTF-8 read the wrong way, so running it twice is harmless.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5e7a9b1d3f6'
down_revision: Union[str, Sequence[str], None] = 'b4d6f8a0c2e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _repair(text: str) -> str:
    for codec in ("cp1252", "latin-1"):
        try:
            fixed = text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != text:
            return fixed
    return text


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, guest_name FROM trip WHERE guest_name ~ '[^\\x01-\\x7f]'")
    ).all()
    for trip_id, name in rows:
        fixed = _repair(name)
        if fixed != name:
            bind.execute(
                sa.text("UPDATE trip SET guest_name = :name WHERE id = :id"),
                {"name": fixed, "id": trip_id},
            )


def downgrade() -> None:
    """Downgrade schema.

    Nothing to undo: the old spelling was the damage.
    """
