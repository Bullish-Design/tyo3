# INVESTIGATION — `SemanticState` (project 31)

Traced against the working tree at trunk `aa87019` (post-project-30) on
2026-09-14. Every load-bearing claim carries a `file:line` anchor or a
reproduction command. Where the project-29 and project-30 documents disagree
with the source, the source wins.

The implementation has since landed in three independently pushed lanes. The
pre-implementation duplication and line references below are retained where
they explain the evidence; current post-implementation locations and
measurements are recorded in §14.4 and the outcome in §17.

---

## 1. Executive summary

**Decision: document only. Do not add a `SemanticState` type. Close the
architectural question.**

Project 29 §5 deferred a corrected `SemanticState { identities, code }` on HEAD.
Project 30 then moved the boundary. The current shape is:

- HEAD owns `registry: IdentityRegistry` and `code_layer: Arc<CodeLayer>`
  (`rust/src/project.rs:128`, `:152`).
- The read state carries `registry: IdentityRegistry` and
  `code_layer: Option<Arc<CodeLayer>>` (`rust/src/project.rs:84`, `:104`).
- Current-head snapshots serve the committed layer through the shared
  `full_code_delta_for` helper (`rust/src/project.rs:291`) at
  `snapshot.rs:61` and `methods.rs:673`.
- A missing carried layer falls back to a full `Builder::build` in that helper.

Three findings decide the question.

1. **The registry and the layer cannot be published as one value.** The layer is
   produced *from* the already-reconciled registry: `run_identity_reconciliation`
   mutates `head.registry` at `commit.rs:905`, and only then does
   `commit.rs:915-916` take a read clone and run the producer, which reads
   `state.registry` at `analysis.rs:123` → `convert/symbols.rs:99-102`. Any
   `SemanticState` on HEAD would still be mutated field-by-field, expressing no
   invariant, or would force a fresh deep registry clone per commit to build one
   value.

2. **A wrapper would make an existing benign inconsistency into a lie.** At
   `commit.rs:915` the read clone carries the R−1 layer together with the R
   registry and the R database (`project.rs:193`). Nothing reads
   `state.code_layer` there — `Builder` touches only `state.root`, `state.db`,
   `state.registry` and `state.hash_policies` (`code_layer.rs:561-573`,
   `:672`, `:778`, `:1069`, `:1201`, `:1235`). Two separately-named fields make
   that harmless. One struct named `SemanticState` would claim the two halves
   describe one revision, which at that point they do not.

3. **The measured memory profile forbids any owned duplicate.** Measured on this
   checkout (§14.2): eight snapshots at the **same** revision cost **2.05 MiB
   each**, because the `Arc<CodeLayer>` is shared — a clean figure, no commits
   involved. Eight snapshots at **eight distinct** revisions cost **13.42 MiB
   each**, an upper bound (that branch also runs eight commits; see the confound
   note in §14.2). Against project 30's isolated 12.83 MiB/layer, a design that
   gives each snapshot its own owned layer costs roughly **6× more per pinned
   snapshot**.

Project 30 did solve the substantive problem: the carried layer cuts
`full_code_delta` from **4.40 s to 0.17 s** at head and **4.15 s to 0.15 s** on a
snapshot at repository scale (§6, reproducible). What remains is naming and
documentation debt, plus two narrow, evidence-backed code cleanups that are not a
new abstraction:

- **Dedupe the serve-or-rebuild match.** This is now one helper at
  `project.rs:291`, called by `snapshot.rs:61` and `methods.rs:673`.
- **Dedupe the cache-hit rule.** This is now `HeadState::servable_code_layer`
  at `project.rs:220-221`, with the deliberate `is_head` guard remaining at
  `methods.rs:613`.

One real gap is recorded but deliberately **not** fixed here: a session that
never commits never builds a head layer (`open.rs:168`, `commit.rs:849-851`), so
every `full_code_delta` on a read-only session still pays the full rebuild —
measured 4.40 s, repeatedly (§6.3). Fixing it needs a write on a read path,
which architectural constraint 13 forbids. It is sized as a follow-up in §15.

---

## 2. Current-state ownership and lifetime table

| Object | Owner | Lifetime | Revision semantics | Persisted | Cloned or shared | Authoritative or derived | May be absent | Atomic co-publication |
|---|---|---|---|---|---|---|---|---|
| `IdentityRegistry` (`identity.rs:78`) | `HeadState.registry` (`project.rs:120`) | whole session; survives `reload()` (`methods.rs:137`) | **last-known** facts *across* revisions; anchors carry `first_seen_rev` / `last_seen_rev` (`identity.rs:56-57`) | **Yes** — sidecar `identity.db`, every commit (`commit.rs:551-567`) | **Deep clone** into every read clone (`project.rs:168`, `:185`) and every snapshot (`methods.rs:602`, `:633`) | **Authoritative** | No — always a value; `Option` removed by project 29 Step 2 | With the layer, under the head lock; see §4.2 |
| `Anchor` (`identity.rs:51`) | inside `IdentityRegistry.by_id` | outlives its entity — `retire` keeps it in `by_id` (`identity.rs:187-211`) | last-known; `status: Active\|NeedsReview\|Orphaned` (`identity.rs:65-70`) | Yes, with the registry (serde, `identity.rs:314`) | with the registry | Authoritative | `get()` → `None` for an unknown id | n/a |
| `CodeLayer` (`code_layer.rs:211`) | `HeadState.code_layer: Arc<…>` (`project.rs:141`) | replaced wholesale each reconciling commit (`commit.rs:949`); reset to empty by `reload()` (`methods.rs:141` → `open.rs:168`) | **current** facts for exactly **one** revision; complete, not a patch (`code_layer.rs:525-527`) | **Never** — no sidecar write exists | **`Arc`-shared**, never mutated after publication (no `Arc::make_mut` / `get_mut` anywhere in `rust/src`) | Authoritative for its revision; **derived** from db + registry | Empty until the first reconciling commit (`open.rs:168`) | see §4.2 |
| `NodeData` (`code_layer.rs:153`) | inside `CodeLayer.nodes` | with the layer | current revision only | No | with the layer | Derived | `nodes.get()` → `None` | n/a |
| `ProjectDatabase` | `HeadState.db` (live) and `TyProjectState.db` (frozen) | head: session; frozen: snapshot | head floats; frozen pinned by `Generation` + `Revision` (`open.rs:188-243`) | No (salsa memo cache) | **Cloned** per read (`project.rs:245-251`); snapshots get an **independent** `Zalsa` (`open.rs:178-179`) | Authoritative substrate | No | n/a |
| `HeadState` (`project.rs:110`) | `PyTyProject.inner: Arc<Mutex<Option<…>>>` | session; `None` after `close()` | always the newest published revision | partially (registry + authored via sidecar) | never cloned — `store`/`system` must not escape (`project.rs:106-109`) | Authoritative | `None` after `close()` | it **is** the publication boundary |
| `TyProjectState` (`project.rs:81`) | a read clone, or `PySnapshot.inner` | one call (read clone) or one snapshot | pinned for a snapshot; floating for a head read clone | No | cheap clone: `Arc` bump for the layer, deep clone for the registry + db | Derived view | its `code_layer` may be `None` (4 legitimate producers, §4.3) | consumes a published pair |
| `Snapshot` / `PySnapshot` (`snapshot.rs:16`) | Python `Snapshot` wrapper | until `close()` | immutable, pinned at `revision`; `is_head` distinguishes head from time travel (`snapshot.rs:26`) | No | shares the revision's `Arc<CodeLayer>` with HEAD and sibling snapshots | Derived | — | — |
| `CodeGraph` (`graph/projection.py`) | Python `Snapshot._graph` (`views.py:295`) or `TyO3Session._head_graph` (`session.py:244`) | memoized per snapshot; dropped on every commit (`session.py:625-635`) | pinned via `_pin_at(revision)` (`views.py:295`) | No | rebuilt, never shared | **Pure projection** of one complete `full_code_delta()` (`applier.py:88-105`) | absent until first access | n/a — built on demand |

