"""DerivedRecomputeWorker — off-actor background produce for slow layers (AB3).

A ``serving="stale"`` derived layer with a slow producer (LLM/HTTP/embedding)
must not stall the cursor path: the daemon serves every read on the single actor
thread, so a multi-second produce would queue every other read behind it. AB3
moves that produce **off the actor**. The read seam serves last-good (``stale``)
or ``absent`` immediately and hands the recompute to this worker; the worker
produces over a **frozen snapshot pinned at the read's revision**, warms the
content-addressed cache, and (if anyone is subscribed) publishes a
``DerivedFresh`` message so the editor re-pulls.

Threading shape mirrors :class:`tyo3.precision.refiner.PrecisionRefiner`: one
daemon :class:`threading.Thread` draining a :class:`queue.Queue`, idempotent
lazy ``start`` / drain-and-join ``stop``, an ``enqueue`` fed from the read seam.

Safety model (mirrors the refiner's graceful-degradation contract)
-----------------------------------------------------------------
* **Reader, never a writer.** The worker recomputes over a frozen pinned
  snapshot and only ever ``cache.put``s an *additive*, content-addressed key +
  updates a per-entity binding dict (a single GIL-atomic assignment). It never
  writes committed truth — the actor stays the one writer (golden rule #3).
* **A failed recompute is never a miss.** The coarse last-good (``stale`` /
  ``absent``) was already served synchronously on the read thread. If the
  revision was evicted, the producer raises, or the worker never runs, that
  last-good stands. Every failure path is swallowed and the worker survives.
* **Dedup per ``(layer, id, revision)``.** Repeated reads of the same missing
  value before the first produce completes enqueue once. A new commit (new
  revision) is a new work item even for the same id.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel pushed onto the queue to stop the daemon worker.
_STOP = object()


class DerivedRecomputeWorker:
    """Background worker that produces slow derived artifacts off the actor.

    Fed ``(layer_name, durable_id, revision)`` from the ``serving="stale"`` read
    seam (:meth:`tyo3.session.views.Snapshot.derived`). A daemon thread drains
    the queue and recomputes through the **existing** reentrant DAG seam
    (``derived_traced`` for a traced layer, ``recompute_now`` for an override
    layer), then publishes a ``DerivedFresh`` message if the bus has subscribers.
    """

    def __init__(self, session: Any) -> None:
        self._session = session
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = False
        # In-flight dedup keys, guarded by its own lock (the worker thread clears
        # an entry once produced; the enqueueing thread adds it). A set membership
        # test + add under the lock keeps repeated reads from stampeding the
        # producer before the first completes.
        self._inflight: set[tuple[str, str, int]] = set()
        self._inflight_lock = threading.Lock()

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Start the daemon worker (idempotent, lazy)."""
        if self._started:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="tyo3-derived-recompute")
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        """Stop the daemon worker, draining and joining (idempotent)."""
        if not self._started:
            return
        self._queue.put(_STOP)
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        self._started = False

    # ── Feed (called from the read seam, off the actor's produce) ──

    def enqueue(self, layer_name: str, durable_id: str, revision: int) -> None:
        """Enqueue a background recompute, deduplicating per ``(layer, id, rev)``.

        Lazily starts the worker on the first enqueue (mirrors
        ``session._get_refiner``). A repeat enqueue of an in-flight key is a
        no-op so a burst of reads of the same missing value produces once."""
        key = (layer_name, durable_id, revision)
        with self._inflight_lock:
            if key in self._inflight:
                return
            self._inflight.add(key)
        self.start()
        self._queue.put(key)

    # ── Daemon loop ────────────────────────────────────────────────

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            layer_name, durable_id, revision = item
            try:
                self._recompute_one(layer_name, durable_id, revision)
            except Exception:
                # Graceful degradation: a failed recompute is never a miss — the
                # last-good (stale/absent) was already served on the read thread.
                # Swallow and keep the worker alive for the next item.
                logger.debug(
                    "async recompute failed for layer=%s id=%s rev=%s",
                    layer_name,
                    durable_id,
                    revision,
                    exc_info=True,
                )
            finally:
                with self._inflight_lock:
                    self._inflight.discard(item)

    # ── Core: recompute (off the actor, over a pinned snapshot) ────

    def _recompute_one(self, layer_name: str, durable_id: str, revision: int) -> None:
        session = self._session
        # Pin the read's revision. A failure (revision evicted) skips gracefully —
        # the coarse last-good already served stands (mirror the refiner's
        # snapshot-pin-or-None contract).
        snap = None
        try:
            snap = session.snapshot(at=revision)
        except Exception:
            logger.debug("async recompute: snapshot(at=%s) unavailable; last-good stands", revision)
            return
        try:
            dag = session._get_derivation()
            if dag.is_empty:
                return
            try:
                layer = dag.layer(layer_name)
            except KeyError:
                return
            # Recompute through the *existing* reentrant produce seam (AB2). Both
            # branches cache + bind as a side effect, so the cache is warm
            # afterwards even if nobody is subscribed (in-process self-heal).
            if layer.is_traced:
                # Produce-then-key. on_miss="produce" forces the slow producer
                # (the cheap self-heal check already failed on the read thread).
                dag.derived_traced(layer, snap, durable_id)
            else:
                scheduler = dag._get_scheduler()
                scheduler.recompute_now(dag, layer, snap, durable_id)
            self._publish_fresh(revision, layer_name, durable_id)
        finally:
            try:
                snap.close()
            except Exception:
                # Best-effort cleanup of the frozen snapshot; a close failure must
                # not mask the recompute result.
                pass

    def _publish_fresh(self, revision: int, layer_name: str, durable_id: str) -> None:
        """Publish a ``DerivedFresh`` on the bus iff someone is subscribed.

        Read the *already-built* bus directly (never ``_get_bus()`` — that would
        build a bus nobody asked for). No subscribers ⇒ skip the publish only;
        the cache is already warm, so the next in-process read is ``fresh``."""
        bus = getattr(self._session, "_bus", None)
        if bus is None or not bus.has_subscribers():
            return
        from tyo3.bus.derived import DerivedFresh

        bus.publish_derived_fresh(DerivedFresh(revision=revision, layer=layer_name, durable_id=durable_id))
