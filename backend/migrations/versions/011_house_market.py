"""Independent house market, normalized amenities and city search preferences."""
import json
from pathlib import Path
from alembic import op
import sqlalchemy as sa

revision = '011'
down_revision = '010'
branch_labels = None
depends_on = None

HOUSE_FEATURES = {
    'object_type': 'Вид объекта', 'readiness': 'Готовность', 'land_category': 'Категория земель',
    'wall_material': 'Материал стен', 'terrace': 'Терраса или веранда',
    'electricity': 'Электричество', 'heating': 'Отопление', 'water_supply': 'Водоснабжение',
    'gas': 'Газ', 'sewerage': 'Канализация', 'internet_tv': 'Интернет и ТВ',
    'communications': 'Коммуникации', 'recreation': 'Для отдыха',
    'transport_access': 'Транспортная доступность', 'infrastructure': 'Инфраструктура',
}


def upgrade():
    op.create_table('property_kinds', sa.Column('code', sa.String(20), primary_key=True),
                    sa.Column('name', sa.String(50), nullable=False, unique=True))
    op.execute("INSERT INTO property_kinds VALUES ('apartment','Квартиры'),('house','Дома')")
    for table in ('listings', 'listing_states', 'crawls'):
        op.add_column(table, sa.Column('property_kind', sa.String(20), sa.ForeignKey('property_kinds.code'),
                                      nullable=False, server_default='apartment'))
    for table in ('listings', 'listing_states'):
        op.add_column(table, sa.Column('land_area', sa.Numeric(12, 2)))
        op.add_column(table, sa.Column('distance_to_center_km', sa.Numeric(9, 2)))
        op.add_column(table, sa.Column('source_distance_km', sa.Numeric(9, 2)))
        op.create_check_constraint('ck_' + table + '_land_area', table, 'land_area IS NULL OR land_area > 0')
        op.create_check_constraint('ck_' + table + '_distance', table, 'distance_to_center_km IS NULL OR distance_to_center_km >= 0')
    op.drop_constraint('ck_listings_rooms_known_or_open_plan', 'listings', type_='check')
    op.create_check_constraint('ck_listings_rooms_known_or_open_plan', 'listings',
                              "rooms IS NOT NULL OR is_open_plan OR property_kind = 'house'")
    op.create_index('ix_listings_kind_address', 'listings', ['property_kind', 'address_id'])
    op.drop_index('uq_active_city_mode_crawl', table_name='crawls')
    op.create_index('uq_active_city_kind_mode_crawl', 'crawls', ['city_id', 'property_kind', 'mode'],
                    unique=True, postgresql_where=sa.text("status = 'active'"))
    op.add_column('crawls', sa.Column('radius_km', sa.Numeric(6, 2)))
    op.add_column('cities', sa.Column('population', sa.Integer))
    op.add_column('cities', sa.Column('house_radius_km', sa.Numeric(6, 2)))
    op.create_check_constraint('ck_city_house_radius', 'cities', 'house_radius_km BETWEEN 5 AND 100')
    bind = op.get_bind()
    for row in json.loads((Path(__file__).parents[2] / 'app/data/cities.json').read_text()):
        if row.get('population'):
            bind.execute(sa.text('UPDATE cities SET population=:population WHERE external_id=:external_id'), row)
    op.execute("INSERT INTO market_types (code,name) VALUES ('unknown','Не указан') ON CONFLICT DO NOTHING")
    for code, name in HOUSE_FEATURES.items():
        bind.execute(sa.text('INSERT INTO feature_types (code,name) VALUES (:code,:name) ON CONFLICT DO NOTHING'),
                     {'code': code, 'name': name})


def downgrade():
    raise RuntimeError('Откат удалит рынок домов и его историю. Восстановите резервную копию.')
