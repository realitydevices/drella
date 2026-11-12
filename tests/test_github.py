"""Drella's GitHub App (issue #50): signing, minting against a fake GitHub API, and
how repo jobs get their token at launch and later."""

import base64
import dataclasses
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from drella.config import GitHubAppConfig, Settings
from drella.github import (
    DEFAULT_PROFILE,
    PROFILES,
    GitHubApp,
    GitHubAppError,
    InstallationToken,
    app_configs,
    app_jwt,
    load_key,
)
from drella.models import Job
from drella.runner import JOB_TOKEN_KEY, RunState, RunStatus

BUILDER, READER, SUPERSTAR = PROFILES["builder"], PROFILES["reader"], PROFILES["superstar"]

APP_ID = "123456"  # the App named "builder" (DRELLA_GITHUB_APP_ID)
SUPERSTAR_APP_ID = "654321"  # the App named "superstar" (issue #73)
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
        # Statuses to answer the next mint requests with (one each), before token_status.
        self.token_statuses: list[int] = []
        # Overrides for the minted token's answer; a key set to None is left out.
        self.answer: dict = {}
        self.minted = 0
        # The App ID each authenticated request was signed for (its JWT's iss).
        self.issuers: list[str] = []
        # What GitHub says it granted; None: exactly what was asked for, as GitHub does.
        self.granted: dict | None = None
        self.expires_in = 3600
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
                if claims["iss"] not in (APP_ID, SUPERSTAR_APP_ID):
                    return False
                fake.issuers.append(claims["iss"])
                return claims["iat"] <= now < claims["exp"]

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
                status = fake.token_statuses.pop(0) if fake.token_statuses else fake.token_status
                if status != 201:
                    return self._reply(
                        status, {"message": "The permissions requested are not granted"}
                    )
                fake.minted += 1
                expires = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + fake.expires_in)
                )
                answer = {
                    "token": f"ghs_minted{fake.minted:04d}",
                    "expires_at": expires,
                    "permissions": fake.granted or {**body["permissions"], "metadata": "read"},
                    "repository_selection": "selected",
                    **fake.answer,
                }
                self._reply(201, {k: v for k, v in answer.items() if v is not None})

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
    with pytest.raises(GitHubAppError, match="private key can't be read") as e:
        load_key(str(tmp_path / "missing.pem"))
    assert "missing.pem" not in str(e.value) and "No such file" not in str(e.value)
    bad = tmp_path / "bad.pem"
    bad.write_text("not a key at all")
    with pytest.raises(GitHubAppError, match="isn't a PEM RSA key") as e:
        load_key(str(bad))
    assert "not a key at all" not in str(e.value)


# -- minting --------------------------------------------------------------------


# -- profiles -------------------------------------------------------------------


def test_the_profiles_and_their_permissions():
    # Pinned exactly: a change here changes what every job of a profile can do.
    assert {p.name: p.permissions for p in PROFILES.values()} == {
        "builder": {"contents": "write", "pull_requests": "write", "issues": "read"},
        "reader": {"contents": "read", "pull_requests": "read", "issues": "read"},
        "superstar": {
            "contents": "write",
            "pull_requests": "write",
            "issues": "write",
            "checks": "read",
            "statuses": "read",
        },
    }
    # Each profile names the App its tokens are minted from (issue #73).
    assert {p.name: p.app for p in PROFILES.values()} == {
        "builder": "builder",
        "reader": "builder",
        "superstar": "superstar",
    }
    assert BUILDER.opens_prs and not READER.opens_prs and not SUPERSTAR.opens_prs
    assert BUILDER.read_only is False and READER.read_only and SUPERSTAR.read_only is False
    assert DEFAULT_PROFILE == "reader"


def test_no_profile_can_touch_workflows_or_administer_the_repo():
    for p in PROFILES.values():
        assert set(p.permissions) <= {"contents", "pull_requests", "issues", "checks", "statuses"}
        assert set(p.permissions.values()) <= {"read", "write"}, p.name
        # Only a profile that can open PRs, or the supervisor (which acts on GitHub with gh
        # and reports in /out), can write anything.
        assert p.opens_prs or p.name == "superstar" or "write" not in p.permissions.values()


# -- minting --------------------------------------------------------------------


def test_mint_looks_up_the_installation_and_asks_for_a_one_repo_token(gh_app, github):
    before = time.time()
    token = gh_app.token("octo/widgets", BUILDER)
    assert token.token == "ghs_minted0001"
    assert before + 3590 < token.expires <= time.time() + 3600
    assert "ghs_" not in repr(token)
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


def test_a_reader_token_asks_for_read_access_only(gh_app, github):
    gh_app.token("octo/widgets", READER)
    assert github.requests[-1][2] == {
        "repositories": ["widgets"],
        "permissions": {"contents": "read", "pull_requests": "read", "issues": "read"},
    }


