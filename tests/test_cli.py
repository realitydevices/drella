import pytest

from drella import crypto
from drella.cli import main
from drella.db import make_engine, make_sessionmaker
from drella.models import Token


def test_token_create_prints_token_once_and_stores_only_its_hash(monkeypatch, tmp_path, capsys):
    url = f"sqlite:///{tmp_path}/cli.db"
    monkeypatch.setenv("DRELLA_DATABASE_URL", url)
    main(["token", "create", "--name", "alice"])
    token = capsys.readouterr().out.strip()
    assert token.startswith("drl_")
    with make_sessionmaker(make_engine(url))() as s:
        (row,) = s.query(Token).all()
    assert row.name == "alice" and row.lookup_hash == crypto.lookup_hash(token)


def _rows(url):
    with make_sessionmaker(make_engine(url))() as s:
        return [(t.name, t.github_profile) for t in s.query(Token).order_by(Token.id)]


def test_a_new_token_is_a_reader_unless_the_admin_says_otherwise(monkeypatch, tmp_path, capsys):
    url = f"sqlite:///{tmp_path}/cli.db"
    monkeypatch.setenv("DRELLA_DATABASE_URL", url)
    main(["token", "create", "--name", "rita"])
    main(["token", "create", "--name", "drella-bot", "--github-profile", "builder"])
    assert _rows(url) == [("rita", "reader"), ("drella-bot", "builder")]
    assert "GitHub profile: builder" in capsys.readouterr().err


def test_an_unknown_profile_or_token_is_refused(monkeypatch, tmp_path, capsys):
    url = f"sqlite:///{tmp_path}/cli.db"
    monkeypatch.setenv("DRELLA_DATABASE_URL", url)
    with pytest.raises(SystemExit):
        main(["token", "create", "--name", "x", "--github-profile", "admin"])
    main(["token", "create", "--name", "rita"])
    with pytest.raises(SystemExit):
        main(["token", "set-profile", "1", "admin"])
    with pytest.raises(SystemExit):
        main(["token", "set-profile", "99", "builder"])  # no such token
    assert _rows(url) == [("rita", "reader")]


def test_token_list_and_set_profile(monkeypatch, tmp_path, capsys):
    url = f"sqlite:///{tmp_path}/cli.db"
    monkeypatch.setenv("DRELLA_DATABASE_URL", url)
    main(["token", "create", "--name", "rita"])
    token = capsys.readouterr().out.strip()
    main(["token", "set-profile", "1", "builder"])
    main(["token", "list"])
    out = capsys.readouterr().out
    assert out == "1\tbuilder\tmember\trita\n"
    assert token not in out


def test_token_roles_are_set_only_by_the_cli(monkeypatch, tmp_path, capsys):
    url = f"sqlite:///{tmp_path}/cli.db"
    monkeypatch.setenv("DRELLA_DATABASE_URL", url)
    main(["token", "create", "--name", "rita"])
    main(["token", "create", "--name", "superstar", "--github-profile", "superstar",
          "--role", "operator"])
    assert "Role: operator" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["token", "create", "--name", "x", "--role", "admin"])
    with pytest.raises(SystemExit):
        main(["token", "set-role", "1", "admin"])
    with pytest.raises(SystemExit):
        main(["token", "set-role", "99", "operator"])  # no such token
    main(["token", "set-role", "1", "operator"])
    main(["token", "set-role", "2", "member"])
    capsys.readouterr()
    main(["token", "list"])
    assert capsys.readouterr().out == "1\treader\toperator\trita\n2\tsuperstar\tmember\tsuperstar\n"


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "", "1.5"])
def test_the_child_job_limit_must_be_a_positive_integer(monkeypatch, raw):
    from drella.config import Settings

    monkeypatch.setenv("DRELLA_OPERATOR_MAX_CHILD_JOBS", raw)
    with pytest.raises(ValueError, match="DRELLA_OPERATOR_MAX_CHILD_JOBS"):
        Settings()
    monkeypatch.setenv("DRELLA_OPERATOR_MAX_CHILD_JOBS", "3")
    assert Settings().operator_max_child_jobs == 3
