import pytest

from drella.cli import create_token
from drella.runner import JOB_TOKEN_KEY, RunState, RunStatus

JOB = {
    "prompt": "add a CHANGELOG.md",
    "repo": "octo/widgets",
    "model": "local",
}
LOCAL = {
    "interface": "openai-compatible",
    "base_url": "http://ollama:11434/v1",
    "model_id": "some-model",
}


@pytest.fixture(autouse=True)
def local_model(client, auth):
    """Every test can use the model alias "local" (no API key)."""
    r = client.put("/models/local", json=LOCAL, headers=auth)
    assert r.status_code == 200, r.text


@pytest.fixture
def job(client, auth):
    """A queued job, plus its job-scoped token (as the pod would receive it)."""
    assert client.put("/credentials/github", json={"value": "ghp_x"}, headers=auth).status_code == 204
    r = client.post("/jobs", json=JOB, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def job_auth(runner, job_id):
    return {"Authorization": f"Bearer {runner.secrets[job_id][JOB_TOKEN_KEY]}"}


# -- auth ---------------------------------------------------------------------


def test_requires_token(client):
    assert client.get("/jobs").status_code == 401
    assert client.get("/jobs", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/healthz").status_code == 200


# -- credentials ----------------------------------------------------------------


def test_put_credential_and_list_names(client, auth):
    assert client.put("/credentials/github", json={"value": "a"}, headers=auth).status_code == 204
    assert client.put("/credentials/github", json={"value": "b"}, headers=auth).status_code == 204
    assert client.get("/credentials", headers=auth).json() == ["github"]


@pytest.mark.parametrize("name", ["_job-token", "UPPER", "has space", "a" * 64])
def test_credential_names_are_restricted(client, auth, name):
    assert client.put(f"/credentials/{name}", json={"value": "a"}, headers=auth).status_code in (404, 422)


# -- jobs -----------------------------------------------------------------------


def test_job_with_repo_requires_github_credential(client, auth):
    r = client.post("/jobs", json=JOB, headers=auth)
    assert r.status_code == 422
    assert "github" in r.json()["detail"]


def test_job_without_repo_needs_no_credentials(client, auth, runner):
    no_repo = {k: v for k, v in JOB.items() if k != "repo"}
    r = client.post("/jobs", json=no_repo, headers=auth)
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["repo"] is None
    assert set(runner.secrets[job["id"]]) == {JOB_TOKEN_KEY}


def test_create_job_validates_body(client, auth):
    client.put("/credentials/github", json={"value": "x"}, headers=auth)
    for bad in ({"repo": "not a repo"}, {"model": "Not An Alias"}, {"prompt": ""}):
        assert client.post("/jobs", json=JOB | bad, headers=auth).status_code == 422


def test_job_secret_holds_only_the_credentials_it_needs(client, auth, runner):
    client.put("/credentials/github", json={"value": "ghp_x"}, headers=auth)
    client.put("/credentials/anthropic", json={"value": "sk-x"}, headers=auth)
    job = client.post("/jobs", json=JOB, headers=auth).json()
    assert job["status"] == "queued"
    secret = runner.secrets[job["id"]]
    assert set(secret) == {"github", JOB_TOKEN_KEY}
    assert secret["github"] == "ghp_x"
    assert secret[JOB_TOKEN_KEY].startswith("drj_")


def test_create_job_fails_cleanly_if_secret_cannot_be_created(client, auth, runner):
    client.put("/credentials/github", json={"value": "x"}, headers=auth)
    runner.prepare = lambda *a: (_ for _ in ()).throw(RuntimeError("no cluster"))
    assert client.post("/jobs", json=JOB, headers=auth).status_code == 503
    assert client.get("/jobs", headers=auth).json() == []


def test_list_and_get_jobs(client, auth, job):
    assert [j["id"] for j in client.get("/jobs", headers=auth).json()] == [job["id"]]
    got = client.get(f"/jobs/{job['id']}", headers=auth).json()
    assert got["repo"] == JOB["repo"] and got["outputs"] == []


def test_jobs_are_private_to_their_token(client, settings, job):
    other = {"Authorization": f"Bearer {create_token(settings, 'bob')}"}
    assert client.get("/jobs", headers=other).json() == []
    assert client.get(f"/jobs/{job['id']}", headers=other).status_code == 404
    assert client.get(f"/jobs/{job['id']}/logs", headers=other).status_code == 404


# -- the queue ----------------------------------------------------------------


def test_queue_runs_one_job_at_a_time_oldest_first(client, auth, job, queue, runner):
    second = client.post("/jobs", json=JOB, headers=auth).json()
    queue.tick()
    assert [s.job_id for s in runner.launched] == [job["id"]]
    assert client.get(f"/jobs/{job['id']}", headers=auth).json()["status"] == "running"
    assert client.get(f"/jobs/{second['id']}", headers=auth).json()["status"] == "queued"
    queue.tick()
    assert len(runner.launched) == 1  # concurrency 1

    runner.states[job["id"]] = RunStatus(RunState.SUCCEEDED)
    queue.tick()
    assert [s.job_id for s in runner.launched] == [job["id"], second["id"]]


def test_full_job_lifecycle(client, auth, job, queue, runner):
    queue.tick()
    jid, ja = job["id"], job_auth(runner, job["id"])
    spec = runner.launched[0]
    assert (spec.repo, spec.model.alias, spec.model.base_url, spec.model.model_id, spec.prompt) == (
        JOB["repo"], "local", LOCAL["base_url"], LOCAL["model_id"], JOB["prompt"]
    )
    assert not spec.model.has_key

    # The pod uploads its outputs and reports.
    assert client.put(f"/jobs/{jid}/outputs/diff.patch", content=b"+hello\n", headers=ja).status_code == 204
    assert client.put(f"/jobs/{jid}/outputs/pr-url.txt", content=b"https://github.com/x/y/pull/1", headers=ja).status_code == 204
    r = client.post(f"/jobs/{jid}/status", json={"status": "succeeded", "message": "PR opened"}, headers=ja)
    assert r.status_code == 204
    # Not final until the pod has exited and its log is archived.
    assert client.get(f"/jobs/{jid}", headers=auth).json()["status"] == "running"

    runner.pod_logs[jid] = b"cloning...\ndone\n"
    runner.states[jid] = RunStatus(RunState.SUCCEEDED)
    queue.tick()
    got = client.get(f"/jobs/{jid}", headers=auth).json()
    assert got["status"] == "succeeded" and got["message"] == "PR opened"
    assert got["outputs"] == ["diff.patch", "pr-url.txt"]
    assert runner.cleaned == [jid]

    # Archived log and outputs are downloadable after the pod is gone.
    del runner.pod_logs[jid]
    assert client.get(f"/jobs/{jid}/logs", headers=auth).text == "cloning...\ndone\n"
    assert client.get(f"/jobs/{jid}/outputs/diff.patch", headers=auth).content == b"+hello\n"
    assert client.get(f"/jobs/{jid}/outputs/missing", headers=auth).status_code == 404

    # The job token stops working once the job has finished.
    assert client.post(f"/jobs/{jid}/status", json={"status": "failed"}, headers=ja).status_code == 409


def test_job_that_exits_without_reporting_fails(client, auth, job, queue, runner):
    queue.tick()
    runner.states[job["id"]] = RunStatus(RunState.SUCCEEDED)
    queue.tick()
    got = client.get(f"/jobs/{job['id']}", headers=auth).json()
    assert got["status"] == "failed" and "without reporting" in got["message"]


def test_killed_job_fails_with_reason_and_keeps_log(client, auth, job, queue, runner):
    queue.tick()
    runner.pod_logs[job["id"]] = b"working...\n"
    runner.states[job["id"]] = RunStatus(RunState.FAILED, "DeadlineExceeded")
    queue.tick()
    got = client.get(f"/jobs/{job['id']}", headers=auth).json()
    assert got["status"] == "failed" and got["message"] == "DeadlineExceeded"
    assert client.get(f"/jobs/{job['id']}/logs", headers=auth).text == "working...\n"


def test_log_survives_the_pod_via_snapshots(client, auth, job, queue, runner):
    queue.log_snapshot_seconds = 0
    queue.tick()
    runner.pod_logs[job["id"]] = b"step 1\nstep 2\n"
    queue.tick()  # running: snapshot taken
    # Timed out: Kubernetes deleted the pod, and its log with it.
    del runner.pod_logs[job["id"]]
    runner.states[job["id"]] = RunStatus(RunState.FAILED, "DeadlineExceeded")
    queue.tick()
    got = client.get(f"/jobs/{job['id']}", headers=auth).json()
    assert got["status"] == "failed" and got["message"] == "DeadlineExceeded"
    assert client.get(f"/jobs/{job['id']}/logs", headers=auth).text == "step 1\nstep 2\n"


def test_a_rotated_log_keeps_the_earlier_segment(client, auth, job, queue, runner):
    queue.log_snapshot_seconds = 0
    queue.tick()
    runner.pod_logs[job["id"]] = b"line\n" * 100
    queue.tick()
    # The kubelet rotated the log: the API now returns only the newest part.
    runner.pod_logs[job["id"]] = b"tail\n"
    queue.tick()
    # The new segment grows past the old one's length; the old one is still kept.
    runner.pod_logs[job["id"]] = b"tail\n" + b"more\n" * 200
    queue.tick()
    runner.pod_logs[job["id"]] = b""  # an empty read never replaces anything
    runner.states[job["id"]] = RunStatus(RunState.FAILED, "DeadlineExceeded")
    queue.tick()
    log = client.get(f"/jobs/{job['id']}/logs", headers=auth).text
    assert log == "line\n" * 100 + "\n[drella] the pod's log was rotated here; some output may be missing\n" + "tail\n" + "more\n" * 200


def test_a_log_that_only_grows_is_one_segment(client, auth, job, queue, runner, storage):
    queue.log_snapshot_seconds = 0
    queue.tick()
    for n in (1, 2, 3):
        runner.pod_logs[job["id"]] = b"step\n" * n
        queue.tick()
    assert storage.list(f"jobs/{job['id']}/log/") == []
    runner.states[job["id"]] = RunStatus(RunState.SUCCEEDED)
    queue.tick()
    assert client.get(f"/jobs/{job['id']}/logs", headers=auth).text == "step\n" * 3


def test_a_failing_job_check_does_not_stop_the_tick(client, auth, job, queue, runner):
    queue.tick()

    def broken(job_id, follow=False):
        raise RuntimeError("pod vanished mid-read")

    runner.logs = broken
    runner.states[job["id"]] = RunStatus(RunState.FAILED, "Evicted")
    queue.tick()  # archiving fails, but the job still settles
    assert client.get(f"/jobs/{job['id']}", headers=auth).json()["status"] == "failed"

    def status_boom(job_id):
        raise RuntimeError("API server unreachable")

    second = client.post("/jobs", json=JOB, headers=auth).json()
    queue.tick()
    runner.status = status_boom
    queue.tick()  # logged, not raised
    assert queue.alive()
    assert client.get(f"/jobs/{second['id']}", headers=auth).json()["status"] == "running"


def test_leftover_secrets_are_reaped(client, auth, job, queue, runner):
    from datetime import timedelta

    queue.tick()
    runner.states[job["id"]] = RunStatus(RunState.FAILED, "x")
    runner.cleanup = lambda job_id: (_ for _ in ()).throw(RuntimeError("API down"))
    queue.tick()  # finished, but cleanup failed: the Secret is left behind
    queued = client.post("/jobs", json=JOB | {"prompt": "later"}, headers=auth).json()
    old = runner.secret_created[job["id"]] - timedelta(hours=1)
    runner.secret_created = {job["id"]: old, queued["id"]: old, "gone": old, "young": runner.secret_created[queued["id"]]}
    del runner.cleanup  # back to the real fake
    queue._last_reap = None
    queue.concurrency = 0  # keep the second job queued
    queue.tick()
    assert set(runner.cleaned) == {job["id"], "gone"}


def test_livez_fails_when_the_queue_stalls(client, queue):
    assert client.get("/livez").status_code == 200
    queue._last_tick -= 10_000
    assert client.get("/livez").status_code == 503


def test_health_checks_answer_when_every_worker_thread_is_busy(client):
    """Log followers can occupy the whole thread pool (#5); liveness mustn't fail
    because of that, or Kubernetes would restart a Drella that's working."""
    import threading

    import anyio.to_thread

    def limiter():
        return anyio.to_thread.current_default_thread_limiter()

    tokens = client.portal.call(lambda: limiter().total_tokens)
    release = threading.Event()
    try:
        # One worker thread, and something blocking it.
        client.portal.call(lambda: setattr(limiter(), "total_tokens", 1))
        client.portal.start_task_soon(anyio.to_thread.run_sync, release.wait)
        results = {}

        def probe(path):
            results[path] = client.get(path).status_code

        threads = [threading.Thread(target=probe, args=(p,)) for p in ("/healthz", "/livez")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert results == {"/healthz": 200, "/livez": 200}
    finally:
        release.set()
        client.portal.call(lambda: setattr(limiter(), "total_tokens", tokens))


def test_launch_failure_fails_the_job(client, auth, job, queue, runner):
    runner.fail_launch = True
    queue.tick()
    got = client.get(f"/jobs/{job['id']}", headers=auth).json()
    assert got["status"] == "failed" and "cluster says no" in got["message"]
    assert runner.cleaned == [job["id"]]


# -- job-scoped endpoints -------------------------------------------------------


def test_job_token_only_works_for_its_own_running_job(client, auth, job, queue, runner):
    ja = job_auth(runner, job["id"])
    # Queued: the pod doesn't exist yet.
    assert client.post(f"/jobs/{job['id']}/status", json={"status": "succeeded"}, headers=ja).status_code == 409
    queue.tick()
    other = client.post("/jobs", json=JOB, headers=auth).json()
    assert client.post(f"/jobs/{other['id']}/status", json={"status": "succeeded"}, headers=ja).status_code == 401
    # A user token isn't a job token, and a job token isn't a user token.
    assert client.post(f"/jobs/{job['id']}/status", json={"status": "succeeded"}, headers=auth).status_code == 401
    assert client.get("/jobs", headers=ja).status_code == 401
    assert client.post(f"/jobs/{job['id']}/status", json={"status": "bogus"}, headers=ja).status_code == 422


@pytest.mark.parametrize("name", ["..", ".hidden", "a%2Fb"])
def test_output_names_are_restricted(client, job, queue, runner, storage, name):
    queue.tick()
    ja = job_auth(runner, job["id"])
    assert client.put(f"/jobs/{job['id']}/outputs/{name}", content=b"x", headers=ja).status_code != 204
    assert storage.list() == []


# -- logs -----------------------------------------------------------------------


def test_logs_of_queued_job(client, auth, job):
    assert client.get(f"/jobs/{job['id']}/logs", headers=auth).status_code == 409


def test_live_logs_while_running(client, auth, job, queue, runner):
    queue.tick()
    assert client.get(f"/jobs/{job['id']}/logs", headers=auth).status_code == 409  # pod pending
    runner.pod_logs[job["id"]] = b"line 1\nline 2\n"
    r = client.get(f"/jobs/{job['id']}/logs?follow=1", headers=auth)
    assert r.status_code == 200 and r.text == "line 1\nline 2\n"


def test_logs_as_server_sent_events(client, auth, job, queue, runner):
    queue.tick()
    runner.pod_logs[job["id"]] = b"line 1\nline 2\n"
    runner.states[job["id"]] = RunStatus(RunState.FAILED, "OOMKilled")
    queue.tick()
    r = client.get(
        f"/jobs/{job['id']}/logs?follow=1",
        headers=auth | {"Accept": "text/event-stream"},
    )
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text == "data: line 1\n\ndata: line 2\n\nevent: end\ndata: failed\n\n"


def test_reported_failure_message_wins_over_kubernetes_reason(client, auth, job, queue, runner):
    queue.tick()
    ja = job_auth(runner, job["id"])
    client.post(f"/jobs/{job['id']}/status", json={"status": "failed", "message": "no PR"}, headers=ja)
    runner.states[job["id"]] = RunStatus(RunState.FAILED, "BackoffLimitExceeded")
    queue.tick()
    got = client.get(f"/jobs/{job['id']}", headers=auth).json()
    assert (got["status"], got["message"]) == ("failed", "no PR")


# -- models [27] ------------------------------------------------------------------

CLAUDE = {
    "interface": "anthropic",
    "base_url": "https://api.anthropic.com/v1/",
    "model_id": "claude-x",
    "credential": "anthropic-key",
    "context_length": 200000,
    "cost_in_per_mtok": "3",
    "cost_out_per_mtok": "15",
    "notes": "reviews",
}


def test_models_api(client, auth):
    r = client.put("/models/claude", json=CLAUDE, headers=auth)
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["alias"] == "claude" and m["base_url"] == "https://api.anthropic.com/v1"
    assert [x["alias"] for x in client.get("/models", headers=auth).json()] == ["claude", "local"]
    # PUT replaces.
    r = client.put("/models/claude", json=CLAUDE | {"notes": None, "context_length": None}, headers=auth)
    assert r.json()["notes"] is None and r.json()["context_length"] is None
    assert client.get("/models/claude", headers=auth).json()["model_id"] == "claude-x"
    assert client.delete("/models/claude", headers=auth).status_code == 204
    assert client.get("/models/claude", headers=auth).status_code == 404
    assert client.delete("/models/claude", headers=auth).status_code == 404
    assert client.get("/models").status_code == 401


@pytest.mark.parametrize(
    "bad",
    [
        {"interface": "carrier-pigeon"}, {"base_url": "ftp://x"}, {"model_id": ""},
        {"credential": "_llm-key"}, {"context_length": 0}, {"cost_in_per_mtok": "-1"},
        {"max_output_tokens": 0}, {"notes": "x" * 2001},
        # A model's key can't be the GitHub credential.
        {"credential": "github"},
        # A key is never sent in the clear.
        {"base_url": "http://api.example.com/v1"},
        # OpenCode substitutes {env:...} and {file:...} in its config, so a model row
        # could read the pod's secrets.
        {"base_url": "https://evil.example/{env:GH_TOKEN}/v1"},
        {"base_url": "https://evil.example/{file:/creds/github}"},
        {"model_id": "m{env:GH_TOKEN}"},
        # No whitespace or control characters.
        {"base_url": "https://api.example.com/v1\nx"}, {"base_url": "https://a b/v1"},
        {"model_id": "a b"},
        # Unknown fields.
        {"endpoint": "http://x"},
        # Normalised before validation: "https:///" is "https:", which isn't a URL.
        {"base_url": "https:///"},
        # curl would read [..] as a range in the preflight.
        {"base_url": "https://x.example/[1-100000]"},
        # Values that wouldn't fit the database columns.
        {"context_length": 2**40}, {"max_output_tokens": 10_000_001},
        {"cost_in_per_mtok": "1e12"}, {"cost_out_per_mtok": "0.00001"},
    ],
)
def test_models_are_validated(client, auth, bad):
    assert client.put("/models/claude", json=CLAUDE | bad, headers=auth).status_code == 422
    assert client.put("/models/Bad Alias", json=CLAUDE, headers=auth).status_code in (404, 422)


def test_job_with_unknown_model_is_rejected(client, auth):
    r = client.post("/jobs", json={"prompt": "x", "model": "nope"}, headers=auth)
    assert r.status_code == 422 and "nope" in r.json()["detail"]


def test_job_needs_the_models_credential(client, auth):
    client.put("/models/claude", json=CLAUDE, headers=auth)
    r = client.post("/jobs", json={"prompt": "x", "model": "claude"}, headers=auth)
    assert r.status_code == 422 and "anthropic-key" in r.json()["detail"]


def test_job_secret_gets_the_model_key_and_nothing_else(client, auth, runner, queue):
    client.put("/models/claude", json=CLAUDE, headers=auth)
    for name, value in [("github", "ghp_x"), ("anthropic-key", "sk-x"), ("other", "o")]:
        client.put(f"/credentials/{name}", json={"value": value}, headers=auth)

    no_repo = client.post("/jobs", json={"prompt": "x", "model": "claude"}, headers=auth).json()
    assert set(runner.secrets[no_repo["id"]]) == {"_llm-key", JOB_TOKEN_KEY}
    assert runner.secrets[no_repo["id"]]["_llm-key"] == "sk-x"

    repo = client.post("/jobs", json=JOB | {"model": "claude"}, headers=auth).json()
    assert set(runner.secrets[repo["id"]]) == {"github", "_llm-key", JOB_TOKEN_KEY}

    local = client.post("/jobs", json=JOB, headers=auth).json()
    assert set(runner.secrets[local["id"]]) == {"github", JOB_TOKEN_KEY}

    queue.tick()
    assert runner.launched[0].model.has_key


def test_a_job_keeps_its_model_snapshot(client, auth, runner, queue):
    client.put("/models/claude", json=CLAUDE, headers=auth)
    client.put("/credentials/anthropic-key", json={"value": "sk-x"}, headers=auth)
    job = client.post("/jobs", json={"prompt": "x", "model": "claude"}, headers=auth).json()
    snapshot = {
        "alias": "claude", "interface": "anthropic", "base_url": "https://api.anthropic.com/v1",
        "model_id": "claude-x", "credential": "anthropic-key", "context_length": 200000,
    }
    assert {k: job["model"][k] for k in snapshot} == snapshot
    assert float(job["model"]["cost_out_per_mtok"]) == 15

    # Changing or deleting the model doesn't rewrite the job, or what it runs.
    client.put("/models/claude", json=CLAUDE | {"model_id": "claude-y"}, headers=auth)
    client.delete("/models/claude", headers=auth)
    got = client.get(f"/jobs/{job['id']}", headers=auth).json()
    assert got["model"]["model_id"] == "claude-x"
    queue.tick()
    spec = runner.launched[0]
    assert (spec.model.model_id, spec.model.interface, spec.model.has_key) == (
        "claude-x", "anthropic", True
    )


def test_only_the_owner_can_change_or_delete_a_model(client, auth, settings):
    client.put("/models/claude", json=CLAUDE, headers=auth)
    other = {"Authorization": f"Bearer {create_token(settings, 'mallory')}"}
    # Mallory can see it...
    assert client.get("/models/claude", headers=other).json()["owner"] == "alice"
    # ...but can't repoint it (e.g. at her own URL) or delete it.
    evil = CLAUDE | {"base_url": "https://evil.example/v1"}
    assert client.put("/models/claude", json=evil, headers=other).status_code == 403
    assert client.delete("/models/claude", headers=other).status_code == 403
    assert client.get("/models/claude", headers=auth).json()["base_url"] == "https://api.anthropic.com/v1"
    # Her own new alias is hers.
    r = client.put("/models/mine", json=LOCAL, headers=other)
    assert r.status_code == 200 and r.json()["owner"] == "mallory"


def test_a_job_refuses_a_model_that_names_the_github_credential(client, auth, runner):
    # Rows can't be created like this through the API; this covers rows that are.
    from drella.models import Model

    client.put("/models/claude", json=CLAUDE, headers=auth)
    with client.app.state.deps.sessions() as s:
        s.get(Model, "claude").credential = "github"
        s.commit()
    client.put("/credentials/github", json={"value": "ghp_x"}, headers=auth)
    r = client.post("/jobs", json={"prompt": "x", "model": "claude"}, headers=auth)
    assert r.status_code == 422 and "reserved" in r.json()["detail"]
    assert runner.secrets == {}


def test_credentials_are_stripped(client, auth, runner):
    client.put("/models/claude", json=CLAUDE, headers=auth)
    client.put("/credentials/anthropic-key", json={"value": "  sk-x\n"}, headers=auth)
    job = client.post("/jobs", json={"prompt": "x", "model": "claude"}, headers=auth).json()
    assert runner.secrets[job["id"]]["_llm-key"] == "sk-x"
    assert client.put("/credentials/empty", json={"value": " \n"}, headers=auth).status_code == 422


@pytest.mark.parametrize(
    "key",
    ['sk-a"b', "sk-a\\b", "sk-a\nb", "sk-a\tb", "sk-a\x7fb",
     # OpenCode would substitute these again, e.g. sending the GitHub token.
     "{file:/creds/github}", "sk-{env:GH_TOKEN}"],
)
def test_a_model_key_that_would_break_the_config_is_rejected_without_echoing_it(
    client, auth, runner, key
):
    client.put("/models/claude", json=CLAUDE, headers=auth)
    client.put("/credentials/anthropic-key", json={"value": key}, headers=auth)
    r = client.post("/jobs", json={"prompt": "x", "model": "claude"}, headers=auth)
    assert r.status_code == 422
    assert "anthropic-key" in r.json()["detail"] and key.strip() not in r.text
    assert runner.secrets == {}


def test_jobs_reject_unknown_fields(client, auth):
    r = client.post(
        "/jobs", json={"prompt": "x", "model": "local", "endpoint": "http://x"}, headers=auth
    )
    assert r.status_code == 422 and "endpoint" in r.text


def test_put_model_race_on_a_new_alias(client, auth, monkeypatch):
    # Another request creates the alias between our read and our insert.
    from sqlalchemy.orm import Session

    from drella.models import Model, Token

    deps = client.app.state.deps
    real_get = Session.get
    raced = {"done": False}

    def get(self, cls, key, *a, **kw):
        row = real_get(self, cls, key, *a, **kw)
        if cls is Model and key == "racy" and not raced["done"]:
            raced["done"] = True
            with deps.sessions() as other:
                owner = other.scalars(__import__("sqlalchemy").select(Token)).first()
                other.add(Model(alias="racy", owner_token_id=owner.id, interface="openai-compatible",
                                base_url="http://x/v1", model_id="first"))
                other.commit()
            return None
        return row

    monkeypatch.setattr(Session, "get", get)
    r = client.put("/models/racy", json=LOCAL, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["model_id"] == "some-model"


def test_a_model_with_a_key_is_usable_only_by_its_owner(client, auth, runner, settings):
    # Otherwise its owner could repoint it later and receive other tokens' keys.
    client.put("/models/claude", json=CLAUDE, headers=auth)
    other = {"Authorization": f"Bearer {create_token(settings, 'mallory')}"}
    client.put("/credentials/anthropic-key", json={"value": "sk-mallory"}, headers=other)
    r = client.post("/jobs", json={"prompt": "x", "model": "claude"}, headers=other)
    assert r.status_code == 403 and "register your own model" in r.json()["detail"]
    assert runner.secrets == {}
    # A model without a key is anyone's to use.
    r = client.post("/jobs", json={"prompt": "x", "model": "local"}, headers=other)
    assert r.status_code == 201
    # With her own model, Mallory's key goes where she chose.
    client.put("/models/mine", json=CLAUDE, headers=other)
    job = client.post("/jobs", json={"prompt": "x", "model": "mine"}, headers=other).json()
    assert runner.secrets[job["id"]]["_llm-key"] == "sk-mallory"


def test_listing_models_survives_a_row_that_predates_a_rule(client, auth, settings):
    from drella.models import Model

    client.put("/models/claude", json=CLAUDE, headers=auth)
    with client.app.state.deps.sessions() as s:
        s.get(Model, "claude").base_url = "https:"
        s.commit()
    other = {"Authorization": f"Bearer {create_token(settings, 'bob')}"}
    r = client.get("/models", headers=other)
    assert r.status_code == 200
    assert {m["alias"]: m["base_url"] for m in r.json()}["claude"] == "https:"


def test_credentials_must_be_valid_text(client, auth):
    # A lone surrogate, as JSON allows it; it can't be encrypted or used as text.
    body = '{"value": "\\ud800abc"}'
    r = client.put("/credentials/k", content=body,
                   headers=auth | {"Content-Type": "application/json"})
    assert r.status_code == 422


def test_validation_errors_never_echo_the_value(client, auth):
    r = client.put("/credentials/k", json={"value": 12345678}, headers=auth)
    assert r.status_code == 422 and "12345678" not in r.text


def test_model_keys_are_stripped_when_read(client, auth, runner):
    # A credential stored before stripping existed still works as a key.
    from drella import crypto
    from drella.models import Credential, Token

    client.put("/models/claude", json=CLAUDE, headers=auth)
    raw = auth["Authorization"].removeprefix("Bearer ")
    with client.app.state.deps.sessions() as s:
        tok = s.scalars(__import__("sqlalchemy").select(Token)).first()
        s.add(Credential(token_id=tok.id, name="anthropic-key",
                         ciphertext=crypto.encrypt(raw, tok.salt, "sk-old\n")))
        s.commit()
    job = client.post("/jobs", json={"prompt": "x", "model": "claude"}, headers=auth).json()
    assert runner.secrets[job["id"]]["_llm-key"] == "sk-old"