@pytest.mark.parametrize(
    "granted",
    [
        {"contents": "write", "pull_requests": "read", "issues": "read"},
        {"contents": "read", "pull_requests": "read", "issues": "read", "workflows": "write"},
        {"contents": "read", "pull_requests": "read", "issues": "read", "administration": "x"},
    ],
)
def test_a_token_wider_than_its_profile_is_never_used(gh_app, github, granted):
    github.granted = granted
    with pytest.raises(GitHubAppError, match="granted more than the 'reader' profile") as e:
        gh_app.token("octo/widgets", READER)
    assert "ghs_" not in str(e.value)


@pytest.mark.parametrize(
    "answer",
    [
        {"permissions": None},
        {"permissions": ["contents"]},
        {"repository_selection": "all"},
        {"repository_selection": None},
        {"repositories": [{"name": "widgets"}, {"name": "gadgets"}]},
        {"repositories": [{"name": "gadgets"}]},
        {"repositories": []},
        {"repositories": "widgets"},
    ],
)
def test_a_token_that_isnt_clearly_one_repo_with_known_permissions_is_never_used(
    gh_app, github, answer
):
    github.answer = answer
    with pytest.raises(GitHubAppError, match="not using") as e:
        gh_app.token("octo/widgets", READER)
    assert "ghs_" not in str(e.value)


def test_a_token_for_exactly_this_repo_is_used(gh_app, github):
    github.answer = {"repositories": [{"name": "widgets"}]}
    assert gh_app.token("octo/widgets", READER).token == "ghs_minted0001"


def test_the_expiry_is_never_later_than_an_hour_from_now(gh_app, github):
    github.expires_in = 7200  # GitHub's clock ahead, or a longer token
    assert gh_app.token("octo/widgets", BUILDER).expires <= time.time() + 3600
    github.expires_in = 1800
    assert gh_app.token("octo/widgets", BUILDER).expires <= time.time() + 1800


@pytest.mark.parametrize("repo", ["octo/..", "../widgets", "octo/.", "octo", "a/b/c"])
def test_mint_refuses_a_repo_that_isnt_owner_name(gh_app, github, repo):
    with pytest.raises(GitHubAppError, match="isn't a GitHub repo"):
        gh_app.token(repo, BUILDER)
    assert github.requests == []


def test_installation_token_repr_hides_the_token():
    assert "secret" not in repr(InstallationToken("ghs_secret123", 0.0))


def test_mint_fails_clearly_when_the_app_is_not_installed(gh_app, github):
    with pytest.raises(GitHubAppError, match="isn't installed on octo/other"):
        gh_app.token("octo/other", BUILDER)
    assert [m for m, *_ in github.requests] == ["GET"]


def test_mint_reports_githubs_refusal(gh_app, github):
    github.token_status = 422
    with pytest.raises(GitHubAppError, match="HTTP 422.*permissions requested are not granted"):
        gh_app.token("octo/widgets", BUILDER)


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
        GitHubApp(APP_ID, str(p), github.url, timeout=5).token("octo/widgets", BUILDER)


def test_mint_never_follows_a_redirect(gh_app, github):
    with pytest.raises(GitHubAppError, match="HTTP 301"):
        gh_app.token("octo/moved", BUILDER)


def test_mint_reports_an_unreachable_github(key_file):
    app = GitHubApp(APP_ID, str(key_file), "http://127.0.0.1:1", timeout=2)
    with pytest.raises(GitHubAppError, match="couldn't reach GitHub"):
        app.token("octo/widgets", BUILDER)


def test_from_settings(settings, key_file, tmp_path):
    assert GitHubApp.from_settings(settings) == {}  # no App ID: not configured
    with_app = dataclasses.replace(settings, github_app_id=APP_ID, github_app_key_file=str(key_file))
    apps = GitHubApp.from_settings(with_app)
    assert list(apps) == ["builder"] and apps["builder"].app_id == APP_ID
    # Configured with a missing key: Drella refuses to start rather than fail every job.
    broken = dataclasses.replace(with_app, github_app_key_file=str(tmp_path / "nope.pem"))
    with pytest.raises(GitHubAppError):
        GitHubApp.from_settings(broken)


def test_the_single_app_env_vars_are_the_app_named_builder(monkeypatch, key_file):
    monkeypatch.setenv("DRELLA_GITHUB_APP_ID", APP_ID)
    monkeypatch.setenv("DRELLA_GITHUB_APP_KEY_FILE", str(key_file))
    settings = Settings()
    assert app_configs(settings) == {
        "builder": GitHubAppConfig("builder", APP_ID, str(key_file))
    }
    assert GitHubApp.from_settings(settings)["builder"].key_file == str(key_file)


