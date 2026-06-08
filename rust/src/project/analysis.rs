//! GIL-free analysis cores (the read-only  LSP surface).
//!
//! Split from `project.rs` (Phase 13). Pulls shared imports + state
//! types from the parent module via `use super::*`.

use super::*;

/// Resolve a file handle and return its source text as a String.
pub(crate) fn resolve_file_and_source(
    state: &TyProjectState,
    path: &str,
) -> Result<(File, String), AnalysisError> {
    let file = file_resolver::resolve_file(
        &state.db,
        state.root.as_std_path(),
        path,
    )
    .map_err(AnalysisError::Path)?;

    let src = source_text(&state.db, file);
    let source_str = src.as_str().to_string();

    Ok((file, source_str))
}

// ── GIL-free analysis cores ──────────────────────────────────────────────
//
// Each `compute_*` takes `&TyProjectState`, touches no Python state, and
// returns a serializable DTO (or a bare value when it cannot fail). They are
// safe to call inside `py.detach(...)`.

/// List all source files in the project.
pub(crate) fn compute_files(state: &TyProjectState) -> Vec<String> {
    let project = state.db.project();
    let indexed = project.files(&state.db);
    indexed
        .iter()
        .filter(|f: &&File| {
            f.path(&state.db)
                .extension()
                .and_then(PySourceType::try_from_extension)
                .is_some()
        })
        .map(|f: &File| f.path(&state.db).as_str().to_string())
        .collect()
}

/// Run the project type-check and build the DTO.
pub(crate) fn compute_check(state: &TyProjectState) -> dto::CheckResultDto {
    let result = state.db.check();
    let diagnostics = convert::diagnostics::convert_diagnostics(&state.db, &result);
    dto::CheckResultDto {
        diagnostics,
        files_checked: None,
        elapsed_ms: None,
    }
}

/// Run a full project check and return only the diagnostics for `path`.
pub(crate) fn compute_check_file(
    state: &TyProjectState,
    path: &str,
) -> Result<dto::CheckResultDto, AnalysisError> {
    let (file, _) = resolve_file_and_source(state, path)?;
    let target_path = file.path(&state.db).as_str().to_string();

    // Full project check (Salsa-cached if unchanged)
    let all_diagnostics = state.db.check();

    // Filter in Rust — only convert matching diagnostics to DTOs
    let matching: Vec<_> = all_diagnostics
        .iter()
        .filter(|d| {
            convert::diagnostics::diagnostic_matches_file(&state.db, d, &target_path)
        })
        .collect();

    let diagnostics =
        convert::diagnostics::convert_diagnostic_refs(&state.db, &matching);

    Ok(dto::CheckResultDto {
        diagnostics,
        files_checked: Some(1),
        elapsed_ms: None,
    })
}

/// Get document symbols for a file.
pub(crate) fn compute_document_symbols(
    state: &TyProjectState,
    path: &str,
) -> Result<Vec<dto::SymbolDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;

    let flat_symbols = ty_ide::document_symbols(&state.db, file);
    let hierarchical = flat_symbols.to_hierarchical();

    let file_path = file.path(&state.db).as_str().to_string();
    let line_index = ruff_source_file::LineIndex::from_source_text(&source_str);

    // Index the file's parsed statements once for AST-canonical entity hashing
    // (an entity's `full_range` looks up its defining `Stmt`).
    let parsed = ruff_db::parsed::parsed_module(&state.db, file).load(&state.db);
    let stmt_index = crate::hash::index_statements(&parsed.syntax().body);

    let mut symbols: Vec<dto::SymbolDto> = Vec::new();
    let policies_ref = if state.hash_policies.is_empty() {
        None
    } else {
        Some(&state.hash_policies)
    };
    let default_profile = if state.hash_policies.is_empty() {
        None
    } else {
        Some(state.default_hash_profile.as_str())
    };
    for (id, info) in hierarchical.iter() {
        convert::symbols::collect_symbols_recursive(
            &hierarchical,
            id,
            &info,
            &source_str,
            &stmt_index,
            &line_index,
            &file_path,
            None,
            None,
            state.registry.as_ref(),
            policies_ref,
            default_profile,
            &mut symbols,
        );
    }

    Ok(symbols)
}

