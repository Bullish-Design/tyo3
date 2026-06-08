//! The native code layer and code-delta producer (Phase 2, §5.4).
//!
//! `CodeLayer` is the authoritative structural state: nodes keyed by
//! `DurableId`, a deterministically-ordered edge set, and a reverse-dependency
//! index (target → sources that reference / import / inherit it) that Phase 3's
//! affected-set closure reads.
//!
//! [`produce_code_delta`] builds the full structural set for a project state and
//! diffs it against the previous layer to emit a minimal [`CodeDeltaDto`]. It is
//! a faithful native port of the legacy six-pass Python build
//! (`graph/graph.py`): materialise all nodes, then containment, then
//! references/imports, then inheritance in **two passes** (all `inherits`, then
//! all `overrides`). It reads the same analysis surface the FFI read methods use
//! (`compute_document_symbols` / `compute_file_occurrences` / `compute_supertypes`)
//! so the node payloads are identical to the legacy build's — which is what the
//! tiered parity oracle checks.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};

use crate::dto::{
    CodeDeltaDto, CodeEdgeDto, CodeNodeDto, CodeNodeMovedDto, NameOccurrenceDto, PositionDto,
    RangeDto, ReferenceRoleDto, SymbolDto, SymbolKindDto,
};
use crate::project::{
    compute_document_symbols, compute_file_occurrences, compute_files, compute_supertypes,
    TyProjectState,
};

// ── Synthetic id helpers (must match `graph/identity.py` byte-for-byte) ──────

/// Stable synthetic DurableId for a module node: `"<module>" + file`
/// (mirrors `make_module_durable_id`, `graph/identity.py:58`).
pub fn make_module_durable_id(file: &str) -> String {
    format!("<module>{}", file)
}

// ── EdgeKind (mirrors Python `EdgeKind` StrEnum values used by the graph) ────

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum EdgeKind {
    Defines,
    Contains,
    References,
    Imports,
    Inherits,
    Overrides,
}

impl EdgeKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            EdgeKind::Defines => "defines",
            EdgeKind::Contains => "contains",
            EdgeKind::References => "references",
            EdgeKind::Imports => "imports",
            EdgeKind::Inherits => "inherits",
            EdgeKind::Overrides => "overrides",
        }
    }

    /// Dependency edges contribute to the reverse-dependency index (target →
    /// sources). Containment is structural nesting, not a dependency.
    fn is_dependency(&self) -> bool {
        matches!(
            self,
            EdgeKind::References | EdgeKind::Imports | EdgeKind::Inherits | EdgeKind::Overrides
        )
    }
}

/// A comparable range tuple `(start_line, start_col, end_line, end_col)`, used so
/// `Edge` can derive `Ord` (the DTO `RangeDto` is not `Ord`).
type RangeTuple = (u32, u32, u32, u32);

fn range_tuple(r: &RangeDto) -> RangeTuple {
    (r.start.line, r.start.column, r.end.line, r.end.column)
}

fn tuple_to_range(t: RangeTuple) -> RangeDto {
    RangeDto {
        start: PositionDto { line: t.0, column: t.1 },
        end: PositionDto { line: t.2, column: t.3 },
    }
}

// ── Edge ─────────────────────────────────────────────────────────────────────

/// One graph relationship. `Ord` is derived for deterministic, reproducible
/// emission order (§5.12). Reference/import edges carry the cosmetic
/// `role`/`file`/`range`; containment/inheritance/override edges leave them
/// `None` so they contribute only the structural `(source, target, kind)` fact.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Edge {
    pub source: String,
    pub target: String,
    pub kind: EdgeKind,
    pub role: Option<String>,
    pub file: Option<String>,
    pub range: Option<RangeTuple>,
}

impl Edge {
    fn to_dto(&self) -> CodeEdgeDto {
        CodeEdgeDto {
            source_id: self.source.clone(),
            destination_id: self.target.clone(),
            kind: self.kind.as_str().to_string(),
            role: self.role.clone(),
            file: self.file.clone(),
            range: self.range.map(tuple_to_range),
        }
    }
}

// ── NodeData ─────────────────────────────────────────────────────────────────

/// A graph node payload. Small structural fields only (§5.4): no vectors of text,
/// no source bodies. The content hash is the only heavy-ish field and it is
/// fixed-width hex.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NodeData {
    /// Unqualified leaf name (module stem for module nodes).
    pub name: String,
    pub qualified_name: String,
    /// Lowercased `SymbolKind` value, e.g. `"class_"` / `"function"` / `"module"`.
    pub kind: String,
    pub file: String,
    pub range: RangeDto,
    /// Selection (name) range; `None` for synthetic module/external nodes.
    pub name_range: Option<RangeDto>,
    pub content_hash: Option<String>,
    pub content_hashes: BTreeMap<String, String>,
    pub external: bool,
    pub package: Option<String>,
}