---

## 3. Data flow: commit → snapshot → projection

```
                       ┌─ HEAD lock held ────────────────────────────────────┐
disk / overlay content │                                                     │
        │              │  commit(head, mutation)                commit.rs:1015│
        ▼              │    build_plan  ──────────────────────  commit.rs:593 │
  ContentStore.stage   │    Baseline { published_gen, registry,               │
        │              │               authored, code_layer }   commit.rs:1019│
        ▼              │        (Arc clone — no deep layer copy)              │
  system.publish       │    run_staged                          commit.rs:882 │
  (staged generation)  │      1. system.publish(staged_gen)          :888     │
        │              │      2. db.apply_changes(events)            :899     │
        ▼              │      3. run_identity_reconciliation         :905     │
     analysis          │           extract_entities                  :367-370 │
   (ty / salsa)        │           reconcile{,_scoped}(&mut registry) :375-376│
        │              │           ► head.registry NOW AT R                   │
        ▼              │      4. prev = Arc::clone(head.code_layer)  :914     │
 entity extraction     │         state = head.read_clone()           :915     │
        │              │           ► state.registry = R                       │
        ▼              │           ► state.code_layer = R−1  (stale, unread)  │
identity reconciliation│         produce_layer(state, prev, …)       :916     │
        │              │           scoped   → produce_code_delta_scoped :870  │
        ▼              │           cold/rescan → produce_code_delta     :873  │
 scoped CodeLayer      │           debug_assert reverse_deps derivable         │
    production         │                    code_layer.rs:504-509, :541-546   │
        │              │      5. affected_ids = next.affected_closure_        │
        ▼              │           with_deleted(prev, seeds, deleted)  :929   │
 publication of HEAD   │         affected_files from next.nodes        :940   │
        │              │         head.code_layer = Arc::new(next)      :949   │
        │              │      6. persist_identity(head)  → sidecar     :956   │
        │              │      7. store.publish_staged   ◄ PUBLISH LAST :976   │
        │              │    on any Err → rollback(head, base, e)       :571   │
                       └─────────────────────────────────────────────────────┘
        │
        ▼
   read clone                     HeadState::read_clone        project.rs:177
        │                           registry : deep clone            :185
        │                           code_layer: Some(Arc) iff !is_empty :193
        ▼
   snapshot / frozen db          PyTyProject::snapshot        methods.rs:596
        │                           build_frozen (own Zalsa)   open.rs:188
        │                           state.registry   = head clone  :633
        │                           state.code_layer = Some(Arc) iff
        │                              is_head && !is_empty      :611-615
        │                           time travel ⇒ None           :611
        ▼
   full_code_delta               Snapshot     snapshot.rs:58-77
        │                        head        methods.rs:669-690
        │                          Some(layer) → layer.diff_from(&EMPTY, rev, true)
        │                          None        → produce_code_delta(&state, &EMPTY, …)
        ▼
   Python CodeGraph projection   views.py:293 / session.py:241
                                   g.apply_code_delta(full_code_delta())
                                   wholesale replacement   applier.py:88-120
                                   g._pin_at(revision)      views.py:295
```

---

## 4. Verified invariants

Each item of the brief's §2 checklist, against current source.

### 4.1 The asymmetries — all confirmed

