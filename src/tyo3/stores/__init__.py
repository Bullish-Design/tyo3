"""Store backend registry."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import importlib
from pathlib import Path
from typing import Any

from tyo3.exceptions import StoreBackendUnavailable
from tyo3.sidecar import Sidecar
from tyo3.stores.base import Store, VectorStore
from tyo3.stores.fs import FsStore


_OPTIONAL_BACKENDS = {
    "lancedb": "lancedb",
    "qdrant": "qdrant_client",
    "sqlite-vec": "sqlite_vec",
}


def open_store(store_cfg: Any, sidecar: Sidecar, layer: str | None = None) -> Store:
    cfg = _cfg_dict(store_cfg)
    backend = cfg.get("backend")
    if backend == "fs":
        root = _store_root(cfg, sidecar, layer)
        return FsStore(root)
    if backend == "lancedb":
        try:
            importlib.import_module("lancedb")
        except ImportError as exc:
            raise StoreBackendUnavailable(
                "Store backend 'lancedb' requires optional package 'lancedb'"
            ) from exc
        root = _store_root(cfg, sidecar, layer)
        dim = cfg.get("dim", 1536)
        metric = cfg.get("metric", "cosine")
        from tyo3.stores.lancedb_store import LanceDbStore
        return LanceDbStore(str(root), dim=dim, metric=metric)
    if backend in _OPTIONAL_BACKENDS:
        module = _OPTIONAL_BACKENDS[backend]
        try:
            importlib.import_module(module)
        except ImportError as exc:
            raise StoreBackendUnavailable(
                f"Store backend '{backend}' requires optional package '{module}'"
            ) from exc
        raise StoreBackendUnavailable(
            f"Store backend '{backend}' adapter is not implemented in Gate 4"
        )
    raise ValueError(f"Unknown store backend: {backend}")


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


__all__ = ["FsStore", "Store", "StoreBackendUnavailable", "VectorStore", "open_store"]