impl NodeData {
    fn to_dto(&self, durable_id: &str) -> CodeNodeDto {
        CodeNodeDto {
            durable_id: durable_id.to_string(),
            name: self.name.clone(),
            qualified_name: self.qualified_name.clone(),
            kind: self.kind.clone(),
            file: self.file.clone(),
            range: self.range.clone(),
            name_range: self.name_range.clone(),
            content_hash: self.content_hash.clone(),
            content_hashes: self.content_hashes.clone().into_iter().collect(),
            external: self.external,
            package: self.package.clone(),
        }
    }

    /// Structural-field equality for diff classification (a difference here means
    /// re-emit the node, not a move).
    fn structurally_same(&self, other: &NodeData) -> bool {
        self.name == other.name
            && self.qualified_name == other.qualified_name
            && self.kind == other.kind
            && self.content_hash == other.content_hash
            && self.external == other.external
    }
}

// ── CodeLayer ────────────────────────────────────────────────────────────────

/// The canonical structural state for one revision.
#[derive(Debug, Clone, Default)]
pub struct CodeLayer {
    pub nodes: BTreeMap<String, NodeData>,
    pub edges: BTreeSet<Edge>,
    /// target id → sources that reference / import / inherit it.
    pub reverse_deps: BTreeMap<String, BTreeSet<String>>,
}

impl CodeLayer {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn is_empty(&self) -> bool {
        self.nodes.is_empty() && self.edges.is_empty()
    }

    pub fn upsert_node(&mut self, durable_id: String, node: NodeData) {
        self.nodes.insert(durable_id, node);
    }

    /// Insert an edge, maintaining `reverse_deps` for dependency kinds. Returns
    /// `true` if the edge was newly added.
    pub fn add_edge(&mut self, edge: Edge) -> bool {
        if edge.kind.is_dependency() {
            self.reverse_deps
                .entry(edge.target.clone())
                .or_default()
                .insert(edge.source.clone());
        }
        self.edges.insert(edge)
    }

    /// Remove an edge, pruning its `reverse_deps` entry when no parallel
    /// dependency edge of the same (source, target) remains.
    pub fn remove_edge(&mut self, edge: &Edge) -> bool {
        let removed = self.edges.remove(edge);
        if removed && edge.kind.is_dependency() {
            let still_dep = self.edges.iter().any(|e| {
                e.kind.is_dependency() && e.source == edge.source && e.target == edge.target
            });
            if !still_dep {
                if let Some(sources) = self.reverse_deps.get_mut(&edge.target) {
                    sources.remove(&edge.source);
                    if sources.is_empty() {
                        self.reverse_deps.remove(&edge.target);
                    }
                }
            }
        }
        removed
    }

    /// Diff `self` (the freshly-produced layer) against `prev`, emitting a
    /// minimal incremental delta. With an empty `prev` (cold start) this yields a
    /// full delta: every node upserted, every edge added.
    pub fn diff_from(&self, prev: &CodeLayer, revision: u64, rescan: bool) -> CodeDeltaDto {
        let mut nodes_upserted = Vec::new();
        let mut nodes_moved = Vec::new();
        let mut nodes_removed = Vec::new();

        for (did, node) in &self.nodes {
            match prev.nodes.get(did) {
                None => nodes_upserted.push(node.to_dto(did)),
                Some(p) if !p.structurally_same(node) => nodes_upserted.push(node.to_dto(did)),
                Some(p) if p.file != node.file || p.range != node.range => {
                    nodes_moved.push(CodeNodeMovedDto {
                        durable_id: did.clone(),
                        file: node.file.clone(),
                        range: node.range.clone(),
                        name_range: node.name_range.clone(),
                    });
                }
                Some(_) => {} // unchanged
            }
        }
        for did in prev.nodes.keys() {
            if !self.nodes.contains_key(did) {
                nodes_removed.push(did.clone());
            }
        }

        let edges_added: Vec<CodeEdgeDto> = self
            .edges
            .difference(&prev.edges)
            .map(Edge::to_dto)
            .collect();
        let edges_removed: Vec<CodeEdgeDto> = prev
            .edges
            .difference(&self.edges)
            .map(Edge::to_dto)
            .collect();

        CodeDeltaDto {
            revision,
            rescan,
            nodes_upserted,
            nodes_removed,
            nodes_moved,
            edges_added,
            edges_removed,
        }
    }
}

// ── kind / role helpers ──────────────────────────────────────────────────────

/// The lowercased string the Python `SymbolKind` StrEnum uses (the serde
/// `rename_all = "snake_case"` form, with the `class_` / `import_` overrides).
fn symbol_kind_str(kind: &SymbolKindDto) -> &'static str {
    match kind {
        SymbolKindDto::Module => "module",
        SymbolKindDto::Class => "class_",
        SymbolKindDto::Function => "function",
        SymbolKindDto::Method => "method",
        SymbolKindDto::Constructor => "constructor",
        SymbolKindDto::Variable => "variable",
        SymbolKindDto::Constant => "constant",
        SymbolKindDto::Field => "field",
        SymbolKindDto::Parameter => "parameter",
        SymbolKindDto::Property => "property",
        SymbolKindDto::TypeParameter => "type_parameter",
        SymbolKindDto::Import => "import_",
        SymbolKindDto::Unknown => "unknown",
    }
}

