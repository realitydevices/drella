"""Settings, read from the environment."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get("DRELLA_DATABASE_URL", "sqlite:///drella.db")
    )
    # Root directory for the local-filesystem storage backend.
    storage_path: str = field(
        default_factory=lambda: os.environ.get("DRELLA_STORAGE_PATH", "./data")
    )
    # Namespace that Jobs and Secrets are created in.
    namespace: str = field(default_factory=lambda: os.environ.get("DRELLA_NAMESPACE", "drella"))
    # The image's own version, baked in at build time (a release tag like v0.1.0,
    # or main / sha-<commit> for test builds).
    version: str = field(default_factory=lambda: os.environ.get("DRELLA_VERSION", "main"))
    # Defaults to the drella-job image with the same tag as this image, so the two
    # always match. Set DRELLA_JOB_IMAGE only to override that.
    job_image: str = field(
        default_factory=lambda: os.environ.get(
            "DRELLA_JOB_IMAGE",
            f"ghcr.io/realitydevices/drella-job:{os.environ.get('DRELLA_VERSION', 'main')}",
        )
    )
    # How job pods reach the API.
    api_url: str = field(
        default_factory=lambda: os.environ.get("DRELLA_API_URL", "http://drella.drella.svc:8000")
    )
    # The one queue group in v0 [22].
    queue_concurrency: int = field(
        default_factory=lambda: int(os.environ.get("DRELLA_QUEUE_CONCURRENCY", "1"))
    )
    queue_interval: float = field(
        default_factory=lambda: float(os.environ.get("DRELLA_QUEUE_INTERVAL", "5"))
    )
    # Default limits [23].
    job_timeout_seconds: int = 3600
    job_memory: str = "2Gi"
    job_disk: str = "10Gi"
    # The wrapper stops the agent this long before the hard limit, to leave time to
    # upload outputs and report (the soft limit).
    agent_grace_seconds: int = 300
    # How long a job pod gets to upload and report after being told to stop.
    job_termination_grace_seconds: int = 120
    # While a job runs, its log is copied to storage this often.
    log_snapshot_seconds: float = 30
    # Timeout for each (non-streaming) Kubernetes API call.
    k8s_timeout_seconds: float = 30
    # The health check fails if the queue hasn't ticked for this long.
    queue_stall_seconds: float = 300
