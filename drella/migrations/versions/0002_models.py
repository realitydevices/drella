"""models table; jobs snapshot their model

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Models are per token until O2 [27]: an alias is unique per owner, not globally.
    op.create_table(
        "models",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_token_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(length=63), nullable=False),
        sa.Column("interface", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("credential", sa.String(length=63), nullable=True),
        sa.Column("context_length", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_in_per_mtok", sa.Numeric(12, 4), nullable=True),
        sa.Column("cost_out_per_mtok", sa.Numeric(12, 4), nullable=True),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_token_id"], ["tokens.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_token_id", "alias"),
    )
    # Jobs from before v0.2 named a model ID and an OpenAI-compatible endpoint. Keep
    # both in the snapshot; their alias is the model ID.
    with op.batch_alter_table("jobs") as b:
        b.alter_column("endpoint", new_column_name="base_url", existing_type=sa.String(500))
        b.add_column(
            sa.Column(
                "interface",
                sa.String(length=32),
                nullable=False,
                server_default="openai-compatible",
            )
        )
        b.add_column(sa.Column("model_id", sa.String(length=200), nullable=True))
        b.add_column(sa.Column("credential", sa.String(length=200), nullable=True))
        b.add_column(sa.Column("context_length", sa.Integer(), nullable=True))
        b.add_column(sa.Column("max_output_tokens", sa.Integer(), nullable=True))
        b.add_column(sa.Column("cost_in_per_mtok", sa.Numeric(12, 4), nullable=True))
        b.add_column(sa.Column("cost_out_per_mtok", sa.Numeric(12, 4), nullable=True))
    op.execute("UPDATE jobs SET model_id = model")
    with op.batch_alter_table("jobs") as b:
        b.alter_column("model_id", existing_type=sa.String(200), nullable=False)
        b.alter_column("interface", existing_type=sa.String(32), server_default=None)


def downgrade() -> None:
    # v0 can only run a job against an OpenAI-compatible endpoint with no API key. A
    # queued or running job that needs more would run without its key (or the wrong
    # way), so refuse rather than break it.
    blocking = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')"
            " AND (interface != 'openai-compatible' OR credential IS NOT NULL)"
        )
    )
    if blocking:
        raise RuntimeError(
            f"can't downgrade: {blocking} queued or running job(s) use a model v0 can't run"
            " (an anthropic interface or an API key). Let them finish or fail them first."
        )
    # v0 named the model by its ID; v0.2 keeps the alias in `model`.
    op.execute("UPDATE jobs SET model = model_id")
    with op.batch_alter_table("jobs") as b:
        b.drop_column("cost_out_per_mtok")
        b.drop_column("cost_in_per_mtok")
        b.drop_column("max_output_tokens")
        b.drop_column("context_length")
        b.drop_column("credential")
        b.drop_column("model_id")
        b.drop_column("interface")
        b.alter_column("base_url", new_column_name="endpoint", existing_type=sa.String(500))
    op.drop_table("models")
