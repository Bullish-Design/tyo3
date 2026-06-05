use ruff_db::files::File;
use ruff_db::parsed::parsed_module;
use ruff_python_ast as ast;
use ruff_source_file::LineIndex;

use ty_ide::{SemanticTokenType, SemanticTokenModifier, goto_definition};
use ty_project::Db;

use crate::coordinates;
use crate::dto::{NameOccurrenceDto, ReferenceRoleDto};

/// Name-like semantic token types.
const NAME_TOKEN_TYPES: &[SemanticTokenType] = &[
    SemanticTokenType::Namespace,
    SemanticTokenType::Class,
    SemanticTokenType::Parameter,
    SemanticTokenType::SelfParameter,
    SemanticTokenType::ClsParameter,
    SemanticTokenType::Variable,
    SemanticTokenType::Property,
    SemanticTokenType::Function,
    SemanticTokenType::Method,
    SemanticTokenType::Decorator,
    SemanticTokenType::BuiltinConstant,
    SemanticTokenType::TypeParameter,
];

/// Batch-resolve all name occurrences in a file.
///
/// For every name-like semantic token in the file, emits an occurrence with:
/// - `role` — definition, import, read, write
/// - `name` — the source token text
/// - `range` — the source range
/// - `target_file`, `target_name`, `target_qualified_name` — when the token
///   resolves to a definition (may be None for genuinely external/unknown
///   symbols; `name` is always set for real tokens).
pub fn convert_file_occurrences(
    db: &dyn Db,
    file: File,
    source: &str,
    line_index: &LineIndex,
) -> Vec<NameOccurrenceDto> {
    let tokens = ty_ide::semantic_tokens(db, file, None);

    let mut occurrences = Vec::with_capacity(tokens.len());

    for token in tokens.iter() {
        if !is_name_token(&token.token_type) {
            continue;
        }

        // Extract the name text directly from source using the token range.
        let range = token.range;
        let start: usize = range.start().into();
        let end: usize = range.end().into();
        let name_text = &source[start..end];

        // Classify the role from token type and modifiers.
        let role = classify_role(&token.token_type, token.modifiers);

        // Resolve the definition target.
        let (target_file, target_name, _target_qualified_name) =
            resolve_occurrence_target(db, file, &token, range, source);

        occurrences.push(NameOccurrenceDto {
            range: coordinates::range_to_dto_with_index(source, line_index, range),
            name: Some(name_text.to_string()),
            target_file,
            target_name,
            target_qualified_name: None,
            role,
        });
    }

    // Also walk the AST to find additional name references that semantic
    // tokens may skip: attribute access (`obj.method`), constructor calls
    // (`ClassName()`), and import aliases (`from x import Y`).
    let parsed = parsed_module(db, file).load(db);
    let module_body = &parsed.syntax().body;
    // Pre-populate seen ranges from semantic token occurrences
    let mut seen_ranges: std::collections::HashSet<(u32, u32)> = std::collections::HashSet::new();
    for occ in &occurrences {
        seen_ranges.insert((
            occ.range.start.line,
            occ.range.start.column,
        ));
    }
    let mut ast_walker = AstOccurrenceWalker {
        db,
        file,
        source,
        line_index,
        occurrences: &mut occurrences,
        seen_ranges,
    };
    ast_walker.walk_body(module_body);

    occurrences
}

// ── AST Walker for additional occurrences ────────────────────────────

struct AstOccurrenceWalker<'a, 'b> {
    db: &'a dyn Db,
    file: File,
    source: &'a str,
    line_index: &'a LineIndex,
    occurrences: &'b mut Vec<NameOccurrenceDto>,
    seen_ranges: std::collections::HashSet<(u32, u32)>, // (start_offset, end_offset)
}

impl<'a, 'b> AstOccurrenceWalker<'a, 'b> {
    fn walk_body(&mut self, body: &'a [ast::Stmt]) {
        for stmt in body {
            self.walk_stmt(stmt);
        }
    }

