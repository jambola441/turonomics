"""filing proposals, filed only when the operator presses File

Revision ID: f4a6c8e0b2d3
Revises: e9b1d3f5a7c2
Create Date: 2026-10-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a6c8e0b2d3'
down_revision: Union[str, Sequence[str], None] = 'e9b1d3f5a7c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'filing_proposal',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('trip_id', sa.Uuid(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(length=12), nullable=False),
        sa.Column('created_by', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('command_id', sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(['trip_id'], ['trip.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['command_id'], ['extension_command.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_filing_proposal_trip_id', 'filing_proposal', ['trip_id'])
    op.create_index('ix_filing_proposal_state', 'filing_proposal', ['state'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_filing_proposal_state', table_name='filing_proposal')
    op.drop_index('ix_filing_proposal_trip_id', table_name='filing_proposal')
    op.drop_table('filing_proposal')