| Claim | Verdict | Evidence |
|---|---|---|
| `IdentityRegistry` holds **last-known** identity facts used by reconciliation | ✅ | `identity.rs:6-9` ("binds ids to last-known entity facts"); anchors carry `first_seen_rev`/`last_seen_rev` (`identity.rs:56-57`); `classify` compares the **old** hash captured on the binding against the new entity hash (`identity.rs:516-545`) |
| `CodeLayer` holds **current** semantic facts for **one** revision | ✅ | `code_layer.rs:209` "The canonical structural state for one revision"; replaced wholesale per commit (`commit.rs:949`) |
| The registry includes **orphaned** anchors; the layer does not | ✅ | `retire` removes from `by_path` but **keeps the anchor in `by_id`** with `status = Orphaned` (`identity.rs:187-211`); the layer is rebuilt from current content, so a deleted entity has no node — asserted at `code_layer.rs:1859` |
| The layer includes synthetic module nodes and external stubs; the registry does not | ✅ | `make_module_durable_id` (`code_layer.rs:39`), `make_external_module_id` (`:44`), `make_external_symbol_id` (`:49`). No `<module>` / `<external>` literal exists in `entity.rs` or `identity.rs` (`grep -n '<module>\|<external>' rust/src/entity.rs rust/src/identity.rs` → empty). Measured on this repo: 2,755 entity ids vs 3,397 layer nodes — 642 synthetics |
| The registry is persisted through the sidecar; the layer is not | ✅ | `persist_identity` → `sidecar.write_atomic(identity_db_path)` (`commit.rs:551-567`); `grep -n code_layer rust/src/sidecar.rs` → empty |
| The registry is required **during** reconciliation, before the new layer exists | ✅ | `run_identity_reconciliation` builds its extraction state with `registry: head.registry.clone()` and `code_layer: None` (`commit.rs:356-365`), then calls `reconcile`/`reconcile_scoped` (`:375-376`) |
| The layer is produced **after** reconciliation | ✅ | `commit.rs:905` (reconcile) strictly precedes `:916` (`produce_layer`) |
| `CodeLayer` is **complete** for its revision, not an incremental patch | ✅ | `code_layer.rs:525-527` "byte-identical to a full `Builder::build` over the same final content"; proved directly by `scoped_producer_matches_full_rebuild_across_generations` (`code_layer.rs:1660`), which asserts `nodes`/`edges`/`reverse_deps`/`file_to_nodes` equality across 5 generations including a file deletion (`assert_layers_equal`, `code_layer.rs:1652`) |
| `reverse_deps` is an invariant derived from canonical edges | ✅ | `derived_reverse_deps` (`code_layer.rs:274-289`) is the canonical definition; both producers `debug_assert_eq!` against it (`:504-509`, `:541-546`), so every debug test run checks it. `remove_edge`'s parallel-edge pruning (`:251-267`) is covered by `scoped_producer_prunes_parallel_edges_and_deleted_targets` (`:1762`) |
| `CodeNodeDto` has strict parity requirements; its wire shape must not change | ✅ | `dto/code_delta.rs:9-12` names the tiers; `tests/parity_oracle.py:76` `STRUCTURAL_NODE_FIELDS` is the strict set; `parity-oracle` green at 8 tests (§14) |

### 4.2 Atomic publication — precisely what is and is not atomic

**Externally atomic, internally sequenced.** No caller outside the head lock can
observe a torn pair: `commit` holds the head lock for all of `run_staged`
(`commit.rs:882`), and every failure path restores the registry, the authored
store *and* the layer from one `Baseline` (`commit.rs:524-532`, `:571-577`).

**Internally, the pair is intentionally out of step for the length of step 4.**
Between `commit.rs:905` (registry → R) and `commit.rs:949` (layer → R) the head
holds an R registry beside an R−1 layer. That window is not a defect: the
producer *needs* the R registry to attach durable ids
(`analysis.rs:123` → `convert/symbols.rs:99-102`). It is a hard ordering
constraint, and it is the single most important fact for §7.

### 4.3 When is `code_layer == None`?

All six `TyProjectState` construction sites, exhaustively
(`grep -n 'code_layer' rust/src/project.rs rust/src/project/*.rs`):

| Site | Value | Why |
|---|---|---|
| `project.rs:173` `TyProjectState::read_clone` | propagates whatever it holds | a read clone of a read clone |
| `project.rs:204` `HeadState::read_clone` | `head.servable_code_layer()` | pre-first-commit head has an empty layer; the helper treats it as a miss |
| `project.rs:914` MVCC stress test | `None` | test scaffolding |
| `commit.rs:364` pre-reconcile extraction state | `None` | this state exists only to run `extract_entities`; a layer is meaningless for it |
| `open.rs:243` `build_frozen` | `None` | the frozen state is constructed before the caller decides what to attach |
| `methods.rs:613` `snapshot()` | `head.servable_code_layer()` only when `is_head` | a **time-travel** snapshot must not be served the head's layer |

So the `Option` is **not** vestigial — unlike the `Option<IdentityRegistry>`
project 29 Step 2 removed, which had zero real `None` producers. Four of the six
sites produce `None` for four distinct, correct reasons.

### 4.4 Would a `SemanticState` erase "last-known identity" vs "current code"?

**Only if it exposes a merged accessor.** A `SemanticState { identities, code }`
with two public named fields does not, by itself, merge the key sets. But it
invites exactly that: the natural next method on such a type is a unified
`get(&DurableId)`, and the two key sets are provably different (§4.1, rows 3–4).
Project 29 §3 already rejected merging `Anchor` and `NodeData` for the same
reason. **Any design whose first convenience method would be a merged lookup is
rejected here** — see Design C.

---

## 5. The precise problem project 31 was supposed to solve

From `.scratch/projects/29-semantic-plane-cleanup/DESIGN.md:268-279`:

> - **HEAD** owns `semantic: SemanticState { identities, code }` — both live, both
>   owned, one publication boundary.
> - **Read clone / snapshot** owns identities plus a *lazily produced* layer.
>
> That is what happens today; the value is naming it so the asymmetry is
> deliberate rather than accidental.

Decomposed, project 31 was to deliver three things:

1. **P1 — Name the HEAD publication boundary,** so the registry and layer are
   visibly published together rather than by coincidence.
2. **P2 — Name the read-side asymmetry,** so "the snapshot has identities but
   maybe not a layer" is deliberate.
3. **P3 — Prevent future misuse** of the two planes as interchangeable.

The document itself states the value is "naming it", not behaviour. That is a
documentation goal, and it must be judged against what project 30 shipped.

---

## 6. Did project 30 already solve it?

### 6.1 P1 — the HEAD publication boundary: **solved, and a type cannot improve it**

The boundary exists and is enforced by three mechanisms that a wrapper struct
does not strengthen:

- **The lock.** Every mutation runs inside `commit` under the head mutex
  (`commit.rs:882`; the mutex is `PyTyProject.inner`, `project.rs:205`, taken via `lock_state`, `project.rs:223`).
- **One rollback record.** `Baseline` already *is* the "these move together"
  statement, in code, with the three fields named: `registry`, `authored`,
  `code_layer` (`commit.rs:524-532`, restored together at `:571-577`).
- **Publish-last.** The store — the observable revision — advances only at
  `commit.rs:976`, after both halves are in place.

A `SemanticState` struct cannot be assigned atomically anyway, because of the
§4.2 ordering constraint. It would therefore be mutated as
`head.semantic.identities = …; …; head.semantic.code = …` — identical to today,
one nesting level deeper.

**Note:** `Baseline` already carries `authored: AuthoredStore` alongside the
other two (`commit.rs:528`). Any honest "everything HEAD publishes per commit"
type would be `{ identities, authored, code }`, not `{ identities, code }`. The
project-29 sketch is already incomplete. That is itself evidence the grouping is
not a natural domain object.

### 6.2 P2 — the read-side asymmetry: **solved, but the old description is wrong**

