"""Filesystem artifact store backend."""

from __future__ import annotations

from pathlib import Path
import hashlib
import os


class FsStore:
    """Content-hash-keyed file store rooted at a cache directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.exists():
            return None
        return path.read_bytes()

    def put(self, key: str, artifact: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as fh:
            fh.write(artifact)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)

    def has(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / digest[:2] / digest[2:4] / digest


__all__ = ["FsStore"]
