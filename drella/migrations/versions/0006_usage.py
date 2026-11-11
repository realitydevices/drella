"""token usage and cost per job

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

COLUMNS = {
    "input_tokens": sa.BigInteger(),
    "output_tokens": sa.BigInteger(),
    "cache_read_tokens": sa.BigInteger(),
    "cache_write_tokens": sa.BigInteger(),
    "peak_context_tokens": sa.BigInteger(),
    "cost_usd": sa.Numeric(14, 6),
}


def upgrade() -> None:
    # Nullable: jobs that ran before this, or whose agent reported nothing, have no usage.
    with op.batch_alter_table("jobs") as b:
        for name, type_ in COLUMNS.items():
            b.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as b:
        for name in reversed(COLUMNS):
            b.drop_column(name)