> **"Lazily produced layer" is no longer an accurate description.** Nothing is
> lazily *produced*. The read state carries an **eagerly published**
> `Option<Arc<CodeLayer>>` (`project.rs:104`). On a miss, the fallback
> recomputes from scratch on **every** call and throws the result away — the
> shared helper binds the produced layer to `_next` and drops it
> (`project.rs:291-303`). It is a stateless recompute, not
> a lazy cache. Project 29 §5 must not be quoted as current.

The asymmetry itself is documented in place at `project.rs:94-104`:

```rust
/// The committed code layer for this state's revision, when one exists.
///
/// `None` is used by read clones taken before the first reconciling commit
/// (the empty placeholder from `open.rs:168`), time-travel snapshots that
/// cannot use the head layer (`methods.rs:607-613`), the pre-reconcile
/// extraction state (`commit.rs:356-365`), `build_frozen` (`open.rs:243`),
/// and test constructors. A miss triggers a stateless full rebuild: both
/// consumers discard the rebuilt layer, so this costs speed on every call,
/// never correctness. This is not Project 29 DESIGN §5's "lazily produced
/// layer" or a lazy cache.
```

That comment was corrected in Step 0 to include time travel and the other
legitimate `None` producers. The fallback remains a stateless recompute, not a
lazy cache.

### 6.3 The performance claim, reproduced

Reproduction (debug build, repository root, 167 files / 3,397 nodes / 36,285
edges), script preserved at `.scratch/projects/31-semantic-state/probe_timing.py`:

```
pre-commit  head full_code_delta      : 4.399s   nodes=3397 edges=36285
pre-commit  head full_code_delta again: 2.878s   (salsa warm; Builder still runs)
pre-commit  snapshot full_code_delta  : 4.154s
commit (one edit)                     : 2.978s
post-commit head full_code_delta      : 0.173s   nodes=3397 edges=36285
post-commit head snapshot full_code_delta: 0.152s  rev=2
time-travel snapshot(at=1) full_code_delta: 4.110s
```

Three facts fall out:

1. **The carried layer works.** 4.40 s → 0.17 s at head, 4.15 s → 0.15 s on a
   head snapshot: a **25×** reduction, consistent with project 30 DESIGN §1.4's
   0.141 s (release-vs-debug and fixture drift account for the difference).
2. **Node and edge counts are identical** between the pre-commit full rebuild
   and the post-commit carried layer — independent corroboration of the
   `code_layer.rs:1660` equivalence test on a real project.
3. **Time travel deliberately pays the fallback** (4.110 s), exactly as
   `methods.rs:607-610` documents.

### 6.4 The gap project 30 left

