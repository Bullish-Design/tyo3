//! Batch name-occurrence resolution for a file.
//!
//! For every name reference in a file we emit a [`NameOccurrenceDto`] carrying
//! the source token text, its role, its source range, and — crucially — the
//! *resolved definition target* (`target_file` + `target_name`).
//!
//! The target resolution follows import aliases through to the original
//! definition, **across files**, using the semantic engine's
//! `definitions_for_name` / `definitions_for_attribute` (with
//! `ImportAliasResolution::ResolveAliases`).  This is what lets the Python code
//! layer build cross-file reference and import edges: a use of `User` imported
//! `from models import User` resolves to `models.py::User`, not to the local
//! import binding.
//!
//! Traversal uses ruff's `SourceOrderVisitor` so coverage is complete (calls,
//! attributes, comprehensions, f-strings, decorators, annotations, …) rather
//! than a hand-maintained list of expression kinds.

use ruff_db::files::File;
use ruff_db::parsed::parsed_module;
use ruff_db::source::source_text;
use ruff_python_ast as ast;
use ruff_python_ast::visitor::source_order::{self, SourceOrderVisitor};
use ruff_python_ast::AnyNodeRef;
use ruff_source_file::LineIndex;
use ruff_text_size::{Ranged, TextRange};

use ty_project::Db;
use ty_python_semantic::{
    definitions_for_attribute, definitions_for_imported_symbol, definitions_for_name,
    ImportAliasResolution, ResolvedDefinition, SemanticModel,
};

use crate::coordinates;
use crate::dto::{NameOccurrenceDto, ReferenceRoleDto};

/// Batch-resolve all name occurrences in a file.
///
/// Emits one occurrence per name reference / binding / import, each with:
/// - `name` — the source token text (never `None` for a real token);
/// - `role` — read / write / import / definition;
/// - `range` — the source range of the token;
/// - `target_file` / `target_name` — the resolved definition site, following
///   import aliases across files (`None` only when the symbol genuinely does
///   not resolve, e.g. an unresolved external).
pub fn convert_file_occurrences(
    db: &dyn Db,
    file: File,
    source: &str,
    line_index: &LineIndex,
) -> Vec<NameOccurrenceDto> {
    let parsed = parsed_module(db, file).load(db);
    let model = SemanticModel::new(db, file);

    let mut visitor = OccurrenceVisitor {
        db,
        file,
        model,
        source,
        line_index,
        occurrences: Vec::new(),
    };
    visitor.visit_body(&parsed.syntax().body);
    visitor.occurrences
}

// ── Visitor ──────────────────────────────────────────────────────────────

struct OccurrenceVisitor<'a> {
    db: &'a dyn Db,
    file: File,
    model: SemanticModel<'a>,
    source: &'a str,
    line_index: &'a LineIndex,
    occurrences: Vec<NameOccurrenceDto>,
}

impl<'a> OccurrenceVisitor<'a> {
    /// Push a self-targeting definition occurrence (the binding site of a
    /// name defined in this file). No FFI resolution — the target is itself.
    fn add_definition(&mut self, range: TextRange, name: &str) {
        let path = self.file.path(self.db).as_str().to_string();
        self.push(
            range,
            name,
            ReferenceRoleDto::Definition,
            Some(path),
            Some(name.to_string()),
        );
    }

    fn push(
        &mut self,
        range: TextRange,
        name: &str,
        role: ReferenceRoleDto,
        target_file: Option<String>,
        target_name: Option<String>,
    ) {
        self.occurrences.push(NameOccurrenceDto {
            range: coordinates::range_to_dto_with_index(self.source, self.line_index, range),
            name: Some(name.to_string()),
            target_file,
            target_name,
            target_qualified_name: None,
            role,
        });
    }
}

