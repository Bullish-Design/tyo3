"""Filesystem artifact store backend.

On-disk layout is *structural content-addressing*: an artifact for store key
``<input_hash>:<generator_version>`` lives at
``root/<version_enc>/<input_hash[:2]>/<input_hash>``. The path *is* the key, so
the key space is enumerable (``iter_keys``) and GC needs no reverse lookup. Each
``generator_version`` is its own subtree (version isolation / rollback).

(The format changed from the prior opaque ``sha256(key)`` sharding; old caches
are abandoned, not migrated — derived artifacts are regenerable.)
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote, unquote

from tyo3.exceptions import StoreBackendBroken


class FsStore:
    """Content-hash-keyed file store rooted at a cache directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            # A genuinely absent artifact — the one condition that is *not* an
            # error (§5.12). Every other IO failure propagates typed below.
            return None
        except OSError as exc:
            raise StoreBackendBroken(f"FsStore read failed for key {key!r}: {exc}") from exc

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

    def prune(self, reachable_keys: set[str]) -> int:
        """Delete unreachable artifacts in the supplied version spaces.

        An empty set is deliberately a no-op: the caller has not identified a
        safe generator-version space to collect. Other versions are retained
        for rollback, so the backend owns the version filtering rather than
        requiring the cache or DAG to understand this layout.
        """
        if not reachable_keys:
            return 0

        versions = {key.split(":", 1)[1] for key in reachable_keys if ":" in key}
        if not versions:
            return 0

        deleted = 0
        for key in list(self.iter_keys()):
            input_hash, _, version = key.partition(":")
            if version not in versions or key in reachable_keys:
                continue
            try:
                self._path(f"{input_hash}:{version}").unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise StoreBackendBroken(f"FsStore prune failed for key {key!r}: {exc}") from exc
            deleted += 1

        # Remove empty shard/version directories, but never the cache root.
        if self.root.exists():
            for directory in sorted(
                (path for path in self.root.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        return deleted

    def _path(self, key: str) -> Path:
        input_hash, _, version = key.partition(":")
        vdir = quote(version, safe="")
        shard = input_hash[:2] if len(input_hash) >= 2 else "_"
        return self.root / vdir / shard / input_hash

    def iter_keys(self) -> Iterator[str]:
        """Yield every store key held, reconstructed from the path layout
        (``root/<version_enc>/<shard>/<input_hash>``)."""
        if not self.root.exists():
            return
        for vdir in self.root.iterdir():
            if not vdir.is_dir():
                continue
            version = unquote(vdir.name)
            for shard in vdir.iterdir():
                if not shard.is_dir():
                    continue
                for f in shard.iterdir():
                    # In-flight `put` writes a sibling `.tmp` in the same shard.
                    if f.is_file() and f.suffix != ".tmp":
                        yield f"{f.name}:{version}"


__all__ = ["FsStore"]