    fn walk_stmt(&mut self, stmt: &'a ast::Stmt) {
        match stmt {
            ast::Stmt::FunctionDef(func) => {
                // Walk return type annotation
                if let Some(returns) = &func.returns {
                    self.walk_expr(returns);
                }
                // Walk decorators
                for decorator in &func.decorator_list {
                    self.walk_expr(&decorator.expression);
                }
                // Walk the body
                for s in &func.body {
                    self.walk_stmt(s);
                }
            }
            ast::Stmt::ClassDef(class) => {
                // Walk bases and arguments (for `class Foo(Bar):`)
                if let Some(args) = &class.arguments {
                    for base in &args.args {
                        self.walk_expr(base);
                    }
                    for keyword in &args.keywords {
                        self.walk_expr(&keyword.value);
                    }
                }
                // Walk decorators
                for decorator in &class.decorator_list {
                    self.walk_expr(&decorator.expression);
                }
                // Walk the body
                for s in &class.body {
                    self.walk_stmt(s);
                }
            }
            ast::Stmt::Import(import) => {
                for alias in &import.names {
                    // Record the imported name
                    self.try_add_occurrence(
                        alias.name.range,
                        alias.name.as_str(),
                        ReferenceRoleDto::Import,
                    );
                    if let Some(as_name) = &alias.asname {
                        self.try_add_occurrence(
                            as_name.range,
                            as_name.as_str(),
                            ReferenceRoleDto::Definition,
                        );
                    }
                }
            }
            ast::Stmt::ImportFrom(import_from) => {
                // Record module name
                if let Some(module) = &import_from.module {
                    // Find the module name position in source
                    let stmt_range = import_from.range;
                    let stmt_start: usize = stmt_range.start().into();
                    let src_after_from = &self.source[stmt_start..];
                    if let Some(mod_pos) = find_word_in_source(src_after_from, module.as_str()) {
                        let abs_start = stmt_start + mod_pos;
                        let abs_end = abs_start + module.as_str().len();
                        if abs_end <= self.source.len() {
                            self.try_add_occurrence(
                                ruff_text_size::TextRange::new(
                                    ruff_text_size::TextSize::new(abs_start as u32),
                                    ruff_text_size::TextSize::new(abs_end as u32),
                                ),
                                module.as_str(),
                                ReferenceRoleDto::Import,
                            );
                        }
                    }
                }
                // Record each imported name as Import role
                for alias in &import_from.names {
                    self.try_add_occurrence(
                        alias.name.range,
                        alias.name.as_str(),
                        ReferenceRoleDto::Import,
                    );
                    if let Some(as_name) = &alias.asname {
                        self.try_add_occurrence(
                            as_name.range,
                            as_name.as_str(),
                            ReferenceRoleDto::Definition,
                        );
                    }
                }
            }
            ast::Stmt::Assign(assign) => {
                for target in &assign.targets {
                    if let ast::Expr::Name(name) = target {
                        self.try_add_occurrence(
                            name.range,
                            name.id.as_str(),
                            ReferenceRoleDto::Definition,
                        );
                    }
                }
                self.walk_expr(&assign.value);
            }
            ast::Stmt::AnnAssign(ann_assign) => {
                self.walk_expr(&ann_assign.annotation);
                if let Some(value) = &ann_assign.value {
                    self.walk_expr(value);
                }
            }
            ast::Stmt::For(for_stmt) => {
                self.walk_expr(&for_stmt.iter);
                for s in &for_stmt.body {
                    self.walk_stmt(s);
                }
                for s in &for_stmt.orelse {
                    self.walk_stmt(s);
                }
            }
            ast::Stmt::While(while_stmt) => {
                self.walk_expr(&while_stmt.test);
                for s in &while_stmt.body {
                    self.walk_stmt(s);
                }
                for s in &while_stmt.orelse {
                    self.walk_stmt(s);
                }
            }
            ast::Stmt::If(if_stmt) => {
                self.walk_expr(&if_stmt.test);
                for s in &if_stmt.body {
                    self.walk_stmt(s);
                }
                for clause in &if_stmt.elif_else_clauses {
                    for s in &clause.body {
                        self.walk_stmt(s);
                    }
                }
            }
            ast::Stmt::Try(try_stmt) => {
                for s in &try_stmt.body {
                    self.walk_stmt(s);
                }
                for handler in &try_stmt.handlers {
                    if let ast::ExceptHandler::ExceptHandler(exc) = handler {
                        for s in &exc.body {
                            self.walk_stmt(s);
                        }
                    }
                }
                for s in &try_stmt.orelse {
                    self.walk_stmt(s);
                }
                for s in &try_stmt.finalbody {
                    self.walk_stmt(s);
                }
            }
            ast::Stmt::With(with_stmt) => {
                for s in &with_stmt.body {
                    self.walk_stmt(s);
                }
            }
            ast::Stmt::Raise(raise) => {
                if let Some(exc) = &raise.exc {
                    self.walk_expr(exc);
                }
            }
            ast::Stmt::Return(ret) => {
                if let Some(value) = &ret.value {
                    self.walk_expr(value);
                }
            }
            ast::Stmt::Expr(expr_stmt) => {
                self.walk_expr(&expr_stmt.value);
            }
            _ => {}
        }
    }

