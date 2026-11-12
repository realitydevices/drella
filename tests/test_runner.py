import json
from unittest.mock import MagicMock

import pytest
from kubernetes.client import (
    V1ContainerState,
    V1ContainerStateTerminated,
    V1ContainerStatus,
    V1Job,
    V1JobCondition,
    V1JobStatus,
    V1ObjectMeta,
    V1Pod,
    V1PodList,
    V1PodStatus,
)
from kubernetes.client.exceptions import ApiException

from drella.adapters import Interface, ModelSpec
from drella.config import Settings
from drella.runner import JOB_TOKEN_KEY, JobSpec, KubernetesRunner, LogsUnavailable, RunState

JOB_ID = "abc123"
NAME = "drella-job-abc123"
LOCAL = ModelSpec("local", Interface.OPENAI_COMPATIBLE, "http://ollama:11434/v1", "some-model")
CLAUDE = ModelSpec(
    "claude", Interface.ANTHROPIC, "https://api.anthropic.com/v1", "claude-x", has_key=True
)
SPEC = JobSpec(JOB_ID, "octo/widgets", LOCAL, "do it")


@pytest.fixture
def k8s():
    batch, core = MagicMock(), MagicMock()
    batch.create_namespaced_job.return_value = V1Job(metadata=V1ObjectMeta(name=NAME, uid="uid-1"))
    core.list_namespaced_pod.return_value = V1PodList(items=[])
    return KubernetesRunner(Settings(), batch=batch, core=core), batch, core


def pod(phase="Running", terminated=None):
    statuses = None
    if terminated:
        statuses = [
            V1ContainerStatus(
                name="job", image="i", image_id="", ready=False, restart_count=0,
                state=V1ContainerState(terminated=V1ContainerStateTerminated(exit_code=137, reason=terminated)),
            )
        ]
    return V1Pod(metadata=V1ObjectMeta(name=NAME + "-xyz"), status=V1PodStatus(phase=phase, container_statuses=statuses))


def test_prepare_creates_the_secret(k8s):
    r, _, core = k8s
    r.prepare(JOB_ID, {"github": "ghp", JOB_TOKEN_KEY: "drj"})
    ns, body = core.create_namespaced_secret.call_args.args
    assert ns == "drella"
    assert body["metadata"]["name"] == NAME
    assert body["stringData"] == {"github": "ghp", JOB_TOKEN_KEY: "drj"}


def test_launch_creates_job_with_limits_and_hands_it_the_secret(k8s):
    r, batch, core = k8s
    r.launch(SPEC)
    ns, job = batch.create_namespaced_job.call_args.args
    assert ns == "drella" and job["metadata"]["name"] == NAME
    spec = job["spec"]
    assert spec["activeDeadlineSeconds"] == 3600
    assert spec["backoffLimit"] == 0
    pod_spec = spec["template"]["spec"]
    assert pod_spec["restartPolicy"] == "Never"
    assert pod_spec["automountServiceAccountToken"] is False
    (c,) = pod_spec["containers"]
    assert c["resources"]["limits"]["memory"] == "2Gi"
    env = {e["name"]: e for e in c["env"]}
    assert env["DRELLA_REPO"]["value"] == SPEC.repo
    assert env["DRELLA_PROMPT"]["value"] == SPEC.prompt
    assert env["DRELLA_PREFLIGHT_URL"]["value"] == "http://ollama:11434/v1/models"
    assert json.loads(env["DRELLA_OPENCODE_CONFIG"]["value"])["model"] == "drella/some-model"
    # No key for this model, so none is mapped in.
    assert "DRELLA_LLM_KEY" not in env
    assert env["DRELLA_JOB_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": NAME, "key": JOB_TOKEN_KEY}
    # Only the GitHub credential is a file; the job token and model key are env vars.
    assert {
        "name": "creds",
        "secret": {"secretName": NAME, "items": [{"key": "github", "path": "github"}]},
    } in pod_spec["volumes"]

    # The Secret is owned by the Job, so it's deleted with it.
    name, ns, patch = core.patch_namespaced_secret.call_args.args
    assert name == NAME
    (owner,) = patch["metadata"]["ownerReferences"]
    assert owner == {"apiVersion": "batch/v1", "kind": "Job", "name": NAME, "uid": "uid-1"}


def test_launch_cleans_up_if_secret_cannot_be_adopted(k8s):
    r, batch, core = k8s
    core.patch_namespaced_secret.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        r.launch(SPEC)
    batch.delete_namespaced_job.assert_called_once()
    core.delete_namespaced_secret.assert_called_once()


@pytest.mark.parametrize(
    "status, state",
    [
        (V1JobStatus(active=1), RunState.RUNNING),
        (V1JobStatus(), RunState.RUNNING),
        (V1JobStatus(succeeded=1, conditions=[V1JobCondition(type="Complete", status="True")]), RunState.SUCCEEDED),
        (V1JobStatus(failed=1), RunState.FAILED),
    ],
)
def test_status(k8s, status, state):
    r, batch, _ = k8s
    batch.read_namespaced_job_status.return_value = V1Job(status=status)
    assert r.status(JOB_ID).state == state


