"""NYC parking and camera tickets, by plate

Revision ID: b8d0f2a4c6e8
Revises: a6c8e0b2d4f5
Create Date: 2026-11-13 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d0f2a4c6e8'
down_revision: Union[str, Sequence[str], None] = 'a6c8e0b2d4f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'ticket',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('summons_number', sa.String(length=20), nullable=False),
        sa.Column('plate', sa.String(length=16), nullable=False),
        sa.Column('plate_state', sa.String(length=4), nullable=True),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('violation', sa.String(length=80), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('time_known', sa.Boolean(), nullable=False),
        sa.Column('county', sa.String(length=20), nullable=True),
        sa.Column('precinct', sa.String(length=10), nullable=True),
        sa.Column('issuing_agency', sa.String(length=60), nullable=True),
        sa.Column('fine_cents', sa.Integer(), nullable=False),
        sa.Column('penalty_cents', sa.Integer(), nullable=False),
        sa.Column('interest_cents', sa.Integer(), nullable=False),
        sa.Column('reduction_cents', sa.Integer(), nullable=False),
        sa.Column('paid_cents', sa.Integer(), nullable=False),
        sa.Column('due_cents', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=60), nullable=True),
        sa.Column('image_url', sa.Text(), nullable=True),
        sa.Column('vehicle_id', sa.Uuid(), nullable=True),
        sa.Column('trip_id', sa.Uuid(), nullable=True),
        sa.Column('filed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('recovered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['vehicle_id'], ['vehicle.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['trip_id'], ['trip.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('summons_number'),
    )
    op.create_index('ix_ticket_plate', 'ticket', ['plate'])
    op.create_index('ix_ticket_issued_at', 'ticket', ['issued_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_ticket_issued_at', table_name='ticket')
    op.drop_index('ix_ticket_plate', table_name='ticket')
    op.drop_table('ticket')
