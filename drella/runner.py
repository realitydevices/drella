"""The job launcher [18]. Kubernetes is the only implementation."""

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from drella.config import Settings

log = logging.getLogger(__name__)

# Secret key holding the job-scoped token. Credential names can't start with "_".
JOB_TOKEN_KEY = "_job-token"


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    repo: str
    model: str
    endpoint: str
    prompt: str


class RunState(StrEnum):
    RUNNING = "running"  # created; pod pending or running
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MISSING = "missing"  # no such Job (deleted, or never created)


@dataclass(frozen=True)
class RunStatus:
    state: RunState
    message: str | None = None


class LogsUnavailable(Exception):
    pass


class Runner(Protocol):
    def prepare(self, job_id: str, secrets: dict[str, str]) -> None:
        """Store the job's decrypted credentials, ready for launch()."""

    def launch(self, spec: JobSpec) -> None:
        """Start the job. The Secret from prepare() becomes owned by it."""

    def status(self, job_id: str) -> RunStatus: ...

    def logs(self, job_id: str, follow: bool = False) -> Iterator[bytes]:
        """Stream the job's log. Raises LogsUnavailable if there's no pod to read."""

    def cleanup(self, job_id: str) -> None:
        """Delete the job and its Secret. Safe to call more than once."""


def k8s_name(job_id: str) -> str:
    return f"drella-job-{job_id}"


class KubernetesRunner:
    def __init__(self, settings: Settings, batch=None, core=None):
        self.settings = settings
        self.ns = settings.namespace
        if batch is None or core is None:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            batch, core = client.BatchV1Api(), client.CoreV1Api()
        self.batch, self.core = batch, core

    # -- prepare / launch ------------------------------------------------------

    def prepare(self, job_id: str, secrets: dict[str, str]) -> None:
        self.core.create_namespaced_secret(
            self.ns,
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": k8s_name(job_id), "labels": self._labels(job_id)},
                "type": "Opaque",
                "stringData": secrets,
            },
        )

    def launch(self, spec: JobSpec) -> None:
        job = self.batch.create_namespaced_job(self.ns, self._job_manifest(spec))
        uid = job.metadata.uid
        try:
            # Owned by the Job, so Kubernetes deletes the Secret along with it.
            self.core.patch_namespaced_secret(
                k8s_name(spec.job_id),
                self.ns,
                {
                    "metadata": {
                        "ownerReferences": [
                            {
                                "apiVersion": "batch/v1",
                                "kind": "Job",
                                "name": k8s_name(spec.job_id),
                                "uid": uid,
                            }
                        ]
                    }
                },
            )
        except Exception:
            self.cleanup(spec.job_id)
            raise

    def _labels(self, job_id: str) -> dict[str, str]:
        return {"app.kubernetes.io/name": "drella-job", "drella.dev/job-id": job_id}

    def _job_manifest(self, spec: JobSpec) -> dict:
        name = k8s_name(spec.job_id)
        s = self.settings
        env = {
            "DRELLA_API_URL": s.api_url,
            "DRELLA_JOB_ID": spec.job_id,
            "DRELLA_REPO": spec.repo,
            "DRELLA_MODEL": spec.model,
            "DRELLA_ENDPOINT": spec.endpoint,
            "DRELLA_PROMPT": spec.prompt,
        }
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "labels": self._labels(spec.job_id)},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": s.job_timeout_seconds,
                "template": {
                    "metadata": {"labels": self._labels(spec.job_id)},
                    "spec": {
                        "restartPolicy": "Never",
                        # Job pods talk only to the Drella API, never to Kubernetes.
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "job",
                                "image": s.job_image,
                                "env": [{"name": k, "value": v} for k, v in env.items()]
                                + [
                                    {
                                        "name": "DRELLA_JOB_TOKEN",
                                        "valueFrom": {
                                            "secretKeyRef": {"name": name, "key": JOB_TOKEN_KEY}
                                        },
                                    }
                                ],
                                "resources": {
                                    "requests": {"memory": "512Mi", "cpu": "250m"},
                                    "limits": {"memory": s.job_memory},
                                },
                                "volumeMounts": [
                                    {"name": "creds", "mountPath": "/creds", "readOnly": True},
                                    {"name": "out", "mountPath": "/out"},
                                    {"name": "work", "mountPath": "/work"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "creds", "secret": {"secretName": name}},
                            {"name": "out", "emptyDir": {}},
                            {"name": "work", "emptyDir": {}},
                        ],
                    },
                },
            },
        }

    # -- status / logs / cleanup ----------------------------------------------

    def status(self, job_id: str) -> RunStatus:
        from kubernetes.client.exceptions import ApiException

        try:
            job = self.batch.read_namespaced_job_status(k8s_name(job_id), self.ns)
        except ApiException as e:
            if e.status == 404:
                return RunStatus(RunState.MISSING, "Kubernetes Job not found")
            raise
        st = job.status
        for cond in st.conditions or []:
            if cond.status != "True":
                continue
            if cond.type == "Complete":
                return RunStatus(RunState.SUCCEEDED)
            if cond.type == "Failed":
                return RunStatus(RunState.FAILED, self._failure(job_id, cond))
        if st.succeeded:
            return RunStatus(RunState.SUCCEEDED)
        if st.failed:
            return RunStatus(RunState.FAILED, self._failure(job_id, None))
        return RunStatus(RunState.RUNNING)

    def _failure(self, job_id: str, cond) -> str:
        """Why the job failed, e.g. 'DeadlineExceeded' or 'OOMKilled'."""
        pod = self._pod(job_id)
        if pod is not None:
            for cs in pod.status.container_statuses or []:
                term = cs.state and cs.state.terminated
                if term and term.reason and term.reason != "Error":
                    return f"{term.reason} (exit code {term.exit_code})"
        if cond is not None:
            return " ".join(filter(None, [cond.reason, cond.message])) or "failed"
        return "failed"

    def _pod(self, job_id: str):
        pods = self.core.list_namespaced_pod(
            self.ns, label_selector=f"drella.dev/job-id={job_id}"
        ).items
        return pods[0] if pods else None

    def logs(self, job_id: str, follow: bool = False, wait: float = 300) -> Iterator[bytes]:
        pod = self._pod(job_id)
        # A pod that hasn't started yet has no log. When following, wait for it.
        deadline = time.monotonic() + (wait if follow else 0)
        while pod is None or pod.status.phase == "Pending":
            if time.monotonic() >= deadline:
                raise LogsUnavailable("the job's pod hasn't started")
            time.sleep(2)
            pod = self._pod(job_id)
        resp = self.core.read_namespaced_pod_log(
            pod.metadata.name, self.ns, follow=follow, _preload_content=False
        )
        return self._stream(resp)

    @staticmethod
    def _stream(resp) -> Iterator[bytes]:
        try:
            yield from resp.stream(4096)
        finally:
            resp.release_conn()

    def cleanup(self, job_id: str) -> None:
        from kubernetes.client.exceptions import ApiException

        name = k8s_name(job_id)
        for delete in (
            lambda: self.batch.delete_namespaced_job(
                name, self.ns, propagation_policy="Background"
            ),
            # Normally garbage-collected with the Job; this covers a job that was
            # never launched.
            lambda: self.core.delete_namespaced_secret(name, self.ns),
        ):
            try:
                delete()
            except ApiException as e:
                if e.status != 404:
                    raise
