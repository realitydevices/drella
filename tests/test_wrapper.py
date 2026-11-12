"""The job wrapper's security switches (docs/v0.2.md, docs/v0.3.md).

These flags are the actual control that stops a cloned repo configuring the agent: a
repo's opencode.json or .opencode/ could point the provider (and the key) elsewhere,
and a repo's .claude/settings.json or .mcp.json could run hooks or set an API base URL.
They're easy to drop by accident in an edit, so pin them.
"""

from pathlib import Path

WRAPPER = (Path(__file__).parent.parent / "images/drella-job/drella-job").read_text()


def test_opencode_never_reads_project_config():
    assert "export OPENCODE_DISABLE_PROJECT_CONFIG=1" in WRAPPER


def test_claude_code_reads_only_user_settings_and_no_project_mcp():
    assert "--setting-sources user" in WRAPPER or "--setting-sources=user" in WRAPPER
    assert "--strict-mcp-config" in WRAPPER


def test_git_sends_the_github_token_only_to_github():
    assert "credential.https://github.com.helper" in WRAPPER
    assert "credential.helper " not in WRAPPER


def test_agent_output_is_never_read_through_a_symlink():
    assert "iflag=nofollow" in WRAPPER
    assert 'cp "$WORK/agent.out"' not in WRAPPER


def test_wrapper_redacts_every_job_secret():
    for name in ("DRELLA_LLM_KEY", "GH_TOKEN", "DRELLA_JOB_TOKEN", "DRELLA_API_TOKEN"):
        assert f'ENVIRON["{name}"]' in WRAPPER


def test_redact_hides_an_operator_jobs_api_token():
    # issue #74: the API token can queue jobs as the caller, so it's redacted like the
    # others. Run the wrapper's own redact() on a line that prints it.
    import os
    import subprocess

    fn = "redact() {" + WRAPPER.split("\nredact() {", 1)[1].split("\n}\n", 1)[0] + "\n}"
    env = {**os.environ, "DRELLA_API_TOKEN": "dra_secretsecret", "DRELLA_JOB_TOKEN": "drj_jobjobjob"}
    r = subprocess.run(
        ["bash", "-c", fn + "\nredact"],
        input="token dra_secretsecret and drj_jobjobjob\n",
        capture_output=True, text=True, env=env, check=True,
    )
    assert r.stdout == "token [redacted] and [redacted]\n"


def test_an_operator_job_is_told_about_its_api_token():
    assert 'if [ -n "${DRELLA_API_TOKEN:-}" ]; then' in WRAPPER
    assert "$DRELLA_API_TOKEN as a bearer token" in WRAPPER


def test_wrapper_turns_on_auto_merge_on_success():
    assert 'gh pr merge "$pr" --auto --squash' in WRAPPER


# -- usage parsing [46] ---------------------------------------------------------------

import json
import shutil
import subprocess

import pytest

