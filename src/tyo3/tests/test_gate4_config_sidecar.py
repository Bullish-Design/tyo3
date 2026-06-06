"""Gate 4 config/session integration coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from tyo3 import TyO3Session
from tyo3.exceptions import ConfigError, FormatVersionError, RevisionEvictedError


def _project(root: Path) -> None:
    (root / "pyproject.toml").write_text("[project]\nname = 'gate4'\nversion = '0.1.0'\n")
    (root / "mod.py").write_text("x = 1\n")


FULL_CONFIG = """
schema_version = 1

[spine]
retain_cap = 256
default_hash_profile = "structure"

[hashing.profiles.structure]

[hashing.profiles.semantic]
include_comments = true
include_docstrings = true

[layers.descriptions]
origin = "derived"
depends_on = ["code"]
generator = "describe"
generator_version = "v1"
hash_profile = "semantic"
store = "kv_descriptions"

[layers.description_embeddings]
origin = "derived"
depends_on = ["descriptions"]
generator = "embed"
generator_version = "v1"
hash_profile = "semantic"
store = "vectors"

[layers.intent]
origin = "authored"
history = true
review_on_change = true

[generators.describe]
type = "python"
callable = "pkg:describe"

[generators.embed]
type = "http"
endpoint = "https://example.invalid/embed"
dim = 3

[stores.kv_descriptions]
backend = "fs"
path = "cache/descriptions"

[stores.vectors]
backend = "fs"
path = "cache/vectors"
dim = 3
"""


def _write_config(root: Path, text: str) -> None:
    sidecar = root / ".tyo3"
    sidecar.mkdir()
    (sidecar / "config.toml").write_text(text)


def test_no_config_opens_with_defaults(tmp_path: Path) -> None:
    _project(tmp_path)

    with TyO3Session(tmp_path) as session:
        assert session.config.schema_version == 1
        assert session.config.spine.retain_cap == 256
        assert "structure" in session.config.hashing_profiles
        assert session.files()


def test_full_config_exposes_topological_layers(tmp_path: Path) -> None:
    _project(tmp_path)
    _write_config(tmp_path, FULL_CONFIG)

    with TyO3Session(tmp_path) as session:
        order = session.config.topo_order
        assert "descriptions" in session.config.layers
        assert order.index("code") < order.index("descriptions")
        assert order.index("descriptions") < order.index("description_embeddings")


def test_retain_cap_config_drives_snapshot_eviction(tmp_path: Path) -> None:
    _project(tmp_path)
    _write_config(
        tmp_path,
        """
schema_version = 1
[spine]
retain_cap = 4
[hashing.profiles.structure]
""",
    )

    with TyO3Session(tmp_path) as session:
        old = session.head
        for i in range(6):
            session.edit("mod.py", f"x = {i}\n")
        with pytest.raises(RevisionEvictedError):
            session.snapshot(at=old)


def test_cyclic_config_rejected_before_session_opens(tmp_path: Path) -> None:
    _project(tmp_path)
    _write_config(
        tmp_path,
        """
schema_version = 1
[hashing.profiles.structure]
[layers.a]
origin = "derived"
depends_on = ["b"]
generator = "g"
generator_version = "v1"
store = "s"
[layers.b]
origin = "derived"
depends_on = ["a"]
generator = "g"
generator_version = "v1"
store = "s"
[generators.g]
type = "python"
callable = "pkg:g"
[stores.s]
backend = "fs"
path = "cache/s"
""",
    )

    with pytest.raises(ConfigError):
        TyO3Session(tmp_path)


def test_unknown_schema_version_is_format_version_error(tmp_path: Path) -> None:
    _project(tmp_path)
    _write_config(
        tmp_path,
        """
schema_version = 999
[hashing.profiles.structure]
""",
    )

    with pytest.raises(FormatVersionError):
        TyO3Session(tmp_path)
