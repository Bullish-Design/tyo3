"""Tests for core domain models — tyo3-core.allium.

Obligation groups:
- Entity and value type field presence, types, optional handling
- Derived value tests (is_open, has_error)
- Enum tests (ProjectStatus, FileCategory)
- Config tests (TyProjectConfig)
- Relationship tests (ProjectFile.project)
"""

from datetime import datetime, timezone

from tyo3.models.core import (
    BackendInfo,
    FileCategory,
    Path,
    ProjectFile,
    ProjectStatus,
    TyProject,
    TyProjectConfig,
)


class TestPath:
    """Path value type tests."""

    def test_components_field(self) -> None:
        p = Path(components=["home", "user", "project"])
        assert p.components == ["home", "user", "project"]

    def test_equality(self) -> None:
        a = Path(components=["a", "b"])
        b = Path(components=["a", "b"])
        c = Path(components=["a", "c"])
        assert a == b
        assert a != c
        # Hashable for set membership
        s = {a, b, c}
        assert len(s) == 2  # a and b are equal


class TestTyProjectConfig:
    """TyProjectConfig value type tests."""

    def test_defaults(self) -> None:
        config = TyProjectConfig()
        assert config.python_version is None
        assert config.config_path is None
        assert config.extra_search_paths == set()
        assert config.respect_gitignore is True
        assert config.force_exclude is False
        assert config.check_all_files is True

    def test_explicit_values(self) -> None:
        config = TyProjectConfig(
            python_version="3.13",
            respect_gitignore=False,
            force_exclude=True,
            check_all_files=False,
        )
        assert config.python_version == "3.13"
        assert config.respect_gitignore is False
        assert config.force_exclude is True
        assert config.check_all_files is False

    def test_extra_search_paths(self) -> None:
        p = Path(components=["extras"])
        config = TyProjectConfig(extra_search_paths={p})
        assert p in config.extra_search_paths


class TestBackendInfo:
    """BackendInfo value type tests."""

    def test_all_fields(self) -> None:
        info = BackendInfo(
            tyo3_version="0.1.0",
            ty_version="0.0.40",
            ty_commit="abc123",
            ruff_submodule_commit="def456",
            backend_source="astral-sh/ty@0.0.40",
        )
        assert info.tyo3_version == "0.1.0"
        assert info.ty_version == "0.0.40"
        assert info.ty_commit == "abc123"
        assert info.ruff_submodule_commit == "def456"
        assert info.backend_source == "astral-sh/ty@0.0.40"


class TestProjectStatus:
    """ProjectStatus enum tests."""

    def test_values(self) -> None:
        assert ProjectStatus.CLOSED == "closed"
        assert ProjectStatus.OPEN == "open"
        assert ProjectStatus.ERROR == "error"

    def test_comparison(self) -> None:
        assert ProjectStatus.OPEN != ProjectStatus.CLOSED

    def test_known_values(self) -> None:
        valid = {ProjectStatus.CLOSED, ProjectStatus.OPEN, ProjectStatus.ERROR}
        assert "open" in valid
        assert "closed" in valid
        assert "error" in valid
        assert "unknown" not in valid


class TestFileCategory:
    """FileCategory enum tests."""

    def test_values(self) -> None:
        assert FileCategory.FIRST_PARTY == "first_party"
        assert FileCategory.VENDORED == "vendored"
        assert FileCategory.STUB == "stub"
        assert FileCategory.DEPENDENCY == "dependency"

    def test_category_discrimination(self) -> None:
        assert FileCategory.FIRST_PARTY != FileCategory.VENDORED
        assert FileCategory.STUB != FileCategory.DEPENDENCY


class TestTyProject:
    """TyProject entity tests."""

    def test_creation(self) -> None:
        now = datetime.now(timezone.utc)
        root = Path(components=["home", "user", "proj"])
        project = TyProject(
            root=root,
            status=ProjectStatus.OPEN,
            opened_at=now,
        )
        assert project.root == root
        assert project.status == ProjectStatus.OPEN
        assert project.coordinate_mode == "python"  # default
        assert project.opened_at == now
        assert project.last_reloaded_at is None  # optional

    def test_derived_is_open_true(self) -> None:
        project = TyProject(
            root=Path(components=["r"]),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
        )
        assert project.is_open is True
        assert project.has_error is False

    def test_derived_is_open_false_when_closed(self) -> None:
        project = TyProject(
            root=Path(components=["r"]),
            status=ProjectStatus.CLOSED,
            opened_at=datetime.now(timezone.utc),
        )
        assert project.is_open is False

    def test_derived_has_error_true(self) -> None:
        project = TyProject(
            root=Path(components=["r"]),
            status=ProjectStatus.ERROR,
            opened_at=datetime.now(timezone.utc),
        )
        assert project.has_error is True
        assert project.is_open is False

    def test_optional_fields(self) -> None:
        project = TyProject(
            root=Path(components=["r"]),
            status=ProjectStatus.OPEN,
            python_version="3.13",
            opened_at=datetime.now(timezone.utc),
        )
        assert project.python_version == "3.13"

    def test_optional_fields_null(self) -> None:
        project = TyProject(
            root=Path(components=["r"]),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
        )
        assert project.python_version is None
        assert project.config_path is None
        assert project.last_reloaded_at is None

    def test_set_field_types(self) -> None:
        p1 = Path(components=["a"])
        p2 = Path(components=["b"])
        project = TyProject(
            root=Path(components=["r"]),
            status=ProjectStatus.OPEN,
            extra_search_paths={p1, p2},
            opened_at=datetime.now(timezone.utc),
        )
        assert len(project.extra_search_paths) == 2


class TestProjectFile:
    """ProjectFile entity tests."""

    def test_creation(self) -> None:
        root = Path(components=["r"])
        project = TyProject(
            root=root,
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
        )
        file_path = Path(components=["r", "main.py"])
        pf = ProjectFile(
            path=file_path,
            project=project,
            file_category=FileCategory.FIRST_PARTY,
        )
        assert pf.path == file_path
        assert pf.project == project
        assert pf.file_category == FileCategory.FIRST_PARTY
        assert pf.last_checked_at is None

    def test_relationship_to_project(self) -> None:
        project = TyProject(
            root=Path(components=["r"]),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
        )
        pf = ProjectFile(
            path=Path(components=["r", "f.py"]),
            project=project,
            file_category=FileCategory.FIRST_PARTY,
        )
        assert pf.project.root == project.root
        assert pf.project.status == ProjectStatus.OPEN

    def test_all_categories(self) -> None:
        project = TyProject(
            root=Path(components=["r"]),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
        )
        for cat in [FileCategory.FIRST_PARTY, FileCategory.VENDORED, FileCategory.STUB, FileCategory.DEPENDENCY]:
            pf = ProjectFile(
                path=Path(components=["r", "f"]),
                project=project,
                file_category=cat,
            )
            assert pf.file_category == cat
