"""Tests for symbol domain models — tyo3-symbols.allium.

Obligation groups:
- Enum tests (SymbolKind)
- Entity tests (Symbol)
"""

from tyo3.models.analysis import FileRange, Position, Range
from tyo3.models.symbols import Symbol, SymbolKind


def _file_range(path) -> FileRange:
    return FileRange(
        path=path,
        range=Range(start=Position(line=1, column=1), end=Position(line=1, column=1)),
    )


class TestSymbolKind:
    """SymbolKind enum tests."""

    def test_values(self) -> None:
        assert SymbolKind.MODULE == "module"
        assert SymbolKind.CLASS_ == "class_"
        assert SymbolKind.FUNCTION == "function"
        assert SymbolKind.METHOD == "method"
        assert SymbolKind.CONSTRUCTOR == "constructor"
        assert SymbolKind.VARIABLE == "variable"
        assert SymbolKind.CONSTANT == "constant"
        assert SymbolKind.FIELD == "field"
        assert SymbolKind.PARAMETER == "parameter"
        assert SymbolKind.PROPERTY == "property"
        assert SymbolKind.TYPE_PARAMETER == "type_parameter"
        assert SymbolKind.IMPORT_ == "import_"
        assert SymbolKind.UNKNOWN == "unknown"

    def test_variants_are_distinct(self) -> None:
        kinds = {SymbolKind.MODULE, SymbolKind.CLASS_, SymbolKind.FUNCTION}
        assert len(kinds) == 3


class TestSymbol:
    """Symbol entity tests."""

    def test_required_fields(self, open_project, first_party_file) -> None:
        sym = Symbol(
            project=open_project,
            name="foo",
            kind=SymbolKind.FUNCTION,
            location=_file_range(first_party_file.path),
        )
        assert sym.project == open_project
        assert sym.name == "foo"
        assert sym.kind == SymbolKind.FUNCTION
        assert sym.qualified_name is None
        assert sym.selection_range is None
        assert sym.container_name is None
        assert sym.deprecated is False

    def test_symbol_with_qualified_name(self, open_project, first_party_file) -> None:
        sym = Symbol(
            project=open_project,
            name="MyClass",
            qualified_name="pkg.module.MyClass",
            kind=SymbolKind.CLASS_,
            location=_file_range(first_party_file.path),
            container_name="module",
            deprecated=True,
        )
        assert sym.qualified_name == "pkg.module.MyClass"
        assert sym.container_name == "module"
        assert sym.deprecated is True

    def test_all_symbol_kinds_creatable(self, open_project, first_party_file) -> None:
        for kind_name in [
            "module", "class_", "function", "method", "constructor",
            "variable", "constant", "field", "parameter", "property",
            "type_parameter", "import_", "unknown",
        ]:
            sym = Symbol(
                project=open_project,
                name="sym",
                kind=kind_name,
                location=_file_range(first_party_file.path),
            )
            assert sym.kind == kind_name