def test_each_app_is_configured_by_its_own_env_pair(monkeypatch, tmp_path):
    monkeypatch.setenv("DRELLA_GITHUB_APP_ID", APP_ID)
    monkeypatch.setenv("DRELLA_GITHUB_APP_SUPERSTAR_ID", SUPERSTAR_APP_ID)
    monkeypatch.setenv("DRELLA_GITHUB_APP_SUPERSTAR_KEY_FILE", str(tmp_path / "s.pem"))
    monkeypatch.setenv("DRELLA_GITHUB_APP_OTHER_ID", "7")  # key: the default path
    apps = app_configs(Settings())
    assert apps["superstar"] == GitHubAppConfig(
        "superstar", SUPERSTAR_APP_ID, str(tmp_path / "s.pem")
    )
    assert apps["other"] == GitHubAppConfig(
        "other", "7", "/etc/drella/github-app-other/private-key.pem"
    )
    assert apps["builder"].app_id == APP_ID and sorted(apps) == ["builder", "other", "superstar"]


def test_an_empty_app_id_is_not_configured_and_a_key_without_an_id_is_refused(monkeypatch):
    # A Secret without its app-id key gives an empty value: the App isn't configured.
    monkeypatch.setenv("DRELLA_GITHUB_APP_SUPERSTAR_ID", "")
    monkeypatch.setenv("DRELLA_GITHUB_APP_SUPERSTAR_KEY_FILE", "/k.pem")
    assert app_configs(Settings()) == {}
    # A key with no ID variable at all is a mistake, refused at startup.
    monkeypatch.delenv("DRELLA_GITHUB_APP_SUPERSTAR_ID")
    with pytest.raises(ValueError, match="KEY_FILE is set, but not DRELLA_GITHUB_APP_SUPERSTAR"):
        Settings()


def test_an_app_named_twice_is_refused(monkeypatch):
    monkeypatch.setenv("DRELLA_GITHUB_APP_ID", APP_ID)
    monkeypatch.setenv("DRELLA_GITHUB_APP_BUILDER_ID", "9")
    with pytest.raises(GitHubAppError, match="'builder' is configured twice"):
        app_configs(Settings())


def test_every_configured_app_key_is_checked_at_startup(settings, key_file, tmp_path):
    both = dataclasses.replace(
        settings,
        github_app_id=APP_ID,
        github_app_key_file=str(key_file),
        github_apps=(GitHubAppConfig("superstar", SUPERSTAR_APP_ID, str(tmp_path / "no.pem")),),
    )
    with pytest.raises(GitHubAppError, match="private key can't be read"):
        GitHubApp.from_settings(both)


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
        c.sleeps = []
        c.queue._sleep = c.sleeps.append  # no real waiting between mint attempts
        yield c


@pytest.fixture
def app_auth(app_settings, app_client):
    """A builder token: drella-bot's normal use."""
    from drella.cli import create_token

    auth = {"Authorization": f"Bearer {create_token(app_settings, 'alice', 'builder')}"}
    assert app_client.put("/models/local", json=LOCAL, headers=auth).status_code == 200
    return auth


@pytest.fixture
def reader_auth(app_settings, app_client):
    from drella.cli import create_token

    auth = {"Authorization": f"Bearer {create_token(app_settings, 'rita')}"}  # the default
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
    before = time.time()
    app_client.queue.tick()
    assert runner.secrets[jid]["github"] == "ghs_minted0001"
    assert github.requests[-1][2]["permissions"] == BUILDER.permissions
    (spec,) = runner.launched
    assert spec.opens_prs is True
    # The job's deadline is capped at the token's expiry (see test_runner.py).
    assert before + 3590 < spec.github_token_expires <= time.time() + 3600
    assert app_client.get(f"/jobs/{jid}", headers=app_auth).json()["status"] == "running"


def test_a_reader_tokens_job_gets_a_read_only_token_and_opens_no_pr(
    app_client, reader_auth, runner, github
):
    jid = app_client.post("/jobs", json=JOB, headers=reader_auth).json()["id"]
    app_client.queue.tick()
    assert github.requests[-1][2] == {
        "repositories": ["widgets"],
        "permissions": {"contents": "read", "pull_requests": "read", "issues": "read"},
    }
    (spec,) = runner.launched
    assert spec.opens_prs is False
    assert runner.secrets[jid]["github"] == "ghs_minted0001"


