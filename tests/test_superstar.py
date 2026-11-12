import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
MARKER = "drella-superstar-run:v1"


def test_the_prompt_exists_starts_with_the_marker_and_fits_a_job_prompt():
    text = (ROOT / "docs" / "superstar.md").read_text()
    assert text.splitlines()[0] == MARKER
    assert len(text.encode()) <= 64 * 1024  # MAX_PROMPT_BYTES in drella/app.py
    assert "<!-- drella-superstar-ledger:v1 -->" in text
    assert "Verdict: approve with nits" in text


def test_the_prompt_has_the_merge_rules_it_must_keep():
    text = (ROOT / "docs" / "superstar.md").read_text()
    # The merge command must pass --admin, or GitHub refuses it (the ruleset bypass needs it).
    assert "gh pr merge N --repo realitydevices/drella --squash --admin --match-head-commit HEAD" in text
    # Only the App's own ledger is read.
    assert '.user.login=="drella-superstar[bot]"' in text
    # Carried approvals need the mechanical merge-tree check.
    assert "git merge-tree --write-tree R origin/main" in text
    assert 'test "$(head -n 1 /tmp/mt-N.txt)" = "$(git rev-parse HEAD^{tree})"' in text


OWNERSHIP = (
    '.isCrossRepository==false and .author.login=="app/drella-bot"'
    ' and (.headRefName|startswith("drella/"))'
)


def _commands(text: str, prefix: str) -> list[str]:
    """Each command line in the prompt that starts with `prefix` (in or out of backticks)."""
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("`").strip()
        if line.startswith(prefix) and "--repo" in line:
            out.append(line)
    return out


def test_every_pr_selection_checks_it_is_ours_and_not_a_fork():
    text = (ROOT / "docs" / "superstar.md").read_text()
    lists = _commands(text, "gh pr list")
    assert len(lists) >= 2  # Step 1 and 2g
    for cmd in lists:
        assert "isCrossRepository" in cmd and "author" in cmd, cmd
        assert OWNERSHIP in cmd, cmd
    # (Not the CI check or the merge subject: they read a PR already chosen.)
    views = [
        c for c in _commands(text, "gh pr view N")
        if "statusCheckRollup,headRefOid" not in c and "--json title,number" not in c
    ]
    assert views
    for cmd in views:
        assert "isCrossRepository" in cmd and "author" in cmd, cmd
    # The check is repeated right before the merge, with isDraft fetched where it's used.
    merge_at = text.index("gh pr merge N --repo")
    recheck = text.index("gh pr view N", text.index("**Immediately before merging**"))
    assert recheck < merge_at
    block = text[recheck:merge_at]
    assert "isDraft" in block and '.isDraft==false' in block
    assert '.isCrossRepository==false and .author.login=="app/drella-bot"' in block


def test_the_prompt_uses_scripts_not_shell_state():
    text = (ROOT / "docs" / "superstar.md").read_text()
    # Functions and variables don't survive between Bash calls: no api() function, no $REPO.
    assert "api()" not in text and "$REPO" not in text and "REPO=" not in text
    assert "cat > /tmp/api <<'EOF'" in text and "cat > /tmp/bapi <<'EOF'" in text
    assert "$DRELLA_BUILDER_TOKEN" in text


def test_reviews_use_reader_and_builds_use_the_builder_token():
    text = (ROOT / "docs" / "superstar.md").read_text()
    assert (
        '''model:$model, profile:"reader"}' > /tmp/review-N.json\n/tmp/api /jobs -X POST''' in text
    )
    assert "/tmp/bapi /jobs -X POST -d @/tmp/fix-N.json" in text
    # Never a superstar or builder profile on a child job.
    assert 'profile:"builder"' not in text and '"profile":"builder"' not in text
    assert '"profile":"superstar"' not in text and 'profile:"superstar"' not in text
    assert "/tmp/api /models" in text and "/tmp/bapi /models" in text


def test_the_prompt_change_check_pages_through_all_files():
    text = (ROOT / "docs" / "superstar.md").read_text()
    assert (
        "gh api repos/realitydevices/drella/pulls/N/files --paginate --jq '.[].filename'"
        " | grep -qx 'docs/superstar.md'" in text
    )
    for cmd in _commands(text, "gh pr view"):
        fields = cmd.split("--json ", 1)[1].split()[0].split(",")
        assert "files" not in fields, cmd