/// Search for symbols matching a query across all workspace files.
pub(crate) fn compute_workspace_symbols(
    state: &TyProjectState,
    query: &str,
) -> Vec<dto::SymbolDto> {
    let results = ty_ide::workspace_symbols(&state.db, query);

    // Cache source text and LineIndex per file — workspace symbol results
    // from the same file reuse the precomputed LineIndex instead of rebuilding.
    let mut file_cache: HashMap<File, (String, LineIndex)> = HashMap::new();
    let mut symbols: Vec<dto::SymbolDto> = Vec::with_capacity(results.len());
    for ws_info in &results {
        let (source_str, line_index) = file_cache
            .entry(ws_info.file)
            .or_insert_with(|| {
                let src = source_text(&state.db, ws_info.file);
                let s = src.as_str().to_string();
                let idx = LineIndex::from_source_text(&s);
                (s, idx)
            });
        let file_path = ws_info.file.path(&state.db).as_str().to_string();

        let sym = convert::symbols::convert_symbol(
            source_str,
            line_index,
            &file_path,
            &ws_info.symbol.name,
            &ws_info.symbol.kind,
            ws_info.symbol.deprecated,
            ws_info.symbol.name_range,
            ws_info.symbol.full_range,
            ws_info
                .symbol
                .imported_from
                .as_ref()
                .map(|i| i.module_name().as_str()),
            None,
            None,
            None,
            std::collections::HashMap::new(),
        );
        symbols.push(sym);
    }

    symbols
}

/// Shared core for `goto_definition` / `goto_declaration` /
/// `goto_type_definition`.  Resolves the path, computes the source offset,
/// calls the provided navigation function, and converts the results.
///
/// `navigate_fn` is a plain function pointer — function pointers are
/// `Send`/`Ungil`, so they cross the `detach` boundary fine.
pub(crate) fn compute_navigate(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
    navigate_fn: fn(
        &dyn Db,
        File,
        ruff_text_size::TextSize,
    ) -> Option<ty_ide::RangedValue<ty_ide::NavigationTargets>>,
) -> Result<Vec<dto::DefinitionTargetDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;

    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    let targets = match navigate_fn(&state.db, file, offset) {
        Some(targets) => {
            convert::navigation::convert_navigation_targets(&state.db, &targets)
        }
        None => Vec::new(),
    };

    Ok(targets)
}

/// Find all references to the symbol at the given position.
pub(crate) fn compute_find_references(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
    include_declaration: bool,
) -> Result<Vec<dto::ReferenceDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;

    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    let references = match ty_ide::find_references(
        &state.db, file, offset, include_declaration,
    ) {
        Some(refs) => convert::navigation::convert_references(&state.db, &refs),
        None => Vec::new(),
    };

    Ok(references)
}

/// Return semantic tokens for a file, optionally scoped to a range.
pub(crate) fn compute_semantic_tokens(
    state: &TyProjectState,
    path: &str,
    start_line: Option<u32>,
    start_col: Option<u32>,
    end_line: Option<u32>,
    end_col: Option<u32>,
) -> Result<Vec<dto::SemanticTokenDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);

    let range = match (start_line, start_col, end_line, end_col) {
        (Some(sl), Some(sc), Some(el), Some(ec)) => {
            let start = coordinates::position_to_offset_with_index(
                &source_str, &line_index, sl, sc,
            )
            .map_err(AnalysisError::Position)?;
            let end = coordinates::position_to_offset_clamped_line_end_with_index(
                &source_str, &line_index, el, ec,
            )
            .map_err(AnalysisError::Position)?;
            Some(ruff_text_size::TextRange::new(start, end))
        }
        _ => None,
    };

    let tokens = ty_ide::semantic_tokens(&state.db, file, range);

    let result = convert::tokens::convert_semantic_tokens(
        &source_str,
        &line_index,
        &tokens,
    );

    Ok(result)
}

/// Batch-resolve all name occurrences in a file.
pub(crate) fn compute_file_occurrences(
    state: &TyProjectState,
    path: &str,
) -> Result<Vec<dto::NameOccurrenceDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);

    let result = convert::occurrences::convert_file_occurrences(
        &state.db,
        file,
        &source_str,
        &line_index,
    );

    Ok(result)
}

