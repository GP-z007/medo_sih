"""Preserve an unavailable displayed cabin as NULL instead of inventing one."""
from alembic import op
import sqlalchemy as sa

revision = '0006_nullable_cabin'
down_revision = '0005_canonical_discovery'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('observations', 'cabin', existing_type=sa.String(40), nullable=True)


def downgrade():
    # Do not fabricate classifications to force a downgrade.
    connection = op.get_bind()
    if connection.execute(sa.text('SELECT EXISTS (SELECT 1 FROM observations WHERE cabin IS NULL)')).scalar():
        raise RuntimeError('Cannot restore required cabin while genuine unclassified observations exist')
    op.alter_column('observations', 'cabin', existing_type=sa.String(40), nullable=False)
