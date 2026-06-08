"""AffectedRefinement — the bus's refinement-channel message (§5.4 / Concept V2 §6).

A revision-stamped, after-the-fact narrowing (or, in expansion mode, widening)
of a published revision's ``affected`` set.  The synchronous in-commit
``affected`` set is **sound-and-coarse** (container-granular); the *optional*
async precision layer (Phase 9) re-resolves member-access targets over a frozen
snapshot and emits a refinement that sharpens it to method precision.

The refinement travels on a channel **separate** from the primary delta stream:
a refinement for revision R may arrive *after* R's primary delta (and even after
R+1's), so it must never participate in the primary ``revision > last`` ordering
invariant.  A subscriber reconciles a refinement against the primary delta it
already saw for that same revision.

This module is the **contract seam only** — Phase 7 adds the type and ordered
delivery; **nothing emits a refinement yet** (Phase 9 wires the producer).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AffectedRefinement:
    """A revision-stamped refinement of a published revision's ``affected`` set.

    ``narrowed`` is the precise affected set for ``revision`` — a **subset** of
    the coarse synchronous set (narrowing can never introduce a miss, since the
    synchronous set is a sound superset, Concept V2 §5.4).  ``added`` carries the
    *expansion* mode's extra non-nominal dependents (off by default, Phase 9);
    it is empty for the default narrowing mode.

    Immutable — delivered to subscriber refinement queues.
    """

    revision: int
    narrowed: frozenset[str] = field(default_factory=frozenset)
    added: frozenset[str] = field(default_factory=frozenset)


__all__ = ["AffectedRefinement"]
