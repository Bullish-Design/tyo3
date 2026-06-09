//! Content hashing for the per-revision content store and entity identity.
//!
//! A 128-bit content hash, computed once and carried on every `Document::Text`.
//! Hashing is done at byte level via xxh3_128 — a fast, stable non-cryptographic
//! hash with good distribution properties.
//!
//! # Choice of hasher
//!
//! We use `xxhash-rust` xxh3_128 (the 128-bit variant of xxHash's XXH3
//! algorithm, which is the fastest hash in the xxHash family and does not
//! require hardware SIMD).  xxHash is widely adopted, has a stable output
//! format, and its 128-bit output fits neatly into a `u128`.  For our purposes
//! (content identity), collision probability is negligible: 2⁻¹²⁸ is
//! cryptographically microscopic, and we are hashing source files measured in
//! KiB, not exhaustive adversarial input streams.
//!
//! # Entity hashing (Gate 2, SPEC §7; AST-canonical — V2 Phase 10)
//!
//! `hash_entity` hashes a *canonical rendering of the entity's AST subtree*
//! produced per a `HashPolicy`, so the content hash reflects **meaning, not
//! formatting**: whitespace outside literals and trailing-comma style are
//! insignificant, while literal *content* (including the exact text inside
//! string literals) is significant. Comments are never part of the AST, so
//! they are always excluded; docstrings are excluded/included per policy and
//! identified *positionally* (the first string-statement of a def/class/module
//! body), never by line-shape pattern matching. The resulting hash is the
//! `ContentHash` carried on every `Entity` and on the graph node DTO; it is the
//! secondary reconciliation key and the derived-cache key (Phase 8).
//!
//! ## Load-bearing invariant: container-subsumes-members
//!
//! A container's canonical rendering **includes its members' bodies** — the
//! renderer recurses into nested `def`/`class` suites. This is **not** a
//! convenience: V2's `affected`-set coverage of inference-flow dependencies
//! (`w = make_widget(); w.draw()`, which emits *no* `render → draw` edge) holds
//! *only* because editing a method body also moves the enclosing class's
//! `content_hash`, and the class is reachable by the named reference chain.
//! See Concept V2 §5.2–§5.3. **Never hash members independently of their
//! container** for "finer cache keys" — if per-method keys are ever wanted,
//! do it in the derived layer's key (Phase 8 `key_locality`), not here, or you
//! silently reintroduce a §5.4 no-miss regression. Guarded by
//! `class_hash_changes_when_method_body_changes` (this module) and
//! `test_inference_flow_coverage.py::test_container_hash_subsumes_member_bodies`.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use ruff_python_ast::visitor::source_order::{self, SourceOrderVisitor, TraversalSignal};
use ruff_python_ast::{
    AnyNodeRef, BytesLiteral, Expr, Identifier, InterpolatedStringElement, Stmt, StringLiteral,
};
use ruff_text_size::{Ranged, TextRange};

use crate::config::HashProfileCfg;

/// A 128-bit content hash.
///
/// Derived properties (`Ord`, `Hash`, etc.) exist so `ContentHash` can be used
/// as a key in collections, but no one currently *needs* ordering — it is here
/// because it costs nothing and prevents surprises.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct ContentHash(pub u128);

/// Hash an arbitrary byte slice, returning a 128-bit hash.
///
/// Uses xxh3_128: fast, deterministic, and stable across process runs.
pub fn hash_bytes(bytes: &[u8]) -> ContentHash {
    ContentHash(xxhash_rust::xxh3::xxh3_128(bytes))
}

/// Hash the UTF-8 bytes of a string.
///
/// This is a convenience wrapper around `hash_bytes`; the hash is of the raw
/// bytes, not the abstract text — it reflects the exact on-disk / in-memory
/// representation.
pub fn hash_text(text: &str) -> ContentHash {
    hash_bytes(text.as_bytes())
}

// ── Entity-level AST-normalised hashing (Gate 2, SPEC §7) ───────────────

/// A canonical rendering of an entity's AST subtree, ready for hashing.
///
/// Produced by `render_stmt` / `render_module` according to a `HashPolicy`.
/// The normal form is canonical: two entities with identical *meaning* (same
/// identifiers, literal content, structure, signatures, annotations,
/// decorators, bases) produce the same `NormalForm` regardless of formatting.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NormalForm(pub String);

