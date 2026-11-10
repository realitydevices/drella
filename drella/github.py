"""GitHub App installation tokens (issue #50).

Drella holds a GitHub App's private key and mints, for each repo job, an installation
token limited to that job's one repository. The key is read from a file (mounted from
a Kubernetes Secret); neither it nor any token it mints is stored in the database.

Minting a token:
1. sign a short-lived JWT (RS256) with the App's key;
2. look up the App's installation on the repo: GET /repos/{owner}/{repo}/installation;
3. ask for a token for that repo only: POST /app/installations/{id}/access_tokens.

Installation tokens last an hour. What a token may do depends on the GitHub profile of
the Drella token that created the job (PROFILES), never on anything the caller sends.
"""

import base64
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from drella.config import Settings

log = logging.getLogger(__name__)

API_VERSION = "2022-11-28"
# GitHub's installation tokens last an hour.
TOKEN_LIFETIME = 3600


@dataclass(frozen=True)
class Profile:
    """What the GitHub tokens minted for a Drella token's jobs may do. Whatever the
    profile, a token is for the job's one repository only."""

    name: str
    permissions: dict[str, str]
    # The job's standing instructions and success test: push a branch and open a PR,
    # or (read-only) write results to /out.
    opens_prs: bool
    description: str


# The fixed set of profiles. A Drella token's profile is set by an admin with the CLI
# (`drella token create --github-profile`, `drella token set-profile`), never by the
# caller and never per job. To add one (e.g. a reviewer that comments on PRs, with
# pull_requests: write), add it here. pull_requests: write also lets a token approve
# PRs; only branch protection stops that approval counting (deploy/README.md).
PROFILES = {
    p.name: p
    for p in (
        Profile(
            "builder",
            {"contents": "write", "pull_requests": "write", "issues": "read"},
            opens_prs=True,
            description="push branches and open PRs; read issues",
        ),
        Profile(
            "reader",
            {"contents": "read", "pull_requests": "read", "issues": "read"},
            opens_prs=False,
            description="read the repo, its PRs and issues; results go to /out",
        ),
    )
}
# A new Drella token gets the least access unless the admin asks for more.
DEFAULT_PROFILE = "reader"
# GitHub owner and repo names. "." and ".." are never names, and must not reach a URL
# path.
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# A token is one line of plain characters: it ends up in git's credential helper
# output and in an environment variable.
_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{8,}$")


class GitHubAppError(Exception):
    """Minting failed. The message is safe to show the caller (no secrets in it)."""


class _NotFound(GitHubAppError):
    pass


@dataclass(frozen=True)
class InstallationToken:
    token: str
    # When it expires (epoch seconds): GitHub's expires_at, but never later than an
    # hour after it was asked for, whatever GitHub's clock says.
    expires: float

    def __repr__(self) -> str:  # never print the token
        return f"InstallationToken(expires={self.expires})"


def check_repo(repo: str) -> None:
    if not _REPO.match(repo) or any(part in (".", "..") for part in repo.split("/")):
        raise GitHubAppError("the job's repo isn't a GitHub repo (owner/name)")


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
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            raise _http_error(e, what) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            raise GitHubAppError(f"couldn't reach GitHub to {what}: {reason}") from None
        if not isinstance(data, dict):
            raise GitHubAppError(f"GitHub's answer to {what} wasn't a JSON object")
        return data

    def installation_id(self, repo: str) -> int:
        try:
            data = self._call("GET", f"/repos/{repo}/installation", "find its installation")
        except _NotFound:
            raise GitHubAppError(
                f"the GitHub App isn't installed on {repo} (install it on the repo, or store"
                " a 'github' credential)"
            ) from None
        installation = data.get("id")
        if not isinstance(installation, int) or isinstance(installation, bool):
            raise GitHubAppError(f"GitHub returned no installation for {repo}")
        return installation

    def token(self, repo: str, profile: Profile) -> InstallationToken:
        """An installation token for `repo` (owner/name) only, with the profile's
        permissions and nothing else."""
        check_repo(repo)
        installation = self.installation_id(repo)
        asked = time.time()
        try:
            data = self._call(
                "POST",
                f"/app/installations/{installation}/access_tokens",
                f"mint a token for {repo}",
                {
                    "repositories": [repo.split("/", 1)[1]],
                    "permissions": dict(profile.permissions),
                },
            )
        except _NotFound:
            raise GitHubAppError(f"the GitHub App's installation on {repo} is gone") from None
        token = data.get("token")
        if not isinstance(token, str) or not _TOKEN.match(token):
            raise GitHubAppError(f"GitHub returned no usable token for {repo}")
        # GitHub refuses (422) a request for more than the App has, and grants exactly
        # what was asked for. Checked anyway, so a token can never be wider than its
        # profile.
        granted = data.get("permissions")
        if isinstance(granted, dict):
            wider = sorted(
                k
                for k, v in granted.items()
                if k != "metadata" and _level(v) > _level(profile.permissions.get(k))
            )
            if wider:
                raise GitHubAppError(
                    f"GitHub granted more than the '{profile.name}' profile asked for"
                    f" ({', '.join(wider)}); not using the token"
                )
        return InstallationToken(token, _expiry(data.get("expires_at"), asked))


def _level(access: object) -> int:
    """How much a permission level allows. Anything unknown counts as the most."""
    if access is None:
        return 0
    return {"read": 1, "write": 2}.get(access, 3) if isinstance(access, str) else 3


def _expiry(expires_at: object, asked: float) -> float:
    latest = asked + TOKEN_LIFETIME
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return latest
    return min(parsed, latest)


def _http_error(e: urllib.error.HTTPError, what: str) -> GitHubAppError:
    if e.code == 404:
        return _NotFound(f"GitHub answered 404 to {what}")
    try:
        message = str(json.loads(e.read()).get("message", ""))[:200]
    except Exception:
        message = ""
    return GitHubAppError(f"GitHub answered HTTP {e.code} to {what}" + (f": {message}" if message else ""))
