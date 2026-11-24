"""Durable catalog cursor and listing queue, independent of browser sessions."""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("crawls",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("catalog_url", sa.Text, nullable=False),
        sa.Column("next_page", sa.Integer, nullable=False),
        sa.Column("skip_existing_before", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('active','completed','abandoned')"),
        sa.CheckConstraint("next_page > 0"))
    op.create_index("uq_one_active_crawl", "crawls", [sa.text("(1)")], unique=True,
                    postgresql_where=sa.text("status = 'active'"))
    op.create_table("crawl_pages",
        sa.Column("crawl_id", sa.Integer, sa.ForeignKey("crawls.id"), primary_key=True),
        sa.Column("number", sa.Integer, primary_key=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("has_next", sa.Boolean),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False))
    op.add_column("collection_runs", sa.Column("crawl_id", sa.Integer))
    op.create_foreign_key("fk_run_crawl", "collection_runs", "crawls", ["crawl_id"], ["id"])
    op.create_table("crawl_items",
        sa.Column("crawl_id", sa.Integer, sa.ForeignKey("crawls.id"), primary_key=True),
        sa.Column("avito_id", sa.String(30), primary_key=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("page_number", sa.Integer, nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("last_run_id", sa.Integer, sa.ForeignKey("collection_runs.id")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending','saved','skipped','error')"),
        sa.ForeignKeyConstraint(["crawl_id", "page_number"], ["crawl_pages.crawl_id", "crawl_pages.number"]))
    op.create_index("ix_crawl_queue", "crawl_items", ["crawl_id", "status", "page_number", "position"])
    op.execute("""UPDATE collection_runs SET status = 'interrupted', browser_token = NULL,
                  finished_at = now(), message = 'Сборщик обновлён. Перезапустите его и нажмите «Продолжить сбор».'
                  WHERE status IN ('queued', 'running') AND crawl_id IS NULL""")


def downgrade():
    op.drop_table("crawl_items")
    op.drop_constraint("fk_run_crawl", "collection_runs", type_="foreignkey")
    op.drop_column("collection_runs", "crawl_id")
    op.drop_table("crawl_pages")
    op.drop_table("crawls")
