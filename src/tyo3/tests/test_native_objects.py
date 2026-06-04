"""Tests for native PyO3 DTO objects at the Rust-Python boundary.

Verifies the transport layer that feeds ``model_validate()``:
- Native objects survive a Python roundtrip (repr, eq, hash)
- Every Pydantic model roundtrips through its native counterpart
- Frozen native objects reject mutation

These tests require the Rust native extension to be built.
Run with: PYTHONPATH=src pytest src/tyo3/tests/test_native_objects.py -v
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

# Check if native extension is available
try:
    from tyo3._native_impl import (  # type: ignore[import-untyped]
        NativeCheckResult,
        NativeDefinitionTarget,
        NativeDiagnostic,
        NativeFileRange,
        NativeHover,
        NativeHoverContent,
        NativeHoverContentKind,
        NativePosition,
        NativeRange,
        NativeReference,
        NativeReferenceKind,
        NativeSeverity,
        NativeSymbol,
        NativeSymbolKind,
    )

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False

from tyo3.models.analysis import (
    CheckResult,
    Diagnostic,
    DiagnosticSeverity,
    FileRange,
    Position,
    Range,
)
from tyo3.models.navigation import (
    DefinitionTarget,
    HoverContent,
    HoverContentKind,
    HoverResult,
    Reference,
    ReferenceKind,
)
from tyo3.models.symbols import Symbol, SymbolKind

# ── Skip marker ───────────────────────────────────────────────────────────

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")


# ═══════════════════════════════════════════════════════════════════════════
# Native object __repr__, __eq__, __hash__ tests
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
class TestNativePosition:
    """NativePosition: repr, eq, hash, frozen."""

    def test_repr(self) -> None:
        p = NativePosition(1, 5)
        r = repr(p)
        assert "NativePosition" in r
        assert "line=1" in r
        assert "column=5" in r

    def test_eq(self) -> None:
        a = NativePosition(1, 1)
        b = NativePosition(1, 1)
        c = NativePosition(2, 1)
        assert a == b
        assert a != c

    def test_hash(self) -> None:
        a = NativePosition(1, 1)
        b = NativePosition(1, 1)
        assert hash(a) == hash(b)
        s = {a, b}
        assert len(s) == 1

    def test_frozen(self) -> None:
        p = NativePosition(1, 5)
        with pytest.raises((AttributeError, TypeError)):
            p.line = 2  # type: ignore[misc]


@needs_native
class TestNativeRange:
    """NativeRange: repr, eq, hash, frozen."""

    def test_repr(self) -> None:
        r = NativeRange(NativePosition(1, 1), NativePosition(1, 10))
        rep = repr(r)
        assert "NativeRange" in rep

    def test_eq(self) -> None:
        a = NativeRange(NativePosition(1, 1), NativePosition(1, 10))
        b = NativeRange(NativePosition(1, 1), NativePosition(1, 10))
        c = NativeRange(NativePosition(2, 1), NativePosition(2, 10))
        assert a == b
        assert a != c

    def test_hash(self) -> None:
        a = NativeRange(NativePosition(1, 1), NativePosition(1, 10))
        b = NativeRange(NativePosition(1, 1), NativePosition(1, 10))
        assert hash(a) == hash(b)

    def test_frozen(self) -> None:
        r = NativeRange(NativePosition(1, 1), NativePosition(1, 10))
        with pytest.raises((AttributeError, TypeError)):
            r.start = NativePosition(2, 1)  # type: ignore[misc]


@needs_native
class TestNativeFileRange:
    """NativeFileRange: repr, eq, hash, frozen."""

    def test_repr(self) -> None:
        fr = NativeFileRange("src/main.py", NativeRange(NativePosition(1, 1), NativePosition(1, 10)))
        rep = repr(fr)
        assert "NativeFileRange" in rep

    def test_eq(self) -> None:
        rng = NativeRange(NativePosition(1, 1), NativePosition(1, 10))
        a = NativeFileRange("src/main.py", rng)
        b = NativeFileRange("src/main.py", rng)
        assert a == b

    def test_frozen(self) -> None:
        fr = NativeFileRange("src/main.py", NativeRange(NativePosition(1, 1), NativePosition(1, 10)))
        with pytest.raises((AttributeError, TypeError)):
            fr.path = "other.py"  # type: ignore[misc]


@needs_native
class TestNativeEnums:
    """Native enum types: __str__, __repr__, eq."""

    def test_symbol_kind_str(self) -> None:
        assert str(NativeSymbolKind.Function) == "function"
        assert str(NativeSymbolKind.Class) == "class_"
        assert str(NativeSymbolKind.Module) == "module"
        assert str(NativeSymbolKind.Import) == "import_"
        assert str(NativeSymbolKind.Unknown) == "unknown"

    def test_symbol_kind_repr(self) -> None:
        assert "NativeSymbolKind" in repr(NativeSymbolKind.Function)

    def test_symbol_kind_eq(self) -> None:
        assert NativeSymbolKind.Function == NativeSymbolKind.Function
        assert NativeSymbolKind.Function != NativeSymbolKind.Class

    def test_severity_str(self) -> None:
        assert str(NativeSeverity.Error) == "error"
        assert str(NativeSeverity.Warning) == "warning"
        assert str(NativeSeverity.Fatal) == "fatal"
        assert str(NativeSeverity.Information) == "information"
        assert str(NativeSeverity.Hint) == "hint"

    def test_severity_repr(self) -> None:
        assert "NativeSeverity" in repr(NativeSeverity.Error)

    def test_severity_eq(self) -> None:
        assert NativeSeverity.Error == NativeSeverity.Error
        assert NativeSeverity.Error != NativeSeverity.Warning

    def test_reference_kind_str(self) -> None:
        assert str(NativeReferenceKind.Read) == "read"
        assert str(NativeReferenceKind.Write) == "write"
        assert str(NativeReferenceKind.Other) == "other"

    def test_reference_kind_eq(self) -> None:
        assert NativeReferenceKind.Read == NativeReferenceKind.Read
        assert NativeReferenceKind.Read != NativeReferenceKind.Write

    def test_hover_content_kind_str(self) -> None:
        assert str(NativeHoverContentKind.Markdown) == "markdown"
        assert str(NativeHoverContentKind.PlainText) == "plain_text"
        assert str(NativeHoverContentKind.Type) == "type"

    def test_hover_content_kind_eq(self) -> None:
        assert NativeHoverContentKind.Type == NativeHoverContentKind.Type
        assert NativeHoverContentKind.Type != NativeHoverContentKind.Markdown


@needs_native
class TestNativeCompositeStructs:
    """Native composite DTOs: repr, eq, field access."""

    def test_native_symbol_repr(self) -> None:
        sym = NativeSymbol(
            name="greet",
            kind=NativeSymbolKind.Function,
            location=NativeFileRange(
                "src/main.py",
                NativeRange(NativePosition(3, 1), NativePosition(5, 1)),
            ),
        )
        rep = repr(sym)
        assert "NativeSymbol" in rep
        assert "greet" in rep

    def test_native_symbol_field_access(self) -> None:
        sym = NativeSymbol(
            name="greet",
            kind=NativeSymbolKind.Function,
            location=NativeFileRange(
                "src/main.py",
                NativeRange(NativePosition(3, 1), NativePosition(5, 1)),
            ),
            deprecated=True,
        )
        assert sym.name == "greet"
        assert sym.kind == NativeSymbolKind.Function
        assert sym.deprecated is True
        assert sym.location.path == "src/main.py"

    def test_native_symbol_frozen(self) -> None:
        sym = NativeSymbol(
            name="greet",
            kind=NativeSymbolKind.Function,
            location=NativeFileRange(
                "src/main.py",
                NativeRange(NativePosition(3, 1), NativePosition(5, 1)),
            ),
        )
        with pytest.raises((AttributeError, TypeError)):
            sym.name = "other"  # type: ignore[misc]

    def test_native_diagnostic_repr(self) -> None:
        d = NativeDiagnostic(
            severity=NativeSeverity.Error,
            message="Undefined name",
            file="src/main.py",
            code="undefined-name",
        )
        rep = repr(d)
        assert "NativeDiagnostic" in rep
        assert "undefined-name" in rep

    def test_native_check_result_repr(self) -> None:
        cr = NativeCheckResult(diagnostics=[], files_checked=1, elapsed_ms=42)
        rep = repr(cr)
        assert "NativeCheckResult" in rep

    def test_native_hover_repr(self) -> None:
        content = NativeHoverContent(kind=NativeHoverContentKind.Markdown, value="**greet**")
        hover = NativeHover(
            location=NativeFileRange(
                "src/main.py",
                NativeRange(NativePosition(3, 1), NativePosition(5, 1)),
            ),
            contents=[content],
        )
        rep = repr(hover)
        assert "NativeHover" in rep

    def test_native_definition_target_repr(self) -> None:
        target = NativeDefinitionTarget(
            path="src/main.py",
            range=NativeRange(NativePosition(3, 1), NativePosition(5, 1)),
        )
        rep = repr(target)
        assert "NativeDefinitionTarget" in rep

    def test_native_reference_repr(self) -> None:
        ref = NativeReference(
            path="src/main.py",
            range=NativeRange(NativePosition(10, 5), NativePosition(10, 15)),
            kind=NativeReferenceKind.Read,
        )
        rep = repr(ref)
        assert "NativeReference" in rep


# ═══════════════════════════════════════════════════════════════════════════
# model_validate() roundtrip tests
# ═══════════════════════════════════════════════════════════════════════════


def _to_python(obj):
    """Mirror the _to_python conversion from rust_project.py.

    Converts native enum variants to str, structs to dicts.
    This is a standalone version for testing without importing rust_project.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_to_python(item) for item in obj]

    # Native enums: check for __str__ pattern
    if hasattr(type(obj), "__str__") and type(obj).__name__.startswith("Native"):
        s = str(obj)
        # Heuristic: native enum classes are not str themselves but have a short __str__
        if s in {
            "module", "class_", "function", "method", "constructor",
            "variable", "constant", "field", "parameter", "property",
            "type_parameter", "import_", "unknown",
            "fatal", "error", "warning", "information", "hint",
            "read", "write", "other",
            "type", "signature", "docstring", "typed_dict_key",
            "markdown", "plain_text",
        }:
            return s

    # Native struct: convert to dict via attribute access
    result = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            val = getattr(obj, name)
            if not callable(val):
                result[name] = _to_python(val)
        except Exception:
            pass
    return result


