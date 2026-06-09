# Symbol/Entity walk refactor — overview

> **Status:** planned (not started). **Created:** 2026-06-08, on a clean
> `v0.2.0` base (spine refactor complete; full gate green).
> **Type:** behaviour-preserving cleanup. **Owner-decision needed:** no — this is
> a self-contained mechanical refactor flagged as a follow-up in PROGRESS §9.14.

---

## 1. Why this exists

Phase 14 drove `cargo clippy --all-targets -- -D warnings` to zero. Six functions
tripped `clippy::too_many_arguments` (limit 7). Five were resolved with narrow
justified `#[allow(...)]`s because their parameters are genuinely distinct
(PyO3 `#[pymethod]` signatures that are the Python-facing API; a DTO assembler).
**Two are different**: the recursive symbol/entity tree-walks thread a *fixed,
immutable analysis context* through every recursion level alongside a small
per-node cursor. For those, a context struct is the right fix — it was deferred
(not skipped) so Phase 14 could stay focused on the acceptance proof.

This doc is the kickoff for doing it properly.

### In scope (exactly two functions)
- `rust/src/convert/symbols.rs::collect_symbols_recursive` — **13 params**
- `rust/src/entity.rs::collect_entities_recursive` — **12 params**

Doing this removes **exactly two** `#[allow(clippy::too_many_arguments)]`:
- `rust/src/convert/symbols.rs:73` (on `collect_symbols_recursive`)
- `rust/src/entity.rs:209` (on `collect_entities_recursive`)

### Explicitly OUT of scope
- `convert_symbol` (`convert/symbols.rs`, 13 params, `#[allow]` at line 33) —
  **stays**. Its arguments are per-symbol distinct fields that map 1:1 onto the
  emitted `SymbolDto` (`name`, `kind`, `deprecated`, `name_range`, `full_range`,
  `container_name`, `qualified_name`, `durable_id`, `content_hash`,
  `content_hashes`). Folding `source`/`line_index`/`file_path` into the shared
  context would drop it 13→11 — still over 7 — while the rest can't be bundled
  without just mirroring the DTO. Not worth it; keep the justified allow.
- The other three allows (`project/head_view.rs:197`, `project/commit.rs:153`,
  `project/snapshot.rs:246`) — unrelated (PyO3 method + DTO assembler). Untouched.
- Any behaviour change. This is a pure signature refactor; the produced
  `SymbolDto`/`Entity` values must be byte-identical.

---

## 2. Current state (exact signatures)

### `collect_symbols_recursive` (convert/symbols.rs)

```rust
pub fn collect_symbols_recursive(
    hierarchical: &ty_ide::HierarchicalSymbols,                                  // context
    id: ty_ide::SymbolId,                                                        // cursor
    info: &ty_ide::SymbolInfo,                                                   // cursor
    source: &str,                                                               // context
    stmt_index: &HashMap<ruff_text_size::TextRange, &ruff_python_ast::Stmt>,    // context
    line_index: &ruff_source_file::LineIndex,                                   // context
    file_path: &str,                                                            // context
    parent_name: Option<&str>,                                                  // cursor
    parent_identity_path: Option<&str>,                                         // cursor
    registry: Option<&IdentityRegistry>,                                        // context
    hash_policies: Option<&HashMap<String, crate::hash::HashPolicy>>,           // context
    default_profile_name: Option<&str>,                                         // context
    symbols: &mut Vec<dto::SymbolDto>,                                          // accumulator
)
```

Grouping:
- **Context (fixed for the whole walk — 8):** `hierarchical`, `source`,
  `stmt_index`, `line_index`, `file_path`, `registry`, `hash_policies`,
  `default_profile_name`.
- **Cursor (changes per node — 4):** `id`, `info`, `parent_name`,
  `parent_identity_path`.
- **Accumulator (threaded `&mut` — 1):** `symbols`.

### `collect_entities_recursive` (entity.rs)

