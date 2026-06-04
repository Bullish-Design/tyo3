"""Tests for the Rust-Python bridge layer (native type conversion)."""

from __future__ import annotations

import pytest

try:
    from tyo3 import _HAS_NATIVE
    from tyo3 import _native_impl as _native
except ImportError:
    _HAS_NATIVE = False
    _native = None

needs_native = pytest.mark.skipif(
    not _HAS_NATIVE, reason="Rust native extension not built"
)


# All struct DTOs that should have __fields__
EXPECTED_STRUCT_TYPES = [
    "NativePosition",
    "NativeRange",
    "NativeFileRange",
    "NativeSymbol",
    "NativeDiagnostic",
    "NativeCheckResult",
    "NativeDefinitionTarget",
    "NativeReference",
    "NativeHoverContent",
    "NativeHover",
    "NativeTypeHierarchyItem",
    "NativeTypeHierarchy",
    "NativeNameOccurrence",
    "NativeSemanticToken",
]

# All enum types that should be in _TYO3_ENUM_TYPES
EXPECTED_ENUM_TYPES = [
    "NativeSymbolKind",
    "NativeSeverity",
    "NativeReferenceKind",
    "NativeReferenceRole",
    "NativeSemanticTokenType",
    "NativeSemanticTokenModifier",
    "NativeHoverContentKind",
]


@needs_native
class TestFieldsClassattr:
    """Verify every struct DTO exposes __fields__ with correct type tags."""

    @pytest.mark.parametrize("type_name", EXPECTED_STRUCT_TYPES)
    def test_struct_has_fields(self, type_name: str) -> None:
        cls = getattr(_native, type_name)
        fields = cls.__fields__
        assert isinstance(fields, list), f"{type_name}.__fields__ should be a list"
        assert len(fields) > 0, f"{type_name}.__fields__ should not be empty"
        for entry in fields:
            assert isinstance(entry, tuple) and len(entry) == 2, (
                f"{type_name}.__fields__ entries should be (name, tag) tuples"
            )
            name, tag = entry
            assert isinstance(name, str) and isinstance(tag, str)

    @pytest.mark.parametrize("type_name", EXPECTED_STRUCT_TYPES)
    def test_fields_have_valid_tags(self, type_name: str) -> None:
        """Every field tag must be from the known set."""
        cls = getattr(_native, type_name)
        valid_tag_prefixes = {"str", "int", "float", "bool", "obj", "opt:", "list:"}
        for name, tag in cls.__fields__:
            assert name.isidentifier(), f"{type_name}.{name} is not a valid identifier"
            assert any(tag == p or tag.startswith(p) for p in valid_tag_prefixes), (
                f"{type_name}.{name} has unknown tag: {tag}"
            )

    def test_no_struct_missing_fields(self) -> None:
        """Catch new struct DTOs that forgot #[derive(PyFields)]."""
        known = set(EXPECTED_STRUCT_TYPES) | set(EXPECTED_ENUM_TYPES)
        skip = {"TyProject", "ProjectClosedError", "PathResolutionError",
                "PositionError", "AnalysisError"}
        for name in dir(_native):
            if name.startswith("_"):
                continue
            obj = getattr(_native, name)
            if not isinstance(obj, type):
                continue
            if name in skip or name in known:
                continue
            assert hasattr(obj, "__fields__"), (
                f"New native type {name} found without __fields__. "
                f"Add #[derive(PyFields)] to its Rust definition and add it "
                f"to EXPECTED_STRUCT_TYPES in this test."
            )


@needs_native
class TestEnumTypeRegistry:
    """Verify the _TYO3_ENUM_TYPES module attribute."""

    def test_enum_types_exists(self) -> None:
        assert hasattr(_native, "_TYO3_ENUM_TYPES"), (
            "_TYO3_ENUM_TYPES not found on native module"
        )

    def test_enum_types_complete(self) -> None:
        enum_types = set(_native._TYO3_ENUM_TYPES)
        for name in EXPECTED_ENUM_TYPES:
            cls = getattr(_native, name)
            assert cls in enum_types, (
                f"{name} missing from _TYO3_ENUM_TYPES in lib.rs"
            )

    def test_enum_types_count(self) -> None:
        assert len(_native._TYO3_ENUM_TYPES) == len(EXPECTED_ENUM_TYPES), (
            f"Expected {len(EXPECTED_ENUM_TYPES)} enum types, "
            f"got {len(_native._TYO3_ENUM_TYPES)}. Update this test if a "
            f"new enum was intentionally added."
        )

    def test_no_enum_missing(self) -> None:
        """Catch new enum types added to the module but not to _TYO3_ENUM_TYPES."""
        enum_types = set(_native._TYO3_ENUM_TYPES)
        skip = {"TyProject", "ProjectClosedError", "PathResolutionError",
                "PositionError", "AnalysisError"}
        for name in dir(_native):
            if name.startswith("_"):
                continue
            obj = getattr(_native, name)
            if not isinstance(obj, type):
                continue
            if name in skip:
                continue
            if hasattr(obj, "__fields__"):
                continue  # It's a struct
            if obj not in enum_types:
                pytest.fail(
                    f"Native type {name} is not in _TYO3_ENUM_TYPES and has no "
                    f"__fields__. If it's an enum, add it to _TYO3_ENUM_TYPES in "
                    f"lib.rs and EXPECTED_ENUM_TYPES in this test."
                )


@needs_native
class TestToPythonConversion:
    """Integration tests for _to_python with real native objects."""

    def test_position_roundtrip(self) -> None:
        from tyo3.rust_project import _to_python
        pos = _native.NativePosition(line=1, column=5)
        result = _to_python(pos)
        assert result == {"line": 1, "column": 5}

    def test_range_roundtrip(self) -> None:
        from tyo3.rust_project import _to_python
        r = _native.NativeRange(
            start=_native.NativePosition(1, 1),
            end=_native.NativePosition(1, 10),
        )
        result = _to_python(r)
        assert result == {
            "start": {"line": 1, "column": 1},
            "end": {"line": 1, "column": 10},
        }

    def test_enum_becomes_string(self) -> None:
        from tyo3.rust_project import _to_python
        kind = _native.NativeSymbolKind.Function
        result = _to_python(kind)
        assert result == "function"

    def test_optional_none(self) -> None:
        from tyo3.rust_project import _to_python
        diag = _native.NativeDiagnostic(
            severity=_native.NativeSeverity.Error,
            message="test",
        )
        result = _to_python(diag)
        assert result["file"] is None
        assert result["code"] is None
        assert result["message"] == "test"
        assert result["severity"] == "error"

    def test_list_field(self) -> None:
        from tyo3.rust_project import _to_python
        diag = _native.NativeDiagnostic(
            severity=_native.NativeSeverity.Warning,
            message="test",
            details=["detail1", "detail2"],
        )
        result = _to_python(diag)
        assert result["details"] == ["detail1", "detail2"]

    def test_primitives_passthrough(self) -> None:
        from tyo3.rust_project import _to_python
        assert _to_python(None) is None
        assert _to_python(True) is True
        assert _to_python(42) == 42
        assert _to_python(3.14) == 3.14
        assert _to_python("hello") == "hello"

    def test_list_passthrough(self) -> None:
        from tyo3.rust_project import _to_python
        assert _to_python([1, 2, 3]) == [1, 2, 3]
        assert _to_python(("a", "b")) == ["a", "b"]
