"""Operator tokens (issue #74): an operator token's job gets a job-scoped API token with
which it can queue and read its caller's jobs, and nothing else."""

import dataclasses
import threading
from datetime import UTC, datetime, timedelta

import pytest

from drella import app as app_module
from drella import crypto
from drella.adapters import LLM_KEY_SECRET
from drella.cli import create_token, set_role
from drella.models import Job
from drella.runner import API_TOKEN_KEY, BUILDER_TOKEN_KEY, JOB_TOKEN_KEY, RunState, RunStatus

LOCAL = {
    "interface": "openai-compatible",
    "base_url": "https://llm.example/v1",
    "model_id": "some-model",
    "credential": "llm",
}
JOB = {"prompt": "supervise", "repo": "octo/widgets", "model": "local"}
# What an API token queues in these tests: no repo, since these callers store a `github`
# credential, which a repo child can't use (test_github.py covers repo children).
CHILD = {"prompt": "review", "model": "local"}


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
    assert r.status_code == 401 and "hasn't started" in r.json()["detail"]


def test_the_api_token_queues_jobs_as_its_caller(client, op_auth, running, api_auth, runner, app):
    r = client.post("/jobs", json={**CHILD, "prompt": "build #1"}, headers=api_auth)
    assert r.status_code == 201, r.text
    child = r.json()
    assert child["parent_job_id"] == running["id"]
    assert _row(app, child["id"]).token_id == _row(app, running["id"]).token_id
    # The caller's credentials, decrypted with the unwrapped key.
    secrets = runner.secrets[child["id"]]
    assert secrets[LLM_KEY_SECRET] == "sk-llm"
    assert "github" not in secrets  # a job without a repo gets no GitHub credential
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
    child = client.post("/jobs", json=CHILD, headers=api_auth).json()
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
    assert r.status_code == 401 and "has finished (failed)" in r.json()["detail"]
    assert client.post("/jobs", json=CHILD, headers=api_auth).status_code == 401


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
        assert client.post("/jobs", json=CHILD, headers=api_auth).status_code == 201, i
    r = client.post("/jobs", json=CHILD, headers=api_auth)
    assert r.status_code == 429
    assert "6" in r.json()["detail"] and "DRELLA_OPERATOR_MAX_CHILD_JOBS" in r.json()["detail"]
    # The caller itself isn't limited.
    assert client.post("/jobs", json=JOB, headers=op_auth).status_code == 201


def test_the_child_limit_is_a_setting(client, running, api_auth, app):
    deps = app.state.deps
    deps.settings = dataclasses.replace(deps.settings, operator_max_child_jobs=1)
    assert client.post("/jobs", json=CHILD, headers=api_auth).status_code == 201
    assert client.post("/jobs", json=CHILD, headers=api_auth).status_code == 429


def _child(client, api_auth, **extra):
    return client.post("/jobs", json={**CHILD, **extra}, headers=api_auth)



# -- profiles with a stored `github` credential --------------------------------
# (These tokens store `github`, so no GitHub App token is minted; test_github.py covers
# profiles on the App path.)


def test_a_profile_cant_differ_from_the_tokens_with_a_stored_github_credential(
    client, running, api_auth, op_auth
):
    for a in (op_auth, api_auth):  # the operator itself, and its job's API token
        r = _child(client, a, profile="reader")  # narrower than builder, but not honoured
        assert r.status_code == 422, r.text
        assert "stored 'github' credential" in r.json()["detail"]
    # Its own profile changes nothing, so it's accepted; the job uses the credential.
    r = _child(client, op_auth, profile="builder")
    assert r.status_code == 201 and r.json()["github_profile"] is None


