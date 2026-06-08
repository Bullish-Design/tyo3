"""Delta — the revision-stamped, scopable notification object.

A thin immutable projection of the id-level ``CommitDelta`` (§5.11): the
``created``/``changed``/``deleted``/``moved``/``authored`` id sets are taken
**straight from the commit delta's id fields** — no path→id reconstruction.

The one graph-touch that survives is the *transitive affected closure*
(§5.4 / §5.11): ``affected`` is the closure of ``changed ∪ deleted`` under
reverse-deps, so a reverse-dependent (e.g. an importer of a changed entity) is
notified even though its own body didn't change.  This is computed natively in
the commit once the in-commit code-layer producer lands; while that producer is
deferred (Phases 3–5), ``CommitDelta.affected_ids`` is the *seed* set only, so
we expand it over the materialised head graph here — purely id→id and id→file,
never path→id.  When the native producer lands, ``affected_ids`` is already
transitive and these two helpers are deleted (the bus becomes a pure
projection).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tyo3.bus.interest import Interest
    from tyo3.graph.graph import CodeGraph
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
    created: frozenset[str]   # DurableIds created this revision
    changed: frozenset[str]   # DurableIds changed (content hash differs)
    deleted: frozenset[str]   # DurableIds deleted
    moved: frozenset[str]     # DurableIds moved (same hash, new location)
    authored: frozenset[str]  # authored-record ids edited (Gate 6)
    affected: frozenset[str]  # transitive closure of changed∪deleted (§5.4)
    rescan: bool
    files: frozenset[str]     # project-relative paths touched
    layers: frozenset[str]    # layers touched ("code" + derived/authored layers)

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
        return not (
            self.created
            or self.changed
            or self.deleted
            or self.moved
            or self.authored
            or self.affected
        )

    @classmethod
    def from_commit_delta(
        cls,
        delta: CommitDelta,
        graph: CodeGraph | None = None,
        *,
        root: str | None = None,
    ) -> Delta:
        """Wrap an id-level ``CommitDelta`` for bus delivery (§5.11).

        The id sets come straight from the commit delta's id fields — no
        path→id reconstruction.  ``affected`` is the native ``affected_ids``
        (the transitive closure of ``changed ∪ deleted`` under reverse-deps,
        §5.4); while the in-commit producer is deferred that field is the seed
        set only, so it is expanded over *graph* (the materialised head graph
        at this revision) when one is available — id→id only.

        ``touched_files`` is path metadata (absolute on the native delta);
        when *root* is given it is normalised to project-relative for
        file-interest matching, and the project-relative files of the affected
        ids are unioned in so a file-interested reverse-dependent matches.
        """
        created = frozenset(delta.created_ids)
        changed = frozenset(delta.changed_ids)
        deleted = frozenset(delta.deleted_ids)
        moved = frozenset(m.id for m in delta.moved)
        authored = frozenset(delta.authored_ids)

        # Native affected_ids is the seed set (changed∪deleted) until the
        # in-commit producer lands; expand it transitively over the head graph
        # so reverse-dependents are still notified (§5.4 / §5.11; the Phase 3
        # "no capability lost" decision).  When the native producer lands,
        # affected_ids is already transitive and _compute_affected is a no-op
        # (the closure of a closed set is itself) — then this helper is deleted.
        seed = frozenset(delta.affected_ids) or (changed | deleted)
        affected = _compute_affected(seed, graph)

        touched = delta.touched_files
        if root is not None:
            files = frozenset(_to_relative(root, p) for p in touched)
        else:
            files = frozenset(touched)
        # Union the project-relative files of affected ids so a file-interested
        # reverse-dependent matches (the affected importer's own file).
        files = files | _resolve_files(affected, graph)

        return cls(
            revision=delta.revision,
            created=created,
            changed=changed,
            deleted=deleted,
            moved=moved,
            authored=authored,
            affected=affected,
            rescan=delta.rescan,
            files=files,
            layers=_layers_touched(delta),
        )


# ── Helpers ──────────────────────────────────────────────────────────


def _to_relative(root: str, path: str) -> str:
    """Convert an absolute path to project-relative (idempotent for
    already-relative paths)."""
    from pathlib import Path, PurePosixPath

    try:
        root_p = Path(root).resolve()
        path_p = Path(path).resolve()
        return str(PurePosixPath(path_p.relative_to(root_p)))
    except Exception:
        return path


def _compute_affected(
    seed: frozenset[str],
    graph: CodeGraph | None,
) -> frozenset[str]:
    """Expand *seed* (durable ids) to its transitive reverse-dep closure
    (§5.4): ``affected = seed ∪ transitive_dependents(seed)``.

    Pure id→id over the graph's reverse-dependency index.  Returns *seed*
    unchanged when no graph is materialised (the native seed set).  Deleted
    once the native in-commit producer emits a transitive ``affected_ids``.
    """
    if graph is None:
        return seed

    affected: set[str] = set(seed)
    try:
        for did in seed:
            affected.update(graph.transitive_dependents(did))
    except Exception:
        pass
    return frozenset(affected)


def _resolve_files(
    ids: frozenset[str],
    graph: CodeGraph | None,
) -> frozenset[str]:
    """Map durable *ids* to their defining project-relative files (id→file).

    Used so a file-interested subscriber matches a reverse-dependent by its
    own file.  Empty when no graph is materialised.
    """
    if graph is None or not ids:
        return frozenset()

    files: set[str] = set()
    try:
        for did in ids:
            idx = graph._id_to_index.get(did)
            if idx is not None:
                node = graph._graph[idx]
                if node.file:
                    files.add(node.file)
    except Exception:
        pass
    return frozenset(files)


def _layers_touched(delta: CommitDelta) -> frozenset[str]:
    """Resolve the set of layers touched by this revision, keyed off the
    commit delta's id fields (not file strings).

    Any structural change touches the implicit ``"code"`` layer; an authored
    write touches the ``"authored"`` layer (so a layer-interested subscriber
    is notified).
    """
    layers: set[str] = set()

    has_code_change = bool(
        delta.created_ids
        or delta.changed_ids
        or delta.deleted_ids
        or delta.moved
        or delta.rescan
    )
    if has_code_change:
        layers.add("code")

    if delta.authored_ids:
        layers.add("authored")

    return frozenset(layers)
