"""PrecisionRefiner — the async container→method affected-set narrowing worker.

Concept V2 §5.4 / Phase 9. For a committed revision ``R`` the refiner opens a
**frozen snapshot pinned at R** (never the live head — the salsa constraint,
V1 §3: a reader sharing the live db blocks/cancels the writer), re-resolves
member-access targets with the type-aware read surface, and **narrows** the
container-granular coarse ``affected`` set to the entities that actually depend
on the changed *members*. The narrowing rides the Phase-7 refinement channel,
separate from the primary revision-ordered delta stream.

Safety model
------------
* **Narrow only.** The published ``narrowed`` set is a **subset** of the coarse
  ``affected`` set and **always retains** ``changed_ids``. Expansion (adding
  non-nominal dependents) is explicitly out of scope for Phase 9.
* **Graceful degradation.** The coarse set is delivered synchronously on the
  primary channel *before* the refiner is ever fed. If the worker lags,
  raises, or never runs, that coarse set stays correct — a refinement is purely
  advisory precision. No refiner failure ever propagates to the writer or the
  primary delivery.

Precision boundary (Phase 9, resolved rigorously by Phase 10)
-------------------------------------------------------------
A method-body edit moves both the member's and its container's content hash
(subsumption), so a changed class with a changed member child is treated as
"changed by subsumption" and narrowed via that member's *precise* users. The
refinement is therefore **exact for method-body edits** — the common case. A
class that simultaneously changes its own structure *and* a member body is
narrowed by member users only, which can drop a pure-type referrer of that
class; this is the documented bound, made rigorous when Phase 10 lands the
container-subsumes-members hashing that lets us distinguish the two. Because the
coarse set is always delivered first, this boundary is a precision limit, never
a correctness one.
"""

from __future__ import annotations

import os
import queue
import threading
from typing import TYPE_CHECKING, Any

from tyo3.bus.refinement import AffectedRefinement
from tyo3.models.symbols import SymbolKind

if TYPE_CHECKING:
    from tyo3.models.analysis import Position, Range

# Sentinel pushed onto the queue to stop the daemon worker.
_STOP = object()


