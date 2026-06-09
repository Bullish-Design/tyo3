"""Store backend registry.

Built-in backends (``fs``/``lancedb``) self-register their factories into
``tyo3.extend._STORES`` at this module's import time; ``open_store`` is a thin
dispatcher through that registry (AB1). A custom backend registered via
``tyo3.extend.register_store`` resolves here with no edit to ``open_store``.
"""

from __future__ import annotations

import importlib
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tyo3.exceptions import StoreBackendUnavailable
from tyo3.sidecar import Sidecar
from tyo3.stores.base import Store, VectorStore
from tyo3.stores.fs import FsStore

if TYPE_CHECKING:
    from tyo3.extend import StoreContext


def open_store(store_cfg: Any, sidecar: Sidecar, layer: str | None = None) -> Store:
    """Open the backing store for *store_cfg* through the backend registry.

    Builds a :class:`~tyo3.extend.StoreContext` internally so the legacy
    ``open_store(store_cfg, sidecar, layer=None)`` signature is preserved
    (``dag.py``/test call sites depend on it). An unknown backend — one not in
    ``_STORES`` — raises ``ValueError``; a registered-but-unavailable optional
    backend (e.g. ``lancedb`` with the package missing) raises
    ``StoreBackendUnavailable``."""
    from tyo3.extend import _STORES, StoreContext

    cfg = _cfg_dict(store_cfg)
    backend = cfg.get("backend")
    factory = _STORES.get(backend)
    if factory is None:
        raise ValueError(f"Unknown store backend: {backend}")
    ctx = StoreContext(
        layer=layer,
        sidecar=sidecar,
        dim=cfg.get("dim"),
        metric=cfg.get("metric"),
        config=cfg,
    )
    return factory(ctx)


def _cfg_dict(store_cfg: Any) -> dict[str, Any]:
    if isinstance(store_cfg, dict):
        return dict(store_cfg)
    if is_dataclass(store_cfg):
        return asdict(store_cfg)
    return dict(vars(store_cfg))


def _store_root(cfg: dict[str, Any], sidecar: Sidecar, layer: str | None) -> Path:
    if layer is not None:
        return sidecar.cache_dir(layer)
    if "layer" in cfg and cfg["layer"]:
        return sidecar.cache_dir(str(cfg["layer"]))
    if cfg.get("path"):
        path = Path(str(cfg["path"]))
        return path if path.is_absolute() else sidecar.root / path
    raise ValueError("fs store requires a layer or path")


# ── Built-in store factories (self-register into tyo3.extend) ────────────────


def _fs_factory(ctx: StoreContext) -> Store:
    root = _store_root(ctx.config, ctx.sidecar, ctx.layer)
    return FsStore(root)


def _lancedb_factory(ctx: StoreContext) -> Store:
    try:
        importlib.import_module("lancedb")
    except ImportError as exc:
        raise StoreBackendUnavailable("Store backend 'lancedb' requires optional package 'lancedb'") from exc
    root = _store_root(ctx.config, ctx.sidecar, ctx.layer)
    dim = ctx.config.get("dim", 1536)
    metric = ctx.config.get("metric", "cosine")
    from tyo3.stores.lancedb_store import LanceDbStore

    return LanceDbStore(str(root), dim=dim, metric=metric)


def _register_builtins() -> None:
    from tyo3.extend import register_store

    # ``override=True`` keeps re-import idempotent without the dup-raise that
    # protects *user* registrations. Optional backends (qdrant/sqlite-vec) are
    # intentionally NOT registered here — they become first-class only via
    # ``register_store`` (an unregistered backend simply raises ValueError).
    register_store("fs", _fs_factory, override=True)
    register_store("lancedb", _lancedb_factory, override=True)


_register_builtins()


__all__ = ["FsStore", "Store", "StoreBackendUnavailable", "VectorStore", "open_store"]
