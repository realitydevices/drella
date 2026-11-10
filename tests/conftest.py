import os
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from drella.app import create_app
from drella.cli import create_token
from drella.config import Settings
from drella.runner import LogsUnavailable, RunState, RunStatus
from drella.storage import LocalStorage


class FakeRunner:
    """In-memory stand-in for the Kubernetes runner."""

    def __init__(self):
        self.secrets: dict[str, dict] = {}
        self.secret_created: dict[str, datetime] = {}
        self.launched = []
        self.states: dict[str, RunStatus] = {}
        self.pod_logs: dict[str, bytes] = {}
        self.cleaned: list[str] = []
        self.fail_launch = False

    def prepare(self, job_id, secrets):
        self.secrets[job_id] = dict(secrets)
        self.secret_created[job_id] = datetime.now(UTC)

    def launch(self, spec):
        if self.fail_launch:
            raise RuntimeError("cluster says no")
        self.launched.append(spec)
        self.states[spec.job_id] = RunStatus(RunState.RUNNING)

    def status(self, job_id):
        return self.states.get(job_id, RunStatus(RunState.MISSING))

    def logs(self, job_id, follow=False):
        if job_id not in self.pod_logs:
            raise LogsUnavailable("the job's pod hasn't started")
        return iter([self.pod_logs[job_id]])

    def cleanup(self, job_id):
        self.cleaned.append(job_id)

    def job_secrets(self):
        return {j: t for j, t in self.secret_created.items() if j not in self.cleaned}


@pytest.fixture
def settings(tmp_path):
    # Set DRELLA_TEST_DATABASE_URL to run against Postgres; the schema is reset.
    url = os.environ.get("DRELLA_TEST_DATABASE_URL")
    if url:
        engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        engine.dispose()
    else:
        url = f"sqlite:///{tmp_path}/drella.db"
    return Settings(database_url=url, storage_path=str(tmp_path / "storage"))


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def storage(settings):
    return LocalStorage(settings.storage_path)


@pytest.fixture
def app(settings, runner, storage):
    return create_app(settings, runner=runner, storage=storage, start_queue=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def queue(app):
    return app.state.deps.queue


@pytest.fixture
def token(settings, client):
    return create_token(settings, "alice")


@pytest.fixture
def auth(token):
    return {"Authorization": f"Bearer {token}"}
