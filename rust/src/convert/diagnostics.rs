use ruff_db::diagnostic::{Diagnostic, DiagnosticId};

use crate::dto::DiagnosticDto;

/// Convert a slice of ruff_db diagnostics into DiagnosticDto objects.
///
/// ruff_db's `Diagnostic` carries severity, id, primary message, and
/// sub-diagnostics. File/range information is attached to annotations,
/// not the diagnostic itself, so we leave `file` and `range` as `None`.
pub fn convert_diagnostics(diagnostics: &[Diagnostic]) -> Vec<DiagnosticDto> {
    diagnostics
        .iter()
        .map(|d| {
            let severity = severity_to_string(d.severity());

            DiagnosticDto {
                file: None,
                range: None,
                severity,
                code: diagnostic_id_to_code(d.id()),
                message: d.primary_message().to_string(),
                details: vec![],
            }
        })
        .collect()
}

/// Convert a DiagnosticId to an optional code string.
fn diagnostic_id_to_code(id: DiagnosticId) -> Option<String> {
    match id {
        DiagnosticId::Lint(name) => Some(name.as_str().to_string()),
        _ => None,
    }
}

/// Convert a ruff_db Severity to a lowercase string matching the Pydantic model.
fn severity_to_string(severity: ruff_db::diagnostic::Severity) -> String {
    match severity {
        ruff_db::diagnostic::Severity::Fatal => "fatal".to_string(),
        ruff_db::diagnostic::Severity::Error => "error".to_string(),
        ruff_db::diagnostic::Severity::Warning => "warning".to_string(),
        ruff_db::diagnostic::Severity::Info => "information".to_string(),
    }
}