def test_with_a_stored_github_credential_an_api_token_cant_queue_a_repo_job(
    client, running, api_auth, op_auth, runner
):
    """The child would get the caller's own credential, not a reader token."""
    before = set(runner.secrets)
    for extra in ({}, {"profile": "builder"}, {"profile": "reader"}, {"pr": 5}):
        r = client.post("/jobs", json={**JOB, **extra}, headers=api_auth)
        assert r.status_code == 422, (extra, r.text)
        assert "stored 'github' credential" in r.json()["detail"]
    assert set(runner.secrets) == before  # no Secret was made
    # Without a repo it's fine, and the caller itself may still queue repo jobs.
    child = _child(client, api_auth).json()
    assert child["github_profile"] is None and "github" not in runner.secrets[child["id"]]
    assert client.post("/jobs", json=JOB, headers=op_auth).status_code == 201


@pytest.mark.parametrize("profile", ["superstar", "admin", "", "Builder", 1])
def test_a_child_cant_ask_for_other_profiles(client, running, api_auth, profile):
    assert _child(client, api_auth, profile=profile).status_code == 422


# -- models --------------------------------------------------------------------


def test_the_api_token_reads_its_callers_models(client, auth, running, api_auth):
    _setup(client, auth)  # another token's model with the same alias
    client.put("/models/other", json={**LOCAL, "model_id": "x"}, headers=auth)
    r = client.get("/models", headers=api_auth)
    assert r.status_code == 200 and [m["alias"] for m in r.json()] == ["local"]
    r = client.get("/models/local", headers=api_auth)
    assert r.status_code == 200 and r.json()["model_id"] == "some-model"
    assert client.get("/models/other", headers=api_auth).status_code == 404


# -- following logs --------------------------------------------------------------


def _end(app, job_id):
    """What the queue does when an operator job's pod ends."""
    with app.state.deps.sessions() as s:
        job = s.get(Job, job_id)
        job.status = "succeeded"
        job.api_key_wrap = None
        s.commit()


def test_a_follow_with_the_api_token_stops_when_its_job_ends(
    client, running, api_auth, runner, app, queue, monkeypatch
):
    monkeypatch.setattr(app_module, "FOLLOW_POLL", 0.05)
    child = _child(client, api_auth).json()
    queue.concurrency = 2
    queue.tick()  # the child runs
    release = threading.Event()

    def silent_log(job_id, follow=False):
        # A chunk, then the operator job ends while the child's pod stays silent.
        yield b"child says hi\n"
        _end(app, running["id"])
        release.wait(30)
        yield b"never seen\n"

    runner.logs = silent_log
    try:
        r = client.get(f"/jobs/{child['id']}/logs?follow=1", headers=api_auth)
    finally:
        release.set()
    assert r.status_code == 200
    assert r.text.startswith("child says hi\n")
    assert "never seen" not in r.text
    assert "succeeded" in r.text and "stopped following" in r.text


def test_a_follow_with_the_api_token_stops_waiting_when_its_job_ends(
    client, running, api_auth, app, monkeypatch
):
    monkeypatch.setattr(app_module, "FOLLOW_POLL", 0.05)
    child = _child(client, api_auth).json()  # stays queued: concurrency 1
    timer = threading.Timer(0.3, _end, (app, running["id"]))
    timer.start()
    r = client.get(f"/jobs/{child['id']}/logs?follow=1", headers=api_auth)
    timer.join()
    assert r.status_code == 200 and "succeeded" in r.text


def test_a_follow_with_the_api_token_streams_while_its_job_runs(
    client, running, api_auth, runner
):
    runner.pod_logs[running["id"]] = b"all of it\n"
    r = client.get(f"/jobs/{running['id']}/logs?follow=1", headers=api_auth)
    assert r.status_code == 200 and r.text == "all of it\n"


# -- the builder token (DRELLA_BUILDER_TOKEN) -------------------------------------


def test_an_operators_top_level_job_gets_its_stored_builder_token(
    client, op_auth, runner, queue
):
    assert client.put(
        "/credentials/drella-builder", json={"value": "drt_builderbuilder"}, headers=op_auth
    ).status_code == 204
    job = client.post("/jobs", json=JOB, headers=op_auth).json()
    assert runner.secrets[job["id"]][BUILDER_TOKEN_KEY] == "drt_builderbuilder"
    queue.tick()
    api = _bearer(runner.secrets[job["id"]][API_TOKEN_KEY])
    # Its children never do.
    child = client.post("/jobs", json=CHILD, headers=api).json()
    assert BUILDER_TOKEN_KEY not in runner.secrets[child["id"]]


