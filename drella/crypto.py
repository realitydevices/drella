"""Tokens and credential encryption [19].

From each token we derive two separate values:
- the lookup hash, sha256(token), which is stored and used to find the token;
- the encryption key, HKDF(token, salt, "creds"), which is never stored and
  encrypts that token's credentials with Fernet.

So only someone holding the token can decrypt its credentials.
"""

import base64
import hashlib
import secrets

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TOKEN_PREFIX = "drl_"
JOB_TOKEN_PREFIX = "drj_"


def new_token(prefix: str = TOKEN_PREFIX) -> str:
    """A random, high-entropy token (256 bits)."""
    return prefix + secrets.token_urlsafe(32)


def new_salt() -> bytes:
    return secrets.token_bytes(16)


def lookup_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _fernet(token: str, salt: bytes) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"creds").derive(
        token.encode()
    )
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(token: str, salt: bytes, plaintext: str) -> bytes:
    return _fernet(token, salt).encrypt(plaintext.encode())


def decrypt(token: str, salt: bytes, ciphertext: bytes) -> str:
    return _fernet(token, salt).decrypt(ciphertext).decode()
