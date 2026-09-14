# IMPLEMENTATION — Semantic-plane cleanup (project 29, historical guide)

The three steps are complete. This file is retained as the execution record;
it is not a current work order. Read [README.md](README.md) for current status
and [Project 31](../31-semantic-state/README.md) for the later semantic-state
decision.

## Ground rules

- **Behaviour-preserving except where stated.** Step 1 changes one observable
  behaviour (it stops leaking external stubs as entities) — that is the bug fix.
  Steps 2 and 3 change nothing observable.
- **Do not change `CodeNodeDto`'s wire shape.** Its fields are in the parity
  oracle's strict structural tier. Everything below is achievable without
  touching it.
- **Do not adopt the v2 document's §7.1 or §8.2–8.3.** They reinstate machinery
  `Project 31` deliberately retired. See DESIGN §1.
- Version control: route through **gitman**. One lane per step.

## Commands

```sh
devenv shell -- check-rust        # fast type-check, ~1s — the Rust edit loop
devenv shell -- clippy            # -D warnings, matches the CI gate
devenv shell -- build             # maturin develop (~17s) — needed to run Python
devenv shell -- test-fast         # the fast Python suite
devenv shell -- tests             # cargo test + full Python suite
devenv shell -- parity-oracle     # the tiered structural/cosmetic oracle
```

Baseline before you start: `devenv shell -- tests` must be green (816 tests).
Record the count; it must not drop.

---

# Step 1 — Explicit entity populations

> Historical starting state: the Python half was **DONE** (landed 2026-09-13,
> initially uncommitted in the working tree). §1.3 (Rust) was the only part
> left at that point. §1.4–1.6 were already applied — read them as a record of
> what was done, not as work to do. Re-run
> §1.2 to confirm you are looking at the fixed state: `code.ids()` must return
> the ULID only.
>
> What landed: `is_entity_node(durable_id, *, external)` in
> `src/tyo3/graph/identity.py`; `is_entity_durable_id` narrowed to "not a module
> id"; the dead `startswith("<external>")` branch removed from
> `file_from_durable_id`; all callers in `layers/code.py`, `layers/derived.py`,
> `layers/authored.py`, `models/diff.py` and `daemon/handlers.py` moved onto the
> node-based check; `tests/test_entity_populations.py` added (12 tests).
> Verified: suite **828 passed**, `parity-oracle` green, `ruff` clean, and the
> tests fail 7-of-12 against the old predicate (not vacuous).

**Goal.** Make the three id populations explicit, and stop classifying them by
guessing at string shapes. Fixes a verified leak.

**Risk.** Low. No wire change. One deliberate behaviour change.

## 1.1 The problem, in one line

`src/tyo3/graph/identity.py:68 is_entity_durable_id` returns `True` for every
external stub, because external stub ids never start with `<external>` — only
their `file` field does. See DESIGN §4.2 for the reproduction.

## 1.2 Reproduce it first

```sh
mkdir -p /tmp/tyo3probe && cd /tmp/tyo3probe
printf 'import json\n\ndef load(text):\n    return json.loads(text)\n' > main.py
cd "$DEVENV_ROOT"
PYTHONPATH=src python -c "
from tyo3.session import TyO3Session
snap = TyO3Session('/tmp/tyo3probe').snapshot()
print(sorted(snap.code.ids()))
"
```

Expected today (the bug):

```
['01M2...', 'stdlib/json/__init__.pyi::', 'stdlib/json/__init__.pyi::loads', 'unknown::<module>']
```

Expected after Step 1: the ULID only.

## 1.3 Rust — centralize the sentinels

All in `rust/src/code_layer.rs`, next to the existing `make_module_durable_id`
(`:33`).

Add:

```rust
/// The `file` value carried by every external stub node. Not a real path.
pub const EXTERNAL_FILE: &str = "<external>";

/// The `qualified_name` carried by every synthetic module node.
pub const MODULE_QUALIFIED_NAME: &str = "<module>";

/// Stable synthetic id for an off-project package's module stub.
pub fn make_external_module_id(package: &str) -> String {
    format!("{}::{}", package, MODULE_QUALIFIED_NAME)
}

/// Stable synthetic id for an off-project reference target.
pub fn make_external_symbol_id(package_or_file: &str, name: &str) -> String {
    format!("{}::{}", package_or_file, name)
}

/// The three populations a `CodeLayer` node can belong to.
///
/// NOTE: an external stub is **not** identifiable from its id alone — its id is
/// an ordinary `"a::b"` string. `NodeData::external` is the only authority.
/// This is the defect Step 1 fixes; do not reintroduce id-shape guessing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Population {
    Entity,
    Module,
    External,
}

pub fn is_module_id(durable_id: &str) -> bool {
    durable_id.starts_with("<module>")
}
```

And on `NodeData`:

```rust
impl NodeData {
    pub fn population(&self, durable_id: &str) -> Population {
        if self.external {
            Population::External
        } else if is_module_id(durable_id) {
            Population::Module
        } else {
            Population::Entity
        }
    }
}
```

Replace the scattered literals with the constants and constructors:

| Site | Today | Becomes |
|---|---|---|
| `code_layer.rs:546` | `node.qualified_name == "<module>"` | `== MODULE_QUALIFIED_NAME` |
| `code_layer.rs:548` | `"<module>".to_string()` | `MODULE_QUALIFIED_NAME.to_string()` |
| `code_layer.rs:679` | `src_node.file != "<external>"` | `src_node.file != EXTERNAL_FILE` |
| `code_layer.rs:804` | `qualified_name: "<module>".to_string()` | `MODULE_QUALIFIED_NAME.to_string()` |
| `code_layer.rs:816` | `"<module>".to_string()` | `MODULE_QUALIFIED_NAME.to_string()` |
| `code_layer.rs:875` | `file: "<external>".to_string()` | `EXTERNAL_FILE.to_string()` |
| `code_layer.rs:1075` | `format!("{}::{}", p, target_name)` | `make_external_symbol_id(p, target_name)` |
| `code_layer.rs:1101` | `format!("{}::<module>", package)` | `make_external_module_id(&package)` |
| `code_layer.rs:1103` | `"<module>"` arg | `MODULE_QUALIFIED_NAME` |
| `commit.rs:944` | `file != "<external>"` | `file != crate::code_layer::EXTERNAL_FILE` |

Check as you go: `devenv shell -- check-rust`.

## 1.4 Python — classify from the node, not the id

In `src/tyo3/graph/identity.py`:

1. **Add** the honest classifier. It takes the one fact only the producer knows:

```python
def is_entity_node(durable_id: str, *, external: bool) -> bool:
    """True when this node is a real code entity.

    *external* comes from the producer (``CodeNodeDto.external`` /
    ``SymbolNode.external``) and is the only reliable discriminator for an
    external stub: stub ids are ordinary ``"package::name"`` strings and carry
    no prefix. Classifying by id shape alone misreads every stub as an entity.
    """
    if external:
        return False
    return not durable_id.startswith("<module>")
```

2. **Narrow** `is_entity_durable_id` rather than deleting it. Keep it for the
   callers that only hold an id, and make its limit explicit in the docstring:

```python
def is_entity_durable_id(durable_id: str) -> bool:
    """True when *durable_id* is not a synthetic **module** id.

    Cannot detect external stubs — their ids carry no prefix. Prefer
    :func:`is_entity_node`, which takes the producer's ``external`` flag.
    """
    return not durable_id.startswith("<module>")
```

   Dropping the ULID length heuristic and the `startswith("<")` fallback is
   deliberate: both were guesses, and the ULID check silently misclassifies the
   compound nested ids `derive_durable_id` builds (`identity.py:55`,
   `f"{durable_id}::{qn}"`).

