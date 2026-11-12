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


def test_password_file_is_read_and_line_ending_dropped(monkeypatch, tmp_path):
    set_parts(monkeypatch)
    f = tmp_path / "pw"
    f.write_bytes(f"{PASSWORD}\r\n".encode())
    monkeypatch.setenv("DRELLA_DATABASE_PASSWORD_FILE", str(f))
    assert make_url(Settings().database_url).password == PASSWORD


def test_password_env_trailing_newline_dropped(monkeypatch):
    set_parts(monkeypatch)
    monkeypatch.setenv("DRELLA_DATABASE_PASSWORD", f"{PASSWORD}\n")
    assert make_url(Settings().database_url).password == PASSWORD


@pytest.mark.parametrize("source", ["env", "file"])
def test_password_inner_and_outer_spaces_kept(monkeypatch, tmp_path, source):
    set_parts(monkeypatch)
    if source == "env":
        monkeypatch.setenv("DRELLA_DATABASE_PASSWORD", " a b ")
    else:
        f = tmp_path / "pw"
        f.write_text(" a b \n")
        monkeypatch.setenv("DRELLA_DATABASE_PASSWORD_FILE", str(f))
    assert make_url(Settings().database_url).password == " a b "


@pytest.mark.parametrize("content", ["", "\n", "  \t\n"])
def test_empty_password_file_is_an_error(monkeypatch, tmp_path, content):
    set_parts(monkeypatch)
    f = tmp_path / "pw"
    f.write_text(content)
    monkeypatch.setenv("DRELLA_DATABASE_PASSWORD_FILE", str(f))
    with pytest.raises(ValueError, match="DRELLA_DATABASE_PASSWORD_FILE.*empty"):
        Settings()


def test_missing_password_file_is_an_error(monkeypatch, tmp_path):
    set_parts(monkeypatch)
    monkeypatch.setenv("DRELLA_DATABASE_PASSWORD_FILE", str(tmp_path / "nope"))
    with pytest.raises(ValueError, match="DRELLA_DATABASE_PASSWORD_FILE"):
        Settings()


def test_user_and_database_with_url_characters_round_trip(monkeypatch):
    set_parts(monkeypatch)
    monkeypatch.setenv("DRELLA_DATABASE_USER", "u@x:y/z")
    monkeypatch.setenv("DRELLA_DATABASE_NAME", "d?b#x")
    monkeypatch.setenv("DRELLA_DATABASE_PASSWORD", PASSWORD)
    url = make_url(Settings().database_url)
    assert (url.username, url.database, url.password) == ("u@x:y/z", "d?b#x", PASSWORD)


@pytest.mark.parametrize("name", ["NAME", "USER"])
def test_name_and_user_required_with_host(monkeypatch, name):
    set_parts(monkeypatch)
    monkeypatch.delenv(f"DRELLA_DATABASE_{name}")
    with pytest.raises(ValueError, match=f"DRELLA_DATABASE_{name} is required"):
        Settings()


def test_non_integer_port_is_an_error(monkeypatch):
    set_parts(monkeypatch)
    monkeypatch.setenv("DRELLA_DATABASE_PORT", "54x")
    with pytest.raises(ValueError, match="DRELLA_DATABASE_PORT must be an integer"):
        Settings()


@pytest.mark.parametrize("port", ["0", "-1", "65536"])
def test_port_out_of_range_is_an_error(monkeypatch, port):
    set_parts(monkeypatch)
    monkeypatch.setenv("DRELLA_DATABASE_PORT", port)
    with pytest.raises(ValueError, match="between 1 and 65535"):
        Settings()


def test_port_bounds_accepted(monkeypatch):
    set_parts(monkeypatch)
    for port in (1, 65535):
        monkeypatch.setenv("DRELLA_DATABASE_PORT", str(port))
        assert make_url(Settings().database_url).port == port


def test_socket_path_host_is_an_error(monkeypatch):
    set_parts(monkeypatch)
    monkeypatch.setenv("DRELLA_DATABASE_HOST", "/var/run/postgresql")
    with pytest.raises(ValueError, match="Unix socket"):
        Settings()


@pytest.mark.parametrize("host", [None, "", " "])
@pytest.mark.parametrize("setting", ["PORT", "NAME", "USER", "PASSWORD", "PASSWORD_FILE"])
def test_settings_without_host_are_an_error(monkeypatch, host, setting):
    if host is not None:
        monkeypatch.setenv("DRELLA_DATABASE_HOST", host)
    monkeypatch.setenv(f"DRELLA_DATABASE_{setting}", "x")
    with pytest.raises(ValueError, match="DRELLA_DATABASE_HOST"):
        Settings()


def test_repr_hides_password(monkeypatch):
    set_parts(monkeypatch)
    monkeypatch.setenv("DRELLA_DATABASE_PASSWORD", PASSWORD)
    settings = Settings()
    # The URL holds the password percent-escaped.
    escaped = settings.database_url.split("://drella:", 1)[1].split("@postgres", 1)[0]
    assert make_url(settings.database_url).password == PASSWORD
    assert PASSWORD not in repr(settings)
    assert escaped not in repr(settings)
    assert "database_url" not in repr(settings)


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
