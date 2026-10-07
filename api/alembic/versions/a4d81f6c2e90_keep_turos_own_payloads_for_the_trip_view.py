"""keep Turo's own payloads for the trip view

Revision ID: a4d81f6c2e90
Revises: 7c2e9a41d0b3
Create Date: 2026-10-07 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a4d81f6c2e90'
down_revision: Union[str, Sequence[str], None] = '7c2e9a41d0b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'trip',
        sa.Column('turo_detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'reimbursement_invoice',
        sa.Column('turo_body', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('reimbursement_invoice', 'turo_body')
    op.drop_column('trip', 'turo_detail')