impl<'a> SourceOrderVisitor<'a> for OccurrenceVisitor<'a> {
    fn visit_stmt(&mut self, stmt: &'a ast::Stmt) {
        match stmt {
            // Definition sites: the defined name targets itself.
            ast::Stmt::FunctionDef(func) => {
                self.add_definition(func.name.range(), func.name.as_str());
            }
            ast::Stmt::ClassDef(class) => {
                self.add_definition(class.name.range(), class.name.as_str());
            }
            // `from module import name [as alias]` — resolve each imported
            // symbol to its original definition (cross-file, alias-following).
            ast::Stmt::ImportFrom(import) => {
                for alias in &import.names {
                    let imported = alias.name.as_str();
                    let defs = definitions_for_imported_symbol(
                        &self.model,
                        import,
                        imported,
                        ImportAliasResolution::ResolveAliases,
                    );
                    let (target_file, target_name) = target_from_defs(self.db, &defs);
                    // The binding's source location is the `as` name if present,
                    // else the imported name itself.
                    let range = alias
                        .asname
                        .as_ref()
                        .map(|a| a.range())
                        .unwrap_or_else(|| alias.name.range());
                    self.push(
                        range,
                        imported,
                        ReferenceRoleDto::Import,
                        target_file,
                        target_name,
                    );
                }
            }
            // `import module [as alias]` — a module dependency. We record the
            // import without a per-symbol target; module-level import edges are
            // driven by resolved references in the Python layer.
            ast::Stmt::Import(import) => {
                for alias in &import.names {
                    let range = alias
                        .asname
                        .as_ref()
                        .map(|a| a.range())
                        .unwrap_or_else(|| alias.name.range());
                    self.push(range, alias.name.as_str(), ReferenceRoleDto::Import, None, None);
                }
            }
            _ => {}
        }
        source_order::walk_stmt(self, stmt);
    }

    fn visit_expr(&mut self, expr: &'a ast::Expr) {
        match expr {
            ast::Expr::Name(name) => match name.ctx {
                // A use of a name: resolve through aliases to its definition.
                ast::ExprContext::Load => {
                    let defs = definitions_for_name(
                        &self.model,
                        name.id.as_str(),
                        AnyNodeRef::from(name),
                        ImportAliasResolution::ResolveAliases,
                    );
                    let (target_file, target_name) = target_from_defs(self.db, &defs);
                    self.push(
                        name.range(),
                        name.id.as_str(),
                        ReferenceRoleDto::Read,
                        target_file,
                        target_name,
                    );
                }
                // A binding target (`x = …`, `del x`): defines the name here.
                ast::ExprContext::Store | ast::ExprContext::Del => {
                    self.add_definition(name.range(), name.id.as_str());
                }
                _ => {}
            },
            // Attribute access (`obj.method`): resolve the member to its
            // definition (e.g. the method on the class), following the LHS type.
            ast::Expr::Attribute(attr) => {
                let defs = definitions_for_attribute(&self.model, attr);
                let (target_file, target_name) = target_from_defs(self.db, &defs);
                let role = match attr.ctx {
                    ast::ExprContext::Store | ast::ExprContext::Del => ReferenceRoleDto::Write,
                    _ => ReferenceRoleDto::Read,
                };
                self.push(attr.attr.range(), attr.attr.as_str(), role, target_file, target_name);
            }
            _ => {}
        }
        source_order::walk_expr(self, expr);
    }
}

// ── Resolution helper ─────────────────────────────────────────────────────

