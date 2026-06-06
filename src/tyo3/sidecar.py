"""Python mirror of the `.tyo3/` sidecar layout."""

from __future__ import annotations

from pathlib import Path


class Sidecar:
    """Owns Python-side sidecar path construction for layer writers."""

    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root) / ".tyo3"

    def config_path(self) -> Path:
        return self.root / "config.toml"

    def config_local_path(self) -> Path:
        return self.root / "config.local.toml"

    def secrets_path(self) -> Path:
        return self.root / "secrets.toml"

    def identity_db_path(self) -> Path:
        return self.root / "identity.db"

    def authored_dir(self, layer: str) -> Path:
        return self.root / "authored" / layer

    def cache_dir(self, layer: str) -> Path:
        return self.root / "cache" / layer

    def gitignore_path(self) -> Path:
        return self.root / ".gitignore"


__all__ = ["Sidecar"]
