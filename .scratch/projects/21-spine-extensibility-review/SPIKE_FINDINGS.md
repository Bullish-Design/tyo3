# SPIKE_FINDINGS — empirical resolution of the review's open questions

> Throwaway spikes run against the real engine + daemon (built `_native_impl.abi3.so`)
> on temp projects in `/tmp`, via `devenv shell -- python`. **No engine/plugin code
> was modified** — the probes only *use* the public surface. Scripts:
> `spikes/run_spikes.py`, `spikes/spike_c2.py`, `spikes/spike_gen.py` (all
> throwaway, untracked under `.scratch/`). Re-run:
> `devenv shell -- python .scratch/projects/21-spine-extensibility-review/spikes/run_spikes.py`
>
> These supersede the "open questions" in the first-pass `ASSESSMENT.md`/`API_DESIGN.md`.
> Two results **change prior recommendations** (flagged ⚠).

## Summary table

| Spike | Question | Verdict |
|---|---|---|
| A | Can a custom authored + derived layer be added via config alone? | **YES — zero core files.** Confirmed end-to-end. |
| A⁻ | Is there a programmatic registration API? | **NO.** `register_layer`/`tyo3.extend` absent. |
| B | Does `entity_at`'s card auto-include arbitrary new layers? | **YES** — via the real daemon path. |
| C | Is a callee's reverse-dep ("references") layer invalidated when a new caller appears? | **NO — stale-forever** (the AB2 correctness wall). ⚠ |
| D | Does "rename via atomic move" preserve the durable id (QW2)? | **NO** — rename always mints a new id. ⚠ |
| E | Does `find_references` return callers (is a references-layer content-possible)? | **YES.** |
| F | What derived status does the card emit (panel render-bug)? | **`fresh`, never `present`** — the panel/compact renderers drop it. |

---

## Spike A — custom layers via config alone (the headline, confirmed)

A temp project with **no engine changes**, a `.tyo3/config.toml` declaring a new
authored layer `tests` and two new derived layers `complexity`/`refs` (python
generators referenced by `callable = "spike_gen:..."`), and an importable
`spike_gen.py`. Raw output:

```
declared layers (validated native config): ['complexity', 'refs', 'tests']
  tests:      origin=authored  kinds=()           key_locality=local
  complexity: origin=derived   kinds=('function',) key_locality=local
  refs:       origin=derived   kinds=('function',) key_locality=semantic
authored read-back: status=present value={'n': 2, 'paths': ['tests/test_lib.py::test_target']}
derived complexity:  status=fresh   artifact=b'{"score": 2}'
```

`author("tests", id, {...})` committed and read back; `derived("complexity", id)`
computed through the python generator. **The only files I authored were the config
and a generator module** — nothing under `rust/src` or `src/tyo3`. This is the
strongest evidence for ASSESSMENT §4: declaring + producing + reading a brand-new
attachment is config-only **as long as you reuse a built-in generator type and
store backend**.

**Negative (the gap):**
```
hasattr(tyo3, 'register_layer')     = False
hasattr(tyo3, 'register_generator') = False
import tyo3.extend -> ModuleNotFoundError: No module named 'tyo3.extend'
```
There is no programmatic registration path — exactly the API_DESIGN target.

## Spike B — card auto-inclusion (confirmed, via the real daemon)

Instantiated the real `SessionActor` + `Handlers` and called `entity_at`:

```
card.authored keys = ['tests']
card.derived  keys = ['complexity', 'refs']
card.authored['tests'] = {'value': {...}, 'status': 'present', 'revision': 3}
card.derived['complexity'] = {'artifact': '{"score": 2}', 'status': 'fresh'}
open() layers list = ['complexity', 'refs', 'tests']
```

The new layers ride the card with **zero handler changes**. ASSESSMENT §6's
"`entity_at` already generalizes" is now empirical, not inferred.

## Spike E — `find_references` returns callers (references-layer is content-possible)