fn role_str(role: &ReferenceRoleDto) -> &'static str {
    match role {
        ReferenceRoleDto::Read => "read",
        ReferenceRoleDto::Write => "write",
        ReferenceRoleDto::Import => "import",
        ReferenceRoleDto::Definition => "definition",
        ReferenceRoleDto::Other => "other",
    }
}

fn module_range() -> RangeDto {
    RangeDto {
        start: PositionDto { line: 1, column: 1 },
        end: PositionDto { line: 1, column: 1 },
    }
}

/// The module stem (`PurePosixPath(file).stem`): drop the directory and the last
/// extension. `"pkg/mod.py"` → `"mod"`, `"main.py"` → `"main"`.
fn file_stem(file: &str) -> String {
    let base = file.rsplit('/').next().unwrap_or(file);
    match base.rsplit_once('.') {
        Some((stem, _ext)) if !stem.is_empty() => stem.to_string(),
        _ => base.to_string(),
    }
}

/// Port of `CodeGraph._infer_package` (`graph/graph.py:953`).
fn infer_package(file_path: &str) -> Option<String> {
    if let Some((_, rest)) = file_path.split_once("site-packages/") {
        if let Some(first) = rest.split('/').next() {
            if !first.is_empty() {
                return Some(first.to_string());
            }
        }
    }
    if file_path.contains("/lib/python") || file_path.contains("typeshed") {
        return Some("stdlib".to_string());
    }
    for sep in ["/.venv/", "/venv/"] {
        if let Some((_, rest)) = file_path.split_once(sep) {
            let parts: Vec<&str> = rest.split('/').collect();
            if parts.len() > 2 && parts[0] == "lib" {
                for (i, part) in parts.iter().enumerate() {
                    if *part == "site-packages" && i + 1 < parts.len() {
                        return Some(parts[i + 1].to_string());
                    }
                }
            }
        }
    }
    None
}

// ── The producer ─────────────────────────────────────────────────────────────

/// Build the full code layer for `state` and diff it against `prev`.
///
/// Returns the next `CodeLayer` (to store on the head) and the minimal
/// `CodeDeltaDto` (to emit alongside the legacy build). `rescan` marks a full
/// delta; the cold-start accessor passes `rescan = true` against an empty `prev`.
///
/// `_scope` is accepted for the Phase 3/5 incremental contract; Phase 2 always
/// rebuilds the full set and relies on the diff to stay minimal.
pub fn produce_code_delta(
    state: &TyProjectState,
    prev: &CodeLayer,
    revision: u64,
    rescan: bool,
    _scope: Option<&HashSet<String>>,
) -> (CodeLayer, CodeDeltaDto) {
    let mut builder = Builder::new(state);
    builder.build();
    let next = builder.layer;
    let delta = next.diff_from(prev, revision, rescan || prev.is_empty());
    (next, delta)
}

/// Per-file collected symbols, in document order.
struct FileSymbols {
    graph_path: String,
    native_path: String,
    symbols: Vec<SymbolDto>,
}

/// Mutable build state mirroring `CodeGraph`'s secondary indexes.
struct Builder<'a> {
    state: &'a TyProjectState,
    root: String,
    layer: CodeLayer,
    /// (graph_file, name) → durable_id, with `""` marking a short-name collision.
    name_to_id: HashMap<(String, String), String>,
    /// graph_file → durable_ids in insertion order (module first).
    file_to_nodes: HashMap<String, Vec<String>>,
    /// graph_file → range cache sorted by span ascending: (sl, sc, el, ec, did).
    range_cache: HashMap<String, Vec<(u32, u32, u32, u32, String)>>,
    /// The set of project (graph) file paths.
    project_files: HashSet<String>,
}

impl<'a> Builder<'a> {
    fn new(state: &'a TyProjectState) -> Self {
        Builder {
            state,
            root: state.root.as_str().to_string(),
            layer: CodeLayer::new(),
            name_to_id: HashMap::new(),
            file_to_nodes: HashMap::new(),
            range_cache: HashMap::new(),
            project_files: HashSet::new(),
        }
    }

    /// Convert an absolute native path to a project-relative POSIX graph path.
    /// Paths outside the root are returned unchanged (external).
    fn to_graph_path(&self, native: &str) -> String {
        if let Some(rest) = native.strip_prefix(&self.root) {
            let rest = rest.strip_prefix('/').unwrap_or(rest);
            if !rest.is_empty() {
                return rest.to_string();
            }
        }
        native.to_string()
    }

