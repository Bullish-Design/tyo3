"""DerivedFresh — the bus's "a derived value became fresh" message (AB3).

A ``serving="stale"`` layer serves last-good (``stale``) or ``absent`` at read
and recomputes the slow producer **off the actor** (AB3,
:class:`tyo3.derive.async_recompute.DerivedRecomputeWorker`). When the off-actor
produce completes, the worker emits a ``DerivedFresh`` so a subscribed editor
re-pulls and replaces the stale card/decoration with the now-fresh value.

Like an :class:`~tyo3.bus.refinement.AffectedRefinement`, this travels on a
channel **separate** from the primary delta stream: it is out-of-band and may
arrive after later revisions' deltas, so it must **never** participate in the
primary ``revision > last`` ordering invariant. A subscriber treats it purely as
"go re-read ``(layer, durable_id)`` — the cache is now warm".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DerivedFresh:
    """A revision-stamped "derived value is now fresh" signal.

    ``revision`` is the revision the off-actor worker produced over (the read's
    pinned revision); ``layer`` / ``durable_id`` identify the value that warmed.
    Immutable — delivered to subscriber derived-fresh queues.
    """

    revision: int
    layer: str
    durable_id: str


__all__ = ["DerivedFresh"]
