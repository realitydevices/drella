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

from drella.config import Settings
from drella.runner import JOB_TOKEN_KEY, JobSpec, KubernetesRunner, LogsUnavailable, RunState

JOB_ID = "abc123"
NAME = "drella-job-abc123"
SPEC = JobSpec(JOB_ID, "realitydevices/drella", "qwen", "http://ollama:11434/v1", "do it")


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
    assert env["DRELLA_ENDPOINT"]["value"] == SPEC.endpoint
    assert env["DRELLA_JOB_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": NAME, "key": JOB_TOKEN_KEY}
    assert {"name": "creds", "secret": {"secretName": NAME}} in pod_spec["volumes"]

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