    /// `_normalize_result_path`: relativise, but keep external targets raw.
    fn normalize_target(&self, native: &str) -> String {
        let candidate = self.to_graph_path(native);
        if self.project_files.contains(&candidate) {
            candidate
        } else {
            native.to_string()
        }
    }

    fn build(&mut self) {
        // Pass 1: collect symbols per file, in `files()` order (so node insertion
        // order — which the short-name collision fallback depends on — matches the
        // legacy build).
        let mut files: Vec<FileSymbols> = Vec::new();
        for native_path in compute_files(self.state) {
            let graph_path = self.to_graph_path(&native_path);
            self.project_files.insert(graph_path.clone());
            match compute_document_symbols(self.state, &native_path) {
                Ok(symbols) => files.push(FileSymbols {
                    graph_path,
                    native_path,
                    symbols,
                }),
                Err(_) => {
                    // Legacy logs and skips the file for the remaining passes.
                }
            }
        }

        // Pass 2: materialise all nodes before any edge.
        for fs in &files {
            self.materialize_file_nodes(&fs.graph_path, &fs.symbols);
        }

        // Pass 3: containment edges + range caches.
        for fs in &files {
            self.add_containment_edges(&fs.graph_path, &fs.symbols);
            self.build_range_cache(&fs.graph_path);
        }

        // Pass 4: references + imports.
        for fs in &files {
            self.resolve_references(&fs.graph_path, &fs.native_path);
        }

        // Pass 5: inheritance in two passes.
        for fs in &files {
            self.inherits_pass(&fs.graph_path, &fs.native_path, &fs.symbols);
        }
        for fs in &files {
            self.overrides_pass(&fs.graph_path, &fs.symbols);
        }
    }

    fn register_name(&mut self, file: &str, name: &str, did: &str) {
        let key = (file.to_string(), name.to_string());
        match self.name_to_id.get(&key) {
            None => {
                self.name_to_id.insert(key, did.to_string());
            }
            Some(existing) if existing == did => {}
            // Short-name collision: mark ambiguous so lookups fall back to the
            // ordered file scan (which returns the first-inserted match).
            Some(_) => {
                self.name_to_id.insert(key, String::new());
            }
        }
    }

    fn materialize_file_nodes(&mut self, file: &str, symbols: &[SymbolDto]) {
        let module_id = make_module_durable_id(file);
        if !self.layer.nodes.contains_key(&module_id) {
            self.layer.upsert_node(
                module_id.clone(),
                NodeData {
                    name: file_stem(file),
                    qualified_name: "<module>".to_string(),
                    kind: "module".to_string(),
                    file: file.to_string(),
                    range: module_range(),
                    name_range: None,
                    content_hash: None,
                    content_hashes: BTreeMap::new(),
                    external: false,
                    package: None,
                },
            );
            self.name_to_id
                .insert((file.to_string(), "<module>".to_string()), module_id.clone());
            self.file_to_nodes
                .entry(file.to_string())
                .or_default()
                .push(module_id);
        }

        for symbol in symbols {
            let Some(did) = symbol.durable_id.clone() else {
                // Legacy raises here; for parity-only Phase 2 we skip an
                // unbound symbol rather than panic in the commit path.
                continue;
            };
            if self.layer.nodes.contains_key(&did) {
                continue;
            }
            let qn = symbol
                .qualified_name
                .clone()
                .unwrap_or_else(|| symbol.name.clone());
            let node = NodeData {
                name: symbol.name.clone(),
                qualified_name: qn.clone(),
                kind: symbol_kind_str(&symbol.kind).to_string(),
                file: file.to_string(),
                range: symbol.location.range.clone(),
                name_range: symbol.selection_range.clone(),
                content_hash: symbol.content_hash.clone(),
                content_hashes: symbol.content_hashes.clone().into_iter().collect(),
                external: false,
                package: None,
            };
            self.layer.upsert_node(did.clone(), node);
            self.register_name(file, &symbol.name, &did);
            self.register_name(file, &qn, &did);
            self.file_to_nodes
                .entry(file.to_string())
                .or_default()
                .push(did);
        }
    }

    fn add_stub_node(
        &mut self,
        did: &str,
        name: &str,
        qualified_name: &str,
        kind: &str,
        package: &str,
    ) {
        if self.layer.nodes.contains_key(did) {
            return;
        }
        self.layer.upsert_node(
            did.to_string(),
            NodeData {
                name: name.to_string(),
                qualified_name: qualified_name.to_string(),
                kind: kind.to_string(),
                file: "<external>".to_string(),
                range: module_range(),
                name_range: None,
                content_hash: None,
                content_hashes: BTreeMap::new(),
                external: true,
                package: Some(package.to_string()),
            },
        );
    }

