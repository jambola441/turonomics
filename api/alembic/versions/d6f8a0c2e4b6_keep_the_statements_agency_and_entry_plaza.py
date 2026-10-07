"""keep the statement's agency and entry plaza

Revision ID: d6f8a0c2e4b6
Revises: c3e5f7a9b1d2
Create Date: 2026-10-07 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd6f8a0c2e4b6'
down_revision: Union[str, Sequence[str], None] = 'c3e5f7a9b1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('toll', sa.Column('agency', sa.String(length=40), nullable=True))
    op.add_column('toll', sa.Column('entry_plaza', sa.String(length=60), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('toll', 'entry_plaza')
    op.drop_column('toll', 'agency')
