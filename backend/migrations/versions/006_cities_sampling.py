"""City-scoped crawls and durable stratified sample planning."""
import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("cities_name_key", "cities", type_="unique")
    op.add_column("cities", sa.Column("region", sa.String(150), nullable=False, server_default=""))
    op.add_column("cities", sa.Column("external_id", sa.String(36)))
    op.add_column("cities", sa.Column("slug", sa.String(200)))
    op.create_unique_constraint("uq_city_name_region", "cities", ["name", "region"])
    op.create_unique_constraint("uq_city_external", "cities", ["external_id"])
    bind = op.get_bind()
    for row in json.loads((Path(__file__).parents[2] / "app/data/cities.json").read_text()):
        existing = bind.scalar(sa.text("SELECT id FROM cities WHERE name=:name AND region=''"), row)
        if existing is not None:
            bind.execute(sa.text("UPDATE cities SET region=:region, external_id=:external_id, slug=:slug WHERE id=:id"), {**row, "id": existing})
        else:
            bind.execute(sa.text("INSERT INTO cities(name,region,external_id,slug) VALUES (:name,:region,:external_id,:slug)"), row)
    op.add_column("crawls", sa.Column("city_id", sa.Integer, sa.ForeignKey("cities.id")))
    op.add_column("crawls", sa.Column("mode", sa.String(20), nullable=False, server_default="full"))
    op.execute("UPDATE crawls SET city_id=(SELECT id FROM cities WHERE name='Иркутск' LIMIT 1)")
    op.create_check_constraint("ck_crawl_mode", "crawls", "mode IN ('full','sample')")
    op.drop_index("uq_one_active_crawl", table_name="crawls")
    op.create_index("uq_active_city_mode_crawl", "crawls", ["city_id", "mode"], unique=True, postgresql_where=sa.text("status = 'active'"))
    op.create_table("sample_plans",
        sa.Column("crawl_id", sa.Integer, sa.ForeignKey("crawls.id"), primary_key=True),
        sa.Column("total_count", sa.Integer), sa.Column("upper_price", sa.BigInteger, nullable=False),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.CheckConstraint("total_count IS NULL OR total_count >= 0"),
        sa.CheckConstraint("upper_price > 0"), sa.CheckConstraint("phase IN ('count','expand','boundaries','collect')"))
    op.create_table("sample_boundaries",
        sa.Column("crawl_id", sa.Integer, sa.ForeignKey("sample_plans.crawl_id"), primary_key=True),
        sa.Column("number", sa.Integer, primary_key=True), sa.Column("low", sa.BigInteger, nullable=False),
        sa.Column("high", sa.BigInteger, nullable=False), sa.Column("price", sa.BigInteger),
        sa.CheckConstraint("number BETWEEN 1 AND 9"), sa.CheckConstraint("low > 0 AND high >= low"))
    op.create_table("sample_probes",
        sa.Column("crawl_id", sa.Integer, sa.ForeignKey("sample_plans.crawl_id"), primary_key=True),
        sa.Column("maximum", sa.BigInteger, primary_key=True), sa.Column("count", sa.Integer, nullable=False),
        sa.CheckConstraint("maximum > 0 AND count >= 0"))
    op.drop_constraint("crawl_items_status_check", "crawl_items", type_="check")
    op.create_check_constraint("ck_crawl_item_status", "crawl_items", "status IN ('pending','saved','skipped','error','not_selected')")


def downgrade():
    raise RuntimeError("Откат объединит города и потеряет прогресс выборок. Восстановите резервную копию.")
