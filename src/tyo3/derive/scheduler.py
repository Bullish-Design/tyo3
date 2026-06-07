"""Recompute scheduler — idempotent, topo-ordered, fault-isolating (§9.2.5/§9.2.6).

Work item = ``(layer, durable_id, input_hash)``. Idempotency: the same
``(layer, input_hash)`` computes at most once. Topological gating:
layer-derived items wait for their upstream. Failure leaves the prior
artifact intact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tyo3.exceptions import GeneratorFailed

if TYPE_CHECKING:
    from tyo3.derive.dag import DerivationDAG
    from tyo3.derive.layer import DerivedLayer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkItem:
    """One item to recompute."""

    layer_name: str
    durable_id: str
    input_hash: str


class RecomputeScheduler:
    """Idempotent, bounded-concurrency recompute worker.

    Scheduled items keyed by ``(layer_name, input_hash)`` are computed at most
    once. A failure leaves the prior artifact intact and marks the target
    ``failed`` — partial artifacts are never written.
    """

    def __init__(self) -> None:
        # (layer_name, input_hash) → whether already computed or in-flight.
        self._done: set[tuple[str, str]] = set()
        # Eager queue: items to process.
        self._pending: list[WorkItem] = []
        # Lazy items: only recompute on first read.
        self._lazy: dict[tuple[str, str], WorkItem] = {}

    def enqueue(
        self,
        layer: "DerivedLayer",
        durable_id: str,
        input_hash: str,
        *,
        lazy: bool = False,
    ) -> None:
        """Enqueue a work item, deduplicating by (layer, input_hash)."""
        key = (layer.name, input_hash)
        if key in self._done:
            return
        item = WorkItem(layer.name, durable_id, input_hash)
        if lazy:
            self._lazy[key] = item
        else:
            if key not in {(w.layer_name, w.input_hash) for w in self._pending}:
                self._pending.append(item)

    def process_all(self, dag: "DerivationDAG", snapshot) -> None:
        """Process all pending eager items in topological order.

        Simple single-threaded implementation. The DAG's topological order
        ensures upstream layers compute before downstream.
        """
        if not self._pending:
            return

        for layer in dag.iter_layers():
            # Collect items for this layer.
            layer_items = [
                item for item in self._pending
                if item.layer_name == layer.name
            ]
            if not layer_items:
                continue

            for item in layer_items:
                key = (layer.name, item.input_hash)
                if key in self._done:
                    continue
                try:
                    gen_input, _ = dag.resolve_input(layer, snapshot, item.durable_id)
                    artifacts = layer.generator.generate([gen_input])
                    if artifacts:
                        cache_key = layer.keys_for(item.input_hash)
                        layer.cache.put(cache_key, artifacts[0])
                        layer.bind(
                            item.durable_id,
                            item.input_hash,
                            cache_key.to_store_key(),
                        )
                        layer.mark_clear(item.durable_id)
                except GeneratorFailed:
                    layer.mark_failed(item.durable_id)
                    logger.warning(
                        "Recompute failed for layer=%s id=%s — prior artifact intact",
                        layer.name,
                        item.durable_id,
                    )
                except Exception:
                    layer.mark_failed(item.durable_id)
                    logger.warning(
                        "Recompute failed for layer=%s id=%s — prior artifact intact",
                        layer.name,
                        item.durable_id,
                        exc_info=True,
                    )
                self._done.add(key)

        self._pending.clear()

    def recompute_now(
        self, dag: "DerivationDAG", layer: "DerivedLayer", snapshot, durable_id: str
    ) -> bytes | None:
        """Synchronous recompute for blocking reads (§8.2.5)."""
        from tyo3.exceptions import GeneratorFailed

        gen_input, input_hash = dag.resolve_input(layer, snapshot, durable_id)
        key = (layer.name, input_hash)

        try:
            artifacts = layer.generator.generate([gen_input])
            if artifacts:
                cache_key = layer.keys_for(input_hash)
                layer.cache.put(cache_key, artifacts[0])
                layer.bind(durable_id, input_hash, cache_key.to_store_key())
                layer.mark_clear(durable_id)
                self._done.add(key)
                return artifacts[0]
        except GeneratorFailed:
            layer.mark_failed(durable_id)
        except Exception:
            layer.mark_failed(durable_id)
            logger.warning(
                "Blocking recompute failed for layer=%s id=%s",
                layer.name, durable_id, exc_info=True,
            )

        return None
