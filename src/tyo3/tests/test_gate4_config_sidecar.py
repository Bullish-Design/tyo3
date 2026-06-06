"""Gate 4 config/session integration coverage."""

from __future__ import annotations

from pathlib import Path
import json

import pytest

import tyo3.exceptions as exc
from tyo3 import _native_impl
from tyo3 import TyO3Session
from tyo3.exceptions import (
    ConfigError,
    FormatVersionError,
    RevisionEvictedError,
    StoreBackendUnavailable,
)
from tyo3.sidecar import Sidecar
from tyo3.stores import FsStore, open_store


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


def test_public_format_version_error_reexports_native_class() -> None:
    assert exc.FormatVersionError is _native_impl.FormatVersionError


def test_identity_newer_format_version_uses_same_format_error(tmp_path: Path) -> None:
    _project(tmp_path)
    sidecar = tmp_path / ".tyo3"
    sidecar.mkdir()
    (sidecar / "identity.db").write_text('{"format_version": 2, "anchors": []}')

    with pytest.raises(FormatVersionError):
        TyO3Session(tmp_path)


def test_project_closed_error_is_caught_by_tyo3_base(tmp_path: Path) -> None:
    _project(tmp_path)
    session = TyO3Session(tmp_path)
    session.close()

    with pytest.raises(exc.TyO3Error):
        session.files()


def test_config_validation_failure_mentions_key(tmp_path: Path) -> None:
    _project(tmp_path)
    _write_config(
        tmp_path,
        """
schema_version = 1
[hashing.profiles.structure]
[layers.bad]
origin = "derived"
generator = "missing"
generator_version = "v1"
store = "s"
[stores.s]
backend = "fs"
path = "cache/s"
""",
    )

    with pytest.raises(ConfigError, match="layers.bad.generator"):
        TyO3Session(tmp_path)


@pytest.mark.parametrize(
    ("config_text", "pattern"),
    [
        (
            """
schema_version = 1
[hashing.profiles.structure]
[layers.code]
origin = "authored"
""",
            "reserved layer name",
        ),
        (
            """
schema_version = 1
[hashing.profiles.structure]
[layers.bad]
origin = "derived"
generator = "g"
generator_version = "v1"
store = "s"
entity_kinds = ["wizard"]
[generators.g]
type = "python"
callable = "pkg:g"
[stores.s]
backend = "fs"
path = "cache/s"
""",
            "wizard",
        ),
        (
            """
schema_version = 1
[hashing.profiles.structure]
[generators.g]
type = "http"
endpoint = "sk-livesecret1234567890"
""",
            "inline secret",
        ),
    ],
)
def test_invalid_configs_rejected_with_config_error(
    tmp_path: Path, config_text: str, pattern: str
) -> None:
    _project(tmp_path)
    _write_config(tmp_path, config_text)

    with pytest.raises(ConfigError, match=pattern):
        TyO3Session(tmp_path)


def test_sidecar_is_sole_path_owner_in_source() -> None:
    repo = Path(__file__).parents[3]
    allowed = {
        repo / "rust" / "src" / "sidecar.rs",
        repo / "src" / "tyo3" / "sidecar.py",
    }
    offenders: list[Path] = []
    for base in (repo / "rust" / "src", repo / "src" / "tyo3"):
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".rs", ".py"} and path not in allowed:
                if ".tyo3" in path.read_text():
                    offenders.append(path.relative_to(repo))
    assert offenders == []


def test_identity_write_is_crash_safe_against_leftover_tmp(tmp_path: Path) -> None:
    _project(tmp_path)
    with TyO3Session(tmp_path) as session:
        session.sync_all()

    identity_path = tmp_path / ".tyo3" / "identity.db"
    original = json.loads(identity_path.read_text())
    identity_path.with_suffix(".tmp").write_text("not valid json")

    with TyO3Session(tmp_path) as session:
        session.sync_all()

    assert json.loads(identity_path.read_text()) == original


def test_deleting_sidecar_returns_to_defaults(tmp_path: Path) -> None:
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
    for path in (tmp_path / ".tyo3").iterdir():
        path.unlink()
    (tmp_path / ".tyo3").rmdir()

    with TyO3Session(tmp_path) as session:
        assert session.config.spine.retain_cap == 256


def test_fs_store_round_trips_and_uses_atomic_tmp(tmp_path: Path) -> None:
    store = FsStore(tmp_path / "cache")

    assert store.get("missing@v1") is None
    store.put("hash@v1", b"artifact")
    assert store.has("hash@v1")
    assert store.get("hash@v1") == b"artifact"
    assert not list((tmp_path / "cache").rglob("*.tmp"))
    store.delete("hash@v1")
    assert store.get("hash@v1") is None


def test_open_store_fs_uses_sidecar_cache_dir_lazily(tmp_path: Path) -> None:
    sidecar = Sidecar(tmp_path)
    store = open_store({"backend": "fs"}, sidecar, layer="descriptions")

    assert isinstance(store, FsStore)
    assert not sidecar.cache_dir("descriptions").exists()
    store.put("hash@v1", b"artifact")
    assert sidecar.cache_dir("descriptions").exists()


def test_lazy_vector_backend_missing_dependency(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail_import(name: str):
        if name == "lancedb":
            raise ImportError("missing")
        return __import__(name)

    monkeypatch.setattr("importlib.import_module", fail_import)

    with pytest.raises(StoreBackendUnavailable, match="lancedb"):
        open_store({"backend": "lancedb"}, Sidecar(tmp_path), layer="vectors")


def test_python_sidecar_layout_matches_rust_contract(tmp_path: Path) -> None:
    sidecar = Sidecar(tmp_path)

    assert sidecar.config_path() == tmp_path / ".tyo3" / "config.toml"
    assert sidecar.config_local_path() == tmp_path / ".tyo3" / "config.local.toml"
    assert sidecar.secrets_path() == tmp_path / ".tyo3" / "secrets.toml"
    assert sidecar.identity_db_path() == tmp_path / ".tyo3" / "identity.db"
    assert sidecar.cache_dir("x") == tmp_path / ".tyo3" / "cache" / "x"
    assert sidecar.authored_dir("x") == tmp_path / ".tyo3" / "authored" / "x"
    assert sidecar.record_path("x", "id1") == tmp_path / ".tyo3" / "authored" / "x" / "id1.json"
    assert sidecar.history_dir("x", "id1") == tmp_path / ".tyo3" / "authored" / "x" / "id1.history"


def test_open_with_sidecar_writes_managed_gitignore_preserving_user_lines(tmp_path: Path) -> None:
    _project(tmp_path)
    sidecar = tmp_path / ".tyo3"
    sidecar.mkdir()
    (sidecar / ".gitignore").write_text("user.log\n")

    with TyO3Session(tmp_path):
        pass

    text = (sidecar / ".gitignore").read_text()
    assert "user.log" in text
    assert "# managed by tyo3" in text
    assert "cache/" in text
    assert "config.local.toml" in text
    assert "secrets.toml" in text
    assert (sidecar / "LAYOUT.md").exists()


def test_open_without_sidecar_touches_no_source_files(tmp_path: Path) -> None:
    _project(tmp_path)
    before = {path: path.read_text() for path in tmp_path.rglob("*") if path.is_file()}

    with TyO3Session(tmp_path):
        pass

    after = {path: path.read_text() for path in tmp_path.rglob("*") if path.is_file()}
    assert before == after
    assert not (tmp_path / ".tyo3").exists()
