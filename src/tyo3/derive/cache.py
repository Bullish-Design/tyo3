"""Content-addressed, immutable artifact cache (SPEC §8.2.1/§8.2.2).

Keys are ``(input_hash, generator_version)`` — never by revision or
``DurableId``. A given key maps to exactly one immutable artifact, so the
cache is trivially consistent across revisions and agents.
"""

from __future__ import annotations

from dataclasses import dataclass

from tyo3.stores.base import Store


@dataclass(frozen=True)
class CacheKey:
    """Immutable cache key: (input_hash, generator_version).

    The input_hash is the hex content hash of the entity (for code-derived
    layers) or the upstream artifact (for layer-derived layers), computed
    under the layer's hash profile.
    """

    input_hash: str
    generator_version: str

    def to_store_key(self) -> str:
        """Serialise to the store key used by the backing Store."""
        return f"{self.input_hash}:{self.generator_version}"

    @classmethod
    def from_store_key(cls, key: str) -> CacheKey:
        parts = key.split(":", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid store key: {key}")
        return cls(input_hash=parts[0], generator_version=parts[1])


class ArtifactCache:
    """Content-addressed, immutable per key (§8.2.2).

    Put-once semantics: re-putting the same key with different bytes is a
    programming error (raises AssertionError). Re-putting with equal bytes
    is a no-op. The cache has no revision awareness — that is the whole
    point of §8.2.2.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def get(self, key: CacheKey) -> bytes | None:
        """Return the artifact bytes for this key, or None."""
        return self._store.get(key.to_store_key())

    def put(self, key: CacheKey, artifact: bytes) -> None:
        """Store an artifact. Idempotent for equal bytes; raises on conflict."""
        store_key = key.to_store_key()
        existing = self._store.get(store_key)
        if existing is not None:
            if existing != artifact:
                raise AssertionError(
                    f"ArtifactCache: conflicting re-put for key {store_key}. "
                    f"Cache is immutable — a different artifact for the same "
                    f"(input_hash, generator_version) is a programming error."
                )
            return  # idempotent — same bytes, no-op
        self._store.put(store_key, artifact)

    def has(self, key: CacheKey) -> bool:
        return self._store.has(key.to_store_key())

    def delete(self, key: CacheKey) -> None:
        self._store.delete(key.to_store_key())

    def gc(self, reachable_keys: set[str]) -> None:
        """Delete artifacts not in *reachable_keys*.

        *reachable_keys* should be the set of store keys (``input_hash:version``)
        for all currently-referenced artifacts. Only removes keys from this
        specific generator_version space.
        """
        # Collect all store keys currently in the store.
        # For FsStore, we need to walk the directory tree.
        # For now, this is a no-op — GC is invoked explicitly via session.gc().
        pass
