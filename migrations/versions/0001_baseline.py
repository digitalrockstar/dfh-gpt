"""Baseline the existing DFH metadata schema.

This establishes Alembic version tracking for new installations. Existing
fully-created databases should be stamped with this revision rather than
running the baseline against a live dataset.
"""
from alembic import op

from dfh.schema import metadata

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError(
        "The baseline migration is intentionally irreversible. "
        "Restore a database backup instead of dropping the application schema."
    )
