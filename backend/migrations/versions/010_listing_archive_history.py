"""Archive status, saved-listing refresh jobs, and normalized attribute history."""
from alembic import op
import sqlalchemy as sa

revision = '010'
down_revision = '009'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('listings', sa.Column('is_archived', sa.Boolean, nullable=False, server_default=sa.text('false')))
    op.add_column('listings', sa.Column('archived_at', sa.DateTime(timezone=True)))
    op.add_column('listings', sa.Column('archive_reason', sa.Text))
    op.create_index('ix_listings_is_archived', 'listings', ['is_archived'])
    op.add_column('collection_runs', sa.Column('archived', sa.Integer, nullable=False, server_default='0'))
    op.drop_constraint('ck_crawl_mode', 'crawls', type_='check')
    op.create_check_constraint('ck_crawl_mode', 'crawls', "mode IN ('full','sample','refresh')")
    op.create_table('listing_states',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('listing_id', sa.Integer, sa.ForeignKey('listings.id', ondelete='CASCADE'), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('url', sa.Text, nullable=False), sa.Column('title', sa.Text, nullable=False),
        sa.Column('market_type_code', sa.String(20), sa.ForeignKey('market_types.code'), nullable=False),
        sa.Column('address_id', sa.Integer, sa.ForeignKey('addresses.id'), nullable=False),
        sa.Column('complex_id', sa.Integer, sa.ForeignKey('residential_complexes.id')),
        sa.Column('rooms', sa.Integer), sa.Column('is_open_plan', sa.Boolean, nullable=False),
        sa.Column('total_area', sa.Numeric(10, 2), nullable=False),
        sa.Column('kitchen_area', sa.Numeric(10, 2)), sa.Column('living_area', sa.Numeric(10, 2)),
        sa.Column('floor', sa.Integer), sa.Column('floors_total', sa.Integer),
        sa.Column('ceiling_height', sa.Numeric(6, 2)), sa.Column('construction_year', sa.Integer),
        sa.Column('source_updated_at', sa.DateTime(timezone=True)),
        sa.Column('is_archived', sa.Boolean, nullable=False),
        sa.Column('archived_at', sa.DateTime(timezone=True)), sa.Column('archive_reason', sa.Text))
    op.create_index('ix_listing_states_time', 'listing_states', ['listing_id', 'observed_at', 'id'])
    op.create_table('listing_state_features',
        sa.Column('state_id', sa.Integer, sa.ForeignKey('listing_states.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('value_id', sa.Integer, sa.ForeignKey('feature_values.id'), primary_key=True))
    # Only the most recently observed attributes of old records are known. Do not
    # backdate them to first_seen_at or fabricate snapshots for older price rows.
    op.execute('''INSERT INTO listing_states
        (listing_id, observed_at, url, title, market_type_code, address_id, complex_id,
         rooms, is_open_plan, total_area, kitchen_area, living_area, floor, floors_total,
         ceiling_height, construction_year, source_updated_at, is_archived, archived_at, archive_reason)
        SELECT id, last_seen_at, url, title, market_type_code, address_id, complex_id,
         rooms, is_open_plan, total_area, kitchen_area, living_area, floor, floors_total,
         ceiling_height, construction_year, source_updated_at, is_archived, archived_at, archive_reason
        FROM listings''')
    op.execute('''INSERT INTO listing_state_features (state_id, value_id)
        SELECT s.id, f.value_id FROM listing_states s JOIN listing_features f ON f.listing_id=s.listing_id''')


def downgrade():
    raise RuntimeError('Откат удалит историю характеристик и статусы архива. Восстановите резервную копию.')
