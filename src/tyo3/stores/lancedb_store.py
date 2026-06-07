"""LanceDB VectorStore backend (SPEC §8.2.6).

ANN search is delegated entirely to LanceDB. TyO3 stores the hash↔vector
linkage; the backend owns the index and search.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class LanceDbStore:
    """VectorStore backed by LanceDB (lazy import)."""

    def __init__(
        self,
        uri: str,
        *,
        dim: int = 1536,
        metric: str = "cosine",
    ) -> None:
        self._uri = uri
        self._dim = dim
        self._metric = metric
        self._table: Any = None
        self._initialized = False

    def _ensure_table(self) -> None:
        if self._initialized:
            return
        try:
            import lancedb
        except ImportError as exc:
            from tyo3.exceptions import StoreBackendUnavailable
            raise StoreBackendUnavailable(
                "LanceDB backend requires optional package 'lancedb'"
            ) from exc

        db = lancedb.connect(self._uri)
        try:
            self._table = db.open_table("vectors")
        except Exception:
            import pyarrow as pa
            schema = pa.schema([
                pa.field("key", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), self._dim)),
            ])
            self._table = db.create_table("vectors", schema=schema)
        self._initialized = True

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def metric(self) -> str:
        return self._metric

    def get(self, key: str) -> bytes | None:
        """Return the vector bytes for *key*, or None."""
        import json
        self._ensure_table()
        try:
            results = self._table.search().where(f"key = '{key}'").limit(1).to_list()
            if results:
                vec = results[0]["vector"]
                return json.dumps(vec).encode("utf-8")
        except Exception:
            pass
        return None

    def put(self, key: str, artifact: bytes) -> None:
        """Insert a vector under *key*. Idempotent — overwrites if exists."""
        import json
        self._ensure_table()
        try:
            vec = json.loads(artifact.decode("utf-8"))
            # Delete any existing row with this key.
            try:
                self._table.delete(f"key = '{key}'")
            except Exception:
                pass
            import pyarrow as pa
            data = pa.table({"key": [key], "vector": [vec]})
            self._table.add(data)
        except Exception:
            pass

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def delete(self, key: str) -> None:
        self._ensure_table()
        try:
            self._table.delete(f"key = '{key}'")
        except Exception:
            pass

    def nearest(self, query: Sequence[float], k: int) -> list[tuple[str, float]]:
        """Return the k nearest neighbors to *query*.

        Returns list of ``(key, score)`` sorted by ascending distance.
        """
        self._ensure_table()
        try:
            results = (
                self._table.search(list(query))
                .metric(self._metric)
                .limit(k)
                .to_list()
            )
            return [(r["key"], r.get("_distance", 0.0)) for r in results]
        except Exception:
            return []