def test_a_caller_cannot_choose_the_jobs_github_access(app_client, reader_auth, github):
    # The request can't name permissions, nor a profile wider than its token's (a reader
    # here; narrowing is covered below).
    for extra in (
        {"github_profile": "builder"},
        {"github_profile": "superstar"},
        {"permissions": {"contents": "write"}},
        {"profile": "builder"},
    ):
        r = app_client.post("/jobs", json=JOB | extra, headers=reader_auth)
        assert r.status_code == 422, extra
    # And the API has no route that changes a token's profile: only the CLI does.
    paths = set(app_client.get("/openapi.json").json()["paths"])
    assert "/jobs" in paths
    assert not any("token" in p or "profile" in p for p in paths), paths
    assert github.requests == []


def test_the_profile_is_read_when_the_job_launches(app_client, app_auth, app_settings, github):
    """An admin who narrows a token's profile narrows its queued jobs too."""
    from drella.cli import list_tokens, set_profile

    app_client.post("/jobs", json=JOB, headers=app_auth)
    ((tid, name, profile, _role),) = list_tokens(app_settings)
    assert (name, profile) == ("alice", "builder")
    set_profile(app_settings, tid, "reader")
    app_client.queue.tick()
    assert github.requests[-1][2]["permissions"] == READER.permissions


