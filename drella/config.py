"""Settings, read from the environment."""

import os
from dataclasses import dataclass, field

from sqlalchemy.engine import URL


def _database_url() -> str:
    """The database URL: DRELLA_DATABASE_URL, or built from the separate
    DRELLA_DATABASE_* settings when DRELLA_DATABASE_HOST is set (which quotes the
    password correctly, unlike a password pasted into a URL)."""
    env = os.environ
    host = env.get("DRELLA_DATABASE_HOST", "").strip()
    if not host:
        return env.get("DRELLA_DATABASE_URL", "sqlite:///drella.db")
    if "DRELLA_DATABASE_URL" in env:
        raise ValueError(
            "Both DRELLA_DATABASE_URL and DRELLA_DATABASE_HOST are set; use one or the other"
        )
    if "DRELLA_DATABASE_PASSWORD" in env and "DRELLA_DATABASE_PASSWORD_FILE" in env:
        raise ValueError(
            "Both DRELLA_DATABASE_PASSWORD and DRELLA_DATABASE_PASSWORD_FILE are set; "
            "use one or the other"
        )
    for name in ("NAME", "USER"):
        if not env.get(f"DRELLA_DATABASE_{name}"):
            raise ValueError(f"DRELLA_DATABASE_{name} is required when DRELLA_DATABASE_HOST is set")
    password = env.get("DRELLA_DATABASE_PASSWORD")
    if "DRELLA_DATABASE_PASSWORD_FILE" in env:
        with open(env["DRELLA_DATABASE_PASSWORD_FILE"]) as f:
            password = f.read().strip()
    try:
        port = int(env.get("DRELLA_DATABASE_PORT") or 5432)
    except ValueError:
        raise ValueError("DRELLA_DATABASE_PORT must be an integer") from None
    url = URL.create(
        "postgresql+psycopg",
        username=env["DRELLA_DATABASE_USER"],
        password=password,
        host=host,
        port=port,
        database=env["DRELLA_DATABASE_NAME"],
    )
    return url.render_as_string(hide_password=False)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=_database_url)
    # Root directory for the local-filesystem storage backend.
    storage_path: str = field(
        default_factory=lambda: os.environ.get("DRELLA_STORAGE_PATH", "./data")
    )
    # Namespace that Jobs and Secrets are created in.
    namespace: str = field(default_factory=lambda: os.environ.get("DRELLA_NAMESPACE", "drella"))
    # Defaults to the drella-job image with the same tag as this image, so the two
    # always match. DRELLA_VERSION is baked into the image at build time: a release
    # tag like v0.1.0, sha-<commit> for builds from main, or "main" for a local build.
    # Set DRELLA_JOB_IMAGE only to override that.
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
    # The GitHub App Drella mints repo jobs' tokens with (drella/github.py). Both come
    # from a Kubernetes Secret (deploy/README.md); without an App ID, Drella doesn't
    # mint tokens and a repo job needs the caller's own `github` credential.
    github_app_id: str = field(
        default_factory=lambda: os.environ.get("DRELLA_GITHUB_APP_ID", "").strip()
    )
    github_app_key_file: str = field(
        default_factory=lambda: os.environ.get(
            "DRELLA_GITHUB_APP_KEY_FILE", "/etc/drella/github-app/private-key.pem"
        )
    )
    github_api_url: str = field(
        default_factory=lambda: os.environ.get("DRELLA_GITHUB_API_URL", "https://api.github.com")
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
