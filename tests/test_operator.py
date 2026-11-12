"""Operator tokens (issue #74): an operator token's job gets a job-scoped API token with
which it can queue and read its caller's jobs, and nothing else."""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from drella import crypto
from drella.adapters import LLM_KEY_SECRET
from drella.cli import create_token, set_role
from drella.models import Job
from drella.runner import API_TOKEN_KEY, JOB_TOKEN_KEY, RunState, RunStatus

LOCAL = {
    "interface": "openai-compatible",
    "base_url": "https://llm.example/v1",
    "model_id": "some-model",
    "credential": "llm",
}
JOB = {"prompt": "supervise", "repo": "octo/widgets", "model": "local"}


def _bearer(t):
    return {"Authorization": f"Bearer {t}"}


def _setup(client, auth):
    assert client.put("/models/local", json=LOCAL, headers=auth).status_code == 200
    assert client.put("/credentials/github", json={"value": "ghp_x"}, headers=auth).status_code == 204
    assert client.put("/credentials/llm", json={"value": "sk-llm"}, headers=auth).status_code == 204


@pytest.fixture
def op_token(settings, client):
    return create_token(settings, "superstar", "builder", role="operator")


@pytest.fixture
def op_auth(client, op_token):
    auth = _bearer(op_token)
    _setup(client, auth)
    return auth


@pytest.fixture
def op_job(client, op_auth):
    r = client.post("/jobs", json=JOB, headers=op_auth)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def api_auth(runner, op_job):
    return _bearer(runner.secrets[op_job["id"]][API_TOKEN_KEY])


@pytest.fixture
def running(op_job, queue):
    queue.tick()
    return op_job


def _row(app, job_id):
    with app.state.deps.sessions() as s:
        return s.get(Job, job_id)


def test_member_jobs_get_no_api_token(client, auth, runner, queue, app):
    _setup(client, auth)
    job = client.post("/jobs", json=JOB, headers=auth).json()
    assert API_TOKEN_KEY not in runner.secrets[job["id"]]
    queue.tick()
    assert runner.launched[-1].api_token is False
    row = _row(app, job["id"])
    assert row.api_token_hash is None and row.api_key_wrap is None
    assert job["parent_job_id"] is None


def test_operator_job_gets_its_own_api_token_and_only_its_hash_is_stored(
    client, op_token, op_job, runner, queue, app
):
    api = runner.secrets[op_job["id"]][API_TOKEN_KEY]
    assert api.startswith("dra_") and api != op_token
    assert op_token not in runner.secrets[op_job["id"]].values()
    row = _row(app, op_job["id"])
    assert row.api_token_hash == crypto.lookup_hash(api)
    assert api.encode() not in row.api_key_wrap and op_token.encode() not in row.api_key_wrap
    queue.tick()
    assert runner.launched[-1].api_token is True
    assert op_job["parent_job_id"] is None


def test_the_api_token_does_nothing_before_its_job_runs(client, op_job, api_auth):
    r = client.get("/jobs", headers=api_auth)
    assert r.status_code == 401 and "queued" in r.json()["detail"]


def test_the_api_token_queues_jobs_as_its_caller(client, op_auth, running, api_auth, runner, app):
    r = client.post("/jobs", json={**JOB, "prompt": "build #1"}, headers=api_auth)
    assert r.status_code == 201, r.text
    child = r.json()
    assert child["parent_job_id"] == running["id"]
    assert _row(app, child["id"]).token_id == _row(app, running["id"]).token_id
    # The caller's credentials, decrypted with the unwrapped key.
    secrets = runner.secrets[child["id"]]
    assert secrets["github"] == "ghp_x" and secrets[LLM_KEY_SECRET] == "sk-llm"
    # A child gets no API token of its own: no fan-out beyond one level.
    assert API_TOKEN_KEY not in secrets
    assert _row(app, child["id"]).api_token_hash is None
    # The caller sees it too, with its parent.
    got = client.get(f"/jobs/{child['id']}", headers=op_auth).json()
    assert got["parent_job_id"] == running["id"]


def test_the_api_token_reads_only_its_callers_jobs(
    client, auth, op_auth, running, api_auth, runner, storage
):
    _setup(client, auth)
    others = client.post("/jobs", json=JOB, headers=auth).json()
    child = client.post("/jobs", json=JOB, headers=api_auth).json()
    ids = {j["id"] for j in client.get("/jobs", headers=api_auth).json()}
    assert ids == {running["id"], child["id"]}
    assert client.get(f"/jobs/{child['id']}", headers=api_auth).status_code == 200
    assert client.get(f"/jobs/{others['id']}", headers=api_auth).status_code == 404
    # Logs and outputs of its caller's jobs (here, its own job's).
    runner.pod_logs[running["id"]] = b"hello\n"
    r = client.get(f"/jobs/{running['id']}/logs", headers=api_auth)
    assert r.status_code == 200 and r.text == "hello\n"
    ja = _bearer(runner.secrets[running["id"]][JOB_TOKEN_KEY])
    assert client.put(f"/jobs/{running['id']}/outputs/report.md", content=b"r", headers=ja).status_code == 204
    r = client.get(f"/jobs/{running['id']}/outputs/report.md", headers=api_auth)
    assert r.status_code == 200 and r.content == b"r"
    assert client.get(f"/jobs/{others['id']}/logs", headers=api_auth).status_code == 404
    assert client.get(f"/jobs/{others['id']}/outputs/x", headers=api_auth).status_code == 404