    fn find_symbol_in_file(&self, file: &str, name: &str) -> Option<String> {
        if let Some(cached) = self.name_to_id.get(&(file.to_string(), name.to_string())) {
            if !cached.is_empty() {
                return Some(cached.clone());
            }
        }
        // Fallback: ordered scan returns the first-inserted matching node.
        for did in self.file_to_nodes.get(file).into_iter().flatten() {
            if let Some(node) = self.layer.nodes.get(did) {
                if node.name == name || node.qualified_name == name {
                    return Some(did.clone());
                }
            }
        }
        None
    }

    fn add_containment_edges(&mut self, file: &str, symbols: &[SymbolDto]) {
        let module_id = make_module_durable_id(file);
        for symbol in symbols {
            let Some(did) = symbol.durable_id.clone() else {
                continue;
            };
            // `_resolve_parent_id`: container_name → node, else the module.
            let parent_id = match &symbol.container_name {
                Some(cn) => self
                    .find_symbol_in_file(file, cn)
                    .unwrap_or_else(|| module_id.clone()),
                None => module_id.clone(),
            };
            if self.layer.nodes.contains_key(&parent_id) {
                let kind = if parent_id == module_id {
                    EdgeKind::Defines
                } else {
                    EdgeKind::Contains
                };
                self.try_add_edge(&parent_id, &did, kind, None, None, None);
            }
        }
    }

    fn build_range_cache(&mut self, file: &str) {
        let mut entries: Vec<(u32, u32, u32, u32, String)> = Vec::new();
        for did in self.file_to_nodes.get(file).into_iter().flatten() {
            if let Some(node) = self.layer.nodes.get(did) {
                if node.kind == "module" {
                    continue;
                }
                let r = &node.range;
                entries.push((
                    r.start.line,
                    r.start.column,
                    r.end.line,
                    r.end.column,
                    did.clone(),
                ));
            }
        }
        // `_range_size`: (line span, end column) ascending — smallest first.
        entries.sort_by(|a, b| {
            let span_a = (a.2 - a.0, a.3);
            let span_b = (b.2 - b.0, b.3);
            span_a.cmp(&span_b)
        });
        self.range_cache.insert(file.to_string(), entries);
    }

    fn find_enclosing_symbol(&self, file: &str, range: &RangeDto) -> Option<String> {
        if let Some(cache) = self.range_cache.get(file) {
            for (sl, sc, el, ec, did) in cache {
                if (*sl, *sc) <= (range.start.line, range.start.column)
                    && (*el, *ec) >= (range.end.line, range.end.column)
                {
                    return Some(did.clone());
                }
            }
        }
        let module_id = make_module_durable_id(file);
        if self.layer.nodes.contains_key(&module_id) {
            return Some(module_id);
        }
        None
    }

    /// Add an edge only if both endpoints exist (mirrors `_add_edge`).
    fn try_add_edge(
        &mut self,
        source: &str,
        target: &str,
        kind: EdgeKind,
        role: Option<String>,
        file: Option<String>,
        range: Option<RangeTuple>,
    ) {
        if !self.layer.nodes.contains_key(source) || !self.layer.nodes.contains_key(target) {
            return;
        }
        self.layer.add_edge(Edge {
            source: source.to_string(),
            target: target.to_string(),
            kind,
            role,
            file,
            range,
        });
    }

    fn resolve_references(&mut self, file: &str, native_file: &str) {
        let occurrences: Vec<NameOccurrenceDto> =
            match compute_file_occurrences(self.state, native_file) {
                Ok(occ) => occ,
                Err(_) => return, // legacy falls back to tokens; occurrences rarely fail.
            };

        for occ in &occurrences {
            // Import occurrence without a resolved target → goto fallback.
            if occ.role == ReferenceRoleDto::Import && occ.target_file.is_none() {
                self.resolve_import_via_goto(native_file, file, &occ.range);
                continue;
            }
            let (Some(target_file_raw), Some(target_name)) =
                (occ.target_file.as_ref(), occ.target_name.as_ref())
            else {
                continue;
            };
            let target_file = self.normalize_target(target_file_raw);

            // Resolve the target node: qualified name first (always None here),
            // then short name — whose collision fallback is load-bearing.
            let mut target_did = occ
                .target_qualified_name
                .as_ref()
                .and_then(|qn| self.find_symbol_in_file(&target_file, qn));
            if target_did.is_none() {
                target_did = self.find_symbol_in_file(&target_file, target_name);
            }

            let target_did = match target_did {
                Some(t) => t,
                None => {
                    if self.project_files.contains(&target_file) {
                        // Project-local but unresolved (local var/param, or a
                        // genuine miss): skip, like the legacy resolver.
                        continue;
                    }
                    match self.ensure_external_target(&target_file, target_name) {
                        Some(t) => t,
                        None => continue,
                    }
                }
            };

            let Some(enclosing_id) = self.find_enclosing_symbol(file, &occ.range) else {
                continue;
            };
            if enclosing_id == target_did {
                continue; // no self-reference at a definition site
            }

            if occ.role == ReferenceRoleDto::Import {
                if target_file != file {
                    self.add_import_edge(file, &target_file, &occ.range);
                }
                continue;
            }

            self.try_add_edge(
                &enclosing_id,
                &target_did,
                EdgeKind::References,
                Some(role_str(&occ.role).to_string()),
                Some(file.to_string()),
                Some(range_tuple(&occ.range)),
            );

            if target_file != file {
                self.add_import_edge(file, &target_file, &occ.range);
            }
        }
    }

