"""BusPump — forward committed deltas (and precision refinements) to clients.

The session's delta bus is a **push** channel. One subscription on
``Interest.ALL`` receives every committed revision; a background worker drains it
and writes a ``delta`` notification to every connected socket client. The same
worker drains the *refinement* channel (``precision = "method"`` only) and emits
``refinement`` notifications.

Threading shape mirrors ``tyo3.precision.refiner.PrecisionRefiner``: a single
daemon thread, a ``_STOP``-style stop via closing the subscription, idempotent
``start`` / ``stop``. The subscription queue is already thread-safe, so the pump
never touches the session from this thread — it only reads its own queue and
calls the broadcast callback. The one session interaction (``subscribe``) is
done through the actor, so the lazy bus build stays serialised.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from tyo3.bus.interest import Interest
from tyo3.daemon.protocol import encode_notification

if TYPE_CHECKING:
    from tyo3.bus.delta import Delta
    from tyo3.bus.derived import DerivedFresh
    from tyo3.bus.refinement import AffectedRefinement
    from tyo3.daemon.session_actor import SessionActor
    from tyo3.daemon.tracking import AffectedTracker

# How long the worker blocks on the primary delta queue before draining the
# refinement queue. Small enough that a refinement lands promptly, large enough
# that the loop is not a busy-wait.
_POLL_TIMEOUT_S = 0.2


class BusPump:
    """Drains the session bus and broadcasts notifications to clients."""

    def __init__(
        self,
        actor: SessionActor,
        broadcast: Callable[[str], None],
        broadcast_delta: Callable[[Delta], None],
        *,
        tracker: AffectedTracker | None = None,
    ) -> None:
        self._actor = actor
        self._broadcast = broadcast
        self._broadcast_delta = broadcast_delta
        self._tracker = tracker
        self._sub = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe (via the actor) and start the drain worker. Idempotent."""
        if self._started:
            return
        self._started = True
        # Build the subscription on the actor thread — the bus is built lazily
        # inside the session and must not race a concurrent edit.
        self._sub = self._actor.submit(lambda s: s.subscribe(Interest.ALL))
        self._thread = threading.Thread(target=self._run, name="tyo3-bus-pump", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the worker and close the subscription. Idempotent."""
        if not self._started:
            return
        self._stop.set()
        sub = self._sub
        if sub is not None:
            # Closing wakes the blocked poll() so the worker exits promptly.
            try:
                sub.close()
            except Exception:
                pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None
        self._sub = None
        self._started = False

    # ── Drain loop ─────────────────────────────────────────────────

    def _run(self) -> None:
        sub = self._sub
        if sub is None:
            return
        while not self._stop.is_set():
            try:
                delta = sub.poll(timeout=_POLL_TIMEOUT_S)
            except Exception:
                break
            if delta is not None:
                self._emit_delta(delta)
            # Drain any refinements that have queued up (they ride a separate
            # channel and may arrive between or after primary deltas).
            while True:
                try:
                    ref = sub.poll_refinement(timeout=0.0)
                except Exception:
                    ref = None
                if ref is None:
                    break
                self._emit_refinement(ref)
            # Drain derived-fresh signals (AB3) — another out-of-band channel;
            # each says "go re-pull (layer, id), the cache is now warm".
            while True:
                try:
                    msg = sub.poll_derived(timeout=0.0)
                except Exception:
                    msg = None
                if msg is None:
                    break
                self._emit_derived(msg)

    def _emit_delta(self, delta: Delta) -> None:
        # Tracker recording is delta-level — it runs once per revision,
        # independent of how many clients match. Keep it here, before the
        # per-connection fan-out (AB7).
        if self._tracker is not None:
            self._tracker.record(
                delta.revision,
                set(delta.affected) | set(delta.changed) | set(delta.created) | set(delta.deleted) | set(delta.moved),
            )
        # Hand the raw Delta to the server, which scopes + encodes it per
        # connected client according to that client's Interest (AB7).
        self._broadcast_delta(delta)

    def _emit_refinement(self, ref: AffectedRefinement) -> None:
        if self._tracker is not None:
            self._tracker.record(ref.revision, set(ref.narrowed))
        params = {
            "revision": ref.revision,
            "narrowed": sorted(ref.narrowed),
            "added": sorted(ref.added),
        }
        self._broadcast(encode_notification("refinement", params))

    def _emit_derived(self, msg: DerivedFresh) -> None:
        """Emit a ``derived`` notification (AB3): a ``serving="stale"`` value the
        off-actor worker just produced is now fresh — the editor re-pulls it.

        Broadcast-to-all like a refinement: a client that never read the stale
        value simply ignores the signal (re-pulling is idempotent)."""
        params = {
            "revision": msg.revision,
            "layer": msg.layer,
            "durable_id": msg.durable_id,
        }
        self._broadcast(encode_notification("derived", params))
