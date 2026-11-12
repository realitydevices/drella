"""The queue loop [22].

v0 has one queue group with concurrency 1. Each tick:
1. checks each running job on its own, so one bad job can't stall the others.
   While a job runs, its log is copied to storage every so often, so the log
   survives the pod [21]. When its Kubernetes Job has ended, the log is archived
   once more, the final status is settled, and the Job is deleted (its Secret
   goes with it);
2. launches the oldest queued jobs while there's room;
3. now and then, deletes job Secrets whose job has finished or doesn't exist.
"""

import dataclasses
import itertools
import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from drella.adapters import Interface, ModelSpec
from drella.github import PROFILES, GitHubApp, GitHubAppError
from drella.models import FAILED, QUEUED, RUNNING, SUCCEEDED, Job, Token
from drella.runner import JobSpec, LogsUnavailable, Runner, RunState, is_transient
from drella.storage import CHUNK, NotFound, Storage

log = logging.getLogger(__name__)

# A Secret younger than this is never reaped: its job may not be committed yet.
SECRET_MIN_AGE = timedelta(minutes=10)
REAP_EVERY = 60  # seconds
# Minting a GitHub token: up to this many attempts, waiting these seconds between them,
# and only for failures that may pass (GitHub 5xx, 429, network errors).
MINT_ATTEMPTS = 3
MINT_BACKOFF = (0.5, 1.0)
# runner.launch (or adding a minted GitHub token to the job's Secret) failing
# transiently, e.g. the API server is briefly down: the job stays queued and is retried
# after min(LAUNCH_BACKOFF * 2^(attempts-1), LAUNCH_BACKOFF_MAX) seconds, attempts
# counting this failure (30 s, 60 s, 120 s, ...); after LAUNCH_ATTEMPTS failures it
# fails.
LAUNCH_ATTEMPTS = 6
LAUNCH_BACKOFF = 30
LAUNCH_BACKOFF_MAX = 600


class _RetryLaunch(Exception):
    """A launch step failed in a way that may pass. `reason` goes into the job's
    message and `detail` into the log; neither quotes anything secret."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _reason(e: Exception) -> str:
    reason = type(e).__name__
    if getattr(e, "status", None):
        reason += f" {e.status}"
    return reason


def log_key(job_id: str) -> str:
    """The archived log: its current segment (see Queue._archive_log)."""
    return f"jobs/{job_id}/log.txt"


def log_segment_key(job_id: str, n: int) -> str:
    """An earlier segment of the archived log, from before a rotation."""
    return f"jobs/{job_id}/log/{n:04d}.txt"


ROTATED = b"\n[drella] the pod's log was rotated here; some output may be missing\n"


def archived_log(storage: Storage, job_id: str) -> Iterator[bytes]:
    """The archived log: earlier segments, then the current one. Raises NotFound if
    nothing was archived."""
    keys = storage.list(f"jobs/{job_id}/log/")
    if storage.size(log_key(job_id)) is not None:
        keys.append(log_key(job_id))
    if not keys:
        raise NotFound(log_key(job_id))
    parts = []
    for i, key in enumerate(keys):
        if i:
            parts.append([ROTATED])
        parts.append(storage.get(key))
    return itertools.chain.from_iterable(parts)


def output_prefix(job_id: str) -> str:
    return f"jobs/{job_id}/outputs/"


def model_spec(job: Job) -> ModelSpec:
    """The job's model, from its snapshot."""
    return ModelSpec(
        alias=job.model,
        interface=Interface(job.interface),
        base_url=job.base_url,
        model_id=job.model_id,
        has_key=job.credential is not None,
        context_length=job.context_length,
        max_output_tokens=job.max_output_tokens,
    )