LIB = Path(__file__).parent.parent / "images/drella-job"
needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _jq(args, path):
    r = subprocess.run(["jq", *args, str(path)], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def _opencode(tmp_path, doc):
    f = tmp_path / "session.json"
    f.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return _jq(["-c", "-f", str(LIB / "usage-opencode.jq")], f)


def _claude(tmp_path, lines):
    f = tmp_path / "session.jsonl"
    f.write_text("\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines) + "\n")
    return _jq(["-c", "-R", "-n", "-f", str(LIB / "usage-claude-code.jq")], f)


def _msg(role, inp, out, reasoning=0, read=0, write=0):
    return {"info": {"role": role, "tokens": {
        "input": inp, "output": out, "reasoning": reasoning,
        "cache": {"read": read, "write": write}}}, "parts": []}


@needs_jq
def test_opencode_sums_assistant_messages(tmp_path):
    rc, out = _opencode(tmp_path, {"info": {}, "messages": [
        {"info": {"role": "user"}, "parts": []},
        _msg("assistant", 100, 20, reasoning=5, read=0, write=300),
        _msg("assistant", 150, 30, reasoning=10, read=300, write=50),
    ]})
    assert rc == 0
    assert json.loads(out) == {
        "input_tokens": 250, "output_tokens": 65,
        "cache_read_tokens": 300, "cache_write_tokens": 350,
        "peak_context_tokens": 500,
    }


@needs_jq
@pytest.mark.parametrize("doc", [{"messages": []}, {"messages": [{"info": {"role": "user"}}]}, {}])
def test_opencode_without_assistant_messages_has_no_usage(tmp_path, doc):
    assert _opencode(tmp_path, doc) == (0, "null")


@needs_jq
def test_opencode_unparseable_record_fails_quietly(tmp_path):
    rc, out = _opencode(tmp_path, "{not json")
    assert rc != 0 and out == ""


def _assistant(inp, read, write):
    return {"type": "assistant", "message": {"usage": {
        "input_tokens": inp, "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write, "output_tokens": 1}}}


RESULT = {"type": "result", "subtype": "success", "total_cost_usd": 0.0421, "usage": {
    "input_tokens": 12, "output_tokens": 3400,
    "cache_read_input_tokens": 90000, "cache_creation_input_tokens": 8000}}


@needs_jq
def test_claude_code_uses_the_result_event(tmp_path):
    rc, out = _claude(tmp_path, [
        {"type": "system", "subtype": "init"},
        _assistant(5, 0, 8000), "garbage line", _assistant(7, 40000, 100), RESULT,
    ])
    assert rc == 0
    assert json.loads(out) == {
        "input_tokens": 12, "output_tokens": 3400,
        "cache_read_tokens": 90000, "cache_write_tokens": 8000,
        "peak_context_tokens": 40107, "cost_usd": 0.0421,
    }


@needs_jq
def test_claude_code_without_cost_or_assistant_events(tmp_path):
    result = {"type": "result", "usage": RESULT["usage"]}
    rc, out = _claude(tmp_path, [result])
    assert rc == 0
    got = json.loads(out)
    assert "cost_usd" not in got and "peak_context_tokens" not in got


@needs_jq
@pytest.mark.parametrize("lines", [[], ["not json"], [{"type": "assistant"}]])
def test_claude_code_without_a_result_has_no_usage(tmp_path, lines):
    assert _claude(tmp_path, lines) == (0, "null")


def test_wrapper_sends_usage_only_as_an_object():
    assert "usage-opencode.jq" in WRAPPER and "usage-claude-code.jq" in WRAPPER
    assert "type == \"object\"" in WRAPPER


def test_the_github_token_is_redacted():
    assert 'k[++n] = ENVIRON["GH_TOKEN"]' in WRAPPER


def test_a_read_only_repo_job_writes_results_instead_of_a_pr():
    # A read-only GitHub token (the reader profile) can't push or open a PR, so the
    # agent is told so, the job succeeds on its outputs, and auto-merge isn't touched.
    assert '[ "${DRELLA_REPO_READ_ONLY:-}" = 1 ] && READ_ONLY=1' in WRAPPER
    assert "Your GitHub access is read-only" in WRAPPER
    assert 'if [ -n "$REPO" ] && [ -z "$REPORT" ]; then' in WRAPPER
    assert 'elif [ -z "$REPO" ] || [ -n "$REPORT" ]; then' in WRAPPER


def test_a_report_job_can_write_to_github_but_succeeds_on_its_report():
    # superstar (issue #73): its token can write, so the standing instructions don't say
    # it's read-only, but its outcome is its report, as for a reader: no PR is looked for,
    # and the job succeeds on the files it wrote to /out.
    assert '[ -n "$READ_ONLY" ] || [ "${DRELLA_REPO_REPORT:-}" = 1 ]' in WRAPPER
    assert 'elif [ -n "$REPORT" ]; then' in WRAPPER
    assert "Your result is a report: write it as files in $OUT." in WRAPPER
    report_branch = WRAPPER.split('elif [ -n "$REPORT" ]; then', 1)[1].split("elif [ -n", 1)[0]
    assert "Your GitHub access is read-only" not in report_branch


def test_the_wrapper_never_asks_drella_for_another_github_token():
    # The token minted at dispatch is the job's only one: its deadline is capped at the
    # token's expiry instead (test_runner.py).
    assert "github-token" not in WRAPPER


def test_opencode_export_writes_to_file_before_redact():
    # Piping opencode export straight into redact truncates at ~64 KiB because the
    # process exits before stdout is drained when stdout is a pipe. The fix writes to
    # a temp file from mktemp first, then redacts it (issue #55).
    assert 'opencode export "$session" 2>/dev/null | redact' not in WRAPPER
    assert 'session.raw.json' not in WRAPPER
    assert 'mktemp' in WRAPPER
    # mktemp must appear in the session export block (within 10 lines before "opencode export")
    lines = WRAPPER.split('\n')
    for i, line in enumerate(lines):
        if 'opencode export' in line:
            block = '\n'.join(lines[max(0, i - 10):i])
            assert 'mktemp' in block, "mktemp must appear within 10 lines before opencode export"


def test_wrapper_keeps_usage_only_within_the_apis_limits():
    # drella/app.py rejects anything else (MAX_USAGE_TOKENS = 10**13, MAX_COST = 100_000).
    assert "1e13" in WRAPPER and "<= 100000" in WRAPPER
    assert ". != floor" in WRAPPER and "< 0" in WRAPPER
    assert "|| USAGE=\"\"" in WRAPPER


def test_wrapper_retries_the_status_without_usage():
    report = WRAPPER[WRAPPER.index("report() {"):WRAPPER.index("# finish STATUS MESSAGE")]
    assert "retrying without it" in report
    assert report.count("/status") == 2
    assert "usage: $u" in report and "{status: $s, message: $m}')" in report


def test_usage_is_parsed_after_the_pr_lookup():
    assert WRAPPER.index("gh pr list") < WRAPPER.index("usage-claude-code.jq") < WRAPPER.index("# -- 5. report")


# -- fixing an existing PR (issue #58) ------------------------------------------------


def test_fix_pr_only_touches_drella_branches_of_open_same_repo_prs():
    assert "--json state,headRefName,headRepositoryOwner,isCrossRepository" in WRAPPER
    assert '[ "$state" = OPEN ]' in WRAPPER
    assert '[ "$cross" = false ]' in WRAPPER
    assert "drella/*) ;;" in WRAPPER
    assert '"$PR_BRANCH" = "$DEFAULT_BRANCH"' in WRAPPER and '"$PR_BRANCH" = main' in WRAPPER


def test_fix_pr_pushes_with_force_with_lease_and_never_plain_force():
    assert "git push --force-with-lease origin $PR_BRANCH" in WRAPPER
    assert "--force " not in WRAPPER.replace("--force-with-lease", "")
    assert "Do not open a new pull request." in WRAPPER


def test_fix_pr_rebases_then_merges_then_leaves_the_branch():
    assert 'git rebase "origin/$DEFAULT_BRANCH"' in WRAPPER
    assert "git rebase --abort" in WRAPPER
    assert 'git merge --no-edit "origin/$DEFAULT_BRANCH"' in WRAPPER
    assert "git merge --abort" in WRAPPER


def test_fix_pr_succeeds_when_the_remote_head_moves_and_comments_without_failing():
    assert 'git ls-remote origin "refs/heads/$PR_BRANCH"' in WRAPPER
    assert '"$remote_head" != "$PR_HEAD"' in WRAPPER
    assert "Updated by Drella job $DRELLA_JOB_ID." in WRAPPER
    assert "couldn't comment on PR" in WRAPPER


# -- the wait for the agent [15] ----------------------------------------------------
#
# These run the wrapper's own lines (between "# -- agent: start" and "# -- agent: end"),
# with a fake agent and stubs for what comes before them.

import os
import re
import time


def _section(start, end):
    m = re.search(start + r".*?" + end, WRAPPER, re.S | re.M)
    assert m, f"{start!r} not found in the wrapper"
    return m.group(0)


def _agent_block():
    return _section(r"^# -- agent: start", r"^# -- agent: end\n")


def _wait_logic():
    return _section(r"^DRAIN_GRACE=", r"^wait_for_agent\(\) \{.*?^\}\n")


def _read(p):
    try:
        return p.read_bytes()
    except OSError:
        return b""


def _left_alive(tmp_path):
    """Whether the leftover whose pid the fake agent wrote to leftover.pid still runs."""
    time.sleep(0.5)
    pid = int((tmp_path / "leftover.pid").read_text())
    stat = _read(Path(f"/proc/{pid}/stat"))
    return bool(stat) and stat.rsplit(b")", 1)[1].split()[0] != b"Z"


LEFT = "echo $! >{}/leftover.pid; "


def _run_agent(tmp_path, agent_body, grace=2, finish_in=1000, reserve=2, budget=100, before=""):
    """Run the wrapper's agent block with AGENT_BODY as the agent (a bash script)."""
    (tmp_path / "out").mkdir()
    (tmp_path / "agent.sh").write_text(agent_body)
    if before:
        (tmp_path / "out/transcript.txt").write_text(before)
    script = f"""
set -uo pipefail
WORK={tmp_path}; OUT={tmp_path}/out
AGENT=opencode DRELLA_LLM_KEY=x stopped=""
RESERVE={reserve} budget={budget}
FINISH_BY=$(($(date +%s) + {finish_in}))
left() {{ echo $((FINISH_BY - $(date +%s))); }}
log() {{ echo "[drella] $*"; }}
finish() {{ echo "finish $*"; exit 1; }}
redact() {{ cat; }}; render() {{ cat; }}
agent_cmd=(bash {tmp_path}/agent.sh)
DRELLA_DRAIN_GRACE={grace}
{_agent_block()}
read -r -a me </proc/$$/stat
echo "rc=$rc left=$(left) elapsed=$elapsed agent_pid=$agent_pid wrapper_sid=${{me[5]}} deadline=$stopped_at_deadline"
"""
    t = time.monotonic()
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    return r, time.monotonic() - t


def _transcript(tmp_path):
    return (tmp_path / "out/transcript.txt").read_text()


def _field(r, name):
    m = re.search(rf"\b{name}=(\S*)", r.stdout)
    assert m, r.stdout + r.stderr
    return m.group(1)


def test_wait_returns_when_the_agent_left_a_process_holding_stdout(tmp_path):
    r, took = _run_agent(tmp_path, "sleep 300 & " + LEFT.format(tmp_path) + "echo hi; exit 0")
    assert _field(r, "rc") == "0", r.stdout + r.stderr
    assert took < 15
    assert "stopped the agent's leftover processes" in _transcript(tmp_path)
    assert "hi" in _transcript(tmp_path)
    assert not _left_alive(tmp_path)


def test_a_leftover_that_redirected_its_output_is_stopped_too(tmp_path):
    # `npm run dev > dev.log 2>&1 &`: the pipe ends by itself, the server mustn't live on.
    r, took = _run_agent(tmp_path, "sleep 301 >/dev/null 2>&1 & " + LEFT.format(tmp_path) + "echo hi; exit 0")
    assert _field(r, "rc") == "0", r.stdout + r.stderr
    assert took < 5
    assert "stopped the agent's leftover processes" in _transcript(tmp_path)
    assert not _left_alive(tmp_path)


def test_wait_is_unchanged_when_nothing_is_left_running(tmp_path):
    r, took = _run_agent(tmp_path, "echo hi; exit 3")
    assert _field(r, "rc") == "3"
    assert took < 5
    assert "left processes" not in _transcript(tmp_path)


def test_the_transcript_is_appended_to(tmp_path):
    _run_agent(tmp_path, "echo hi; exit 0", before="setup output\n")
    assert _transcript(tmp_path).startswith("setup output\nhi\n")


def test_the_agent_runs_in_its_own_session_and_process_group(tmp_path):
    r, _ = _run_agent(tmp_path, 'read -r -a f </proc/$$/stat; echo "agent_pgid=${f[4]} agent_sid=${f[5]}"')
    agent_pid = _field(r, "agent_pid")
    assert _field(r, "agent_pgid") == agent_pid
    assert _field(r, "agent_sid") == agent_pid  # setsid, not just timeout's own group
    assert _field(r, "agent_sid") != _field(r, "wrapper_sid")


def test_the_exit_code_comes_from_wait_not_a_file_the_agent_writes(tmp_path):
    # Writing "0" to the old exit-code files mustn't end the wait or change the result.
    r, took = _run_agent(tmp_path, f'echo 0 >{tmp_path}/agent.rc; echo 1 >{tmp_path}/agent.pid; sleep 2; exit 4')
    assert _field(r, "rc") == "4", r.stdout + r.stderr
    assert took >= 2


def test_wait_respects_the_deadline_and_leaves_the_reserve(tmp_path):
    r, took = _run_agent(tmp_path, "sleep 61 & " + LEFT.format(tmp_path) + "wait", grace=10, finish_in=6, reserve=3)
    assert int(_field(r, "left")) >= 3, r.stdout + r.stderr
    assert took < 10
    assert _field(r, "rc") == "137"
    assert _field(r, "deadline") == "1"
    assert "the agent was still running at the deadline; stopped it" in _transcript(tmp_path)
    assert not _left_alive(tmp_path)


def test_the_agents_timer_stops_it(tmp_path):
    r, took = _run_agent(tmp_path, "sleep 64 & " + LEFT.format(tmp_path) + "wait", budget=2)
    assert _field(r, "rc") == "124", r.stdout + r.stderr
    assert _field(r, "deadline") == ""
    assert took < 8
    assert not _left_alive(tmp_path)


def test_elapsed_does_not_include_the_drain(tmp_path):
    r, took = _run_agent(tmp_path, "sleep 302 & exit 5", grace=3)
    assert took >= 3
    assert _field(r, "rc") == "5"
    assert int(_field(r, "elapsed")) <= 1, r.stdout


def test_wait_does_not_claim_success_when_a_process_escaped(tmp_path):
    try:
        r, _ = _run_agent(tmp_path, "setsid sleep 30 & " + LEFT.format(tmp_path) + "echo hi; exit 0")
        text = _transcript(tmp_path)
        assert "may still be running" in text, r.stdout + r.stderr
        assert "stopped the agent's leftover processes" not in text
    finally:
        # setsid execs in place here (a background job isn't a group leader), so the
        # recorded pid is the escaped process.
        if _left_alive(tmp_path):
            os.kill(int((tmp_path / "leftover.pid").read_text()), 9)


def _run_wait_with_fake_kill(tmp_path, agent_pid, grace="2"):
    """wait_for_agent with `kill` recorded instead of run: nothing is really signalled."""
    script = f"""
set -uo pipefail
WORK={tmp_path}; OUT={tmp_path}
RESERVE=2; left() {{ echo 1000; }}; log() {{ :; }}
kill() {{ echo "kill $*" >>"$WORK/kills"; return 1; }}
DRELLA_DRAIN_GRACE='{grace}'
{_wait_logic()}
agent_pid='{agent_pid}' pipe_pid=999999
wait_for_agent
echo "drain=$DRAIN_GRACE"
"""
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    kills = tmp_path / "kills"
    return r, kills.read_text() if kills.exists() else ""


@pytest.mark.parametrize("pid", ["0", "1", "-1", "abc", ""])
def test_a_bad_agent_pid_never_signals_a_process_group(tmp_path, pid):
    r, kills = _run_wait_with_fake_kill(tmp_path, pid)
    assert r.returncode == 0, r.stderr
    assert " -- " not in kills, kills


def test_a_good_agent_pid_signals_its_process_group(tmp_path):
    _, kills = _run_wait_with_fake_kill(tmp_path, "12345")
    assert "kill -KILL -- -12345" in kills


@pytest.mark.parametrize("grace", ["abc", "-5", "1; true", ""])
def test_a_bad_drain_grace_falls_back_to_the_default(tmp_path, grace):
    r, _ = _run_wait_with_fake_kill(tmp_path, "12345", grace=grace)
    assert "drain=10" in r.stdout, r.stdout + r.stderr


def _failure(**v):
    fn = _section(r"^failure\(\) \{", r"^\}\n")
    vars_ = "\n".join(f"{k}='{x}'" for k, x in v.items())
    script = f"AGENT=opencode\nagent_out() {{ :; }}\n{vars_}\n{fn}\nfailure\n"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.strip()


def test_an_agent_the_wrapper_stopped_at_the_deadline_is_reported_as_such():
    base = dict(stopped="", rc=137, elapsed=500, budget=400)
    assert _failure(**base, stopped_at_deadline=1) == "the agent was still running at the deadline and was stopped"
    assert _failure(**base, stopped_at_deadline="") == "the agent ran out of time (stopped after 400s)"
    assert "possibly out of memory" in _failure(**{**base, "elapsed": 5}, stopped_at_deadline="")


def test_the_operator_paragraph_is_in_the_instructions_only_with_an_api_token():
    import os
    import subprocess

    # The wrapper's own standing-instruction code, from the first STANDING to the MESSAGE.
    start = WRAPPER.index("if [ -n \"$READ_ONLY\" ]; then\nread -r -d '' STANDING")
    block = WRAPPER[start : WRAPPER.index("\nMESSAGE=", start)]
    base = {k: v for k, v in os.environ.items() if k != "DRELLA_API_TOKEN"}

    def run(**env):
        r = subprocess.run(
            ["bash", "-c", "OUT=/out; REPO=; READ_ONLY=; " + block + '\nprintf %s "$STANDING"'],
            capture_output=True, text=True, env={**base, **env}, check=True,
        )
        return r.stdout

    without = run()
    assert "Drella API" not in without and "DRELLA_API_TOKEN" not in without
    with_token = run(DRELLA_API_TOKEN="dra_secretsecret")
    assert "$DRELLA_API_TOKEN as a bearer token" in with_token
    assert '"profile": "builder"' in with_token
    assert "dra_secretsecret" not in with_token  # told about, never given the value
    assert with_token.startswith(without)
