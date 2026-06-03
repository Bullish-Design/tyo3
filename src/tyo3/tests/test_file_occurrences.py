"""Tests for file_occurrences API (Phase 5 — Batch Occurrence API)."""

from __future__ import annotations

from pathlib import Path as StdPath

import pytest

from tyo3.graph.models import ReferenceRole

try:
    from tyo3 import _HAS_NATIVE
except ImportError:
    _HAS_NATIVE = False

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    return str((FIXTURES_DIR / name).resolve())


needs_native = pytest.mark.skipif(
    not _HAS_NATIVE, reason="Rust native extension not built"
)


@needs_native
class TestFileOccurrences:
    def test_returns_list(self) -> None:
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("simple_package"))
        try:
            files = rp.files()
            assert len(files) > 0
            occs = rp.file_occurrences(str(files[0]))
            assert isinstance(occs, list)
        finally:
            rp.close()

    def test_occurrences_have_range_and_role(self) -> None:
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("graph_test"))
        try:
            files = rp.files()
            occs = rp.file_occurrences(str(files[0]))
            assert len(occs) > 0, "Expected at least one occurrence"
            for occ in occs:
                assert occ.range is not None
                assert occ.role in ReferenceRole
        finally:
            rp.close()

    def test_some_occurrences_resolve(self) -> None:
        """Some occurrences should have resolved targets."""
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("graph_test"))
        try:
            files = [str(f) for f in rp.files()]
            models_file = [f for f in files if "models.py" in f]
            assert len(models_file) == 1

            occs = rp.file_occurrences(models_file[0])
            resolved = [o for o in occs if o.target_file is not None]
            # At minimum, the `Base` reference in `User(Base)` should resolve
            # back to models.py itself (or stdlib/builtins.pyi for `object`)
            assert len(resolved) > 0, (
                f"Expected at least one resolved occurrence, got {len(resolved)} "
                f"from {len(occs)} total"
            )
        finally:
            rp.close()

    def test_import_role_detected(self) -> None:
        """Import statements should get the Import role."""
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("graph_test"))
        try:
            files = [str(f) for f in rp.files()]
            app_file = [f for f in files if "app.py" in f]
            assert len(app_file) == 1

            occs = rp.file_occurrences(app_file[0])
            imports = [o for o in occs if o.role == ReferenceRole.IMPORT]
            # app.py has `from models import MAX_USERS, User`
            assert len(imports) >= 1, (
                f"Expected Import role occurrences, got roles: "
                f"{[o.role for o in occs]}"
            )
        finally:
            rp.close()

    def test_definition_role_detected(self) -> None:
        """Definition sites should get the Definition role."""
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("graph_test"))
        try:
            files = [str(f) for f in rp.files()]
            models_file = [f for f in files if "models.py" in f]
            assert len(models_file) == 1

            occs = rp.file_occurrences(models_file[0])
            definitions = [o for o in occs if o.role == ReferenceRole.DEFINITION]
            # models.py defines Base, User, __init__, save, MAX_USERS
            assert len(definitions) >= 3, (
                f"Expected Definition role occurrences, got {len(definitions)}: "
                f"{[(o.target_name, o.role) for o in definitions]}"
            )
        finally:
            rp.close()

    def test_bad_path_raises(self) -> None:
        from tyo3.exceptions import PathResolutionError
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("simple_package"))
        try:
            with pytest.raises(PathResolutionError):
                rp.file_occurrences("nonexistent.py")
        finally:
            rp.close()

    def test_after_close_raises(self) -> None:
        from tyo3.exceptions import ProjectClosedError
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("simple_package"))
        rp.close()
        with pytest.raises(ProjectClosedError):
            rp.file_occurrences("anything.py")

    def test_session_method(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("graph_test")) as session:
            files = session.files()
            assert len(files) > 0
            occs = session.file_occurrences(str(files[0]))
            assert isinstance(occs, list)
            assert len(occs) > 0

    def test_graph_construction_uses_occurrences(self) -> None:
        """Verify that CodeGraph.build() completes successfully using the
        batch occurrence API (Phase 5 path)."""
        from tyo3.graph import CodeGraph
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("graph_test")) as session:
            graph = CodeGraph.build(session)
            assert graph.node_count > 0
            assert graph.edge_count >= 0
            # Should have at least some edges from the occurrence API
            assert graph.edge_count > 0, (
                "Expected at least some reference edges from file_occurrences"
            )
