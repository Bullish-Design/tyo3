# Gate 3N Recovery Analysis

> **Date:** 2026-06-07  
> **Source branch:** `gate1-content-store` (commits `d9c2179`, `7b59c68`, `ef6c1cf`)  
> **Target branch:** Current `HEAD` (post Gate-8 completion)  
> **Purpose:** Detailed analysis and recommendation for every file that exists on
> `gate1-content-store` but not on the current branch — what they are, whether they
> align with the project's current direction, whether importing them would cause
> problems, and what to do with each.

---

## Summary

Six files on `gate1-content-store` do not exist on `HEAD`. They all belong to
**Gate 3N** — a proposal to re-home code-layer delta production from Python into
the native Rust commit, running under the single write lock. The implementation
was coded but **never compiled, never tested**. The current `HEAD` took a
different path: it left the code-layer delta production in Python and
implemented Gates 5–8 (derived layers, authored layers, read surface,
subscription bus) directly on top of the existing architecture.

The foundational design documents (REFINED_SPEC, REFINED_ARCHITECTURE,
REFINED_CONCEPT, CONFIG_SCHEMA) are **byte-identical** between the two branches,
confirming that Gate 3N was an *implementation strategy choice*, not a spec-level
divergence.

Additionally, five files exist on both branches but differ in content (the
GATE_5–8 guides and README). Their divergence is minor — the `gate1-content-store`
versions reference the native `CodeDelta` applier; `HEAD` versions reference
file-level reverse-dep indices instead.

---

## File-by-file analysis

### 1. `.scratch/projects/13-refined-concept/GATE_3N_NATIVE_CODE_DELTA_GUIDE.md`

**What it is:** The full implementation guide (615 lines, 8 steps) for moving
code-layer delta production into the native Rust commit. Covers the contract,
wire format, `CodeLayer` data model, node materialisation, containment edges,
reference/import resolution, two-pass inheritance, incremental production,
cutover, snapshot integration, and cleanup.

**Status on current branch:** Does not exist. No equivalent document in `HEAD`.

**Alignment with current direction:** **Low.** The current codebase has fully
implemented Gates 5–8 without this migration. The code graph is built and
maintained in Python via the read surface, and it works. Gate 3N was a
*hardening/performance* gate, not a feature gate — it does not change observable
behaviour. The spec itself (§3.3.1) requires the code layer to update under the
write lock; the current Python implementation achieves this through the
revision-gated apply pattern (the same revision gate Gate 3N Step 6 planned).
The spec requirement is met, just with a different boundary.

**Would it cause problems to import?** No direct problems — it's a design
document. But it would create confusion if present in the directory without any
corresponding implementation, especially since the README on `HEAD` already
removed its reference. It describes an architecture that diverges from the
current codebase.

**Recommendation:** **Do not import.** Keep it accessible only via `git show
gate1-content-store:...` if ever needed for reference. The architecture choice
it describes was considered and deferred — it may be useful as a future
performance optimisation blueprint, but keeping it in the working tree would
imply it's an active or pending gate, which it is not.

---

### 2. `.scratch/projects/13-refined-concept/GATE_3N_QUICK_IMPLEMENTATION_REPORT.md`

**What it is:** A handoff report (242 lines) documenting what was implemented
vs. deferred in a time-boxed coding pass against the guide above. Lists: Steps
0–4 partial implementation, Step 6 partial cutover, a 9-item risk register, and
a validation order. Explicitly states "nothing compiled, nothing tested."

**Status on current branch:** Does not exist.

**Alignment with current direction:** **None.** The report describes work that
was intentionally abandoned. The risk register (uncompiled code,
`qualified_name` format mismatches, inheritance-cursor position bugs,
`content_hash` decimal vs. hex, etc.) is only relevant to someone resuming that
specific implementation effort.

**Would it cause problems to import?** As a status document for abandoned work,
it would be misleading to have in the working tree. It also references a
non-existent codebase state (`code_layer.rs`, `code_delta.rs`, the Gate 3N
Entity fields).

**Recommendation:** **Do not import.** Historical interest only. The risk
register may be useful if Gate 3N is ever revisited as a performance
optimisation, but it belongs in git history, not the working directory.

---

### 3. `rust/src/code_layer.rs`

**What it is:** The authoritative native `CodeLayer` (308 lines) — the Rust
data model for the code graph's structural state. Contains:
- `NodeData` struct (durable_id, kind, qualified_name, file, range, content_hash)
- `Edge` struct with `Ord` for deterministic emission
- `CodeLayer` struct with `nodes: HashMap`, `edges: HashSet`,
  `reverse_deps: HashMap` — the authority behind the Python rustworkx replica