    /// `_ensure_target_node_simple`: create an external stub for an off-project
    /// reference target (kind UNKNOWN). Returns the stub id, or `None`.
    fn ensure_external_target(&mut self, target_file: &str, target_name: &str) -> Option<String> {
        if self.project_files.contains(target_file) {
            return None;
        }
        let package = infer_package(target_file);
        let ext_did = match &package {
            Some(p) => format!("{}::{}", p, target_name),
            None => format!("{}::{}", target_file, target_name),
        };
        self.add_stub_node(
            &ext_did,
            target_name,
            target_name,
            "unknown",
            package.as_deref().unwrap_or("unknown"),
        );
        Some(ext_did)
    }

    /// `_add_import_edge`: a module-level IMPORTS edge, with an external
    /// `package::<module>` stub when the target is off-project.
    fn add_import_edge(&mut self, source_file: &str, target_file: &str, range: &RangeDto) {
        let source_module = make_module_durable_id(source_file);
        if !self.layer.nodes.contains_key(&source_module) {
            return;
        }
        let mut target_module = make_module_durable_id(target_file);
        if !self.layer.nodes.contains_key(&target_module) {
            if self.project_files.contains(target_file) {
                return; // project file without a module node — skip
            }
            let package = infer_package(target_file).unwrap_or_else(|| "unknown".to_string());
            target_module = format!("{}::<module>", package);
            if !self.layer.nodes.contains_key(&target_module) {
                self.add_stub_node(&target_module, &package, "<module>", "module", &package);
            }
        }
        self.try_add_edge(
            &source_module,
            &target_module,
            EdgeKind::Imports,
            None,
            Some(source_file.to_string()),
            Some(range_tuple(range)),
        );
    }

    /// `_resolve_import_via_goto`: a plain `import x` whose occurrence carries no
    /// target. Use goto_definition at the import site to find the module.
    fn resolve_import_via_goto(&mut self, native_file: &str, file: &str, range: &RangeDto) {
        use crate::project::compute_navigate;
        let targets = match compute_navigate(
            self.state,
            native_file,
            range.start.line,
            range.start.column,
            ty_ide::goto_definition,
        ) {
            Ok(t) => t,
            Err(_) => return,
        };
        let Some(target) = targets.first() else {
            return;
        };
        let target_file = self.normalize_target(&target.path);
        if target_file != file {
            self.add_import_edge(file, &target_file, range);
        }
    }

    /// Pass I: all INHERITS edges. The supertype cursor sits on the class
    /// **name range** (`selection_range`), never the `class` keyword.
    fn inherits_pass(&mut self, file: &str, native_file: &str, symbols: &[SymbolDto]) {
        for symbol in symbols {
            if symbol.kind != SymbolKindDto::Class {
                continue;
            }
            let Some(did) = symbol.durable_id.clone() else {
                continue;
            };
            let start = symbol
                .selection_range
                .as_ref()
                .map(|r| (r.start.line, r.start.column))
                .unwrap_or((symbol.location.range.start.line, symbol.location.range.start.column));

            let supertypes = match compute_supertypes(self.state, native_file, start.0, start.1) {
                Ok(s) => s,
                Err(_) => continue,
            };

            for supertype in &supertypes {
                let super_file = self.normalize_target(&supertype.path);
                let super_did = match self.find_symbol_in_file(&super_file, &supertype.name) {
                    Some(s) => s,
                    None => {
                        let mut package = infer_package(&super_file);
                        if package.is_none() && !self.file_to_nodes.contains_key(&super_file) {
                            package = Some("unknown".to_string());
                        }
                        match package {
                            Some(pkg) => {
                                let ext_did = format!("{}::{}", pkg, supertype.name);
                                self.add_stub_node(
                                    &ext_did,
                                    &supertype.name,
                                    &format!("{}.{}", pkg, supertype.name),
                                    "class_",
                                    &pkg,
                                );
                                ext_did
                            }
                            None => continue,
                        }
                    }
                };
                self.try_add_edge(&did, &super_did, EdgeKind::Inherits, None, None, None);
            }
        }
    }

    /// Direct children via outgoing DEFINES/CONTAINS edges (insertion order).
    fn children_of(&self, did: &str) -> Vec<String> {
        let mut seen: HashSet<String> = HashSet::new();
        let mut out = Vec::new();
        for edge in &self.layer.edges {
            if edge.source == did
                && matches!(edge.kind, EdgeKind::Defines | EdgeKind::Contains)
                && seen.insert(edge.target.clone())
            {
                out.push(edge.target.clone());
            }
        }
        out
    }

