"""tokens have a GitHub profile: what their jobs' GitHub App tokens may do

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A token created from now on is a `reader` unless the admin says otherwise. The
    # tokens that already exist were all made to build (push branches, open PRs), so
    # they keep that: `builder`.
    with op.batch_alter_table("tokens") as b:
        b.add_column(
            sa.Column(
                "github_profile", sa.String(32), nullable=False, server_default="reader"
            )
        )
    op.execute("UPDATE tokens SET github_profile = 'builder'")


def downgrade() -> None:
    # 0004 and 0005 ship in the same release, so no release runs at 0004: a downgrade
    # goes on to 0003, where Drella has no GitHub App and profiles mean nothing. (Code
    # at exactly 0004 would mint every App token as a builder.) Jobs keep nothing that
    # depends on the profile: it's read when a job's token is minted.
    with op.batch_alter_table("tokens") as b:
        b.drop_column("github_profile")
