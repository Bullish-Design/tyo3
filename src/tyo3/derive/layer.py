"""DerivedLayer — runtime for one declared derived layer (SPEC §8).

Pure policy + cache + generator; no scheduling and no DAG wiring yet.
Reads its contract entirely from `session.config` (Gate 4) — no per-layer
code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from tyo3.config import LayerConfig
from tyo3.derive.cache import ArtifactCache, CacheKey
from tyo3.stores.base import Store

if TYPE_CHECKING:
    from tyo3.derive.generators import Generator


@dataclass
class DerivedLayer:
    """Runtime for one declared derived layer.

    Owns a single derived layer's behaviour: its cache, its generator,
    and its declared contract. Scheduling (Step 6) and DAG wiring (Step 4)
    are separate concerns.
    """

    name: str
    depends_on: tuple[str, ...]  # e.g. ("code",) or ("descriptions",)
    generator: Generator  # from generators.py — protocol
    generator_version: str
    hash_profile: str
    serving: Literal["stale", "block"]
    recompute: Literal["lazy", "eager"]
    key_locality: Literal["local", "semantic"]
    entity_kinds: frozenset[str] | None  # None = all
    cache: ArtifactCache

    # Per-entity binding: durable_id -> last-served input_hash.
    # Populated/maintained by invalidation and recompute (Steps 5–7).
    _bindings: dict[str, str] = field(default_factory=dict, repr=False)
    # Per-entity last-good artifact store key for stale serving.
    _last_good: dict[str, str] = field(default_factory=dict, repr=False)
    # Per-entity failure state.
    _failed: set[str] = field(default_factory=set, repr=False)

    @classmethod
    def from_config(
        cls,
        layer_cfg: LayerConfig,
        store: Store,
        generator: Generator,
    ) -> DerivedLayer:
        """Construct from a validated LayerConfig (Gate 4)."""
        cache = ArtifactCache(store)
        entity_kinds: frozenset[str] | None
        if layer_cfg.entity_kinds:
            entity_kinds = frozenset(layer_cfg.entity_kinds)
        else:
            entity_kinds = None
        return cls(
            name="<unnamed>",  # caller sets name
            depends_on=layer_cfg.depends_on,
            generator=generator,
            generator_version=layer_cfg.generator_version or "v0",
            hash_profile=layer_cfg.hash_profile or "structure",
            serving=layer_cfg.serving,  # type: ignore[arg-type]
            recompute=layer_cfg.recompute,  # type: ignore[arg-type]
            key_locality=getattr(layer_cfg, "key_locality", "local"),  # type: ignore[arg-type]
            entity_kinds=entity_kinds,
            cache=cache,
        )

    @property
    def is_code_derived(self) -> bool:
        """True when this layer derives directly from code entities."""
        return self.depends_on == ("code",)

    def keys_for(self, input_hash: str) -> CacheKey:
        """Build the cache key for an input_hash under this layer's version."""
        return CacheKey(input_hash=input_hash, generator_version=self.generator_version)

    def is_fresh(self, input_hash: str) -> bool:
        """True when the cache already holds an artifact for this input_hash."""
        return self.cache.has(self.keys_for(input_hash))

    def applies_to(self, kind: str) -> bool:
        """True when this layer should process entities of *kind*."""
        if self.entity_kinds is None:
            return True
        return kind in self.entity_kinds

    def bind(self, durable_id: str, input_hash: str, store_key: str) -> None:
        """Record the served binding for *durable_id*."""
        self._bindings[durable_id] = input_hash
        self._last_good[durable_id] = store_key
        self._failed.discard(durable_id)

    def binding(self, durable_id: str) -> str | None:
        """The last-served input_hash for *durable_id*, or None."""
        return self._bindings.get(durable_id)

    def last_good_store_key(self, durable_id: str) -> str | None:
        """The store key of the last-good artifact for *durable_id*."""
        return self._last_good.get(durable_id)

    def mark_stale(self, durable_id: str) -> None:
        """Mark *durable_id* as stale (retaining last-good key)."""
        pass  # staleness is inferred: binding present but key != current

    def mark_failed(self, durable_id: str) -> None:
        self._failed.add(durable_id)

    def mark_clear(self, durable_id: str) -> None:
        self._failed.discard(durable_id)
        # keep binding and last_good for stale serving

    def drop(self, durable_id: str) -> None:
        """Drop all state for *durable_id* (entity deleted)."""
        self._bindings.pop(durable_id, None)
        self._last_good.pop(durable_id, None)
        self._failed.discard(durable_id)