def test_an_operator_without_a_stored_builder_token_gets_none(op_job, runner):
    assert BUILDER_TOKEN_KEY not in runner.secrets[op_job["id"]]


def test_a_member_tokens_job_never_gets_a_builder_token(client, auth, runner):
    _setup(client, auth)
    client.put("/credentials/drella-builder", json={"value": "drt_builderbuilder"}, headers=auth)
    job = client.post("/jobs", json=JOB, headers=auth).json()
    assert BUILDER_TOKEN_KEY not in runner.secrets[job["id"]]
    assert "drt_builderbuilder" not in runner.secrets[job["id"]].values()


def test_an_sse_follow_with_the_api_token_ends_when_its_job_ends(
    client, running, api_auth, runner, app, queue, monkeypatch
):
    monkeypatch.setattr(app_module, "FOLLOW_POLL", 0.05)
    child = _child(client, api_auth).json()
    queue.concurrency = 2
    queue.tick()

    def log(job_id, follow=False):
        yield b"hi\n"
        _end(app, running["id"])

    runner.logs = log
    r = client.get(
        f"/jobs/{child['id']}/logs?follow=1",
        headers={**api_auth, "Accept": "text/event-stream"},
    )
    assert r.status_code == 200
    assert r.text.startswith("data: hi\n\n")
    assert r.text.endswith("event: end\ndata: running\n\n")  # the child runs on


def test_a_follow_with_the_api_token_is_cut_off_while_the_log_is_still_opening(
    client, running, api_auth, runner, app, queue, monkeypatch
):
    # runner.logs() itself can wait minutes for a pending pod; the stop checks go on.
    monkeypatch.setattr(app_module, "FOLLOW_POLL", 0.05)
    child = _child(client, api_auth).json()
    queue.concurrency = 2
    queue.tick()
    release = threading.Event()

    def pending_pod(job_id, follow=False):
        _end(app, running["id"])
        release.wait(30)  # still waiting for the pod when the operator job ends
        return iter([b"never seen\n"])

    runner.logs = pending_pod
    try:
        r = client.get(f"/jobs/{child['id']}/logs?follow=1", headers=api_auth)
    finally:
        release.set()
    assert r.status_code == 200
    assert "never seen" not in r.text and "stopped following" in r.text


def _follow_threads():
    return [t for t in threading.enumerate() if t.name == "drella-log-follow"]


def test_until_ends_at_end_of_log_with_a_full_queue_and_a_slow_consumer():
    import time

    chunks = [f"{i}\n".encode() for i in range(20)]
    out = []
    gen = app_module._until(lambda: iter(chunks), lambda: None, poll=0.02, maxsize=1)
    for item in gen:  # slower than the poll, so the reader keeps finding the queue full
        out.append(item)
        time.sleep(0.05)
    assert out == chunks  # every chunk, then the end marker: the follow ended
    deadline = time.monotonic() + 2
    while _follow_threads() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _follow_threads()


def test_until_reports_a_failed_log_even_with_a_full_queue():
    import time

    def source():
        yield b"a\n"
        yield b"b\n"
        raise RuntimeError("connection reset")

    out = []
    for item in app_module._until(source, lambda: None, poll=0.02, maxsize=1):
        out.append(item)
        time.sleep(0.05)
    assert out == [b"a\n", b"b\n", b"[drella] the log stream failed\n"]


def test_until_reader_thread_ends_after_the_client_goes_away():
    import time

    def endless():
        while True:
            yield b"x\n"

    gen = app_module._until(endless, lambda: None, poll=0.02, maxsize=1)
    assert next(gen) == b"x\n"
    gen.close()  # the client disconnected, with the queue full
    deadline = time.monotonic() + 2
    while _follow_threads() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _follow_threads()
