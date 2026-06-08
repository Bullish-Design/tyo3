//! Entity extraction from the code layer at a revision.
//!
//! Produces the set of `Entity` records reconciliation operates on (SPEC §5.4).
//! Walks the project's symbols via the analysis engine (the same source the
//! code layer uses), computing a `qualified_path` for each and an
//! AST-normalised `content_hash`.

use std::collections::HashSet;

use serde::{Deserialize, Serialize};

use ruff_db::files::File;
use ruff_db::source::source_text;
use ruff_source_file::LineIndex;
use ty_project::Db;

use crate::hash::{hash_entity, normalise_entity_source, ContentHash, HashPolicy};
use crate::project::TyProjectState;

// ── SymbolKind (internal, not the DTO) ───────────────────────────────────

/// The kind of an addressable symbol. Mirrors `ty_ide::SymbolKind`, kept as an
/// independent enum so the identity layer does not depend on ty_ide types for
/// its core data model.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum SymbolKind {
    Module,
    Class,
    Function,
    Method,
    Constructor,
    Variable,
    Constant,
    Field,
    Parameter,
    Property,
    TypeParameter,
    Import,
}

impl From<&ty_ide::SymbolKind> for SymbolKind {
    fn from(k: &ty_ide::SymbolKind) -> Self {
        match k {
            ty_ide::SymbolKind::Module => SymbolKind::Module,
            ty_ide::SymbolKind::Class => SymbolKind::Class,
            ty_ide::SymbolKind::Function => SymbolKind::Function,
            ty_ide::SymbolKind::Method => SymbolKind::Method,
            ty_ide::SymbolKind::Constructor => SymbolKind::Constructor,
            ty_ide::SymbolKind::Variable => SymbolKind::Variable,
            ty_ide::SymbolKind::Constant => SymbolKind::Constant,
            ty_ide::SymbolKind::Field => SymbolKind::Field,
            ty_ide::SymbolKind::Parameter => SymbolKind::Parameter,
            ty_ide::SymbolKind::Property => SymbolKind::Property,
            ty_ide::SymbolKind::TypeParameter => SymbolKind::TypeParameter,
            ty_ide::SymbolKind::Import => SymbolKind::Import,
        }
    }
}

// ── Entity ──────────────────────────────────────────────────────────────

/// One addressable entity in a project at a given revision.
///
/// `qualified_path` is `file_path::qualified_name` (e.g. `"pkg/mod.py::User::save"`).
/// It is the primary EXACT match key and the registry key. `content_hash` is the
/// secondary HASH match key. `name` is the unqualified leaf name. `container` is
/// the enclosing qualified path (used by STRUCT matching in Pass C).
///
/// `file`, `full_range`, `name_range`, and `qualified_name` are the structural
/// fields the native code-layer producer needs (Phase 2, §5.4). They are the
/// node-facing decomposition of `qualified_path`:
///   - `file` — the source file path (`File::path(...).as_str()` form).
///   - `full_range` — the entity's full definition range.
///   - `name_range` — the range of the symbol's *name* only (the analysis
///     "selection range"). Inheritance/supertype resolution MUST position its
///     cursor on this range, never on `full_range` start, or supertype lookups
///     silently return nothing.
///   - `qualified_name` — the leaf-relative *dotted* name (`User.save`), kept
///     separate from the `::`-joined, file-prefixed `qualified_path` the
///     registry keys on.
#[derive(Debug, Clone)]
pub struct Entity {
    pub qualified_path: String,
    pub kind: SymbolKind,
    pub content_hash: ContentHash,
    pub container: Option<String>,
    pub name: String,
    pub file: String,
    pub full_range: ruff_text_size::TextRange,
    pub name_range: ruff_text_size::TextRange,
    pub qualified_name: String,
}

// ── Extraction ──────────────────────────────────────────────────────────

/// Extract every addressable entity from a project state snapshot.
///
/// Walks the project's Python source files, calls `document_symbols` for each,
/// and builds an `Entity` with a normalised content hash for every symbol.
///
/// Symbols without a natural qualified name get a structural name derived from
/// container + kind + ordinal — never a line number (SPEC §5.6).
pub fn extract_entities(state: &TyProjectState) -> Vec<Entity> {
    let project = state.db.project();
    let indexed = project.files(&state.db);

    let source_files: HashSet<String> = indexed
        .iter()
        .filter(|f: &&File| {
            f.path(&state.db)
                .extension()
                .and_then(ruff_python_ast::PySourceType::try_from_extension)
                .is_some()
        })
        .map(|f| f.path(&state.db).as_str().to_string())
        .collect();

    extract_entities_for(state, &source_files)
}