```rust
fn collect_entities_recursive(
    hierarchical: &ty_ide::HierarchicalSymbols,    // context
    id: ty_ide::SymbolId,                          // cursor
    info: &ty_ide::SymbolInfo,                     // cursor
    source_str: &str,                              // context
    stmt_index: &HashMap<TextRange, &Stmt>,        // context
    _line_index: &LineIndex,                       // context — UNUSED (underscore)
    file_path: &str,                               // context
    parent_qualified: Option<&str>,                // cursor
    parent_dotted: Option<&str>,                   // cursor
    policy: &HashPolicy,                           // context
    entities: &mut Vec<Entity>,                    // accumulator
    visited: &mut HashSet<ty_ide::SymbolId>,       // threaded mutable state
)
```

Grouping:
- **Context (fixed — 6, but `_line_index` is dead):** `hierarchical`,
  `source_str`, `stmt_index`, `_line_index`, `file_path`, `policy`.
- **Cursor (per node — 4):** `id`, `info`, `parent_qualified`, `parent_dotted`.
- **Threaded `&mut` (2):** `entities`, `visited`.

> **Incidental win:** `_line_index` is already unused (underscore-prefixed). The
> refactor should **drop it entirely** rather than bundle it — one fewer param
> and one fewer thing for the caller to compute/pass.

### Why two structs, not one shared one
The two contexts overlap but differ materially: the **symbol** walk carries
`registry` + per-profile `hash_policies` + `default_profile_name` (it resolves
durable ids and computes per-profile content hashes); the **entity** walk carries
a single `policy` and no registry. Forcing a shared struct would bloat each call
site with fields it doesn't use. Define **two** small structs.

---

## 3. Proposed design

### Symbol walk

```rust
/// Immutable analysis context for one document-symbol walk. Built once per file
/// by `compute_document_symbols`, then threaded unchanged through the recursion.
struct SymbolWalkCtx<'a> {
    hierarchical: &'a ty_ide::HierarchicalSymbols,
    source: &'a str,
    stmt_index: &'a HashMap<ruff_text_size::TextRange, &'a ruff_python_ast::Stmt>,
    line_index: &'a ruff_source_file::LineIndex,
    file_path: &'a str,
    registry: Option<&'a IdentityRegistry>,
    hash_policies: Option<&'a HashMap<String, crate::hash::HashPolicy>>,
    default_profile_name: Option<&'a str>,
}

pub fn collect_symbols_recursive(
    ctx: &SymbolWalkCtx<'_>,
    id: ty_ide::SymbolId,
    info: &ty_ide::SymbolInfo,
    parent_name: Option<&str>,
    parent_identity_path: Option<&str>,
    symbols: &mut Vec<dto::SymbolDto>,
) // 6 params — under the limit
```

### Entity walk

```rust
/// Immutable analysis context for one entity-extraction walk.
struct EntityWalkCtx<'a> {
    hierarchical: &'a ty_ide::HierarchicalSymbols,
    source: &'a str,
    stmt_index: &'a HashMap<TextRange, &'a Stmt>,
    file_path: &'a str,
    policy: &'a HashPolicy,
    // note: line_index dropped (was unused)
}

fn collect_entities_recursive(
    ctx: &EntityWalkCtx<'_>,
    id: ty_ide::SymbolId,
    info: &ty_ide::SymbolInfo,
    parent_qualified: Option<&str>,
    parent_dotted: Option<&str>,
    entities: &mut Vec<Entity>,
    visited: &mut HashSet<ty_ide::SymbolId>,
) // 7 params — at the limit (ok)
```

> If 7 still feels heavy for the entity walk, `entities`/`visited` are the
> natural pair to fold into a tiny `EntityWalkState<'a>` mutable accumulator
> (`&mut`), dropping it to 6. Optional — 7 already clears the lint.

### Lifetime note (the one real subtlety)
`stmt_index` is a `HashMap<TextRange, &Stmt>` — a map whose **values are
borrows**. The struct field is therefore `&'a HashMap<TextRange, &'a Stmt>`
(or two lifetimes `&'a HashMap<TextRange, &'b Stmt>` if the borrow checker
complains about the elision). Build the struct in the same scope that owns the
parsed module / `stmt_index` (the callers already do — see §4), so all the
borrows share one stack frame and the lifetime is trivially satisfiable. No
`Rc`/clone needed.

---

## 4. Call sites to update