/// Policy controlling which source elements are excluded from an entity's
/// AST-canonical hash (SPEC §7, V2 Phase 10).
///
/// Note on AST-canonical semantics: whitespace and trailing commas are *never*
/// part of the AST, so `ignore_whitespace` / `ignore_trailing_comma` are
/// structurally always honoured (they exist for config-mapping compatibility
/// and document intent). Comments are likewise not represented in the AST, so
/// `include_comments` cannot re-introduce them — comments are always excluded.
/// Only `include_docstrings` actively changes the rendering.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HashPolicy {
    /// Whitespace is insensitive (default true). AST-canonical rendering never
    /// carries whitespace, so this is structurally always true; retained for
    /// config-profile mapping and intent.
    pub ignore_whitespace: bool,
    /// Trailing-comma style is insensitive (default true). The AST does not
    /// represent trailing commas, so this is structurally always true.
    pub ignore_trailing_comma: bool,
    /// Include docstrings in the hash (default false). When false, the first
    /// statement of a def/class/module body that is a bare string literal (the
    /// *docstring position*) is omitted from the rendering. A string statement
    /// in any other position is always significant.
    pub include_docstrings: bool,
    /// Include comments in the hash (default false). Comments are not part of
    /// the AST, so they are always excluded regardless of this flag; retained
    /// for config-profile mapping.
    pub include_comments: bool,
}

impl Default for HashPolicy {
    fn default() -> Self {
        Self {
            ignore_whitespace: true,
            ignore_trailing_comma: true,
            include_docstrings: false,
            include_comments: false,
        }
    }
}

impl From<&HashProfileCfg> for HashPolicy {
    fn from(profile: &HashProfileCfg) -> Self {
        Self {
            ignore_whitespace: profile.whitespace_insensitive,
            ignore_trailing_comma: profile.normalize_trailing_commas,
            include_docstrings: profile.include_docstrings,
            include_comments: profile.include_comments,
        }
    }
}

/// Hash a canonical entity rendering.
///
/// The `NormalForm` is expected to have been produced by `render_stmt` /
/// `render_module` (a canonical rendering of the entity's AST subtree). Two
/// entities with identical meaning produce identical `NormalForm` and therefore
/// identical `ContentHash`.
pub fn hash_entity(normalised: &NormalForm) -> ContentHash {
    hash_bytes(normalised.0.as_bytes())
}

// ── AST-canonical rendering ──────────────────────────────────────────────

/// Build an index from each statement's source range to its node, descending
/// into every nested suite (so methods inside a class are indexed too).
///
/// ty's `document_symbols` sets `full_range == Stmt::range()` for
/// def/class/assign/import symbols, so an entity's `full_range` looks up its
/// defining statement here by exact range equality.
pub fn index_statements(body: &[Stmt]) -> HashMap<TextRange, &Stmt> {
    let mut indexer = StmtIndexer { map: HashMap::new() };
    for stmt in body {
        indexer.visit_stmt(stmt);
    }
    indexer.map
}

/// Produce the canonical `NormalForm` for the entity whose `full_range` matches
/// a statement in `index`. Falls back to the raw source slice only when no
/// statement matches (should not happen for real def/class/assign/import
/// symbols) so we never panic on an unexpected symbol shape.
pub fn entity_normal_form(
    index: &HashMap<TextRange, &Stmt>,
    full_range: TextRange,
    fallback_source: &str,
    policy: &HashPolicy,
) -> NormalForm {
    if let Some(&stmt) = index.get(&full_range) {
        render_stmt(stmt, policy)
    } else {
        let start = full_range.start().to_usize().min(fallback_source.len());
        let end = full_range.end().to_usize().min(fallback_source.len());
        NormalForm(fallback_source[start..end].to_string())
    }
}

/// Render a single statement (and, recursively, any members it contains) to a
/// canonical token stream. A `class`'s rendering includes its methods' bodies
/// — the container-subsumes-members invariant (see the module docs).
pub fn render_stmt(stmt: &Stmt, policy: &HashPolicy) -> NormalForm {
    let mut renderer = CanonicalRenderer::new(policy);
    renderer.visit_stmt(stmt);
    NormalForm(renderer.out)
}

/// Field/record separator between emitted tokens. Keeps adjacent tokens from
/// merging (e.g. two consecutive identifiers) so the rendering is unambiguous.
const SEP: char = '\u{1f}';

/// Collects every statement (recursively, into nested suites) keyed by range.
struct StmtIndexer<'a> {
    map: HashMap<TextRange, &'a Stmt>,
}

