"""Require TimescaleDB and convert authoritative observations to a hypertable."""
from alembic import op

revision = "0002_timescale"
down_revision = "63714acc0a95"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.execute("SELECT create_hypertable('observations', 'collected_at', if_not_exists => TRUE, migrate_data => TRUE)")


def downgrade():
    # Conversion back to a regular table would require a data-copy migration.
    raise RuntimeError("Timescale downgrade requires an explicit reviewed data-preserving migration")
