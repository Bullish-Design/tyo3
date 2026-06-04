use crate::dto::{ParameterDto, SignatureDto, SignatureHelpDto};

/// Convert a ty_ide SignatureDetails to an owned SignatureDto,
/// rendering all `'db` fields to `String`.
fn convert_signature_details(
    sig: &ty_ide::SignatureDetails<'_>,
) -> SignatureDto {
    let documentation = sig
        .documentation
        .as_ref()
        .map(|doc| doc.render(ty_ide::MarkupKind::Markdown));
    let parameters: Vec<ParameterDto> = sig
        .parameters
        .iter()
        .map(|p| ParameterDto {
            label: p.label.clone(),
            documentation: p.documentation.clone(),
        })
        .collect();

    SignatureDto {
        label: sig.label.clone(),
        documentation,
        parameters,
        active_parameter: sig.active_parameter.map(|i| i as u32),
    }
}

/// Convert a ty_ide SignatureHelpInfo to an owned SignatureHelpDto.
pub fn convert_signature_help(
    info: &ty_ide::SignatureHelpInfo<'_>,
) -> SignatureHelpDto {
    let signatures: Vec<SignatureDto> = info
        .signatures
        .iter()
        .map(convert_signature_details)
        .collect();

    SignatureHelpDto {
        signatures,
        active_signature: info.active_signature.map(|i| i as u32),
    }
}