/// Extract addressable entities only from the supplied file paths.
///
/// `files` uses the same path strings as `File::path(...).as_str()` and
/// `ChangeEvent::system_path()`. The full-project extractor delegates here with
/// the complete source-file set; normal commits pass their touched-file scope.
pub fn extract_entities_for(state: &TyProjectState, files: &HashSet<String>) -> Vec<Entity> {
    let policy = state.hash_policy;
    let project = state.db.project();
    let indexed = project.files(&state.db);

    let mut source_files: Vec<File> = indexed
        .iter()
        .filter(|f: &&File| {
            let path = f.path(&state.db);
            files.contains(path.as_str())
                && path
                    .extension()
                    .and_then(ruff_python_ast::PySourceType::try_from_extension)
                    .is_some()
        })
        .copied()
        .collect();
    source_files.sort_by(|a, b| {
        a.path(&state.db)
            .as_str()
            .cmp(b.path(&state.db).as_str())
    });

    let mut entities: Vec<Entity> = Vec::new();

    for file in &source_files {
        let file_path = file.path(&state.db).as_str().to_string();

        let flat_symbols = ty_ide::document_symbols(&state.db, *file);
        let hierarchical = flat_symbols.to_hierarchical();

        let src = source_text(&state.db, *file);
        let source_str = src.as_str();
        let line_index = LineIndex::from_source_text(source_str);

        // Walk the hierarchy from top-level entries, tracking visited ids to
        // avoid double-processing symbols that appear both as a root entry and
        // as a child.
        let mut visited: HashSet<ty_ide::SymbolId> = HashSet::new();

        for (id, info) in hierarchical.iter() {
            if visited.contains(&id) {
                continue;
            }
            collect_entities_recursive(
                &hierarchical,
                id,
                &info,
                source_str,
                &line_index,
                &file_path,
                None,
                None,
                &policy,
                &mut entities,
                &mut visited,
            );
        }
    }

    entities
}

/// Recursively walk a hierarchical symbol subtree, building `Entity` records.
fn collect_entities_recursive(
    hierarchical: &ty_ide::HierarchicalSymbols,
    id: ty_ide::SymbolId,
    info: &ty_ide::SymbolInfo,
    source_str: &str,
    _line_index: &LineIndex,
    file_path: &str,
    parent_qualified: Option<&str>,
    parent_dotted: Option<&str>,
    policy: &HashPolicy,
    entities: &mut Vec<Entity>,
    visited: &mut HashSet<ty_ide::SymbolId>,
) {
    visited.insert(id);

    let name = info.name.clone();
    let kind = SymbolKind::from(&info.kind);
    // The `::`-joined, file-prefixed registry key (EXACT match key).
    let qualified_path = match parent_qualified {
        Some(p) => format!("{}::{}", p, name),
        None => format!("{}::{}", file_path, name),
    };
    // The leaf-relative dotted name the graph node carries (`User.save`).
    let dotted_qualified_name = match parent_dotted {
        Some(p) => format!("{}.{}", p, name),
        None => name.to_string(),
    };
    let container = parent_qualified.map(|s| s.to_string());

    // Extract the entity's source text from its full_range.
    let entity_source = extract_range(source_str, info.full_range);

    // Normalise and hash.
    let normal_form = normalise_entity_source(&entity_source, policy);
    let content_hash = hash_entity(&normal_form);

    entities.push(Entity {
        qualified_path: qualified_path.clone(),
        kind,
        content_hash,
        container,
        name: name.to_string(),
        file: file_path.to_string(),
        full_range: info.full_range,
        name_range: info.name_range,
        qualified_name: dotted_qualified_name.clone(),
    });

    // Recurse into children.
    let children: Vec<_> = hierarchical.children(id).collect();
    for (child_id, child_info) in children {
        if visited.contains(&child_id) {
            continue;
        }
        collect_entities_recursive(
            hierarchical,
            child_id,
            &child_info,
            source_str,
            _line_index,
            file_path,
            Some(&qualified_path),
            Some(&dotted_qualified_name),
            policy,
            entities,
            visited,
        );
    }
}