@needs_native
class TestModelValidateRoundtrips:
    """Verify every Pydantic model roundtrips through its native counterpart."""

    def test_position_roundtrip(self) -> None:
        native = NativePosition(3, 7)
        data = _to_python(native)
        model = Position.model_validate(data)
        assert model.line == 3
        assert model.column == 7

    def test_range_roundtrip(self) -> None:
        native = NativeRange(NativePosition(1, 1), NativePosition(10, 5))
        data = _to_python(native)
        model = Range.model_validate(data)
        assert model.start.line == 1
        assert model.start.column == 1
        assert model.end.line == 10
        assert model.end.column == 5

    def test_file_range_roundtrip(self) -> None:
        native = NativeFileRange(
            "src/main.py",
            NativeRange(NativePosition(1, 1), NativePosition(5, 1)),
        )
        data = _to_python(native)
        model = FileRange.model_validate(data)
        assert model.path == PurePosixPath("src/main.py")
        assert model.range.start.line == 1

    def test_diagnostic_roundtrip(self) -> None:
        native = NativeDiagnostic(
            severity=NativeSeverity.Error,
            message="Undefined name 'foo'",
            file="src/main.py",
            range=NativeRange(NativePosition(10, 5), NativePosition(10, 8)),
            code="undefined-name",
            details=["foo is not defined"],
        )
        data = _to_python(native)
        model = Diagnostic.model_validate(data)
        assert model.severity == DiagnosticSeverity.ERROR
        assert model.message == "Undefined name 'foo'"
        assert model.file == "src/main.py"
        assert model.range is not None
        assert model.range.start.line == 10
        assert model.code == "undefined-name"

    def test_diagnostic_minimal(self) -> None:
        """Diagnostic with None file/range."""
        native = NativeDiagnostic(
            severity=NativeSeverity.Warning,
            message="Something",
        )
        data = _to_python(native)
        model = Diagnostic.model_validate(data)
        assert model.file is None
        assert model.range is None
        assert model.severity == DiagnosticSeverity.WARNING

    def test_check_result_roundtrip(self) -> None:
        d = NativeDiagnostic(
            severity=NativeSeverity.Error,
            message="Bad thing",
            file="a.py",
        )
        native = NativeCheckResult(
            diagnostics=[d],
            files_checked=3,
            elapsed_ms=150,
        )
        data = _to_python(native)
        model = CheckResult.model_validate(data)
        assert len(model.diagnostics) == 1
        assert model.diagnostics[0].message == "Bad thing"
        assert model.files_checked == 3
        assert model.elapsed_ms == 150

    def test_symbol_roundtrip(self) -> None:
        native = NativeSymbol(
            name="greet",
            kind=NativeSymbolKind.Function,
            location=NativeFileRange(
                "src/main.py",
                NativeRange(NativePosition(3, 1), NativePosition(5, 1)),
            ),
            selection_range=NativeRange(NativePosition(3, 1), NativePosition(3, 5)),
            qualified_name="pkg.greet",
            container_name="pkg",
            deprecated=False,
        )
        data = _to_python(native)
        model = Symbol.model_validate(data)
        assert model.name == "greet"
        assert model.kind == SymbolKind.FUNCTION
        assert model.location.path == PurePosixPath("src/main.py")
        assert model.location.range.start.line == 3
        assert model.qualified_name == "pkg.greet"
        assert model.container_name == "pkg"
        assert model.deprecated is False
        assert model.selection_range is not None
        assert model.selection_range.start.line == 3

    def test_definition_target_roundtrip(self) -> None:
        native = NativeDefinitionTarget(
            path="src/module.py",
            range=NativeRange(NativePosition(5, 1), NativePosition(7, 1)),
            selection_range=NativeRange(NativePosition(5, 1), NativePosition(5, 5)),
            module_name="module",
        )
        data = _to_python(native)
        model = DefinitionTarget.model_validate(data)
        assert model.path == PurePosixPath("src/module.py")
        assert model.range.start.line == 5
        assert model.selection_range is not None
        assert model.module_name == "module"

    def test_reference_roundtrip(self) -> None:
        native = NativeReference(
            path="src/main.py",
            range=NativeRange(NativePosition(10, 5), NativePosition(10, 15)),
            kind=NativeReferenceKind.Write,
        )
        data = _to_python(native)
        model = Reference.model_validate(data)
        assert model.path == PurePosixPath("src/main.py")
        assert model.kind == ReferenceKind.WRITE

    def test_hover_content_roundtrip(self) -> None:
        native = NativeHoverContent(
            kind=NativeHoverContentKind.Markdown,
            value="**bold**",
        )
        data = _to_python(native)
        model = HoverContent.model_validate(data)
        assert model.kind == HoverContentKind.MARKDOWN
        assert model.value == "**bold**"

    def test_hover_result_roundtrip(self) -> None:
        content = NativeHoverContent(
            kind=NativeHoverContentKind.Type,
            value="str",
        )
        native = NativeHover(
            location=NativeFileRange(
                "src/main.py",
                NativeRange(NativePosition(3, 1), NativePosition(5, 1)),
            ),
            contents=[content],
        )
        data = _to_python(native)
        model = HoverResult.model_validate(data)
        assert len(model.contents) == 1
        assert model.contents[0].kind == HoverContentKind.TYPE
        assert model.contents[0].value == "str"
        assert model.location.path == PurePosixPath("src/main.py")

    def test_hover_result_none_handled(self) -> None:
        """None from hover is handled before model_validate."""
        # This is tested at the RustProject level — just verify the model
        # validates correctly
        hc = HoverContent(kind=HoverContentKind.PLAIN_TEXT, value="text")
        hr = HoverResult(
            location=FileRange(
                path=PurePosixPath("f.py"),
                range=Range(start=Position(line=1, column=1), end=Position(line=1, column=1)),
            ),
            contents=[hc],
        )
        assert hr.contents[0].value == "text"


