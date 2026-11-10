"""Drella's GitHub App (issue #50): signing, minting against a fake GitHub API, and
how repo jobs get their token at launch and later."""

import base64
import dataclasses
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from drella.github import PERMISSIONS, GitHubApp, GitHubAppError, app_jwt, load_key
from drella.runner import JOB_TOKEN_KEY, RunState, RunStatus

APP_ID = "123456"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,  # what GitHub hands out
    serialization.NoEncryption(),
)
JOB = {"prompt": "add a CHANGELOG.md", "repo": "octo/widgets", "model": "local"}
LOCAL = {"interface": "openai-compatible", "base_url": "http://ollama:11434/v1", "model_id": "m"}


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_jwt(jwt: str, public_key) -> dict:
    """Check the JWT's RS256 signature with the App's public key; return its claims."""
    header, payload, sig = jwt.split(".")
    public_key.verify(
        _unb64(sig), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    assert json.loads(_unb64(header)) == {"alg": "RS256", "typ": "JWT"}
    return json.loads(_unb64(payload))


class FakeGitHub:
    """GitHub's App endpoints, enough to mint a token. Checks every request's JWT."""

    def __init__(self):
        self.installations = {"octo/widgets": 42}  # repo -> installation id
        self.requests: list[tuple[str, str, dict | None]] = []
        self.token_status = 201
        self.minted = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _auth_ok(self):
                auth = self.headers.get("Authorization", "")
                try:
                    claims = verify_jwt(auth.removeprefix("Bearer "), KEY.public_key())
                except (InvalidSignature, ValueError):
                    return False
                now = time.time()
                return claims["iss"] == APP_ID and claims["iat"] <= now < claims["exp"]

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                if not self._auth_ok():
                    return self._reply(401, {"message": "A JSON web token could not be decoded"})
                prefix, suffix = "/repos/", "/installation"
                if self.path.startswith(prefix) and self.path.endswith(suffix):
                    repo = self.path[len(prefix) : -len(suffix)]
                    if repo in fake.installations:
                        return self._reply(200, {"id": fake.installations[repo]})
                if self.path == "/repos/octo/moved/installation":
                    self.send_response(301)
                    self.send_header("Location", "http://elsewhere.invalid/x")
                    self.end_headers()
                    return
                self._reply(404, {"message": "Not Found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(("POST", self.path, body))
                if not self._auth_ok():
                    return self._reply(401, {"message": "bad JWT"})
                if self.path not in {
                    f"/app/installations/{i}/access_tokens" for i in fake.installations.values()
                }:
                    return self._reply(404, {"message": "Not Found"})
                if fake.token_status != 201:
                    return self._reply(
                        fake.token_status, {"message": "The permissions requested are not granted"}
                    )
                fake.minted += 1
                self._reply(
                    201,
                    {"token": f"ghs_minted{fake.minted:04d}", "expires_at": "2026-11-10T13:00:00Z"},
                )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def github():
    fake = FakeGitHub()
    yield fake
    fake.close()


@pytest.fixture
def key_file(tmp_path):
    p = tmp_path / "private-key.pem"
    p.write_bytes(PEM)
    return p


@pytest.fixture
def gh_app(github, key_file):
    return GitHubApp(APP_ID, str(key_file), github.url, timeout=5)


# -- signing ------------------------------------------------------------------


def test_the_jwt_is_rs256_signed_by_the_app_key():
    now = 1_700_000_000
    claims = verify_jwt(app_jwt(APP_ID, KEY, now=now), KEY.public_key())
    assert claims == {"iat": now - 60, "exp": now + 540, "iss": APP_ID}


def test_a_jwt_does_not_verify_with_another_key():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidSignature):
        verify_jwt(app_jwt(APP_ID, other), KEY.public_key())


def test_a_bad_key_file_is_reported_without_its_contents(tmp_path):
    with pytest.raises(GitHubAppError, match="can't read"):
        load_key(str(tmp_path / "missing.pem"))
    bad = tmp_path / "bad.pem"
    bad.write_text("not a key at all")
    with pytest.raises(GitHubAppError, match="isn't a PEM RSA key") as e:
        load_key(str(bad))
    assert "not a key at all" not in str(e.value)


# -- minting --------------------------------------------------------------------


def test_mint_looks_up_the_installation_and_asks_for_a_one_repo_token(gh_app, github):
    token = gh_app.token("octo/widgets")
    assert token.token == "ghs_minted0001"
    assert token.expires_at == "2026-11-10T13:00:00Z"
    assert github.requests == [
        ("GET", "/repos/octo/widgets/installation", None),
        (
            "POST",
            "/app/installations/42/access_tokens",
            {
                "repositories": ["widgets"],
                "permissions": {
                    "contents": "write",
                    "pull_requests": "write",
                    "issues": "read",
                },
            },
        ),
    ]
    assert PERMISSIONS == github.requests[1][2]["permissions"]


def test_mint_fails_clearly_when_the_app_is_not_installed(gh_app, github):
    with pytest.raises(GitHubAppError, match="isn't installed on octo/other"):
        gh_app.token("octo/other")
    assert [m for m, *_ in github.requests] == ["GET"]


def test_mint_reports_githubs_refusal(gh_app, github):
    github.token_status = 422
    with pytest.raises(GitHubAppError, match="HTTP 422.*permissions requested are not granted"):
        gh_app.token("octo/widgets")


def test_mint_with_the_wrong_key_is_refused(github, tmp_path):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "other.pem"
    p.write_bytes(
        other.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(GitHubAppError, match="HTTP 401"):
        GitHubApp(APP_ID, str(p), github.url, timeout=5).token("octo/widgets")


def test_mint_never_follows_a_redirect(gh_app, github):
    with pytest.raises(GitHubAppError, match="HTTP 301"):
        gh_app.token("octo/moved")


def test_mint_reports_an_unreachable_github(key_file):
    app = GitHubApp(APP_ID, str(key_file), "http://127.0.0.1:1", timeout=2)
    with pytest.raises(GitHubAppError, match="couldn't reach GitHub"):
        app.token("octo/widgets")


def test_from_settings(settings, key_file, tmp_path):
    assert GitHubApp.from_settings(settings) is None  # no App ID: not configured
    with_app = dataclasses.replace(settings, github_app_id=APP_ID, github_app_key_file=str(key_file))
    assert GitHubApp.from_settings(with_app).app_id == APP_ID
    # Configured with a missing key: Drella refuses to start rather than fail every job.
    broken = dataclasses.replace(with_app, github_app_key_file=str(tmp_path / "nope.pem"))
    with pytest.raises(GitHubAppError):
        GitHubApp.from_settings(broken)


# -- jobs ---------------------------------------------------------------------


@pytest.fixture
def app_settings(settings, github, key_file):
    return dataclasses.replace(
        settings, github_app_id=APP_ID, github_app_key_file=str(key_file), github_api_url=github.url
    )


@pytest.fixture
def app_client(app_settings, runner, storage):
    from fastapi.testclient import TestClient

    from drella.app import create_app

    app = create_app(app_settings, runner=runner, storage=storage, start_queue=False)
    with TestClient(app) as c:
        c.queue = app.state.deps.queue
        yield c


@pytest.fixture
def app_auth(app_settings, app_client):
    from drella.cli import create_token

    auth = {"Authorization": f"Bearer {create_token(app_settings, 'alice')}"}
    assert app_client.put("/models/local", json=LOCAL, headers=auth).status_code == 200
    return auth


def job_auth(runner, job_id):
    return {"Authorization": f"Bearer {runner.secrets[job_id][JOB_TOKEN_KEY]}"}


def test_a_repo_job_without_a_credential_gets_an_app_token_at_launch(
    app_client, app_auth, runner, github
):
    r = app_client.post("/jobs", json=JOB, headers=app_auth)
    assert r.status_code == 201, r.text
    jid = r.json()["id"]
    # Nothing is minted while the job is queued: it could wait longer than an hour.
    assert "github" not in runner.secrets[jid]
    assert github.minted == 0
    app_client.queue.tick()
    assert runner.secrets[jid]["github"] == "ghs_minted0001"
    (spec,) = runner.launched
    assert spec.github_app is True
    assert app_client.get(f"/jobs/{jid}", headers=app_auth).json()["status"] == "running"


def test_the_minted_token_is_never_stored_in_the_database(
    app_client, app_auth, runner, app_settings
):
    from sqlalchemy import text

    from drella.db import make_engine

    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    engine = make_engine(app_settings.database_url)
    with engine.connect() as conn:
        for table in ("jobs", "credentials", "tokens", "models"):
            for row in conn.execute(text(f"SELECT * FROM {table}")):
                assert "ghs_minted" not in repr(tuple(row))
    engine.dispose()
    assert runner.secrets[jid]["github"].startswith("ghs_minted")


def test_a_stored_github_credential_wins_over_the_app(app_client, app_auth, runner, github):
    app_client.put("/credentials/github", json={"value": "github_pat_mine"}, headers=app_auth)
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    assert runner.secrets[jid]["github"] == "github_pat_mine"
    (spec,) = runner.launched
    assert spec.github_app is False
    assert github.requests == []
    # And no refresh for it.
    ja = job_auth(runner, jid)
    r = app_client.post(f"/jobs/{jid}/github-token", headers=ja)
    assert r.status_code == 409 and "caller's github credential" in r.json()["detail"]


def test_a_job_without_a_repo_gets_no_github_token(app_client, app_auth, runner, github):
    no_repo = {k: v for k, v in JOB.items() if k != "repo"}
    jid = app_client.post("/jobs", json=no_repo, headers=app_auth).json()["id"]
    app_client.queue.tick()
    assert set(runner.secrets[jid]) == {JOB_TOKEN_KEY}
    assert github.requests == []
    r = app_client.post(f"/jobs/{jid}/github-token", headers=job_auth(runner, jid))
    assert r.status_code == 409


def test_without_an_app_or_a_credential_a_repo_job_is_refused(client, auth):
    assert client.put("/models/local", json=LOCAL, headers=auth).status_code == 200
    r = client.post("/jobs", json=JOB, headers=auth)
    assert r.status_code == 422
    assert "'github' credential" in r.json()["detail"] and "GitHub App" in r.json()["detail"]


def test_a_repo_the_app_is_not_installed_on_fails_at_launch(app_client, app_auth, runner):
    jid = app_client.post("/jobs", json=JOB | {"repo": "octo/other"}, headers=app_auth).json()["id"]
    app_client.queue.tick()
    got = app_client.get(f"/jobs/{jid}", headers=app_auth).json()
    assert got["status"] == "failed"
    assert "isn't installed on octo/other" in got["message"]
    assert runner.launched == []
    assert runner.cleaned == [jid]


def test_a_mint_failure_fails_the_job_and_the_queue_moves_on(app_client, app_auth, runner, github):
    github.token_status = 500
    first = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    got = app_client.get(f"/jobs/{first}", headers=app_auth).json()
    assert got["status"] == "failed" and "HTTP 500" in got["message"]
    github.token_status = 201
    second = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    assert app_client.get(f"/jobs/{second}", headers=app_auth).json()["status"] == "running"


# -- the refresh endpoint -------------------------------------------------------


def test_a_running_app_job_can_refresh_its_token(app_client, app_auth, runner, github):
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    ja = job_auth(runner, jid)
    # Queued: the pod doesn't exist yet, as for the other job routes.
    assert app_client.post(f"/jobs/{jid}/github-token", headers=ja).status_code == 409
    app_client.queue.tick()
    r = app_client.post(f"/jobs/{jid}/github-token", headers=ja)
    assert r.status_code == 200, r.text
    assert r.json() == {"token": "ghs_minted0002", "expires_at": "2026-11-10T13:00:00Z"}
    # For the job's own repo only, with the same permissions.
    assert github.requests[-2][1] == "/repos/octo/widgets/installation"
    assert github.requests[-1][2]["repositories"] == ["widgets"]


def test_refresh_needs_the_jobs_own_token(app_client, app_auth, runner, github):
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    other = app_client.post("/jobs", json=JOB | {"repo": "octo/gadgets"}, headers=app_auth)
    app_client.queue.tick()
    url = f"/jobs/{jid}/github-token"
    # The caller's token isn't a job token; another job's token isn't this job's.
    assert app_client.post(url, headers=app_auth).status_code == 401
    assert app_client.post(url, headers=job_auth(runner, other.json()["id"])).status_code == 401
    assert app_client.post(url).status_code == 401
    assert github.minted == 1  # only the launch


def test_refresh_stops_once_the_job_has_finished(app_client, app_auth, runner):
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    runner.states[jid] = RunStatus(RunState.SUCCEEDED)
    app_client.queue.tick()
    r = app_client.post(f"/jobs/{jid}/github-token", headers=job_auth(runner, jid))
    assert r.status_code == 409


def test_a_failed_refresh_is_a_502_without_secrets(app_client, app_auth, runner, github):
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    github.token_status = 500
    r = app_client.post(f"/jobs/{jid}/github-token", headers=job_auth(runner, jid))
    assert r.status_code == 502
    assert "HTTP 500" in r.json()["detail"] and "ghs_" not in r.text
