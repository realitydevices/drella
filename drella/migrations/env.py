"""Alembic environment. Run via drella.db.migrate(), which passes a connection."""

from alembic import context

from drella.models import Base

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
