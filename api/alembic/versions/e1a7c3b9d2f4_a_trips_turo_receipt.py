"""a trip's turo receipt

Revision ID: e1a7c3b9d2f4
Revises: d6f8a0c2e4b6
Create Date: 2026-10-11 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e1a7c3b9d2f4'
down_revision: Union[str, Sequence[str], None] = 'd6f8a0c2e4b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'trip', sa.Column('turo_receipt', postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column(
        'trip', sa.Column('receipt_synced_at', sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('trip', 'receipt_synced_at')
    op.drop_column('trip', 'turo_receipt')
