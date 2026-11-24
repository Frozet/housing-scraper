"""Optional building characteristics and normalized categorical values."""
from alembic import op
import sqlalchemy as sa

revision = '008'
down_revision = '007'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('listings', sa.Column('construction_year', sa.Integer))
    op.create_check_constraint('ck_listings_construction_year', 'listings',
                               'construction_year IS NULL OR construction_year BETWEEN 1000 AND 2999')
    types = sa.table('feature_types', sa.column('code', sa.String), sa.column('name', sa.String))
    op.bulk_insert(types, [
        {'code': 'house_type', 'name': 'Тип дома'},
        {'code': 'passenger_elevator', 'name': 'Пассажирский лифт'},
        {'code': 'parking', 'name': 'Парковка'},
    ])


def downgrade():
    bind = op.get_bind()
    codes = ('house_type', 'passenger_elevator', 'parking')
    values = sa.table('feature_values', sa.column('id', sa.Integer), sa.column('type_code', sa.String))
    links = sa.table('listing_features', sa.column('value_id', sa.Integer))
    bind.execute(links.delete().where(links.c.value_id.in_(sa.select(values.c.id).where(values.c.type_code.in_(codes)))))
    bind.execute(values.delete().where(values.c.type_code.in_(codes)))
    types = sa.table('feature_types', sa.column('code', sa.String))
    bind.execute(types.delete().where(types.c.code.in_(codes)))
    op.drop_constraint('ck_listings_construction_year', 'listings', type_='check')
    op.drop_column('listings', 'construction_year')
