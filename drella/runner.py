"""The job launcher [18]. Kubernetes is the only implementation."""

import base64
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from drella import adapters
from drella.adapters import ModelSpec
from drella.config import Settings

log = logging.getLogger(__name__)

# Secret key holding the job-scoped token. Credential names can't start with "_".
JOB_TOKEN_KEY = "_job-token"


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    repo: str | None
    model: ModelSpec
    prompt: str
    # A repo job pushes a branch and opens a PR, unless its GitHub token is a profile
    # that doesn't open PRs: then its outcome is the report it writes to /out.
    opens_prs: bool = True
    # The job's GitHub token can't write at all (a read-only profile). Only meaningful
    # for a repo job that doesn't open PRs.
    read_only: bool = False
    # When the job's GitHub App token expires (epoch seconds), if it has one. The job's
    # deadline is capped at it, so the token outlasts everything the wrapper does.
    github_token_expires: float | None = None
    # An existing PR in `repo` for the job to push fixes to, instead of opening a new one.
    pr: int | None = None


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

    def add_secrets(self, job_id: str, secrets: dict[str, str]) -> None:
        """Add to (or replace keys in) the job's Secret before launch(), e.g. a GitHub
        token minted at launch."""

    def launch(self, spec: JobSpec) -> None:
        """Start the job. The Secret from prepare() becomes owned by it."""

    def status(self, job_id: str) -> RunStatus: ...

    def logs(self, job_id: str, follow: bool = False) -> Iterator[bytes]:
        """Stream the job's log. Raises LogsUnavailable if there's no pod to read."""

    def cleanup(self, job_id: str) -> None:
        """Delete the job and its Secret. Safe to call more than once."""

    def job_secrets(self) -> dict[str, datetime]:
        """Job id -> creation time, for every job Secret that exists."""


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
        # Every non-streaming call gets a timeout, so a hung connection can't stall
        # the queue.
        self.t = settings.k8s_timeout_seconds

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
            _request_timeout=self.t,
        )

    def add_secrets(self, job_id: str, secrets: dict[str, str]) -> None:
        # `data` (base64), not `stringData`, so the patch merges like any other field.
        self.core.patch_namespaced_secret(
            k8s_name(job_id),
            self.ns,
            {"data": {k: base64.b64encode(v.encode()).decode() for k, v in secrets.items()}},
            _request_timeout=self.t,
        )

    def launch(self, spec: JobSpec) -> None:
        from kubernetes.client.exceptions import ApiException

        try:
            job = self.batch.create_namespaced_job(
                self.ns, self._job_manifest(spec), _request_timeout=self.t
            )
        except ApiException as e:
            if e.status != 409:
                raise
            # Already exists: Drella restarted between creating the Job and recording
            # it as running. Adopt it rather than failing a job that's running.
            log.info("job %s already exists in Kubernetes; adopting it", spec.job_id)
            job = self.batch.read_namespaced_job(
                k8s_name(spec.job_id), self.ns, _request_timeout=self.t
            )
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
                _request_timeout=self.t,
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
            "DRELLA_PROMPT": spec.prompt,
        }
        # The job's model, rendered by its adapter [27, 28]. The wrapper has no
        # provider-specific code: it runs the agent the adapter names, writes the
        # config and runs the check.
        m = spec.model
        agent = adapters.agent(m)
        env["DRELLA_AGENT"] = agent.value
        if agent == adapters.Agent.CLAUDE_CODE:
            env["DRELLA_MODEL_LABEL"] = f"{m.alias} ({m.model_id} with Claude Code)"
            env["DRELLA_AGENT_MODEL"] = m.model_id
        else:
            check = adapters.preflight(m)
            env["DRELLA_MODEL_LABEL"] = f"{m.alias} ({m.model_id} at {m.base_url})"
            env["DRELLA_OPENCODE_CONFIG"] = json.dumps(adapters.opencode_config(m))
            env["DRELLA_PREFLIGHT_URL"] = check.url
            env["DRELLA_PREFLIGHT_HEADERS"] = "\n".join(check.headers)
        if spec.repo:
            env["DRELLA_REPO"] = spec.repo
            if not spec.opens_prs:
                env["DRELLA_REPO_REPORT"] = "1"
            if spec.read_only:
                env["DRELLA_REPO_READ_ONLY"] = "1"
            if spec.pr is not None:
                env["DRELLA_PR"] = str(spec.pr)
        # The soft limit: the wrapper stops the agent this long before the hard limit
        # (activeDeadlineSeconds), which runs from roughly now. The wrapper finishes
        # everything (PR check, auto-merge, report) before DRELLA_DEADLINE, so capping it
        # at the GitHub token's expiry means the token never runs out mid-job.
        deadline = int(time.time()) + s.job_timeout_seconds
        if spec.github_token_expires is not None:
            deadline = min(deadline, int(spec.github_token_expires))
        env["DRELLA_DEADLINE"] = str(deadline)
        env["DRELLA_AGENT_GRACE"] = str(s.agent_grace_seconds)
        # Once told to stop, the wrapper has this long to report before it's killed.
        env["DRELLA_TERM_GRACE"] = str(s.job_termination_grace_seconds)
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
                        # Time to upload and report after being told to stop.
                        "terminationGracePeriodSeconds": s.job_termination_grace_seconds,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 1000,
                            "runAsGroup": 1000,
                            "fsGroup": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "job",
                                "image": s.job_image,
                                "env": [{"name": k, "value": v} for k, v in env.items()]
                                + self._secret_env(name, spec),
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "requests": {
                                        "memory": "512Mi",
                                        "cpu": "250m",
                                        "ephemeral-storage": "1Gi",
                                    },
                                    "limits": {
                                        "memory": s.job_memory,
                                        "ephemeral-storage": s.job_disk,
                                    },
                                },
                                "volumeMounts": self._creds_mount(spec)
                                + [
                                    {"name": "out", "mountPath": "/out"},
                                    {"name": "work", "mountPath": "/work"},
                                ],
                            }
                        ],
                        "volumes": self._creds_volume(name, spec)
                        + [
                            {"name": "out", "emptyDir": {"sizeLimit": "1Gi"}},
                            {"name": "work", "emptyDir": {"sizeLimit": s.job_disk}},
                        ],
                    },
                },
            },
        }

    # Only the GitHub credential is a file (/creds/github), and only for a repo job.
    # The job token and the model's key reach the pod as env vars, not files.
    @staticmethod
    def _creds_mount(spec: JobSpec) -> list[dict]:
        if not spec.repo:
            return []
        return [{"name": "creds", "mountPath": "/creds", "readOnly": True}]

    @staticmethod
    def _creds_volume(name: str, spec: JobSpec) -> list[dict]:
        if not spec.repo:
            return []
        return [
            {
                "name": "creds",
                "secret": {"secretName": name, "items": [{"key": "github", "path": "github"}]},
            }
        ]

    @staticmethod
    def _secret_env(name: str, spec: JobSpec) -> list[dict]:
        """Env vars from the job's Secret: its token, and its model's key if any."""
        pairs = [("DRELLA_JOB_TOKEN", JOB_TOKEN_KEY)]
        if spec.model.has_key:
            pairs.append((adapters.LLM_KEY_ENV, adapters.LLM_KEY_SECRET))
        return [
            {"name": env, "valueFrom": {"secretKeyRef": {"name": name, "key": key}}}
            for env, key in pairs
        ]

    # -- status / logs / cleanup ----------------------------------------------

    def status(self, job_id: str) -> RunStatus:
        from kubernetes.client.exceptions import ApiException

        try:
            job = self.batch.read_namespaced_job_status(
                k8s_name(job_id), self.ns, _request_timeout=self.t
            )
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
            self.ns, label_selector=f"drella.dev/job-id={job_id}", _request_timeout=self.t
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
            pod.metadata.name,
            self.ns,
            follow=follow,
            _preload_content=False,
            # Following has no overall timeout; a one-off read does.
            _request_timeout=None if follow else self.t,
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
                name, self.ns, propagation_policy="Background", _request_timeout=self.t
            ),
            # Normally garbage-collected with the Job; this covers a job that was
            # never launched.
            lambda: self.core.delete_namespaced_secret(name, self.ns, _request_timeout=self.t),
        ):
            try:
                delete()
            except ApiException as e:
                if e.status != 404:
                    raise

    def job_secrets(self) -> dict[str, datetime]:
        secrets = self.core.list_namespaced_secret(
            self.ns, label_selector="drella.dev/job-id", _request_timeout=self.t
        ).items
        return {
            sec.metadata.labels["drella.dev/job-id"]: sec.metadata.creation_timestamp
            for sec in secrets
        }
