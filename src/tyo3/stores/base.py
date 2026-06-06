"""Artifact store protocols."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class Store(Protocol):
    """Content-hash-keyed artifact store. Keys are (content_hash, generator_version)."""

    def get(self, key: str) -> bytes | None: ...

    def put(self, key: str, artifact: bytes) -> None: ...

    def has(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


class VectorStore(Store, Protocol):
    """Adds nearest-neighbour search; ANN is delegated to the backend."""

    dim: int
    metric: str

    def nearest(self, query: Sequence[float], k: int) -> list[tuple[str, float]]: ...


__all__ = ["Store", "VectorStore"]
