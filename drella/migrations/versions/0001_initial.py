"""initial

Revision ID: 0001
Revises: 
Create Date: 2026-11-08 23:13:09.637598
"""

import sqlalchemy as sa
from alembic import op


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('tokens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('lookup_hash', sa.String(length=64), nullable=False),
    sa.Column('salt', sa.LargeBinary(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('lookup_hash')
    )
    op.create_table('credentials',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('ciphertext', sa.LargeBinary(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['token_id'], ['tokens.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_id', 'name')
    )
    op.create_table('jobs',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('token_id', sa.Integer(), nullable=False),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('repo', sa.String(length=200), nullable=False),
    sa.Column('model', sa.String(length=200), nullable=False),
    sa.Column('endpoint', sa.String(length=500), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('reported_status', sa.String(length=16), nullable=True),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('job_token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['token_id'], ['tokens.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_token_hash')
    )
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_jobs_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_jobs_status'), ['status'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_jobs_status'))
        batch_op.drop_index(batch_op.f('ix_jobs_created_at'))

    op.drop_table('jobs')
    op.drop_table('credentials')
    op.drop_table('tokens')