class PrecisionRefiner:
    """Background worker that narrows the coarse ``affected`` set to method
    precision and publishes an :class:`AffectedRefinement` per revision.

    Fed ``(revision, changed_ids, affected_ids)`` from the session's single
    post-commit hook *after* primary delivery. In ``async`` mode a daemon thread
    drains the queue and does the snapshot work off the commit hot path; in
    ``sync`` mode the same computation runs inline (opt-in; pays the
    per-occurrence resolution cost on the writer, but still only after the
    primary delta is published).
    """

    def __init__(self, session: Any, *, mode: str = "async") -> None:
        self._session = session
        self._mode = mode
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = False

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Start the daemon worker (async mode only; idempotent)."""
        if self._mode != "async" or self._started:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="tyo3-precision-refiner"
        )
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        """Stop the daemon worker, draining and joining (idempotent)."""
        if not self._started:
            return
        self._queue.put(_STOP)
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        self._started = False

    # ── Feed (called from session._after_commit, after primary publish) ──

    def feed(self, revision: int, changed_ids: list[str], affected_ids: list[str]) -> None:
        """Hand the refiner a committed revision's coarse delta.

        ``async`` ⇒ enqueue and return immediately (never blocks the writer).
        ``sync`` ⇒ compute and publish inline (opt-in).
        """
        if self._mode == "sync":
            self._compute_and_publish(revision, changed_ids, affected_ids)
            return
        self.start()
        self._queue.put((revision, list(changed_ids), list(affected_ids)))

    # ── Daemon loop ────────────────────────────────────────────────

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            revision, changed_ids, affected_ids = item
            try:
                self._compute_and_publish(revision, changed_ids, affected_ids)
            except Exception:
                # Graceful degradation: a refiner failure is never a miss —
                # the coarse set was already delivered on the primary channel.
                # Swallow and keep the worker alive for the next revision.
                pass

    # ── Core: narrow + publish ─────────────────────────────────────

    def _compute_and_publish(
        self, revision: int, changed_ids: list[str], affected_ids: list[str]
    ) -> None:
        # No bus / no subscribers ⇒ nothing to refine. Read the *already-built*
        # bus directly; never call _get_bus() (it would build a bus the writer
        # never asked for).
        bus = getattr(self._session, "_bus", None)
        if bus is None or not bus.has_subscribers():
            return
        if not affected_ids:
            return

        narrowed = self._compute_narrowed(revision, changed_ids, affected_ids)
        if narrowed is None:
            return
        bus.publish_refinement(
            AffectedRefinement(revision=revision, narrowed=frozenset(narrowed))
        )

    def _compute_narrowed(
        self, revision: int, changed_ids: list[str], affected_ids: list[str]
    ) -> set[str] | None:
        """Resolve member-access over a frozen snapshot at ``revision`` and
        return the narrowed affected set (subset of coarse ∪ ``changed_ids``).

        Returns ``None`` if the snapshot can't be pinned (e.g. the revision was
        evicted) — the caller then publishes nothing and the coarse set stands.
        """
        snap = None
        try:
            snap = self._session.snapshot(at=revision)
            root = snap._root
            g = snap.graph()

            changed = set(changed_ids)
            affected = set(affected_ids)

            # Members of a class (parent kind == CLASS). Member ids are bare
            # ULIDs, not ``parent::name`` — so parent kind, not string shape, is
            # the reliable test.
            members_changed: set[str] = set()
            for c in changed:
                parent = g.parent(c)
                if parent is not None and parent.kind == SymbolKind.CLASS:
                    members_changed.add(c)

            # Classes that are in `changed` and own a changed member: presumed
            # changed-by-subsumption ⇒ narrow via the member's precise users,
            # not the class's full container-granular referrer set.
            subsumption_classes: set[str] = set()
            for m in members_changed:
                parent = g.parent(m)
                if parent is not None and parent.durable_id in changed:
                    subsumption_classes.add(parent.durable_id)

            kept: set[str] = set(changed)  # always retain the directly-changed ids
            # Fan-out roots whose *dependents* are genuinely affected and must be
            # kept. Crucially this **excludes** subsumption-only classes: a class
            # is retained (it's in `changed`) but its full referrer set is NOT
            # re-expanded — that container-granular fan-out is exactly the
            # over-fire we're narrowing away. Only precise member-users and
            # genuine non-member changes seed the expansion.
            roots: set[str] = set()

            file_index = _build_file_index(g, root)

            # 1) Precise member-access users for each changed member.
            for m in members_changed:
                node = g.symbol(m)
                if node is None:
                    continue
                sel = node.selection_range or node.range
                try:
                    refs = snap.find_references(node.file, sel.start.line, sel.start.column)
                except Exception:
                    # Couldn't resolve precisely ⇒ fall back to the container's
                    # coarse dependents (sound, just less precise).
                    parent = g.parent(m)
                    if parent is not None:
                        roots.add(parent.durable_id)
                    continue
                for ref in refs:
                    owner = _enclosing_entity_id(file_index, g, str(ref.path), ref.range.start)
                    if owner is not None:
                        kept.add(owner)
                        roots.add(owner)

            # 2) Genuine non-member changes (top-level funcs, classes without a
            #    changed member, etc.) fan out at container granularity — they
            #    truly changed, so all their dependents stay affected.
            for c in changed - subsumption_classes - members_changed:
                roots.add(c)

            # 3) A dependent of any root is itself affected — expand transitively.
            #    `transitive_dependents` returns the full closure per node, so one
            #    pass over the roots is a fixpoint. Bounded below by `affected`.
            for r in roots:
                kept |= g.transitive_dependents(r)

            return (kept & affected) | changed
        except Exception:
            return None
        finally:
            if snap is not None:
                try:
                    snap.close()
                except Exception:
                    pass


# ── id ↔ position bridge helpers ───────────────────────────────────────────


def _build_file_index(g: Any, root: Any) -> dict[str, list]:
    """Map an **absolute, normalised** file path → its graph entity nodes.

    Graph nodes store *project-relative* files (``models.py``); the read surface
    returns *absolute* reference paths. Keying the index by the absolute path
    reconciles the two so a reference occurrence maps back to an entity id.
    """
    index: dict[str, list] = {}
    for i in g._graph.node_indices():
        node = g._graph[i]
        nf = node.file
        if not nf or nf in ("<external>", "<project>") or getattr(node, "external", False):
            continue
        key = os.path.normpath(os.path.join(str(root), nf))
        index.setdefault(key, []).append(node)
    return index


def _range_contains(rng: "Range", pos: "Position") -> bool:
    p = (pos.line, pos.column)
    return (rng.start.line, rng.start.column) <= p <= (rng.end.line, rng.end.column)


def _range_size(rng: "Range") -> tuple[int, int]:
    return (rng.end.line - rng.start.line, rng.end.column - rng.start.column)


def _enclosing_entity_id(
    file_index: dict[str, list], g: Any, abs_path: str, pos: "Position"
) -> str | None:
    """The durable id of the **tightest** entity whose range contains *pos*.

    A reference occurrence (``w.draw()`` inside ``consume_a``) maps to the entity
    that encloses it (``consume_a``). Falls back to the file's module entity when
    no finer entity contains the position (e.g. a module-level use site).
    """
    key = os.path.normpath(abs_path)
    nodes = file_index.get(key)
    if not nodes:
        return None
    best = None
    best_size: tuple[int, int] | None = None
    module_node = None
    for node in nodes:
        if node.kind == SymbolKind.MODULE:
            module_node = node
            continue
        if _range_contains(node.range, pos):
            size = _range_size(node.range)
            if best_size is None or size < best_size:
                best, best_size = node, size
    if best is not None:
        return best.durable_id
    if module_node is not None:
        return module_node.durable_id
    return None