/// Query type hierarchy at a position: the item plus its supertypes and subtypes.
pub(crate) fn compute_type_hierarchy(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Option<dto::TypeHierarchyDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    // Step 1: Prepare the hierarchy item at this position
    let item = match ty_ide::prepare_type_hierarchy(&state.db, file, offset) {
        Some(item) => item,
        None => return Ok(None),
    };

    // Step 2: Resolve supertypes and subtypes using the prepared item
    let supertypes = ty_ide::type_hierarchy_supertypes(
        &state.db, item.file, item.selection_range.start(),
    );
    let subtypes = ty_ide::type_hierarchy_subtypes(
        &state.db, item.file, item.selection_range.start(),
    );

    let item_dto = convert::hierarchy::convert_hierarchy_item(&state.db, &item);
    let supertypes_dto = convert::hierarchy::convert_hierarchy_items(&state.db, &supertypes);
    let subtypes_dto = convert::hierarchy::convert_hierarchy_items(&state.db, &subtypes);

    Ok(Some(dto::TypeHierarchyDto {
        item: item_dto,
        supertypes: supertypes_dto,
        subtypes: subtypes_dto,
    }))
}

/// Resolve only the direct supertypes (base classes) of the class at a position.
///
/// This is the lean half of `compute_type_hierarchy`: it runs
/// `prepare_type_hierarchy` + `type_hierarchy_supertypes` and deliberately
/// SKIPS `type_hierarchy_subtypes`. Subtype resolution scans every module in
/// the workspace (including typeshed/stdlib) to find inheritors and is, per
/// ty's own docs, "quite expensive in large projects" — yet CodeGraph build
/// only ever reads supertypes (for INHERITS edges). Skipping it removes that
/// global scan from the hot build path.
///
/// Returns an empty vec when the position is not on a class.
pub(crate) fn compute_supertypes(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Vec<dto::TypeHierarchyItemDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    // Confirm the cursor is on a class and normalize to its name position.
    let item = match ty_ide::prepare_type_hierarchy(&state.db, file, offset) {
        Some(item) => item,
        None => return Ok(Vec::new()),
    };

    let supertypes = ty_ide::type_hierarchy_supertypes(
        &state.db, item.file, item.selection_range.start(),
    );
    Ok(convert::hierarchy::convert_hierarchy_items(&state.db, &supertypes))
}

/// Get inlay hints for a file (whole-file).
///
/// NOTE: ty_ide does not publicly re-export InlayHint, so the conversion
/// is inlined here to work with the inferred type.
pub(crate) fn compute_inlay_hints(
    state: &TyProjectState,
    path: &str,
) -> Result<Vec<dto::InlayHintDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);

    let text_len = ruff_text_size::TextSize::from(source_str.len() as u32);
    let full_range = ruff_text_size::TextRange::up_to(text_len);
    let settings = ty_ide::InlayHintSettings::default();

    let hints = ty_ide::inlay_hints(&state.db, file, full_range, &settings);
    Ok(hints
        .iter()
        .map(|hint| {
            // Flatten structured label parts into a single display string
            let label: String = hint.label.parts()
                .iter()
                .map(|p| p.text().to_string())
                .collect::<Vec<_>>()
                .join("");

            let loc = line_index.source_location(
                hint.position,
                &source_str,
                ruff_source_file::PositionEncoding::Utf32,
            );

            let kind = match hint.kind {
                ty_ide::InlayHintKind::Type => dto::InlayHintKindDto::Type,
                ty_ide::InlayHintKind::CallArgumentName => dto::InlayHintKindDto::CallArgumentName,
            };

            dto::InlayHintDto {
                position: dto::PositionDto {
                    line: loc.line.get() as u32,
                    column: (loc.character_offset.to_zero_indexed() + 1) as u32,
                },
                label,
                kind,
            }
        })
        .collect())
}

/// Get code actions (quick fixes) for a diagnostic at a range.
pub(crate) fn compute_code_actions(
    state: &TyProjectState,
    path: &str,
    start_line: u32,
    start_col: u32,
    end_line: u32,
    end_col: u32,
    diagnostic_id: &str,
) -> Result<Vec<dto::QuickFixDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let start_offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, start_line, start_col,
    )
    .map_err(AnalysisError::Position)?;
    let end_offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, end_line, end_col,
    )
    .map_err(AnalysisError::Position)?;

    let diagnostic_range = ruff_text_size::TextRange::new(start_offset, end_offset);
    let file_path = file.path(&state.db).as_str().to_string();

    let fixes = ty_ide::code_actions(&state.db, file, diagnostic_range, diagnostic_id);
    Ok(fixes
        .iter()
        .map(|fix| {
            convert::code_action::convert_quick_fix(&source_str, &line_index, &file_path, fix)
        })
        .collect())
}

