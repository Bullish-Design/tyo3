"""Tests for symbol domain models — tyo3-symbols.allium.

Obligation groups:
- Enum tests (SymbolKind)
- Entity tests (Symbol)
"""

from pathlib import PurePosixPath

from tyo3.models.analysis import FileRange, Position, Range
from tyo3.models.symbols import Symbol, SymbolKind

SAMPLE_PATH = PurePosixPath("src/main.py")


def _file_range(path=SAMPLE_PATH) -> FileRange:
    return FileRange(
        path=path,
        range=Range(start=Position(line=1, column=1), end=Position(line=1, column=1)),
    )


class TestSymbolKind:
    """SymbolKind enum tests."""

    def test_values(self) -> None:
        assert SymbolKind.MODULE == "module"
        assert SymbolKind.CLASS == "class_"
        assert SymbolKind.FUNCTION == "function"
        assert SymbolKind.METHOD == "method"
        assert SymbolKind.CONSTRUCTOR == "constructor"
        assert SymbolKind.VARIABLE == "variable"
        assert SymbolKind.CONSTANT == "constant"
        assert SymbolKind.FIELD == "field"
        assert SymbolKind.PARAMETER == "parameter"
        assert SymbolKind.PROPERTY == "property"
        assert SymbolKind.TYPE_PARAMETER == "type_parameter"
        assert SymbolKind.IMPORT == "import_"
        assert SymbolKind.UNKNOWN == "unknown"

    def test_variants_are_distinct(self) -> None:
        kinds = {SymbolKind.MODULE, SymbolKind.CLASS, SymbolKind.FUNCTION}
        assert len(kinds) == 3


class TestSymbol:
    """Symbol entity tests."""

    def test_required_fields(self) -> None:
        sym = Symbol(
            name="foo",
            kind=SymbolKind.FUNCTION,
            location=_file_range(),
        )
        assert sym.name == "foo"
        assert sym.kind == SymbolKind.FUNCTION
        assert sym.qualified_name is None
        assert sym.selection_range is None
        assert sym.container_name is None
        assert sym.deprecated is False

    def test_symbol_with_qualified_name(self) -> None:
        sym = Symbol(
            name="MyClass",
            qualified_name="pkg.module.MyClass",
            kind=SymbolKind.CLASS,
            location=_file_range(),
            container_name="module",
            deprecated=True,
        )
        assert sym.qualified_name == "pkg.module.MyClass"
        assert sym.container_name == "module"
        assert sym.deprecated is True

    def test_all_symbol_kinds_creatable(self) -> None:
        for kind_name in [
            "module",
            "class_",
            "function",
            "method",
            "constructor",
            "variable",
            "constant",
            "field",
            "parameter",
            "property",
            "type_parameter",
            "import_",
            "unknown",
        ]:
            sym = Symbol(
                name="sym",
                kind=kind_name,
                location=_file_range(),
            )
            assert sym.kind == kind_name
