"""Tokens and credential encryption [19].

From each token we derive two separate values:
- the lookup hash, sha256(token), which is stored and used to find the token;
- the encryption key, HKDF(token, salt, "creds"), which is never stored and
  encrypts that token's credentials with Fernet.

So only someone holding the token can decrypt its credentials.

An operator token's job (issue #74) gets its own API token, never the caller's. To let
that job create jobs with the caller's credentials, the caller's credential key (the
HKDF output above) is wrapped with a key derived from the API token, HKDF(api token,
"operator-wrap"), and stored on the job until it finishes. So only someone holding the
API token, while the job runs, can unwrap it; it can't be used to recover the caller's
token or to sign in as the caller anywhere else.
"""

import base64
import hashlib
import secrets

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TOKEN_PREFIX = "drl_"
JOB_TOKEN_PREFIX = "drj_"
API_TOKEN_PREFIX = "dra_"


def new_token(prefix: str = TOKEN_PREFIX) -> str:
    """A random, high-entropy token (256 bits)."""
    return prefix + secrets.token_urlsafe(32)


def new_salt() -> bytes:
    return secrets.token_bytes(16)


def lookup_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def credential_key(token: str, salt: bytes) -> bytes:
    """The Fernet key a token's credentials are encrypted with. Never stored as is."""
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"creds").derive(
        token.encode()
    )
    return base64.urlsafe_b64encode(key)


def _fernet(token: str, salt: bytes) -> Fernet:
    return Fernet(credential_key(token, salt))


def encrypt(token: str, salt: bytes, plaintext: str) -> bytes:
    return _fernet(token, salt).encrypt(plaintext.encode())


def decrypt(token: str, salt: bytes, ciphertext: bytes) -> str:
    return _fernet(token, salt).decrypt(ciphertext).decode()


def decrypt_with_key(key: bytes, ciphertext: bytes) -> str:
    """Decrypt a credential with a credential key (credential_key, or one unwrapped)."""
    return Fernet(key).decrypt(ciphertext).decode()


def _wrap_fernet(api_token: str) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"operator-wrap").derive(
        api_token.encode()
    )
    return Fernet(base64.urlsafe_b64encode(key))


def wrap_key(api_token: str, key: bytes) -> bytes:
    """A credential key, encrypted so only the holder of api_token can recover it."""
    return _wrap_fernet(api_token).encrypt(key)


def unwrap_key(api_token: str, wrapped: bytes) -> bytes:
    return _wrap_fernet(api_token).decrypt(wrapped)
