use ruff_source_file::LineIndex;

use crate::coordinates;
use crate::dto::{SemanticTokenDto, SemanticTokenModifierDto, SemanticTokenTypeDto};

/// Map a `ty_ide::SemanticTokenType` to our DTO enum.
fn token_type_to_dto(tt: &ty_ide::SemanticTokenType) -> SemanticTokenTypeDto {
    match tt {
        ty_ide::SemanticTokenType::Namespace => SemanticTokenTypeDto::Namespace,
        ty_ide::SemanticTokenType::Class => SemanticTokenTypeDto::Class,
        ty_ide::SemanticTokenType::Parameter => SemanticTokenTypeDto::Parameter,
        ty_ide::SemanticTokenType::SelfParameter => SemanticTokenTypeDto::SelfParameter,
        ty_ide::SemanticTokenType::ClsParameter => SemanticTokenTypeDto::ClsParameter,
        ty_ide::SemanticTokenType::Variable => SemanticTokenTypeDto::Variable,
        ty_ide::SemanticTokenType::Property => SemanticTokenTypeDto::Property,
        ty_ide::SemanticTokenType::Function => SemanticTokenTypeDto::Function,
        ty_ide::SemanticTokenType::Method => SemanticTokenTypeDto::Method,
        ty_ide::SemanticTokenType::Keyword => SemanticTokenTypeDto::Keyword,
        ty_ide::SemanticTokenType::String => SemanticTokenTypeDto::String,
        ty_ide::SemanticTokenType::Number => SemanticTokenTypeDto::Number,
        ty_ide::SemanticTokenType::Decorator => SemanticTokenTypeDto::Decorator,
        ty_ide::SemanticTokenType::BuiltinConstant => SemanticTokenTypeDto::BuiltinConstant,
        ty_ide::SemanticTokenType::TypeParameter => SemanticTokenTypeDto::TypeParameter,
    }
}

/// Map `ty_ide::SemanticTokenModifier` bitflags to a Vec of DTO enum variants.
fn modifiers_to_dto(modifiers: ty_ide::SemanticTokenModifier) -> Vec<SemanticTokenModifierDto> {
    let mut result = Vec::new();
    if modifiers.contains(ty_ide::SemanticTokenModifier::DEFINITION) {
        result.push(SemanticTokenModifierDto::Definition);
    }
    if modifiers.contains(ty_ide::SemanticTokenModifier::READONLY) {
        result.push(SemanticTokenModifierDto::Readonly);
    }
    if modifiers.contains(ty_ide::SemanticTokenModifier::ASYNC) {
        result.push(SemanticTokenModifierDto::Async);
    }
    if modifiers.contains(ty_ide::SemanticTokenModifier::DOCUMENTATION) {
        result.push(SemanticTokenModifierDto::Documentation);
    }
    result
}

/// Convert a `ty_ide::SemanticTokens` result to a Vec of DTOs.
pub fn convert_semantic_tokens(
    source: &str,
    line_index: &LineIndex,
    tokens: &ty_ide::SemanticTokens,
) -> Vec<SemanticTokenDto> {
    tokens
        .iter()
        .map(|t| SemanticTokenDto {
            range: coordinates::range_to_dto_with_index(source, line_index, t.range),
            token_type: token_type_to_dto(&t.token_type),
            modifiers: modifiers_to_dto(t.modifiers),
        })
        .collect()
}
