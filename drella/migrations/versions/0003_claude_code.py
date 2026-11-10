"""claude-code models have no base URL

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A claude-code model [28] has no base URL: Claude Code talks to Anthropic itself.
    with op.batch_alter_table("models") as b:
        b.alter_column("base_url", existing_type=sa.String(500), nullable=True)
    with op.batch_alter_table("jobs") as b:
        b.alter_column("base_url", existing_type=sa.String(500), nullable=True)


def downgrade() -> None:
    # v0.2 can't run Claude Code. A queued or running claude-code job would launch the
    # wrong agent, so refuse rather than break it.
    blocking = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')"
            " AND interface = 'claude-code'"
        )
    )
    if blocking:
        raise RuntimeError(
            f"can't downgrade: {blocking} queued or running claude-code job(s)."
            " Let them finish or fail them first."
        )
    # claude-code models can't exist in v0.2. Finished claude-code jobs keep their
    # snapshot, with an empty base URL.
    op.execute("DELETE FROM models WHERE interface = 'claude-code'")
    op.execute("UPDATE jobs SET base_url = '' WHERE base_url IS NULL")
    with op.batch_alter_table("jobs") as b:
        b.alter_column("base_url", existing_type=sa.String(500), nullable=False)
    with op.batch_alter_table("models") as b:
        b.alter_column("base_url", existing_type=sa.String(500), nullable=False)
