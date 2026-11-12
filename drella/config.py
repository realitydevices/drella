"""Settings, read from the environment."""

import os
import re
from dataclasses import dataclass, field

from sqlalchemy.engine import URL


_PART_SETTINGS = ("PORT", "NAME", "USER", "PASSWORD", "PASSWORD_FILE")


def _database_url() -> str:
    """The database URL: DRELLA_DATABASE_URL, or built from the separate
    DRELLA_DATABASE_* settings when DRELLA_DATABASE_HOST is set (which quotes the
    password correctly, unlike a password pasted into a URL)."""
    env = os.environ
    host = env.get("DRELLA_DATABASE_HOST", "").strip()
    if not host:
        # Any of the other separate settings without a host is a mistake, not a
        # request for the SQLite default.
        given = [f"DRELLA_DATABASE_{n}" for n in _PART_SETTINGS if f"DRELLA_DATABASE_{n}" in env]
        if given:
            raise ValueError(
                f"{', '.join(given)} set but DRELLA_DATABASE_HOST is unset or empty; "
                "set DRELLA_DATABASE_HOST too"
            )
        return env.get("DRELLA_DATABASE_URL", "sqlite:///drella.db")
    if "DRELLA_DATABASE_URL" in env:
        raise ValueError(
            "Both DRELLA_DATABASE_URL and DRELLA_DATABASE_HOST are set; use one or the other"
        )
    if host.startswith("/"):
        raise ValueError(
            "DRELLA_DATABASE_HOST must be a host name or address, not a Unix socket path; "
            "for a socket use DRELLA_DATABASE_URL"
        )
    if "DRELLA_DATABASE_PASSWORD" in env and "DRELLA_DATABASE_PASSWORD_FILE" in env:
        raise ValueError(
            "Both DRELLA_DATABASE_PASSWORD and DRELLA_DATABASE_PASSWORD_FILE are set; "
            "use one or the other"
        )
    for name in ("NAME", "USER"):
        if not env.get(f"DRELLA_DATABASE_{name}"):
            raise ValueError(f"DRELLA_DATABASE_{name} is required when DRELLA_DATABASE_HOST is set")
    # Both sources are normalised the same way: only a trailing line ending (as
    # left by `echo` or an editor) is dropped; any other spaces are kept.
    password = env.get("DRELLA_DATABASE_PASSWORD")
    if password is not None:
        password = password.rstrip("\r\n")
    if "DRELLA_DATABASE_PASSWORD_FILE" in env:
        path = env["DRELLA_DATABASE_PASSWORD_FILE"]
        try:
            with open(path) as f:
                password = f.read().rstrip("\r\n")
        except OSError as e:
            raise ValueError(
                f"DRELLA_DATABASE_PASSWORD_FILE: can't read {path!r}: {e.strerror or e}"
            ) from e
        if not password.strip():
            raise ValueError(f"DRELLA_DATABASE_PASSWORD_FILE: {path!r} is empty")
    try:
        port = int(env.get("DRELLA_DATABASE_PORT") or 5432)
    except ValueError:
        raise ValueError("DRELLA_DATABASE_PORT must be an integer") from None
    if not 1 <= port <= 65535:
        raise ValueError("DRELLA_DATABASE_PORT must be between 1 and 65535")
    url = URL.create(
        "postgresql+psycopg",
        username=env["DRELLA_DATABASE_USER"],
        password=password,
        host=host,
        port=port,
        database=env["DRELLA_DATABASE_NAME"],
    )
    return url.render_as_string(hide_password=False)


# DRELLA_GITHUB_APP_<NAME>_ID and DRELLA_GITHUB_APP_<NAME>_KEY_FILE configure a GitHub App
# named <name> in lower case (issue #73). DRELLA_GITHUB_APP_ID is not one of these: it's
# the App named "builder" (drella/github.py).
_APP_ID_VAR = re.compile(r"^DRELLA_GITHUB_APP_([A-Z][A-Z0-9_]*)_ID$")
_APP_KEY_VAR = re.compile(r"^DRELLA_GITHUB_APP_([A-Z][A-Z0-9_]*)_KEY_FILE$")
_APP_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass(frozen=True)
class GitHubAppConfig:
    """A GitHub App Drella mints tokens with: its name (what a profile in
    drella/github.py names), its App ID and where its private key file is."""

    name: str
    app_id: str
    key_file: str


def _github_apps_from_env() -> tuple[GitHubAppConfig, ...]:
    """The Apps configured by DRELLA_GITHUB_APP_<NAME>_ID, each with its key from
    DRELLA_GITHUB_APP_<NAME>_KEY_FILE or, by default, the Secret mounted at
    /etc/drella/github-app-<name>/. An ID that's empty (a Secret without that key, say)
    means the App isn't configured. A key file without an ID is a mistake, and refused."""
    ids = {}
    keys = {}
    for var, value in os.environ.items():
        if m := _APP_ID_VAR.match(var):
            ids[m.group(1)] = value.strip()
        elif m := _APP_KEY_VAR.match(var):
            keys[m.group(1)] = value.strip()
    for name in sorted(set(keys) - set(ids)):
        raise ValueError(
            f"DRELLA_GITHUB_APP_{name}_KEY_FILE is set, but not DRELLA_GITHUB_APP_{name}_ID"
        )
    apps = []
    for var in sorted(ids):
        if not ids[var]:
            continue
        name = var.lower()
        if not _APP_NAME.match(name):
            raise ValueError(f"DRELLA_GITHUB_APP_{var}_ID: '{name}' isn't a usable GitHub App name")
        key_file = keys.get(var) or f"/etc/drella/github-app-{name}/private-key.pem"
        apps.append(GitHubAppConfig(name, ids[var], key_file))
    return tuple(apps)


@dataclass(frozen=True)
class Settings:
    # Not in repr: it contains the database password.
    database_url: str = field(default_factory=_database_url, repr=False)
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
    # More GitHub Apps, besides the one above (e.g. superstar, for profiles that name
    # it). The Apps together: drella/github.py's app_configs.
    github_apps: tuple[GitHubAppConfig, ...] = field(default_factory=_github_apps_from_env)
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
