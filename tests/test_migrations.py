"""Migrations run on SQLite and on Postgres (DRELLA_TEST_DATABASE_URL)."""

from datetime import UTC, datetime

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from drella.db import MIGRATIONS, make_engine


def _upgrade(engine, rev):
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, rev)


def test_0002_keeps_old_jobs_model_and_endpoint(settings):
    engine = make_engine(settings.database_url)
    _upgrade(engine, "0001")
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tokens (id, name, lookup_hash, salt, created_at) "
                 "VALUES (1, 'alice', 'h', :salt, :now)"),
            {"salt": b"0" * 16, "now": now},
        )
        conn.execute(
            text("INSERT INTO jobs (id, token_id, prompt, model, endpoint, status, "
                 "job_token_hash, created_at) VALUES ('j1', 1, 'p', 'some-model', "
                 "'http://ollama:11434/v1', 'succeeded', 'jh', :now)"),
            {"now": now},
        )
    _upgrade(engine, "head")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT model, interface, base_url, model_id, credential FROM jobs")
        ).one()
        assert tuple(row) == (
            "some-model", "openai-compatible", "http://ollama:11434/v1", "some-model", None
        )
        assert conn.execute(text("SELECT count(*) FROM models")).scalar() == 0
    engine.dispose()
