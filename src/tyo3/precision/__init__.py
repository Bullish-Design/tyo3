"""Optional async precision layer (Concept V2 §5.4, Phase 9).

The synchronous in-commit ``affected`` set is **sound-and-coarse**
(container-granular, never-miss). This package sharpens it to method-level
precision *asynchronously*, over frozen snapshots, and publishes the result as
an :class:`~tyo3.bus.refinement.AffectedRefinement` on the bus's refinement
channel. It exists **only** to improve precision: the system is fully correct
without it, and a lagging, crashing, or disabled worker leaves the coarse set
intact (graceful degradation).
"""

from __future__ import annotations

from tyo3.precision.refiner import PrecisionRefiner

__all__ = ["PrecisionRefiner"]