```
find_references(target) -> 3 refs:
  lib.py 1:5     (the def)
  app.py 1:17    (the import)
  app.py 5:12    (the call site)
```
So the *content* of a references/callers layer is available from the engine
(reachable in Python via `_ReadOps.find_references`). The blocker is not "can we
compute callers" — it's invalidation (Spike C) and the missing producer-snapshot
access (ASSESSMENT §4.3 / API_DESIGN §2.4).

## Spike F — derived status is never `present` (panel render-bug, confirmed)

```
derived status literal = 'fresh'   (panel.lua:169 / card.lua:124 gate on == 'present')
```
Derived statuses are `fresh|stale|failed|absent` (`models/derived.py:23`). The
panel SUMMARY pane and compact context card render only `status == "present"`, so
they drop **every** derived artifact today. Confirms ROADMAP QW6. (The
`:TyO3Inspect` float is correct; inline summaries use the separate `decorate` path.)

---

## ⚠ Spike C — the reverse-dependency invalidation wall (RESOLVES API_DESIGN open Q2)

Setup: `lib.py: def target(...)`, `app.py: def caller(): target(5)` — `caller`
depends on `target`. A `refs` derived layer (`key_locality="semantic"`) sits on
the callee. Observations:

```
[edit callee body] lib.py
  changed_ids  = [target]
  affected_ids = [caller, target]      # affected = the change's DEPENDENTS
  caller in affected? True             # caller depends on callee → affected ✓

[add a new caller] app.py
  changed_ids  = [caller]
  affected_ids = [caller]              # ONLY caller
  callee in affected_ids? False        # <-- target is a DEPENDENCY of caller, not a dependent
  refcount-generator invocations = 0   # callee's refs layer NOT recomputed

[add a new caller in a NEW file] app2.py
  created_ids  = [other]
  affected_ids = []                    # nothing depends on the new fn yet
  callee in affected_ids? False
```

And the read-path follow-up (`spike_c2.py`):
```
[read #1 of refs(callee)]                    recomputes = 1   (cold)
[read #2 of refs(callee) after new caller]   recomputes = 0   (cache hit → STALE)
```

**Conclusion — the correctness wall is real and total.** The affected set is the
**dependents-closure** of a change; it never flows to a change's *dependencies*. A
layer whose value depends on an entity's *dependents* (references / callers /
"my subclasses" / "who implements me") is invisible to **all three** of today's
freshness mechanisms:
1. commit-time affected-driven invalidation (`session.py:_invalidate_derived`) —
   the callee is never in `affected_ids` when a caller changes;
2. forward-dep `semantic` fingerprinting (`dag.py:_dependency_fingerprint` walks
   `graph.dependencies`, i.e. *forward*) — a new caller doesn't change the callee's
   forward closure, so the cache key is identical;
3. read-time self-heal — same key ⇒ cache hit ⇒ no recompute.

So a `references` layer built naively today would be **stale-forever**. This is a
*correctness* wall, not a wiring one — the single most important spike result.

**Resolution — a taxonomy by cost × direction, not one enum value.** (Reasoned
through after the spike; recommended over the naive "just add reverse-semantic".)

| | Forward (depends on what I call) | Reverse (depends on who calls me) |
|---|---|---|
| **Cheap** | `local` ✅ today | **live RPC, not a layer** (references, diagnostics) |
| **Expensive** | `semantic` ✅ today | **traced read-set** keying (LLM-over-callsites) |

1. **Cheap reverse data should not be a cached layer.** The engine recomputes
   references/diagnostics in ms (Spike E); caching buys nothing and takes on the
   hardest invalidation in the system. **Serve via QW1 live RPCs** — this dissolves
   ~90% of Spike C at lowest effort, highest correctness.