def test_status_reports_why_it_failed(k8s):
    r, batch, core = k8s
    batch.read_namespaced_job_status.return_value = V1Job(
        status=V1JobStatus(conditions=[V1JobCondition(type="Failed", status="True", reason="DeadlineExceeded", message="Job was active longer than specified deadline")])
    )
    assert r.status(JOB_ID).message == "DeadlineExceeded Job was active longer than specified deadline"
    core.list_namespaced_pod.return_value = V1PodList(items=[pod("Failed", terminated="OOMKilled")])
    assert r.status(JOB_ID).message == "OOMKilled (exit code 137)"


def test_status_of_missing_job(k8s):
    r, batch, _ = k8s
    batch.read_namespaced_job_status.side_effect = ApiException(status=404)
    assert r.status(JOB_ID).state == RunState.MISSING


def test_logs_stream_from_the_pod(k8s):
    r, _, core = k8s
    core.list_namespaced_pod.return_value = V1PodList(items=[pod()])
    resp = MagicMock()
    resp.stream.return_value = iter([b"a\n", b"b\n"])
    core.read_namespaced_pod_log.return_value = resp
    assert b"".join(r.logs(JOB_ID, follow=True)) == b"a\nb\n"
    assert core.read_namespaced_pod_log.call_args.kwargs["follow"] is True
    assert core.list_namespaced_pod.call_args.kwargs["label_selector"] == f"drella.dev/job-id={JOB_ID}"
    resp.release_conn.assert_called_once()


def test_logs_unavailable_while_pod_pending(k8s):
    r, _, core = k8s
    with pytest.raises(LogsUnavailable):
        r.logs(JOB_ID)
    core.list_namespaced_pod.return_value = V1PodList(items=[pod("Pending")])
    with pytest.raises(LogsUnavailable):
        r.logs(JOB_ID, follow=True, wait=0)


def test_cleanup_ignores_already_deleted(k8s):
    r, batch, core = k8s
    batch.delete_namespaced_job.side_effect = ApiException(status=404)
    core.delete_namespaced_secret.side_effect = ApiException(status=404)
    r.cleanup(JOB_ID)
    assert batch.delete_namespaced_job.call_args.kwargs["propagation_policy"] == "Background"


def test_launch_without_repo_omits_drella_repo(k8s):
    r, batch, _ = k8s
    r.launch(JobSpec(JOB_ID, None, LOCAL, "summarise"))
    _, job = batch.create_namespaced_job.call_args.args
    (c,) = job["spec"]["template"]["spec"]["containers"]
    assert "DRELLA_REPO" not in {e["name"] for e in c["env"]}
    # No repo, so no GitHub credential and nothing mounted at /creds.
    assert "creds" not in {v["name"] for v in job["spec"]["template"]["spec"]["volumes"]}
    assert "/creds" not in {m["mountPath"] for m in c["volumeMounts"]}


def test_launch_adopts_a_job_that_already_exists(k8s):
    # Drella restarted after creating the Job but before recording it as running.
    r, batch, core = k8s
    batch.create_namespaced_job.side_effect = ApiException(status=409)
    batch.read_namespaced_job.return_value = V1Job(metadata=V1ObjectMeta(name=NAME, uid="uid-old"))
    r.launch(SPEC)
    patch = core.patch_namespaced_secret.call_args.args[2]
    assert patch["metadata"]["ownerReferences"][0]["uid"] == "uid-old"
    batch.delete_namespaced_job.assert_not_called()


