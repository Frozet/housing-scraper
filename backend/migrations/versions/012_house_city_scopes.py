"""A house can belong to the search areas of several cities without duplicating prices."""
from alembic import op
import sqlalchemy as sa

revision = '012'
down_revision = '011'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('listing_city_scopes',
        sa.Column('listing_id', sa.Integer, sa.ForeignKey('listings.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('city_id', sa.Integer, sa.ForeignKey('cities.id'), primary_key=True),
        sa.Column('distance_km', sa.Numeric(9, 2)),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_listing_city_scopes_city', 'listing_city_scopes', ['city_id', 'listing_id'])
    op.create_table('listing_state_scopes',
        sa.Column('state_id', sa.Integer, sa.ForeignKey('listing_states.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('city_id', sa.Integer, sa.ForeignKey('cities.id'), primary_key=True),
        sa.Column('distance_km', sa.Numeric(9, 2)))
    op.execute('''INSERT INTO listing_city_scopes (listing_id,city_id,distance_km,first_seen_at)
        SELECT l.id,a.city_id,l.distance_to_center_km,l.first_seen_at FROM listings l JOIN addresses a ON a.id=l.address_id
        WHERE l.property_kind='house' ''')
    op.execute('''INSERT INTO listing_state_scopes (state_id,city_id,distance_km)
        SELECT s.id,a.city_id,s.distance_to_center_km FROM listing_states s JOIN addresses a ON a.id=s.address_id
        WHERE s.property_kind='house' ''')


def downgrade():
    raise RuntimeError('Откат удалит принадлежность домов к городам и её историю. Восстановите резервную копию.')
