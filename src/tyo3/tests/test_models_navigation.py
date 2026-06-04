"""Tests for navigation domain models — tyo3-navigation.allium.

Obligation groups:
- Enum tests (ReferenceKind)
- Entity tests (DefinitionTarget, Reference)
"""

from pathlib import PurePosixPath

from tyo3.models.navigation import DefinitionTarget, Reference, ReferenceKind


class TestReferenceKind:
    """ReferenceKind enum tests."""

    def test_values(self) -> None:
        assert ReferenceKind.READ == "read"
        assert ReferenceKind.WRITE == "write"
        assert ReferenceKind.OTHER == "other"

    def test_distinct(self) -> None:
        assert ReferenceKind.READ != ReferenceKind.WRITE


class TestDefinitionTarget:
    """DefinitionTarget entity tests."""

    def test_required_fields(self) -> None:
        from tyo3.models.analysis import Position, Range

        target = DefinitionTarget(
            path=PurePosixPath("src/main.py"),
            range=Range(start=Position(line=1, column=1), end=Position(line=5, column=1)),
        )
        assert target.module_name is None
        assert target.symbol is None
        assert target.selection_range is None

    def test_with_optional_fields(self, symbol) -> None:
        from tyo3.models.analysis import Position, Range

        target = DefinitionTarget(
            path=PurePosixPath("src/main.py"),
            range=Range(start=Position(line=1, column=1), end=Position(line=5, column=1)),
            selection_range=Range(start=Position(line=2, column=1), end=Position(line=2, column=10)),
            symbol=symbol,
            module_name="my_module",
        )
        assert target.symbol == symbol
        assert target.module_name == "my_module"


class TestReference:
    """Reference entity tests."""

    def test_creation(self) -> None:
        from tyo3.models.analysis import Position, Range

        ref = Reference(
            path=PurePosixPath("src/main.py"),
            range=Range(start=Position(line=10, column=5), end=Position(line=10, column=15)),
            kind=ReferenceKind.READ,
        )
        assert ref.kind == ReferenceKind.READ

    def test_all_reference_kinds(self) -> None:
        from tyo3.models.analysis import Position, Range

        r = Range(start=Position(line=1, column=1), end=Position(line=1, column=2))
        for kind in [ReferenceKind.READ, ReferenceKind.WRITE, ReferenceKind.OTHER]:
            ref = Reference(path=PurePosixPath("f.py"), range=r, kind=kind)
            assert ref.kind == kind