def test_job_pod_is_hardened_and_has_a_deadline(k8s):
    import time

    r, batch, _ = k8s
    r.launch(SPEC)
    _, job = batch.create_namespaced_job.call_args.args
    pod = job["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert pod["terminationGracePeriodSeconds"] == 120
    (c,) = pod["containers"]
    assert c["securityContext"] == {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    assert c["resources"]["limits"]["ephemeral-storage"] == "10Gi"
    env = {e["name"]: e.get("value") for e in c["env"]}
    deadline = int(env["DRELLA_DEADLINE"])
    assert abs(deadline - (time.time() + 3600)) < 60
    assert env["DRELLA_AGENT_GRACE"] == "300"
    assert env["DRELLA_TERM_GRACE"] == "120"


def test_job_secrets_lists_secrets_by_job_id(k8s):
    from datetime import UTC, datetime

    from kubernetes.client import V1Secret, V1SecretList

    r, _, core = k8s
    t = datetime(2026, 11, 9, tzinfo=UTC)
    core.list_namespaced_secret.return_value = V1SecretList(
        items=[V1Secret(metadata=V1ObjectMeta(labels={"drella.dev/job-id": "j1"}, creation_timestamp=t))]
    )
    assert r.job_secrets() == {"j1": t}


def test_launch_maps_the_model_key_from_the_secret(k8s):
    r, batch, _ = k8s
    r.launch(JobSpec(JOB_ID, None, CLAUDE, "summarise"))
    _, job = batch.create_namespaced_job.call_args.args
    (c,) = job["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e for e in c["env"]}
    assert env["DRELLA_LLM_KEY"]["valueFrom"]["secretKeyRef"] == {"name": NAME, "key": "_llm-key"}
    assert env["DRELLA_PREFLIGHT_HEADERS"]["value"] == (
        "x-api-key: {key}\nanthropic-version: 2023-06-01"
    )
    # The key itself is never in the manifest.
    assert "sk-" not in json.dumps(job)


def test_launch_with_claude_code(k8s):
    r, batch, _ = k8s
    cc = ModelSpec("cc", Interface.CLAUDE_CODE, None, "sonnet", has_key=True)
    r.launch(JobSpec(JOB_ID, "octo/widgets", cc, "do it"))
    _, job = batch.create_namespaced_job.call_args.args
    (c,) = job["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e for e in c["env"]}
    assert env["DRELLA_AGENT"]["value"] == "claude-code"
    assert env["DRELLA_AGENT_MODEL"]["value"] == "sonnet"
    # The OAuth token comes from the Secret, like any model key; never in the manifest.
    assert env["DRELLA_LLM_KEY"]["valueFrom"]["secretKeyRef"] == {"name": NAME, "key": "_llm-key"}
    assert "sk-" not in json.dumps(job)
    # No OpenCode config or preflight for Claude Code.
    assert not {"DRELLA_OPENCODE_CONFIG", "DRELLA_PREFLIGHT_URL"} & set(env)


def test_opencode_jobs_name_their_agent(k8s):
    r, batch, _ = k8s
    r.launch(SPEC)
    _, job = batch.create_namespaced_job.call_args.args
    (c,) = job["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["DRELLA_AGENT"] == "opencode" and "DRELLA_OPENCODE_CONFIG" in env


def test_add_secrets_patches_the_job_secret(k8s):
    r, _, core = k8s
    r.add_secrets(JOB_ID, {"github": "ghs_minted"})
    name, ns, patch = core.patch_namespaced_secret.call_args.args
    assert (name, ns) == (NAME, "drella")
    assert patch == {"data": {"github": "Z2hzX21pbnRlZA=="}}


def _env(r, batch, spec):
    r.launch(spec)
    _, job = batch.create_namespaced_job.call_args.args
    (c,) = job["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e.get("value") for e in c["env"]}


def test_only_a_read_only_repo_job_is_told_so(k8s):
    r, batch, _ = k8s
    assert "DRELLA_REPO_READ_ONLY" not in _env(r, batch, SPEC)
    assert "DRELLA_REPO_REPORT" not in _env(r, batch, SPEC)
    ro = JobSpec(JOB_ID, "octo/widgets", LOCAL, "do it", opens_prs=False, read_only=True)
    assert _env(r, batch, ro)["DRELLA_REPO_READ_ONLY"] == "1"
    assert _env(r, batch, ro)["DRELLA_REPO_REPORT"] == "1"
    no_repo = JobSpec(JOB_ID, None, LOCAL, "do it", opens_prs=False)
    assert "DRELLA_REPO_READ_ONLY" not in _env(r, batch, no_repo)
    assert "DRELLA_REPO_REPORT" not in _env(r, batch, no_repo)


def test_a_superstar_job_reports_to_out_but_its_token_can_write(k8s):
    # superstar (issue #73): its outcome is a report in /out, like a reader's, but its
    # GitHub token can write, so the job isn't told it's read-only.
    r, batch, _ = k8s
    spec = JobSpec(JOB_ID, "octo/widgets", LOCAL, "do it", opens_prs=False, read_only=False)
    env = _env(r, batch, spec)
    assert env["DRELLA_REPO_REPORT"] == "1"
    assert "DRELLA_REPO_READ_ONLY" not in env


def test_the_deadline_never_passes_the_github_tokens_expiry(k8s):
    import time

    r, batch, _ = k8s
    now = time.time()
    # No App token: the job's own time limit.
    assert abs(int(_env(r, batch, SPEC)["DRELLA_DEADLINE"]) - (now + 3600)) < 5
    # A token that expires sooner than the time limit (minted a while before launch):
    # the deadline is the expiry, so the wrapper is done before the token runs out.
    early = JobSpec(JOB_ID, "octo/widgets", LOCAL, "do it", github_token_expires=now + 3000)
    assert int(_env(r, batch, early)["DRELLA_DEADLINE"]) == int(now + 3000)
    late = JobSpec(JOB_ID, "octo/widgets", LOCAL, "do it", github_token_expires=now + 9000)
    assert abs(int(_env(r, batch, late)["DRELLA_DEADLINE"]) - (now + 3600)) < 5


def test_pr_is_passed_to_the_pod_only_when_set(k8s):
    import dataclasses

    r, batch, _ = k8s

    def env_names():
        _, job = batch.create_namespaced_job.call_args.args
        (c,) = job["spec"]["template"]["spec"]["containers"]
        return {e["name"]: e.get("value") for e in c["env"]}

    r.launch(SPEC)
    assert "DRELLA_PR" not in env_names()
    r.launch(dataclasses.replace(SPEC, pr=58))
    assert env_names()["DRELLA_PR"] == "58"
