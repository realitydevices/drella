"""Object storage [13]: put / get / list / delete, streamed.

Job pods never talk to storage directly; they go through the API, so the backend
can be swapped. v0 ships the local-filesystem implementation (S3 comes later).
"""

import os
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import Protocol

CHUNK = 64 * 1024


class NotFound(Exception):
    pass


class Storage(Protocol):
    def put(self, key: str, chunks: Iterable[bytes]) -> int:
        """Write an object from a stream of chunks. Returns the size in bytes."""

    def get(self, key: str) -> Iterator[bytes]:
        """Stream an object. Raises NotFound before yielding if it doesn't exist."""

    def list(self, prefix: str = "") -> list[str]:
        """Keys that start with `prefix`, sorted."""

    def delete(self, key: str) -> None:
        """Remove an object. Missing objects are ignored."""

    def exists(self, key: str) -> bool: ...


def _check_key(key: str) -> str:
    p = PurePosixPath(key)
    if not key or p.is_absolute() or ".." in p.parts or key.endswith("/"):
        raise ValueError(f"invalid storage key: {key!r}")
    return key


class LocalStorage:
    """Stores each object as a file under `root`, keyed by relative path."""

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / _check_key(key)

    def put(self, key: str, chunks: Iterable[bytes]) -> int:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        # Write to a temp file and rename, so readers never see a partial object.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in chunks:
                    f.write(chunk)
                    size += len(chunk)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
        return size

    def get(self, key: str) -> Iterator[bytes]:
        path = self._path(key)
        try:
            f = open(path, "rb")
        except FileNotFoundError:
            raise NotFound(key) from None
        return self._read(f)

    @staticmethod
    def _read(f) -> Iterator[bytes]:
        with f:
            while chunk := f.read(CHUNK):
                yield chunk

    def list(self, prefix: str = "") -> list[str]:
        keys = []
        for path in self.root.rglob("*"):
            if path.is_file() and not path.name.startswith(".tmp-"):
                key = path.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    keys.append(key)
        return sorted(keys)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()
