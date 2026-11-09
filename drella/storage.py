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

    def size(self, key: str) -> int | None:
        """The object's size in bytes, or None if it doesn't exist."""


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
        if ".." in PurePosixPath(prefix).parts or prefix.startswith("/"):
            raise ValueError(f"invalid storage prefix: {prefix!r}")
        # Walk only the deepest directory the prefix names, not the whole tree.
        base = self.root / prefix.rsplit("/", 1)[0] if "/" in prefix else self.root
        if not base.is_dir():
            return []
        keys = []
        for path in base.rglob("*"):
            if path.is_file() and not path.name.startswith(".tmp-"):
                key = path.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    keys.append(key)
        return sorted(keys)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def size(self, key: str) -> int | None:
        p = self._path(key)
        return p.stat().st_size if p.is_file() else None
