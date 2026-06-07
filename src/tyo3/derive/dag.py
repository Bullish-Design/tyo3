"""Derivation DAG — assembly, registration, and input resolution (§9).

Consumes Gate 4's validated topo order. Authored layers are sinks (§9.2.4);
they are excluded from the DAG entirely. Input resolution is the central
logic: code-derived layers key on the entity's profile hash; layer-derived
layers key on the upstream artifact's hash.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from tyo3.derive.cache import CacheKey
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
        self._layer_map: dict[str, DerivedLayer] = {l.name: l for l in layers}
        self._topo_order = topo_order  # includes "code" at position 0

        # Defensive acyclicity check (Gate 4 already validated).
        for layer in layers:
            for dep in layer.depends_on:
                if dep != "code" and dep not in self._layer_map:
                    raise AssertionError(
                        f"Layer '{layer.name}' depends on unknown layer '{dep}'"
                    )

    @classmethod
    def from_session(cls, session: "TyO3Session") -> "DerivationDAG":
        """Build the DAG from a session's validated config."""
        from tyo3.derive.generators import make_generator

        config = session.config
        sidecar = getattr(session, "_sidecar", None)
        # Snapshot doesn't have _sidecar; use root-based sidecar.
        if sidecar is None:
            from tyo3.sidecar import Sidecar
            root = getattr(session, "root", getattr(session, "_root", None))
            sidecar = Sidecar(str(root)) if root else None

        if not config.topo_order:
            return cls([], [])

        layers: list[DerivedLayer] = []
        for name in config.topo_order:
            if name == "code":
                continue
            if name not in config.layers:
                continue
            layer_cfg = config.layers[name]
            # Skip authored layers — they are sinks.
            if layer_cfg.origin == "authored":
                continue

            # Build the store.
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

            # Build the generator.
            gen_name = layer_cfg.generator
            gen_cfg = config.generators.get(gen_name) if gen_name else None
            if gen_cfg is None:
                raise ValueError(f"Layer '{name}' has no generator config")
            generator = make_generator(gen_cfg, name=name)

            layer = DerivedLayer.from_config(layer_cfg, store, generator)
            layer.name = name
            layers.append(layer)

        return cls(layers, list(config.topo_order))

    @property
    def is_empty(self) -> bool:
        return len(self._layers) == 0

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

    def resolve_input(
        self, layer: DerivedLayer, snapshot: "Snapshot", durable_id: str
    ) -> tuple[Any, str]:
        """Resolve the input and input_hash for *durable_id* under *layer*.

        Returns (GenInput, input_hash_hex).

        - Code-derived: input_hash = node.content_hashes[layer.hash_profile]
        - Layer-derived: input_hash = hash of upstream artifact bytes
        """
        from tyo3.derive.generators import GenInput

        if layer.is_code_derived:
            node = snapshot.graph().symbol(durable_id)
            input_hash = node.content_hashes.get(layer.hash_profile)
            if input_hash is None:
                raise KeyError(
                    f"Entity '{durable_id}' has no content hash for profile "
                    f"'{layer.hash_profile}' (available: {list(node.content_hashes)})"
                )
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
            up_input_hash = self._resolve_upstream_hash(
                upstream, snapshot, durable_id
            )
            if up_input_hash is None:
                raise KeyError(
                    f"Cannot resolve upstream hash for '{durable_id}' in layer "
                    f"'{upstream_name}'"
                )
            up_key = upstream.keys_for(up_input_hash)
            up_artifact = upstream.cache.get(up_key)
            if up_artifact is None:
                raise KeyError(
                    f"No upstream artifact for '{durable_id}' in layer "
                    f"'{upstream_name}'"
                )
            # Decode artifact as source for the downstream generator.
            up_source = up_artifact.decode("utf-8", errors="replace")
            # The input hash for a layer-derived layer is the hash of the
            # upstream artifact bytes, combined with the downstream version.
            input_hash = _hash_bytes(up_artifact)
            return (
                GenInput(durable_id=durable_id, source=up_source),
                input_hash,
            )

    def _resolve_upstream_hash(
        self, upstream: DerivedLayer, snapshot: "Snapshot", durable_id: str
    ) -> str | None:
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


def _hash_bytes(data: bytes) -> str:
    """SHA-256 hex of data, truncated to 32 chars for readability."""
    return hashlib.sha256(data).hexdigest()[:32]


def _entity_source(snapshot: "Snapshot", durable_id: str) -> str:
    """Extract the source text of an entity by DurableId."""
    # The snapshot can locate + read entity source.
    graph = snapshot.graph()
    node = graph.symbol(durable_id)

    # Read source from the file at the entity's range.
    try:
        from tyo3.models.analysis import Position
        path = node.file
        start = node.range.start
        end = node.range.end
        # Use document_symbols to get the full source, or read the file directly.
        # Simple approach: read the whole file and slice.
        import io
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
                        result_lines.append(line[start.column - 1:])
                    elif i == end.line - 1:
                        result_lines.append(line[:end.column - 1])
                    else:
                        result_lines.append(line)
                return "\n".join(result_lines)
        # Fallback: use the name/qualified_name.
        return node.qualified_name
    except Exception:
        return node.qualified_name
