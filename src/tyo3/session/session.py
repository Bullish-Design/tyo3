"""``TyO3Session`` — the public, thin session facade.

Split from ``session.py`` (Phase 13). The facade owns the live native handle,
the post-commit hook (``_after_commit``), and the integration wiring (bus,
precision refiner, derived layers, watcher). It re-uses the shared read surface
from ``read_ops`` and hands back the read views from ``views``.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path as StdPath
from typing import Any

from tyo3.config import TyConfig
from tyo3.exceptions import (
    ConfigError,
    FormatVersionError,
    InternalTyError,
    ProjectClosedError,
    ProjectOpenError,
    RevisionEvictedError,
    TyO3Error,
)
from tyo3.models.authored import AuthoredValue
from tyo3.models.delta import CommitDelta
from tyo3.session._native import (
    _native,
    _NativeClosedError,
    _NativeConfigError,
    _NativeFormatVersionError,
    _NativeRevisionEvictedError,
)
from tyo3.session.read_ops import _ReadOps
from tyo3.session.views import LatestView, Snapshot, _OwnedView


class TyO3Session(_ReadOps):
    """A live session with the ty semantic engine for a single project root.

    Owns an internal handle to the ``ProjectDatabase`` via the PyO3 boundary.
    All data returned by Rust methods is already-native Python (dicts/lists
    via pythonize) and validated directly through Pydantic's ``model_validate``.

    Session read methods release the GIL during analysis, so they are
    non-blocking: a ``check()`` on one thread no longer freezes other threads
    or the event loop.

    Usage::

        with TyO3Session("/path/to/project") as session:
            result = session.check()
            symbols = session.document_symbols("src/main.py")
            definitions = session.goto_definition("src/main.py", 10, 5)
    """

    def __init__(self, root: str | StdPath) -> None:
        if _native is None:
            raise ProjectOpenError("Rust native extension is not built. Run `devenv shell -- build` first.")
        root_str = str(root)
        try:
            self._inner = _native.TyProject.open(root_str)
        except _NativeFormatVersionError as e:
            raise FormatVersionError(str(e)) from e
        except _NativeConfigError as e:
            raise ConfigError(str(e)) from e
        except Exception as e:
            raise ProjectOpenError(f"Cannot open project at '{root_str}': {e}") from e
        self._config = TyConfig.from_json(self._inner.config_json())
        self._root = StdPath(root_str).resolve()
        self._closed = False
        self._head_snap: Any = None  # cached native head snapshot (current revision)
        self._head_graph: Any = None  # lazily-built mutable CodeGraph for HEAD
        self._derivation: Any = None  # lazily-built DerivationDAG
        self._bus: Any = None  # lazily-built Bus (Gate 8)
        self._watcher_thread: Any = None  # auto-poll daemon thread
        self._watcher_stop: Any = None  # threading.Event for watcher stop
        self._refiner: Any = None  # lazily-built PrecisionRefiner (Phase 9)
        # Affected-set precision policy (Concept V2 §5.4). Read from the one
        # validated native config (single source) — not a second TOML parser.
        cg = self._config.code_graph
        self._precision_mode: str = cg.precision
        self._refinement_mode: str = cg.refinement
        from tyo3.sidecar import Sidecar

        self._sidecar = Sidecar(str(root_str))
        # Coordination settings come from the one validated native config
        # (single source; no second TOML parser, no silent default fallback).
        self._coord_cfg = self._config.coordination
        # Auto-start watcher if configured (Step 7).
        if self._coord_cfg.watcher_enabled:
            self._start_watcher_loop()

    @property
    def root(self) -> StdPath:
        return self._root

    @property
    def config(self) -> TyConfig:
        return self._config

    @property
    def head(self) -> int:
        """The current application revision."""
        self._check_open()
        return self._inner.head

    @property
    def latest(self) -> LatestView:
        """A floating, warm read view of the live HEAD (Phase 9).

        ``session.latest.check()`` reflects the newest revision, warm.
        Contrast ``session.snapshot()`` (pinned, cold, isolated).
        See :class:`LatestView`."""
        self._check_open()
        try:
            native_head_view = self._inner.head_view()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in latest: {e}") from e
        lv = LatestView(native_head_view, session=self)
        return lv

    @property
    def graph(self):
        """The live HEAD graph — a pure projection of the native code delta.

        Built (and rebuilt) by applying ``full_code_delta()`` to a fresh
        ``CodeGraph``: no read-surface walk, no identity priming, no session
        write. Reading it never advances ``head`` (§5.3 / §5.9).
        """
        self._check_open()
        if self._head_graph is None:
            self._rebuild_head_graph_from_native()
        return self._head_graph

    def _rebuild_head_graph_from_native(self) -> None:
        """(Re)build the live HEAD graph from a full native code delta.

        A pure projection: apply ``full_code_delta()`` (a full/``rescan`` delta
        over the current head state) to the live HEAD ``CodeGraph``. A rescan
        delta clears-and-rebuilds the graph **in place**, so the head-graph
        instance is stable across commits (callers may hold a reference to it);
        a fresh ``CodeGraph`` is allocated only on first materialisation. Shared
        by the lazy ``graph`` property and the post-commit rebuild branch (the
        deferred-producer path). Mutates no native state — ``full_code_delta()``
        is a pure read of the head.
        """
        from tyo3.graph import CodeGraph

        g = self._head_graph
        if g is None:
            g = CodeGraph()
            g._root = self._root
            self._head_graph = g
        g.apply_code_delta(self._inner.full_code_delta())
        # Read-only diagnostics refresh (check() is a read, never sync_all).
        g.refresh_diagnostics(self, root=self._root)

    def _head_graph_or_none(self) -> Any:
        """Return the live HEAD graph if materialized; never build it."""
        return self._head_graph

    def _get_derivation(self):
        """Lazily build the DerivationDAG from session config."""
        if self._derivation is None:
            from tyo3.derive.dag import DerivationDAG

            self._derivation = DerivationDAG.from_session(self)
        return self._derivation

    def _invalidate_derived(self, result: CommitDelta) -> None:
        """Drive derived invalidation from the id-level delta (§5.5, Phase 8).

        Feeds the loop **durable ids**, never the path-shaped metadata. The
        candidate set is the transitive, container-granular ``affected_ids``
        closure (which already subsumes ``changed`` ∪ ``created`` ∪ the
        reverse-dependents); ``created``/``changed`` are unioned in defensively
        so a delta that populates only those still invalidates. Deletions drop
        by durable id.
        """
        dag = self._get_derivation()
        if dag.is_empty:
            return
        dirty = set(result.affected_ids) | set(result.changed_ids) | set(result.created_ids)
        dag.invalidate(self, dirty, set(result.deleted_ids), result.revision)

    # ── Watcher lifecycle (Gate 8 Step 7) ──────────────────────────

    def _start_watcher_loop(self) -> None:
        """Start the file watcher and a daemon auto-poll thread.

        Called automatically if ``[coordination.watcher].enabled = true``.
        Idempotent — a second call stops the previous loop first.
        """
        import threading

        # Stop any existing loop.
        self._stop_watcher_loop()

        # Start the native watcher.
        self.watch()

        # Start the auto-poll daemon thread.
        self._watcher_stop = threading.Event()
        debounce = self._coord_cfg.watcher_debounce_ms / 1000.0

        def _poll_loop() -> None:
            while not self._watcher_stop.is_set():
                # Wait with interruptible sleep.
                if self._watcher_stop.wait(timeout=debounce):
                    break
                if self._closed:
                    break
                try:
                    self.poll_changes()
                    # poll_changes funnels through the one _after_commit hook
                    # (applies the graph delta + publishes) on a real commit.
                except Exception:
                    pass

        self._watcher_thread = threading.Thread(target=_poll_loop, daemon=True, name="tyo3-watcher")
        self._watcher_thread.start()

    def _stop_watcher_loop(self) -> None:
        """Stop the watcher auto-poll daemon thread (Step 7).

        No-op if the watcher was never started or the attributes
        don't exist (session constructed without __init__).
        """
        wt = getattr(self, "_watcher_thread", None)
        ws = getattr(self, "_watcher_stop", None)
        if wt is not None:
            if ws is not None:
                ws.set()
            if wt.is_alive():
                wt.join(timeout=2.0)
            self._watcher_thread = None
            self._watcher_stop = None
        # Also stop the native watcher.
        try:
            self.unwatch()
        except Exception:
            pass

    # ── Subscription bus (Gate 8) ──────────────────────────────────

    def _get_bus(self):
        """Lazily build the Bus from coordination config."""
        if self._bus is None:
            from tyo3.bus.bus import Bus

            self._bus = Bus(
                capacity=self._coord_cfg.bus_capacity,
                overflow=self._coord_cfg.bus_overflow,
            )
        return self._bus

    def subscribe(self, interest):
        """Register a subscriber on the delta subscription bus.

        Returns a ``Subscription`` that receives scoped deltas for
        every committed revision whose transitive affected set
        intersects *interest* (§12.2.1).
        """
        self._check_open()
        return self._get_bus().subscribe(interest)

    def _publish_delta(self, result: CommitDelta) -> None:
        """Publish a committed delta to the bus (the last step of the one
        post-commit hook, ``_after_commit``).

        Single-threaded writes serialised through the native commit guarantee
        revision order. Fast no-op when no subscribers are registered.
        """
        bus = self._bus
        if bus is None or not bus.has_subscribers():
            return
        from tyo3.bus.delta import Delta

        # The bus delta is a *pure projection* of the commit delta (§5.11): every
        # field — including the transitive ``affected`` closure and its
        # project-relative ``affected_files`` — is emitted natively by the
        # in-commit producer (Phase 6). The bus no longer depends on the
        # materialised head graph (Phase 7 deleted the Option-B bridge).
        delta = Delta.from_commit_delta(result)
        bus.publish(delta)

    # ── Head snapshot caching ───────────────────────────────────────

    def _native(self) -> Any:
        """A head snapshot pinned at the current revision, built lazily and
        reused across reads until the next mutation invalidates it. Because
        snapshots are independent (own Zalsa), holding it does NOT block a
        later edit."""
        self._check_open()
        if self._head_snap is None:
            self._head_snap = self._inner.snapshot(None)
        return self._head_snap

    def _invalidate_head_snap(self) -> None:
        """Drop the cached head snapshot reference after a mutation so the
        next read re-pins at the new revision.

        Does NOT forcibly close the old native snapshot — any in-flight read
        that already grabbed it may finish; Python drops it when the last
        reference is gone. This trades prompt cache cleanup for thread safety
        (Phase 5 §5.1)."""
        self._head_snap = None

    # ── Snapshot ─────────────────────────────────────────────────────

    def snapshot(self, at: int | None = None) -> Snapshot:
        """Pin an explicit MVCC snapshot. ``at=None`` pins the current
        revision; ``at=r`` time-travels to a still-retained revision."""
        self._check_open()
        try:
            native_snapshot = self._inner.snapshot(at)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except _NativeRevisionEvictedError as e:
            raise RevisionEvictedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in snapshot(): {e}") from e
        return Snapshot(
            native_snapshot,
            root=self._root,
            config=self._config,
            head_graph_getter=self._head_graph_or_none,
            derivation_getter=self._get_derivation,
        )

    # ── Write path ────────────────────────────────────────────────────

    def edit(self, path: str | StdPath, text: str) -> CommitDelta:
        """Overlay ``path`` with in-memory ``text`` (no disk write).
        Returns a SyncResult with the new revision and affected paths."""
        self._check_open()
        try:
            native_result = self._inner.edit(str(path), text)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in edit(): {e}") from e
        result = CommitDelta.model_validate(native_result)
        self._after_commit(result)
        return result

    def edit_many(self, edits: dict[str, str]) -> CommitDelta:
        """Overlay many files atomically (one publish, one revision)."""
        self._check_open()
        try:
            native_result = self._inner.edit_many(edits)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in edit_many(): {e}") from e
        result = CommitDelta.model_validate(native_result)
        self._after_commit(result)
        return result

    def edit_virtual(self, uri: str, text: str) -> CommitDelta:
        """Overlay a virtual/unsaved buffer (e.g. "untitled:1").
        No disk involvement."""
        self._check_open()
        try:
            native_result = self._inner.edit_virtual(uri, text)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in edit_virtual(): {e}") from e
        result = CommitDelta.model_validate(native_result)
        self._after_commit(result)
        return result

    def sync_path(self, path: str | StdPath) -> CommitDelta:
        """Ingest a disk change for ``path``: drop any overlay and re-read
        disk."""
        self._check_open()
        try:
            native_result = self._inner.sync_path(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in sync_path(): {e}") from e
        result = CommitDelta.model_validate(native_result)
        self._after_commit(result)
        return result

    def discard(self, path: str | StdPath) -> CommitDelta:
        """Drop the overlay buffer for ``path``, reverting to disk."""
        self._check_open()
        try:
            native_result = self._inner.discard(str(path))
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in discard(): {e}") from e
        result = CommitDelta.model_validate(native_result)
        # Routes through the one post-commit hook — so discard now publishes
        # like every other write (closes defect #6).
        self._after_commit(result)
        return result

    def sync_all(self) -> CommitDelta:
        """Rescan everything (in-place). Existing overlay buffers are
        preserved; ty re-walks and re-reads all files."""
        self._check_open()
        try:
            native_result = self._inner.sync_all()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in sync_all(): {e}") from e
        result = CommitDelta.model_validate(native_result)
        self._after_commit(result)
        return result

    # ── File watching (Phase 8) ────────────────────────────────────

    def watch(self) -> None:
        """Start observing the filesystem for changes under the project's
        watched paths. Observed changes are debounced by ty and queued; call
        :meth:`poll_changes` to fold them into HEAD. Idempotent: a second
        call replaces the watcher."""
        self._check_open()
        try:
            self._inner.watch()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in watch(): {e}") from e

    def unwatch(self) -> None:
        """Stop observing the filesystem. Pending unpolled events are
        discarded."""
        self._check_open()
        try:
            self._inner.unwatch()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in unwatch(): {e}") from e

    def flush_watch(self) -> None:
        """Prompt the watcher to emit any debounced batch now. Still
        asynchronous; follow with a short poll loop in tests."""
        self._check_open()
        try:
            self._inner.flush_watch()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in flush_watch(): {e}") from e

    def poll_changes(self) -> CommitDelta | None:
        """Drain every change the watcher has observed and fold it into HEAD
        as one revision. Returns the SyncResult, or None if nothing was
        pending (or every event was for a path with a live overlay buffer,
        which the buffer wins).

        Like the explicit write methods, this advances the revision and
        updates the live HEAD graph (if materialised)."""
        self._check_open()
        try:
            native_result = self._inner.poll_changes()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in poll_changes(): {e}") from e

        # A no-event poll commits nothing — return None and publish nothing
        # (correct, not a missed publish). The hook runs only on a real commit.
        if native_result is None:
            return None
        result = CommitDelta.model_validate(native_result)
        self._after_commit(result)
        return result

    def _inject_changes(self, changes: list[tuple[str, str]]) -> None:
        """Test seam: enqueue events as if the watcher observed them."""
        self._check_open()
        try:
            self._inner._inject_changes(changes)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in _inject_changes(): {e}") from e

    def _apply_graph_delta(self, result: CommitDelta) -> None:
        """Update the materialized HEAD graph from the native code delta (§5.3).

        The pure graph applier (Phase 4); derived invalidation is a separate
        post-commit step (``_schedule_derived``), not done here.

        An **authored-only** write (only ``authored_ids``, no code churn)
        touches no code/edge structure, so this is a no-op for it — that is what
        lets ``author`` route through the one ``_after_commit`` hook without
        mutating the code graph (§6.2).

        Three-state on ``result.code_delta`` (Phase 4):
          * ``None`` (absent) — no structural delta was computed this commit ⇒
            **rebuild** the head graph from a full native delta. This is the
            deferred-producer path and is taken on every materialised-graph
            code commit today.
          * present, empty — computed, nothing changed structurally (e.g. a
            whitespace-only edit) ⇒ a clean **no-op** apply.
          * present, populated — the incremental delta ⇒ **apply** it,
            revision-gated.
        """
        if self._head_graph is None:
            return
        # An authored write carries no code structure — never mutate the graph
        # (its code_delta is None like the deferred-producer rebuild path, so it
        # must be discriminated by its id shape, not by code_delta).
        if self._is_authored_only(result):
            return
        code_delta = result.code_delta
        if code_delta is None:
            self._rebuild_head_graph_from_native()
            return
        # Present delta — incremental apply, revision-gated. A revision *gap*
        # (the delta skips revisions) can't be applied incrementally, so rebuild
        # from a fresh full delta; the in-order case applies (a stale delta is an
        # internal no-op inside apply_code_delta).
        cur = self._head_graph.revision
        new_rev = code_delta.get("revision")
        if cur is not None and new_rev is not None and new_rev > cur + 1:
            self._rebuild_head_graph_from_native()
        else:
            self._head_graph.apply_code_delta(code_delta)

    @staticmethod
    def _is_authored_only(delta: CommitDelta) -> bool:
        """True for a pure authored write: it carries ``authored_ids`` but no
        code churn (no created/changed/deleted/moved ids and no rescan), so it
        must not mutate the code graph."""
        return bool(delta.authored_ids) and not (
            delta.created_ids or delta.changed_ids or delta.deleted_ids or delta.moved or delta.rescan
        )

    def _schedule_derived(self, delta: CommitDelta) -> None:
        """Schedule derived-layer invalidation from the id-level delta (§5.7).

        Phase 6 only *calls* the existing invalidation from the one post-commit
        hook; Phase 7 makes it precise (read-time staleness, snapshot lifetime).
        An authored-only write produces no dirty/deleted ids, so this is a
        no-op for it.
        """
        self._invalidate_derived(delta)

    def _after_commit(self, delta: CommitDelta) -> None:
        """The single post-commit path every write funnels through (§6.1/§6.3).

        Invalidate the head snapshot (so the next read re-pins at the new
        revision), apply the native code delta to the head graph, schedule
        derived invalidation, then publish to the bus — in that order. Because
        every write method calls exactly this, no path can diverge and every
        committed revision publishes (closes defect #6: ``discard`` forgetting
        to publish).
        """
        self._invalidate_head_snap()
        self._apply_graph_delta(delta)
        self._schedule_derived(delta)
        self._publish_delta(delta)
        # Async precision refinement (Phase 9) — strictly *after* primary
        # delivery, so the coarse set is always delivered first and the writer
        # never waits on precision. A no-op unless precision=method.
        self._maybe_refine(delta)

    def _get_refiner(self):
        """Lazily build the PrecisionRefiner (Phase 9, precision=method)."""
        if self._refiner is None:
            from tyo3.precision import PrecisionRefiner

            self._refiner = PrecisionRefiner(self, mode=self._refinement_mode)
        return self._refiner

    def _maybe_refine(self, delta: CommitDelta) -> None:
        """Feed the precision refiner when precision=method (§5.4).

        Defaults to ``container`` ⇒ the refiner is never built and the writer
        pays nothing. With ``method`` the coarse delta is handed to the refiner
        (async: enqueued; sync: computed inline) which narrows it and publishes
        an ``AffectedRefinement`` on the Phase-7 refinement channel.
        """
        if self._precision_mode != "method":
            return
        # Nothing to refine without a bus to publish to (and no subscribers
        # means a no-op publish anyway) — skip building the worker.
        bus = self._bus
        if bus is None or not bus.has_subscribers():
            return
        try:
            self._get_refiner().feed(delta.revision, delta.changed_ids, delta.affected_ids)
        except Exception:
            # Never let precision wiring surface as a write-path failure — the
            # coarse set was already published (graceful degradation).
            pass

    # ── Lifecycle ────────────────────────────────────────────────────

    def reload(self) -> None:
        """Reload the project, clearing cached diagnostics and re-scanning."""
        self._check_open()
        self._invalidate_head_snap()
        try:
            self._inner.reload()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in reload(): {e}") from e
        self._head_graph = None

    # ── Identity (Gate 2) ────────────────────────────────────────────

    def id_for(self, path: str, line: int, col: int) -> str | None:
        """Resolve the DurableId of the entity at (path, line, col).

        Returns None if no entity was found or no identity is registered.
        """
        self._check_open()
        try:
            return self._inner.id_for(path, line, col)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in id_for(): {e}") from e

    def locate(self, durable_id: str) -> str | None:
        """Locate the current file::qualified_path for a DurableId.

        Returns None if the id is not in the registry.
        """
        self._check_open()
        try:
            return self._inner.locate(durable_id)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in locate(): {e}") from e

    def needs_review(self) -> list[str]:
        """List durable ids currently flagged as NeedsReview."""
        self._check_open()
        try:
            return self._inner.needs_review()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in needs_review(): {e}") from e

    def orphaned(self) -> list[str]:
        """List durable ids currently flagged as Orphaned."""
        self._check_open()
        try:
            return self._inner.orphaned()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in orphaned(): {e}") from e

    def gc(self) -> None:
        """Explicit GC pass: evict orphaned derived artifacts (Step 9).

        Only layers with ``gc = "orphans"`` are affected; ``gc = "never"``
        (the default) keeps everything. GC is never run implicitly.
        """
        self._check_open()
        dag = self._get_derivation()
        if dag.is_empty:
            return
        dag.gc_orphans(self)

    # ── Derived (Gate 5) ───────────────────────────────────────────

    def derived(self, layer: str, durable_id: str) -> Any:
        """Read a derived value from the HEAD snapshot.

        Delegates to the head snapshot's Python-level derived method,
        not the native handle (which doesn't expose derived directly).
        """
        self._check_open()
        snap = self.snapshot()
        try:
            return snap.derived(layer, durable_id)
        finally:
            snap.close()

    def nearest(self, query_vector: list[float], k: int = 10, *, layer: str | None = None) -> list[tuple[str, float]]:
        """Nearest-neighbour search from the HEAD snapshot."""
        self._check_open()
        snap = self.snapshot()
        try:
            return snap.nearest(query_vector, k, layer=layer)
        finally:
            snap.close()

    def embedding(self, durable_id: str) -> Any:
        """Convenience: derived value from the 'embeddings' layer."""
        return self.derived("embeddings", durable_id)

    def docstring(self, durable_id: str) -> Any:
        """Convenience: derived value from the 'docstrings' layer."""
        return self.derived("docstrings", durable_id)

    # ── Authored (Gate 6) ───────────────────────────────────────────

    def author(self, layer: str, durable_id: str, value: Any) -> CommitDelta:
        """Author (write) an authored value for ``(layer, durable_id)``.

        An authored write is a real revision-producing commit: it bumps the
        revision, persists the record crash-safely, and returns a delta.
        Derived layers are unaffected — authored layers are sinks (§9.2.4).
        """
        self._check_open()
        payload = json.dumps(value)
        try:
            native = self._inner.author(layer, durable_id, payload)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except TyO3Error:
            # Native commit-transaction errors (CommitFailed / SidecarWriteError)
            # are already typed — surface them untouched, never re-wrap (§5.12).
            raise
        except Exception as e:
            raise InternalTyError(f"Unexpected error in author(): {e}") from e
        result = CommitDelta.model_validate(native)
        # Routes through the one post-commit hook like every other write; its
        # graph apply is a no-op (authored-only delta), so the code graph is
        # not mutated (§6.2).
        self._after_commit(result)
        return result

    def authored(self, layer: str, durable_id: str) -> AuthoredValue:
        """Read an authored value from the HEAD snapshot."""
        self._check_open()
        native = self._native()
        try:
            dto = native.authored(layer, durable_id)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in authored(): {e}") from e
        return AuthoredValue.model_validate(dto)

    def authored_needs_review(self, layer: str | None = None) -> list[str]:
        """List durable ids with authored records flagged `needs_review`.

        If `layer` is given, filters to that authored layer."""
        self._check_open()
        # Collect from most recent sync result (or derive from registry).
        # For the HEAD, we derive from the current state.
        ids = []
        for lname, lcfg in self.config.layers.items():
            if lcfg.origin != "authored":
                continue
            if layer is not None and lname != layer:
                continue
            if not lcfg.review_on_change:
                continue
            for nrid in self.needs_review():
                # Check if this id has an authored record in this layer.
                try:
                    val = self.authored(lname, nrid)
                    if val.status == "needs_review":
                        ids.append(nrid)
                except Exception:
                    # An id without a readable authored record in this layer
                    # simply doesn't contribute to the needs-review list.
                    pass
        return sorted(set(ids))

    def authored_orphaned(self, layer: str | None = None) -> list[str]:
        """List durable ids with authored records flagged `orphaned`.

        If `layer` is given, filters to that authored layer."""
        self._check_open()
        ids = []
        for lname, lcfg in self.config.layers.items():
            if lcfg.origin != "authored":
                continue
            if layer is not None and lname != layer:
                continue
            if not lcfg.review_on_change:
                continue
            for orph_id in self.orphaned():
                try:
                    val = self.authored(lname, orph_id)
                    if val.status == "orphaned":
                        ids.append(orph_id)
                except Exception:
                    pass
        return sorted(set(ids))

    # ── Convenience read sugar (owns / pins a fresh head snapshot) ──
    #
    # Each convenience read opens a fresh head snapshot. ``entity`` / ``diff``
    # build an eagerly-materialised frozen result and close the snapshot before
    # returning, so no lazy state escapes. ``code`` / ``layer`` return a *lazy*
    # layer view that reads the snapshot on demand, so the view owns the
    # snapshot (via :class:`_OwnedView`) and keeps it pinned for its lifetime.
    # No convenience read ever returns a view backed by an already-closed
    # snapshot (V1 §5.2 / deviation #7).

    @property
    def code(self):
        """A ``CodeLayerView`` over a fresh head snapshot the view **owns**.

        The returned view keeps its snapshot pinned for its lifetime — use it as
        a context manager (``with session.code as code: ...``), call ``close()``,
        or let GC release it. Reading it never advances ``head``.
        """
        self._check_open()
        snap = self.snapshot()
        try:
            return _OwnedView(snap, snap.code)
        except Exception:
            snap.close()
            raise

    def layer(self, name: str):
        """Dispatch to the right ``LayerView`` by *name* over an **owned** snapshot.

        The returned view owns its snapshot (see :meth:`code`). Raises
        ``KeyError`` if *name* is not a declared layer.
        """
        self._check_open()
        snap = self.snapshot()
        try:
            return _OwnedView(snap, snap.layer(name))
        except Exception:
            snap.close()
            raise

    def entity(self, durable_id: str):
        """An ``EntityView`` for *durable_id* at a fresh head snapshot.

        The cross-layer join is **eagerly materialised** against the snapshot
        before it is closed, so the returned frozen view holds no live snapshot
        state. Sugar for ``session.snapshot().entity(id)``.
        """
        self._check_open()
        with self.snapshot() as snap:
            return snap.entity(durable_id)

    def diff(self, before: Snapshot) -> Any:
        """A ``SnapshotDiff`` between *before* and a fresh head snapshot.

        ``SnapshotDiff.compute`` **eagerly materialises** every layer diff
        against both snapshots, so closing the after-snapshot before returning
        is safe. Sugar for ``Snapshot.diff(before)``.
        """
        self._check_open()
        with self.snapshot() as snap:
            return snap.diff(before)

    def close(self) -> None:
        """Close the project and free Rust-side resources.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._closed:
            return
        # Stop watcher auto-poll loop (Step 7).
        self._stop_watcher_loop()
        # Stop the precision refiner daemon (Phase 9) before tearing the bus
        # down, so no in-flight refinement races a closing bus.
        refiner = getattr(self, "_refiner", None)
        if refiner is not None:
            try:
                refiner.stop()
            except Exception:
                # Best-effort shutdown: a refiner that fails to stop cleanly must
                # not block session close.
                pass
            self._refiner = None
        # Close the bus (closes all subscriptions).
        bus = getattr(self, "_bus", None)
        if bus is not None:
            bus.close()
            self._bus = None
        self._invalidate_head_snap()
        self._head_graph = None
        self._inner.close()
        self._closed = True

    def __enter__(self) -> TyO3Session:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        # Guard against interpreter shutdown — if the native module is already
        # unloaded, self._inner will be None and we must not call into Rust.
        if getattr(self, "_inner", None) is None:
            return
        if not getattr(self, "_closed", True):
            warnings.warn(
                "TyO3Session was not closed explicitly. Use 'with TyO3Session(...)' or call session.close().",
                ResourceWarning,
                stacklevel=2,
            )
            try:
                self.close()
            except Exception:
                pass


# ── Snapshot ────────────────────────────────────────────────────────────────
