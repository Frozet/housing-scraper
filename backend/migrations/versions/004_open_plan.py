"""Allow an explicitly free layout without inventing a room count."""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("listings", sa.Column("is_open_plan", sa.Boolean, nullable=False, server_default=sa.false()))
    op.alter_column("listings", "rooms", existing_type=sa.Integer, nullable=True)
    op.create_check_constraint("ck_listings_rooms_known_or_open_plan", "listings", "rooms IS NOT NULL OR is_open_plan")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM listings WHERE is_open_plan)")):
        raise RuntimeError("Откат невозможен без потери данных о свободной планировке.")
    op.drop_constraint("ck_listings_rooms_known_or_open_plan", "listings", type_="check")
    op.alter_column("listings", "rooms", existing_type=sa.Integer, nullable=False)
    op.drop_column("listings", "is_open_plan")
