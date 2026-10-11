"""a trip's miles by the tracker

Revision ID: a3c5e7b9d1f2
Revises: f2b8d4c6a1e3
Create Date: 2026-10-11 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3c5e7b9d1f2'
down_revision: Union[str, Sequence[str], None] = 'f2b8d4c6a1e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('trip', sa.Column('tracker_miles', sa.Float(), nullable=True))
    op.add_column(
        'trip', sa.Column('tracker_miles_at', sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('trip', 'tracker_miles_at')
    op.drop_column('trip', 'tracker_miles')
