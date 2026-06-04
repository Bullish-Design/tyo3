/// Information about a single parameter in a signature.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ParameterDto {
    pub label: String,
    pub documentation: Option<String>,
}

/// Information about a single function signature.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SignatureDto {
    pub label: String,
    pub documentation: Option<String>,
    pub parameters: Vec<ParameterDto>,
    pub active_parameter: Option<u32>,
}

/// Signature help information for function calls.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SignatureHelpDto {
    pub signatures: Vec<SignatureDto>,
    pub active_signature: Option<u32>,
}