- `full_build()` — produces a `rescan` delta covering every node and edge
- `incremental_from_full()` — diffs a fresh full-set against the current layer
  to produce a minimal incremental delta
- `full_delta()` — serialises the current layer as a `rescan` delta (cold
  start/snapshot path)
- `importers_of()` — inbound reverse-dep lookup for revalidation and Gate 8
  scoping
- `kind_str()` — SymbolKind → lowercase string serialisation

**Status on current branch:** Does not exist. `rust/src/lib.rs` on HEAD has no
`mod code_layer`. `HeadState` on HEAD has `authored: AuthoredStore` where
`gate1-content-store` had `code_layer: CodeLayer`.

**Would it compile?** No. The Gate 3N implementation report states it was never
compiled. Additionally, the surrounding types it depends on have changed on HEAD:
- `Entity` no longer carries `file`, `range`, `selection_range`, or
  `qualified_name` (these were added on the `gate1-content-store` branch
  specifically for the code-layer producer and were removed on HEAD)
- `HeadState` no longer has a `code_layer` field
- `dto/mod.rs` no longer exports `code_delta` types — it exports
  `AuthoredValueDto` and `AuthoredVersionDto` instead
- The `extract_entities` function no longer filters by root path
  (`path_is_under_root` was removed)

**Would it cause problems to import?** **Yes.** Importing this file would:
1. Fail to compile (missing types, changed module structure)
2. Introduce an unreferenced module that diverges from the current architecture
3. Create confusion about whether the native code layer is authoritative or not

**Alignment with current direction:** **Conflicts.** The current architecture
keeps code-layer state in Python (`CodeGraph` backed by rustworkx), with the
native side providing a read surface (document symbols, occurrences,
supertypes) consumed by the Python builder. Introducing a native `CodeLayer`
would create two competing authorities for the same state.

**Recommendation:** **Do not import.** This is dead code for an abandoned
implementation strategy. If Gate 3N is resurrected as a performance
optimisation, start from the guide, not from uncompiled code that targets a
different `Entity` shape and module structure.

---

### 4. `rust/src/dto/code_delta.rs`

**What it is:** The wire contract (66 lines) for the code-layer delta: 
`SymbolNodeDto`, `EdgeDto`, and `CodeDelta` structs with serde
serialisation. The `CodeDelta` carries `revision`, `rescan`, `nodes_upserted`,
`nodes_removed`, `nodes_moved`, `edges_added`, `edges_removed`.

**Status on current branch:** Does not exist. `rust/src/dto/mod.rs` on HEAD has
no `mod code_delta` and no re-export. Instead it has `AuthoredValueDto` and
`AuthoredVersionDto`.

**Would it compile?** In isolation, yes — it only depends on `RangeDto` from
`crate::dto` and serde. But it's unreferenced without `code_layer.rs` to
produce the deltas.

**Would it cause problems to import?** **Moderate.** The types defined here
(`CodeDelta`, `SymbolNodeDto`, `EdgeDto`) have no consumers on HEAD:
- No Python mirror exists in `src/tyo3/models/analysis.py` 
- The `SyncResultDto` on HEAD has no `code_delta` field
- The `apply_code_delta` method does not exist in `CodeGraph`