3. **Fix** `file_from_durable_id` (`identity.py:83`). Its
   `startswith("<external>")` branch is dead code — no id ever has that prefix.
   Delete the branch and say so in the docstring.

## 1.5 Python — move every caller onto the node-based check

| File | Line | Today | Becomes |
|---|---|---|---|
| `layers/code.py` | 59 | `if is_entity_durable_id(did)` | `if is_entity_node(did, external=self._node_index[did]["external"])` |
| `layers/code.py` | 70 | `not is_entity_durable_id(durable_id)` | `not is_entity_node(durable_id, external=n["external"])` |
| `layers/code.py` | 96-97 | id-only set comprehensions | node-based, using each `_node_index` entry |
| `layers/derived.py` | 42 | `not is_entity_durable_id(node.durable_id)` | `not is_entity_node(node.durable_id, external=node.external)` |
| `layers/authored.py` | 56 | same | same |
| `models/diff.py` | 150, 156 | `is_entity_durable_id(node.durable_id)` | `is_entity_node(node.durable_id, external=node.external)` |
| `models/diff.py` | 184 | id-only, on edge endpoints | leave as-is; it has no node. Add a comment naming the limit. |
| `daemon/handlers.py` | 177, 435, 497, 514 | `node.external or not is_entity_durable_id(...)` | `not is_entity_node(..., external=node.external)` |

The `handlers.py` sites were already correct — they paired the predicate with an
explicit `node.external` check. Collapsing them into one call removes the
duplication that made the other call sites look safe when they were not.

Also update the stale docstrings at `layers/code.py:45-46` and `:56`.

## 1.6 Tests

Add `tests/test_entity_populations.py`:

1. **Unit.** `is_entity_node` over the four shapes: ULID, `<module>main.py`,
   `requests::Session` with `external=True`, `unknown::<module>` with
   `external=True`.
2. **Regression (the bug).** A project whose only import is a stdlib module.
   Assert `snap.code.ids()` contains exactly the real entity ids — no
   `<external>`-filed node, no `<module>` node.
3. **`value()` gate.** `snap.code.value(<an external stub id>)` returns `None`.
4. **Layer diff.** A `models.diff` between two revisions of that project reports
   no external stub in created/changed/deleted.

Add to `rust/src/code_layer.rs` tests:

5. `population()` returns `External` for a stub node regardless of id shape, and
   `Module` / `Entity` for the others.

## 1.7 Verify

```sh
devenv shell -- check-rust && devenv shell -- clippy
devenv shell -- build
devenv shell -- test-fast
devenv shell -- parity-oracle     # must stay green — no wire change was made
devenv shell -- tests
```

**Exit condition.** The §1.2 reproduction returns the ULID only *(already true)*;
the full suite is green at **828 or more**; `parity-oracle` is green; no
`"<module>"` or `"<external>"` string literal remains outside `code_layer.rs`
and `graph/identity.py` *(the remaining work — the Rust half, §1.3)*.

---

# Step 2 — De-`Option` the identity registry

**Goal.** `TyProjectState.registry: IdentityRegistry`, default-empty. Remove 25
unwrap sites.

**Risk.** Low, mechanical. Rust only. No Python change, no wire change.

## 2.1 Why it is safe

`Some(&empty_registry)` is behaviourally identical to `None`. Verified at
`rust/src/convert/symbols.rs:94`:

```rust
let anchor = registry.and_then(|r| r.by_path(&identity_path).and_then(|id| r.get(id)));
```

An empty registry makes `by_path` return `None`, so the chain short-circuits the
same way. The `Option` carries no information. (DESIGN §4.1.)

## 2.2 The change

1. `rust/src/project.rs:84` — `pub(crate) registry: IdentityRegistry`.
2. `rust/src/project.rs:162,178` — the two `ReadCloneSource` impls: drop
   `Some(...)` / keep `self.registry.clone()`.