2. **Expensive-reverse layers key on a *traced read-set*, not a direction enum.**
   The recording `ProduceContext` (API_DESIGN §2.4) logs every id the producer
   touches (`find_references`, `symbol`, graph walks) and the framework fingerprints
   *exactly that set* — correct for forward/reverse/sibling/mixed **by
   construction** (salsa's model; ty/salsa is the engine). A lazy read self-heals
   when any traced id changes; no enum to get wrong.
3. **`reverse-semantic` (direct, lazy, memoized) only as an interim** if (2) slips:
   fingerprint *direct* reverse edges (the maintained `reverse_deps` index), **not**
   the transitive cone (`graph.transitive_dependents`) which thrashes on hot symbols;
   lazy serving only (no eager reverse); memoize per snapshot; dirty `deleted_ids`'
   deps too.

**Avoid** the "expand the commit dirty-set with deps-of-each-changed-id"
invalidator hack as the primary mechanism — it over-fires and gives no lazy
self-heal. Folded into ROADMAP AB2/QW1 and API_DESIGN §2.1/§2.4/§5.2/§5.2b/§5.3/§8.

## ⚠ Spike D — identity matrix (CORRECTS ROADMAP QW2)

```
[cosmetic edit]   id same? True   note: present        (binds 'changed', hash unchanged)
[body change]     id same? True   note: needs_review   (binds 'changed', hash changed)
[rename in place] id same? False  note @old: orphaned, @new: absent
                  delta: created=[new] deleted=[old] changed=[] moved=[]
[atomic move]     id same? True   note: present         (binds 'moved')
                  delta: moved=[(id, lib.py, moved.py)]
[QW2 rename+move atomically] id same? False  note @old: orphaned, @new: absent
                  delta: created=[new] deleted=[old] changed=[] moved=[]
```

**Mechanism (confirmed in `hash.rs`).** `visit_identifier` (`hash.rs:337`)
"Captures def/class names" — the **entity name is part of the content hash**. So:
- *atomic move* keeps the name ⇒ content hash unchanged ⇒ `by_hash` matches at the
  new path ⇒ **`Moved`** ⇒ id + note ride. ✓
- *rename* changes the name ⇒ content hash changes **and** qualified_path changes;
  rule-1 `Exact` needs the same path, rule-2 `Moved` needs the same hash, rule-3
  `Struct` needs the **same name** — **all three miss** ⇒ **`Minted`**. Note lost.

**⚠ This overturns ROADMAP QW2 as originally written.** Routing a rename through an
atomic `edit_many` does **not** preserve the id — a rename perturbs the exact two
keys reconciliation indexes (path, hash) *and* the one structural fallback (name),
so reconciliation cannot infer it by construction. Rename-survival therefore
requires an **explicit rename intent** carried from the editor (which knows it is
performing a rename) to a **native rebind** — a small `Mutation::Rename`/rebind
that re-keys the id deliberately — *not* a daemon-only quick win, and *not* a
matcher heuristic (which would risk mis-binding swapped names and break the
deterministic-matcher landmine §12). Reclassified in ROADMAP as an architectural
item with a small native change.

The rest of the matrix confirms the memory `durable-identity-binding-rules`:
edit ✅, atomic move ✅ (note rides), rename ❌. New nuance: a *meaningful* body
change flags the authored note `needs_review` (honest staleness), while a cosmetic
edit leaves it `present`.

---

## Net effect on the deliverables

- ASSESSMENT §2 — add the name-in-hash mechanism + the identity matrix evidence.
- ASSESSMENT §4/§5 — add the reverse-dep invalidation wall (Spike C) + taxonomy.
- ASSESSMENT §6 — Spike F upgrades the render-bug from "latent" to "confirmed".
- ROADMAP **QW2 corrected** → AB8 "explicit rename intent + native rebind"
  (architectural, small native change); **AB2** gains the cost×direction taxonomy:
  cheap-reverse → live RPC (QW1), expensive-reverse → traced read-set keying,
  `reverse-semantic` only as an interim.
- API_DESIGN **open Q2 RESOLVED** → traced read-set is the default key strategy
  (§2.4); `references`/`diagnostics` examples downgraded to live RPCs (§5.2/§5.3),
  a new expensive-reverse example added (§5.2b); `key_locality` demoted to an
  optional override.