def test_an_unknown_profile_fails_the_launch_without_minting(
    app_client, app_auth, app_settings, runner, github
):
    from sqlalchemy import text

    from drella.db import make_engine

    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    engine = make_engine(app_settings.database_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE tokens SET github_profile = 'admin'"))
    engine.dispose()
    app_client.queue.tick()
    got = app_client.get(f"/jobs/{jid}", headers=app_auth).json()
    assert got["status"] == "failed" and "no known GitHub profile" in got["message"]
    assert github.requests == [] and runner.launched == []


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


def test_a_secret_patch_failure_fails_the_job_without_quoting_the_cluster(
    app_client, app_auth, runner, github
):
    def broken(job_id, secrets):
        raise RuntimeError(f"422 Unprocessable: {secrets}")  # an error that quotes the request

    runner.add_secrets = broken
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    got = app_client.get(f"/jobs/{jid}", headers=app_auth).json()
    assert got["status"] == "failed"
    assert "couldn't add the GitHub token" in got["message"]
    assert "ghs_" not in got["message"]
    assert runner.cleaned == [jid]


def test_a_stored_github_credential_wins_over_the_app(app_client, app_auth, runner, github):
    app_client.put("/credentials/github", json={"value": "github_pat_mine"}, headers=app_auth)
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    assert runner.secrets[jid]["github"] == "github_pat_mine"
    (spec,) = runner.launched
    assert spec.github_token_expires is None and spec.opens_prs is True
    assert github.requests == []


def test_deleting_the_stored_credential_switches_to_the_app(app_client, app_auth, runner, github):
    app_client.put("/credentials/github", json={"value": "github_pat_stale"}, headers=app_auth)
    assert app_client.delete("/credentials/github", headers=app_auth).status_code == 204
    assert app_client.get("/credentials", headers=app_auth).json() == []
    assert app_client.delete("/credentials/github", headers=app_auth).status_code == 404
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    assert runner.secrets[jid]["github"] == "ghs_minted0001"


def test_a_job_without_a_repo_gets_no_github_token(app_client, app_auth, runner, github):
    no_repo = {k: v for k, v in JOB.items() if k != "repo"}
    jid = app_client.post("/jobs", json=no_repo, headers=app_auth).json()["id"]
    app_client.queue.tick()
    assert set(runner.secrets[jid]) == {JOB_TOKEN_KEY}
    assert github.requests == []


@pytest.mark.parametrize("repo", ["octo/..", "../widgets", "octo/.", "./widgets"])
def test_a_dot_repo_is_refused_at_creation(app_client, app_auth, github, repo):
    r = app_client.post("/jobs", json=JOB | {"repo": repo}, headers=app_auth)
    assert r.status_code == 422
    assert github.requests == []


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
    second = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    got = app_client.get(f"/jobs/{first}", headers=app_auth).json()
    assert got["status"] == "failed" and "HTTP 500" in got["message"]
    # A failed launch takes its slot for the rest of the tick; the next job is tried at
    # the next tick, not stuck behind it.
    app_client.queue.tick()
    assert app_client.get(f"/jobs/{second}", headers=app_auth).json()["status"] == "failed"
    github.token_status = 201
    third = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    assert app_client.get(f"/jobs/{third}", headers=app_auth).json()["status"] == "running"


def test_a_launch_failure_after_minting_cleans_up_and_leaves_no_token(
    app_client, app_auth, runner, github
):
    runner.fail_launch = True
    real_cleanup = runner.cleanup

    def cleanup(job_id):  # the real one deletes the Secret; the fake only records
        real_cleanup(job_id)
        runner.secrets.pop(job_id, None)

    runner.cleanup = cleanup
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    assert github.minted == 1  # minted (and added to the Secret) before the launch failed
    got = app_client.get(f"/jobs/{jid}", headers=app_auth).json()
    assert got["status"] == "failed" and "launch failed" in got["message"]
    assert runner.cleaned == [jid]
    assert "github" not in runner.secrets.get(jid, {})
    assert runner.launched == []


def test_a_transient_mint_failure_is_retried(app_client, app_auth, runner, github):
    github.token_statuses = [502]
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    assert app_client.get(f"/jobs/{jid}", headers=app_auth).json()["status"] == "running"
    assert app_client.sleeps == [0.5]
    assert runner.secrets[jid]["github"] == "ghs_minted0001"


def test_three_transient_mint_failures_fail_the_job(app_client, app_auth, runner, github):
    github.token_statuses = [502, 502, 502]
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    got = app_client.get(f"/jobs/{jid}", headers=app_auth).json()
    assert got["status"] == "failed" and "HTTP 502" in got["message"]
    assert app_client.sleeps == [0.5, 1.0]
    assert [m for m, *_ in github.requests].count("POST") == 3


def test_a_rate_limited_mint_is_retried(app_client, app_auth, github):
    github.token_statuses = [429]
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    assert app_client.get(f"/jobs/{jid}", headers=app_auth).json()["status"] == "running"


@pytest.mark.parametrize("status", [404, 422])
def test_a_client_error_is_not_retried(app_client, app_auth, github, status):
    github.token_status = status
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    assert app_client.get(f"/jobs/{jid}", headers=app_auth).json()["status"] == "failed"
    assert [m for m, *_ in github.requests].count("POST") == 1
    assert app_client.sleeps == []


def test_there_is_no_token_refresh_route(app_client, app_auth, runner):
    """The token minted at dispatch is the job's only one: a job (or a prompt-injected
    agent holding its job token) can't mint more."""
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    app_client.queue.tick()
    r = app_client.post(f"/jobs/{jid}/github-token", headers=job_auth(runner, jid))
    assert r.status_code in (404, 405)


def _api_error(status):
    from kubernetes.client.exceptions import ApiException

    return ApiException(status=status, reason="nope")


def test_a_transient_launch_error_keeps_the_callers_stored_github_credential(
    app_client, app_auth, runner, github
):
    app_client.put("/credentials/github", json={"value": "github_pat_mine"}, headers=app_auth)
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    runner.launch_errors = [_api_error(503)]
    app_client.queue.tick()
    assert app_client.get(f"/jobs/{jid}", headers=app_auth).json()["status"] == "queued"
    assert runner.reset == [jid]
    # It exists only in the Secret: removing it would lose it for good.
    assert runner.secrets[jid]["github"] == "github_pat_mine"
    _backoff_over(app_client.queue, jid)
    app_client.queue.tick()
    assert [sp.job_id for sp in runner.launched] == [jid]
    assert runner.secrets[jid]["github"] == "github_pat_mine"
    assert github.requests == []


def test_a_transient_launch_error_removes_a_minted_token_and_mints_again(
    app_client, app_auth, runner, github
):
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    runner.launch_errors = [_api_error(503)]
    app_client.queue.tick()
    assert github.minted == 1 and "github" not in runner.secrets[jid]
    _backoff_over(app_client.queue, jid)
    app_client.queue.tick()
    assert github.minted == 2 and runner.secrets[jid]["github"] == "ghs_minted0002"
    assert app_client.get(f"/jobs/{jid}", headers=app_auth).json()["status"] == "running"


@pytest.mark.parametrize("error", [_api_error(503), ConnectionError("down")])
def test_a_transient_secret_patch_failure_backs_off_instead_of_failing(
    app_client, app_auth, runner, github, error
):
    jid = app_client.post("/jobs", json=JOB, headers=app_auth).json()["id"]
    runner.add_secrets_errors = [error]
    app_client.queue.tick()
    with app_client.queue.sessions() as s:
        row = s.get(Job, jid)
        assert (row.status, row.launch_attempts) == ("queued", 1)
        assert row.not_before is not None and row.message is None
    # The token is removed in case the patch went through after all.
    assert runner.reset == [jid] and "github" not in runner.secrets[jid]
    assert runner.launched == [] and runner.cleaned == []
    _backoff_over(app_client.queue, jid)
    app_client.queue.tick()
    assert runner.secrets[jid]["github"] == "ghs_minted0002"
    assert app_client.get(f"/jobs/{jid}", headers=app_auth).json()["status"] == "running"


def _backoff_over(queue, job_id):
    with queue.sessions() as s:
        s.get(Job, job_id).not_before = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()


# -- several Apps (issue #73) ---------------------------------------------------


@pytest.fixture
def superstar_settings(app_settings, key_file):
    """The builder App and a second one, superstar, with the same key."""
    return dataclasses.replace(
        app_settings,
        github_apps=(GitHubAppConfig("superstar", SUPERSTAR_APP_ID, str(key_file)),),
    )


@pytest.fixture
def two_apps_client(superstar_settings, runner, storage):
    from fastapi.testclient import TestClient

    from drella.app import create_app

    app = create_app(superstar_settings, runner=runner, storage=storage, start_queue=False)
    with TestClient(app) as c:
        c.queue = app.state.deps.queue
        c.queue._sleep = lambda s: None
        yield c


@pytest.fixture
def superstar_auth(superstar_settings, two_apps_client):
    from drella.cli import create_token

    auth = {"Authorization": f"Bearer {create_token(superstar_settings, 'sam', 'superstar')}"}
    assert two_apps_client.put("/models/local", json=LOCAL, headers=auth).status_code == 200
    return auth


def test_superstar_is_minted_from_its_own_app_and_reports_to_out(
    two_apps_client, superstar_auth, runner, github
):
    jid = two_apps_client.post("/jobs", json=JOB, headers=superstar_auth).json()["id"]
    two_apps_client.queue.tick()
    assert github.issuers[-1] == SUPERSTAR_APP_ID
    assert github.requests[-1][2]["permissions"] == SUPERSTAR.permissions
    assert github.requests[-1][2]["repositories"] == ["widgets"]
    assert runner.secrets[jid]["github"] == "ghs_minted0001"
    (spec,) = runner.launched
    # The job's outcome is its report, not a PR; its token can still write.
    assert spec.opens_prs is False and spec.read_only is False
    assert two_apps_client.get(f"/jobs/{jid}", headers=superstar_auth).json()["status"] == "running"


def test_builder_and_reader_still_mint_from_the_builder_app(
    two_apps_client, app_settings, superstar_settings, github
):
    from drella.cli import create_token

    two_apps_client.queue.concurrency = 2  # both jobs launch in one tick
    for name, profile in (("alice", "builder"), ("rita", "reader")):
        auth = {"Authorization": f"Bearer {create_token(superstar_settings, name, profile)}"}
        assert two_apps_client.put("/models/local", json=LOCAL, headers=auth).status_code == 200
        two_apps_client.post("/jobs", json=JOB, headers=auth)
    two_apps_client.queue.tick()
    assert [r[2]["permissions"] for r in github.requests if r[0] == "POST"] == [
        BUILDER.permissions,
        READER.permissions,
    ]
    assert set(github.issuers) == {APP_ID}  # the builder App only, never superstar's


def test_superstar_fails_clearly_at_launch_when_its_app_is_not_configured(
    app_client, runner, github, app_settings
):
    # Only the builder App is configured here.
    from drella.cli import create_token

    auth = {"Authorization": f"Bearer {create_token(app_settings, 'sam', 'superstar')}"}
    assert app_client.put("/models/local", json=LOCAL, headers=auth).status_code == 200
    jid = app_client.post("/jobs", json=JOB, headers=auth).json()["id"]
    app_client.queue.tick()
    got = app_client.get(f"/jobs/{jid}", headers=auth).json()
    assert got["status"] == "failed"
    assert "needs the GitHub App 'superstar', which isn't configured" in got["message"]
    assert github.requests == [] and runner.launched == []
    assert runner.cleaned == [jid]


def test_a_superstar_token_is_checked_like_any_other(two_apps_client, superstar_auth, github):
    # GitHub granting more than the profile (here: administration) is never used.
    github.granted = {**SUPERSTAR.permissions, "administration": "write"}
    jid = two_apps_client.post("/jobs", json=JOB, headers=superstar_auth).json()["id"]
    two_apps_client.queue.tick()
    got = two_apps_client.get(f"/jobs/{jid}", headers=superstar_auth).json()
    assert got["status"] == "failed"
    assert "granted more than the 'superstar' profile" in got["message"]


def test_a_superstar_operators_children_get_the_profile_they_ask_for(
    two_apps_client, superstar_settings, runner, github
):
    """Issue #74: a child never inherits the superstar profile; it's reader or builder."""
    from drella.cli import create_token

    op = create_token(superstar_settings, "sam", "superstar", role="operator")
    auth = {"Authorization": f"Bearer {op}"}
    c = two_apps_client
    assert c.put("/models/local", json=LOCAL, headers=auth).status_code == 200
    jid = c.post("/jobs", json=JOB, headers=auth).json()["id"]
    c.queue.concurrency = 10
    c.queue.tick()
    api = {"Authorization": f"Bearer {runner.secrets[jid]['_api-token']}"}

    reader = c.post("/jobs", json=JOB, headers=api).json()
    builder = c.post("/jobs", json={**JOB, "profile": "builder"}, headers=api).json()
    fix = c.post("/jobs", json={**JOB, "profile": "builder", "pr": 3}, headers=api)
    assert fix.status_code == 201
    assert c.post("/jobs", json={**JOB, "pr": 3}, headers=api).status_code == 422
    c.queue.tick()
    specs = {s.job_id: s for s in runner.launched}
    assert specs[reader["id"]].opens_prs is False and specs[reader["id"]].read_only is True
    assert specs[builder["id"]].opens_prs is True and specs[builder["id"]].read_only is False
    assert specs[fix.json()["id"]].opens_prs is True
    perms = [r[2]["permissions"] for r in github.requests if r[2]]
    assert perms[-3:] == [READER.permissions, BUILDER.permissions, BUILDER.permissions]
    assert github.issuers[-3:] == [APP_ID] * 3  # the builder App, never superstar's


def test_a_transient_launch_error_re_mints_a_superstar_token_from_its_own_app(
    two_apps_client, superstar_auth, runner, github
):
    # The retry path is the same whatever the profile: the minted token is removed and
    # minted again, from the profile's own App.
    jid = two_apps_client.post("/jobs", json=JOB, headers=superstar_auth).json()["id"]
    runner.launch_errors = [_api_error(503)]
    two_apps_client.queue.tick()
    assert runner.reset == [jid] and "github" not in runner.secrets[jid]
    assert github.minted == 1
    _backoff_over(two_apps_client.queue, jid)
    two_apps_client.queue.tick()
    assert github.minted == 2 and runner.secrets[jid]["github"] == "ghs_minted0002"
    assert set(github.issuers) == {SUPERSTAR_APP_ID}
    (spec,) = runner.launched
    assert spec.opens_prs is False
    assert two_apps_client.get(f"/jobs/{jid}", headers=superstar_auth).json()["status"] == "running"


# -- a job's own profile (`profile` on POST /jobs, issue #74) ----------------------


def _token_id(settings, name):
    from drella.cli import list_tokens

    return next(t[0] for t in list_tokens(settings) if t[1] == name)


def _auth_for(c, settings, name, profile, role="member"):
    from drella.cli import create_token

    auth = {"Authorization": f"Bearer {create_token(settings, name, profile, role=role)}"}
    assert c.put("/models/local", json=LOCAL, headers=auth).status_code == 200
    return auth


def _running_api_auth(c, runner, auth):
    """Queue and launch an operator token's job; its API token's bearer header."""
    jid = c.post("/jobs", json=JOB, headers=auth).json()["id"]
    c.queue.concurrency = 10
    c.queue.tick()
    assert c.get(f"/jobs/{jid}", headers=auth).json()["status"] == "running"
    return {"Authorization": f"Bearer {runner.secrets[jid]['_api-token']}"}


def test_a_builder_token_may_queue_a_reader_job_and_it_mints_read_only(
    app_client, app_auth, runner, github
):
    r = app_client.post("/jobs", json={**JOB, "profile": "reader"}, headers=app_auth)
    assert r.status_code == 201, r.text
    assert r.json()["github_profile"] == "reader" and r.json()["parent_job_id"] is None
    app_client.queue.tick()
    assert github.requests[-1][2] == {
        "repositories": ["widgets"],
        "permissions": {"contents": "read", "pull_requests": "read", "issues": "read"},
    }
    (spec,) = runner.launched
    assert spec.read_only is True and spec.opens_prs is False
    assert runner.secrets[r.json()["id"]]["github"] == "ghs_minted0001"


def test_a_wider_profile_is_refused(app_client, app_auth, reader_auth, github):
    for auth, profile, own in ((reader_auth, "builder", "reader"), (app_auth, "superstar", "builder")):
        r = app_client.post("/jobs", json={**JOB, "profile": profile}, headers=auth)
        assert r.status_code == 422, (profile, r.text)
        assert "wider" in r.json()["detail"] and f"'{own}'" in r.json()["detail"]
    # Its own profile is fine.
    assert app_client.post("/jobs", json={**JOB, "profile": "reader"}, headers=reader_auth).status_code == 201
    assert github.requests == []


def test_a_superstar_token_may_queue_builder_and_reader_jobs(
    two_apps_client, superstar_settings, superstar_auth, runner, github
):
    c = two_apps_client
    c.queue.concurrency = 10
    ids = [
        c.post("/jobs", json={**JOB, "profile": p}, headers=superstar_auth).json()["id"]
        for p in ("builder", "reader", "superstar")
    ]
    c.queue.tick()
    perms = [r[2]["permissions"] for r in github.requests if r[2]]
    assert perms == [BUILDER.permissions, READER.permissions, SUPERSTAR.permissions]
    assert github.issuers == [APP_ID] * 4 + [SUPERSTAR_APP_ID] * 2  # two calls per mint
    specs = {s.job_id: s for s in runner.launched}
    assert specs[ids[0]].opens_prs is True and specs[ids[1]].read_only is True


def test_an_api_token_child_is_never_superstar(two_apps_client, superstar_settings, runner):
    c = two_apps_client
    op = _auth_for(c, superstar_settings, "sam", "superstar", role="operator")
    api = _running_api_auth(c, runner, op)
    r = c.post("/jobs", json={**JOB, "profile": "superstar"}, headers=api)
    assert r.status_code == 422 and "superstar" in r.json()["detail"]
    # The operator itself, with its own token, may.
    assert c.post("/jobs", json={**JOB, "profile": "superstar"}, headers=op).status_code == 201


def test_a_reader_operators_children_cant_be_builders(app_client, app_settings, runner, github):
    op = _auth_for(app_client, app_settings, "rob", "reader", role="operator")
    api = _running_api_auth(app_client, runner, op)
    r = app_client.post("/jobs", json={**JOB, "profile": "builder"}, headers=api)
    assert r.status_code == 422 and "wider" in r.json()["detail"]
    r = app_client.post("/jobs", json={**JOB, "pr": 3, "profile": "builder"}, headers=api)
    assert r.status_code == 422
    child = app_client.post("/jobs", json=JOB, headers=api).json()
    assert child["github_profile"] == "reader"


def test_a_job_whose_profile_its_token_no_longer_covers_fails_at_mint(
    app_client, app_auth, app_settings, runner, github
):
    from drella.cli import set_profile

    asked = app_client.post("/jobs", json={**JOB, "profile": "builder"}, headers=app_auth).json()
    own = app_client.post("/jobs", json=JOB, headers=app_auth).json()
    set_profile(app_settings, _token_id(app_settings, "alice"), "reader")
    app_client.queue.concurrency = 10
    app_client.queue.tick()
    got = app_client.get(f"/jobs/{asked['id']}", headers=app_auth).json()
    assert got["status"] == "failed"
    assert "'builder'" in got["message"] and "no longer covers" in got["message"]
    # The job without a profile of its own just follows the token, narrowed.
    assert [r[2]["permissions"] for r in github.requests if r[2]] == [READER.permissions]
    assert [s.job_id for s in runner.launched] == [own["id"]]


def test_a_child_whose_profile_its_operator_no_longer_covers_fails_at_mint(
    app_client, app_settings, runner, github
):
    from drella.cli import set_profile

    op = _auth_for(app_client, app_settings, "bob", "builder", role="operator")
    api = _running_api_auth(app_client, runner, op)
    builder = app_client.post("/jobs", json={**JOB, "profile": "builder"}, headers=api).json()
    reader = app_client.post("/jobs", json=JOB, headers=api).json()
    set_profile(app_settings, _token_id(app_settings, "bob"), "reader")
    app_client.queue.tick()
    assert app_client.get(f"/jobs/{builder['id']}", headers=op).json()["status"] == "failed"
    assert app_client.get(f"/jobs/{reader['id']}", headers=op).json()["status"] == "running"
    assert github.requests[-1][2]["permissions"] == READER.permissions


def test_a_launch_retry_checks_the_profile_again(app_client, app_auth, app_settings, runner, github):
    from drella.cli import set_profile

    jid = app_client.post("/jobs", json={**JOB, "profile": "builder"}, headers=app_auth).json()["id"]
    runner.launch_errors = [_api_error(503)]
    app_client.queue.tick()
    assert runner.reset == [jid] and github.minted == 1
    set_profile(app_settings, _token_id(app_settings, "alice"), "reader")
    _backoff_over(app_client.queue, jid)
    app_client.queue.tick()
    got = app_client.get(f"/jobs/{jid}", headers=app_auth).json()
    assert got["status"] == "failed" and "no longer covers" in got["message"]
    assert github.minted == 1 and runner.launched == []


def test_a_launch_retry_keeps_the_operators_builder_token(app_client, app_settings, runner, github):
    from drella.runner import BUILDER_TOKEN_KEY

    op = _auth_for(app_client, app_settings, "bob", "builder", role="operator")
    app_client.put("/credentials/drella-builder", json={"value": "drt_builderbuilder"}, headers=op)
    jid = app_client.post("/jobs", json=JOB, headers=op).json()["id"]
    runner.launch_errors = [_api_error(503)]
    app_client.queue.tick()
    assert "github" not in runner.secrets[jid]
    assert runner.secrets[jid][BUILDER_TOKEN_KEY] == "drt_builderbuilder"
    _backoff_over(app_client.queue, jid)
    app_client.queue.tick()
    (spec,) = runner.launched
    assert spec.api_token is True
    assert runner.secrets[jid][BUILDER_TOKEN_KEY] == "drt_builderbuilder"
    assert runner.secrets[jid]["github"] == "ghs_minted0002"
