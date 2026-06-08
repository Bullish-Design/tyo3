# Phase 10 kickoff — AST-canonical hashing (paste into a fresh session)

We're continuing the TyO3 spine refactor. All planning is done and lives in
`.scratch/projects/15-implementation-plan/`. Your job this session is to implement
**Phase 10: AST-canonical content hashing** — replace the line-by-line text
normaliser with a renderer over the entity's **canonical AST subtree**, so the
content hash reflects *meaning* not *formatting*; literal *content* (including
the exact text inside string literals) becomes significant; whitespace outside
literals and trailing-comma style stay insignificant; and the
**container-subsumes-members** property is promoted from an accident to a
**named, documented, tested invariant**.

This is mostly **Rust** (`rust/src/hash.rs`, `rust/src/entity.rs`) — **rebuild
after every change** (`devenv shell -- build`) before any pytest gate; the #1
phantom-failure source is testing a stale extension.

Before writing any code, read in this order:
  1. `.scratch/projects/15-implementation-plan/START_HERE_V2.md`            (orientation — read fully; note the V2 phase-numbering table)
  2. `REFINED_IMPLEMENTATION_CONCEPT.md` (V1) §5.6                          (content hashing / normalisation — the normative rules)
  3. `REFINED_IMPLEMENTATION_CONCEPT_V2.md` §5.2–§5.3                       (why container-subsumes-members is load-bearing for `affected` coverage)
  4. `REFINED_IMPLEMENTATION_PLAN_V2.md`  (Phase 10 section)               (current state + phase map)
  5. `PHASE_10_IMPLEMENTATION_GUIDE.md`                                     (the step-by-step 10.1–10.4 you'll execute)
Then skim, for current ground truth:
  6. `.scratch/projects/16-refined-implementation-work/PROGRESS.md` §9.8 and §9.9  (what Phases 8 and 9 landed)

## Critical context — Phases 6–9 are LANDED (this is why Phase 10 lands last)

Hashing is largely independent of the spine, but it **underpins** two things
that must already be correct for the invariant test to be meaningful, so it
lands after them:

- **Phase 6** made `affected` a sound, container-granular superset. Its coverage
  of inference-flow dependencies (`w = make_widget(); w.draw()` with no
  `render → draw` edge) relies entirely on **the container hash moving when a
  member body changes** — the very property Phase 10 makes explicit (§5.2).
- **Phase 8** keys the derived cache by `content_hash` (per-layer locality).
  Changing the hash function changes every cache key, so Phase 10's `local`-layer
  no-op-on-formatting behaviour matters for cache reuse.
- **Phase 9** (just landed, commit `74adf25`) is the async precision refiner. It
  **classifies a changed class that owns a changed member as "subsumption-only"**
  and narrows via the member's precise users. That classification is a documented
  *precision* heuristic precisely because today's hash can't distinguish "class
  changed by member-body subsumption" from "class changed its own structure."
  **Phase 10's explicit subsumption invariant is what would let a future refiner
  make that distinction rigorous** — but Phase 10 only needs to make the invariant
  *true, documented, and tested*; wiring the refiner to exploit it is forward work,
  not this phase.

## What Phase 10 changes — what exists vs what you build

- **Replace, do not extend:** `rust/src/hash.rs::normalise_entity_source` is a
  **line/text heuristic** today — `collapse_whitespace` (which also collapses
  whitespace *inside* string literals — the core bug), `is_likely_docstring_line`
  (pattern-matches docstrings by line shape), `strip_trailing_comma`. Replace the
  whole `NormalForm`-from-text path with a **canonical AST rendering**.
- **The entity "source" is a text slice today:** `rust/src/entity.rs:220` does
  `entity_source = extract_range(source_str, info.full_range)` then
  `normalise_entity_source(&entity_source, policy)` → `hash_entity(...)`. So the
  renderer's input is currently a substring of the file. Decide deliberately how
  you get an AST from it (see stale points #2 below).
- **Keep:** `ContentHash(u128)` via `xxhash_rust::xxh3::xxh3_128` (`hash.rs:39`)
  — already ≥128-bit, deterministic, machine-stable. You change *what bytes get
  hashed* (the canonical rendering), not the hash function. The `HashPolicy` /
  `HashProfileCfg` mapping (`hash.rs:97`) stays; you re-implement how the policy
  is *applied* (over the AST, not over lines).
- **You build:** the AST renderer in `hash.rs`; the per-policy docstring/comment
  handling over the AST; and you **promote the invariant test** (10.4).

## The concrete target tests (this is the acceptance, be precise)

`src/tyo3/tests/test_final_hash_ast.py` is the contract. Today **one test fails
(the documented baseline) and two are `xfail(strict=True)`** — Phase 10 must flip
all three:

1. `test_formatting_only_hashes_same` — **currently FAILING** (the lone known
   baseline failure). Whitespace-only edits (extra spaces around `+`, an extra
   blank line) currently change the hash; they must not. Make it pass.
2. `test_string_literal_whitespace_hashes_differently` — **`xfail(strict=True)`**.
   `"a  b"` vs `"a b"` must hash *differently* (literal content significant).
   After your renderer makes it pass, **remove the xfail marker** (strict xfail
   turns an unexpected pass into a failure).
3. `test_docstring_policy_and_non_docstring_strings` — **`xfail(strict=True)`**.
   The `structure` profile excludes the docstring but a *non-docstring* string
   statement (`"side"` not in docstring position) stays significant; the
   `semantic` profile includes the docstring. Pattern-matching by line shape
   can't do this — the AST can (a docstring is the first string-statement of a
   def/class/module body). Make it pass and **remove the xfail marker**.
   (`test_meaningful_changes_*` and `test_signature_surface_*` already pass —
   keep them green.)

## Stale-guide points to reconcile (VERIFY before following the guide literally)

1. **The plan says "hex-encoded"; reality is DECIMAL.** Plan §10.3 says the hash
   is "hex-encoded," but `ContentHash(u128)` is rendered as a **decimal** string
   end-to-end (the failing test compares `'317809467066...'`). The native producer
   compares `content_hash` as decimal in some paths (see memory
   `gate3n-native-code-delta`). **Do NOT switch to hex** unless you change it
   *consistently* everywhere (producer, store keys, parity oracle) and accept a
   derived-cache-key rebuild — and there's no reason to. Width (≥128) and
   determinism are **already satisfied**; treat 10.3 as "don't regress encoding,"
   not "switch to hex." State your choice; the safe one is "keep decimal."

2. **There is `ruff_python_ast` but NO `ruff_python_parser` dep.** `Cargo.toml`
   pins `ruff_python_ast`, `ruff_db`, `ruff_text_size`, `ruff_source_file`
   (ty v0.0.40, rev `3cb09eba`), and the AST types + `PySourceType` are already
   used (`code_layer.rs:1391`, `content.rs:608`, `overlay.rs:37`). But a *parser*
   to turn entity text into an AST is not obviously a direct dep. **Decide your
   AST source deliberately:**
   - **(preferred) reuse the file's already-parsed module** from the ty/`ruff_db`
     database (the producer in `code_layer.rs`/`content.rs` already has parsed
     access) and render the entity's existing AST subtree by range — no
     re-parsing, no indentation problems; **or**
   - add `ruff_python_parser` and re-parse the `extract_range` slice — but note a
     sliced **method** body is *indented* and won't parse as a standalone module,
     so you'd have to dedent or wrap it. The first option avoids this trap.
   Verify which AST access the producer already holds before committing to a path.

3. **V1↔V2 numbering in the test file is stale.** `test_final_hash_ast.py`'s
   header and the two xfail `reason=` strings say **"Phase 9"** (V1 numbering) —
   this work is **V2 Phase 10**. Update those references when you touch the file.

## Things to verify exist before relying on them

- **The invariant test:** `test_container_hash_subsumes_member_bodies` lives at
  `src/tyo3/graph/tests/test_inference_flow_coverage.py:56` (with
  `test_nominal_chain_carries_coverage` and `test_inference_flow_edge_absent_canary`).
  10.4 promotes/owns the subsumption assertion into the permanent hash-module
  surface (move it, or add a Rust `hash.rs` test asserting a class hash changes
  when a method body changes) **and** documents in `hash.rs` that this property is
  **load-bearing for `affected` coverage — never hash members independently of
  their container without revisiting Concept V2 §5.2–§5.3.** Keep all three
  inference-flow tests green.
- **The renderer must recurse into members.** A class's canonical rendering MUST
  include its methods' bodies (that's the subsumption mechanism). Confirm the AST
  subtree you render for a container includes member defs.
