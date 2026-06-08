# Phase 10 (V2) — AST-canonical hashing (+ container-subsumes-members invariant)

> Execution guide for **Phase 10 of `REFINED_IMPLEMENTATION_PLAN_V2.md`**. Read
> V1 §5.6 (content hashing / normalisation) and Concept V2 §5.2 / §7 (the
> container-subsumes-members invariant) once before starting.
>
> **This is the V1 "Phase 9 — AST-canonical hashing" work, with one addition:**
> the container-subsumes-members behavior — which V2's affected-coverage model
> *depends on* — is promoted from an accident to a **named, documented, tested
> invariant**.
>
> **Depends on Phases 1–9.** Hashing is largely independent of the spine, but it
> underpins both the derived cache key (Phase 8) and the coverage of `affected`
> (Phase 6), so it lands after they are correct so the invariant test is meaningful.

---

## 0. Dev environment

All commands via `devenv shell --`. **Native** (`rust/src/hash.rs`,
`rust/src/entity.rs`) — **rebuild** (`devenv shell -- build`) before any pytest
gate. Suites are slow.

---

## 1. What Phase 10 changes, and why

The current normaliser (`rust/src/hash.rs`, `normalise_entity_source`) works on
source text line-by-line and collapses whitespace **inside** string literals, so
`"a  b"` → `"a b"` does not change the hash (V1 §6.3 deviation #8). Replace it
with an AST-canonical renderer; literal *content* becomes significant. And make
the container-subsumes-members property explicit.

### Files in scope

| File | Role |
|---|---|
| `rust/src/hash.rs` | **edit (core)** — AST renderer replacing the line heuristic; per-policy docstring/comment handling (10.1, 10.2) |
| `rust/src/entity.rs` | **reference/edit** — the entity source/AST subtree the renderer consumes; confirm a container's subtree includes its members (10.4) |
| `src/tyo3/tests/test_final_hash_ast.py` | the contract test for AST-canonical hashing (10.1–10.3) |
| `src/tyo3/graph/tests/test_inference_flow_coverage.py` | **move/own** — the container-subsumes-members invariant test becomes a permanent hash-module guard (10.4) |

---

## 2. Working rules

1. **Hash over the canonical AST subtree, not bytes** (V1 §5.6). ≥128-bit width,
   machine-stable, hex-encoded.
2. **Literal content is significant.** `"a  b"` ≠ `"a b"`.
3. **Whitespace outside literals and trailing-comma style are ignored.**
4. **Docstrings/comments per layer policy** (comments always excluded by default).
5. **A container's hash MUST include its members' bodies** — this is the coverage
   mechanism for inference-flow dependencies (Concept V2 §5.2). The renderer must
   recurse into member subtrees. Guard it with a test.

---

## 3. Step-by-step

### Step 10.1 — Replace the line heuristic with an AST renderer

Parse the entity source with the ruff parser, walk the AST, emit canonical tokens
for node kind, identifiers, literals (literal content preserved exactly),
signatures, annotations, decorators, bases, control flow, assignments. Exclude
comments; include/exclude docstrings per policy. Recurse into member definitions
so a class subtree's rendering includes its methods' bodies.

**Verify.** `devenv shell -- build && devenv shell -- cargo test --manifest-path rust/Cargo.toml hash entity`

### Step 10.2 — Define and test the policy

Whitespace outside literals ignored; trailing commas ignored; docstrings
included/excluded by policy; annotations, decorators, default values, import
aliases significant.

**Verify.** `devenv shell -- pytest src/tyo3/tests/test_final_hash_ast.py -q --no-cov -rA`

### Step 10.3 — Keep width and stability

≥128 bits, stable across machines, consistently hex-encoded. (Note: the native
producer compares `content_hash` as decimal in some paths — keep encoding
consistent end-to-end; see `gate3n-native-code-delta`.)

### Step 10.4 — Promote the container-subsumes-members invariant

Move the hash-subsumption assertion from
`test_inference_flow_coverage::test_container_hash_subsumes_member_bodies` into
the permanent hash-module test surface (or reference it from there), and document
in `hash.rs` that **this property is load-bearing for `affected`-set coverage —
do not hash members independently of their container without revisiting Concept
V2 §5.2–§5.3.** A future "finer cache keys" optimization that breaks this would
silently reintroduce a §5.4 no-miss regression.

**Verify.** `devenv shell -- pytest src/tyo3/graph/tests/test_inference_flow_coverage.py -q --no-cov`

> Commit here: `refactor(hash): AST-canonical hashing; explicit container-subsumes-members invariant`.

---

## 4. Acceptance

```bash
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_hash_ast.py src/tyo3/graph/tests/test_inference_flow_coverage.py -q --no-cov -rA
devenv shell -- cargo test --manifest-path rust/Cargo.toml hash entity
devenv shell -- pytest -q --no-cov
```

**Exit criteria:** hashing is AST-canonical; literal content is significant; the
container-subsumes-members invariant is explicit, documented, and tested; both
suites green.

---

## 5. Pitfalls

- **Forgetting the rebuild** — the pytest gate probes the old extension.
- **Breaking container-subsumes-members for "precision."** It is load-bearing for
  coverage; if you ever want per-method cache keys, do it in the *derived layer's*
  key (Phase 8), not the entity content hash.
- **Encoding drift** (decimal vs hex) between producer and store — keep it
  consistent.

## 6. Leaves for later

- Read-surface lifetime — Phase 11. Config surfacing of hash policy — Phase 12.