# ═══════════════════════════════════════════════════════════════════════════
# Helper: independent _to_python correctness
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
class TestToPythonConversion:
    """Verify the _to_python helper correctly converts native object graphs."""

    def test_simple_position(self) -> None:
        native = NativePosition(1, 5)
        data = _to_python(native)
        assert data == {"line": 1, "column": 5}

    def test_nested_range(self) -> None:
        native = NativeRange(NativePosition(1, 1), NativePosition(10, 5))
        data = _to_python(native)
        assert data == {
            "start": {"line": 1, "column": 1},
            "end": {"line": 10, "column": 5},
        }

    def test_list_of_symbols(self) -> None:
        s1 = NativeSymbol(
            name="a",
            kind=NativeSymbolKind.Function,
            location=NativeFileRange(
                "a.py",
                NativeRange(NativePosition(1, 1), NativePosition(1, 1)),
            ),
        )
        s2 = NativeSymbol(
            name="b",
            kind=NativeSymbolKind.Class,
            location=NativeFileRange(
                "b.py",
                NativeRange(NativePosition(1, 1), NativePosition(1, 1)),
            ),
        )
        data = _to_python([s1, s2])
        assert len(data) == 2
        assert data[0]["name"] == "a"
        assert data[0]["kind"] == "function"
        assert data[1]["name"] == "b"
        assert data[1]["kind"] == "class_"

    def test_none_passthrough(self) -> None:
        assert _to_python(None) is None

    def test_str_passthrough(self) -> None:
        assert _to_python("hello") == "hello"

    def test_int_passthrough(self) -> None:
        assert _to_python(42) == 42

    def test_bool_passthrough(self) -> None:
        assert _to_python(True) is True
        assert _to_python(False) is False

    def test_empty_list(self) -> None:
        assert _to_python([]) == []

    def test_tuple(self) -> None:
        assert _to_python((1, 2)) == [1, 2]
