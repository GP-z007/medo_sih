"""Canonical fare details and versioned real airport/route discovery."""
from alembic import op
import sqlalchemy as sa
revision = '0005_canonical_discovery'
down_revision = '0004_recipe_build'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('observations', sa.Column('details', sa.JSON(), nullable=False, server_default='{}'))
    op.execute('CREATE TABLE airport_reference (\n\tiata VARCHAR(3) NOT NULL, \n\tairport_name VARCHAR(250) NOT NULL, \n\tcity VARCHAR(250), \n\tstate VARCHAR(250), \n\tcountry VARCHAR(2) NOT NULL, \n\ttimezone VARCHAR(80), \n\treference_url VARCHAR(1000) NOT NULL, \n\treference_checksum VARCHAR(64) NOT NULL, \n\timported_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (iata)\n)')
    op.execute('CREATE TABLE discovery_runs (\n\tsource_id VARCHAR(36) NOT NULL, \n\trecipe_id VARCHAR(36) NOT NULL, \n\tjob_id VARCHAR(36) NOT NULL, \n\tscope VARCHAR(20) NOT NULL, \n\tstate VARCHAR(30) NOT NULL, \n\tcomplete BOOLEAN NOT NULL, \n\tairport_count INTEGER NOT NULL, \n\troute_count INTEGER NOT NULL, \n\torigins_checked INTEGER NOT NULL, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\treason VARCHAR(500), \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(source_id) REFERENCES sources (id), \n\tFOREIGN KEY(recipe_id) REFERENCES recipes (id), \n\tFOREIGN KEY(job_id) REFERENCES jobs (id)\n)')
    op.execute('CREATE INDEX ix_discovery_runs_source_id ON discovery_runs (source_id)')
    op.execute('CREATE TABLE airport_availability (\n\tsource_id VARCHAR(36) NOT NULL, \n\trecipe_id VARCHAR(36) NOT NULL, \n\tdiscovery_id VARCHAR(36) NOT NULL, \n\tiata VARCHAR(3) NOT NULL, \n\tairport_name VARCHAR(250), \n\tcity VARCHAR(250), \n\tstate VARCHAR(250), \n\tcountry VARCHAR(2), \n\tis_origin BOOLEAN NOT NULL, \n\tactive BOOLEAN NOT NULL, \n\tdiscovered_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (source_id, recipe_id, iata), \n\tFOREIGN KEY(source_id) REFERENCES sources (id), \n\tFOREIGN KEY(recipe_id) REFERENCES recipes (id), \n\tFOREIGN KEY(discovery_id) REFERENCES discovery_runs (id)\n)')
    op.execute('CREATE TABLE route_availability (\n\tsource_id VARCHAR(36) NOT NULL, \n\trecipe_id VARCHAR(36) NOT NULL, \n\tdiscovery_id VARCHAR(36) NOT NULL, \n\torigin VARCHAR(3) NOT NULL, \n\tdestination VARCHAR(3) NOT NULL, \n\tactive BOOLEAN NOT NULL, \n\tdiscovered_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (source_id, recipe_id, origin, destination), \n\tCONSTRAINT discovered_distinct_airports CHECK (origin <> destination), \n\tFOREIGN KEY(source_id) REFERENCES sources (id), \n\tFOREIGN KEY(recipe_id) REFERENCES recipes (id), \n\tFOREIGN KEY(discovery_id) REFERENCES discovery_runs (id)\n)')


def downgrade():
    op.drop_table('route_availability')
    op.drop_table('airport_availability')
    op.drop_table('discovery_runs')
    op.drop_table('airport_reference')
    op.drop_column('observations', 'details')
