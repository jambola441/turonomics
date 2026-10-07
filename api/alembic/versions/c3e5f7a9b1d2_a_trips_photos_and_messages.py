"""a trip's photos and messages

Revision ID: c3e5f7a9b1d2
Revises: a4d81f6c2e90
Create Date: 2026-10-07 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c3e5f7a9b1d2'
down_revision: Union[str, Sequence[str], None] = 'a4d81f6c2e90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'trip', sa.Column('turo_photos', postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column(
        'trip', sa.Column('turo_messages', postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column(
        'trip', sa.Column('extras_synced_at', sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('trip', 'extras_synced_at')
    op.drop_column('trip', 'turo_messages')
    op.drop_column('trip', 'turo_photos')