impl<'a> SourceOrderVisitor<'a> for StmtIndexer<'a> {
    fn visit_stmt(&mut self, stmt: &'a Stmt) {
        self.map.insert(stmt.range(), stmt);
        source_order::walk_stmt(self, stmt);
    }
}

/// Walks an AST subtree in source order, emitting a canonical token stream.
///
/// - `enter_node` emits one tag per node *kind* — the structural skeleton over
///   every node type (statements, expressions, parameters, patterns, …).
/// - the `visit_*` overrides add scalar *leaf data* the skeleton omits:
///   identifiers, the exact content of string/bytes/f-string literals, numeric
///   and boolean literal values, and the operators that the source-order
///   traversal does not surface for boolean/comparison expressions.
/// - docstrings are handled positionally in `visit_body` per policy.
struct CanonicalRenderer<'p> {
    out: String,
    policy: &'p HashPolicy,
    /// One-shot flag: the *next* `visit_body` opens a docstring-bearing scope
    /// (a def/class/module body). Set in `visit_stmt` for def/class and at the
    /// module entry; consumed by the next `visit_body`. No other `visit_body`
    /// runs between a def/class and its own body, so nested scopes nest right.
    pending_doc_scope: bool,
}

impl<'p> CanonicalRenderer<'p> {
    fn new(policy: &'p HashPolicy) -> Self {
        Self { out: String::new(), policy, pending_doc_scope: false }
    }

    fn tok(&mut self, s: &str) {
        self.out.push_str(s);
        self.out.push(SEP);
    }

    fn tok_dbg<T: std::fmt::Debug>(&mut self, v: T) {
        use std::fmt::Write;
        let _ = write!(self.out, "{v:?}");
        self.out.push(SEP);
    }
}

/// Is `stmt` a bare string-literal expression statement (a docstring when it is
/// the first statement of a def/class/module body)?
fn is_docstring_stmt(stmt: &Stmt) -> bool {
    matches!(stmt, Stmt::Expr(e) if matches!(&*e.value, Expr::StringLiteral(_)))
}

impl<'a, 'p> SourceOrderVisitor<'a> for CanonicalRenderer<'p> {
    fn enter_node(&mut self, node: AnyNodeRef<'a>) -> TraversalSignal {
        self.tok_dbg(node.kind());
        TraversalSignal::Traverse
    }

    fn visit_stmt(&mut self, stmt: &'a Stmt) {
        // The next body we descend into is a docstring-bearing scope.
        if matches!(stmt, Stmt::FunctionDef(_) | Stmt::ClassDef(_)) {
            self.pending_doc_scope = true;
        }
        // `is_async` is skipped by the source-order traversal — emit it here so
        // `async def` / `async for` / `async with` differ from their sync forms.
        let is_async = match stmt {
            Stmt::FunctionDef(s) => s.is_async,
            Stmt::For(s) => s.is_async,
            Stmt::With(s) => s.is_async,
            _ => false,
        };
        if is_async {
            self.tok("async");
        }
        source_order::walk_stmt(self, stmt);
    }

    fn visit_body(&mut self, body: &'a [Stmt]) {
        let doc_scope = std::mem::replace(&mut self.pending_doc_scope, false);
        let skip_first = doc_scope
            && !self.policy.include_docstrings
            && body.first().is_some_and(is_docstring_stmt);
        let start = if skip_first { 1 } else { 0 };
        // Mark the suite boundary so a nested body is distinguishable from a
        // flat sequence of the same statements.
        self.tok("{");
        for stmt in &body[start..] {
            self.visit_stmt(stmt);
        }
        self.tok("}");
    }

    fn visit_expr(&mut self, expr: &'a Expr) {
        // Emit scalar leaves the structural skeleton + child traversal omit.
        match expr {
            Expr::Name(e) => self.tok(e.id.as_str()),
            Expr::BinOp(e) => self.tok_dbg(e.op),
            Expr::BoolOp(e) => self.tok_dbg(e.op),
            Expr::UnaryOp(e) => self.tok_dbg(e.op),
            Expr::Compare(e) => {
                for op in e.ops.iter() {
                    self.tok_dbg(*op);
                }
            }
            Expr::NumberLiteral(e) => self.tok_dbg(&e.value),
            Expr::BooleanLiteral(e) => self.tok(if e.value { "True" } else { "False" }),
            _ => {}
        }
        source_order::walk_expr(self, expr);
    }

