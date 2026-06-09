"""Delta — the revision-stamped, scopable notification object.

A thin immutable **pure projection** of the id-level ``CommitDelta`` (§5.11):
every field is read straight off the commit delta — no path→id reconstruction,
no graph walk, no materialised-head-graph dependency.

``affected`` is the transitive, container-granular closure of ``changed ∪
deleted`` under the code layer's reverse-dependency index, *computed natively in
the commit* (the Phase 6 scoped in-commit producer maintains ``reverse_deps``
edge-by-edge and emits ``affected_ids`` already-transitive).  ``files`` is the
union of the directly-edited ``touched_files`` and the closure's
``affected_files`` (both project-relative, both native) — so a file-interested
subscriber matches a reverse-dependent by its own file without the bus ever
touching a graph (Phase 7 deleted the interim Option-B bridge).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tyo3.bus.interest import Interest
    from tyo3.models.delta import CommitDelta


@dataclass(frozen=True)
class Delta:
    """A revision-stamped delta produced by a single committed write.

    ``affected`` is the transitive closure of ``changed ∪ deleted`` under the
    dependency graph's inbound edges (§5.4): the set that determines whether a
    subscriber is notified.

    Immutable — the bus delivers these to subscriber queues.
    """

    revision: int
    created: frozenset[str]  # DurableIds created this revision
    changed: frozenset[str]  # DurableIds changed (content hash differs)
    deleted: frozenset[str]  # DurableIds deleted
    moved: frozenset[str]  # DurableIds moved (same hash, new location)
    authored: frozenset[str]  # authored-record ids edited (Gate 6)
    affected: frozenset[str]  # transitive closure of changed∪deleted (§5.4)
    rescan: bool
    files: frozenset[str]  # project-relative paths touched
    layers: frozenset[str]  # layers touched ("code" + derived/authored layers)

    def scoped_to(self, interest: Interest) -> Delta:
        """Return a new ``Delta`` whose id/file/layer sets are all
        intersected with *interest*, so a subscriber sees only its slice
        (§12.2.1).

        A ``rescan`` delta is always delivered in full (rescan is
        "everything").
        """
        if self.rescan or interest.all:
            return self

        # When interest has files but not ids, we keep all ids (file
        # matching was already validated by interest.matches() in the
        # bus — the delta is relevant to this subscriber).
        has_id_filter = bool(interest.ids)

        if has_id_filter:
            scoped_created = self.created & interest.ids
            scoped_changed = self.changed & interest.ids
            scoped_deleted = self.deleted & interest.ids
            scoped_moved = self.moved & interest.ids
            scoped_authored = self.authored & interest.ids
            scoped_affected = self.affected & interest.ids
        else:
            # File-based, layer-only, or empty interest: keep all ids
            # (the bus already matched this delta to the subscriber).
            scoped_created = self.created
            scoped_changed = self.changed
            scoped_deleted = self.deleted
            scoped_moved = self.moved
            scoped_authored = self.authored
            scoped_affected = self.affected

        return Delta(
            revision=self.revision,
            created=scoped_created,
            changed=scoped_changed,
            deleted=scoped_deleted,
            moved=scoped_moved,
            authored=scoped_authored,
            affected=scoped_affected,
            rescan=self.rescan,
            files=self.files & interest.files if interest.files else self.files,
            layers=self.layers & interest.layers if interest.layers else self.layers,
        )

    def is_empty(self) -> bool:
        """Return True if no ids were touched in any category."""
        return not (self.created or self.changed or self.deleted or self.moved or self.authored or self.affected)

    @classmethod
    def from_commit_delta(cls, delta: CommitDelta) -> Delta:
        """Project an id-level ``CommitDelta`` for bus delivery (§5.11).

        A pure projection: every field is read straight off the delta.  The id
        sets come from the commit delta's id fields; ``affected`` is the native
        ``affected_ids`` (already the transitive, container-granular closure of
        ``changed ∪ deleted`` — the Phase 6 in-commit producer maintains
        ``reverse_deps`` and computes the closure natively).  ``files`` unions
        the directly-edited ``touched_files`` with the closure's
        ``affected_files`` (both native, both project-relative) so a
        file-interested reverse-dependent matches by its own file — no graph.
        """
        return cls(
            revision=delta.revision,
            created=frozenset(delta.created_ids),
            changed=frozenset(delta.changed_ids),
            deleted=frozenset(delta.deleted_ids),
            moved=frozenset(m.id for m in delta.moved),
            authored=frozenset(delta.authored_ids),
            affected=frozenset(delta.affected_ids),
            rescan=delta.rescan,
            files=frozenset(delta.touched_files) | frozenset(delta.affected_files),
            layers=_layers_touched(delta),
        )


# ── Helpers ──────────────────────────────────────────────────────────


def _layers_touched(delta: CommitDelta) -> frozenset[str]:
    """Resolve the set of layers touched by this revision, keyed off the
    commit delta's id fields (not file strings).

    Any structural change touches the implicit ``"code"`` layer; an authored
    write touches the ``"authored"`` layer (so a layer-interested subscriber
    is notified).
    """
    layers: set[str] = set()

    has_code_change = bool(delta.created_ids or delta.changed_ids or delta.deleted_ids or delta.moved or delta.rescan)
    if has_code_change:
        layers.add("code")

    if delta.authored_ids:
        layers.add("authored")

    return frozenset(layers)
