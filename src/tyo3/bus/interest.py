"""Interest — composable subscription filter.

Describes what a subscriber cares about.  Immutable, hashable, cheap
``matches``/``scope`` against an affected set (§12.2.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from tyo3.bus.delta import Delta


@dataclass(frozen=True)
class Interest:
    """A composable description of what a subscriber cares about.

    Compound interests are built with ``|`` (union).  Matching is
    ``all`` OR any non-empty intersection of ``ids``/``files``/``layers``
    with the delta's affected sets.

    Immutable and hashable so the bus can key subscribers cheaply.
    """

    files: frozenset[str] = frozenset()  # project-relative paths
    ids: frozenset[str] = frozenset()  # DurableIds
    layers: frozenset[str] = frozenset()  # layer names (e.g. "embeddings", "intent")
    all: bool = False  # ALL: every committed revision

    ALL: ClassVar[Interest]

    @classmethod
    def files_of(cls, paths: set[str] | frozenset[str]) -> Interest:
        """Build an interest scoped to *paths* (project-relative)."""
        return cls(files=frozenset(paths))

    @classmethod
    def ids_of(cls, ids: set[str] | frozenset[str]) -> Interest:
        """Build an interest scoped to *ids* (DurableIds)."""
        return cls(ids=frozenset(ids))

    @classmethod
    def layer(cls, name: str) -> Interest:
        """Build an interest for a single layer *name*."""
        return cls(layers=frozenset({name}))

    def __or__(self, other: Interest) -> Interest:
        """Union: Interest | Interest produces a compound filter."""
        if not isinstance(other, Interest):
            return NotImplemented
        return Interest(
            files=self.files | other.files,
            ids=self.ids | other.ids,
            layers=self.layers | other.layers,
            all=self.all or other.all,
        )

    def matches(
        self,
        affected_ids: set[str] | frozenset[str],
        affected_files: set[str] | frozenset[str],
        touched_layers: set[str] | frozenset[str] = frozenset(),
    ) -> bool:
        """Return True iff the affected sets intersect this interest.

        ALL matches everything.  Otherwise at least one non-empty
        intersection across ids/files/layers is required.
        """
        if self.all:
            return True
        if self.ids and affected_ids:
            if self.ids & affected_ids:
                return True
        if self.files and affected_files:
            if self.files & affected_files:
                return True
        if self.layers and touched_layers:
            if self.layers & touched_layers:
                return True
        return False

    def scope(self, delta: Delta) -> Delta:
        """Return a new ``Delta`` scoped to this interest.

        Delegates to ``delta.scoped_to(self)``.
        """
        return delta.scoped_to(self)

    @property
    def is_empty(self) -> bool:
        """An interest that matches nothing."""
        return not self.all and not self.files and not self.ids and not self.layers


# Post-definition classvar.
Interest.ALL = Interest(all=True)
