"""Persist listing read retries and append-only duplicate review decisions."""
from alembic import op
import sqlalchemy as sa

revision = '014'
down_revision = '013'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('crawl_items', sa.Column('read_attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('collection_runs', sa.Column('queue_revision', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('map_imports', sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('map_imports', sa.Column('retry_at', sa.DateTime(timezone=True)))
    op.create_index('ix_crawl_pending_order', 'crawl_items',
        ['crawl_id', 'range_number', 'page_number', 'position'], postgresql_where=sa.text("status = 'pending'"))
    op.execute('ALTER TABLE duplicate_reviews ADD COLUMN id BIGSERIAL NOT NULL')
    op.drop_constraint('duplicate_reviews_pkey', 'duplicate_reviews', type_='primary')
    op.create_primary_key('duplicate_reviews_pkey', 'duplicate_reviews', ['id'])
    op.create_index('ix_duplicate_review_pair_time', 'duplicate_reviews',
        ['left_id', 'right_id', 'reviewed_at', 'id'])


def downgrade():
    raise RuntimeError('Откат может потерять историю ручных решений. Восстановите резервную копию.')