- **Rust unit tests in `hash.rs`** (the `#[cfg(test)]` block, ~line 230+:
  `reformatting_whitespace_same_hash`, `docstring_excluded_by_default`,
  `trailing_comma_normalised`, etc.) encode the *current* heuristic's behaviour.
  Some will need updating to the AST-canonical contract (e.g. literal-content
  significance). Update them deliberately; don't delete coverage.

## Working rules (every phase)

- Run EVERYTHING through `devenv shell --` (Nix toolchain). Never bare
  `pytest`/`cargo`.
- **After any Rust change, `devenv shell -- build`** before a pytest gate.
- Suites are slow (~10–15 min); run full suites in the background.
- **Hash over the canonical AST subtree, not bytes.** Literal content significant;
  whitespace-outside-literals and trailing-comma style insignificant; comments
  excluded by default; docstrings per layer policy.
- **Do not break container-subsumes-members for "precision."** It is load-bearing
  for `affected` coverage. If you ever want per-method cache keys, do it in the
  *derived layer's key* (Phase 8 `key_locality`), never the entity content hash.
- Don't weaken what's green: the Phase-7 bus contracts, the Phase-8 derived
  suites (`test_final_derived_contract.py`, `test_gate5_derived.py`), the Phase-9
  `test_precision_refinement.py`, and `test_inference_flow_coverage.py` all stay
  green. Changing the hash function changes derived cache keys — confirm the
  derived suites still pass (content-addressed reuse is fine; a key *shape* change
  is not).

