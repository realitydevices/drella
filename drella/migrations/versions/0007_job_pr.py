"""the PR a job fixes

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: only jobs told to fix an existing PR have one.
    with op.batch_alter_table("jobs") as b:
        b.add_column(sa.Column("pr", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as b:
        b.drop_column("pr")
