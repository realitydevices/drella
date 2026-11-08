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
    job_image: str = field(
        default_factory=lambda: os.environ.get(
            "DRELLA_JOB_IMAGE", "ghcr.io/realitydevices/drella-job:latest"
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