Orphan DTOs are dead code and a maintenance burden (they imply a contract that
doesn't exist).

**Alignment with current direction:** **None.** Current deltas flow through
`SyncResult` (created/changed/deleted/moved files and ids), not a structural
graph delta.

**Recommendation:** **Do not import.** Same rationale as `code_layer.rs`. If
Gate 3N is revisited, redesign the wire contract against the current `Entity`
shape, not the abandoned one.

---

### 5. `src/tyo3/graph/tests/test_code_delta_apply.py`

**What it is:** Four pure Python unit tests (128 lines) for the
`CodeGraph.apply_code_delta()` method that was added on the `gate1-content-store`
branch. Tests:
- `test_apply_nodes_and_edges_round_trip` — two nodes + containment + reference
- `test_apply_moved_does_not_churn_edges` — moved node changes file/range but
  keeps edges
- `test_remove_delta_edge_matches_full_identity` — removing one parallel edge
  keeps distinct surviving occurrences
- `test_external_stub_classified_by_file_payload` — external stubs with
  `<external>` file payload

**Status on current branch:** Does not exist. `CodeGraph` on HEAD has no
`apply_code_delta` method.

**Would it cause problems to import?** **Yes — immediate test failure.** These
tests import `CodeDelta`, `EdgeDelta`, `SymbolNodeDelta`, and `Range` from
`tyo3.models.analysis` — none of which exist on HEAD. They call
`g.apply_code_delta(delta)` — a method that does not exist. Importing this file
would break the test suite.

**Alignment with current direction:** **None.** Tests for a feature that was
never completed and whose supporting code was removed.

**Recommendation:** **Do not import.** The tests have no target to test.

---

### 6. `src/tyo3/graph/tests/test_native_code_delta.py`

**What it is:** Seven integration tests (97 lines) for the native
`CodeDelta` emission and parity checking:
- `test_commit_emits_code_delta` — asserts every write carries a code_delta
- `test_native_full_matches_codegraph_build` — parity: native full delta vs.
  legacy `CodeGraph.build`
- `test_full_code_delta_is_byte_deterministic` — same content → identical JSON
- `test_incremental_matches_full_after_edit` — incremental replica equals fresh full
- `test_cross_file_reference_added` — cross-file references resolve
- `test_cross_file_inheritance_overrides_parity` — two-pass inheritance parity
- `test_file_deletion_parity` — file deletion leaves consistent graph

**Status on current branch:** Does not exist. These tests call
`s._inner.code_delta_full()` — a PyO3 method that does not exist on HEAD. They
import from `test_incremental_parity` which exists but expects a different
comparison framework.

**Would it cause problems to import?** **Yes — immediate test failure.** Every
test depends on native methods (`code_delta_full()`, `SyncResult.code_delta`)
that do not exist on HEAD. The file would break `pytest` collection.

**Alignment with current direction:** **None.** Tests for an abandoned
architecture.

**Recommendation:** **Do not import.** Same as the applier tests.

---

## Diverged files (exist on both branches, content differs)

These are on the `gate1-content-store` branch but also exist on `HEAD` in
modified form. They are not strictly "missing" but the `gate1-content-store`
versions differ from current.

### `.scratch/projects/13-refined-concept/README.md`

**Differences:** The `gate1-content-store` version has an "Implementation
guides" section listing per-gate documents, including:
```
- GATE_3N_NATIVE_CODE_DELTA_GUIDE.md — re-homes the code-layer delta into the
  native commit so the graph updates in-lock and ordered (§3.3.1/§3.3.3); retires the
  Python write lock and the snapshot identity-priming workaround. Do before Gate 8.
```
The `HEAD` version removed this entire section. The GATE_3N reference is gone.

**Recommendation:** **Keep the HEAD version as-is.** It correctly reflects that
Gate 3N is not in scope. No action needed.

### GATE_5, GATE_6, GATE_7, GATE_8 guides

**Differences:** Minor. The `gate1-content-store` versions are 4–16 lines
shorter and differ in some wording around the reverse-dep index. Specifically:
- `gate1-content-store` Gate 8 references `graph._importers_of` (a
  `CodeDelta`-era API)
- `HEAD` Gate 8 references `graph._file_importers` and
  `graph.transitive_dependents` (the current file-level API)

The changes are small terminology fixes reflecting the absence of the native
`CodeDelta` applier. The `HEAD` versions are the canonical ones.

**Recommendation:** **Keep the HEAD versions.** The `gate1-content-store`
versions are slightly stale drafts. No action needed.

### Code files (rust/src/*.rs, src/tyo3/*.py)

**Differences:** Significant in `entity.rs`, `project.rs`, `dto/mod.rs`,
`dto/sync.rs`, `lib.rs`. These divergences fall into two categories:

1. **Gate 3N additions removed on HEAD:** `Entity.file`, `Entity.range`,
   `Entity.selection_range`, `Entity.qualified_name` fields; `CodeLayer` in
   `HeadState`; `code_delta` in `SyncResultDto`; `mod code_layer` in `lib.rs`.
   All removed because Gate 3N was abandoned.

2. **Gates 5–6 additions absent on gate1-content-store:** `AuthoredStore` in
   `HeadState`; `hash_policies` and `default_hash_profile` in state structs;
   `AuthoredValueDto`/`AuthoredVersionDto` in DTOs; `authored.rs` module. All
   added after the branch split because Gates 5–8 were implemented on later
   branches.

**Recommendation:** **Keep the HEAD versions of all code files.** They
represent the current architecture. The `gate1-content-store` code files are a
detour that was reverted.

---

## Risk assessment

### What we lose by not importing these files

**Nothing.** Gate 3N was a hardening/performance gate, not a feature gate. The
observable behaviour it aimed to produce — the code graph updating atomically
with each commit, revision-ordered — is already achieved through the current
Python-side implementation. The current architecture:

- Produces graph deltas in Python via `_apply_graph_delta` in `session.py`
- Applies them revision-gated (no out-of-order application)
- Uses the read surface (document_symbols, file_occurrences, class_supertypes)
  to build/update the graph
- Has passed all parity tests under the strengthened comparator

Gate 3N would have moved this work into Rust for performance and lock-consistency
reasons, but the correctness property is already met.

### What we risk by importing these files

| Risk | Severity | File(s) |
|------|----------|---------|
| Compilation failure | High | `code_layer.rs` — depends on removed `Entity` fields |
| Test suite breakage | High | Both test files — call non-existent methods |
| Orphan DTOs (dead code) | Medium | `code_delta.rs` — no producer, no consumer |
| Architectural confusion | Medium | `code_layer.rs` — implies competing authority |
| Documentation drift | Low | `GATE_3N_*_GUIDE.md` — describes abandoned architecture |

### Recommendation summary

```
                         Import?   Rationale
────────────────────────────────────────────────────────────────────
GATE_3N_..._GUIDE.md      No       Design doc for deferred work; keep in git history
GATE_3N_..._REPORT.md     No       Status report for abandoned work; keep in git history
rust/src/code_layer.rs    No       Dead code targeting removed types; won't compile
rust/src/dto/code_delta.rs No      Orphan DTO with no producer or consumer
test_code_delta_apply.py  No       Tests for non-existent method; breaks suite
test_native_code_delta.py No       Tests for non-existent FFI; breaks suite
Diverged docs (GATE 5-8)  Keep HEAD versions (already present)
Diverged README           Keep HEAD version (already present; Gate 3N removed)
Diverged code             Keep HEAD versions (already present; current architecture)
```

---

## Path forward

### If Gate 3N should be resurrected

Gate 3N is conceptually sound — moving code-layer delta production into the
native commit is a valid performance optimisation that would:
- Eliminate O(nodes) FFI calls from the graph build path
- Make the write lock the single serialisation point for all state layers
- Provide the `CodeDelta` as a natural input to Gate 8's subscription bus

If resurrected, **do not start from the abandoned code**. Instead:

1. **Design against the current codebase.** The `Entity` struct has changed
   (no `file`/`range`/`selection_range`/`qualified_name` fields). The DTO
   module has changed (`AuthoredValueDto` added). The `HeadState` has changed
   (`authored` instead of `code_layer`).

2. **Use the guide, not the code.** `GATE_3N_NATIVE_CODE_DELTA_GUIDE.md` (in
   git history) is still a valid step-by-step plan. Its 8-step sequencing,
   parity-oracle pattern, and risk notes are structurally sound. Update the
   file paths and type references to match the current codebase.

3. **Re-examine the premise.** The guide's premise that "rustworkx stays in
   Python" may no longer hold if you've moved other structural state to Rust.
   The current architecture already has `authored.rs` as a Rust-native store —
   adding a native `CodeLayer` would be consistent with that pattern.

4. **Port the tests first.** The test files (`test_code_delta_apply.py`,
   `test_native_code_delta.py`) define the contract. Rewrite them against
   current models and make them pass before writing any producer code.

### If Gate 3N should remain deferred

**Do nothing.** The current codebase is complete without it. The six files on
`gate1-content-store` are correctly excluded from `HEAD`. The git history
preserves them permanently via `git show gate1-content-store:<path>`.

### Documentation clean-up

The `README.md` on HEAD already reflects the correct state (no Gate 3N
reference). No documentation changes needed.

---

## Appendix: Recovery commands

To retrieve any of these files from git history without polluting the working
tree:

```bash
# View a file
git show gate1-content-store:.scratch/projects/13-refined-concept/GATE_3N_NATIVE_CODE_DELTA_GUIDE.md

# Save a file to /tmp for inspection
git show gate1-content-store:rust/src/code_layer.rs > /tmp/code_layer.rs

# Checkout all six files (WARNING: will break build/tests)
git checkout gate1-content-store -- \
  .scratch/projects/13-refined-concept/GATE_3N_NATIVE_CODE_DELTA_GUIDE.md \
  .scratch/projects/13-refined-concept/GATE_3N_QUICK_IMPLEMENTATION_REPORT.md \
  rust/src/code_layer.rs \
  rust/src/dto/code_delta.rs \
  src/tyo3/graph/tests/test_code_delta_apply.py \
  src/tyo3/graph/tests/test_native_code_delta.py
```
