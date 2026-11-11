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


def test_opencode_export_writes_to_file_before_redact():
    # Piping opencode export straight into redact truncates at ~64 KiB because the
    # process exits before stdout is drained when stdout is a pipe. The fix writes to
    # a temp file in $WORK first, then redacts it (issue #55).
    assert 'opencode export "$session" 2>/dev/null | redact' not in WRAPPER
    assert 'session.raw.json' not in WRAPPER
    assert 'mktemp' in WRAPPER
    # mktemp must appear in the session export block (within 10 lines before "opencode export")
    lines = WRAPPER.split('\n')
    for i, line in enumerate(lines):
        if 'opencode export' in line:
            block = '\n'.join(lines[max(0, i - 10):i])
            assert 'mktemp' in block, "mktemp must appear within 10 lines before opencode export"
