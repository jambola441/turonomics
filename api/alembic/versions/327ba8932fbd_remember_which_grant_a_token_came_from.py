"""remember which grant a token came from

Nullable on purpose. The live row predates this column, and NULL is read as
"unknown authorization" — which makes the client re-exchange once and stamp it,
rather than needing a backfill or leaving the row permanently ambiguous.

Revision ID: 327ba8932fbd
Revises: 7fe2f16a146e
Create Date: 2026-10-03 17:54:03.842319

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '327ba8932fbd'
down_revision: Union[str, Sequence[str], None] = '7fe2f16a146e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "oauth_token", sa.Column("grant_fingerprint", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("oauth_token", "grant_fingerprint")
