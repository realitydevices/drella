"""GitHub App installation tokens (issue #50).

Drella holds a GitHub App's private key and mints, for each repo job, an installation
token limited to that job's one repository. The key is read from a file (mounted from
a Kubernetes Secret); neither it nor any token it mints is stored in the database.

Minting a token:
1. sign a short-lived JWT (RS256) with the App's key;
2. look up the App's installation on the repo: GET /repos/{owner}/{repo}/installation;
3. ask for a token for that repo only: POST /app/installations/{id}/access_tokens.

Installation tokens last an hour.
"""

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from drella.config import Settings

log = logging.getLogger(__name__)

# What a job's token may do: push its branch, open a PR, read issues. Nothing else,
# and only on the job's own repo.
PERMISSIONS = {"contents": "write", "pull_requests": "write", "issues": "read"}
API_VERSION = "2022-11-28"


class GitHubAppError(Exception):
    """Minting failed. The message is safe to show the caller (no secrets in it)."""


class _NotFound(GitHubAppError):
    pass


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: str | None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, key: rsa.RSAPrivateKey, now: float | None = None) -> str:
    """A JWT that authenticates as the App, valid for 9 minutes (GitHub allows 10). It
    starts a minute in the past, to allow for clock drift."""
    now = int(time.time() if now is None else now)
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": now - 60, "exp": now + 540, "iss": app_id}
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, payload)
    )
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


def load_key(path: str) -> rsa.RSAPrivateKey:
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise GitHubAppError(f"can't read the GitHub App's private key ({path}): {e.strerror}")
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError):
        raise GitHubAppError(f"the GitHub App's private key ({path}) isn't a PEM RSA key")
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubAppError(f"the GitHub App's private key ({path}) isn't an RSA key")
    return key


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Never follow a redirect with the App's JWT: GitHub's API answers directly, and a
    # redirect (e.g. for a renamed repo) is treated as an error.
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class GitHubApp:
    def __init__(self, app_id: str, key_file: str, api_url: str, timeout: float = 20):
        self.app_id = app_id
        self.key_file = key_file
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_settings(cls, settings: Settings) -> "GitHubApp | None":
        """The App, if it's configured (DRELLA_GITHUB_APP_ID). Its key is checked now,
        so a bad key stops Drella at startup rather than failing every repo job."""
        if not settings.github_app_id:
            if Path(settings.github_app_key_file).exists():
                log.warning(
                    "a GitHub App key is mounted at %s but DRELLA_GITHUB_APP_ID isn't set;"
                    " Drella won't mint GitHub tokens",
                    settings.github_app_key_file,
                )
            return None
        app = cls(settings.github_app_id, settings.github_app_key_file, settings.github_api_url)
        load_key(app.key_file)
        log.info("minting GitHub tokens as GitHub App %s", app.app_id)
        return app

    def _call(self, method: str, path: str, what: str, body: dict | None = None) -> dict:
        # The key is read for every call, so a rotated Secret takes effect without a
        # restart.
        jwt = app_jwt(self.app_id, load_key(self.key_file))
        req = urllib.request.Request(
            self.api_url + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {jwt}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "drella",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            with _opener.open(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise _http_error(e, what) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            raise GitHubAppError(f"couldn't reach GitHub to {what}: {reason}") from None

    def installation_id(self, repo: str) -> int:
        try:
            data = self._call("GET", f"/repos/{repo}/installation", "find its installation")
        except _NotFound:
            raise GitHubAppError(
                f"the GitHub App isn't installed on {repo} (install it on the repo, or store"
                " a 'github' credential)"
            ) from None
        return int(data["id"])

    def token(self, repo: str) -> InstallationToken:
        """An installation token for `repo` (owner/name) only."""
        installation = self.installation_id(repo)
        try:
            data = self._call(
                "POST",
                f"/app/installations/{installation}/access_tokens",
                f"mint a token for {repo}",
                {"repositories": [repo.split("/", 1)[1]], "permissions": PERMISSIONS},
            )
        except _NotFound:
            raise GitHubAppError(f"the GitHub App's installation on {repo} is gone") from None
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise GitHubAppError(f"GitHub returned no token for {repo}")
        return InstallationToken(token, data.get("expires_at"))


def _http_error(e: urllib.error.HTTPError, what: str) -> GitHubAppError:
    if e.code == 404:
        return _NotFound(f"GitHub answered 404 to {what}")
    try:
        message = str(json.loads(e.read()).get("message", ""))[:200]
    except Exception:
        message = ""
    return GitHubAppError(f"GitHub answered HTTP {e.code} to {what}" + (f": {message}" if message else ""))
