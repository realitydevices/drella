import pytest
from sqlalchemy.engine import make_url

from drella.config import Settings

DB_VARS = [
    "DRELLA_DATABASE_URL",
    "DRELLA_DATABASE_HOST",
    "DRELLA_DATABASE_PORT",
    "DRELLA_DATABASE_NAME",
    "DRELLA_DATABASE_USER",
    "DRELLA_DATABASE_PASSWORD",
    "DRELLA_DATABASE_PASSWORD_FILE",
]
PASSWORD = "a@b/c:d#e%f"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in DB_VARS:
        monkeypatch.delenv(v, raising=False)


def set_parts(monkeypatch):
    monkeypatch.setenv("DRELLA_DATABASE_HOST", "postgres")
    monkeypatch.setenv("DRELLA_DATABASE_NAME", "drella")
    monkeypatch.setenv("DRELLA_DATABASE_USER", "drella")


def test_password_with_url_characters_round_trips(monkeypatch):
    set_parts(monkeypatch)
    monkeypatch.setenv("DRELLA_DATABASE_PASSWORD", PASSWORD)
    url = make_url(Settings().database_url)
    assert url.drivername == "postgresql+psycopg"
    assert url.password == PASSWORD
    assert (url.host, url.port, url.database, url.username) == ("postgres", 5432, "drella", "drella")


def test_port_setting(monkeypatch):
    set_parts(monkeypatch)
    monkeypatch.setenv("DRELLA_DATABASE_PORT", "6543")
    assert make_url(Settings().database_url).port == 6543


def test_password_file_is_read_and_stripped(monkeypatch, tmp_path):
    set_parts(monkeypatch)
    f = tmp_path / "pw"
    f.write_text(f"  {PASSWORD}\n")
    monkeypatch.setenv("DRELLA_DATABASE_PASSWORD_FILE", str(f))
    assert make_url(Settings().database_url).password == PASSWORD


def test_url_and_host_together_is_an_error(monkeypatch):
    set_parts(monkeypatch)
    monkeypatch.setenv("DRELLA_DATABASE_URL", "sqlite://")
    with pytest.raises(ValueError, match="DRELLA_DATABASE_URL and DRELLA_DATABASE_HOST"):
        Settings()


def test_password_and_file_together_is_an_error(monkeypatch, tmp_path):
    set_parts(monkeypatch)
    monkeypatch.setenv("DRELLA_DATABASE_PASSWORD", "x")
    monkeypatch.setenv("DRELLA_DATABASE_PASSWORD_FILE", str(tmp_path / "pw"))
    with pytest.raises(ValueError, match="PASSWORD_FILE"):
        Settings()


def test_url_only_path_unchanged(monkeypatch):
    assert Settings().database_url == "sqlite:///drella.db"
    monkeypatch.setenv("DRELLA_DATABASE_URL", "postgresql://u:p@h:5432/d")
    assert Settings().database_url == "postgresql://u:p@h:5432/d"
