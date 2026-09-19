"""Persist worker liveness and scheduler failure reasons."""
from alembic import op
import sqlalchemy as sa

revision = "0003_worker_health"
down_revision = "0002_timescale"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("worker_status", sa.Column("id", sa.String(36), primary_key=True),
                    sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("state", sa.String(30), nullable=False))
    op.add_column("schedules", sa.Column("last_error", sa.String(500), nullable=True))


def downgrade():
    op.drop_column("schedules", "last_error")
    op.drop_table("worker_status")
