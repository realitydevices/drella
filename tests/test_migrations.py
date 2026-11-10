"""Migrations run on SQLite and on Postgres (DRELLA_TEST_DATABASE_URL)."""

from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from drella.db import MIGRATIONS, make_engine


def _cfg(conn):
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["connection"] = conn
    return cfg


def _upgrade(engine, rev):
    with engine.begin() as conn:
        command.upgrade(_cfg(conn), rev)


def _downgrade(engine, rev):
    with engine.begin() as conn:
        command.downgrade(_cfg(conn), rev)


def _seed_token(engine):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tokens (id, name, lookup_hash, salt, created_at) "
                 "VALUES (1, 'alice', 'h', :salt, :now)"),
            {"salt": b"0" * 16, "now": datetime.now(UTC)},
        )


def _insert_v02_job(engine, job_id, status, interface="openai-compatible", credential=None):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO jobs (id, token_id, prompt, model, interface, base_url, model_id,"
                 " credential, status, job_token_hash, created_at) VALUES (:id, 1, 'p', 'alias',"
                 " :iface, 'http://ollama:11434/v1', 'real-model', :cred, :status, :h, :now)"),
            {"id": job_id, "iface": interface, "cred": credential, "status": status,
             "h": "h" + job_id, "now": datetime.now(UTC)},
        )


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


def test_0002_downgrade_restores_the_model_id(settings):
    engine = make_engine(settings.database_url)
    _upgrade(engine, "head")
    _seed_token(engine)
    _insert_v02_job(engine, "j1", "succeeded", interface="anthropic", credential="k")
    _insert_v02_job(engine, "j2", "queued")
    _downgrade(engine, "0001")
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, model, endpoint FROM jobs ORDER BY id")).all()
        assert [tuple(r) for r in rows] == [
            ("j1", "real-model", "http://ollama:11434/v1"),
            ("j2", "real-model", "http://ollama:11434/v1"),
        ]
    engine.dispose()


@pytest.mark.parametrize(
    "interface,credential", [("anthropic", None), ("openai-compatible", "some-key")]
)
def test_0002_downgrade_refuses_with_jobs_v0_cannot_run(settings, interface, credential):
    engine = make_engine(settings.database_url)
    _upgrade(engine, "head")
    _seed_token(engine)
    _insert_v02_job(engine, "j1", "running", interface=interface, credential=credential)
    with pytest.raises(RuntimeError, match="can't downgrade"):
        _downgrade(engine, "0001")
    engine.dispose()


def test_0003_downgrade_refuses_with_live_claude_code_jobs(settings):
    engine = make_engine(settings.database_url)
    _upgrade(engine, "head")
    _seed_token(engine)
    _insert_v02_job(engine, "j1", "queued", interface="claude-code", credential="k")
    with pytest.raises(RuntimeError, match="can't downgrade"):
        _downgrade(engine, "0002")
    engine.dispose()


def test_0003_downgrade_keeps_finished_claude_code_jobs(settings):
    engine = make_engine(settings.database_url)
    _upgrade(engine, "head")
    _seed_token(engine)
    _insert_v02_job(engine, "j1", "succeeded", interface="claude-code", credential="k")
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET base_url = NULL"))
    _downgrade(engine, "0002")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT base_url FROM jobs")).scalar() == ""
    _upgrade(engine, "head")
    engine.dispose()


def _insert_model(engine, alias, interface, base_url, credential=None):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO models (owner_token_id, alias, interface, base_url, model_id,"
                 " credential, created_at, updated_at) VALUES (1, :a, :i, :u, 'm', :c,"
                 " :now, :now)"),
            {"a": alias, "i": interface, "u": base_url, "c": credential,
             "now": datetime.now(UTC)},
        )


def test_0003_upgrade_keeps_models_and_jobs(settings):
    engine = make_engine(settings.database_url)
    _upgrade(engine, "0002")
    _seed_token(engine)
    _insert_model(engine, "local", "openai-compatible", "http://ollama:11434/v1")
    _insert_model(engine, "claude", "anthropic", "https://api.anthropic.com/v1", "k")
    _insert_v02_job(engine, "j1", "succeeded")
    _insert_v02_job(engine, "j2", "queued", interface="anthropic", credential="k")
    _upgrade(engine, "0003")
    with engine.connect() as conn:
        models = conn.execute(
            text("SELECT alias, interface, base_url, credential FROM models ORDER BY alias")
        ).all()
        jobs = conn.execute(
            text("SELECT id, status, interface, base_url, model_id FROM jobs ORDER BY id")
        ).all()
    assert [tuple(m) for m in models] == [
        ("claude", "anthropic", "https://api.anthropic.com/v1", "k"),
        ("local", "openai-compatible", "http://ollama:11434/v1", None),
    ]
    assert [tuple(j) for j in jobs] == [
        ("j1", "succeeded", "openai-compatible", "http://ollama:11434/v1", "real-model"),
        ("j2", "queued", "anthropic", "http://ollama:11434/v1", "real-model"),
    ]
    engine.dispose()


def test_0003_downgrade_deletes_claude_code_models_only(settings):
    engine = make_engine(settings.database_url)
    _upgrade(engine, "head")
    _seed_token(engine)
    _insert_model(engine, "local", "openai-compatible", "http://ollama:11434/v1")
    _insert_model(engine, "sub", "claude-code", None, "oauth")
    _downgrade(engine, "0002")
    with engine.connect() as conn:
        aliases = conn.execute(text("SELECT alias FROM models")).scalars().all()
    assert aliases == ["local"]
    engine.dispose()


def test_0004_marks_existing_jobs_as_using_their_callers_credential(settings):
    engine = make_engine(settings.database_url)
    _upgrade(engine, "0003")
    _seed_token(engine)
    _insert_v02_job(engine, "j1", "queued")
    _upgrade(engine, "0004")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT github_app FROM jobs")).scalar() in (False, 0)
    engine.dispose()


def test_0004_downgrade_refuses_with_live_app_token_jobs(settings):
    engine = make_engine(settings.database_url)
    _upgrade(engine, "head")
    _seed_token(engine)
    _insert_v02_job(engine, "j1", "running")
    _insert_v02_job(engine, "j2", "succeeded")
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET github_app = :t"), {"t": True})
    with pytest.raises(RuntimeError, match="can't downgrade"):
        _downgrade(engine, "0003")
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status = 'failed' WHERE id = 'j1'"))
    _downgrade(engine, "0003")
    _upgrade(engine, "head")
    engine.dispose()
