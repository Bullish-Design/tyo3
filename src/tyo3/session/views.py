"""Read views over a session: ``_OwnedView`` (snapshot-owning convenience view),
``Snapshot`` (immutable, revision-pinned), and ``LatestView`` (floating warm
view of live HEAD).

Split from ``session.py`` (Phase 13). These are read-only projections; they
reference ``TyO3Session`` only in docstrings, so there is no import cycle back
to the facade.
"""

from __future__ import annotations

import warnings
from pathlib import Path as StdPath
from typing import Any

from tyo3.config import TyConfig
from tyo3.exceptions import InternalTyError
from tyo3.models.authored import AuthoredValue, AuthoredVersion
from tyo3.models.derived import DerivedValue
from tyo3.session.read_ops import _ReadOps


class _OwnedView:
    """A convenience layer view (``session.code`` / ``session.layer(...)``) that
    **owns** the head snapshot it reads over.

    ``session.code`` / ``session.layer`` open a fresh head snapshot and build a
    *lazy* layer view (one that reads the snapshot on demand). This wrapper holds
    that snapshot pinned for the view's lifetime and closes it on context-manager
    exit, explicit :meth:`close`, or garbage collection — so the returned view is
    never backed by an already-closed snapshot (V1 §5.2, deviation #7). Views
    taken directly from a :class:`Snapshot` (``snap.code`` / ``snap.layer``) are
    unaffected: there the ``Snapshot`` owns its own lifetime.

    The wrapper deliberately sits *outside* the ``Snapshot``↔view reference cycle
    (a ``Snapshot`` caches its views and each view refers back to the
    ``Snapshot``), so it is reclaimed by reference counting and releases its
    pinned snapshot deterministically — no leak, no spurious ``ResourceWarning``.
    It never closes the snapshot before handing the view back (the close-in-the-
    wrong-scope footgun this phase removes).
    """

    def __init__(self, snapshot: Any, view: Any) -> None:
        self._snapshot = snapshot
        self._view = view
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        # Reached only for attributes absent on the wrapper itself → delegate to
        # the wrapped view. Guard the private names so a partially constructed
        # wrapper raises ``AttributeError`` rather than recursing.
        if name in ("_snapshot", "_view", "_closed"):
            raise AttributeError(name)
        return getattr(self._view, name)

    def close(self) -> None:
        """Release the owned snapshot. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        self._snapshot.close()

    def __enter__(self) -> _OwnedView:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass




class Snapshot(_ReadOps):
    """Immutable, revision-pinned, thread-shareable read view of a project.

    Created via :meth:`TyO3Session.snapshot`. Exposes every read method a
    session does, but no ``reload()``. All reads see the project exactly as it
    was when the snapshot was taken, regardless of later session reloads, and a
    single snapshot is safe to share across threads.
    """

    def __init__(
        self,
        native_snapshot: Any,
        *,
        root: StdPath,
        config: TyConfig | None = None,
        head_graph_getter: Any | None = None,
        derivation_getter: Any | None = None,
    ) -> None:
        self._inner = native_snapshot
        self._closed = False
        self._root = root
        self._config = config
        self._head_graph_getter = head_graph_getter
        self._derivation_getter = derivation_getter
        self._graph: Any = None
        self._code_view: Any = None
        self._layer_views: dict[str, Any] = {}

    def _native(self) -> Any:
        self._check_open()
        return self._inner

    @property
    def revision(self) -> int:
        """The revision this snapshot is pinned to."""
        self._check_open()
        return self._inner.revision

    @property
    def code(self):
        """A ``CodeLayerView`` pinned at this snapshot's revision."""
        if self._code_view is None:
            from tyo3.layers.code import CodeLayerView
            self._code_view = CodeLayerView(self)
        return self._code_view

    def layer(self, name: str):
        """Dispatch to the right ``LayerView`` by *name* via config origin.

        Raises ``KeyError`` if *name* is not a declared layer.
        """
        if name == "code":
            return self.code
        if name in self._layer_views:
            return self._layer_views[name]
        if self._config is None:
            raise KeyError(f"No config available — cannot resolve layer '{name}'")
        layer_cfg = self._config.layers.get(name)
        if layer_cfg is None:
            raise KeyError(f"Layer '{name}' is not declared in config")
        if layer_cfg.origin == "derived":
            from tyo3.layers.derived import DerivedLayerView
            view = DerivedLayerView(self, name, layer_cfg)
        elif layer_cfg.origin == "authored":
            from tyo3.layers.authored import AuthoredLayerView
            view = AuthoredLayerView(self, name, layer_cfg)
        else:
            raise KeyError(f"Layer '{name}' has unknown origin '{layer_cfg.origin}'")
        self._layer_views[name] = view
        return view

    def entity(self, durable_id: str):
        """Return an ``EntityView`` — the cross-layer join for *durable_id* at this revision.

        Members: ``code``, ``content_hash``, ``location``, ``status``,
        ``derived``, ``authored`` — all describing the pinned revision.
        """
        from tyo3.models.view import EntityView
        return EntityView.from_snapshot(self, durable_id)

    def embedding_drift(self, before: Snapshot) -> Any:
        """Convenience: derived drift from the 'embeddings' layer.

        Sugar for ``self.layer("embeddings").diff(before.layer("embeddings"))``.
        Raises ``KeyError`` if no embeddings layer is configured.
        """
        return self.layer("embeddings").diff(before.layer("embeddings"))

    def diff(self, before: Snapshot) -> Any:
        """Return a ``SnapshotDiff`` — the combined, id-keyed diff across all layers.

        *before* must be a snapshot from the same session; *self* is *after*.
        Raises ``ValueError`` when snapshots are from incompatible sessions.
        """
        from tyo3.models.diff import SnapshotDiff
        return SnapshotDiff.compute(self, before)

    def graph(self):
        """Return an immutable CodeGraph pinned at this snapshot's revision.

        Built by applying the snapshot's **own frozen-database** native code
        delta (4.4) to a fresh CodeGraph — no read-surface walk, no session
        reference, no identity priming. The result is consistent with the
        snapshot's pinned revision and mutates no session state.
        """
        self._check_open()
        if self._graph is not None:
            return self._graph

        # Fast path: if the live HEAD graph is already materialised at exactly
        # this revision, pin a copy of it (avoids recomputing the frozen delta).
        head_graph = self._head_graph_getter() if self._head_graph_getter is not None else None
        if head_graph is not None and head_graph.revision == self.revision:
            self._graph = head_graph._pin_at(self.revision)
            return self._graph

        from tyo3.graph import CodeGraph

        g = CodeGraph()
        g._root = self._root
        g.apply_code_delta(self._inner.full_code_delta())  # frozen-db delta (4.4)
        g.refresh_diagnostics(self, root=self._root)  # read-only (snapshot.check())
        self._graph = g._pin_at(self.revision)
        return self._graph

    def derived(self, layer: str, durable_id: str) -> DerivedValue:
        """Resolve a derived value for *durable_id* under *layer* at this revision.

        Content-addressed: the artifact is looked up by the entity's content
        hash at this revision, so the result is exact for R.

        Honest staleness (§8.2.5):
        - Fresh: artifact matches entity content at R.
        - Stale: last-good artifact served (default policy).
        - Failed: generator failed, prior artifact (if any) served.
        - Absent: no artifact ever produced.
        """
        self._check_open()
        if self._derivation_getter is None:
            return DerivedValue(
                artifact=None, status="absent", revision=self.revision, layer=layer
            )
        dag = self._derivation_getter()
        if dag.is_empty:
            return DerivedValue(
                artifact=None, status="absent", revision=self.revision, layer=layer
            )
        L = dag.layer(layer)
        try:
            gen_input, input_hash = dag.resolve_input(L, self, durable_id)
        except (KeyError, AttributeError):
            # The entity does not resolve at this revision (deleted, or never
            # reconciled to this snapshot) → nothing to serve.
            return DerivedValue(
                artifact=None, status="absent", revision=self.revision, layer=layer
            )
        key = L.keys_for(input_hash)
        art = L.cache.get(key)
        if art is not None:
            # Present at the resolved key ⇒ fresh. Read-time staleness is a pure
            # key comparison (§8.3): the content-addressed key already encodes
            # the entity content (and, for semantic layers, its dependency
            # closure), so a move-unchanged hit serves the reused artifact.
            return DerivedValue(artifact=art, status="fresh", revision=self.revision, layer=layer)

        # Miss at the resolved key. Self-heal with a synchronous recompute over
        # this pinned snapshot (§8.3). With no async worker in Phase 8 this
        # serves both `block` and `stale` layers; the serving policy is honoured
        # on failure, where we fall back to the last-good artifact tagged
        # honestly. The async refinement path is Phase 9.
        scheduler = dag._get_scheduler()
        art = scheduler.recompute_now(dag, L, self, durable_id)
        if art is not None:
            return DerivedValue(artifact=art, status="fresh", revision=self.revision, layer=layer)

        # Recompute failed (or produced nothing) → serve last-good honestly.
        last_key = L.last_good_store_key(durable_id)
        if last_key:
            last_art = L.cache._store.get(last_key)
            if last_art is not None:
                status = "failed" if durable_id in L._failed else "stale"
                return DerivedValue(artifact=last_art, status=status, revision=self.revision, layer=layer)
        # No last-good. A recorded generator failure is reported as `failed`
        # (artifact None); a genuine never-produced artifact is `absent`.
        if durable_id in L._failed:
            return DerivedValue(artifact=None, status="failed", revision=self.revision, layer=layer)
        return DerivedValue(artifact=None, status="absent", revision=self.revision, layer=layer)

    def nearest(
        self, query_vector: list[float], k: int = 10, *, layer: str | None = None
    ) -> list[tuple[str, float]]:
        """Nearest-neighbour search against a vector-backed derived layer.

        Returns list of ``(durable_id, score)`` for entities with the nearest
        vectors at this revision.
        """
        self._check_open()
        if self._derivation_getter is None:
            return []
        dag = self._derivation_getter()
        if dag.is_empty:
            return []

        # Default to the first vector-backed layer.
        if layer is None:
            for name in dag._topo_order:
                if name in dag._layer_map:
                    L = dag._layer_map[name]
                    if hasattr(L.cache._store, "nearest"):
                        layer = name
                        break
        if layer is None:
            return []

        L = dag.layer(layer)
        store = L.cache._store
        if not hasattr(store, "nearest"):
            return []

        # Query the vector backend.
        results: list[tuple[str, float]] = store.nearest(query_vector, k)  # type: ignore[union-attr]
        if not results:
            return []

        # Map input_hash keys back to DurableIds at this revision.
        g = self.graph()
        hash_to_ids: dict[str, list[str]] = {}
        for node_idx in g._graph.node_indices():
            node = g._graph[node_idx]
            for profile, h in node.content_hashes.items():
                hash_to_ids.setdefault(h, []).append(node.durable_id)

        mapped: list[tuple[str, float]] = []
        seen: set[str] = set()
        for store_key, score in results:
            # Store key is "input_hash:version" — extract input_hash.
            input_hash = store_key.split(":")[0] if ":" in store_key else store_key
            ids = hash_to_ids.get(input_hash, [])
            for did in ids:
                if did not in seen:
                    seen.add(did)
                    mapped.append((did, score))

        return mapped

    def embedding(self, durable_id: str) -> DerivedValue:
        """Convenience: derived value from the 'embeddings' layer."""
        return self.derived("embeddings", durable_id)

    def docstring(self, durable_id: str) -> DerivedValue:
        """Convenience: derived value from the 'docstrings' layer."""
        return self.derived("docstrings", durable_id)

    def authored(self, layer: str, durable_id: str) -> AuthoredValue:
        """Resolve an authored value at this snapshot's pinned revision.

        Status is honest (§10.2.3): absent, present, needs_review, or orphaned —
        derived from the snapshot-captured identity registry.
        """
        self._check_open()
        dto = self._inner.authored(layer, durable_id)
        return AuthoredValue.model_validate(dto)

    def authored_history(self, layer: str, durable_id: str) -> list[AuthoredVersion]:
        """Return the full version history for an authored record.

        Returns all versions (history + current) with revision ≤ this
        snapshot's pinned revision, ordered by revision ascending.
        Empty list if the record doesn't exist.
        """
        self._check_open()
        dtos = self._inner.authored_history(layer, durable_id)
        return [AuthoredVersion.model_validate(d) for d in dtos]

    def close(self) -> None:
        """Release the pinned revision. Safe to call multiple times."""
        if self._closed:
            return
        self._inner.close()
        self._graph = None
        self._closed = True

    def __enter__(self) -> Snapshot:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        if getattr(self, "_inner", None) is None:
            return
        if not getattr(self, "_closed", True):
            warnings.warn(
                "Snapshot was not closed explicitly. Use 'with session.snapshot()' or call snapshot.close().",
                ResourceWarning,
                stacklevel=2,
            )
            try:
                self.close()
            except Exception:
                pass


