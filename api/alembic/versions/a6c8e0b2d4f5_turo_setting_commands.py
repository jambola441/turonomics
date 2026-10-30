"""turo setting changes ride on extension commands

Revision ID: a6c8e0b2d4f5
Revises: f4a6c8e0b2d3
Create Date: 2026-10-30 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a6c8e0b2d4f5'
down_revision: Union[str, Sequence[str], None] = 'f4a6c8e0b2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('extension_command',
                  sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('extension_command',
                  sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('extension_command', 'detail')
    op.drop_column('extension_command', 'payload')
