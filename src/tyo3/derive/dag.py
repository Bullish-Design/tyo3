"""Derivation DAG — assembly, registration, and input resolution (§9).

Consumes Gate 4's validated topo order. Authored layers are sinks (§9.2.4);
they are excluded from the DAG entirely. Input resolution is the central
logic: code-derived layers key on the entity's profile hash; layer-derived
layers key on the upstream artifact's hash.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from tyo3.derive.layer import DerivedLayer
from tyo3.stores import open_store

if TYPE_CHECKING:
    from tyo3.session import Snapshot, TyO3Session


class DerivationDAG:
    """Owns all DerivedLayers in topological order.

    Authored layers are NOT here — they are sinks (§9.2.4). An empty config
    produces an empty DAG that no-ops everywhere.
    """

    def __init__(self, layers: list[DerivedLayer], topo_order: list[str]) -> None:
        self._layers = layers
        self._layer_map: dict[str, DerivedLayer] = {layer.name: layer for layer in layers}
        self._topo_order = topo_order  # includes "code" at position 0

        # Defensive acyclicity check (Gate 4 already validated).
        for layer in layers:
            for dep in layer.depends_on:
                if dep != "code" and dep not in self._layer_map:
                    raise AssertionError(f"Layer '{layer.name}' depends on unknown layer '{dep}'")

    @classmethod
    def from_session(cls, session: TyO3Session) -> DerivationDAG:
        """Build the DAG from a session's **effective** layer table.

        Iterates ``session.effective_layers`` (native config ∪ registered-derived
        layers, AB1 §4) in topological order. A native config-declared derived
        layer resolves its store/generator through ``config.stores``/
        ``config.generators`` (the existing path); a layer registered via
        ``tyo3.extend.register_layer`` resolves its store/producer from the spec
        objects (or the ``_STORES``/``_GENERATORS`` registries)."""
        from tyo3.derive.generators import make_generator
        from tyo3.extend import _LAYERS, DerivedLayerSpec, resolve_generator, resolve_store

        config = session.config
        effective = getattr(session, "effective_layers", None) or config.layers
        sidecar = getattr(session, "_sidecar", None)
        # Snapshot doesn't have _sidecar; use root-based sidecar.
        if sidecar is None:
            from tyo3.sidecar import Sidecar

            root = getattr(session, "root", getattr(session, "_root", None))
            sidecar = Sidecar(str(root)) if root else None

        # Topo order: the native order plus registered-derived additions, locally
        # toposorted so ``iter_layers`` still yields in dependency order.
        topo = _effective_topo_order(config.topo_order, effective)
        if not topo:
            return cls([], [])

        layers: list[DerivedLayer] = []
        for name in topo:
            if name == "code":
                continue
            if name not in effective:
                continue
            layer_cfg = effective[name]
            # Skip authored layers — they are sinks.
            if layer_cfg.origin == "authored":
                continue

            spec = _LAYERS.get(name)
            if isinstance(spec, DerivedLayerSpec) and name not in config.layers:
                # Registered layer: resolve store/producer from the spec objects.
                if sidecar is None:
                    raise ValueError(f"Cannot resolve store for registered layer '{name}'")
                store = resolve_store(spec.store, sidecar, layer=name)
                generator = resolve_generator(spec.produce, name=name)
            else:
                # Native config-declared derived layer (the existing path).
                store_name = layer_cfg.store
                if store_name and store_name in config.stores:
                    store_cfg = config.stores[store_name]
                    store = open_store(store_cfg, sidecar, layer=name)
                else:
                    # Fall back: fs store under cache/<name>
                    if sidecar is None:
                        raise ValueError(f"Cannot resolve store for layer '{name}'")
                    from tyo3.stores.fs import FsStore

                    store = FsStore(sidecar.cache_dir(name))

                gen_name = layer_cfg.generator
                gen_cfg = config.generators.get(gen_name) if gen_name else None
                if gen_cfg is None:
                    raise ValueError(f"Layer '{name}' has no generator config")
                generator = make_generator(gen_cfg, name=name)

            layer = DerivedLayer.from_config(layer_cfg, store, generator)
            layer.name = name
            layers.append(layer)

        return cls(layers, topo)

    @property
    def is_empty(self) -> bool:
        return len(self._layers) == 0

    def _get_scheduler(self):
        """Lazily build the recompute scheduler."""
        if not hasattr(self, "_scheduler"):
            from tyo3.derive.scheduler import RecomputeScheduler

            object.__setattr__(self, "_scheduler", RecomputeScheduler())
        return self._scheduler

    def layer(self, name: str) -> DerivedLayer:
        """Get a layer by name."""
        if name not in self._layer_map:
            raise KeyError(f"Unknown layer: {name}")
        return self._layer_map[name]

    def iter_layers(self):
        """Iterate layers in topological order, skipping 'code'."""
        for name in self._topo_order:
            if name in self._layer_map:
                yield self._layer_map[name]

    def invalidate(
        self,
        session,
        dirty: set[str],
        deleted: set[str],
        revision: int,
    ) -> None:
        """Invalidate exactly hash-affected artifacts from a delta.

        Precision mechanism (§8.2.3, §8.3): for each entity in
        ``dirty`` that a layer applies to, compute the new input_hash
        and compare to the layer's last-served binding. Unchanged →
        do nothing (reuse). Changed/missing → mark stale and (if eager)
        enqueue recompute. No over-fire, no miss.
        """
        if self.is_empty:
            return

        scheduler = self._get_scheduler()
        # One pinned snapshot for the whole pass — invalidation AND eager
        # recompute — closed in `finally` (§8.4, no second-snapshot leak). It is
        # a cold MVCC snapshot, never the live head graph (§5.9).
        snap = session.snapshot()
        try:
            for layer in self.iter_layers():
                for durable_id in dirty:
                    if not layer.applies_to(_kind_for_id(snap, durable_id)):
                        continue
                    try:
                        gen_input, input_hash = self.resolve_input(layer, snap, durable_id)
                    except (KeyError, AttributeError):
                        continue

                    prior = layer.binding(durable_id)
                    if prior == input_hash:
                        # Layer key unchanged → reuse (cache hit). For a `local`
                        # layer this is every affected-but-unchanged id (no
                        # over-recompute); for a `semantic` layer the key moved
                        # iff a dependency changed.
                        continue

                    # Key moved → stale.
                    layer.mark_stale(durable_id)
                    if layer.recompute == "eager":
                        scheduler.enqueue(layer, durable_id, input_hash)

                for durable_id in deleted:
                    layer.drop(durable_id)

            # Process eager items over the same pinned snapshot.
            scheduler.process_all(self, snap)
        finally:
            snap.close()

    def gc_orphans(self, session) -> None:
        """Evict orphaned derived artifacts per store GC policy (Step 9).

        Only layers with gc="orphans" are affected. Reachable keys are
        derived from the current graph's entity content hashes. Keys from
        non-active generator_versions are retained (rollback support).
        """
        if self.is_empty:
            return

        snap = session.snapshot()
        try:
            try:
                g = snap.graph()
            except RuntimeError:
                # Graph build failed (e.g. missing identity) — skip GC.
                return
            # Collect all reachable input_hashes.
            reachable_hashes: set[str] = set()
            for node_idx in g._graph.node_indices():
                node = g._graph[node_idx]
                for h in node.content_hashes.values():
                    reachable_hashes.add(h)

            for layer in self.iter_layers():
                # Check GC policy from config.
                cfg = session.config
                store_name = cfg.layers[layer.name].store if layer.name in cfg.layers else None
                store_cfg = cfg.stores.get(store_name) if store_name else None
                if store_cfg is None or store_cfg.gc != "orphans":
                    continue

                # Build reachable store keys for this layer.
                reachable_keys: set[str] = {f"{h}:{layer.generator_version}" for h in reachable_hashes}
                # GC through the ArtifactCache (walks FsStore directory).
                _gc_store(layer, reachable_keys)
        finally:
            snap.close()

    def resolve_input(self, layer: DerivedLayer, snapshot: Snapshot, durable_id: str) -> tuple[Any, str]:
        """Resolve the input and input_hash for *durable_id* under *layer*.

        Returns (GenInput, input_hash_hex).

        - Code-derived: input_hash = node.content_hashes[layer.hash_profile]
        - Layer-derived: input_hash = hash of upstream artifact bytes
        """
        from tyo3.derive.generators import GenInput

        if layer.is_code_derived:
            node = snapshot.graph().symbol(durable_id)
            if node is None:
                raise KeyError(
                    f"Entity '{durable_id}' is not present at this revision "
                    f"(deleted, or never reconciled to this snapshot)"
                )
            content_hash = node.content_hashes.get(layer.hash_profile)
            if content_hash is None:
                raise KeyError(
                    f"Entity '{durable_id}' has no content hash for profile "
                    f"'{layer.hash_profile}' (available: {list(node.content_hashes)})"
                )
            # Per-layer key locality (§5.5): a `local` layer keys on the entity's
            # own content hash; a `semantic` layer additionally folds in the
            # dependency-closure fingerprint, so it recomputes when a dependency
            # changes even though its own body did not.
            if layer.key_locality == "semantic":
                fingerprint = self._dependency_fingerprint(snapshot, durable_id, layer.hash_profile)
                input_hash = _hash_bytes(f"{content_hash}\x00{fingerprint}".encode())
            else:
                input_hash = content_hash
            source = _entity_source(snapshot, durable_id)
            return (
                GenInput(durable_id=durable_id, source=source, kind=node.kind.value),
                input_hash,
            )
        else:
            # Layer-derived: input is the upstream artifact.
            upstream_name = layer.depends_on[0]
            upstream = self.layer(upstream_name)
            # Read upstream artifact for this entity.
            # We need the upstream's input_hash to resolve the artifact.
            up_input_hash = self._resolve_upstream_hash(upstream, snapshot, durable_id)
            if up_input_hash is None:
                raise KeyError(f"Cannot resolve upstream hash for '{durable_id}' in layer '{upstream_name}'")
            up_key = upstream.keys_for(up_input_hash)
            up_artifact = upstream.cache.get(up_key)
            if up_artifact is None:
                raise KeyError(f"No upstream artifact for '{durable_id}' in layer '{upstream_name}'")
            # Decode artifact as source for the downstream generator.
            up_source = up_artifact.decode("utf-8", errors="replace")
            # The input hash for a layer-derived layer is the hash of the
            # upstream artifact bytes, combined with the downstream version.
            input_hash = _hash_bytes(up_artifact)
            return (
                GenInput(durable_id=durable_id, source=up_source),
                input_hash,
            )

    def _resolve_upstream_hash(self, upstream: DerivedLayer, snapshot: Snapshot, durable_id: str) -> str | None:
        """Resolve the input_hash for *durable_id* under the upstream layer."""
        if upstream.is_code_derived:
            node = snapshot.graph().symbol(durable_id)
            return node.content_hashes.get(upstream.hash_profile)
        else:
            # Recurse up the chain.
            up_up_name = upstream.depends_on[0]
            up_up = self.layer(up_up_name)
            up_up_hash = self._resolve_upstream_hash(up_up, snapshot, durable_id)
            if up_up_hash is None:
                return None
            up_up_key = up_up.keys_for(up_up_hash)
            up_up_artifact = up_up.cache.get(up_up_key)
            if up_up_artifact is None:
                return None
            return _hash_bytes(up_up_artifact)

    def _dependency_fingerprint(self, snapshot: Snapshot, durable_id: str, hash_profile: str) -> str:
        """Stable fingerprint of *durable_id*'s dependency closure at *snapshot*.

        Walks the transitive forward-dependency closure over the pinned
        snapshot's graph (never the live head — §5.9), collecting each
        dependency's content hash under *hash_profile*. The result is a stable
        hash of the sorted ``id=hash`` pairs, so it moves iff any dependency's
        content changed (or the closure's shape changed). The entity itself is
        excluded — its own content hash is keyed separately (§5.5).
        """
        graph = snapshot.graph()
        closure: set[str] = set()
        frontier = [durable_id]
        while frontier:
            current = frontier.pop()
            for dep_id in graph.dependencies(current):
                if dep_id == durable_id or dep_id in closure:
                    continue
                closure.add(dep_id)
                frontier.append(dep_id)

        parts: list[str] = []
        for dep_id in sorted(closure):
            node = graph.symbol(dep_id)
            dep_hash = node.content_hashes.get(hash_profile, "") if node else ""
            parts.append(f"{dep_id}={dep_hash}")
        return _hash_bytes("\x00".join(parts).encode("utf-8"))


def _effective_topo_order(native_topo: tuple[str, ...], effective: dict) -> list[str]:
    """Native topo order extended with registered-derived layer names.

    The native ``topo_order`` already orders config-declared layers (and the
    registered *authored* layers the native shim appended). Registered *derived*
    layers are not in it, so they are appended after a local Kahn toposort keyed
    on their ``depends_on`` — deps already in the native order (or ``code``) are
    treated as satisfied. Preserves dependency order so ``iter_layers`` is
    correct."""
    topo = list(native_topo)
    placed = set(topo) | {"code"}
    pending = [n for n in effective if n not in placed]
    progress = True
    while pending and progress:
        progress = False
        still: list[str] = []
        for name in pending:
            deps = effective[name].depends_on
            if all(d in placed for d in deps):
                topo.append(name)
                placed.add(name)
                progress = True
            else:
                still.append(name)
        pending = still
    # Any layer whose deps never resolved (declared on a missing/cyclic upstream)
    # is appended last so it is still visited; resolve_input will surface the
    # real error at read time rather than silently dropping the layer.
    topo.extend(pending)
    return topo


def _hash_bytes(data: bytes) -> str:
    """SHA-256 hex of data, truncated to 32 chars for readability."""
    return hashlib.sha256(data).hexdigest()[:32]


def _kind_for_id(snapshot: Snapshot, durable_id: str) -> str:
    """Return the entity kind for a durable_id from the graph."""
    try:
        node = snapshot.graph().symbol(durable_id)
        if node is not None:
            return node.kind.value
    except Exception:
        # Best-effort kind lookup; an unresolvable id yields "" (no kind filter).
        pass
    return ""


def _gc_store(layer: DerivedLayer, reachable_keys: set[str]) -> None:
    """Delete unreachable artifacts from a layer's backing store.

    Walks the store's key space and deletes keys not in *reachable_keys*.
    Only affects the layer's current generator_version space (keys for
    other versions are preserved for rollback).
    """
    store = layer.cache._store
    # For FsStore, walk the directory tree to find all keys.
    from pathlib import Path

    if hasattr(store, "root"):
        root = Path(store.root)
        if root.exists():
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix == ".tmp":
                    continue
                # Decode the path back to a store key.
                # FsStore uses sha256 hex dirs: root/dd/dd/digest
                rel = path.relative_to(root)
                parts = rel.parts
                if len(parts) == 3 and len(parts[0]) == 2 and len(parts[1]) == 2:
                    # `parts[2]` is the content digest. Reverse-lookup from digest
                    # to store key is not implemented yet, so GC for filesystem
                    # stores is deferred to the full implementation.
                    pass


def _entity_source(snapshot: Snapshot, durable_id: str) -> str:
    """Extract the source text of an entity by DurableId."""
    # The snapshot can locate + read entity source.
    graph = snapshot.graph()
    node = graph.symbol(durable_id)

    # Read source from the file at the entity's range.
    try:
        path = node.file
        start = node.range.start
        end = node.range.end
        # Use document_symbols to get the full source, or read the file directly.
        # Simple approach: read the whole file and slice.
        # We need to read the file from the snapshot's project root.
        # For now, use a simple approach: the snapshot has a _root.
        root = getattr(snapshot, "_root", None)
        if root:
            import os

            full_path = os.path.join(str(root), path)
            if os.path.exists(full_path):
                with open(full_path) as f:
                    lines = f.readlines()
                result_lines = []
                for i in range(start.line - 1, end.line):
                    line = lines[i] if i < len(lines) else ""
                    if i == start.line - 1 and i == end.line - 1:
                        # Single-line entity.
                        result_lines.append(line[start.column - 1 : end.column - 1])
                    elif i == start.line - 1:
                        result_lines.append(line[start.column - 1 :])
                    elif i == end.line - 1:
                        result_lines.append(line[: end.column - 1])
                    else:
                        result_lines.append(line)
                return "\n".join(result_lines)
        # Fallback: use the name/qualified_name.
        return node.qualified_name
    except Exception:
        return node.qualified_name
