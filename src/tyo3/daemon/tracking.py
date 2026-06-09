"""AffectedTracker — a thread-safe record of "which revision last affected an id".

Populated by the bus pump as each ``delta`` (and its later ``refinement``) lands,
read by the ``entity_at`` handler to answer "last affected at rev N". A tiny
piece of derived ambient state — advisory, best-effort, never load-bearing.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable


class AffectedTracker:
    """Maps DurableId → the highest revision whose affected closure included it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, int] = {}

    def record(self, revision: int, ids: Iterable[str]) -> None:
        """Record that *revision* affected every id in *ids* (monotonic)."""
        with self._lock:
            for did in ids:
                prev = self._last.get(did)
                if prev is None or revision > prev:
                    self._last[did] = revision

    def last_affected(self, durable_id: str) -> int | None:
        """The most recent revision that affected *durable_id*, or ``None``."""
        with self._lock:
            return self._last.get(durable_id)
