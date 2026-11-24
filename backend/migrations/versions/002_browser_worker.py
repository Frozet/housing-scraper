"""Lease for a local browser collector; existing listings and prices are unchanged."""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("collection_runs", sa.Column("browser_token", sa.String(32)))
    op.add_column("collection_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("collection_runs", "heartbeat_at")
    op.drop_column("collection_runs", "browser_token")
