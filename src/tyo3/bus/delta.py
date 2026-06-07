"""Delta — the revision-stamped, scopable notification object.

Packs a committed ``SyncResult`` into a ``Delta`` with the transitive
affected set (§4.3.3) so the bus can scope delivery per-subscriber
(§12.2.1) and subscribers can read exactly the notified revision
(§12.2.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tyo3.bus.interest import Interest
    from tyo3.graph.graph import CodeGraph
    from tyo3.models.analysis import SyncResult


@dataclass(frozen=True)
class Delta:
    """A revision-stamped delta produced by a single committed write.

    ``affected`` is the transitive closure of ``changed ∪ deleted``
    under the dependency graph's inbound edges (§4.3.3): an importer of
    a changed file is in the affected set even though its own code
    didn't change.  This is the set that determines whether a
    subscriber is notified.

    Immutable — the bus delivers these to subscriber queues.
    """

    revision: int
    created: frozenset[str]   # DurableIds created this revision
    changed: frozenset[str]   # DurableIds changed (content hash differs)
    deleted: frozenset[str]   # DurableIds deleted
    moved: frozenset[str]     # DurableIds moved (same hash, new location)
    authored: frozenset[str]  # authored-record ids edited (Gate 6)
    affected: frozenset[str]  # transitive closure of changed∪deleted (§4.3.3)
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
        has_file_filter = bool(interest.files)

        if has_id_filter:
            scoped_created = self.created & interest.ids
            scoped_changed = self.changed & interest.ids
            scoped_deleted = self.deleted & interest.ids
            scoped_moved = self.moved & interest.ids
            scoped_authored = self.authored & interest.ids
            scoped_affected = self.affected & interest.ids
        elif has_file_filter:
            # File-based interest: keep all ids (bus already matched).
            scoped_created = self.created
            scoped_changed = self.changed
            scoped_deleted = self.deleted
            scoped_moved = self.moved
            scoped_authored = self.authored
            scoped_affected = self.affected
        else:
            # Layer-only or empty interest: keep all.
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
    def from_sync_result(
        cls,
        result: SyncResult,
        graph: CodeGraph | None = None,
        *,
        root: str | None = None,
    ) -> Delta:
        """Build a ``Delta`` from a committed ``SyncResult``.

        ``result.created/changed/deleted/moved`` are **absolute file paths**
        from the native SyncResult.  *root* is the project root used to
        normalise them to project-relative paths.  The graph maps those
        files to DurableIds so the delta carries id-level precision.

        If *graph* is provided, the transitive affected set (§4.3.3) is
        computed by walking inbound dependency edges from the entities in
        the changed and deleted files.  Without a graph, ``affected`` is
        the union of all ids in changed+deleted files.

        *graph* must be the head graph at ``result.revision``.
        """
        # Normalise file paths from absolute → project-relative.
        changed_raw = frozenset(result.changed)
        deleted_raw = frozenset(result.deleted)
        created_raw = frozenset(result.created)
        moved_raw = frozenset(result.moved)

        # Normalise paths when root is available.
        if root is not None:
            changed_files = frozenset(_to_relative(root, p) for p in changed_raw)
            deleted_files = frozenset(_to_relative(root, p) for p in deleted_raw)
            created_files = frozenset(_to_relative(root, p) for p in created_raw)
            moved_files = frozenset(_to_relative(root, p) for p in moved_raw)
        else:
            changed_files = changed_raw
            deleted_files = deleted_raw
            created_files = created_raw
            moved_files = moved_raw

        if graph is not None:
            changed_ids = _ids_in_files(changed_files, graph)
            deleted_ids = _ids_in_files(deleted_files, graph)
            created_ids = _ids_in_files(created_files, graph)
            moved_ids = _ids_in_files(moved_files, graph)
        else:
            # No graph: treat SyncResult entries as direct DurableIds.
            changed_ids = changed_files
            deleted_ids = deleted_files
            created_ids = created_files
            moved_ids = moved_files

        # Authored ids are already DurableIds (from SyncResult.authored).
        authored_ids = frozenset(result.authored)

        # Transitive affected set (§4.3.3).
        seed = changed_ids | deleted_ids
        affected = _compute_affected(seed, graph)

        # Files touched: the graph-relative paths from the result + any
        # files reachable via the affected set.
        files = changed_files | deleted_files | created_files | moved_files
        # Also add files from affected ids.
        affected_files = _resolve_files(affected, graph)
        files = files | affected_files

        # Resolve layers touched.
        layers = _resolve_layers_from_files(result, authored_ids)

        return cls(
            revision=result.revision,
            created=created_ids,
            changed=changed_ids,
            deleted=deleted_ids,
            moved=moved_ids,
            authored=authored_ids,
            affected=affected,
            rescan=result.rescan,
            files=files,
            layers=layers,
        )


# ── Helpers ──────────────────────────────────────────────────────────


def _to_relative(root: str, path: str) -> str:
    """Convert an absolute path to project-relative."""
    from pathlib import Path, PurePosixPath
    try:
        root_p = Path(root).resolve()
        path_p = Path(path).resolve()
        return str(PurePosixPath(path_p.relative_to(root_p)))
    except Exception:
        return path


def _ids_in_files(
    files: frozenset[str],
    graph: object | None,
) -> frozenset[str]:
    """Resolve the set of ``DurableId``s defined in *files*.

    Uses the graph's ``_file_to_nodes`` index.  Accepts both absolute
    paths (from SyncResult) and project-relative paths (from the graph).
    """
    if graph is None or not files:
        return frozenset()

    # The graph stores project-relative paths.  SyncResult gives absolute
    # paths.  Try both for lookup.
    ids: set[str] = set()
    try:
        for f in files:
            found = False
            # Direct lookup first.
            for idx in graph._file_to_nodes.get(f, []):  # type: ignore[union-attr]
                node = graph._graph[idx]  # type: ignore[union-attr]
                ids.add(node.durable_id)
                found = True
            if found:
                continue
            # Try extracting the relative suffix.  SyncResult absolute
            # paths look like /tmp/.../models.py.  Walk the file_to_nodes
            # keys to find a suffix match.
            for gfile, indices in graph._file_to_nodes.items():  # type: ignore[union-attr]
                if f.endswith("/" + gfile) or f == gfile:
                    for idx in indices:
                        node = graph._graph[idx]  # type: ignore[union-attr]
                        ids.add(node.durable_id)
                    break
    except Exception:
        pass
    return frozenset(ids)


def _compute_affected(
    seed: frozenset[str],
    graph: object | None,
) -> frozenset[str]:
    """Compute the transitive affected set (§4.3.3).

    ``affected = seed ∪ transitive_dependents(seed)``
    using the graph's reverse-dependency index.
    """
    if graph is None:
        return seed

    affected: set[str] = set(seed)
    try:
        for did in seed:
            deps = graph.transitive_dependents(did)  # type: ignore[union-attr]
            affected.update(deps)
    except Exception:
        pass

    return frozenset(affected)


def _resolve_files(
    ids: frozenset[str],
    graph: object | None,
) -> frozenset[str]:
    """Map a set of ``DurableId``s to their defining project-relative files.

    Uses the graph's node registry.  Falls back to an empty set if no
    graph is available.
    """
    if graph is None:
        return frozenset()

    files: set[str] = set()
    try:
        for did in ids:
            idx = graph._id_to_index.get(did)  # type: ignore[union-attr]
            if idx is not None:
                node = graph._graph[idx]  # type: ignore[union-attr]
                f = node.file
                if f:
                    files.add(f)
    except Exception:
        pass

    return frozenset(files)


def _resolve_layers_from_files(result: SyncResult, authored: frozenset[str]) -> frozenset[str]:
    """Resolve the set of layers touched by this revision.

    Every write touches the implicit ``"code"`` layer (unless it's a
    pure-authored write that only touches authored layers).  Authored
    writes touch their respective authored layer.
    """
    layers: set[str] = set()

    # Code/derived changes: always touch "code" at minimum.
    has_code_change = bool(
        result.created or result.changed or result.deleted
        or result.moved or result.rescan
    )
    if has_code_change:
        layers.add("code")

    # Authored writes touch their layer.
    if authored:
        layers.add("authored")

    return frozenset(layers)