def test_the_api_token_cant_touch_credentials_models_or_job_reports(
    client, running, api_auth
):
    calls = [
        ("get", "/credentials", None),
        ("put", "/credentials/github", {"value": "ghp_evil"}),
        ("delete", "/credentials/github", None),
        ("get", "/models", None),
        ("get", "/models/local", None),
        ("put", "/models/local", LOCAL),
        ("delete", "/models/local", None),
        ("post", f"/jobs/{running['id']}/status", {"status": "succeeded"}),
    ]
    for method, path, body in calls:
        kwargs = {"headers": api_auth}
        if body is not None:
            kwargs["json"] = body
        r = getattr(client, method)(path, **kwargs)
        assert r.status_code == 401, (method, path, r.status_code)


def test_the_api_token_is_refused_once_its_job_ends(client, running, api_auth, runner, queue, app):
    assert client.get("/jobs", headers=api_auth).status_code == 200
    runner.states[running["id"]] = RunStatus(RunState.SUCCEEDED)
    queue.tick()
    row = _row(app, running["id"])
    assert row.status == "failed"  # it never reported; ended either way
    assert row.api_key_wrap is None
    r = client.get("/jobs", headers=api_auth)
    assert r.status_code == 401 and "failed" in r.json()["detail"]
    assert client.post("/jobs", json=JOB, headers=api_auth).status_code == 401


def test_the_api_token_is_refused_at_the_jobs_deadline(client, running, api_auth, app, settings):
    with app.state.deps.sessions() as s:
        s.get(Job, running["id"]).started_at = datetime.now(UTC) - timedelta(
            seconds=settings.job_timeout_seconds + 1
        )
        s.commit()
    r = client.get("/jobs", headers=api_auth)
    assert r.status_code == 401 and "deadline" in r.json()["detail"]


def test_the_api_token_stops_working_when_its_token_is_no_longer_an_operator(
    client, settings, running, api_auth
):
    set_role(settings, 1, "member")
    assert client.get("/jobs", headers=api_auth).status_code == 401


def test_child_jobs_per_run_are_limited(client, running, api_auth, op_auth):
    for i in range(6):  # the default
        assert client.post("/jobs", json=JOB, headers=api_auth).status_code == 201, i
    r = client.post("/jobs", json=JOB, headers=api_auth)
    assert r.status_code == 429
    assert "6" in r.json()["detail"] and "DRELLA_OPERATOR_MAX_CHILD_JOBS" in r.json()["detail"]
    # The caller itself isn't limited.
    assert client.post("/jobs", json=JOB, headers=op_auth).status_code == 201


def test_the_child_limit_is_a_setting(client, running, api_auth, app):
    deps = app.state.deps
    deps.settings = dataclasses.replace(deps.settings, operator_max_child_jobs=1)
    assert client.post("/jobs", json=JOB, headers=api_auth).status_code == 201
    assert client.post("/jobs", json=JOB, headers=api_auth).status_code == 429


def _child(client, api_auth, **extra):
    return client.post("/jobs", json={**JOB, **extra}, headers=api_auth)


def test_a_child_defaults_to_the_reader_profile(client, running, api_auth, op_job, op_auth):
    child = _child(client, api_auth).json()
    assert child["github_profile"] == "reader"
    assert client.get(f"/jobs/{child['id']}", headers=op_auth).json()["github_profile"] == "reader"
    # Not its parent's: the operator token here is a builder, and its own job has none set.
    assert op_job["github_profile"] is None


@pytest.mark.parametrize("profile", ["superstar", "admin", "", "Builder", 1])
def test_a_child_cant_ask_for_other_profiles(client, running, api_auth, profile):
    assert _child(client, api_auth, profile=profile).status_code == 422


def test_a_non_operator_caller_cant_send_a_profile(client, auth, op_auth):
    _setup(client, auth)
    for a in (auth, op_auth):  # a member, and an operator using its own token
        r = _child(client, a, profile="reader")
        assert r.status_code == 422 and "profile" in r.text


def test_a_pr_child_needs_builder(client, running, api_auth):
    r = _child(client, api_auth, pr=5)
    assert r.status_code == 422 and "builder" in r.json()["detail"]
    r = _child(client, api_auth, pr=5, profile="reader")
    assert r.status_code == 422
    r = _child(client, api_auth, pr=5, profile="builder")
    assert r.status_code == 201 and r.json()["pr"] == 5
