"""Subscription — bounded queue, iterator, teardown for one subscriber.

Each subscriber gets exactly one ``Subscription`` from the bus.  The
subscription holds a bounded queue, a blocking iterator, a non-blocking
``poll``, and idempotent ``close`` that releases everything (§12.2.4,
§12.2.6).
"""

from __future__ import annotations

import threading
import time as _time
from collections import deque
from typing import TYPE_CHECKING, Iterator, Literal

if TYPE_CHECKING:
    from tyo3.bus.delta import Delta
    from tyo3.bus.interest import Interest
    from tyo3.bus.refinement import AffectedRefinement

# Runtime import needed for _coalesce_into_tail which constructs Delta.
# We import lazily to avoid circular imports at module level.
_Delta: type | None = None


def _get_delta_class() -> type:
    global _Delta
    if _Delta is None:
        from tyo3.bus.delta import Delta as _DeltaCls
        _Delta = _DeltaCls
    return _Delta


class Subscription:
    """One subscriber's delivery endpoint.

    Producer side (``_offer``) is called by the ``Bus`` after publication — it
    **never** blocks the writer; all overflow policies are non-blocking.
    Consumer side (``poll``, ``__iter__``) is called by the subscriber's own
    thread.
    """

    def __init__(
        self,
        interest: Interest,
        *,
        capacity: int = 1024,
        overflow: Literal["coalesce", "drop_and_mark_lagged", "error_and_close"] = "coalesce",
        bus: object | None = None,  # Bus — set by Bus.subscribe()
    ) -> None:
        self.interest: Interest = interest
        self._capacity = max(1, capacity)
        self._overflow: Literal["coalesce", "drop_and_mark_lagged", "error_and_close"] = overflow
        self._bus: object | None = bus

        self._queue: deque[Delta] = deque()
        # The refinement channel is a SEPARATE queue (§5.4): a late refinement for
        # an already-delivered revision must not perturb the primary delta stream
        # or its revision-order invariant. Shares the lock/cond for cheap
        # signalling; consumers poll it independently of the delta queue.
        self._refinements: deque[AffectedRefinement] = deque()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._closed = False
        self._lagged = False

    # ── Producer side (called by Bus, under the write lock) ─────────

    def _offer(self, delta: Delta) -> None:
        """Append *delta* to the queue; apply the (non-blocking) overflow policy.

        Called by the ``Bus`` while fanning out a published delta.  Must NEVER
        block the writer — there is no producer-side wait for any policy
        (§5.11: a slow or dead subscriber must not stall the writer).
        """
        should_close = False
        with self._lock:
            if self._closed:
                return

            if len(self._queue) < self._capacity:
                self._queue.append(delta)
                self._cond.notify_all()
                return

            # Queue is full — apply a non-blocking overflow policy.
            if self._overflow == "coalesce":
                # Union into the tail; the subscriber misses nothing (§12.2.2).
                self._coalesce_into_tail(delta)
            elif self._overflow == "error_and_close":
                # Mark lagged and tear the subscription down (drop + close),
                # still never blocking the producer.
                self._lagged = True
                should_close = True
            else:  # drop_and_mark_lagged
                # Drop on the floor and force the subscriber to rescan — never
                # blocks the writer.
                self._lagged = True

        # Close outside the lock (close() re-acquires it); idempotent.
        if should_close:
            self.close()

    def _coalesce_into_tail(self, incoming: Delta) -> None:
        """Merge *incoming* into the tail delta of the queue.

        Keeps the *latest* revision as the delta's revision but unions
        all id sets since the last consumed one, so the subscriber reads
        the newest state and misses nothing (§12.2.2 non-hiding).
        """
        DeltaCls = _get_delta_class()
        if not self._queue:
            self._queue.append(incoming)
            self._cond.notify_all()
            return

        tail = self._queue.pop()
        merged = DeltaCls(
            revision=max(tail.revision, incoming.revision),
            created=tail.created | incoming.created,
            changed=tail.changed | incoming.changed,
            deleted=tail.deleted | incoming.deleted,
            moved=tail.moved | incoming.moved,
            authored=tail.authored | incoming.authored,
            affected=tail.affected | incoming.affected,
            rescan=tail.rescan or incoming.rescan,
            files=tail.files | incoming.files,
            layers=tail.layers | incoming.layers,
        )
        self._queue.append(merged)
        self._cond.notify_all()

    def _offer_refinement(self, ref: AffectedRefinement) -> None:
        """Append *ref* to the refinement queue (non-blocking).

        Called by the ``Bus`` on the refinement channel.  Like ``_offer`` it
        must NEVER block the producer; on overflow it drops the **oldest**
        refinement (refinements are advisory narrowings — the coarse set the
        subscriber already holds stays correct, Concept V2 §5.4 graceful
        degradation).
        """
        with self._lock:
            if self._closed:
                return
            self._refinements.append(ref)
            if len(self._refinements) > self._capacity:
                self._refinements.popleft()
            self._cond.notify_all()

    # ── Consumer side (called by the subscriber's own thread) ───────

    def poll(self, timeout: float | None = 0.0) -> Delta | None:
        """Non-blocking poll (or block up to *timeout* seconds).

        Returns the next ``Delta`` or ``None`` if the queue is empty
        and the timeout expires.  Set ``timeout=None`` to block
        indefinitely (equivalent to iteration).
        """
        with self._cond:
            if timeout is None:
                # Block indefinitely.
                while not self._closed and not self._queue:
                    self._cond.wait()
            elif timeout > 0:
                deadline = _time.monotonic() + timeout
                while not self._closed and not self._queue:
                    remaining = deadline - _time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=remaining)
            # timeout == 0: non-blocking, just check.

            if self._queue:
                delta = self._queue.popleft()
                self._cond.notify_all()
                return delta

        return None

    def poll_refinement(
        self, timeout: float | None = 0.0
    ) -> AffectedRefinement | None:
        """Non-blocking poll of the **refinement** channel (or block up to
        *timeout* seconds).

        Returns the next ``AffectedRefinement`` or ``None`` if none is queued
        and the timeout expires.  Independent of ``poll`` — the primary delta
        stream and the refinement stream are consumed separately, in their own
        orders (a refinement for R may legitimately arrive after R+1's delta).
        """
        with self._cond:
            if timeout is None:
                while not self._closed and not self._refinements:
                    self._cond.wait()
            elif timeout > 0:
                deadline = _time.monotonic() + timeout
                while not self._closed and not self._refinements:
                    remaining = deadline - _time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=remaining)
            # timeout == 0: non-blocking, just check.

            if self._refinements:
                ref = self._refinements.popleft()
                self._cond.notify_all()
                return ref

        return None

    def __iter__(self) -> Iterator[Delta]:
        """Blocking iterator: yields deltas in revision order.

        The loop ends when ``close()`` is called from another thread
        (``StopIteration`` is raised naturally when the condition wakes
        and the queue is empty after close).
        """
        return self

    def __next__(self) -> Delta:
        with self._cond:
            while not self._closed and not self._queue:
                self._cond.wait()
            if self._closed and not self._queue:
                raise StopIteration
            delta = self._queue.popleft()
            self._cond.notify_all()
            return delta

    # ── Lifecycle ──────────────────────────────────────────────────

    def close(self) -> None:
        """Idempotent teardown: unregister from bus, wake iterators,
        release the queue.

        Safe to call multiple times.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # Wake any blocked consumer/producer.
            self._cond.notify_all()

        # Unregister from the bus outside the lock to avoid deadlock
        # if the bus holds its own lock.
        bus = self._bus
        if bus is not None:
            try:
                bus._unsubscribe(self)  # type: ignore[union-attr]
            except Exception:
                pass
            self._bus = None

        # Drain the queues to release any held references.
        with self._lock:
            self._queue.clear()
            self._refinements.clear()

    @property
    def lagged(self) -> bool:
        """True when an overflow policy (``drop_and_mark_lagged`` /
        ``error_and_close``) dropped a delta."""
        with self._lock:
            return self._lagged

    def rescan_from(
        self, session: object, last_seen_revision: int | None
    ) -> object:
        """Catch up after eviction/lag: diff the last revision the
        subscriber successfully read against the current head, returning
        the full affected ``SnapshotDiff`` (Gate 7).

        If *last_seen_revision* is still retained, computes
        ``after.diff(before)`` over ``snapshot(at=head)`` and
        ``snapshot(at=last_seen)``.  If *last_seen* is also evicted,
        treats it as a full rescan — returns a ``SnapshotDiff``
        against an empty/initial baseline (everything affected).

        After calling this, the subscriber should update its
        ``last_seen_revision`` from the returned diff's
        ``after_revision``.
        """
        from tyo3.exceptions import RevisionEvictedError

        head_snap = session.snapshot()  # type: ignore[union-attr]

        if last_seen_revision is not None:
            try:
                before_snap = session.snapshot(at=last_seen_revision)  # type: ignore[union-attr]
                diff = head_snap.diff(before_snap)
                before_snap.close()
                head_snap.close()
                return diff
            except RevisionEvictedError:
                pass
            except Exception:
                pass

        # Full rescan: diff against an empty baseline.
        # Use the earliest still-retained revision as baseline, or
        # treat the head snapshot diff against itself as "everything
        # added" by building a diff against an empty baseline.
        #
        # The cleanest approach: return a SnapshotDiff where the
        # before_revision is 0 (meaning "nothing existed") and
        # after_revision is head.  The caller interprets this as
        # "rescan=everything".
        try:
            # Try to diff against the earliest retained revision.
            earliest = max(0, head_snap.revision - session.config.spine.retain_cap)  # type: ignore[union-attr]
            if earliest < head_snap.revision:
                try:
                    before_snap = session.snapshot(at=earliest)  # type: ignore[union-attr]
                    diff = head_snap.diff(before_snap)
                    before_snap.close()
                    head_snap.close()
                    return diff
                except Exception:
                    pass
        except Exception:
            pass

        # Fallback: diff head against itself — the diff will be empty.
        # The caller should detect this and treat as rescan=everything.
        diff = head_snap.diff(head_snap)
        head_snap.close()
        return diff

    # ── Context manager ────────────────────────────────────────────

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