/// Extract `(target_file, target_name)` from the first resolved definition.
///
/// `focus_range` gives the definition's name range in its (possibly different)
/// file; the name text is read from that file's source. Returns `(None, None)`
/// when nothing resolves.
fn target_from_defs(db: &dyn Db, defs: &[ResolvedDefinition]) -> (Option<String>, Option<String>) {
    let Some(def) = defs.first() else {
        return (None, None);
    };
    let focus = def.focus_range(db);
    let target_file = focus.file();
    let path = target_file.path(db).as_str().to_string();

    let src = source_text(db, target_file);
    let range = focus.range();
    let start: usize = range.start().into();
    let end: usize = range.end().into();
    let name = src.as_str().get(start..end).map(|s| s.to_string());

    (Some(path), name)
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

        let root =
            SystemPathBuf::from_path_buf(dir.path().canonicalize().unwrap().to_path_buf()).unwrap();

        let system = OverlaySystem::live(root.clone(), ContentStore::new().capture());
        let metadata = ProjectMetadata::new(Name::new("test"), root.clone());
        let db = ProjectDatabase::use_defaults(metadata, system);

        db.check();

        (dir, crate::project::TyProjectState { db, root })
    }

    /// The core cross-file requirement: a use of an imported symbol resolves to
    /// the symbol's *original definition file*, not the local import binding.
    #[test]
    fn test_occurrences_resolve_cross_file_targets() {
        let files = vec![
            ("/project/models.py", "class User:\n    def save(self): ...\n"),
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

        let dump: Vec<String> = occurrences
            .iter()
            .map(|o| {
                format!(
                    "{:?}({:?}) -> {:?}::{:?}",
                    o.name, o.role, o.target_file, o.target_name
                )
            })
            .collect();
        let dump = dump.join("\n  ");

        // `User` used in `User()` must resolve to models.py::User.
        let user_ref = occurrences
            .iter()
            .find(|o| o.name.as_deref() == Some("User") && o.role == ReferenceRoleDto::Read)
            .unwrap_or_else(|| panic!("no User Read occurrence:\n  {dump}"));
        assert!(
            user_ref.target_file.as_deref().is_some_and(|f| f.ends_with("models.py")),
            "User must resolve to models.py, got {:?}\n  {dump}",
            user_ref.target_file
        );
        assert_eq!(
            user_ref.target_name.as_deref(),
            Some("User"),
            "User reference target_name\n  {dump}"
        );

        // `save` used in `.save()` must resolve to models.py (the method).
        let save_ref = occurrences
            .iter()
            .find(|o| o.name.as_deref() == Some("save") && o.role == ReferenceRoleDto::Read)
            .unwrap_or_else(|| panic!("no save Read occurrence:\n  {dump}"));
        assert!(
            save_ref.target_file.as_deref().is_some_and(|f| f.ends_with("models.py")),
            "save must resolve to models.py, got {:?}\n  {dump}",
            save_ref.target_file
        );
        assert_eq!(
            save_ref.target_name.as_deref(),
            Some("save"),
            "save reference target_name\n  {dump}"
        );

        // The import binding resolves to models.py too.
        let user_import = occurrences
            .iter()
            .find(|o| o.name.as_deref() == Some("User") && o.role == ReferenceRoleDto::Import)
            .unwrap_or_else(|| panic!("no User Import occurrence:\n  {dump}"));
        assert!(
            user_import.target_file.as_deref().is_some_and(|f| f.ends_with("models.py")),
            "import User must resolve to models.py, got {:?}\n  {dump}",
            user_import.target_file
        );

        // `run` is a definition in this file.
        assert!(
            occurrences
                .iter()
                .any(|o| o.name.as_deref() == Some("run") && o.role == ReferenceRoleDto::Definition),
            "expected a run Definition occurrence\n  {dump}"
        );
    }

    #[test]
    fn test_every_occurrence_has_a_name() {
        let files = vec![("/project/test.py", "x = 1\nprint(x)\n")];
        let (_dir, state) = state_with_files(files);

        let test_path = state.root.join("test.py");
        let test_file = ruff_db::files::system_path_to_file(&state.db, &test_path)
            .expect("test.py should be known to the DB");

        let source = ruff_db::source::source_text(&state.db, test_file);
        let source_str = source.as_str();
        let line_index = LineIndex::from_source_text(source_str);

        let occurrences = convert_file_occurrences(&state.db, test_file, source_str, &line_index);

        assert!(!occurrences.is_empty(), "expected occurrences");
        for occ in &occurrences {
            assert!(occ.name.is_some(), "every occurrence must carry a name: {occ:?}");
        }
    }
}
