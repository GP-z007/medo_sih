"""Persistent multi-window collection groups and schedule occurrences."""
from alembic import op
import sqlalchemy as sa

revision = '0007_scrape_groups'
down_revision = '0006_nullable_cabin'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('schedules', sa.Column('booking_windows', sa.JSON(), nullable=True))
    op.create_table('scrape_groups',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source_id', sa.String(36), sa.ForeignKey('sources.id'), nullable=False),
        sa.Column('request', sa.JSON(), nullable=False),
        sa.Column('idempotency_key', sa.String(80), unique=True),
        sa.Column('schedule_id', sa.String(36), sa.ForeignKey('schedules.id')),
        sa.Column('scheduled_for', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('schedule_id','scheduled_for'))
    op.add_column('jobs', sa.Column('group_id', sa.String(36), sa.ForeignKey('scrape_groups.id')))
    op.add_column('jobs', sa.Column('group_window', sa.Integer(), server_default='0', nullable=False))
    op.create_index('ix_jobs_group_id','jobs',['group_id'])
    op.drop_constraint('jobs_schedule_id_scheduled_for_key','jobs',type_='unique')
    op.create_unique_constraint('jobs_schedule_occurrence_window_key','jobs',['schedule_id','scheduled_for','group_window'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS (SELECT 1 FROM scrape_groups)')).scalar():
        raise RuntimeError('Grouped job history must be preserved; downgrade refused')
    op.drop_constraint('jobs_schedule_occurrence_window_key','jobs',type_='unique')
    op.create_unique_constraint('jobs_schedule_id_scheduled_for_key','jobs',['schedule_id','scheduled_for'])
    op.drop_index('ix_jobs_group_id',table_name='jobs')
    op.drop_column('jobs','group_id')
    op.drop_column('jobs','group_window')
    op.drop_table('scrape_groups')
    op.drop_column('schedules','booking_windows')
