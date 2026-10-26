"""message drafts, sent only once approved

Revision ID: e9b1d3f5a7c2
Revises: d7f9b1c3e5a8
Create Date: 2026-10-26 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e9b1d3f5a7c2'
down_revision: Union[str, Sequence[str], None] = 'd7f9b1c3e5a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'message_draft',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('trip_id', sa.Uuid(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('state', sa.String(length=12), nullable=False),
        sa.Column('created_by', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('command_id', sa.Uuid(), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('result', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['trip_id'], ['trip.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['command_id'], ['extension_command.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_message_draft_trip_id', 'message_draft', ['trip_id'])
    op.create_index('ix_message_draft_state', 'message_draft', ['state'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_message_draft_state', table_name='message_draft')
    op.drop_index('ix_message_draft_trip_id', table_name='message_draft')
    op.drop_table('message_draft')
