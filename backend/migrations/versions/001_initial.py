"""Нормализованные объявления, справочники, очередь и история цен."""
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("cities", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("name", sa.String(100), nullable=False, unique=True))
    op.create_table("addresses", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("city_id", sa.Integer, sa.ForeignKey("cities.id"), nullable=False),
                    sa.Column("text", sa.Text, nullable=False), sa.UniqueConstraint("city_id", "text"))
    op.create_table("residential_complexes", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("city_id", sa.Integer, sa.ForeignKey("cities.id"), nullable=False),
                    sa.Column("name", sa.String(500), nullable=False), sa.UniqueConstraint("city_id", "name"))
    op.create_table("market_types", sa.Column("code", sa.String(20), primary_key=True),
                    sa.Column("name", sa.String(50), nullable=False, unique=True))
    op.create_table("feature_types", sa.Column("code", sa.String(30), primary_key=True),
                    sa.Column("name", sa.String(100), nullable=False, unique=True))
    op.create_table("feature_values", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("type_code", sa.String(30), sa.ForeignKey("feature_types.code"), nullable=False),
                    sa.Column("value", sa.String(1000), nullable=False), sa.UniqueConstraint("type_code", "value"))
    op.create_table("listings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("avito_id", sa.String(30), nullable=False, unique=True),
        sa.Column("url", sa.Text, nullable=False), sa.Column("title", sa.Text, nullable=False),
        sa.Column("market_type_code", sa.String(20), sa.ForeignKey("market_types.code"), nullable=False),
        sa.Column("address_id", sa.Integer, sa.ForeignKey("addresses.id"), nullable=False),
        sa.Column("complex_id", sa.Integer, sa.ForeignKey("residential_complexes.id")),
        sa.Column("rooms", sa.Integer, nullable=False), sa.Column("total_area", sa.Numeric(10, 2), nullable=False),
        sa.Column("kitchen_area", sa.Numeric(10, 2)), sa.Column("living_area", sa.Numeric(10, 2)),
        sa.Column("floor", sa.Integer), sa.Column("floors_total", sa.Integer),
        sa.Column("ceiling_height", sa.Numeric(6, 2)),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rooms >= 0"), sa.CheckConstraint("total_area > 0"),
        sa.CheckConstraint("kitchen_area IS NULL OR kitchen_area > 0"),
        sa.CheckConstraint("living_area IS NULL OR living_area > 0"),
        sa.CheckConstraint("ceiling_height IS NULL OR ceiling_height > 0"),
        sa.CheckConstraint("floors_total IS NULL OR floors_total > 0"),
        sa.CheckConstraint("floor IS NULL OR floors_total IS NULL OR floor <= floors_total"))
    op.create_index("ix_listings_last_seen_at", "listings", ["last_seen_at"])
    op.create_table("listing_features",
        sa.Column("listing_id", sa.Integer, sa.ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("value_id", sa.Integer, sa.ForeignKey("feature_values.id"), primary_key=True))
    op.create_table("collection_runs",
        sa.Column("id", sa.Integer, primary_key=True), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)),
        *[sa.Column(name, sa.Integer, nullable=False) for name in ("pages", "found", "saved", "errors", "price_changes")],
        sa.Column("message", sa.Text, nullable=False),
        sa.CheckConstraint("status IN ('queued','running','completed','partial','blocked','failed','interrupted')"))
    op.create_index("uq_one_active_run", "collection_runs", [sa.text("(1)")], unique=True,
                    postgresql_where=sa.text("status IN ('queued','running')"))
    op.create_table("price_observations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("listing_id", sa.Integer, sa.ForeignKey("listings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("collection_runs.id")),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)), sa.CheckConstraint("amount > 0"))
    op.create_index("ix_prices_listing_time", "price_observations", ["listing_id", "observed_at", "id"])
    op.bulk_insert(sa.table("market_types", sa.column("code"), sa.column("name")), [
        {"code": "secondary", "name": "Вторичка"}, {"code": "new", "name": "Новостройка"}])
    names = {"balcony": "Балкон или лоджия", "extras": "Дополнительно", "room_type": "Тип комнат",
             "bathroom": "Санузел", "windows": "Окна", "finish": "Отделка",
             "sale_method": "Способ продажи", "sale_conditions": "Условия продажи"}
    op.bulk_insert(sa.table("feature_types", sa.column("code"), sa.column("name")),
                   [{"code": code, "name": name} for code, name in names.items()])


def downgrade():
    for name in ("price_observations", "collection_runs", "listing_features", "listings", "feature_values",
                 "feature_types", "market_types", "residential_complexes", "addresses", "cities"):
        op.drop_table(name)
