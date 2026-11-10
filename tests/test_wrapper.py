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


def test_wrapper_turns_on_auto_merge_on_success():
    assert 'gh pr merge "$pr" --auto --squash' in WRAPPER


def test_a_refreshed_github_token_is_checked_before_use():
    # It ends up in git's credential helper output: one line of plain characters.
    assert "[[ ! $fresh =~ ^[A-Za-z0-9_.-]{8,}$ ]]" in WRAPPER


def test_the_replaced_github_token_stays_redacted():
    assert "export DRELLA_GH_TOKEN_OLD=$GH_TOKEN" in WRAPPER
    assert 'k[++n] = ENVIRON["DRELLA_GH_TOKEN_OLD"]' in WRAPPER
    assert 'k[++n] = ENVIRON["GH_TOKEN"]' in WRAPPER


def test_the_github_token_is_refreshed_only_for_app_token_jobs_and_bounded():
    assert '[ "${DRELLA_GITHUB_REFRESH:-}" = 1 ]' in WRAPPER
    assert "within 30 curl -fsS --retry 3" in WRAPPER
    assert '"$DRELLA_API_URL/jobs/$DRELLA_JOB_ID/github-token"' in WRAPPER