3. `rust/src/project/commit.rs:359` — `registry: None` becomes
   `registry: IdentityRegistry::default()`. **Keep the comment** explaining that
   this state is built before reconcile and must not attach durable ids; the
   empty registry is what enforces that, and it is now the explicit statement of
   intent rather than an `Option` variant.
4. `rust/src/code_layer.rs:1425,1435` — same, in the test helper.
5. Signature changes, from `Option<&IdentityRegistry>` to `&IdentityRegistry`:
   - `rust/src/convert/symbols.rs:84` `collect_symbols_recursive`
   - `rust/src/project/identity_ops.rs` — `locate`, `needs_review`, `orphaned`
     (the `let Some(registry) = … else` at `:28` disappears)
6. Call sites, drop the `Some(&…)` / `.as_ref()`:
   - `rust/src/project/analysis.rs:128`
   - `rust/src/project/snapshot.rs:499,514,527,549`
   - `rust/src/project/methods.rs:137,602,624,689,700,708`
   - `rust/src/project/head_view.rs:355,364,371`

`methods.rs:137` uses `std::mem::take(&mut head.registry)` — that still works;
`IdentityRegistry` derives `Default`.

## 2.3 Watch for

- `methods.rs:602` and `:624` clone the registry into a state and then reassign
  it. With the `Option` gone, `state.registry = registry` is a direct move.
- `identity_ops.rs` currently returns early when the registry is absent. With a
  non-`Option` registry the early return disappears — confirm every caller still
  handles the "id not in registry" case, which is a different and still-real
  condition (`registry.get(&id)` returning `None`).

## 2.4 Verify

```sh
devenv shell -- check-rust && devenv shell -- clippy
devenv shell -- build && devenv shell -- tests && devenv shell -- parity-oracle
```

**Exit condition.** No `Option<IdentityRegistry>` and no
`Option<&IdentityRegistry>` anywhere in `rust/src`. Suite green, count unchanged.

---

# Step 3 — `reverse_deps` derivability invariant

**Goal.** Prove, continuously, that the incrementally maintained `reverse_deps`
index still equals what the canonical edge set implies.

**Risk.** Low. Adds a derivation, an assertion, and tests. Changes no production
behaviour.

## 3.1 Why this matters most