class Queue:
    def __init__(
        self,
        sessions: sessionmaker,
        runner: Runner,
        storage: Storage,
        concurrency: int = 1,
        interval: float = 5,
        log_snapshot_seconds: float = 30,
        stall_seconds: float = 300,
        github_app: GitHubApp | None = None,
        sleep=time.sleep,
    ):
        self.sessions = sessions
        self.runner = runner
        self.storage = storage
        self.concurrency = concurrency
        self.interval = interval
        self.log_snapshot_seconds = log_snapshot_seconds
        self.stall_seconds = stall_seconds
        self.github_app = github_app
        self._sleep = sleep  # injectable, so tests don't wait
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_tick = time.monotonic()
        self._last_snapshot: dict[str, float] = {}
        self._last_reap: float | None = None  # None: never

    # -- loop ----------------------------------------------------------------

    def start(self) -> None:
        self._last_tick = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="drella-queue", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=30)

    def wake(self) -> None:
        """Run a tick now rather than at the next interval (e.g. a job was queued)."""
        self._wake.set()

    def alive(self) -> bool:
        """False if the loop hasn't completed a tick for `stall_seconds`."""
        return time.monotonic() - self._last_tick < self.stall_seconds

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("queue tick failed")
            self._wake.wait(self.interval)
            self._wake.clear()

    # -- one tick ------------------------------------------------------------

    def tick(self) -> None:
        with self.sessions() as s:
            for job in s.scalars(select(Job).where(Job.status == RUNNING)).all():
                try:
                    self._check(s, job)
                except Exception:
                    log.exception("checking job %s failed", job.id)
                    s.rollback()
            running = s.scalar(select(func.count()).select_from(Job).where(Job.status == RUNNING))
            while running < self.concurrency:
                now = datetime.now(UTC)
                job = s.scalars(
                    select(Job)
                    .where(Job.status == QUEUED)
                    .where(or_(Job.not_before.is_(None), Job.not_before <= now))
                    .order_by(Job.created_at)
                    .limit(1)
                ).first()
                if job is None:
                    break
                if not self._launch(s, job):
                    # Backing off: the cluster is likely down, so wait for the next tick.
                    break
                running += 1
            if self._last_reap is None or time.monotonic() - self._last_reap >= REAP_EVERY:
                self._last_reap = time.monotonic()
                try:
                    self._reap_secrets(s)
                except Exception:
                    log.exception("reaping job Secrets failed")
        self._last_tick = time.monotonic()

    def _launch(self, s: Session, job: Job) -> bool:
        """Launch the job. False if it stays queued to be retried later."""
        spec = JobSpec(job.id, job.repo, model_spec(job), job.prompt)
        try:
            if job.github_app and job.repo:
                spec = self._mint_github_token(s, job, spec)
            try:
                self.runner.launch(spec)
            except Exception as e:
                if not is_transient(e):
                    raise
                raise _RetryLaunch(_reason(e), str(e)) from e
        except _RetryLaunch as e:
            return self._retry_launch(s, job, e)
        except Exception as e:
            # A failed mint fails the job rather than leaving it queued: the queue
            # always takes the oldest queued job, so one that can't launch would block
            # every job behind it. GitHubAppError messages carry no secrets.
            log.exception("launching job %s failed", job.id)
            self._finish(s, job, FAILED, f"launch failed: {e}")
            self._cleanup(job.id)
            return True
        job.status = RUNNING
        job.started_at = datetime.now(UTC)
        job.not_before = None
        s.commit()
        log.info("job %s launched", job.id)
        return True

    def _retry_launch(self, s: Session, job: Job, e: _RetryLaunch) -> bool:
        """Launching failed in a way that may pass. Undo this attempt (not the job's
        Secret, which a retry needs) and leave the job queued, not to be tried before a
        backoff has passed; or fail it after LAUNCH_ATTEMPTS."""
        attempts = job.launch_attempts or 0
        reason = e.reason
        try:
            # No Job left behind, nor a GitHub token Drella minted (it's minted again
            # next time). A caller's own `github` credential stays: it exists only in
            # the Secret.
            self.runner.reset_launch(job.id, remove_github=bool(job.github_app and job.repo))
        except Exception:
            log.exception("undoing the launch of job %s failed", job.id)
        attempts += 1
        job.launch_attempts = attempts
        if attempts >= LAUNCH_ATTEMPTS:
            log.error("launching job %s failed %d times (%s)", job.id, attempts, e.detail)
            self._finish(s, job, FAILED, f"launch failed after {attempts} attempts: {reason}")
            self._cleanup(job.id)
            return True
        delay = min(LAUNCH_BACKOFF * 2 ** (attempts - 1), LAUNCH_BACKOFF_MAX)
        job.not_before = datetime.now(UTC) + timedelta(seconds=delay)
        s.commit()
        log.warning(
            "launching job %s failed (%s); attempt %d of %d, retrying in %ds",
            job.id, e.detail, attempts, LAUNCH_ATTEMPTS, delay,
        )
        return False

    def _mint_github_token(self, s: Session, job: Job, spec: JobSpec) -> JobSpec:
        """The job's GitHub token, minted at dispatch rather than at job creation: a
        queued job can wait longer than a token lasts (an hour). Its permissions are its
        Drella token's GitHub profile, read now, so a profile an admin has narrowed
        applies to jobs still queued. It goes into the job's Secret where a caller's
        `github` credential would be, and never into the database. The job's deadline is
        capped at the token's expiry, so the token outlasts everything the job does."""
        if self.github_app is None:
            raise RuntimeError(
                "the job needs a token from Drella's GitHub App, which is no longer"
                " configured (DRELLA_GITHUB_APP_ID)"
            )
        token_row = s.get(Token, job.token_id)
        profile = PROFILES.get(token_row.github_profile) if token_row else None
        if profile is None:
            raise RuntimeError("the job's Drella token has no known GitHub profile")
        for attempt in range(MINT_ATTEMPTS):
            try:
                token = self.github_app.token(job.repo, profile)
                break
            except GitHubAppError as e:
                if not e.transient or attempt == MINT_ATTEMPTS - 1:
                    raise
                log.warning("minting a GitHub token for job %s failed (%s); retrying", job.id, e)
                self._sleep(MINT_BACKOFF[attempt])
        try:
            self.runner.add_secrets(job.id, {"github": token.token})
        except Exception as e:
            # Don't pass on the cluster's error text: it may quote the request.
            log.error("adding the GitHub token to job %s's Secret failed (%s)", job.id,
                      type(e).__name__)
            if is_transient(e):
                # Retried like a failed launch (which also removes the token, in case
                # the patch went through).
                raise _RetryLaunch(
                    _reason(e), f"adding the GitHub token to the Secret failed ({_reason(e)})"
                ) from None
            raise RuntimeError("couldn't add the GitHub token to the job's Secret") from None
        log.info(
            "minted a GitHub App token for job %s (%s, profile %s)", job.id, job.repo, profile.name
        )
        return dataclasses.replace(
            spec, opens_prs=profile.opens_prs, github_token_expires=token.expires
        )

    def _check(self, s: Session, job: Job) -> None:
        st = self.runner.status(job.id)
        if st.state == RunState.RUNNING:
            self._snapshot_log(job.id)
            return
        # The job may have reported while we were asking Kubernetes.
        s.refresh(job)
        if st.state != RunState.MISSING:
            self._archive_log(job.id)
        if st.state == RunState.SUCCEEDED and job.reported_status == SUCCEEDED:
            final, message = SUCCEEDED, job.message
        elif st.state == RunState.SUCCEEDED:
            final = FAILED
            message = job.message or "the job exited without reporting success"
        else:
            # If the job reported, its message says why; if it was killed (e.g.
            # timeout, out of memory), Kubernetes does.
            final = FAILED
            message = job.message if job.reported_status else st.message
        self._finish(s, job, final, message)
        self._last_snapshot.pop(job.id, None)
        self._cleanup(job.id)

    # -- logs ----------------------------------------------------------------

    def _snapshot_log(self, job_id: str) -> None:
        now = time.monotonic()
        last = self._last_snapshot.get(job_id)
        if last is not None and now - last < self.log_snapshot_seconds:
            return
        self._last_snapshot[job_id] = now
        self._archive_log(job_id)

    def _archive_log(self, job_id: str) -> None:
        """Copy the pod's log to storage. Best effort: a failure is logged.

        The kubelet rotates a container's log at about 10 MB, and the log API returns
        only the current file. A pod log of the same file only grows, so a read that
        doesn't start with what's archived, or is shorter than it, means a rotation:
        the archived copy is kept as an earlier segment and a new one starts. An
        empty read never replaces anything."""
        try:
            data = b"".join(self.runner.logs(job_id, follow=False))
        except LogsUnavailable:
            log.info("no pod log for job %s yet (or any more)", job_id)
            return
        except Exception:
            log.warning("reading the log of job %s failed", job_id, exc_info=True)
            return
        key = log_key(job_id)
        try:
            size = self.storage.size(key)
            if size:
                if not data:
                    return
                head = next(iter(self.storage.get(key)), b"")[:CHUNK]
                if len(data) < size or not data.startswith(head):
                    n = len(self.storage.list(f"jobs/{job_id}/log/")) + 1
                    log.info("the log of job %s was rotated; keeping segment %d", job_id, n)
                    self.storage.put(log_segment_key(job_id, n), self.storage.get(key))
            self.storage.put(key, [data])
        except Exception:
            log.warning("storing the log of job %s failed", job_id, exc_info=True)

    # -- cleanup -------------------------------------------------------------

    def _cleanup(self, job_id: str) -> None:
        try:
            self.runner.cleanup(job_id)
        except Exception:
            # The Secret reaper retries later.
            log.exception("cleaning up job %s failed", job_id)

    def _reap_secrets(self, s: Session) -> None:
        """Delete Secrets left behind by a failed create, launch or cleanup."""
        cutoff = datetime.now(UTC) - SECRET_MIN_AGE
        for job_id, created in self.runner.job_secrets().items():
            if created is not None and created > cutoff:
                continue
            job = s.get(Job, job_id)
            if job is None or job.status in (SUCCEEDED, FAILED):
                log.info("deleting leftover Secret for job %s", job_id)
                self._cleanup(job_id)

    def _finish(self, s: Session, job: Job, status: str, message: str | None) -> None:
        job.status = status
        job.message = message
        job.finished_at = datetime.now(UTC)
        s.commit()
        log.info("job %s %s: %s", job.id, status, message)
