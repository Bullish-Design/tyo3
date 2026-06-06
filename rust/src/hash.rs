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
//! # Entity hashing (Gate 2, SPEC §7)
//!
//! `hash_entity` normalises the entity body per a `HashPolicy` before hashing,
//! so cosmetic edits (whitespace, trailing commas, docstrings/comments when
//! excluded) do not change the hash. The resulting hash is the `ContentHash`
//! carried on every `Entity` and used as the secondary reconciliation key.

use serde::{Deserialize, Serialize};

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

/// A normalised form of an entity's definition, ready for hashing.
///
/// Produced by `normalise_entity_source` according to a `HashPolicy`.
/// The normal form is canonical: two entities with identical semantics
/// (same identifiers, literals, structure) produce the same `NormalForm`
/// regardless of cosmetic styling.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NormalForm(pub String);

/// Policy controlling which source elements are excluded from an entity's
/// normal-form hash. Default values follow SPEC §7: whitespace and trailing
/// commas are ignored; docstrings and comments are excluded (on the assumption
/// they are documentation, not behaviour).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HashPolicy {
    /// Collapse whitespace variations (default true). When true, runs of
    /// whitespace are collapsed to a single space and leading/trailing
    /// whitespace is stripped from each normalised line.
    pub ignore_whitespace: bool,
    /// Normalise trailing commas (default true). When true, a trailing comma
    /// at the end of a parameter/argument list is dropped from the normal form.
    pub ignore_trailing_comma: bool,
    /// Include docstrings in the hash (default false). When false, the
    /// opening line of a function/class body that is a bare string literal
    /// is stripped.
    pub include_docstrings: bool,
    /// Include comments in the hash (default false). When false, lines
    /// starting with optional whitespace then `#` are stripped.
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

/// Normalise the source text of a single entity (function, class, method,
/// module-level constant, etc.) according to `policy`.
///
/// Normalisation strips cosmetic differences: whitespace, trailing commas,
/// comments, and docstrings when the policy excludes them. Identifiers,
/// literals, and structural tokens are always preserved exactly as written.
pub fn normalise_entity_source(source: &str, policy: &HashPolicy) -> NormalForm {
    let mut out = String::with_capacity(source.len());

    for line in source.lines() {
        let trimmed = if policy.ignore_whitespace {
            line.trim()
        } else {
            line
        };

        // Skip comment-only lines when comments are excluded.
        if !policy.include_comments && (trimmed.starts_with('#') || trimmed.is_empty()) {
            continue;
        }

        // Skip docstring lines (first statement in a body that is a bare
        // string literal). We use a simple heuristic: any line that is
        // solely a string literal (triple-quoted or single-quoted) on its
        // own is treated as a docstring and skipped when excluded.
        if !policy.include_docstrings && is_likely_docstring_line(trimmed) {
            continue;
        }

        let mut line_out = if policy.ignore_whitespace {
            // Collapse internal whitespace to single spaces.
            collapse_whitespace(trimmed)
        } else {
            trimmed.to_string()
        };

        // Normalise trailing comma.
        if policy.ignore_trailing_comma {
            line_out = strip_trailing_comma(&line_out);
        }

        if !line_out.is_empty() || !policy.ignore_whitespace {
            out.push_str(&line_out);
            out.push('\n');
        }
    }

    // Trim trailing newline for canonical form.
    while out.ends_with('\n') {
        out.pop();
    }

    NormalForm(out)
}

/// Hash a normalised entity definition.
///
/// The `NormalForm` is expected to have been produced by
/// `normalise_entity_source` (or equivalent canonical rendering of the
/// entity's AST). Two entities with identical semantics produce identical
/// `NormalForm` and therefore identical `ContentHash`.
pub fn hash_entity(normalised: &NormalForm) -> ContentHash {
    hash_bytes(normalised.0.as_bytes())
}

// ── Normalisation helpers ───────────────────────────────────────────────

