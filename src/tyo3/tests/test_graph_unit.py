"""Unit tests for graph model types and enums (no native extension required)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tyo3.graph import EdgeData, EdgeKind, SymbolNode
from tyo3.graph.identity import make_module_durable_id
from tyo3.graph.models import ReferenceRole
from tyo3.models.symbols import SymbolKind


class TestSymbolIdentity:
    def test_make_module_durable_id(self) -> None:
        """make_module_durable_id creates a stable synthetic id from a file path."""
        did = make_module_durable_id("src/models.py")
        assert did == "<module>src/models.py"

    def test_make_module_durable_id_nested(self) -> None:
        """Module ids are file-level only, not per-symbol."""
        did = make_module_durable_id("src/pkg/__init__.py")
        assert did == "<module>src/pkg/__init__.py"


class TestEdgeData:
    def test_frozen(self) -> None:
        edge = EdgeData(kind=EdgeKind.REFERENCES)
        with pytest.raises(AttributeError):
            edge.kind = EdgeKind.IMPORTS  # type: ignore[misc]


class TestSymbolNode:
    def test_frozen(self) -> None:
        node = SymbolNode(
            durable_id="test::Foo",
            name="Foo",
            qualified_name="Foo",
            kind=SymbolKind.CLASS,
            file="test.py",
            range={"start": {"line": 1, "column": 1}, "end": {"line": 10, "column": 1}},
        )
        with pytest.raises(ValidationError):
            node.name = "Bar"  # type: ignore[misc]

    def test_optional_fields_default(self) -> None:
        node = SymbolNode(
            durable_id="01KTCTESTTESTTESTTESTTES01",
            name="Foo",
            qualified_name="Foo",
            kind=SymbolKind.CLASS,
            file="test.py",
            range={"start": {"line": 1, "column": 1}, "end": {"line": 10, "column": 1}},
        )
        assert node.content_hash is None
        assert node.external is False
        assert node.package is None
        assert node.selection_range is None


class TestReferenceRoleEnum:
    def test_all_roles_are_valid_str_enum(self) -> None:
        assert ReferenceRole.READ == "read"
        assert ReferenceRole.WRITE == "write"
        assert ReferenceRole.IMPORT == "import"
        assert ReferenceRole.DEFINITION == "definition"
        assert ReferenceRole.OTHER == "other"


class TestEdgeKindEnum:
    def test_all_kinds_are_valid_str_enum(self) -> None:
        assert EdgeKind.DEFINES == "defines"
        assert EdgeKind.CONTAINS == "contains"
        assert EdgeKind.REFERENCES == "references"
        assert EdgeKind.IMPORTS == "imports"
        assert EdgeKind.INHERITS == "inherits"
        assert EdgeKind.OVERRIDES == "overrides"
        assert EdgeKind.TYPE_OF == "type_of"
        assert EdgeKind.RETURNS == "returns"
        assert EdgeKind.INSTANTIATES == "instantiates"
