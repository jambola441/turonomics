"""commands the site queues for the extension

Revision ID: 7c2e9a41d0b3
Revises: 1f319c265255
Create Date: 2026-10-06 14:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c2e9a41d0b3'
down_revision: Union[str, Sequence[str], None] = '1f319c265255'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'extension_command',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('trip_id', sa.Uuid(), nullable=True),
        sa.Column('state', sa.String(length=12), nullable=False),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('result', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['trip_id'], ['trip.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_extension_command_state'), 'extension_command', ['state'], unique=False
    )
    op.create_table(
        'extension_checkin',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('version', sa.String(length=20), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('extension_checkin')
    op.drop_index(op.f('ix_extension_command_state'), table_name='extension_command')
    op.drop_table('extension_command')
