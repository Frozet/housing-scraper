"""Remember counter rechecks across collector restarts."""
from alembic import op
import sqlalchemy as sa

revision = '009'
down_revision = '008'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('sample_plans', sa.Column('probe_retries', sa.Integer, nullable=False, server_default='0'))
    op.create_check_constraint('ck_sample_probe_retries', 'sample_plans', 'probe_retries >= 0')


def downgrade():
    op.drop_constraint('ck_sample_probe_retries', 'sample_plans', type_='check')
    op.drop_column('sample_plans', 'probe_retries')
