"""Parallel observation ownership, egress audit, and grouped export counts."""
from alembic import op
import sqlalchemy as sa

revision = '0009_parallel_ownership'
down_revision = '0008_parallel_groups'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('jobs', sa.Column('network_profile', sa.String(64), nullable=False, server_default='automatic'))
    op.add_column('observations', sa.Column('group_id', sa.String(36), sa.ForeignKey('scrape_groups.id'), nullable=True))
    op.create_index('ix_observations_group_id', 'observations', ['group_id'])
    op.execute('UPDATE observations AS o SET group_id = j.group_id FROM jobs AS j WHERE o.job_id = j.id AND o.group_id IS NULL')
    op.add_column('exports', sa.Column('scope', sa.String(20), nullable=False, server_default='data'))
    op.add_column('exports', sa.Column('group_id', sa.String(36), sa.ForeignKey('scrape_groups.id'), nullable=True))
    op.add_column('exports', sa.Column('matched_source_count', sa.Integer(), nullable=True))
    op.add_column('exports', sa.Column('matched_job_count', sa.Integer(), nullable=True))


def downgrade():
    raise RuntimeError('Parallel ownership and export audit history must be preserved; downgrade refused')