/// Get hints (unused bindings, unreachable code) for a file.
pub(crate) fn compute_hints(
    state: &TyProjectState,
    path: &str,
) -> Result<Vec<dto::HintDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);

    let hints = ty_ide::hints(&state.db, file);
    Ok(hints
        .iter()
        .map(|h| convert::hints::convert_hint(&source_str, &line_index, h))
        .collect())
}

/// Get signature help at a position.
pub(crate) fn compute_signature_help(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Option<dto::SignatureHelpDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    match ty_ide::signature_help(&state.db, file, offset) {
        Some(info) => {
            Ok(Some(convert::signature::convert_signature_help(&info)))
        }
        None => Ok(None),
    }
}

/// Get completion suggestions at a position.
pub(crate) fn compute_completions(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
    auto_import: bool,
) -> Result<Vec<dto::CompletionDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    let settings = ty_ide::CompletionSettings { auto_import };
    let completions = ty_ide::completion(&state.db, &settings, file, offset);
    Ok(convert::completion::convert_completions(&state.db, &completions))
}

/// Compute selection ranges at a position.
pub(crate) fn compute_selection_ranges(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Vec<dto::RangeDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    let ranges = ty_ide::selection_range(&state.db, file, offset);
    Ok(ranges
        .iter()
        .map(|r| coordinates::range_to_dto_with_index(&source_str, &line_index, *r))
        .collect())
}

/// Compute folding ranges for a file (whole-file by default).
pub(crate) fn compute_folding_ranges(
    state: &TyProjectState,
    path: &str,
) -> Result<Vec<dto::FoldingRangeDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);

    let ranges = ty_ide::folding_ranges(&state.db, file, None);
    Ok(ranges
        .iter()
        .map(|r| convert::folding::convert_folding_range(&source_str, &line_index, r))
        .collect())
}

/// Return the editable range of the symbol at the position, or None.
pub(crate) fn compute_can_rename(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Option<dto::RangeDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    let range = ty_ide::can_rename(&state.db, file, offset);
    Ok(range.map(|r| {
        coordinates::range_to_dto_with_index(&source_str, &line_index, r)
    }))
}

/// Perform a rename operation, returning all edit locations.
pub(crate) fn compute_rename(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
    new_name: &str,
) -> Result<Option<dto::WorkspaceEditDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    match ty_ide::rename(&state.db, file, offset, new_name) {
        Some(targets) => {
            Ok(Some(convert::rename::convert_rename_edits(
                &state.db, &targets, new_name,
            )))
        }
        None => Ok(None),
    }
}

/// Find document highlights for the symbol at the given position.
///
/// Highlights are identical to references but scoped to the current file.
pub(crate) fn compute_document_highlights(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Vec<dto::ReferenceDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    let refs = match ty_ide::document_highlights(&state.db, file, offset) {
        Some(targets) => convert::navigation::convert_references(&state.db, &targets),
        None => Vec::new(),
    };
    Ok(refs)
}

/// Get hover information for the symbol at the given position.
///
/// NOTE: ty_ide does not publicly re-export Hover/HoverContent, so the entire
/// hover is rendered as Markdown and returned as a single content item.
pub(crate) fn compute_hover(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Option<dto::HoverDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;

    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    match ty_ide::hover(&state.db, file, offset) {
        None => Ok(None),
        Some(hover_value) => {
            let file_range = hover_value.file_range();
            let file_path = file.path(&state.db).as_str().to_string();

            // Render the entire hover as Markdown
            let rendered = hover_value
                .display(&state.db, ty_ide::MarkupKind::Markdown)
                .to_string();

            let hover_dto = convert::hover::convert_hover_markdown_with_index(
                &source_str,
                &line_index,
                file_path,
                file_range,
                rendered,
            );

            Ok(Some(hover_dto))
        }
    }
}

