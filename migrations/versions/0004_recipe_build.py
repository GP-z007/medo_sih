"""Recipe creation uses the existing durable jobs and operator commands."""
from alembic import op
import sqlalchemy as sa

revision = '0004_recipe_build'
down_revision = '0003_worker_health'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('jobs', 'recipe_id', existing_type=sa.String(36), nullable=True)
    op.add_column('jobs', sa.Column('recipe_candidate', sa.JSON(), nullable=True))
    op.add_column('jobs', sa.Column('build_base_recipe_id', sa.String(36), nullable=True))


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text('SELECT count(*) FROM jobs WHERE recipe_id IS NULL')):
        raise RuntimeError('Recipe creation history exists; preserve it before downgrading.')
    op.drop_column('jobs', 'build_base_recipe_id')
    op.drop_column('jobs', 'recipe_candidate')
    op.alter_column('jobs', 'recipe_id', existing_type=sa.String(36), nullable=False)