## The milestone gate after the phase

```bash
devenv shell -- build
devenv shell -- cargo test --manifest-path rust/Cargo.toml hash entity
devenv shell -- pytest src/tyo3/tests/test_final_hash_ast.py src/tyo3/graph/tests/test_inference_flow_coverage.py -q --no-cov -rA
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

After Phase 10, the full-suite baseline should be **clean**: the lone remaining
baseline failure (`test_final_hash_ast::test_formatting_only_hashes_same`) is now
**green**, the two `xfail(strict=True)` AST tests are now **unmarked and passing**,
and there should be **zero failed / zero xfail** in `test_final_hash_ast.py`. The
current baseline going in is **"only `test_formatting_only_hashes_same` fails;
zero new"** and `cargo test` **162/0** (Phase 9 added 4 config tests). Don't
introduce new xfails or XPASS.

Track progress in `.scratch/projects/16-refined-implementation-work/PROGRESS.md`
(add a `§9.10 — V2 Phase 10` record alongside §9.8/§9.9). Commit at the end with
`refactor(hash): AST-canonical hashing; explicit container-subsumes-members invariant`
(no AI attribution — house rule).

## Start here

Read the docs above (incl. PROGRESS §9.8/§9.9 and the actual current
`rust/src/hash.rs` — `normalise_entity_source`, `collapse_whitespace`,
`is_likely_docstring_line`, `strip_trailing_comma`, the `#[cfg(test)]` block —
and `rust/src/entity.rs:~220` where the entity source slice is hashed), then give
me a short Phase 10 implementation plan: steps 10.1–10.4 mapped to the real
functions you'll touch, your **AST-source decision** (reuse the ty db's parsed
module vs add `ruff_python_parser` and re-parse the slice — with your reasoning
about the indented-member-slice trap), your **encoding decision** (keep decimal —
state why), exactly how the renderer applies the docstring/comment policy over the
AST (how it identifies a docstring positionally), how you'll keep
container-subsumes-members true and promote it to a tested+documented invariant,
and which existing `hash.rs` Rust unit tests you'll update vs keep. Note which of
the three target tests each step flips, and that you'll remove the two stale
`xfail(strict=True)` markers. **Wait for my go-ahead before editing code.**
