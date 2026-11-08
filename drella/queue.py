"""The queue loop [22].

v0 has one queue group with concurrency 1. Each tick:
1. finishes running jobs whose Kubernetes Job has ended: archives the log to
   storage [21], settles the final status, and deletes the Job (its Secret goes
   with it);
2. launches the oldest queued jobs while there's room.
"""

import logging
import threading
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from drella.models import FAILED, QUEUED, RUNNING, SUCCEEDED, Job
from drella.runner import JobSpec, LogsUnavailable, Runner, RunState
from drella.storage import Storage

log = logging.getLogger(__name__)


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
    ):
        self.sessions = sessions
        self.runner = runner
        self.storage = storage
        self.concurrency = concurrency
        self.interval = interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # -- loop ----------------------------------------------------------------

    def start(self) -> None:
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
                self._check(s, job)
            running = s.scalar(select(func.count()).select_from(Job).where(Job.status == RUNNING))
            while running < self.concurrency:
                job = s.scalars(
                    select(Job).where(Job.status == QUEUED).order_by(Job.created_at).limit(1)
                ).first()
                if job is None:
                    break
                self._launch(s, job)
                running += 1

    def _launch(self, s: Session, job: Job) -> None:
        spec = JobSpec(job.id, job.repo, job.model, job.endpoint, job.prompt)
        try:
            self.runner.launch(spec)
        except Exception as e:
            log.exception("launching job %s failed", job.id)
            self._finish(s, job, FAILED, f"launch failed: {e}")
            self.runner.cleanup(job.id)
            return
        job.status = RUNNING
        job.started_at = datetime.now(UTC)
        s.commit()
        log.info("job %s launched", job.id)

    def _check(self, s: Session, job: Job) -> None:
        st = self.runner.status(job.id)
        if st.state == RunState.RUNNING:
            return
        if st.state != RunState.MISSING:
            self._archive_log(job.id)
        if st.state == RunState.SUCCEEDED and job.reported_status == SUCCEEDED:
            final, message = SUCCEEDED, job.message
        elif st.state == RunState.SUCCEEDED:
            final = FAILED
            message = job.message or "the job exited without reporting success"
        else:
            final = FAILED
            message = "; ".join(filter(None, [st.message, job.message]))
        self._finish(s, job, final, message)
        self.runner.cleanup(job.id)

    def _archive_log(self, job_id: str) -> None:
        try:
            self.storage.put(log_key(job_id), self.runner.logs(job_id, follow=False))
        except LogsUnavailable:
            log.warning("no log to archive for job %s", job_id)

    def _finish(self, s: Session, job: Job, status: str, message: str | None) -> None:
        job.status = status
        job.message = message
        job.finished_at = datetime.now(UTC)
        s.commit()
        log.info("job %s %s: %s", job.id, status, message)
