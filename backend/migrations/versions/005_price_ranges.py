"""Independent price-range cursors with one deduplicated queue per sweep."""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("crawl_ranges",
        sa.Column("crawl_id", sa.Integer, sa.ForeignKey("crawls.id"), primary_key=True),
        sa.Column("number", sa.Integer, primary_key=True),
        sa.Column("parent_number", sa.Integer),
        sa.Column("minimum", sa.BigInteger, nullable=False),
        sa.Column("maximum", sa.BigInteger),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("next_page", sa.Integer, nullable=False),
        sa.CheckConstraint("status IN ('pending','active','completed','split','limited')"),
        sa.CheckConstraint("minimum >= 0 AND (maximum IS NULL OR maximum >= minimum)"),
        sa.CheckConstraint("next_page > 0"),
        sa.ForeignKeyConstraint(["crawl_id", "parent_number"], ["crawl_ranges.crawl_id", "crawl_ranges.number"]))
    op.create_index("uq_active_crawl_range", "crawl_ranges", ["crawl_id"], unique=True,
                    postgresql_where=sa.text("status = 'active'"))
    # A previously 'completed' 30-page query still needs the partitioned pass.
    # Reopen only the latest sweep; retain every saved ID and price observation.
    op.execute("""UPDATE crawls SET status = 'active', finished_at = NULL
                  WHERE id = (SELECT max(id) FROM crawls) AND status = 'completed'
                  AND EXISTS (SELECT 1 FROM crawl_pages p WHERE p.crawl_id = crawls.id AND p.number >= 30)""")
    op.execute("""UPDATE crawls SET skip_existing_before = COALESCE(skip_existing_before, now())
                  WHERE id = (SELECT max(id) FROM crawls) AND status = 'active'""")
    op.execute("""INSERT INTO crawl_ranges (crawl_id, number, minimum, maximum, status, next_page)
                  SELECT id, 1, 1, NULL, CASE WHEN status = 'completed' THEN 'completed' ELSE 'active' END, next_page
                  FROM crawls""")
    op.add_column("crawl_pages", sa.Column("range_number", sa.Integer))
    op.add_column("crawl_items", sa.Column("range_number", sa.Integer))
    op.execute("UPDATE crawl_pages SET range_number = 1")
    op.execute("UPDATE crawl_items SET range_number = 1")
    op.alter_column("crawl_pages", "range_number", nullable=False)
    op.alter_column("crawl_items", "range_number", nullable=False)
    op.drop_constraint("crawl_items_crawl_id_page_number_fkey", "crawl_items", type_="foreignkey")
    op.drop_constraint("crawl_pages_crawl_id_fkey", "crawl_pages", type_="foreignkey")
    op.drop_constraint("crawl_pages_pkey", "crawl_pages", type_="primary")
    op.create_primary_key("crawl_pages_pkey", "crawl_pages", ["crawl_id", "range_number", "number"])
    op.create_foreign_key("fk_page_range", "crawl_pages", "crawl_ranges", ["crawl_id", "range_number"], ["crawl_id", "number"])
    op.create_foreign_key("fk_item_page", "crawl_items", "crawl_pages",
                         ["crawl_id", "range_number", "page_number"], ["crawl_id", "range_number", "number"])
    op.drop_column("crawls", "next_page")
    op.execute("""UPDATE collection_runs SET status = 'interrupted', browser_token = NULL, finished_at = now(),
                  message = 'Добавлен обход диапазонов цен. Перезапустите сборщик и нажмите «Продолжить сбор».'
                  WHERE status IN ('queued', 'running')""")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM crawl_ranges WHERE number <> 1)")):
        raise RuntimeError("Откат невозможен без потери прогресса диапазонов цен.")
    op.add_column("crawls", sa.Column("next_page", sa.Integer))
    op.execute("UPDATE crawls SET next_page = r.next_page FROM crawl_ranges r WHERE r.crawl_id = crawls.id")
    op.alter_column("crawls", "next_page", nullable=False)
    op.create_check_constraint("crawls_next_page_check", "crawls", "next_page > 0")
    op.drop_constraint("fk_item_page", "crawl_items", type_="foreignkey")
    op.drop_constraint("fk_page_range", "crawl_pages", type_="foreignkey")
    op.drop_constraint("crawl_pages_pkey", "crawl_pages", type_="primary")
    op.drop_column("crawl_items", "range_number")
    op.drop_column("crawl_pages", "range_number")
    op.create_primary_key("crawl_pages_pkey", "crawl_pages", ["crawl_id", "number"])
    op.create_foreign_key("crawl_pages_crawl_id_fkey", "crawl_pages", "crawls", ["crawl_id"], ["id"])
    op.create_foreign_key("crawl_items_crawl_id_page_number_fkey", "crawl_items", "crawl_pages",
                         ["crawl_id", "page_number"], ["crawl_id", "number"])
    op.drop_table("crawl_ranges")