    /// Pass II: OVERRIDES, BFS-walking the now-complete INHERITS chain.
    fn overrides_pass(&mut self, file: &str, symbols: &[SymbolDto]) {
        for symbol in symbols {
            if symbol.kind != SymbolKindDto::Class {
                continue;
            }
            let class_name = symbol
                .qualified_name
                .clone()
                .unwrap_or_else(|| symbol.name.clone());
            let Some(did) = self.find_symbol_in_file(file, &class_name) else {
                continue;
            };

            let child_methods: Vec<&SymbolDto> = symbols
                .iter()
                .filter(|s| {
                    matches!(s.kind, SymbolKindDto::Method | SymbolKindDto::Constructor)
                        && s.container_name.as_deref() == Some(symbol.name.as_str())
                })
                .collect();
            if child_methods.is_empty() {
                continue;
            }

            // Collect ancestor methods by name across the INHERITS chain.
            let mut ancestor_methods: HashMap<String, String> = HashMap::new();
            let mut visited: HashSet<String> = HashSet::from([did.clone()]);
            let mut queue: VecDeque<String> = VecDeque::from([did.clone()]);
            while let Some(current) = queue.pop_front() {
                let parents: Vec<String> = self
                    .layer
                    .edges
                    .iter()
                    .filter(|e| e.source == current && e.kind == EdgeKind::Inherits)
                    .map(|e| e.target.clone())
                    .collect();
                for parent_did in parents {
                    if visited.insert(parent_did.clone()) {
                        queue.push_back(parent_did.clone());
                    }
                    for child in self.children_of(&parent_did) {
                        if let Some(node) = self.layer.nodes.get(&child) {
                            if (node.kind == "method" || node.kind == "constructor")
                                && !ancestor_methods.contains_key(&node.name)
                            {
                                ancestor_methods.insert(node.name.clone(), child.clone());
                            }
                        }
                    }
                }
            }

            for method in child_methods {
                if let Some(parent_method_did) = ancestor_methods.get(&method.name) {
                    if let Some(method_did) = &method.durable_id {
                        let parent = parent_method_did.clone();
                        self.try_add_edge(
                            method_did,
                            &parent,
                            EdgeKind::Overrides,
                            None,
                            None,
                            None,
                        );
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn node(kind: &str, file: &str) -> NodeData {
        NodeData {
            name: "n".into(),
            qualified_name: "n".into(),
            kind: kind.into(),
            file: file.into(),
            range: module_range(),
            name_range: None,
            content_hash: None,
            content_hashes: BTreeMap::new(),
            external: false,
            package: None,
        }
    }

    fn ref_edge(src: &str, tgt: &str) -> Edge {
        Edge {
            source: src.into(),
            target: tgt.into(),
            kind: EdgeKind::References,
            role: Some("read".into()),
            file: Some("a.py".into()),
            range: Some((1, 1, 1, 2)),
        }
    }

    #[test]
    fn empty_layer_has_no_nodes_or_edges() {
        let layer = CodeLayer::new();
        assert!(layer.is_empty());
        assert!(layer.nodes.is_empty());
        assert!(layer.edges.is_empty());
        assert!(layer.reverse_deps.is_empty());
    }

    #[test]
    fn adding_node_then_edge_updates_reverse_deps_and_removal_prunes() {
        let mut layer = CodeLayer::new();
        layer.upsert_node("A".into(), node("function", "a.py"));
        layer.upsert_node("B".into(), node("function", "a.py"));
        let e = ref_edge("A", "B");
        assert!(layer.add_edge(e.clone()));
        assert_eq!(
            layer.reverse_deps.get("B").map(|s| s.contains("A")),
            Some(true),
            "reverse_deps records target B ← source A"
        );
        assert!(layer.remove_edge(&e));
        assert!(
            layer.reverse_deps.get("B").is_none(),
            "removing the only dependency edge prunes the reverse-dep entry"
        );
    }

    #[test]
    fn module_synthetic_id_matches_python() {
        // Mirrors graph/identity.py:58 — make_module_durable_id.
        assert_eq!(make_module_durable_id("main.py"), "<module>main.py");
        assert_eq!(make_module_durable_id("pkg/mod.py"), "<module>pkg/mod.py");
    }

    #[test]
    fn file_stem_drops_dir_and_extension() {
        assert_eq!(file_stem("main.py"), "main");
        assert_eq!(file_stem("pkg/mod.py"), "mod");
    }

    // ── Producer integration tests over a real reconciled state ──────────────

    use std::io::Write;

    /// Build a `TyProjectState` over a temp dir with the given files and a
    /// populated identity registry (so the producer can attach durable ids).
    fn reconciled_state(files: &[(&str, &str)]) -> (tempfile::TempDir, TyProjectState) {
        use ruff_python_ast::name::Name;
        use ty_project::{ProjectDatabase, ProjectMetadata};

        use crate::content::ContentStore;
        use crate::hash::HashPolicy;
        use crate::overlay::OverlaySystem;

        let dir = tempfile::tempdir().unwrap();
        let mut toml = std::fs::File::create(dir.path().join("pyproject.toml")).unwrap();
        toml.write_all(b"[project]\nname = \"test\"\nversion = \"0.1.0\"\n")
            .unwrap();
        for (rel, content) in files {
            let path = dir.path().join(rel);
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).unwrap();
            }
            std::fs::File::create(&path)
                .unwrap()
                .write_all(content.as_bytes())
                .unwrap();
        }

        let root = ruff_db::system::SystemPathBuf::from_path_buf(
            dir.path().canonicalize().unwrap().to_path_buf(),
        )
        .unwrap();
        let system = OverlaySystem::live(root.clone(), ContentStore::new().capture());
        let metadata = ProjectMetadata::new(Name::new("test"), root.clone());
        let db = ProjectDatabase::use_defaults(metadata, system);
        db.check();

        let mut state = TyProjectState {
            db,
            root,
            registry: None,
            hash_policy: HashPolicy::default(),
            hash_policies: std::collections::HashMap::new(),
            default_hash_profile: "structure".to_string(),
            authored: None,
        };
        // Reconcile identity so every entity has a durable id.
        let entities = crate::entity::extract_entities(&state);
        let mut registry = crate::identity::IdentityRegistry::default();
        crate::identity::reconcile(&mut registry, &entities, crate::content::Revision(1));
        state.registry = Some(registry);
        (dir, state)
    }

    fn edges_of_kind<'a>(layer: &'a CodeLayer, kind: EdgeKind) -> Vec<&'a Edge> {
        layer.edges.iter().filter(|e| e.kind == kind).collect()
    }

    #[test]
    fn producer_two_class_override_structure() {
        let src = "\
class Base:
    def save(self):
        return 1

class User(Base):
    def save(self):
        return 2
";
        let (_dir, state) = reconciled_state(&[("models.py", src)]);
        let empty = CodeLayer::new();
        let (layer, delta) = produce_code_delta(&state, &empty, 1, true, None);

        // Nodes: module + Base + Base.save + User + User.save (+ external object stub).
        let names: Vec<&str> = layer.nodes.values().map(|n| n.name.as_str()).collect();
        assert!(names.contains(&"Base"));
        assert!(names.contains(&"User"));
        assert_eq!(names.iter().filter(|n| **n == "save").count(), 2);
        assert!(layer.nodes.values().any(|n| n.kind == "module"));

        // Exactly one Inherits User→Base (plus possibly Base→external object).
        let inherits = edges_of_kind(&layer, EdgeKind::Inherits);
        let user_did = layer
            .nodes
            .iter()
            .find(|(_, n)| n.name == "User")
            .map(|(id, _)| id.clone())
            .unwrap();
        let base_did = layer
            .nodes
            .iter()
            .find(|(_, n)| n.name == "Base")
            .map(|(id, _)| id.clone())
            .unwrap();
        assert!(
            inherits
                .iter()
                .any(|e| e.source == user_did && e.target == base_did),
            "expected User inherits Base"
        );

        // Exactly one Overrides edge: User.save → Base.save.
        let overrides = edges_of_kind(&layer, EdgeKind::Overrides);
        assert_eq!(overrides.len(), 1, "one override edge expected");

        // Containment: two CONTAINS (class→method) and one DEFINES per top-level.
        let contains = edges_of_kind(&layer, EdgeKind::Contains);
        assert_eq!(contains.len(), 2, "Base→save and User→save");

        // The cold-start delta carries every node and edge with rescan = true.
        assert!(delta.rescan);
        assert_eq!(delta.nodes_upserted.len(), layer.nodes.len());
        assert_eq!(delta.edges_added.len(), layer.edges.len());

        // reverse_deps records Base ← User (an inheritance dependency).
        assert!(layer
            .reverse_deps
            .get(&base_did)
            .map_or(false, |s| s.contains(&user_did)));
    }

    #[test]
    fn producer_no_over_fire_on_identical_reproduce() {
        let src = "class A:\n    def m(self):\n        return 1\n";
        let (_dir, state) = reconciled_state(&[("m.py", src)]);
        let empty = CodeLayer::new();
        let (layer1, _d1) = produce_code_delta(&state, &empty, 1, true, None);
        // Re-produce against the just-built layer: no source changed → empty delta.
        let (_layer2, d2) = produce_code_delta(&state, &layer1, 2, false, None);
        assert!(d2.nodes_upserted.is_empty(), "no node re-emitted: {:?}", d2.nodes_upserted);
        assert!(d2.nodes_removed.is_empty());
        assert!(d2.nodes_moved.is_empty());
        assert!(d2.edges_added.is_empty(), "no edges added: {:?}", d2.edges_added);
        assert!(d2.edges_removed.is_empty());
    }
}