/// Collapse runs of whitespace to a single space.
fn collapse_whitespace(s: &str) -> String {
    let mut result = String::with_capacity(s.len());
    let mut in_whitespace = false;
    for ch in s.chars() {
        if ch.is_whitespace() {
            if !in_whitespace {
                result.push(' ');
                in_whitespace = true;
            }
        } else {
            result.push(ch);
            in_whitespace = false;
        }
    }
    result
}

/// Heuristic: does this trimmed line look like it is solely a docstring?
///
/// Matches lines that are bare string literals, including triple-quoted
/// opening/closing lines and single-quoted string lines.
fn is_likely_docstring_line(trimmed: &str) -> bool {
    // Triple-quoted docstrings.
    if trimmed.starts_with("\"\"\"") || trimmed.starts_with("'''") {
        return true;
    }
    // Single-line string literal: "..." or '...'
    if (trimmed.starts_with('"') && trimmed.ends_with('"') && trimmed.len() > 1)
        || (trimmed.starts_with('\'') && trimmed.ends_with('\'') && trimmed.len() > 1)
    {
        return true;
    }
    false
}

/// Strip trailing commas from a line. Removes commas that appear immediately
/// before `)`, `]`, `}`, or `:` (the common closing tokens after a parameter/
/// argument list). Handles both `foo(a, b,)` and `foo(a, b,):` patterns.
fn strip_trailing_comma(line: &str) -> String {
    // Strip trailing-commas before common closers: ), ], }, :
    // Pattern: `,)"` → `)"`, `,:` → `:`
    let mut result = line.to_string();
    for pat in &[",)", ",]", ",}", ",:"] {
        result = result.replace(pat, &pat[1..]);
    }
    result
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

    // ── Entity hashing tests (Gate 2 Step 1) ───────────────────────────

    fn entity_hash(src: &str) -> ContentHash {
        let nf = normalise_entity_source(src, &HashPolicy::default());
        hash_entity(&nf)
    }

    #[test]
    fn reformatting_whitespace_same_hash() {
        // Different whitespace → same semantic content → same hash.
        let a = "def foo():\n    x = 1\n    return x\n";
        let b = "def foo():\n  x = 1\n  return x\n";
        assert_eq!(entity_hash(a), entity_hash(b));
    }

    #[test]
    fn extra_blank_lines_same_hash() {
        // Extra blank lines within the entity are normalised away.
        let a = "def foo():\n    x = 1\n\n    return x\n";
        let b = "def foo():\n    x = 1\n    return x\n";
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
    fn identical_functions_same_hash() {
        // Two textually-identical functions → same hash.
        let a = "def add(a, b):\n    return a + b\n";
        let b = "def add(a, b):\n    return a + b\n";
        assert_eq!(entity_hash(a), entity_hash(b));
    }

    #[test]
    fn trailing_comma_normalised() {
        // Trailing comma in a parameter list is normalised away.
        let a = "def foo(a, b,):\n    pass\n";
        let b = "def foo(a, b):\n    pass\n";
        assert_eq!(entity_hash(a), entity_hash(b));
    }

    #[test]
    fn docstring_excluded_by_default() {
        // With default policy (include_docstrings=false), docstring edit
        // does not change hash.
        let a = "def foo():\n    \"\"\"Old doc.\"\"\"\n    pass\n";
        let b = "def foo():\n    \"\"\"New doc.\"\"\"\n    pass\n";
        assert_eq!(entity_hash(a), entity_hash(b));
    }

    #[test]
    fn docstring_included_when_policy_says_so() {
        let policy = HashPolicy { include_docstrings: true, ..Default::default() };
        let a = normalise_entity_source("def foo():\n    \"\"\"Old.\"\"\"\n    pass\n", &policy);
        let b = normalise_entity_source("def foo():\n    \"\"\"New.\"\"\"\n    pass\n", &policy);
        assert_ne!(hash_entity(&a), hash_entity(&b));
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

        let a = normalise_entity_source("def foo():\n    \"\"\"Old.\"\"\"\n    pass\n", &policy);
        let b = normalise_entity_source("def foo():\n    \"\"\"New.\"\"\"\n    pass\n", &policy);

        assert!(policy.include_docstrings);
        assert_ne!(hash_entity(&a), hash_entity(&b));
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
