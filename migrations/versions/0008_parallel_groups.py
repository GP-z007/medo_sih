"""Multi-source groups, source cooldowns and user-facing calendar schedules."""
from alembic import op
import sqlalchemy as sa

revision = '0008_parallel_groups'
down_revision = '0007_scrape_groups'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('scrape_groups', 'source_id', nullable=True)
    op.alter_column('schedules', 'source_id', nullable=True)
    op.add_column('scrape_groups', sa.Column('skipped', sa.JSON(), nullable=False, server_default='[]'))
    op.add_column('schedules', sa.Column('group_request', sa.JSON(), nullable=True))
    op.add_column('schedules', sa.Column('timing', sa.JSON(), nullable=True))
    op.add_column('sources', sa.Column('cooldown_until', sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint('jobs_schedule_occurrence_window_key', 'jobs', type_='unique')
    op.create_unique_constraint('jobs_schedule_source_window_key', 'jobs', ['schedule_id', 'scheduled_for', 'source_id', 'group_window'])


def downgrade():
    raise RuntimeError('Multi-source schedule and cooldown history must be preserved; downgrade refused')