    fn visit_string_literal(&mut self, string_literal: &'a StringLiteral) {
        // Literal *content* is significant: "a  b" != "a b".
        self.tok("str");
        self.tok(&string_literal.value);
        source_order::walk_string_literal(self, string_literal);
    }

    fn visit_bytes_literal(&mut self, bytes_literal: &'a BytesLiteral) {
        self.tok("bytes");
        self.tok_dbg(&bytes_literal.value);
        source_order::walk_bytes_literal(self, bytes_literal);
    }

    fn visit_interpolated_string_element(&mut self, element: &'a InterpolatedStringElement) {
        // The literal chunks of an f-string carry significant text; the
        // interpolated expressions are reached by the default traversal.
        if let InterpolatedStringElement::Literal(literal) = element {
            self.tok("fstr");
            self.tok(&literal.value);
        }
        source_order::walk_interpolated_string_element(self, element);
    }

    fn visit_identifier(&mut self, identifier: &'a Identifier) {
        // Captures def/class names, attribute names, parameter names, import
        // aliases, keyword-argument names, etc. (`Expr::Name` is handled in
        // `visit_expr` because its `id` is not visited as an identifier).
        self.tok(identifier.as_str());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── Whole-file hash tests (Gate 1) ─────────────────────────────────

    #[test]
    fn determinism() {
        // Same input → same hash, every time.
        assert_eq!(hash_text("a"), hash_text("a"));
    }

    #[test]
    fn distinctness() {
        // Different input → (overwhelmingly) different hash.
        assert_ne!(hash_text("a"), hash_text("b"));
    }

    #[test]
    fn stability_across_runs() {
        // Lock in one known value so that a dependency upgrade or platform
        // change that silently changes xxHash output is caught immediately.
        //
        // The test is deliberately a single short string: xxh3_128 has no
        // seed and is defined by the xxHash specification, so the output for
        // a given input is a hard-coded cross-platform constant.
        let got = hash_text("X = 1\n");
        assert_eq!(
            got.0,
            189930075863853235169597849719206825685,
            "xxh3_128('X = 1\\n') changed — check for a broken/upgraded hasher implementation"
        );
    }

    // ── Entity hashing tests (Gate 2; AST-canonical, V2 Phase 10) ───────
    //
    // These drive the AST renderer directly: parse the snippet, render the
    // entity's defining statement (the first top-level statement), and hash.
    // This mirrors the production path, where the entity's `full_range` looks
    // up the same `Stmt` in a parsed module.

    fn entity_hash_with(src: &str, policy: &HashPolicy) -> ContentHash {
        let parsed = ruff_python_parser::parse_module(src).expect("valid python source");
        let body = &parsed.syntax().body;
        let nf = render_stmt(&body[0], policy);
        hash_entity(&nf)
    }

    fn entity_hash(src: &str) -> ContentHash {
        entity_hash_with(src, &HashPolicy::default())
    }

    #[test]
    fn reformatting_whitespace_same_hash() {
        // Different whitespace → same meaning → same hash.
        let a = "def foo():\n    x = 1\n    return x\n";
        let b = "def foo():\n  x = 1\n  return x\n";
        assert_eq!(entity_hash(a), entity_hash(b));
    }

    #[test]
    fn extra_blank_lines_same_hash() {
        // Blank lines are not part of the AST.
        let a = "def foo():\n    x = 1\n\n    return x\n";
        let b = "def foo():\n    x = 1\n    return x\n";
        assert_eq!(entity_hash(a), entity_hash(b));
    }

    #[test]
    fn whitespace_around_operators_same_hash() {
        // Whitespace *outside* literals is insignificant.
        let a = "def foo():\n    return 1+2\n";
        let b = "def foo():\n    return 1  +  2\n";
        assert_eq!(entity_hash(a), entity_hash(b));
    }

    #[test]
    fn rename_changes_hash() {
        let a = "def foo():\n    pass\n";
        let b = "def bar():\n    pass\n";
        assert_ne!(entity_hash(a), entity_hash(b));
    }

    #[test]
    fn literal_change_changes_hash() {
        let a = "x = 1\n";
        let b = "x = 2\n";
        assert_ne!(entity_hash(a), entity_hash(b));
    }

    #[test]
    fn string_literal_content_is_significant() {
        // Whitespace *inside* a string literal is significant — the core
        // behaviour the line heuristic got wrong (V1 §6.3 deviation #8).
        let a = "x = \"a  b\"\n";
        let b = "x = \"a b\"\n";
        assert_ne!(entity_hash(a), entity_hash(b));
    }

    #[test]
    fn comparison_and_bool_operators_are_significant() {
        // The source-order traversal does not surface comparison/boolean
        // operators; the renderer emits them explicitly.
        assert_ne!(entity_hash("y = a < b\n"), entity_hash("y = a > b\n"));
        assert_ne!(entity_hash("y = a and b\n"), entity_hash("y = a or b\n"));
    }

    #[test]
    fn identical_functions_same_hash() {
        let a = "def add(a, b):\n    return a + b\n";
        let b = "def add(a, b):\n    return a + b\n";
        assert_eq!(entity_hash(a), entity_hash(b));
    }

    #[test]
    fn trailing_comma_normalised() {
        // Trailing-comma style is not represented in the AST.
        let a = "def foo(a, b,):\n    pass\n";
        let b = "def foo(a, b):\n    pass\n";
        assert_eq!(entity_hash(a), entity_hash(b));
    }

    #[test]
    fn docstring_excluded_by_default() {
        // Default policy (include_docstrings=false): a docstring edit does not
        // change the hash.
        let a = "def foo():\n    \"\"\"Old doc.\"\"\"\n    pass\n";
        let b = "def foo():\n    \"\"\"New doc.\"\"\"\n    pass\n";
        assert_eq!(entity_hash(a), entity_hash(b));
    }

    #[test]
    fn non_docstring_string_statement_is_significant() {
        // A string statement that is NOT in docstring position must remain
        // significant even when docstrings are excluded.
        let policy = HashPolicy::default();
        let b = "def foo():\n    \"\"\"doc\"\"\"\n    return 1\n";
        let c = "def foo():\n    \"\"\"doc\"\"\"\n    \"side\"\n    return 1\n";
        assert_ne!(entity_hash_with(b, &policy), entity_hash_with(c, &policy));
    }

    #[test]
    fn docstring_included_when_policy_says_so() {
        let policy = HashPolicy { include_docstrings: true, ..Default::default() };
        let a = "def foo():\n    \"\"\"Old.\"\"\"\n    pass\n";
        let b = "def foo():\n    \"\"\"New.\"\"\"\n    pass\n";
        assert_ne!(entity_hash_with(a, &policy), entity_hash_with(b, &policy));
    }

    #[test]
    fn class_hash_changes_when_method_body_changes() {
        // INVARIANT (load-bearing for affected-set coverage; see module docs):
        // a container's hash subsumes its members' bodies, so editing a method
        // body moves the enclosing class's content_hash. Do NOT "optimise" the
        // renderer to hash members independently — Concept V2 §5.2–§5.3.
        let a = "class C:\n    def foo(self):\n        return 1\n";
        let b = "class C:\n    def foo(self):\n        return 2\n";
        assert_ne!(
            entity_hash(a),
            entity_hash(b),
            "class hash must move when a member body changes"
        );
    }

    #[test]
    fn hash_profile_default_maps_to_hash_policy_default() {
        let profile = HashProfileCfg::default();
        let policy = HashPolicy::from(&profile);

        assert_eq!(policy, HashPolicy::default());
    }

    #[test]
    fn hash_profile_include_docstrings_changes_docstring_hash() {
        let profile = HashProfileCfg {
            include_docstrings: true,
            ..Default::default()
        };
        let policy = HashPolicy::from(&profile);

        let a = "def foo():\n    \"\"\"Old.\"\"\"\n    pass\n";
        let b = "def foo():\n    \"\"\"New.\"\"\"\n    pass\n";

        assert!(policy.include_docstrings);
        assert_ne!(entity_hash_with(a, &policy), entity_hash_with(b, &policy));
    }

    #[test]
    fn comments_excluded_by_default() {
        let a = "# comment A\nx = 1\n";
        let b = "# comment B\nx = 1\n";
        assert_eq!(entity_hash(a), entity_hash(b));
    }

    #[test]
    fn gate1_whole_file_hash_still_passes() {
        // Gate-1 whole-file hash tests must still pass — entity hashing is
        // additive, not a replacement.
        assert_eq!(hash_text("a"), hash_text("a"));
        assert_ne!(hash_text("a"), hash_text("b"));
        let got = hash_text("X = 1\n");
        assert_eq!(
            got.0,
            189930075863853235169597849719206825685
        );
    }
}
