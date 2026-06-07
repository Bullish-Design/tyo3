"""Bus — register, publish, scoped non-blocking fan-out.

The single registry of subscriptions.  ``publish`` fans a committed
delta out to every subscriber whose interest matches, scoped to that
interest, in revision order, without blocking the writer (§12.2.1–4).

The bus is fed *after* publication, never touches the write transaction.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from tyo3.bus.delta import Delta
    from tyo3.bus.interest import Interest
    from tyo3.bus.subscription import Subscription

logger = logging.getLogger(__name__)


class Bus:
    """Subscription registry + non-blocking fan-out.

    Constructed once per session.  All public methods are safe to call
    from any thread.
    """

    def __init__(
        self,
        *,
        capacity: int = 1024,
        overflow: Literal["coalesce", "block", "error"] = "coalesce",
    ) -> None:
        self._capacity = capacity
        self._overflow: Literal["coalesce", "block", "error"] = overflow
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

            # Defensive: assert monotonic revisions.
            if delta.revision <= self._last_published_revision:
                logger.error(
                    "Bus.publish received revision %d after %d — "
                    "revision order broken (write lock may not be held).",
                    delta.revision,
                    self._last_published_revision,
                )
            self._last_published_revision = max(
                self._last_published_revision, delta.revision
            )

            # Snapshot current subscribers to avoid holding the set lock
            # across _offer calls (which may block for "block" policy).
            subs_snapshot = list(self._subs)

        # Fan out outside the set lock.
        for sub in subs_snapshot:
            interest = sub.interest
            # Rescan deltas are delivered to every subscriber (§4.3.5).
            if delta.rescan or interest.matches(
                affected_ids=delta.affected,
                affected_files=delta.files,
                touched_layers=delta.layers,
            ):
                scoped = delta.scoped_to(interest)
                # Deliver if not empty OR it's a rescan ("everything").
                if not scoped.is_empty() or scoped.rescan:
                    sub._offer(scoped)

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
