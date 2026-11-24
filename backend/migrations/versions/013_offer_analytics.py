"""Offer attributes reuse normalized dictionaries; duplicate reviews never merge listings."""
from alembic import op
import sqlalchemy as sa

revision = '013'
down_revision = '012'
branch_labels = None
depends_on = None


def upgrade():
    op.bulk_insert(sa.table('feature_types', sa.column('code'), sa.column('name')), [
        {'code': 'sale_scope', 'name': 'Объект продажи'},
        {'code': 'premise_kind', 'name': 'Назначение объекта'},
        {'code': 'construction_status', 'name': 'Стадия объекта'},
        {'code': 'district', 'name': 'Район'},
    ])
    op.create_table('duplicate_reviews',
        sa.Column('left_id', sa.Integer, sa.ForeignKey('listings.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('right_id', sa.Integer, sa.ForeignKey('listings.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('decision', sa.String(20), nullable=False),
        sa.Column('left_signature', sa.String(64), nullable=False),
        sa.Column('right_signature', sa.String(64), nullable=False),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('left_id < right_id', name='ck_duplicate_review_order'),
        sa.CheckConstraint("decision IN ('same','different','pending')", name='ck_duplicate_review_decision'))


def downgrade():
    raise RuntimeError('Откат удалит результаты проверки дублей. Восстановите резервную копию.')
