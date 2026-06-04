use ruff_db::diagnostic::{Diagnostic, DiagnosticId, UnifiedFile};
use ruff_db::source::{line_index, source_text};
use ruff_db::Db;

use crate::coordinates;
use crate::dto::{DiagnosticDto, RangeDto, SeverityDto};

/// Convert a slice of ruff_db diagnostics into DiagnosticDto objects.
///
/// Each diagnostic carries severity, id, primary message, and
/// sub-diagnostics. File/range information is extracted from the
/// diagnostic's primary annotation span.
pub fn convert_diagnostics(
    db: &dyn Db,
    diagnostics: &[Diagnostic],
) -> Vec<DiagnosticDto> {
    diagnostics
        .iter()
        .map(|d| {
            let (file, range) = extract_file_and_range(db, d);
            DiagnosticDto {
                file,
                range,
                severity: severity_to_dto(d.severity()),
                code: diagnostic_id_to_code(d.id()),
                message: d.primary_message().to_string(),
                details: d.sub_diagnostics()
                    .iter()
                    .map(|s| s.concise_message().to_string())
                    .collect(),
            }
        })
        .collect()
}

/// Extract the file path and text range from a diagnostic's primary annotation.
///
/// Returns `(None, None)` if the diagnostic has no primary annotation with a
/// ty `File` span.
fn extract_file_and_range(
    db: &dyn Db,
    d: &Diagnostic,
) -> (Option<String>, Option<RangeDto>) {
    let annotation = match d.primary_annotation() {
        Some(a) => a,
        None => return (None, None),
    };
    let span = annotation.get_span();

    // All diagnostics from the ty checker carry ty `File` spans.
    // If we ever encounter a Ruff `SourceFile`, it's a bug we should catch.
    let file = match span.file() {
        UnifiedFile::Ty(file) => *file,
        UnifiedFile::Ruff(_) => return (None, None),
    };
    let path = file.path(db).as_str().to_string();

    let range = span.range().map(|r| {
        let idx = line_index(db, file);
        let src = source_text(db, file);
        coordinates::range_to_dto_with_index(src.as_str(), &idx, r)
    });

    (Some(path), range)
}

/// Check if a diagnostic's primary annotation is for the given file path.
///
/// Extracts the file from the primary annotation span and compares it to
/// *target_path* without computing the full RangeDto — only the file path is
/// needed for filtering. Returns `false` if the diagnostic has no primary
/// annotation or no ty `File` span.
pub fn diagnostic_matches_file(db: &dyn Db, d: &Diagnostic, target_path: &str) -> bool {
    let annotation = match d.primary_annotation() {
        Some(a) => a,
        None => return false,
    };
    let span = annotation.get_span();
    let file = match span.file() {
        UnifiedFile::Ty(file) => *file,
        UnifiedFile::Ruff(_) => return false,
    };
    let path = file.path(db).as_str();
    path == target_path
}

/// Convert a slice of diagnostic references (used by `check_file`).
///
/// Unlike `convert_diagnostics`, this takes `&[&Diagnostic]` references
/// rather than a borrowed slice of owned `Diagnostic` values.
pub fn convert_diagnostic_refs(
    db: &dyn Db,
    diagnostics: &[&Diagnostic],
) -> Vec<DiagnosticDto> {
    diagnostics
        .iter()
        .map(|d| {
            let (file, range) = extract_file_and_range(db, d);
            DiagnosticDto {
                file,
                range,
                severity: severity_to_dto(d.severity()),
                code: diagnostic_id_to_code(d.id()),
                message: d.primary_message().to_string(),
                details: d.sub_diagnostics()
                    .iter()
                    .map(|s| s.concise_message().to_string())
                    .collect(),
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

/// Convert a ruff_db Severity to a SeverityDto.
fn severity_to_dto(severity: ruff_db::diagnostic::Severity) -> SeverityDto {
    match severity {
        ruff_db::diagnostic::Severity::Fatal => SeverityDto::Fatal,
        ruff_db::diagnostic::Severity::Error => SeverityDto::Error,
        ruff_db::diagnostic::Severity::Warning => SeverityDto::Warning,
        ruff_db::diagnostic::Severity::Info => SeverityDto::Information,
    }
}