# ── LatestView — floating warm reads (Phase 9) ────────────────────────────


class LatestView(_ReadOps):
    """A floating, warm read view of the live HEAD.

    Every read reflects the *newest* HEAD revision (including edits made after
    this view was obtained) and reuses the type-checker's warm memos, so it is
    faster than a cold snapshot for "what is the current state?" glances.
    Internally each read is retried if a concurrent write cancels it, so
    callers never see a cancellation.

    Tradeoff (architecture §7): because a floating read shares HEAD's storage,
    it can briefly delay a concurrent write (until the read notices the write
    and retries). For isolated, repeatable reads that never perturb the
    writer, use :meth:`TyO3Session.snapshot` instead — distinct by intent.

    Honest boundary: cross-layer consistency and diff require a pinned
    revision, so ``entity()`` and ``diff()`` are **not** available here.
    Use :meth:`TyO3Session.snapshot` for consistent multi-layer joins.
    """

    def __init__(self, native_head_view: Any, session: Any = None) -> None:
        self._inner = native_head_view
        self._closed = False
        self._session = session  # weak reference for derived/authored warm reads

    def _native(self) -> Any:
        self._check_open()
        return self._inner

    def graph(self):
        """A **non-canonical, floating** code-graph projection of the live HEAD.

        Returns an *immutable, point-in-time copy* pinned at the current head
        revision — deliberately **not** the canonical mutable HEAD graph. Each
        call re-pins the newest head (the floating contract), and the returned
        graph never mutates under the caller. A floating view must not hand out a
        mutable reference to the canonical graph (Phase 11.2); for the canonical
        maintained head graph use ``session.graph``, and for a consistent pinned
        graph use ``session.snapshot().graph()``. Reading it never advances head.
        """
        self._check_open()
        if self._session is None:
            raise InternalTyError("No session reference for latest graph")
        head_graph = self._session.graph
        return head_graph._pin_at(self._session.head)

    def derived(self, layer: str, durable_id: str) -> Any:
        """Warm derived read against the live HEAD. Cancellation-retried.

        Resolves through the session's head snapshot (consistent, not
        floating) — each call is internally consistent but two successive
        calls may straddle a write.
        """
        self._check_open()
        if self._session is not None:
            return self._session.derived(layer, durable_id)
        raise InternalTyError("No session reference for latest derived read")

    def authored(self, layer: str, durable_id: str) -> Any:
        """Warm authored read against the live HEAD. Cancellation-retried."""
        self._check_open()
        if self._session is not None:
            return self._session.authored(layer, durable_id)
        raise InternalTyError("No session reference for latest authored read")

    # No close()/revision/entity/diff: a LatestView pins nothing and has no
    # fixed revision (it floats). It is valid as long as the owning session
    # is open. entity() and diff() are NOT exposed — the honest boundary.