    fn walk_expr(&mut self, expr: &'a ast::Expr) {
        match expr {
            ast::Expr::Call(call) => {
                // Record the call target (could be a Name, Attribute, etc.)
                self.walk_expr(&call.func);
                for arg in &call.arguments.args {
                    self.walk_expr(arg);
                }
                for keyword in &call.arguments.keywords {
                    self.walk_expr(&keyword.value);
                }
            }
            ast::Expr::Attribute(attr) => {
                // Record `obj.method` as a Read reference to `method`
                self.try_add_occurrence(
                    attr.range,
                    attr.attr.as_str(),
                    ReferenceRoleDto::Read,
                );
                // Walk the value (e.g., `user` in `user.save`)
                self.walk_expr(&attr.value);
            }
            ast::Expr::Name(name) => {
                // Record name references not already captured by semantic tokens
                let name_str = name.id.as_str();
                if matches!(name_str, "True" | "False" | "None" | "_") {
                    return;
                }
                self.try_add_occurrence(name.range, name_str, ReferenceRoleDto::Read);
            }
            ast::Expr::BinOp(bin_op) => {
                self.walk_expr(&bin_op.left);
                self.walk_expr(&bin_op.right);
            }
            ast::Expr::Compare(compare) => {
                self.walk_expr(&compare.left);
                for comp in &compare.comparators {
                    self.walk_expr(comp);
                }
            }
            ast::Expr::BoolOp(bool_op) => {
                for value in &bool_op.values {
                    self.walk_expr(value);
                }
            }
            ast::Expr::UnaryOp(unary) => {
                self.walk_expr(&unary.operand);
            }
            ast::Expr::Subscript(subscript) => {
                self.walk_expr(&subscript.value);
                self.walk_expr(&subscript.slice);
            }
            _ => {}
        }
    }

    /// Add an occurrence only if there isn't already one at the same range.
    fn try_add_occurrence(
        &mut self,
        range: ruff_text_size::TextRange,
        name: &str,
        role: ReferenceRoleDto,
    ) {
        // Compute the (line, column) of the range start for dedup
        let one_indexed_line = self.line_index.line_index(range.start());
        let line = (one_indexed_line.get() - 1) as u32; // zero-indexed
        let line_start: usize = self.line_index.line_start(one_indexed_line, self.source).into();
        let start_offset: usize = range.start().into();
        let col = (start_offset - line_start) as u32;

        if self.seen_ranges.contains(&(line, col)) {
            return;
        }
        self.seen_ranges.insert((line, col));

        let name_str = name.to_string();

        // For definitions, target is self; for imports/references, use goto_definition
        let (target_file, target_name, _) = match role {
            ReferenceRoleDto::Definition => {
                let file_path = self.file.path(self.db).as_str().to_string();
                (Some(file_path), Some(name_str.clone()), None)
            }
            _ => self.resolve_via_goto_definition(range),
        };

        self.occurrences.push(NameOccurrenceDto {
            range: coordinates::range_to_dto_with_index(self.source, self.line_index, range),
            name: Some(name_str),
            target_file,
            target_name,
            target_qualified_name: None,
            role,
        });
    }

    fn resolve_via_goto_definition(
        &self,
        range: ruff_text_size::TextRange,
    ) -> (Option<String>, Option<String>, Option<String>) {
        let offset = range.start();
        if let Some(goto_result) = goto_definition(self.db, self.file, offset) {
            let targets: Vec<_> = (&goto_result.value).into_iter().collect();
            if let Some(target) = targets.first() {
                let tgt_file = target.file();
                let file_path = tgt_file.path(self.db).as_str().to_string();
                let tgt_source = ruff_db::source::source_text(self.db, tgt_file);
                let tgt_source_str = tgt_source.as_str();
                let focus_range = target.focus_range();
                let tgt_start: usize = focus_range.start().into();
                let tgt_end: usize = focus_range.end().into();
                let target_name = if tgt_end <= tgt_source_str.len() {
                    Some(tgt_source_str[tgt_start..tgt_end].to_string())
                } else {
                    None
                };
                return (Some(file_path), target_name, None);
            }
        }
        (None, None, None)
    }
}