def test_the_review_rule_always_leads_somewhere():
    """Each rule that holds reviews back names what happens instead: a verify job, a fix,
    a fresh review, or needs-kirk. Text checks, since the prompt is prose."""
    text = (ROOT / "docs" / "superstar.md").read_text()
    reviews = text[text.index("### 2e. Reviews on the current head") : text.index("### 2f. Fix")]
    assert "approvals + in flight < 2" in reviews
    assert "has reached 4 for this head" in reviews and "needs-kirk" in reviews
    assert "waiting on a verify job" in reviews and "2f queues the fix" in reviews
    findings = text[text.index("### 2d. Bring open findings") : text.index("### 2e. Reviews")]
    # Old findings get re-checked against the new head, so a fix unblocks reviews...
    assert "whose `head` isn't `HEAD`" in findings and "set `fixed_in` to `HEAD`" in findings
    # ...and an unverified finding gets one real attempt, then a decision or needs-kirk.
    assert "one verify job" in findings and "label the" in findings and "needs-kirk" in findings
    assert "never left `unverified` with nothing in flight" in findings


def test_the_run_never_checks_out_or_runs_pr_code():
    text = (ROOT / "docs" / "superstar.md").read_text()
    assert "**You never check out or run PR code.**" in text
    # Only the reviewer and verify-job prompts (which run with reader tokens) check out a PR.
    own_steps = text[: text.index("## The review prompt template")]
    rule = own_steps.index("**You never check out or run PR code.**")
    verify = own_steps.index("**Verify jobs.**")
    allowed = [(rule, rule + 900), (verify, verify + 1500)]
    import re

    for m in re.finditer(r"gh\s+pr\s+checkout", own_steps):
        assert any(a < m.start() < b for a, b in allowed), own_steps[m.start() - 200 : m.end()]
    assert "run it." not in own_steps and "running the test it names" not in own_steps


def test_the_cronjob_parses_and_is_not_in_the_default_kustomization():
    docs = list(yaml.safe_load_all((ROOT / "deploy" / "superstar-cronjob.yaml").read_text()))
    (cj,) = [d for d in docs if d and d["kind"] == "CronJob"]
    assert cj["spec"]["schedule"] == "*/30 * * * *"
    assert cj["spec"]["concurrencyPolicy"] == "Forbid"
    script = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    assert "claude-opus" in script and "realitydevices/drella" in script
    assert "profile" not in script
    kustomization = yaml.safe_load((ROOT / "deploy" / "kustomization.yaml").read_text())
    resources = kustomization.get("resources", [])
    assert not any("superstar-cronjob" in r for r in resources)


def _cronjob_script() -> str:
    docs = list(yaml.safe_load_all((ROOT / "deploy" / "superstar-cronjob.yaml").read_text()))
    (cj,) = [d for d in docs if d and d["kind"] == "CronJob"]
    return cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][-1]


class _StubDrella(BaseHTTPRequestHandler):
    """Answers GET /jobs with server.jobs and POST /jobs by recording the body."""

    def log_message(self, *args):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse

        self.server.auths.append(self.headers.get("Authorization"))
        self.server.paths.append(self.path)
        if self.server.fail_get:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # Like Drella's GET /jobs?status=a,b: only jobs with those statuses.
        wanted = parse_qs(urlparse(self.path).query).get("status")
        jobs = self.server.jobs
        if wanted:
            statuses = wanted[0].split(",")
            jobs = [j for j in jobs if j.get("status") in statuses]
        self._send(jobs)

    def do_POST(self):
        self.server.auths.append(self.headers.get("Authorization"))
        length = int(self.headers["Content-Length"])
        self.server.posted.append(json.loads(self.rfile.read(length)))
        self._send({"id": "new-job", "status": "queued"})


def _job(status: str, prompt: str) -> dict:
    return {"id": "job-1", "status": status, "repo": None, "prompt": prompt}


def _run_cronjob(tmp_path: Path, jobs: list[dict], fail_get: bool = False):
    if shutil.which("curl") is None:
        pytest.skip("the CronJob script needs curl")
    prompt_file = tmp_path / "superstar.md"
    prompt_file.write_text((ROOT / "docs" / "superstar.md").read_text())
    job_file = tmp_path / "job.json"
    # The script reads the prompt from /prompt and writes /tmp/job.json. Point both at tmp_path.
    script = (
        _cronjob_script()
        .replace("/prompt/superstar.md", str(prompt_file))
        .replace("/tmp/job.json", str(job_file))
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubDrella)
    server.jobs = jobs
    server.posted = []
    server.auths = []
    server.paths = []
    server.fail_get = fail_get
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {
            "PATH": os.environ["PATH"],
            "DRELLA_URL": f"http://127.0.0.1:{server.server_port}",
            "DRELLA_TOKEN": "test-token",
        }
        result = subprocess.run(
            ["sh", "-ec", script], env=env, capture_output=True, text=True, timeout=60
        )
    finally:
        server.shutdown()
        server.server_close()
    assert server.auths and set(server.auths) == {"Bearer test-token"}
    # Only queued and running jobs are fetched, never the whole history.
    assert server.paths == ["/jobs?status=queued,running"]
    return result, server.posted


