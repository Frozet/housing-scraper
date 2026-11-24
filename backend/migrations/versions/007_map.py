"""House coordinates and a reusable city address index."""
import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = '007'
down_revision = '006'
branch_labels = None
depends_on = None


def upgrade():
    for table in ['cities', 'addresses']:
        op.add_column(table, sa.Column('latitude', sa.Numeric(10, 7)))
        op.add_column(table, sa.Column('longitude', sa.Numeric(10, 7)))
        op.create_check_constraint(f'ck_{table}_coordinates', table,
            '(latitude IS NULL AND longitude IS NULL) OR (latitude IS NOT NULL AND longitude IS NOT NULL AND latitude BETWEEN -85 AND 85 AND longitude BETWEEN -180 AND 180)')
    op.add_column('addresses', sa.Column('map_key', sa.String(500)))
    op.add_column('addresses', sa.Column('coordinate_source', sa.String(20)))
    op.add_column('addresses', sa.Column('geolocated_at', sa.DateTime(timezone=True)))
    op.create_index('ix_addresses_map_key', 'addresses', ['map_key'])
    bind = op.get_bind()
    for row in json.loads((Path(__file__).parents[2] / 'app/data/cities.json').read_text()):
        bind.execute(sa.text('UPDATE cities SET latitude=:latitude,longitude=:longitude WHERE external_id=:external_id'), row)
    op.create_table('map_buildings',
        sa.Column('city_id', sa.Integer, sa.ForeignKey('cities.id'), primary_key=True),
        sa.Column('key', sa.String(500), primary_key=True),
        sa.Column('latitude', sa.Numeric(10, 7), nullable=False),
        sa.Column('longitude', sa.Numeric(10, 7), nullable=False),
        sa.CheckConstraint('latitude BETWEEN -85 AND 85 AND longitude BETWEEN -180 AND 180'))
    op.create_table('map_imports',
        sa.Column('city_id', sa.Integer, sa.ForeignKey('cities.id'), primary_key=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True)),
        sa.Column('error', sa.Text), sa.Column('building_count', sa.Integer, nullable=False),
        sa.CheckConstraint("status IN ('queued','running','completed','failed')"))


def downgrade():
    op.drop_table('map_imports')
    op.drop_table('map_buildings')
    op.drop_index('ix_addresses_map_key', table_name='addresses')
    for field in ['map_key', 'coordinate_source', 'geolocated_at']:
        op.drop_column('addresses', field)
    for table in ['cities', 'addresses']:
        op.drop_constraint(f'ck_{table}_coordinates', table, type_='check')
        op.drop_column(table, 'latitude')
        op.drop_column(table, 'longitude')
