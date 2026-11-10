"""jobs record whether their GitHub token comes from Drella's GitHub App

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing jobs all used their caller's `github` credential.
    with op.batch_alter_table("jobs") as b:
        b.add_column(
            sa.Column("github_app", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    # v0.3 can't mint tokens. A queued or running job that relies on the App would
    # launch, or carry on, without a GitHub token, so refuse rather than break it.
    blocking = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM jobs WHERE status IN ('queued', 'running') AND github_app"
        )
    )
    if blocking:
        raise RuntimeError(
            f"can't downgrade: {blocking} queued or running job(s) use the GitHub App."
            " Let them finish or fail them first."
        )
    with op.batch_alter_table("jobs") as b:
        b.drop_column("github_app")
