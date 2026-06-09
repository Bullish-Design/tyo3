"""RPC handlers — one method per JSON-RPC verb, against a real ``TyO3Session``.

Each handler is a thin wrapper over the engine API exercised in
``src/tyo3/demo/tour.py`` and ``src/tyo3/tests/test_final_acceptance.py`` (the
canonical, tested surface). Handlers run on whatever thread the server hands
them, but **every** session call is funnelled through the :class:`SessionActor`
so the session is only ever touched from its one owner thread.

The handlers translate between the wire (project-relative posix paths, 1-based
positions, JSON-able dicts) and the engine (``CommitDelta``, ``SymbolNode``,
``DerivedValue``, …). Nothing here mutates engine internals — it is a projection
layer.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tyo3.daemon.protocol import INVALID_PARAMS, METHOD_NOT_FOUND, ProtocolError
from tyo3.graph.identity import is_entity_durable_id

if TYPE_CHECKING:
    from tyo3 import TyO3Session
    from tyo3.daemon.session_actor import SessionActor
    from tyo3.daemon.tracking import AffectedTracker
    from tyo3.session import Snapshot


class Handlers:
    """The RPC method table, bound to one :class:`SessionActor`."""

    def __init__(self, actor: SessionActor, *, tracker: AffectedTracker | None = None) -> None:
        self._actor = actor
        self._root = Path(actor.root).resolve()
        self._session_id = hashlib.sha1(str(self._root).encode()).hexdigest()[:12]
        # Shared, thread-safe record of "what revision last affected each id",
        # populated by the bus pump; read by entity_at. Optional (absent in the
        # handler-only tests, present once the pump is wired).
        self._tracker = tracker

    # ── Dispatch ───────────────────────────────────────────────────

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        """Route *method* to its handler, returning a JSON-able result.

        Raises :class:`ProtocolError` (``METHOD_NOT_FOUND`` / ``INVALID_PARAMS``)
        for protocol faults; engine exceptions propagate to the server, which
        renders them as an ``ENGINE_ERROR`` response.
        """
        fn = _METHODS.get(method)
        if fn is None:
            raise ProtocolError(f"unknown method '{method}'", code=METHOD_NOT_FOUND)
        return fn(self, params)

    @property
    def methods(self) -> list[str]:
        return sorted(_METHODS)

    # ── Methods ────────────────────────────────────────────────────

    def ping(self, params: dict[str, Any]) -> dict[str, Any]:
        """Liveness + version probe for ``:checkhealth`` — confirms the actor
        is responsive and reports the engine version, head, and method table."""
        from tyo3 import __version__

        revision = self._actor.submit(lambda s: s.head)
        return {
            "ok": True,
            "engine_version": __version__,
            "root": str(self._root),
            "session_id": self._session_id,
            "revision": revision,
            "methods": self.methods,
        }

    def open(self, params: dict[str, Any]) -> dict[str, Any]:
        """Idempotent project open. The session is already opened and indexed by
        the actor; this returns its identity and current head."""

        def work(s: TyO3Session) -> dict[str, Any]:
            return {
                "session_id": self._session_id,
                "root": str(s.root),
                "revision": s.head,
                "files": [str(p) for p in s.files()],
                "precision": s.config.code_graph.precision,
                "layers": sorted(s.config.layers),
            }

        return self._actor.submit(work)

    def sync_buffer(self, params: dict[str, Any]) -> dict[str, Any]:
        """The editor write path: overlay *text* for *path* (no disk write),
        commit, and return the id-level :class:`CommitDelta`."""
        path = _require(params, "path", str)
        text = _require(params, "text", str)
        rel = self._relpath(path)

        def work(s: TyO3Session) -> dict[str, Any]:
            delta = s.edit(rel, text)
            return _commit_delta_dict(delta)

        return self._actor.submit(work)

    def sync_buffers(self, params: dict[str, Any]) -> dict[str, Any]:
        """Overlay many buffers **atomically** (one commit, one revision).

        The identity-preserving move path: removing a function from one file and
        adding its identical body to another in the *same* commit binds as a
        ``Moved`` (durable id + authored note ride along). Two separate
        ``sync_buffer`` calls would not guarantee that. Mirrors
        ``session.edit_many`` (see test_final_acceptance.py's atomic move)."""
        edits = params.get("edits")
        if not isinstance(edits, dict) or not edits:
            raise ProtocolError("'edits' must be a non-empty object", code=INVALID_PARAMS)
        rel_edits: dict[str, str] = {}
        for path, text in edits.items():
            if not isinstance(text, str):
                raise ProtocolError("each edit value must be a string", code=INVALID_PARAMS)
            rel_edits[self._relpath(path)] = text

        def work(s: TyO3Session) -> dict[str, Any]:
            delta = s.edit_many(rel_edits)
            return _commit_delta_dict(delta)

        return self._actor.submit(work)

    def entity_at(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve the entity under *(path, line, col)* (1-based) and return its
        cross-layer card, or ``null`` if nothing is there."""
        path = _require(params, "path", str)
        line = _require(params, "line", int)
        col = _require(params, "col", int)
        rel = self._relpath(path)

        def work(s: TyO3Session) -> dict[str, Any] | None:
            # Identity resolution is a live-registry op (id_for/locate hit the
            # native handle, not a snapshot). The *layer* reads then share one
            # snapshot (QW4) instead of `session.derived` opening a fresh one
            # per layer — one pin/unpin per cursor move, not one per layer.
            did = s.id_for(rel, line, col)
            if did is None:
                return None
            with s.snapshot() as snap:
                return self._entity_dict(s, snap, did, s.effective_layers)

        return self._actor.submit(work)

    def decorate(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Batch entity → annotation map for one file, for extmark placement.

        Walks the head graph for entity nodes whose ``file`` is *path*, joining
        the authored note layer and the derived summary layer."""
        path = _require(params, "path", str)
        rel = self._relpath(path)

        def work(s: TyO3Session) -> list[dict[str, Any]]:
            note_layer = self._note_layer(s)
            summary_layer = self._summary_layer(s)
            out: list[dict[str, Any]] = []
            # One snapshot for the whole file walk (QW4): the graph and every
            # note/summary read resolve against the same pinned revision.
            with s.snapshot() as snap:
                g = snap.graph()
                for idx in g._graph.node_indices():
                    node = g._graph[idx]
                    if node.file != rel or node.external or not is_entity_durable_id(node.durable_id):
                        continue
                    did = node.durable_id
                    item: dict[str, Any] = {
                        "durable_id": did,
                        "name": node.name,
                        "qualified_name": node.qualified_name,
                        "kind": node.kind.value,
                        "range": _range_dict(node.range),
                    }
                    if note_layer is not None:
                        note = self._read_note(snap, note_layer, did)
                        if note is not None:
                            item["note"] = note
                    if summary_layer is not None:
                        summary = self._read_summary(snap, summary_layer, did)
                        if summary is not None:
                            item["summary"] = summary
                    out.append(item)
            # Stable order: by start line then column — matches buffer order.
            out.sort(key=lambda d: (d["range"]["start"]["line"], d["range"]["start"]["column"]))
            return out

        return self._actor.submit(work)

    def author(self, params: dict[str, Any]) -> dict[str, Any]:
        """Author (write) a value for ``(layer, durable_id)`` — a real commit."""
        layer = _require(params, "layer", str)
        durable_id = _require(params, "durable_id", str)
        if "value" not in params:
            raise ProtocolError("missing 'value'", code=INVALID_PARAMS)
        value = params["value"]

        def work(s: TyO3Session) -> dict[str, Any]:
            delta = s.author(layer, durable_id, value)
            return {"revision": delta.revision, "durable_id": durable_id, "layer": layer}

        return self._actor.submit(work)

    def authored(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read the authored record for ``(layer, durable_id)`` at head."""
        layer = _require(params, "layer", str)
        durable_id = _require(params, "durable_id", str)

        def work(s: TyO3Session) -> dict[str, Any]:
            av = s.authored(layer, durable_id)
            return {
                "layer": av.layer,
                "durable_id": av.durable_id,
                "value": av.value,
                "status": av.status,
                "revision": av.revision,
            }

        return self._actor.submit(work)

    def locate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Resolve a DurableId to its current ``file::qualified_path``."""
        durable_id = _require(params, "durable_id", str)

        def work(s: TyO3Session) -> dict[str, Any]:
            loc = s.locate(durable_id)
            return {"durable_id": durable_id, "location": loc}

        return self._actor.submit(work)

    def diff(self, params: dict[str, Any]) -> dict[str, Any]:
        """Entity-level snapshot diff between ``from_rev`` and ``to_rev`` (or head)."""
        from_rev = _require(params, "from_rev", int)
        to_rev = params.get("to_rev")
        if to_rev is not None and not isinstance(to_rev, int):
            raise ProtocolError("'to_rev' must be an integer", code=INVALID_PARAMS)

        def work(s: TyO3Session) -> dict[str, Any]:
            before = s.snapshot(at=from_rev)
            try:
                after = s.snapshot(at=to_rev) if to_rev is not None else s.snapshot()
                try:
                    d = after.diff(before)
                    return {
                        "before_revision": d.before_revision,
                        "after_revision": d.after_revision,
                        "added": sorted(d.code.added),
                        "removed": sorted(d.code.removed),
                        "changed": sorted(d.code.changed),
                        "moved": sorted(d.code.moved),
                    }
                finally:
                    after.close()
            finally:
                before.close()

        return self._actor.submit(work)

    def derived(self, params: dict[str, Any]) -> dict[str, Any]:
        """Resolve a derived artifact for ``(layer, durable_id)`` at head."""
        layer = _require(params, "layer", str)
        durable_id = _require(params, "durable_id", str)

        def work(s: TyO3Session) -> dict[str, Any]:
            dv = s.derived(layer, durable_id)
            return {
                "layer": dv.layer,
                "durable_id": durable_id,
                "status": dv.status,
                "artifact": _artifact_str(dv.artifact),
                "revision": dv.revision,
            }

        return self._actor.submit(work)

    def reindex(self, params: dict[str, Any]) -> dict[str, Any]:
        """Full rescan (``sync_all``) — the overseer 'reindex' task."""

        def work(s: TyO3Session) -> dict[str, Any]:
            delta = s.sync_all()
            return _commit_delta_dict(delta)

        return self._actor.submit(work)

    def gc(self, params: dict[str, Any]) -> dict[str, Any]:
        """Evict orphaned derived artifacts (only ``gc = "orphans"`` layers)."""

        def work(s: TyO3Session) -> dict[str, Any]:
            s.gc()
            return {"ok": True, "revision": s.head}

        return self._actor.submit(work)

    def check(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run the type-checker; return diagnostics (whole project or one file)."""
        path = params.get("path")
        rel = self._relpath(path) if isinstance(path, str) else None

        def work(s: TyO3Session) -> dict[str, Any]:
            result = s.check_file(rel) if rel is not None else s.check()
            data = result.model_dump(mode="json")
            diags = data.get("diagnostics", data if isinstance(data, list) else [])
            return {"diagnostics": diags, "count": len(diags) if isinstance(diags, list) else 0}

        return self._actor.submit(work)

    # ── Navigation / analysis (the convert/ read surface) ──────────
    # These expose the already-built ``_ReadOps`` methods (read_ops.py) over
    # the wire. They are *reads only* — each runs on the actor over the
    # session's frozen head snapshot (golden rules #2/#3). Per Spike C, the
    # cheap-reverse data (references, diagnostics) is served **live**, never
    # cached as a layer.

    def references(self, params: dict[str, Any]) -> dict[str, Any]:
        """Find all references to the symbol at *(path, line, col)* (1-based).

        Returns the call sites/usages — the "find callers" surface (Spike E)."""
        path = _require(params, "path", str)
        line = _require(params, "line", int)
        col = _require(params, "col", int)
        include_decl = params.get("include_declaration", True)
        if not isinstance(include_decl, bool):
            raise ProtocolError("'include_declaration' must be a boolean", code=INVALID_PARAMS)
        rel = self._relpath(path)

        def work(s: TyO3Session) -> dict[str, Any]:
            return {
                "references": [
                    {"path": str(r.path), "range": _range_dict(r.range), "kind": r.kind.value}
                    for r in s.find_references(rel, line, col, include_decl)
                ]
            }

        return self._actor.submit(work)

    def document_highlights(self, params: dict[str, Any]) -> dict[str, Any]:
        """In-file occurrences of the symbol at *(path, line, col)* (1-based)."""
        path = _require(params, "path", str)
        line = _require(params, "line", int)
        col = _require(params, "col", int)
        rel = self._relpath(path)

        def work(s: TyO3Session) -> dict[str, Any]:
            return {
                "highlights": [
                    {"path": str(r.path), "range": _range_dict(r.range), "kind": r.kind.value}
                    for r in s.document_highlights(rel, line, col)
                ]
            }

        return self._actor.submit(work)

    def hover(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Hover (type/signature/docstring) for the symbol at *(path, line, col)*."""
        path = _require(params, "path", str)
        line = _require(params, "line", int)
        col = _require(params, "col", int)
        rel = self._relpath(path)

        def work(s: TyO3Session) -> dict[str, Any] | None:
            h = s.hover(rel, line, col)
            return h.model_dump(mode="json") if h is not None else None

        return self._actor.submit(work)

    def type_hierarchy(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Type hierarchy (supertypes/subtypes) for the class at *(path, line, col)*."""
        path = _require(params, "path", str)
        line = _require(params, "line", int)
        col = _require(params, "col", int)
        rel = self._relpath(path)

        def work(s: TyO3Session) -> dict[str, Any] | None:
            th = s.type_hierarchy(rel, line, col)
            return th.model_dump(mode="json") if th is not None else None

        return self._actor.submit(work)

    def can_rename(self, params: dict[str, Any]) -> dict[str, Any]:
        """Is the symbol at *(path, line, col)* renameable? Returns the editable range."""
        path = _require(params, "path", str)
        line = _require(params, "line", int)
        col = _require(params, "col", int)
        rel = self._relpath(path)

        def work(s: TyO3Session) -> dict[str, Any]:
            rng = s.can_rename(rel, line, col)
            return {
                "can_rename": rng is not None,
                "range": _range_dict(rng) if rng is not None else None,
            }

        return self._actor.submit(work)

    def rename(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Compute the workspace edit to rename the symbol at *(path, line, col)*.

        Returns ``{new_name, changes}`` where ``changes`` is
        ``{path: [{range, new_text}]}`` — the LSP-shaped edit the editor applies.
        This is the *read* (edit-computing) surface; it does **not** rebind
        identity (that is AB8). Positions are 1-based on the wire."""
        path = _require(params, "path", str)
        line = _require(params, "line", int)
        col = _require(params, "col", int)
        new_name = _require(params, "new_name", str)
        rel = self._relpath(path)

        def work(s: TyO3Session) -> dict[str, Any] | None:
            edit = s.rename(rel, line, col, new_name)
            if edit is None:
                return None
            changes: dict[str, list[dict[str, Any]]] = {}
            for e in edit.edits:
                changes.setdefault(str(e.path), []).append(
                    {"range": _range_dict(e.range), "new_text": edit.new_name}
                )
            return {"new_name": edit.new_name, "changes": changes}

        return self._actor.submit(work)

    def diagnostics_at(self, params: dict[str, Any]) -> dict[str, Any]:
        """Diagnostics from ``check_file`` whose range contains *(line, col)*.

        Cheap-reverse → served **live** (Spike C taxonomy); never cached."""
        path = _require(params, "path", str)
        line = _require(params, "line", int)
        col = _require(params, "col", int)
        rel = self._relpath(path)

        def work(s: TyO3Session) -> dict[str, Any]:
            result = s.check_file(rel)
            hits = [
                {
                    "message": d.message,
                    "severity": d.severity.value,
                    "code": d.code,
                    "range": _range_dict(d.range),
                }
                for d in result.diagnostics
                if d.range is not None and _range_contains(d.range, line, col)
            ]
            return {"diagnostics": hits, "count": len(hits)}

        return self._actor.submit(work)

    # ── Layer discovery (QW3 / QW7) ────────────────────────────────

    def layers(self, params: dict[str, Any]) -> dict[str, Any]:
        """Describe every declared layer, so the editor can author/render any
        layer without hardcoding ``intent``/``summary``/``docs``.

        Authored layers (``origin == "authored"``) are the writable ones — the
        editor offers an "Author …" entry only for those."""

        def work(s: TyO3Session) -> dict[str, Any]:
            out = [
                {
                    "name": name,
                    "origin": c.origin,
                    "entity_kinds": list(c.entity_kinds),
                    "history": c.history,
                    "review_on_change": c.review_on_change,
                    "serving": c.serving,
                    "key_locality": c.key_locality,
                    # ``display`` (QW5/AB1): registered spec's display, else a
                    # name heuristic (intent→inline-note, summary→inline-summary).
                    "display": s._display_for(name),
                }
                # The effective table (native ∪ registered) so a registered layer
                # is discoverable through the same verb (AB1).
                for name, c in s.effective_layers.items()
            ]
            out.sort(key=lambda d: d["name"])
            return {"layers": out}

        return self._actor.submit(work)

    def layer_ids(self, params: dict[str, Any]) -> dict[str, Any]:
        """The ids that have a record in *layer* — one snapshot, one actor hop
        (replaces the picker's per-id ``authored`` loop).

        With ``with_values: true`` also returns ``{id: value}`` so a picker can
        render labels without N follow-up reads. Uses **one shared snapshot**
        (golden rule #2)."""
        layer = _require(params, "layer", str)
        with_values = params.get("with_values", False)
        if not isinstance(with_values, bool):
            raise ProtocolError("'with_values' must be a boolean", code=INVALID_PARAMS)

        def work(s: TyO3Session) -> dict[str, Any]:
            with s.snapshot() as snap:
                view = snap.layer(layer)
                ids = sorted(view.ids())
                result: dict[str, Any] = {"layer": layer, "ids": ids}
                if with_values:
                    result["values"] = {did: _layer_value(view, did) for did in ids}
                return result

        return self._actor.submit(work)

    # ── Internal joins ─────────────────────────────────────────────

    def _entity_dict(self, s: TyO3Session, snap: Snapshot, did: str, layers: dict[str, Any]) -> dict[str, Any]:
        """The full per-entity card used by ``entity_at`` (and the inspector).

        Reads every *layer* off the one *snap* (a pinned :class:`Snapshot`) so
        the cross-layer join reflects a single revision (QW4). Identity
        (``locate``) is a live-registry read on the session, not the snapshot.
        ``layers`` is the open-fixed **effective** table (native ∪ registered),
        so a registered layer rides the card with no further wiring (AB1)."""
        node = _node_by_id(snap.graph(), did)
        card: dict[str, Any] = {
            "durable_id": did,
            "location": s.locate(did),
        }
        if node is not None:
            card.update(
                {
                    "name": node.name,
                    "qualified_name": node.qualified_name,
                    "kind": node.kind.value,
                    "file": node.file,
                    "range": _range_dict(node.range),
                    "content_hash": node.content_hash,
                }
            )
        # Authored records (every authored layer that has a record).
        authored: dict[str, Any] = {}
        for lname, lcfg in layers.items():
            if lcfg.origin != "authored":
                continue
            av = snap.authored(lname, did)
            if av.status != "absent":
                authored[lname] = {"value": av.value, "status": av.status, "revision": av.revision}
        card["authored"] = authored
        # Derived artifacts (every derived layer that applies to this kind).
        derived: dict[str, Any] = {}
        for lname, lcfg in layers.items():
            if lcfg.origin != "derived":
                continue
            if node is not None and lcfg.entity_kinds and node.kind.value not in lcfg.entity_kinds:
                continue
            dv = snap.derived(lname, did)
            if dv.status != "absent":
                derived[lname] = {"artifact": _artifact_str(dv.artifact), "status": dv.status}
        card["derived"] = derived
        # Last revision whose affected closure included this id (best-effort).
        if self._tracker is not None:
            card["last_affected_revision"] = self._tracker.last_affected(did)
        return card

    def _note_layer(self, s: TyO3Session) -> str | None:
        """The authored layer to render as an inline note (QW5).

        Picks the first authored layer whose ``display`` is ``inline-note``
        (a registered spec's declared display, or the ``intent`` name heuristic),
        so a registered layer opts into inline extmarks declaratively — no
        hardcoded ``intent``."""
        candidates = [
            n
            for n, c in s.effective_layers.items()
            if c.origin == "authored" and s._display_for(n) == "inline-note"
        ]
        if candidates:
            return "intent" if "intent" in candidates else sorted(candidates)[0]
        return None

    def _summary_layer(self, s: TyO3Session) -> str | None:
        """The derived layer to render as an inline summary (QW5).

        Picks the first derived layer whose ``display`` is ``inline-summary``."""
        candidates = [
            n
            for n, c in s.effective_layers.items()
            if c.origin == "derived" and s._display_for(n) == "inline-summary"
        ]
        if candidates:
            return "summary" if "summary" in candidates else sorted(candidates)[0]
        return None

    def _read_note(self, snap: Snapshot, layer: str, did: str) -> str | None:
        av = snap.authored(layer, did)
        if av.status == "absent" or av.value is None:
            return None
        return _note_text(av.value)

    def _read_summary(self, snap: Snapshot, layer: str, did: str) -> str | None:
        dv = snap.derived(layer, did)
        if dv.status in ("absent", "failed") or dv.artifact is None:
            return None
        return _artifact_str(dv.artifact)

    # ── Path translation ───────────────────────────────────────────

    def _relpath(self, path: str) -> str:
        """Project-relative posix path (graph nodes store these)."""
        p = Path(path)
        if p.is_absolute():
            try:
                return p.resolve().relative_to(self._root).as_posix()
            except ValueError:
                return p.as_posix()
        return p.as_posix()


# ── Serialisation helpers ────────────────────────────────────────────────────


def _require(params: dict[str, Any], key: str, typ: type) -> Any:
    if key not in params:
        raise ProtocolError(f"missing '{key}'", code=INVALID_PARAMS)
    val = params[key]
    # bool is an int subclass — reject it where an int is required.
    if typ is int and isinstance(val, bool):
        raise ProtocolError(f"'{key}' must be of type {typ.__name__}", code=INVALID_PARAMS)
    if not isinstance(val, typ):
        raise ProtocolError(f"'{key}' must be of type {typ.__name__}", code=INVALID_PARAMS)
    return val


def _range_dict(rng: Any) -> dict[str, Any]:
    """1-based ``{start:{line,column}, end:{line,column}}`` (TyO3 native convention)."""
    return {
        "start": {"line": rng.start.line, "column": rng.start.column},
        "end": {"line": rng.end.line, "column": rng.end.column},
    }


def _range_contains(rng: Any, line: int, col: int) -> bool:
    """Does *rng* (1-based, inclusive) contain the position *(line, col)*?"""
    start = (rng.start.line, rng.start.column)
    end = (rng.end.line, rng.end.column)
    return start <= (line, col) <= end


def _commit_delta_dict(delta: Any) -> dict[str, Any]:
    """Project a ``CommitDelta`` to the wire shape (id-level, JSON-able)."""
    return {
        "revision": delta.revision,
        "created_ids": list(delta.created_ids),
        "changed_ids": list(delta.changed_ids),
        "deleted_ids": list(delta.deleted_ids),
        "affected_ids": list(delta.affected_ids),
        "moved": [
            {"id": m.id, "old_file": m.old_file, "new_file": m.new_file, "new_qualified_path": m.new_qualified_path}
            for m in delta.moved
        ],
        "touched_files": list(delta.touched_files),
        "affected_files": list(delta.affected_files),
        "rescan": delta.rescan,
    }


def _layer_value(view: Any, durable_id: str) -> Any:
    """A JSON-able render of a layer view's per-id record (authored value or
    derived artifact), or ``None`` when absent."""
    v = view.value(durable_id)
    if v is None:
        return None
    if hasattr(v, "value"):  # AuthoredValue
        return v.value
    if hasattr(v, "artifact"):  # DerivedValue
        return _artifact_str(v.artifact)
    return v


def _node_by_id(graph: Any, durable_id: str) -> Any:
    idx = graph._id_to_index.get(durable_id)
    return graph._graph[idx] if idx is not None else None


def _artifact_str(artifact: Any) -> str | None:
    """Render a derived artifact (bytes | str | None) as a wire string."""
    if artifact is None:
        return None
    if isinstance(artifact, bytes):
        return artifact.decode("utf-8", errors="replace")
    return str(artifact)


def _note_text(value: Any) -> str:
    """Human-readable note text from an authored value.

    Notes authored by ``:TyO3Note`` are ``{"note": text}``; fall back to a
    string render for any other shape.
    """
    if isinstance(value, dict) and "note" in value:
        return str(value["note"])
    if isinstance(value, str):
        return value
    return str(value)


# Method table — name → bound function. Defined after the class so the
# functions resolve. Mirrors OVERVIEW.md §5.
_METHODS = {
    "ping": Handlers.ping,
    "open": Handlers.open,
    "sync_buffer": Handlers.sync_buffer,
    "sync_buffers": Handlers.sync_buffers,
    "entity_at": Handlers.entity_at,
    "decorate": Handlers.decorate,
    "author": Handlers.author,
    "authored": Handlers.authored,
    "locate": Handlers.locate,
    "diff": Handlers.diff,
    "derived": Handlers.derived,
    "reindex": Handlers.reindex,
    "gc": Handlers.gc,
    "check": Handlers.check,
    "references": Handlers.references,
    "document_highlights": Handlers.document_highlights,
    "hover": Handlers.hover,
    "type_hierarchy": Handlers.type_hierarchy,
    "can_rename": Handlers.can_rename,
    "rename": Handlers.rename,
    "diagnostics_at": Handlers.diagnostics_at,
    "layers": Handlers.layers,
    "layer_ids": Handlers.layer_ids,
}
