"""Tests for advanced semantic domain models — tyo3-advanced.allium.

Obligation groups:
- Enum tests (SemanticTokenType, SemanticTokenModifier)
- Entity tests (SemanticToken)
- Deferred spec recognition
"""

from tyo3.models.advanced import SemanticToken, SemanticTokenModifier, SemanticTokenType


class TestSemanticTokenType:
    """SemanticTokenType enum tests."""

    def test_values(self) -> None:
        assert SemanticTokenType.NAMESPACE == "namespace"
        assert SemanticTokenType.CLASS_ == "class_"
        assert SemanticTokenType.PARAMETER == "parameter"
        assert SemanticTokenType.SELF_PARAMETER == "self_parameter"
        assert SemanticTokenType.CLS_PARAMETER == "cls_parameter"
        assert SemanticTokenType.VARIABLE == "variable"
        assert SemanticTokenType.PROPERTY == "property"
        assert SemanticTokenType.FUNCTION == "function"
        assert SemanticTokenType.METHOD == "method"
        assert SemanticTokenType.KEYWORD == "keyword"
        assert SemanticTokenType.STRING == "string"
        assert SemanticTokenType.NUMBER == "number"
        assert SemanticTokenType.DECORATOR == "decorator"
        assert SemanticTokenType.BUILTIN_CONSTANT == "builtin_constant"
        assert SemanticTokenType.TYPE_PARAMETER == "type_parameter"

    def test_all_distinct(self) -> None:
        types = {
            SemanticTokenType.NAMESPACE, SemanticTokenType.CLASS_,
            SemanticTokenType.PARAMETER, SemanticTokenType.FUNCTION,
            SemanticTokenType.METHOD, SemanticTokenType.KEYWORD,
        }
        assert len(types) == 6


class TestSemanticTokenModifier:
    """SemanticTokenModifier enum tests."""

    def test_values(self) -> None:
        assert SemanticTokenModifier.DEFINITION == "definition"
        assert SemanticTokenModifier.READONLY == "readonly"
        assert SemanticTokenModifier.ASYNC_ == "async_"
        assert SemanticTokenModifier.DOCUMENTATION == "documentation"

    def test_distinct(self) -> None:
        assert SemanticTokenModifier.DEFINITION != SemanticTokenModifier.READONLY


class TestSemanticToken:
    """SemanticToken entity tests."""

    def test_creation(self, open_project, first_party_file) -> None:
        from tyo3.models.analysis import Position, Range

        token = SemanticToken(
            file=first_party_file,
            range=Range(start=Position(line=1, column=1), end=Position(line=1, column=10)),
            token_type=SemanticTokenType.FUNCTION,
        )
        assert token.file == first_party_file
        assert token.token_type == SemanticTokenType.FUNCTION
        assert token.modifiers == set()

    def test_with_modifiers(self, open_project, first_party_file) -> None:
        from tyo3.models.analysis import Position, Range

        token = SemanticToken(
            file=first_party_file,
            range=Range(start=Position(line=1, column=1), end=Position(line=1, column=10)),
            token_type=SemanticTokenType.FUNCTION,
            modifiers={SemanticTokenModifier.DEFINITION, SemanticTokenModifier.READONLY},
        )
        assert len(token.modifiers) == 2
        assert SemanticTokenModifier.DEFINITION in token.modifiers
