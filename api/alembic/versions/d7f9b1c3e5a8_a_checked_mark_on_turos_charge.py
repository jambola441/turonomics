"""a checked mark on what Turo already charged

Revision ID: d7f9b1c3e5a8
Revises: c5e7a9b1d3f6
Create Date: 2026-10-26 12:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7f9b1c3e5a8'
down_revision: Union[str, Sequence[str], None] = 'c5e7a9b1d3f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('trip', sa.Column('turo_charge_checked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('trip', sa.Column('turo_charge_checked_cents', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('trip', 'turo_charge_checked_cents')
    op.drop_column('trip', 'turo_charge_checked_at')
