"""LayerView protocol — the uniform read surface for all layers (§5.1).

Every layer (code, derived, authored) exposes the same shape:
ids, value, and a cross-revision diff. The join (Step 2) and combined
diff (Step 6) consume this protocol without special-casing any layer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class LayerDiff:
    """A generic per-layer diff across two revisions.

    ``added``: ids present at R1 but absent at R0 (new entity).
    ``removed``: ids present at R0 but absent at R1 (deleted entity).
    ``drifted``: ids present at both; their layer-specific key differs.
    """

    layer: str
    added: frozenset[str]
    removed: frozenset[str]
    drifted: frozenset[str]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.drifted)


@runtime_checkable
class LayerView(Protocol):
    """A single layer's revision-pinned view (§5.1).

    Bound to one ``Snapshot`` — every read resolves against that
    snapshot's pinned revision. There is no head access.
    """

    name: str
    origin: Literal["code", "derived", "authored"]

    def ids(self) -> Iterable[str]:
        """DurableIds present at the pinned revision for this layer.

        Not every id in the project — only those this layer covers.
        For code: entity ids (excluding module/external synthetics).
        For derived: entity ids matching the layer's ``applies_to``.
        For authored: ids with a record at R.
        """
        ...

    def value(self, durable_id: str) -> Any | None:
        """The per-id record at the pinned revision.

        Returns ``None`` when the id is absent at R or not applicable
        to this layer.
        """
        ...

    def diff(self, other: LayerView) -> LayerDiff:
        """Structural diff between *other* (before) and *self* (after).

        Both views must be pinned to the same session and layer.
        """
        ...
