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

import logging
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from drella.models import FAILED, QUEUED, RUNNING, SUCCEEDED, Job
from drella.runner import JobSpec, LogsUnavailable, Runner, RunState
from drella.storage import Storage

log = logging.getLogger(__name__)

# A Secret younger than this is never reaped: its job may not be committed yet.
SECRET_MIN_AGE = timedelta(minutes=10)
REAP_EVERY = 60  # seconds


def log_key(job_id: str) -> str:
    return f"jobs/{job_id}/log.txt"


def output_prefix(job_id: str) -> str:
    return f"jobs/{job_id}/outputs/"


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
    ):
        self.sessions = sessions
        self.runner = runner
        self.storage = storage
        self.concurrency = concurrency
        self.interval = interval
        self.log_snapshot_seconds = log_snapshot_seconds
        self.stall_seconds = stall_seconds
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
                job = s.scalars(
                    select(Job).where(Job.status == QUEUED).order_by(Job.created_at).limit(1)
                ).first()
                if job is None:
                    break
                self._launch(s, job)
                running += 1
            if self._last_reap is None or time.monotonic() - self._last_reap >= REAP_EVERY:
                self._last_reap = time.monotonic()
                try:
                    self._reap_secrets(s)
                except Exception:
                    log.exception("reaping job Secrets failed")
        self._last_tick = time.monotonic()

    def _launch(self, s: Session, job: Job) -> None:
        spec = JobSpec(job.id, job.repo, job.model, job.endpoint, job.prompt)
        try:
            self.runner.launch(spec)
        except Exception as e:
            log.exception("launching job %s failed", job.id)
            self._finish(s, job, FAILED, f"launch failed: {e}")
            self._cleanup(job.id)
            return
        job.status = RUNNING
        job.started_at = datetime.now(UTC)
        s.commit()
        log.info("job %s launched", job.id)

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

        Never replaces a snapshot with a shorter copy: the pod's log can come back
        empty or, once the kubelet has rotated it (at about 10 MB), shorter."""
        try:
            data = b"".join(self.runner.logs(job_id, follow=False))
        except LogsUnavailable:
            log.info("no pod log for job %s yet (or any more)", job_id)
            return
        except Exception:
            log.warning("reading the log of job %s failed", job_id, exc_info=True)
            return
        if len(data) < (self.storage.size(log_key(job_id)) or 0):
            log.info("keeping the longer archived log of job %s", job_id)
            return
        try:
            self.storage.put(log_key(job_id), [data])
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