def test_the_cronjob_posts_the_run_when_no_superstar_job_is_running(tmp_path):
    result, posted = _run_cronjob(
        tmp_path,
        [
            _job("queued", "superstar: review PR 80 head abc1234 round 1\nnot the supervisor"),
            _job("succeeded", MARKER + "\nan old run that finished"),
        ],
    )
    assert result.returncode == 0, result.stderr
    assert "skipping" not in result.stdout
    (job,) = posted
    assert job["prompt"] == (ROOT / "docs" / "superstar.md").read_text()
    assert job["model"] == "claude-opus"
    assert job["repo"] == "realitydevices/drella"
    assert "profile" not in job
    assert "\t" not in job["prompt"]
    # The body must be one the real API accepts (JobIn forbids unknown fields).
    from drella.app import JobIn

    JobIn.model_validate(job)


@pytest.mark.parametrize("status", ["queued", "running"])
def test_the_cronjob_skips_when_a_superstar_run_is_queued_or_running(tmp_path, status):
    result, posted = _run_cronjob(tmp_path, [_job(status, MARKER + "\nthe last run")])
    assert result.returncode == 0, result.stderr
    assert "already queued or running; skipping" in result.stdout
    assert posted == []


def test_the_api_scripts_send_the_right_token(tmp_path):
    if shutil.which("curl") is None:
        pytest.skip("the scripts need curl")
    text = (ROOT / "docs" / "superstar.md").read_text()
    start = text.index("cat > /tmp/api <<'EOF'")
    setup = text[start : text.index("```", start)]
    setup = setup.replace("/tmp/api", str(tmp_path / "api")).replace("/tmp/bapi", str(tmp_path / "bapi"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubDrella)
    server.jobs = [{"id": "j"}]
    server.posted = []
    server.auths = []
    server.paths = []
    server.fail_get = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {
            "PATH": os.environ["PATH"],
            "DRELLA_API_URL": f"http://127.0.0.1:{server.server_port}",
            "DRELLA_API_TOKEN": "api-token",
            "DRELLA_BUILDER_TOKEN": "builder-token",
        }
        # Each step is a separate shell, as each Bash call is for the agent.
        subprocess.run(["sh", "-ec", setup], env=env, check=True, timeout=30)
        got = subprocess.run(
            [str(tmp_path / "api"), "/jobs"], env=env, capture_output=True, text=True, timeout=30
        )
        (tmp_path / "job.json").write_text('{"prompt": "x", "model": "m"}')
        subprocess.run(
            [str(tmp_path / "bapi"), "/jobs", "-X", "POST", "-d", f"@{tmp_path / 'job.json'}"],
            env=env, check=True, capture_output=True, timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert json.loads(got.stdout) == [{"id": "j"}]
    assert server.auths == ["Bearer api-token", "Bearer builder-token"]
    assert server.posted == [{"prompt": "x", "model": "m"}]


def test_the_cronjob_posts_nothing_when_it_cant_list_jobs(tmp_path):
    result, posted = _run_cronjob(tmp_path, [], fail_get=True)
    assert result.returncode != 0
    assert posted == []


def test_the_prompt_has_no_control_characters_the_cronjob_cant_encode():
    """The CronJob JSON-encodes only backslashes, quotes and newlines."""
    text = (ROOT / "docs" / "superstar.md").read_text()
    bad = sorted({hex(ord(c)) for c in text if ord(c) < 0x20 and c != "\n" or ord(c) == 0x7F})
    assert bad == []


def test_the_cronjob_and_readme_install_it_the_same_way():
    """kustomize refuses files outside an overlay, so it's a ConfigMap and kubectl apply."""
    yaml_text = (ROOT / "deploy" / "superstar-cronjob.yaml").read_text()
    readme = (ROOT / "deploy" / "README.md").read_text()
    for text in (yaml_text, readme):
        flat = " ".join(text.replace("#", " ").replace("\\\n", " ").split())
        assert (
            "kubectl -n drella create configmap superstar-prompt"
            " --from-file=superstar.md=docs/superstar.md --dry-run=client -o yaml"
            " | kubectl apply -f -" in flat
        )
        assert "kubectl apply -f deploy/superstar-cronjob.yaml" in flat
        assert "configMapGenerator" not in text
