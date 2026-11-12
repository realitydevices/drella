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
    for name in ("DRELLA_LLM_KEY", "GH_TOKEN", "DRELLA_JOB_TOKEN"):
        assert f'ENVIRON["{name}"]' in WRAPPER


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
    assert 'if [ -n "$REPO" ] && [ -z "$READ_ONLY" ]; then' in WRAPPER
    assert 'elif [ -z "$REPO" ] || [ -n "$READ_ONLY" ]; then' in WRAPPER


def test_the_wrapper_never_asks_drella_for_another_github_token():
    # The token minted at dispatch is the job's only one: its deadline is capped at the
    # token's expiry instead (test_runner.py).
    assert "github-token" not in WRAPPER


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

import os
import re
import time


def _wait_logic():
    m = re.search(r"^DRAIN_GRACE=.*?^wait_for_agent\(\) \{.*?^\}\n", WRAPPER, re.S | re.M)
    assert m, "wait_for_agent not found"
    return m.group(0)


def _run_wait(tmp_path, agent_body, grace=2, finish_in=None, reserve=2, real_timeout=None):
    """Run the wrapper's real pipeline shape and wait_for_agent with a fake agent."""
    (tmp_path / "out").mkdir()
    if real_timeout:
        agent_cmd = f"setsid timeout --signal=TERM --kill-after=30 {real_timeout} bash -c '{agent_body}'"
    else:
        agent_cmd = f"setsid bash -c '{agent_body}'"
    script = f"""
set -uo pipefail
WORK={tmp_path}; OUT={tmp_path}/out
RESERVE={reserve}
FINISH_BY=$(($(date +%s) + {finish_in if finish_in is not None else 1000}))
left() {{ echo $((FINISH_BY - $(date +%s))); }}
log() {{ echo "[drella] $*"; }}
redact() {{ cat; }}; render() {{ cat; }}
DRELLA_DRAIN_GRACE={grace}
{_wait_logic()}
{{
  {agent_cmd} &
  echo $! >"$WORK/agent.pid"
  wait $!
  echo $? >"$WORK/agent.rc"
}} 2>&1 | redact | tee "$WORK/agent.out" | render | tee "$OUT/transcript.txt" &
pipe_pid=$!
for _ in $(seq 100); do [ -s "$WORK/agent.pid" ] && break; sleep 0.1; done
agent_pid=$(cat "$WORK/agent.pid")
wait_for_agent
echo "rc=$(cat "$WORK/agent.rc" 2>/dev/null) left=$(left)"
"""
    t = time.monotonic()
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    return r, time.monotonic() - t


def test_wait_returns_when_the_agent_left_a_process_holding_stdout(tmp_path):
    r, took = _run_wait(tmp_path, "sleep 300 & echo hi; exit 0")
    assert "rc=0" in r.stdout, r.stdout + r.stderr
    assert took < 15
    assert "stopped the agent's leftover processes" in (tmp_path / "out/transcript.txt").read_text()
    assert "hi" in (tmp_path / "out/transcript.txt").read_text()
    time.sleep(0.5)
    left = [p for p in Path("/proc").glob("[0-9]*/cmdline") if b"sleep\x00300" in _read(p)]
    assert left == []


def _read(p):
    try:
        return p.read_bytes()
    except OSError:
        return b""


def test_wait_is_unchanged_when_nothing_is_left_running(tmp_path):
    r, took = _run_wait(tmp_path, "echo hi; exit 3")
    assert "rc=3" in r.stdout
    assert took < 5
    assert "left processes" not in (tmp_path / "out/transcript.txt").read_text()


def _pids(pattern):
    return [p for p in Path("/proc").glob("[0-9]*/cmdline") if pattern in _read(p)]


def test_wait_respects_the_deadline_and_leaves_the_reserve(tmp_path):
    r, took = _run_wait(tmp_path, "sleep 61", grace=10, finish_in=6, reserve=3)
    m = re.search(r"left=(-?\d+)", r.stdout)
    assert m and int(m.group(1)) >= 3, r.stdout + r.stderr
    assert took < 10
    assert "the agent was still running at the deadline; stopped it" in (tmp_path / "out/transcript.txt").read_text()
    time.sleep(0.5)
    assert _pids(b"sleep\x0061") == []


def test_wait_does_not_claim_success_when_a_process_escaped(tmp_path):
    try:
        r, _ = _run_wait(tmp_path, "setsid sleep 30 & echo hi; exit 0")
        text = (tmp_path / "out/transcript.txt").read_text()
        assert "may still be running" in text, r.stdout + r.stderr
        assert "stopped the agent's leftover processes" not in text
    finally:
        for p in _pids(b"sleep\x0030"):
            os.kill(int(p.parent.name), 9)


def test_wait_with_the_real_timeout_command_line(tmp_path):
    r, took = _run_wait(tmp_path, "sleep 62 & echo hi; exit 0", real_timeout=5)
    assert "rc=0" in r.stdout, r.stdout + r.stderr
    assert took < 20
    time.sleep(0.5)
    assert _pids(b"sleep\x0062") == []
