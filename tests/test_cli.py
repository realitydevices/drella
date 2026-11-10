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
