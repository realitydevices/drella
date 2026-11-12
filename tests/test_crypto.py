import pytest
from cryptography.fernet import InvalidToken
from sqlalchemy import create_engine, text

from drella import crypto


def test_tokens_are_random_and_prefixed():
    a, b = crypto.new_token(), crypto.new_token()
    assert a != b
    assert a.startswith("drl_") and len(a) > 40
    assert crypto.new_token(crypto.JOB_TOKEN_PREFIX).startswith("drj_")


def test_lookup_hash_is_sha256():
    assert crypto.lookup_hash("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_encrypt_round_trip():
    token, salt = crypto.new_token(), crypto.new_salt()
    ct = crypto.encrypt(token, salt, "ghp_secret")
    assert b"ghp_secret" not in ct
    assert crypto.decrypt(token, salt, ct) == "ghp_secret"


def test_wrong_token_or_salt_cannot_decrypt():
    token, salt = crypto.new_token(), crypto.new_salt()
    ct = crypto.encrypt(token, salt, "ghp_secret")
    with pytest.raises(InvalidToken):
        crypto.decrypt(crypto.new_token(), salt, ct)
    with pytest.raises(InvalidToken):
        crypto.decrypt(token, crypto.new_salt(), ct)


def test_database_holds_neither_token_nor_plaintext(client, settings, token, auth):
    client.put("/credentials/github", json={"value": "ghp_secret"}, headers=auth)
    engine = create_engine(settings.database_url.replace("postgresql://", "postgresql+psycopg://"))
    with engine.connect() as conn:
        dump = repr(conn.execute(text("SELECT * FROM tokens")).all())
        dump += repr(conn.execute(text("SELECT * FROM credentials")).all())
    assert token not in dump
    assert "ghp_secret" not in dump
    assert crypto.lookup_hash(token) in dump


def test_a_wrapped_credential_key_opens_only_with_its_api_token():
    token, salt = crypto.new_token(), crypto.new_salt()
    ciphertext = crypto.encrypt(token, salt, "ghp_secret")
    api = crypto.new_token(crypto.API_TOKEN_PREFIX)
    wrapped = crypto.wrap_key(api, crypto.credential_key(token, salt))
    assert crypto.decrypt_with_key(crypto.unwrap_key(api, wrapped), ciphertext) == "ghp_secret"
    with pytest.raises(Exception):
        crypto.unwrap_key(crypto.new_token(crypto.API_TOKEN_PREFIX), wrapped)