| Function | Call site | Role |
|---|---|---|
| `collect_symbols_recursive` | `convert/symbols.rs:135` | recursive self-call → pass `ctx` |
| | `project/analysis.rs:118` (`compute_document_symbols`) | build `SymbolWalkCtx` once, pass it |
| `collect_entities_recursive` | `entity.rs:185` | recursive self-call → pass `ctx` |
| | `entity.rs:264` | entry loop in `extract_entities` / `extract_entities_for` (`entity.rs:112` / `:135`) → build `EntityWalkCtx` once |

Both entry points (`compute_document_symbols`, `extract_entities*`) already
assemble every context field locally before the walk loop — so the change at the
call site is "construct the struct, then pass `&ctx`" instead of spreading 8
arguments at each call. Inside the recursion the change is purely
`collect_*_recursive(ctx, child_id, &child_info, …)`.

`convert_symbol` is called from `collect_symbols_recursive` (`symbols.rs:112`)
and `compute_workspace_symbols` (`analysis.rs:160`). It is **not** changed; inside
`collect_symbols_recursive` it now reads its `source`/`line_index`/`file_path`
from `ctx` instead of from the (removed) positional params.

---

## 5. Step-by-step plan

1. **Symbol walk.** Add `SymbolWalkCtx<'a>`; rewrite `collect_symbols_recursive`
   to take `&ctx` + the 4 cursor params + `symbols`; update the body to read
   context fields via `ctx.…`; update the recursive call (`symbols.rs:135`) and
   the entry caller (`analysis.rs:118`). Remove the `#[allow]` at `symbols.rs:73`.
2. **Entity walk.** Add `EntityWalkCtx<'a>` (dropping `line_index`); rewrite
   `collect_entities_recursive`; update body + recursive call (`entity.rs:185`) +
   entry loop(s) (`entity.rs:264`, built in `extract_entities`/`_for`). Remove the
   `#[allow]` at `entity.rs:209`. Confirm nothing else passed `line_index` into
   the walk.
3. **Build + gate.** `devenv shell -- build`, then
   `cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`
   (must stay clean with the two allows now **gone**), `cargo test`
   (167/0 expected), and the Python suite incl. `test_final_acceptance.py`.
4. **Parity oracle.** `devenv shell -- parity-oracle` (and the gate suites that
   build graphs) — the produced symbols/entities must be identical, so the
   structural tier must stay green.

Keep it to **two commits** for legible review: one per walk, each self-contained
and behaviour-preserving. Commit message e.g.
`refactor(convert): fold the document-symbol walk context into a struct`.

---

## 6. Acceptance / definition of done

- The two `#[allow(clippy::too_many_arguments)]` at `convert/symbols.rs:73` and
  `entity.rs:209` are **deleted**, and `clippy --all-targets -- -D warnings` is
  still clean.
- `cargo test` green; `pytest -q` green; `test_final_acceptance.py` green; the
  parity oracle's **structural tier** unchanged (proves identical graph output).
- `convert_symbol`'s allow (`symbols.rs:33`) and the three unrelated allows
  remain, each still carrying its justifying comment.
- Net: fewer parameters, the "fixed walk context" vs "per-node cursor"
  distinction is explicit in the types, the dead `line_index` is gone, and no
  observable behaviour changed.

---

## 7. Risks & notes

- **Lifetimes** are the only place this can fight back — specifically the nested
  borrow in `stmt_index`. Build the ctx in the owner's scope (§3 note). If the
  checker is unhappy with one lifetime, give the struct two (`'a` for the map,
  `'b` for the inner `&Stmt`).
- **`pub` surface.** `collect_symbols_recursive` / `convert_symbol` are `pub`
  (crate-internal usage only — not re-exported to Python). Changing
  `collect_symbols_recursive`'s signature is safe; just update the two Rust
  callers. `collect_entities_recursive` is private (`fn`), even simpler.
- **No Python impact.** None of these cross the PyO3 boundary; the `.so` ABI and
  the Python API are unaffected.
- **Don't scope-creep** into `convert_symbol` or the PyO3 method signatures —
  those allows are correct and documented.

---

## 8. References
- PROGRESS §9.14 (where this was flagged as a follow-up): the symbol/entity
  recursive walks were resolved with narrow allows pending this refactor.
- Memory: `[[phase14-acceptance-done]]`.
- Current allows inventory: `grep -rn "too_many_arguments" rust/src`.
