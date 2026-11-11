"""launch retries: attempts so far and the earliest time to try again

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
    with op.batch_alter_table("jobs") as b:
        b.add_column(
            sa.Column("launch_attempts", sa.Integer(), nullable=False, server_default="0")
        )
        b.add_column(sa.Column("not_before", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as b:
        b.drop_column("not_before")
        b.drop_column("launch_attempts")
