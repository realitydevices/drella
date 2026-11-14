"""The superstar's scheduled run (issue #75): the CronJob script and the prompt it posts."""

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from drella.app import MAX_PROMPT_BYTES, JobIn

ROOT = Path(__file__).resolve().parent.parent
PROMPT = ROOT / "docs" / "superstar.md"
CRONJOB = ROOT / "deploy" / "superstar-cronjob.yaml"
MARKER = "drella-superstar-run:v1"


def _cronjob() -> dict:
    (cj,) = [d for d in yaml.safe_load_all(CRONJOB.read_text()) if d and d["kind"] == "CronJob"]
    return cj


def test_the_cronjob_runs_every_30_minutes_one_at_a_time_outside_the_kustomization():
    cj = _cronjob()
    assert cj["spec"]["schedule"] == "*/30 * * * *"
    assert cj["spec"]["concurrencyPolicy"] == "Forbid"
    kustomization = yaml.safe_load((ROOT / "deploy" / "kustomization.yaml").read_text())
    assert not any("superstar" in r for r in kustomization.get("resources", []))


def test_the_prompt_fits_a_job_and_starts_with_the_marker():
    text = PROMPT.read_text()
    assert text.splitlines()[0] == MARKER
    assert len(text.encode()) <= MAX_PROMPT_BYTES
    # The CronJob JSON-encodes only backslashes, quotes and newlines.
    assert not [c for c in text if (ord(c) < 0x20 and c != "\n") or ord(c) == 0x7F]


@pytest.mark.parametrize("forbidden", ["gh pr merge", "gh pr checkout", "--admin"])
def test_the_prompt_has_no_merge_or_checkout_commands(forbidden):
    assert forbidden not in PROMPT.read_text()


class _StubDrella(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.requests.append(("GET", self.path, self.headers.get("Authorization")))
        if self.server.fail_get:
            return self._send(500, {"detail": "boom"})
        wanted = parse_qs(urlparse(self.path).query).get("status", [""])[0].split(",")
        self._send(200, [j for j in self.server.jobs if j["status"] in wanted])

    def do_POST(self):
        self.server.requests.append(("POST", self.path, self.headers.get("Authorization")))
        self.server.posted.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self._send(201, {"id": "new", "status": "queued"})


def _run(tmp_path: Path, jobs: list[dict], fail_get: bool = False):
    if shutil.which("curl") is None:
        pytest.skip("the CronJob script needs curl")
    prompt = tmp_path / "superstar.md"
    prompt.write_text(PROMPT.read_text())
    script = (
        _cronjob()["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][-1]
        .replace("/prompt/superstar.md", str(prompt))
        .replace("/tmp/job.json", str(tmp_path / "job.json"))
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubDrella)
    server.jobs, server.fail_get, server.posted, server.requests = jobs, fail_get, [], []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = {
            "PATH": os.environ["PATH"],
            "DRELLA_URL": f"http://127.0.0.1:{server.server_port}",
            "DRELLA_TOKEN": "t0ken",
        }
        result = subprocess.run(
            ["sh", "-ec", script], env=env, capture_output=True, text=True, timeout=60
        )
    finally:
        server.shutdown()
        server.server_close()
    assert {auth for _, _, auth in server.requests} == {"Bearer t0ken"}
    # Only in-flight jobs are fetched, never the whole history.
    assert [p for m, p, _ in server.requests if m == "GET"] == ["/jobs?status=queued,running"]
    return result, server.posted


def _job(status: str, prompt: str) -> dict:
    return {"id": "j", "status": status, "prompt": prompt}


def test_the_cronjob_posts_a_valid_run_without_a_profile(tmp_path):
    result, posted = _run(
        tmp_path,
        [
            _job("running", "superstar-review: PR 9 head " + "a" * 40),
            _job("succeeded", MARKER + "\nan old run"),
        ],
    )
    assert result.returncode == 0, result.stderr
    (body,) = posted
    assert "profile" not in body
    job = JobIn.model_validate(body)
    assert job.prompt == PROMPT.read_text()
    assert job.repo == "realitydevices/drella"


@pytest.mark.parametrize("status", ["queued", "running"])
def test_the_cronjob_skips_while_a_run_is_in_flight(tmp_path, status):
    result, posted = _run(tmp_path, [_job(status, MARKER + "\nthe last run")])
    assert result.returncode == 0, result.stderr
    assert posted == []


def test_the_cronjob_fails_and_posts_nothing_when_listing_fails(tmp_path):
    result, posted = _run(tmp_path, [], fail_get=True)
    assert result.returncode != 0
    assert posted == []