`CodeLayer.reverse_deps` (`code_layer.rs:173`) is maintained edge-by-edge and
never rebuilt (`code_layer.rs:457`: "never rebuilt from scratch — working rule
3"). It is the sole input to `affected_closure_with_deleted` (`:263`), which
produces `affected_ids` (`commit.rs:928`), which drives derived-layer
invalidation.

If the index drifts, `affected_ids` **under-fires** and derived artifacts go
silently stale. Nothing currently catches that: the unit tests at
`code_layer.rs:1317` exercise `add_edge`/`remove_edge` on a hand-built layer, and
`tests/test_affected_closure.py` covers one end-to-end case. Neither checks the
invariant after a real scoped incremental commit.

`remove_edge`'s pruning rule (`:209-223`) is the specific fragility: it drops a
`reverse_deps` entry only when no parallel dependency edge of the same
`(source, target)` remains. Correct as written; quiet to break.

This is the v2 document's Invariant C — the one invariant from that document
worth adopting now.

## 3.2 The derivation

In `rust/src/code_layer.rs`, on `impl CodeLayer`:

```rust
/// Derive `reverse_deps` purely from `edges` — the canonical definition the
/// maintained index must always match (Invariant C).
///
/// The maintained index exists for speed only. It is never independent truth.
pub fn derived_reverse_deps(&self) -> BTreeMap<String, BTreeSet<String>> {
    let mut out: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for edge in &self.edges {
        if edge.kind.is_dependency() {
            out.entry(edge.target.clone())
                .or_default()
                .insert(edge.source.clone());
        }
    }
    out
}

/// Panic if the maintained index has drifted from the canonical edge set.
/// Debug-only: the derivation is O(edges) and must not run on the release
/// commit path.
#[cfg(debug_assertions)]
pub fn assert_reverse_deps_derivable(&self, context: &str) {
    let derived = self.derived_reverse_deps();
    assert_eq!(
        self.reverse_deps, derived,
        "reverse_deps drifted from the canonical edge set ({context})"
    );
}
```

Note `is_dependency` is currently private (`code_layer.rs:63`) — it is in the
same module, so no visibility change is needed.

## 3.3 Wire the assertion

In `produce_code_delta_scoped` (`code_layer.rs:465`), after `build_scoped`:

```rust
let next = builder.layer;
#[cfg(debug_assertions)]
next.assert_reverse_deps_derivable("produce_code_delta_scoped");
```

Do the same in `produce_code_delta` (`:434`) — cheap insurance for the full
build, and it pins the cold path as the reference.

Because the whole test suite runs debug builds, this turns **every existing
test** into a check of the invariant, at zero maintenance cost. That is most of
the value of Step 3.

## 3.4 Rust test — multi-generation scoped production

Extend the `reconciled_state` helper (`code_layer.rs:1390`) to thread a prior
registry and a revision through, so successive generations keep stable ids:

```rust
fn reconciled_state_at(
    files: &[(&str, &str)],
    prior: Option<IdentityRegistry>,
    rev: u64,
) -> (tempfile::TempDir, TyProjectState)
```

Then add a test that walks a file through several generations, running the
**scoped** producer each time with the prior layer and the changed file as
`seed_dirty`, and asserting the invariant after each:

- gen 1: `a.py` defines `Base`; `b.py` defines `User(Base)` and calls `Base.save`
- gen 2: edit `User.save`'s body (edge set unchanged)
- gen 3: remove the `Base.save` call (removes one `References` edge while an
  `Inherits` edge between the same pair remains — **this is the parallel-edge
  pruning case**)
- gen 4: remove the `Base` base class (removes the last dependency edge)
- gen 5: delete `a.py` entirely

After each generation assert `next.reverse_deps == next.derived_reverse_deps()`.
Gen 3 is the one that would catch a `remove_edge` pruning regression; do not drop
it.

## 3.5 Python test — the real commit path

The Rust test exercises the producer directly. Add a black-box test over the
actual commit funnel, in `tests/test_affected_closure.py`:

Build a small project, run a sequence of `session.edit()` calls, and after each
commit assert that the `affected_ids` the commit reported is a **superset** of
the closure recomputed from `full_code_delta()`'s edge set. Over-fire is
allowed by `§5.4` (it constrains `changed`, not `affected`); a **miss** is the
failure this test exists to catch.

Skip deletions in this test — their closure legitimately seeds from the prior
layer (`code_layer.rs:258-262`) and is not derivable from the current edge set
alone. The Rust test at gen 5 covers that case.

## 3.6 Verify

```sh
devenv shell -- check-rust && devenv shell -- clippy
devenv shell -- tests            # debug build ⇒ the assertion is live everywhere
devenv shell -- parity-oracle
devenv shell -- test-property
```

**Exit condition.** `derived_reverse_deps` exists and is asserted on both
producer paths under `debug_assertions`; the multi-generation Rust test and the
Python commit-path test pass; the full suite is green.

---

# After this project (historical roadmap)

In order:

- **Project 30 — cache the produced `CodeLayer` on snapshots.** DESIGN §4.4.
  The one change in this area with real performance impact: every traced derived
  production on a fresh snapshot currently pays a full project rebuild. Size the
  memory cost per pinned snapshot first.
- **Project 31 — semantic-state investigation.** The proposed `SemanticState`
  type was investigated and rejected. See
  `../31-semantic-state/README.md`; do not use the old “lazily produced layer”
  wording as a description of current behavior.
- **Jujutsu context via `pyjutsu`.** DESIGN §6. Independent of all the above;
  can start any time.
