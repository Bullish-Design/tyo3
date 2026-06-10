"""Bus — register, publish, scoped non-blocking fan-out.

The single registry of subscriptions.  ``publish`` fans a committed
delta out to every subscriber whose interest matches, scoped to that
interest, in revision order, without blocking the writer (§12.2.1–4).

The bus is fed *after* publication, never touches the write transaction.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from tyo3.bus.delta import Delta
    from tyo3.bus.derived import DerivedFresh
    from tyo3.bus.interest import Interest
    from tyo3.bus.refinement import AffectedRefinement
    from tyo3.bus.subscription import Subscription


class Bus:
    """Subscription registry + non-blocking fan-out.

    Constructed once per session.  All public methods are safe to call
    from any thread.
    """

    def __init__(
        self,
        *,
        capacity: int = 1024,
        overflow: Literal["coalesce", "drop_and_mark_lagged", "error_and_close"] = "coalesce",
    ) -> None:
        self._capacity = capacity
        self._overflow: Literal["coalesce", "drop_and_mark_lagged", "error_and_close"] = overflow
        self._subs: set[Subscription] = set()
        self._lock = threading.Lock()
        self._closed = False
        self._last_published_revision: int = -1

    # ── Registration ───────────────────────────────────────────────

    def subscribe(self, interest: Interest) -> Subscription:
        """Register a new subscriber with *interest*.

        Returns a ``Subscription`` the subscriber uses to receive
        deltas.  The subscription is automatically unregistered on
        ``close()``.
        """
        from tyo3.bus.subscription import Subscription as SubCls

        sub = SubCls(
            interest,
            capacity=self._capacity,
            overflow=self._overflow,
            bus=self,
        )
        with self._lock:
            if self._closed:
                sub.close()
                return sub
            self._subs.add(sub)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        """Remove *sub* from the registry.  Called by Subscription.close()."""
        with self._lock:
            self._subs.discard(sub)

    # ── Publishing ─────────────────────────────────────────────────

    def publish(self, delta: Delta) -> None:
        """Fan *delta* out to every subscriber whose interest matches.

        Called by the write path **in revision order** (the caller holds
        the write lock).  The bus does not re-sort — it relies on the
        caller's ordering guarantee.

        Each subscriber gets a **scoped** delta (``delta.scoped_to(sub.interest)``);
        subscribers whose interest doesn't match receive nothing.

        Never blocks the writer (default overflow policies).
        """
        with self._lock:
            if self._closed or not self._subs:
                return

            # Revision order is a real invariant now (Phase 5: publication is
            # the strictly-last in-lock step under one serialised native
            # commit), so assert it — each commit publishes once, in order. If
            # this fires, a caller double-published or published out of order;
            # fix the caller, never relax to >=.
            assert delta.revision > self._last_published_revision, (
                f"bus received revision {delta.revision} after {self._last_published_revision} — revision order broken"
            )
            self._last_published_revision = delta.revision

            # Snapshot current subscribers so we fan out outside the set lock.
            subs_snapshot = list(self._subs)

        # Fan out outside the set lock.
        for sub in subs_snapshot:
            interest = sub.interest
            # ALL / rescan: every committed revision is delivered
            # unconditionally — even an empty one — so the subscriber can pin a
            # snapshot at exactly this revision (§5.11 "ALL: every committed
            # revision"; rescan is "everything").
            if interest.all or delta.rescan:
                sub._offer(delta.scoped_to(interest))
                continue
            # Scoped interest (ids / files / layers): deliver only a non-empty
            # intersection.
            if interest.matches(
                affected_ids=delta.affected,
                affected_files=delta.files,
                touched_layers=delta.layers,
            ):
                scoped = delta.scoped_to(interest)
                if not scoped.is_empty():
                    sub._offer(scoped)

    def publish_refinement(self, ref: AffectedRefinement) -> None:
        """Fan an ``AffectedRefinement`` out on the **refinement channel** (§5.4).

        Delivered on a channel **separate** from the primary delta stream, so it
        does **not** touch the ``revision > last`` invariant: a refinement for
        revision R may legitimately arrive after R's primary delta (and even
        after R+1's). Each matching subscriber receives it on its refinement
        queue and reconciles it against the primary delta it already saw for R.

        Matching: ``ALL`` subscribers always receive it; a scoped subscriber
        receives it when its id-interest intersects the refinement's narrowed
        (or expansion-added) ids. Like ``publish`` it never blocks the producer.

        Phase 7 ships this as the **contract seam** — nothing emits a refinement
        until the async precision worker lands (Phase 9).
        """
        with self._lock:
            if self._closed or not self._subs:
                return
            subs_snapshot = list(self._subs)

        ids = ref.narrowed | ref.added
        for sub in subs_snapshot:
            interest = sub.interest
            if interest.all or (interest.ids and (interest.ids & ids)):
                sub._offer_refinement(ref)

    def publish_derived_fresh(self, msg: DerivedFresh) -> None:
        """Fan a ``DerivedFresh`` out on the **derived-fresh channel** (AB3).

        Delivered on a channel **separate** from the primary delta stream (like
        ``publish_refinement``), so it does **not** touch the ``revision > last``
        invariant: a "value X is now fresh" signal is produced off the actor and
        may arrive after R's primary delta (and after R+1's). Each matching
        subscriber receives it on its derived-fresh queue and re-pulls.

        Matching: ``ALL`` subscribers always receive it; a scoped subscriber
        receives it when its **id**-interest contains the durable_id or its
        **layer**-interest contains the layer. Like ``publish`` it never blocks
        the off-actor worker."""
        with self._lock:
            if self._closed or not self._subs:
                return
            subs_snapshot = list(self._subs)

        for sub in subs_snapshot:
            interest = sub.interest
            if (
                interest.all
                or (interest.ids and msg.durable_id in interest.ids)
                or (interest.layers and msg.layer in interest.layers)
            ):
                sub._offer_derived(msg)

    def has_subscribers(self) -> bool:
        """Fast check for the write-path no-op guard."""
        with self._lock:
            return len(self._subs) > 0

    # ── Lifecycle ──────────────────────────────────────────────────

    def close(self) -> None:
        """Close all subscriptions and the bus itself."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subs = list(self._subs)
            self._subs.clear()

        for sub in subs:
            try:
                sub.close()
            except Exception:
                pass
