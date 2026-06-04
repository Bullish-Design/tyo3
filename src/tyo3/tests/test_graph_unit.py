"""Unit tests for graph model types and enums (no native extension required)."""

from __future__ import annotations

import pytest

from tyo3.graph import EdgeData, EdgeKind, SymbolNode
from tyo3.graph.identity import make_symbol_id
from tyo3.graph.models import ReferenceRole
from tyo3.models.symbols import SymbolKind


class TestSymbolIdentity:
    def test_make_symbol_id(self) -> None:
        sid = make_symbol_id("src/models.py", "User")
        assert sid == "src/models.py::User"

    def test_make_symbol_id_nested(self) -> None:
        sid = make_symbol_id("src/models.py", "User.save")
        assert sid == "src/models.py::User.save"


class TestEdgeData:
    def test_frozen(self) -> None:
        edge = EdgeData(kind=EdgeKind.REFERENCES)
        with pytest.raises(AttributeError):
            edge.kind = EdgeKind.IMPORTS  # type: ignore[misc]


class TestSymbolNode:
    def test_frozen(self) -> None:
        node = SymbolNode(
            symbol_id="test::Foo",
            name="Foo",
            qualified_name="Foo",
            kind=SymbolKind.CLASS,
            file="test.py",
            range={"start": {"line": 1, "column": 1}, "end": {"line": 10, "column": 1}},
        )
        with pytest.raises(TypeError):
            node.name = "Bar"  # type: ignore[misc]

    def test_optional_fields_default(self) -> None:
        node = SymbolNode(
            symbol_id="test::Foo",
            name="Foo",
            qualified_name="Foo",
            kind=SymbolKind.CLASS,
            file="test.py",
            range={"start": {"line": 1, "column": 1}, "end": {"line": 10, "column": 1}},
        )
        assert node.documentation is None
        assert node.signature is None
        assert node.external is False
        assert node.package is None


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