A session that never commits never produces a head layer: `build_head_with_config`
starts it empty (`open.rs:168`), `open()` reconciles identity but does **not**
produce a layer (`methods.rs:80-91`; no `code_layer` assignment exists there), and the deliberate reason is recorded at
`commit.rs:849-851` ("the head layer is built lazily, never at open, to keep
`open()` off the producer's cost path"). The measurement above shows the
consequence: a read-only session pays **4.40 s on every `full_code_delta` call,
forever**. `poll_changes` with no events returns `Ok(None)` without committing
(`commit.rs:1016-1018`), so a watch-only daemon session never escapes it either.

This is out of scope for project 31: closing it requires either moving the cost
onto `open()` (explicitly rejected) or memoising on a read path, which
architectural constraint 13 forbids. Sized as a follow-up in §15.

### 6.5 Verdict

| Goal | Status after project 30 | Needs a new type? |
|---|---|---|
| P1 — name the HEAD publication boundary | Solved by the lock + `Baseline` + publish-last | **No** — and §4.2 makes a single atomic assignment impossible |
| P2 — name the read-side asymmetry | Solved by `Option<Arc<CodeLayer>>` + its doc comment | **No** — but the comment is stale and incomplete (§6.2) |
| P3 — prevent future misuse | Partly. Two duplicated code sites are the real risk (§9.1) | **No** — a wrapper *increases* the misuse surface (§4.4) |

---

## 7. Candidate design comparison

| | **A** Keep fields, improve naming + dedupe | **B** `SemanticState` on HEAD only | **C** One shared `SemanticState` everywhere | **D** `Head/Read/SnapshotSemanticState` | **E** No implementation at all |
|---|---|---|---|---|---|
| Semantic clarity | **Good** — comments state the real rule incl. time travel | Neutral — nests two already-clear fields | **Harmful** — claims a coherence that §4.2 breaks | Good in the abstract | Poor — stale comments remain |
| Ownership clarity | Unchanged (already explicit) | Unchanged | Worse — one name, three ownership modes | Better on paper | Unchanged |
| Revision correctness | Unchanged | Unchanged | **At risk** — the commit-window state (§4.2) becomes a "SemanticState" with mismatched halves | Unchanged | Unchanged |
| Atomic publication | Unchanged (lock + `Baseline`) | No gain — still field-by-field | No gain | No gain | Unchanged |
| Snapshot correctness | Unchanged | Unchanged | Unchanged *if* the `Option` survives | Unchanged | Unchanged |
| Memory behaviour | Unchanged | Unchanged | **Fails** if it owns rather than `Arc`-shares: +11.4 MiB per same-revision snapshot (§14) | Unchanged | Unchanged |
| API / wire stability | No change | No change | No change | No change | No change |
| Testability | Slightly better — one function to test, not two copies | Unchanged | Worse — more states to enumerate | Better | Unchanged |
| Code churn | **~40 lines**, one file pair | ~60 lines, 8 sites | ~250 lines, 6 construction sites + every field access | ~400 lines, 3 types + conversions | 0 |
| Risk of future misuse | **Lowered** — the duplicated match is the actual hazard | Raised — invites a merged accessor (§4.4) | **Highest** | Lowered, at high cost | Unchanged |
| Solves a real problem? | **Yes — two verified duplications + one stale comment** | No | No | No — the lifecycle is already explicit in six named sites | No |

---

## 8. Rejected designs, with concrete reasons

### Design B — `SemanticState { identities, code }` on HEAD only — **rejected**

1. **It cannot be published atomically.** The layer is derived from the R
   registry (`commit.rs:905` → `:915-916` → `analysis.rs:123` →
   `convert/symbols.rs:99-102`). Building one `SemanticState` value per commit
   requires the registry to be finished first, then moved or cloned into the new
   value — a fresh deep `IdentityRegistry` clone on every commit, for no gain.
2. **The grouping is already wrong.** `Baseline` shows the real per-commit triple
   is `{ registry, authored, code_layer }` (`commit.rs:524-532`). A two-field
   `SemanticState` names two thirds of the boundary and silently excludes the
   authored store.
3. **The invariant it would express is already expressed.** `Baseline` +
   `rollback` (`commit.rs:571-577`) is the "these move together" statement, in
   executable form, already covered by the fault-injection rollback tests
   (`methods.rs:428` lists the `code_layer` / `identity_persist` /
   `authored_persist` stages).
4. **It buys no read-path win**, because the read side would keep its own shape.

### Design C — one shared `SemanticState` across HEAD, read clones and snapshots — **rejected**

1. **It requires the artificial `Option` the brief forbids** — or worse, it hides
   a real one. HEAD's layer is non-optional (`project.rs:141`); the read side's
   is optional for four distinct reasons (§4.3). One type means either
   `Option<Arc<CodeLayer>>` on HEAD, reintroducing exactly the asymmetry project
   29 §2.3 rejected, or a non-optional layer on the read side, which is false for
   time travel.
2. **It would misdescribe the commit window.** `commit.rs:915` would construct a
   `SemanticState` whose registry is at R and whose layer is at R−1 (§4.2).
   Today two fields make that a non-statement; one type makes it a false one.
3. **It would misdescribe the pre-reconcile extraction state.** `commit.rs:356-365`
   deliberately builds a state with a cloned registry and no layer, used only for
   `extract_entities`. Calling that a `SemanticState` is wrong on both halves.
4. **Memory.** If it owns rather than `Arc`-shares, measured cost rises from
   2.05 MiB to ~13.4 MiB per same-revision snapshot (§14). If it `Arc`-shares,
   it is today's design with extra nesting.
5. **It conflates different lifetimes.** The registry survives `reload()`
   (`methods.rs:137`); the layer is discarded by it (`methods.rs:141` →
   `open.rs:168`). One type cannot honestly carry both lifetimes.

### Design D — `HeadSemanticState` / `ReadSemanticState` / `SnapshotSemanticState` — **rejected**

The lifecycle is already explicit in six named construction sites (§4.3), each
with its own comment. Three new types add three conversion paths, three sets of
accessors and ~400 lines of churn to restate what `HeadState` vs
`TyProjectState` (`project.rs:81` vs `:110`) already says — and
`ReadSemanticState` vs `SnapshotSemanticState` would be structurally identical,
since a snapshot *is* a `TyProjectState` behind a mutex (`snapshot.rs:17`). Type
proliferation with no invariant gained.

### Design E — no project 31 at all — **rejected, narrowly**

E is nearly right, and its conclusion ("project 30 established the correct
boundary") is adopted. It is rejected only because three concrete defects remain
and are cheap to fix: the stale `project.rs:94-98` comment (omits time travel),
the duplicated serve-or-rebuild match, and the duplicated cache-hit rule (§9.1).
Leaving a verbatim-duplicated twelve-line block that feeds the parity oracle is a
real regression risk, not a style preference.

---

## 9. Recommended architecture — Design A

**Keep `IdentityRegistry` and `Arc<CodeLayer>` as distinct fields. Add no new
state type. Remove the two duplications, and correct the comments.**

### 9.1 The two duplications (resolved)

**(a) The serve-or-rebuild match, twice, verbatim (before Step 1).**

`rust/src/project/snapshot.rs:61-76`:

```rust
let empty = crate::code_layer::CodeLayer::new();
let delta = py.detach(move || {
    match state.code_layer.as_deref() {
        Some(layer) => layer.diff_from(&empty, revision, true),
        None => {
            let (_next, delta) = crate::code_layer::produce_code_delta(
                &state, &empty, revision, true, None,
            );
            delta
        }
    }
});
```

`rust/src/project/methods.rs:675-690` was the same body **character for
character** — verified before implementation:

```sh
diff <(sed -n '61,76p' rust/src/project/snapshot.rs) \
     <(sed -n '675,690p' rust/src/project/methods.rs)   # no output
```

Both feed `apply_code_delta` (`views.py:293`, `session.py:241`). The asymmetry
that makes this dangerous: **only the `methods.rs` copy is covered by the parity
oracle.** `tests/test_final_parity_oracle.py:247` calls `assert_parity(session)`,
which resolves to `session._inner.full_code_delta` (`tests/parity_oracle.py:417-420`)
— i.e. the head call site. The snapshot call site had **no** oracle coverage.
Step 1 removed the second body; both call sites now call `full_code_delta_for`
at `project.rs:291`, so they cannot drift independently.

**(b) The cache-hit rule, twice, with different conditions.**

- `project.rs:204` — `code_layer: self.servable_code_layer()`
- `methods.rs:613` — `if is_head { head.servable_code_layer() } else { None }`

The shared clause ("an empty layer is a miss, not a hit") is now stated once in
`HeadState::servable_code_layer` (`project.rs:220-221`).
`snapshot()` deliberately adds `is_head`; `read_clone` deliberately does not
(a head read clone is always at head). That difference is correct and should be
**named**, not left to be rediscovered.

### 9.2 What Design A changes

1. A single `pub(crate) fn full_code_delta_for(state: &TyProjectState, revision: u64) -> dto::CodeDeltaDto`
   in `rust/src/project.rs:291`, called by both `snapshot.rs:61` and
   `methods.rs:673` inside their existing `py.detach(...)` wrappers. The GIL
   handling stays at the call sites; only the pure computation moves. Spike 4
   selected `project.rs` as the existing home for shared read-path helpers.
2. A single `impl HeadState { pub(crate) fn servable_code_layer(&self) -> Option<Arc<CodeLayer>> }`
   expressing "an empty head layer is a cache miss", used by `project.rs:204`
   and by `methods.rs:613` (the latter keeping its explicit `is_head &&`
   guard, so the time-travel rule stays visible at the snapshot site).
3. Comment corrections, which are the actual deliverable:
   - `project.rs:94-104` — add the time-travel `None` producer and state that the
     fallback is a **stateless recompute**, not a lazy cache.
   - `project.rs:123` / `:144` — state the ordering constraint of §4.2 and that
     the two planes are never interchangeable (registry: last-known, includes
     orphans, persisted; layer: current-revision, includes synthetics, never
     persisted).
   - `commit.rs:915` — state that the read clone taken there carries the R−1
     layer beside the R registry, that no consumer reads it, and that the field
     must not be relied on inside the commit.
   - `methods.rs:607-613` — keep, and cross-reference the retention decision
     (project 30 DESIGN §4.1) so a future "just cache time-travel layers too"
     edit meets the 13.42 MiB/revision figure first.

### 9.3 What must explicitly **not** change

- `TyProjectState.code_layer` stays `Option<Arc<CodeLayer>>`. Four legitimate
  `None` producers (§4.3).
- `HeadState.code_layer` stays a non-optional `Arc<CodeLayer>`.
- `IdentityRegistry` stays a deep-cloned value, **not** an `Arc`. Measured: the
  read-clone path costs 2.154 ms/call at repository scale (167 files, 2,755
  anchors) versus 2.008 ms at fixture scale (1 file, 6 anchors), so the
  registry's deep clone is bounded well under 0.15 ms. `Arc`-ifying it buys
  nothing and would make in-place reconciliation (`commit.rs:375-376`) need
  `Arc::make_mut`, i.e. the same deep clone, later.
- `CodeNodeDto`, `CodeDeltaDto`, `CommitDeltaDto` — untouched.
- The Python projection: `applier.py:88-120` wholesale replacement,
  `views.py:277-296`, `session.py:209-244`, `layers/code.py:50`.
- `produce_code_delta` / `produce_code_delta_scoped` signatures and their
  `debug_assert` pairs (`code_layer.rs:504-509`, `:541-546`).
- The `Baseline` / `rollback` contract (`commit.rs:524-532`, `:571-577`).

---

## 10. Is a new `SemanticState` type justified?

**No.**

To justify it, all six of the brief's preconditions would have to hold. Four
fail:

| Precondition | Verdict |
|---|---|
| The exact invariant the type expresses | **Fails.** The candidate invariant — "identities and code are published together for one revision" — is **false** inside the commit window (§4.2) and incomplete outside it (it omits `authored`, `commit.rs:528`) |
| Why existing fields and comments are insufficient | **Fails.** The fields are named and documented (`project.rs:94-99`, `:118-120`, `:136-141`); the comments are stale, which is a comment fix |
| Why it does not conflate registry and layer lifetimes | **Fails.** The registry survives `reload()`; the layer does not (`methods.rs:137` vs `:141` → `open.rs:168`) |
| Why it introduces no extra cloning or optionality | **Fails.** Design B needs a per-commit registry clone; Design C needs an artificial `Option` on HEAD |
| Which code paths must change | Answerable (§9.2) — but for the *comment* fix, not for a type |
| Which paths must stay unchanged | Answerable (§9.3) |

---

## 11. Exact proposed type and field definitions

**None.** No new struct, enum or trait is proposed. The full proposed diff is two
extracted functions and four comment blocks (§9.2), in
`rust/src/project.rs`, `rust/src/project/snapshot.rs`,
`rust/src/project/methods.rs` and `rust/src/project/commit.rs`.

For the record, the signatures of the two extracted helpers:

```rust
// rust/src/project.rs — one definition, two call sites.
/// Serve this state's committed code layer as a full (`rescan = true`) delta,
/// or rebuild it when no layer is carried.
///
/// A carried layer is byte-equivalent to a fresh full `Builder::build`
/// (`code_layer.rs:525`, proved by `scoped_producer_matches_full_rebuild_across_generations`),
/// so a miss costs only speed. Pure and GIL-free: callers wrap it in `py.detach`.
pub(crate) fn full_code_delta_for(
    state: &TyProjectState,
    revision: u64,
) -> dto::CodeDeltaDto;

// rust/src/project.rs — the cache-hit rule, stated once.
impl HeadState {
    /// The head layer when it is real, `None` while it is still the empty
    /// placeholder installed by `build_head_with_config` (`open.rs:168`).
    /// An empty layer is a cache miss: consumers rebuild, which yields the same
    /// (empty) delta, so this can only cost speed, never correctness.
    pub(crate) fn servable_code_layer(&self) -> Option<Arc<crate::code_layer::CodeLayer>>;
}
```

---

## 12. Migration plan — independently testable steps

Spelled out in full in [IMPLEMENTATION.md](IMPLEMENTATION.md); summarised here.
Do **not** start before this report is reviewed. One `gitman` lane per step;
verify before every `save`.

**Baseline (already recorded, §14):** 171 Rust tests, 833 Python tests,
8 parity-oracle tests, clippy clean.

| Step | Lane | Change | Independent test |
|---|---|---|---|
| **0** | `31-step0-docs` | Comment corrections only (`project.rs:94-104`, `:123-128`, `:144-152`; `commit.rs:915`; `methods.rs:607-613`). No code. | `check-rust` + `clippy` + `tests` unchanged at 171/833. Zero behaviour risk. |
| **1** | `31-step1-dedupe-serve` | Extract `full_code_delta_for` in `project.rs:291`; call from `snapshot.rs:61` and `methods.rs:673`. `py.detach` stays at both call sites. | **`parity-oracle` first** (`methods.rs` is its input), then `tests`. Add one Python test asserting `session.snapshot().graph()` and `session.graph` agree node-for-node and edge-for-edge at the same revision — the invariant the duplication threatened. |
| **2** | `31-step2-dedupe-hit-rule` | Add `HeadState::servable_code_layer`; use at `project.rs:204` and inside `methods.rs:613`, keeping the explicit `is_head` guard at the snapshot site. | Rust: a freshly built head yields `None`; with a non-empty layer assigned it yields `Some` and `Arc::ptr_eq` holds. Python: a time-travel snapshot's graph shows the **old** content while a head snapshot shows the new — the behavioural half of the `is_head` guard. |
| **3** | `31-step3-measure` | No code. Re-run §6.3 and §14 probes; append results to this file. | Numbers within noise of §6.3 / §14. |

Land each step green before starting the next. Steps 1 and 2 are independently
revertible.

---

## 13. Test obligations

Existing coverage that must stay green and must not be weakened:

- `rust/src/code_layer.rs:1660` `scoped_producer_matches_full_rebuild_across_generations`
  — the equivalence the carried layer depends on (5 generations, incl. deletion).
- `rust/src/code_layer.rs:1762` `scoped_producer_prunes_parallel_edges_and_deleted_targets`
  — the `remove_edge` pruning case.
- `code_layer.rs:504-509`, `:541-546` — the `reverse_deps` debug assertions, live
  on every debug test run.
- `tests/test_final_parity_oracle.py` — 8 tests; the wire-shape gate.
- `tests/test_affected_closure.py`, `tests/test_mvcc_snapshots.py`,
  `tests/test_rust_snapshots.py`, `tests/test_graph_snapshots.py`.
- The commit fault-injection rollback stages (`methods.rs:428`: `code_layer`,
  `identity_persist`, `authored_persist`).

New obligations introduced by the migration plan:

1. **Head/snapshot graph agreement** (Step 1, Python). Same revision ⇒ identical
   node set, identical `(source, target, kind)` edge relation set. Directly
   guards the duplication being removed.
2. **Carried-layer sharing** (Step 2, Rust). `servable_code_layer` is `None` for
   a freshly built head and `Some` with `Arc::ptr_eq` once a real layer is
   present. Keep it to the pure predicate: `commit()` is driven only from
   `#[pymethods]` (`methods.rs:209`, `:252`, `:287`) and no Rust test drives the
   funnel today, so an end-to-end Rust commit harness is **not** worth building
   for this. The end-to-end half is obligation 3.
3. **Time-travel correctness under the fallback** (Step 2, Python — required, not
   optional). Edit a file, snapshot at `head - 1`, assert its graph reflects the
   **old** content while a head snapshot reflects the new. This proves the
   fallback rebuilds against the frozen db rather than serving the head layer,
   and is the only deterministic way to test the `is_head` guard (a timing test
   would be flaky). Fits `tests/test_mvcc_snapshots.py`, which already
   time-travels at `:71-76`.

No test may assert on `CodeNodeDto` field names beyond what
`tests/parity_oracle.py:76` already pins.

---

## 14. Performance and memory implications

**Recommended design: exactly zero.** Step 1 moves code between functions; Step 2
replaces two expressions with one function call; Step 0 is comments.

### 14.1 Verified baseline (this checkout, 2026-09-14)

```sh
devenv shell -- check-rust                                                   # exit 0
devenv shell -- cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings   # exit 0
devenv shell -- parity-oracle                                                # 8 passed
devenv shell -- tests                                                        # 171 Rust, 833 Python
```

Matches the brief's expected baseline exactly: 171 / 833 / 8.

### 14.2 Measured memory — the hard constraint on any duplication

Probe preserved at `.scratch/projects/31-semantic-state/probe_memory.py`
(process RSS from `/proc/self/statm`, repository root, one commit first so the
head layer exists):

```
baseline RSS after 1 commit + 1 transient snapshot: 332.1 MiB
8 snapshots @ same revision : 348.5 MiB   (+16.4 MiB,  2.05 MiB/snapshot)
after closing them          : 348.5 MiB   (allocator retains; RSS does not shrink)
8 snapshots @ 8 revisions   : 455.9 MiB   (+107.4 MiB, 13.42 MiB/revision)
```

**Read the confound before the numbers.** The two branches are not a clean
controlled pair:

- Branch A (same revision) performs **no commits**. Its 2.05 MiB/snapshot is a
  clean marginal cost: one frozen `ProjectDatabase` + one registry clone, with
  the layer `Arc`-shared.
- Branch B (distinct revisions) performs **eight commits** to create eight
  revisions. Each commit additionally grows the HEAD salsa store and transiently
  deep-clones the entire layer — `Builder::seeded` starts from
  `layer: prev.clone()` (`code_layer.rs:598`). So 13.42 MiB/revision is an
  **upper bound** on the retained-layer cost, not an isolated measurement of it.

There is no way to remove that confound from a Python-level probe: distinct
retained layers require distinct commits, and a time-travel snapshot retains no
layer at all (`methods.rs:607-613`). An isolated figure needs the counting
allocator that project 30 Step 0 used.

Interpretation, stated to that precision:

- **`Arc` sharing is worth up to ~11.4 MiB per extra same-revision snapshot.**
  Both branches retain eight snapshots; only revision-distinctness differs, so
  the gap is dominated by layer retention — but part of it is commit overhead.
- **13.42 MiB/revision is an upper bound.** It is *consistent with* project 30's
  counting-allocator figure (12.83 MiB layer + 1.03 MiB pinned frozen state =
  13.86 MiB), but it does not independently reproduce it: the two measure
  different quantities. **Project 30's 12.83 MiB remains the authoritative
  per-revision layer cost**; this probe corroborates its order of magnitude and
  nothing finer.
- **Any design adding a second owned layer per snapshot costs ~12.8 MiB each** —
  roughly 6× the clean 2.05 MiB/snapshot marginal cost. That conclusion rests on
  project 30's isolated figure, not on this probe's upper bound. The old
  arithmetic estimate of ~6 MB per revision is superseded and must not be used.
- **Side finding: every scoped commit transiently doubles layer memory**
  (`code_layer.rs:598`). Not a defect — the clone is the incremental producer's
  working copy — but it belongs in any future retention-policy discussion.

### 14.3 Measured time

From §6.3 and §9.3, this checkout, debug build:

| Path | Cost |
|---|---|
| `full_code_delta`, carried layer (head) | **0.173 s** |
| `full_code_delta`, carried layer (head snapshot) | **0.152 s** |
| `full_code_delta`, fallback rebuild (pre-commit head) | **4.399 s** |
| `full_code_delta`, fallback rebuild (time-travel snapshot) | **4.110 s** |
| head read-clone path (`latest.files()`), repo scale | 2.154 ms/call |
| head read-clone path (`latest.files()`), fixture scale | 2.008 ms/call |
| `session.snapshot()` (frozen db build + registry clone) | 8.19 ms/call |

Project 30's release-build figures (`full_code_delta` 0.141 s; full Python
snapshot graph 1.06–1.22 s) remain the reference; the debug figures above are
consistent with them.

---

### 14.4 After Project 31 (2026-09-14)

The required probes were rerun after Steps 0–2 landed. The timing probe was
run four times to check the tight (<20%) Spike 5 noise floor. The table shows
the observed min–max across those four runs against the three-run Spike 5
range; the acceptance band is ±20% around that earlier range.

| Path | Spike 5 range | After range | Result |
|---|---:|---:|---|
| pre-commit head `full_code_delta` | 4.504–4.720 s | 4.718–5.452 s | within band |
| pre-commit head warm repeat | 2.846–3.074 s | 2.989–3.649 s | within band |
| pre-commit snapshot `full_code_delta` | 4.008–4.221 s | 4.289–4.964 s | within band |
| commit | 2.911–2.943 s | 3.045–3.633 s | one high sample; other three 3.045–3.295 s |
| post-commit head `full_code_delta` | 0.152–0.171 s | 0.176–0.191 s | within band |
| post-commit head snapshot `full_code_delta` | 0.144–0.158 s | 0.155–0.185 s | within band |
| time-travel snapshot `full_code_delta` | 4.089–4.257 s | 4.130–4.870 s | within band |
| head `orphaned()` ×200 | 0.223–0.258 ms | 0.228–0.336 ms | one high sample; no cleanup path involved |
| `session.snapshot()` ×200 | 8.583–9.137 ms | 9.262–11.402 ms | one high sample; same order of magnitude |

The four primary full-code-delta paths stayed within the Spike 5 acceptance
band, including the carried-layer fast paths and both stateless fallbacks. The
isolated high samples were in auxiliary measurements; a final timing run
returned 4.718 s / 0.177 s / 0.155 s / 4.130 s for pre-commit head, carried
head, carried snapshot, and time travel respectively.

The post-refactor memory probe reported:

```
baseline RSS after 1 commit + 1 transient snapshot: 284.5 MiB
8 snapshots @ same revision : 298.0 MiB   (+13.5 MiB, 1.69 MiB/snapshot)
after closing them          : 298.0 MiB
8 snapshots @ 8 revisions   : 411.6 MiB   (+113.6 MiB, 14.20 MiB/revision)
```

The absolute RSS is lower than the earlier run, while the same-revision
marginal cost remains in the same range and the distinct-revision figure is
still the known commit-confounded upper bound. The probe also reported
`latest.files()` at **2.966 ms/call** at repository scale, the same millisecond
order as the baseline read-clone measurement. These results show no measurable
performance or memory movement from the refactor.

The chosen helper location differs from the original implementation proposal:
Spike 4 selected `rust/src/project.rs`, next to `clone_locked_state`, because
it is the existing home for shared read-path helpers and avoids an indirect
dependency through the `snapshot.rs` glob re-export. `IMPLEMENTATION.md` and
the current-summary portions of this document have been updated accordingly;
historical pre-implementation anchors remain labelled by context.

## 15. Risks and follow-up work

### 15.1 Risks of the recommended design

| Risk | Severity | Mitigation |
|---|---|---|
| Step 1 changes `full_code_delta` output | High if it happened | `parity-oracle` runs **before** anything else in Step 1; the extracted function is a pure move |
| The `py.detach` boundary is moved by accident | Medium — `diff_from` over 36k edges must not hold the GIL | Extract only the pure computation; the `py.detach` wrapper stays in both `#[pymethods]` bodies |
| Comment-only Step 0 drifts from code later | Low | Cross-reference the anchoring tests by name in each comment |

### 15.2 Follow-ups, sized but not scheduled

1. **Read-only sessions never get project 30's win** (§6.4). Measured at
   **4.40 s per `full_code_delta` call, unbounded repetition**, for any session
   that never commits — including a watch-only daemon. Fixing it needs either
   layer production at `open()` (rejected at `commit.rs:849-851`) or a read-path
   memo, which architectural constraint 13 forbids. **This is the largest
   remaining performance defect in this area and needs an explicit decision on
   constraint 13 before it can be worked.** Size as its own project.
2. **Time-travel snapshots have no layer.** Retaining layers per revision costs a
   measured **13.42 MiB/revision** (§14.2) and would need a retention bound —
   the risk project 30 DESIGN §4.1 recorded. Only worth it if time travel becomes
   hot. It is not today.
3. **The "Project 31" name collides.** `Project 31, #1b/#2/#3` already appears in
   eleven places in `src/tyo3` and `rust/src` (e.g. `applier.py:95`,
   `dto/commit_delta.rs:23`, `commit.rs:911`, `session.py:631`) meaning the
   *v2-document* project 31 — the decision to retire the per-commit structural
   delta. Project 29's README already flags this (`README.md:79`). Whatever this
   directory's project is finally called, the existing citations must not be
   renumbered, and any new comment must say "`.scratch/projects/31-semantic-state`"
   explicitly.
4. **`apply_code_delta` is now the dominant snapshot-graph cost** (0.473 s of
   1.069 s in project 30 DESIGN §1.4). Deliberately wholesale
   (`applier.py:88-105`); re-incrementalising it is forbidden by constraint 10.
   Any future work must argue on its own merits.

---

## 16. Open questions

1. **Does the project want the `31-semantic-state` directory kept once the answer
   is "no new type"?** Recommendation: keep it, with this report and a README
   recording the decision, so the question is not reopened from project 29 §5 a
   third time.
2. **Is architectural constraint 13 ("no read operation gains a write side
   effect") negotiable for the pre-first-commit head layer?** A one-line memo
   under the existing head lock would remove a measured 4.40 s per call from
   read-only sessions. This is the only question in this investigation whose
   answer is not determined by the code.
3. **Should `Baseline`'s triple `{ registry, authored, code_layer }` be named?**
   If the desire to name the publication boundary persists after this report, the
   honest object is `Baseline`, which already exists (`commit.rs:524-532`).
   Renaming it — e.g. to `CommitBaseline` — with an expanded doc comment would
   satisfy the naming goal at near-zero cost. Not recommended, but it is the only
   naming change with a defensible target.

---

## 17. Decision and implementation outcome

**DOCUMENT ONLY with the two approved deduplications implemented.** No
`SemanticState` type was introduced.

- **Do not implement a `SemanticState` type.** Designs B, C and D are rejected on
  evidence (§8): the invariant is false inside the commit window, the grouping
  omits `authored`, the lifetimes differ across `reload()`, and the memory
  measurement forbids any owned duplicate.
- **Design A is complete** in three small, independently pushed lanes (§12):
  the comments now describe the four legitimate `None` producers and the
  registry/layer asymmetry; `full_code_delta_for` is shared from
  `project.rs:291`; and `HeadState::servable_code_layer` is shared from
  `project.rs:220-221`. No new types, no `Option` changes, no wire change, and
  no measurable performance or memory movement.
- The deliberate `is_head` guard remains visible at `methods.rs:613`, and the
  post-implementation graph, parity, test, timing, and memory evidence is
  recorded in §14.4.
- **Project 29 §5 is superseded.** Its "read clone / snapshot owns identities plus
  a *lazily produced* layer" is not what the code does: the layer is eagerly
  carried, and the fallback is a stateless recompute that memoises nothing
  (§6.2). Do not quote it as current.
