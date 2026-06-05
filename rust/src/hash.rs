//! Content hashing for the per-revision content store.
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
//! # Future refinement (Gate 2, SPEC §7)
//!
//! NOTE: Gate 2 (SPEC §7) replaces text hashing with AST-normalised hashing
//! for entity identity; this byte hash is the content-store default and
//! remains correct for whole-file content.

/// A 128-bit content hash.
///
/// Derived properties (`Ord`, `Hash`, etc.) exist so `ContentHash` can be used
/// as a key in collections, but no one currently *needs* ordering — it is here
/// because it costs nothing and prevents surprises.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
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

#[cfg(test)]
mod tests {
    use super::*;

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
}