/// Find the byte offset of `word` within `source`, using simple search.
fn find_word_in_source(source: &str, word: &str) -> Option<usize> {
    source.find(word)
}

// ── Helpers ───────────────────────────────────────────────────────────

fn is_name_token(tt: &SemanticTokenType) -> bool {
    NAME_TOKEN_TYPES.contains(tt)
}

fn classify_role(
    token_type: &SemanticTokenType,
    modifiers: SemanticTokenModifier,
) -> ReferenceRoleDto {
    if modifiers.contains(SemanticTokenModifier::DEFINITION) {
        ReferenceRoleDto::Definition
    } else if matches!(token_type, SemanticTokenType::Namespace) {
        ReferenceRoleDto::Import
    } else {
        ReferenceRoleDto::Read
    }
}

/// Resolve a token occurrence to its definition target.
///
/// For DEFINITION tokens, the target is the current file + name.
/// For reference/import tokens, uses `goto_definition` to resolve.
fn resolve_occurrence_target(
    db: &dyn Db,
    file: File,
    token: &ty_ide::SemanticToken,
    range: ruff_text_size::TextRange,
    source: &str,
) -> (Option<String>, Option<String>, Option<String>) {
    // For DEFINITION tokens, the target is the definition itself.
    if token.modifiers.contains(SemanticTokenModifier::DEFINITION) {
        let file_path = file.path(db).as_str().to_string();
        let start: usize = range.start().into();
        let end: usize = range.end().into();
        let name_text = if end <= source.len() {
            Some(source[start..end].to_string())
        } else {
            None
        };
        return (Some(file_path), name_text, None);
    }

    // For reference/import tokens, use goto_definition.
    let offset = range.start();
    if let Some(goto_result) = goto_definition(db, file, offset) {
        let targets: Vec<_> = (&goto_result.value).into_iter().collect();
        if let Some(target) = targets.first() {
            let tgt_file = target.file();
            let file_path = tgt_file.path(db).as_str().to_string();
            let tgt_source = ruff_db::source::source_text(db, tgt_file);
            let tgt_source_str = tgt_source.as_str();
            let focus_range = target.focus_range();
            let tgt_start: usize = focus_range.start().into();
            let tgt_end: usize = focus_range.end().into();
            let target_name = if tgt_end <= tgt_source_str.len() {
                Some(tgt_source_str[tgt_start..tgt_end].to_string())
            } else {
                None
            };
            return (Some(file_path), target_name, None);
        }
    }

    (None, None, None)
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use ruff_python_ast::name::Name;
    use ruff_source_file::LineIndex;

    use ruff_db::system::SystemPathBuf;
    use ty_project::{ProjectDatabase, ProjectMetadata};

    use crate::content::ContentStore;
    use crate::convert::occurrences::convert_file_occurrences;
    use crate::dto::ReferenceRoleDto;
    use crate::overlay::OverlaySystem;

    fn state_with_files(
        files: Vec<(&str, &str)>,
    ) -> (tempfile::TempDir, crate::project::TyProjectState) {
        let dir = tempfile::tempdir().unwrap();

        let mut toml = std::fs::File::create(dir.path().join("pyproject.toml")).unwrap();
        toml.write_all(b"[project]\nname = \"test\"\nversion = \"0.1.0\"\n")
            .unwrap();

        for (path_str, content) in &files {
            let path = dir.path().join(path_str.trim_start_matches("/project/"));
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).unwrap();
            }
            let mut f = std::fs::File::create(&path).unwrap();
            f.write_all(content.as_bytes()).unwrap();
        }

        let root = SystemPathBuf::from_path_buf(
            dir.path().canonicalize().unwrap().to_path_buf(),
        ).unwrap();

        let system = OverlaySystem::live(root.clone(), ContentStore::new().capture());
        let metadata = ProjectMetadata::new(Name::new("test"), root.clone());
        let db = ProjectDatabase::use_defaults(metadata, system);

        db.check();

        (
            dir,
            crate::project::TyProjectState { db, root },
        )
    }

    #[test]
    fn test_occurrences_resolve_project_local_targets() {
        // Two-file fixture simulating the cross-file reference case
        // from the refactoring guide.
        // Note: goto_definition resolves imported names to their import
        // alias in the local file, NOT to the original definition file.
        // Cross-file resolution (alias → original def) happens on the
        // Python side.  The Rust layer's job is to produce a complete
        // set of occurrences with correct name, role, and target_name.
        let files = vec![
            (
                "/project/models.py",
                "class User:\n    def save(self): ...\n",
            ),
            (
                "/project/app.py",
                "from models import User\ndef run():\n    return User().save()\n",
            ),
        ];

        let (_dir, state) = state_with_files(files);

        let app_path = state.root.join("app.py");
        let app_file = ruff_db::files::system_path_to_file(&state.db, &app_path)
            .expect("app.py should be known to the DB");

        let source = ruff_db::source::source_text(&state.db, app_file);
        let source_str = source.as_str();
        let line_index = LineIndex::from_source_text(source_str);

        let occurrences = convert_file_occurrences(&state.db, app_file, source_str, &line_index);

        let occ_names: Vec<String> = occurrences
            .iter()
            .map(|o| format!("{:?}({:?})->{:?}", o.name, o.role, o.target_name))
            .collect();

        assert!(
            occurrences.len() >= 3,
            "Expected at least 3 occurrences, got {}\n  {}",
            occurrences.len(),
            occ_names.join("\n  ")
        );

        // Check that `User` appears as an Import role (from 'from models import User')
        let user_imports: Vec<_> = occurrences
            .iter()
            .filter(|o| {
                o.name.as_deref() == Some("User")
                    && o.role == ReferenceRoleDto::Import
            })
            .collect();
        assert!(
            !user_imports.is_empty(),
            "Expected a 'User' Import occurrence (from 'from models import User'), got:\n  {}",
            occ_names.join("\n  ")
        );

        // Check for the `run` definition occurrence.
        let run_defs: Vec<_> = occurrences
            .iter()
            .filter(|o| {
                o.name.as_deref() == Some("run")
                    && o.role == ReferenceRoleDto::Definition
            })
            .collect();
        assert!(
            !run_defs.is_empty(),
            "Expected a 'run' occurrence with Definition role, got:\n  {}",
            occ_names.join("\n  ")
        );

        // Check that `User` reference (in `User()`) exists with Read role
        // and has a resolved target_name (even if target_file is the local
        // import-binding file, not models.py).
        let user_refs: Vec<_> = occurrences
            .iter()
            .filter(|o| o.name.as_deref() == Some("User") && o.role == ReferenceRoleDto::Read)
            .collect();
        assert!(
            !user_refs.is_empty(),
            "Expected a 'User' reference (Read role) occurrence, got:\n  {}",
            occ_names.join("\n  ")
        );

        let user_ref = &user_refs[0];
        assert!(
            user_ref.target_name.as_deref() == Some("User"),
            "User reference should target name 'User', got target_name={:?}\n  {}",
            user_ref.target_name,
            occ_names.join("\n  ")
        );
        assert!(
            user_ref.target_file.is_some(),
            "User reference should have a resolved target_file\n  {}",
            occ_names.join("\n  ")
        );

        // Check that `save` reference (in `.save()`) exists with Read role
        // and has a resolved target_name.
        let save_refs: Vec<_> = occurrences
            .iter()
            .filter(|o| o.name.as_deref() == Some("save") && o.role == ReferenceRoleDto::Read)
            .collect();
        assert!(
            !save_refs.is_empty(),
            "Expected a 'save' reference occurrence (from user.save()), got:\n  {}",
            occ_names.join("\n  ")
        );

        let save_ref = &save_refs[0];
        assert!(
            save_ref.target_name.is_some(),
            "save reference should have a resolved target_name\n  {}",
            occ_names.join("\n  ")
        );
        assert!(
            save_ref.target_file.is_some(),
            "save reference should have a resolved target_file\n  {}",
            occ_names.join("\n  ")
        );
    }

    #[test]
    fn test_occurrences_have_name_for_every_token() {
        let files = vec![
            ("/project/test.py", "x = 1\nprint(x)\n"),
        ];

        let (_dir, state) = state_with_files(files);

        let test_path = state.root.join("test.py");
        let test_file = ruff_db::files::system_path_to_file(&state.db, &test_path)
            .expect("test.py should be known to the DB");

        let source = ruff_db::source::source_text(&state.db, test_file);
        let source_str = source.as_str();
        let line_index = LineIndex::from_source_text(source_str);

        let occurrences = convert_file_occurrences(&state.db, test_file, source_str, &line_index);

        assert!(!occurrences.is_empty(), "Expected at least some occurrences");

        for occ in &occurrences {
            assert!(
                occ.name.is_some(),
                "Every occurrence should have a name, got role={:?} range={:?}",
                occ.role,
                occ.range
            );
        }
    }
}
