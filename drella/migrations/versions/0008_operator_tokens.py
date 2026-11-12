"""operator tokens: a role on tokens, a job-scoped API token and child jobs (issue #74)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every existing token is a member: an operator is only ever made by an admin.
    with op.batch_alter_table("tokens") as b:
        b.add_column(sa.Column("role", sa.String(16), nullable=False, server_default="member"))
    with op.batch_alter_table("jobs") as b:
        b.add_column(sa.Column("parent_job_id", sa.String(32), nullable=True))
        b.add_column(sa.Column("api_token_hash", sa.String(64), nullable=True))
        b.add_column(sa.Column("api_key_wrap", sa.LargeBinary(), nullable=True))
        b.create_foreign_key(
            "fk_jobs_parent_job_id", "jobs", ["parent_job_id"], ["id"], ondelete="SET NULL"
        )
        b.create_index("ix_jobs_parent_job_id", ["parent_job_id"])
        b.create_unique_constraint("uq_jobs_api_token_hash", ["api_token_hash"])


def downgrade() -> None:
    # Jobs lose their links and API tokens. A running operator job's API token stops
    # working (older code doesn't know it); nothing else depends on these columns.
    with op.batch_alter_table("jobs") as b:
        b.drop_constraint("uq_jobs_api_token_hash", type_="unique")
        b.drop_index("ix_jobs_parent_job_id")
        b.drop_constraint("fk_jobs_parent_job_id", type_="foreignkey")
        b.drop_column("api_key_wrap")
        b.drop_column("api_token_hash")
        b.drop_column("parent_job_id")
    with op.batch_alter_table("tokens") as b:
        b.drop_column("role")