/// Extract the source text within a `TextRange` from the full source.
fn extract_range(source: &str, range: ruff_text_size::TextRange) -> String {
    let start = range.start().to_usize();
    let end = range.end().to_usize();
    // Clamp to valid UTF-8 boundaries.
    let start = start.min(source.len());
    let end = end.min(source.len());
    source[start..end].to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use ruff_db::system::{SystemPath, SystemPathBuf};
    use std::io::Write;

    /// Build a minimal TyProjectState over a temp dir with the given source.
    fn state_with_source(python_src: &str) -> (tempfile::TempDir, TyProjectState) {
        use ruff_python_ast::name::Name;
        use ty_project::{ProjectDatabase, ProjectMetadata};
        use crate::overlay::OverlaySystem;
        use crate::content::ContentStore;

        let dir = tempfile::tempdir().unwrap();
        let py_path = dir.path().join("m.py");
        let mut f = std::fs::File::create(&py_path).unwrap();
        f.write_all(python_src.as_bytes()).unwrap();

        // Also write a pyproject.toml to avoid discovery warnings.
        let mut toml = std::fs::File::create(dir.path().join("pyproject.toml")).unwrap();
        toml.write_all(b"[project]\nname = \"test\"\nversion = \"0.1.0\"\n").unwrap();

        let root = SystemPathBuf::from_path_buf(
            dir.path().canonicalize().unwrap().to_path_buf(),
        ).unwrap();

        let system = OverlaySystem::live(root.clone(), ContentStore::new().capture());
        let metadata = ProjectMetadata::new(Name::new("test"), root.clone());
        let db = ProjectDatabase::use_defaults(metadata, system);

        (dir, TyProjectState { db, root, registry: None, hash_policy: HashPolicy::default(), hash_policies: std::collections::HashMap::new(), default_hash_profile: "structure".to_string(), authored: None })
    }

    #[test]
    fn extract_yields_expected_paths_and_kinds() {
        let src = "\
class User:
    def save(self):
        pass

def top_level():
    pass
";
        let (_dir, state) = state_with_source(src);
        let entities = extract_entities(&state);

        // Find each expected entity.
        let user = entities.iter().find(|e| e.name == "User" && e.kind == SymbolKind::Class);
        assert!(user.is_some(), "User class not found");
        assert!(
            user.unwrap().qualified_path.ends_with("m.py::User"),
            "qualified_path ends with m.py::User: {}",
            user.unwrap().qualified_path
        );

        let save = entities.iter().find(|e| e.name == "save" && e.kind == SymbolKind::Method);
        assert!(save.is_some(), "save method not found");
        assert!(
            save.unwrap().qualified_path.contains("User::save"),
            "nested under User: {}",
            save.unwrap().qualified_path
        );

        let top = entities
            .iter()
            .find(|e| e.name == "top_level" && e.kind == SymbolKind::Function);
        assert!(top.is_some(), "top_level function not found");
    }

    #[test]
    fn whitespace_edit_does_not_change_hash() {
        let src = "\
class User:
    def save(self):
        pass
";
        let (_dir, state) = state_with_source(src);
        let entities = extract_entities(&state);
        let save = entities.iter().find(|e| e.name == "save").unwrap();
        let hash1 = save.content_hash;

        // Re-extract with different indentation.
        let src2 = "\
class User:
   def save(self):
      pass
";
        let (_dir2, state2) = state_with_source(src2);
        let entities2 = extract_entities(&state2);
        let save2 = entities2.iter().find(|e| e.name == "save").unwrap();
        let hash2 = save2.content_hash;

        assert_eq!(hash1, hash2, "whitespace edit should not change content hash");
    }

    #[test]
    fn structural_fields_name_range_covers_only_name() {
        // For `class A: def b(self): ...`, the `b` method entity must carry a
        // name_range covering only `b` (one token), a strictly-larger
        // full_range, a file ending in m.py, and a dotted qualified_name "A.b".
        let src = "class A:\n    def b(self):\n        pass\n";
        let (_dir, state) = state_with_source(src);
        let entities = extract_entities(&state);

        let b = entities
            .iter()
            .find(|e| e.name == "b" && e.kind == SymbolKind::Method)
            .expect("method b not found");

        // name_range covers exactly "b" (length 1).
        assert_eq!(
            u32::from(b.name_range.len()),
            1,
            "name_range should cover only the name `b`"
        );
        // full_range is strictly larger than name_range.
        assert!(
            b.full_range.len() > b.name_range.len(),
            "full_range must be strictly larger than name_range"
        );
        // The name_range sits inside the full_range.
        assert!(
            b.full_range.contains_range(b.name_range),
            "full_range must contain name_range"
        );
        assert!(b.file.ends_with("m.py"), "file should end with m.py: {}", b.file);
        assert_eq!(b.qualified_name, "A.b", "dotted qualified_name");
        assert_eq!(b.qualified_path, format!("{}::A::b", b.file));
    }

    #[test]
    fn no_line_number_in_qualified_path() {
        let src = "class A:\n    def b(self):\n        pass\n";
        let (_dir, state) = state_with_source(src);
        let entities = extract_entities(&state);
        for e in &entities {
            assert!(
                !e.qualified_path.contains("line"),
                "qualified_path must not contain 'line': {}",
                e.qualified_path
            );
        }
    }
}
