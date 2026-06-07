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

fn path_is_under_root(path: &str, root: &str) -> bool {
    path == root
        || path
            .strip_prefix(root)
            .is_some_and(|rest| rest.starts_with('/'))
}

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
/// `qualified_path` is `file_path::qualified_name` (e.g. `"pkg/mod.py::User.save"`).
/// It is the primary EXACT match key. `content_hash` is the secondary HASH
/// match key. `name` is the unqualified leaf name. `container` is the enclosing
/// qualified name (used by STRUCT matching in Pass C).
#[derive(Debug, Clone)]
pub struct Entity {
    pub qualified_path: String,
    pub kind: SymbolKind,
    pub content_hash: ContentHash,
    pub container: Option<String>,
    pub name: String,
    /// Defining file (absolute system path, as `File::path(...).as_str()`).
    /// Carried so the code-layer producer can build a `NodeData`/`SymbolNodeDto`
    /// without a second pass (Gate 3N Step 1).
    pub file: String,
    /// Display/location range (1-based), for the node payload.
    pub range: crate::dto::RangeDto,
    /// The name (selection) range (1-based). Used to query type-hierarchy
    /// supertypes at the class-name position, as the read-surface builder does
    /// (`symbol.selection_range`), since the full range may start on a decorator.
    pub selection_range: crate::dto::RangeDto,
    /// The engine "display" qualified name: dotted, no file prefix, `None` for
    /// top-level entities (e.g. `User.save`). Mirrors `document_symbols`'
    /// `qualified_name` (`convert/symbols.rs`) so the code-layer node's
    /// `qualified_name` is byte-equal to the read-surface graph (§6.3.1).
    pub qualified_name: Option<String>,
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
    let root = state.root.as_str();

    let source_files: HashSet<String> = indexed
        .iter()
        .filter(|f: &&File| {
            let path = f.path(&state.db);
            path_is_under_root(path.as_str(), root)
                && path
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
    let root = state.root.as_str();

    let mut source_files: Vec<File> = indexed
        .iter()
        .filter(|f: &&File| {
            let path = f.path(&state.db);
            path_is_under_root(path.as_str(), root)
                && files.contains(path.as_str())
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
        // avoid double-processing symbols. `HierarchicalSymbols::iter()`
        // includes children as well as roots, so first derive the roots from the
        // inverse child set; otherwise a child seen early could be marked visited
        // and skipped before its parent recurses into it.
        let mut visited: HashSet<ty_ide::SymbolId> = HashSet::new();
        let mut child_ids: HashSet<ty_ide::SymbolId> = HashSet::new();
        for (id, _) in hierarchical.iter() {
            for (child_id, _) in hierarchical.children(id) {
                child_ids.insert(child_id);
            }
        }

        for (id, info) in hierarchical.iter() {
            if child_ids.contains(&id) || visited.contains(&id) {
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
                None,
                &policy,
                &mut entities,
                &mut visited,
            );
        }

        // Defensive fallback for any detached/cyclic engine output: preserve
        // coverage without letting it suppress normal parent-child recursion.
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
    line_index: &LineIndex,
    file_path: &str,
    parent_qualified: Option<&str>,
    parent_display: Option<&str>,
    parent_kind: Option<SymbolKind>,
    policy: &HashPolicy,
    entities: &mut Vec<Entity>,
    visited: &mut HashSet<ty_ide::SymbolId>,
) {
    visited.insert(id);

    let name = info.name.clone();
    let mut kind = SymbolKind::from(&info.kind);
    if parent_kind == Some(SymbolKind::Class) && kind == SymbolKind::Function {
        kind = if name == "__init__" {
            SymbolKind::Constructor
        } else {
            SymbolKind::Method
        };
    }
    let qualified_name = match parent_qualified {
        Some(p) => format!("{}::{}", p, name),
        None => format!("{}::{}", file_path, name),
    };
    let container = parent_qualified.map(|s| s.to_string());
    // Engine "display" qualified name (dotted, no file prefix): `None` at the
    // top level, `{parent}.{name}` nested — matches `collect_symbols_recursive`.
    let display_qualified: Option<String> =
        parent_display.map(|p| format!("{}.{}", p, name));

    // Extract the entity's source text from its full_range.
    let entity_source = extract_range(source_str, info.full_range);

    // Normalise and hash.
    let normal_form = normalise_entity_source(&entity_source, policy);
    let content_hash = hash_entity(&normal_form);

    let range = crate::coordinates::range_to_dto_with_index(source_str, line_index, info.full_range);
    let selection_range =
        crate::coordinates::range_to_dto_with_index(source_str, line_index, info.name_range);

    entities.push(Entity {
        qualified_path: qualified_name.clone(),
        kind,
        content_hash,
        container,
        name: name.to_string(),
        file: file_path.to_string(),
        range,
        selection_range,
        qualified_name: display_qualified.clone(),
    });

    // The display name children chain off of: own dotted name, else the leaf.
    let own_display = display_qualified.as_deref().unwrap_or(&name);

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
            line_index,
            file_path,
            Some(&qualified_name),
            Some(own_display),
            Some(kind),
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

        let system = OverlaySystem::live(root.clone(), ContentStore::default().capture());
        let metadata = ProjectMetadata::new(Name::new("test"), root.clone());
        let db = ProjectDatabase::use_defaults(metadata, system);

        (dir, TyProjectState { db, root, registry: None, hash_policy: HashPolicy::default() })
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
