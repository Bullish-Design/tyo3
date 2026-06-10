# DESIGN — `needs_review` durability (E + A)

## 1. The mechanism today (verified)

`needs_review` lives as a status on the identity **anchor** in the Rust registry
and is recomputed **per commit by comparing against the immediately prior
revision** — not against the state when the note was authored.

`rust/src/identity.rs::reconcile_impl`, Pass A (exact-path match):

```rust
// rust/src/identity.rs (~666)
let old_hash = registry.get(&id).content_hash;   // hash from the PRIOR revision
if old_hash != e.content_hash {
    needs_review.push(id.clone());               // changed THIS commit → flag
}
...
// apply phase (~788)
registry.rebind(id, …, e.content_hash, revision);     // anchor.content_hash := new
if !needs_review.contains(id) {
    registry.set_status(id, IdentityStatus::Active);  // unchanged this commit → CLEAR
}
...
// (~810) apply NeedsReview for the flagged ids
for id in &needs_review { registry.set_status(id, IdentityStatus::NeedsReview); }
```

So the registry's `IdentityStatus::NeedsReview` means **"this entity's body
changed in the most recent commit that reconciled its file."** It is
edge-triggered and auto-clears on the next reconcile that re-settles the entity.

Reads built on it:
- `rust/src/project/methods.rs:753 needs_review()` — filters anchors by
  `status == NeedsReview`.
- `rust/src/project/commit.rs:259 derive_authored_status()` — maps the anchor
  status to `authored().status` (`Active→present`, `NeedsReview→needs_review`,
  `Orphaned→orphaned`).
- `CommitDelta.needs_review` (`commit.rs:340`) — the per-commit flagged ids,
  broadcast on the bus.

### 1.1 Verified failure modes

| Action | Effect on a flagged `show_label` | Why |
|---|---|---|
| Edit `show_label` body | flags ✅ | hash changed vs prior rev |
| `:w` re-commits identical text | **clears** ❌ | hash unchanged vs prior rev → `set_status(Active)` |
| Edit a *different* function in same file | **clears** ❌ | scoped reconcile re-settles `show_label` (unchanged) → Active |
| Re-author the note ("reviewed") | **stays flagged** ❌ | `Author` mutation is `reconcile:false` — never touches the registry status |

Reproduced via `TyO3Session` (single session, deterministic): edit→`True`,
identical re-edit→`False`, edit-other-func-same-file→`False`, re-author→`True`.

The "re-author doesn't clear" row is the tell: **there is no intentional approve
path.** The only things that clear the flag are incidental reconcile
side-effects. The authored record (`rust/src/authored.rs::AuthoredVersion`)
stores only `{value, revision}` — **no content hash from author time** — so the
engine currently cannot answer "does the body differ from when this note was
written?"

## 2. The reframe

- The engine's `needs_review` is an **edge signal** — perfect for a bus
  notification ("a noted entity just changed").
- The Phase-2 layer diagnostic, `authored().status`, and the tour treat it as
  **level state** ("stale until a human re-approves").

The mismatch is the bug. The fix introduces real level state, anchored to the
body at author time, and keeps the edge signal for the bus.

## 3. Part E — stop the redundant double-commit (plugin)

### Problem
`editors/tyo3.nvim/lua/tyo3/init.lua` commits the same overlay text twice:
- `on_text_changed` (TextChanged/TextChangedI) → debounced `sync_now` (`:132`).
- `:w`/BufWritePost → `sync_now` again with identical text.
Both call `sync_buffer` with the same bytes → an identical re-commit, which under
today's semantics clears `needs_review`.

### Design
Dedup at the single sync choke point on a per-buffer content hash.

- Add `M._last_synced = {}` (bufnr → sha256 of last-synced text).
- Introduce/extend the sync helper so **all** sync paths funnel through it:
  - compute `text = buffer_text(bufnr)`;
  - `local h = vim.fn.sha256(text)`;
  - if `h == M._last_synced[bufnr]`, **skip** the rpc (no commit);
  - else issue `sync_buffer`, and on success set `M._last_synced[bufnr] = h`.
- Seed `M._last_synced[bufnr]` in `on_buf_enter` after the initial sync so the
  first `:w` of an unedited buffer is a no-op.
- Clear the entry on buffer unload (optional; a stale entry only risks a missed
  *first* dedup, never a wrong sync).

`buffer_text` (`init.lua:71`), `on_buf_enter` (`:77`), `on_text_changed` (`:102`),
`sync_now` (`:132`) are the call sites. Keep `sync_buffer` itself unchanged.

### Pros / cons / opportunities
- **Pros:** removes the edit-then-save clear; one fewer commit/reconcile per save
  (real perf win); pure Lua, no engine touch.
- **Cons:** necessary-but-not-sufficient — editing a *different* function in the
  same file still clears under today's semantics (that's what A fixes).
- **Opportunity:** general cleanliness of the write path; fewer bus deltas.

### Test (headless)
`editors/tyo3.nvim/tests/` — author a note on `show_label`, edit its body via the
debounced sync (flags), then trigger a save-equivalent `sync_now` and assert the
**revision did not advance** (deduped) and `needs_review` still contains the id.
(Even on today's engine, E alone makes the save case pass; the same test should
keep passing after A.)

## 4. Part A — anchor review-state to the author-time hash (engine)

### Core idea
Record the entity's content hash **at author time** on the authored record, and
define review-state as a **level** comparison against the current anchor hash.

### 4.1 Data model
`rust/src/authored.rs`:
- Add `reviewed_hash: Option<ContentHash>` to `AuthoredVersion` (serialized as a
  hex string; `#[serde(default)]` so v1 records load with `None`).
- Bump `AUTHORED_FORMAT_VERSION` `1 → 2` (`authored.rs:15`). `from_bytes` already
  rejects *newer* versions; v1 stays loadable (the new field defaults to `None`).

### 4.2 Stamping at author time
The `Author` mutation (`rust/src/project/methods.rs:307 author()` →
`commit.rs` `Mutation::Author`, `reconcile:false`) must capture the current
anchor hash for `id` and put it on the new `AuthoredVersion`:
- Look up `head.registry.get(&id).map(|a| a.content_hash)` for the durable id
  (the entity's current committed body). Stamp it as `reviewed_hash`.
- If the id has no anchor yet (note authored before the entity exists), stamp
  `None` — it baselines on the first reconcile that binds the id (see §4.5).
- This flows through `AuthoredStore::put` (`authored.rs:122`) which already builds
  the `AuthoredVersion`; thread `reviewed_hash` into the version it stores.

Re-authoring therefore **re-stamps** `reviewed_hash = current` → the note is
"current" again. That makes **re-authoring the acknowledge action** (intuitive,
and the gap from §1.1 closes for free).

Optional nicety: an explicit `approve(layer, id)` verb that re-stamps
`reviewed_hash = current` **without** changing `value` (and without bumping the
authored history). Recommended as a small follow-on, not required for A.

### 4.3 Level-triggered read (the new source of truth)
Replace the registry-status basis for review-state with a hash comparison.

`needs_review()` (`methods.rs:753`) becomes: for every durable id that has a
record in a `review_on_change = true` authored layer (reuse
`compute_authored_lifecycle`'s "monitored" set, `commit.rs:209`), flag the id
when:
- the id has an **active** anchor (not orphaned), **and**
- `record.reviewed_hash` is `Some(h)` and `h != anchor.content_hash`.

`derive_authored_status()` (`commit.rs:259`) becomes:
```text
absent        if no record
present       if layer.review_on_change == false
orphaned      if registry status == Orphaned        (unchanged, registry-driven)
needs_review  if reviewed_hash.is_some() && reviewed_hash != current_anchor_hash
present        otherwise
```

Key consequence: **review-state is fully decoupled from the transient registry
`NeedsReview` status.** The `set_status(Active)` re-settle in `reconcile_impl`
(§1) becomes irrelevant to review-state — leave that code as-is (it still drives
Orphaned and is harmless), or, optionally, stop writing `NeedsReview` to the
registry entirely (see §4.6). Orphaned stays registry-driven (retire logic).

### 4.4 The bus edge-signal stays
`CommitDelta.needs_review` (`commit.rs:340`) should keep meaning "ids that
changed *this commit* and have a note on a review_on_change layer" — the "heads
up, a noted entity just changed" notification. The plugin already re-queries
`review_state` (→ the new level `needs_review()`) on every bus delta, so the
diagnostic stays correct without depending on the edge signal's value. Document
the two notions explicitly (edge in the delta; level in the query/status).

### 4.5 Migration / legacy records
v1 records load with `reviewed_hash = None`.
- **Recommended:** `None` ⇒ **not flagged** (treated as `present`). Conservative:
  avoids a wall of `needs_review` on upgrade. The baseline is established on the
  next author/approve of that note.
- **Optional enhancement (lazy baseline):** on first load/first reconcile that
  binds the id, stamp `reviewed_hash = current anchor hash` and persist as v2, so
  legacy notes start flagging on the *next* real body change. Cleaner long-term;
  implement only if cheap. Pick one and document it.

The synthetic shop project / tour build fresh, so their records are v2 from the
start — demo and tests are unaffected by the migration choice.

### 4.6 Optional simplification (decide during impl)
Once review-state is hash-anchored, the registry `IdentityStatus::NeedsReview`
has no remaining reader. You may either:
- leave it (minimal diff; it's a harmless per-commit scratch value), or
- remove `NeedsReview` writes from `reconcile_impl` and compute the
  `CommitDelta.needs_review` edge set directly from the changed ids ∩ monitored
  notes (cleaner; slightly larger diff). Recommend leaving it for A and noting
  the cleanup as follow-up.

### 4.7 Edge cases to cover (and assert)
- **Cosmetic edit** (whitespace; content hash unchanged): not flagged. ✅
- **Move** (same hash, new path): not flagged (hash unchanged). ✅
- **Revert**: edit body then restore it to the reviewed bytes → unflags
  (level-trigger; this is the key improvement over fallback B).
- **Edit different function, same file**: the flagged note **stays flagged**
  (the fix's headline correctness gain).
- **Save / identical re-commit**: stays flagged.
- **Re-author**: clears (acknowledge).
- **Orphaned**: takes precedence over needs_review; unchanged.
- **`review_on_change = false` layer**: always `present`; never flagged.
- **No anchor yet** (note before entity): `None` baseline → present until bound.
- **Multiple authored layers on one id**: each record carries its own
  `reviewed_hash`; status is per (layer, id). `needs_review()` (id-level) flags
  the id if *any* monitored layer's record is stale.

### 4.8 Pros / cons / implications / opportunities
- **Pros:** correct, durable semantics; survives saves, same-file edits, restart,
  and other editors; cosmetic/move correctly ignored; re-author = acknowledge;
  single source of truth stays in Rust.
- **Cons:** larger change — format bump + migration; author path stamps a hash;
  two read sites change; broad test matrix. Must keep the edge/level distinction
  clear in docs and the bus.
- **Implications:** `authored().status`, `needs_review()`, the daemon
  `review_state`, the Phase-2 layer diagnostic, and the tour all become
  trustworthy with no changes at those layers (they read the now-correct values).
- **Opportunities:** a real `:TyO3Approve`; a **"diff since reviewed"** view (both
  hashes are known); per-layer review acknowledgement; the demo coda can use `:w`
  normally again and editing neighbours won't clear the flag.

## 5. Fallback B (documented, not chosen)
If the format migration must be deferred: guard the `set_status(Active)`
re-settle so the flag is **sticky** once set, and add an explicit approve/re-author
clear. Pros: tiny. Cons: over-flags on revert (no `reviewed_hash` to compare);
still needs an approve verb. B is a stepping stone to A, not a substitute.

## 6. Sequencing
1. **E** first (independent, low-risk, immediately fixes the save case; ship/verify
   on its own headless test).
2. **A** engine change (data model → stamping → reads → migration), `build`,
   then the Rust + Python test matrix (§4.7).
3. Re-verify the consumers: daemon `review_state` handler test, the Phase-2
   `tests/lsp_nav.lua` layer-diagnostic check, smoke/context/lsp.
4. **Opportunity pass (optional, can defer):** restore `:w` in the default tour
   coda and simplify the forced-refresh nudge now that saves don't clear; add
   `:TyO3Approve`; "diff since reviewed".

## 7. Test matrix (engine, A)
Add to the Rust identity/authored tests **and** a Python `TyO3Session` test:
- edit → flagged
- save / identical re-commit → still flagged
- edit different func, same file → still flagged
- revert body → unflagged
- re-author note → unflagged
- cosmetic edit → never flagged
- move (same hash) → never flagged
- `review_on_change=false` → never flagged
- orphaned precedence
- v1 record loads (no `reviewed_hash`) → present (per §4.5 choice)
Daemon: extend `src/tyo3/daemon/tests/test_handlers.py::review_state` to assert
persistence across a save-equivalent second commit.

## 8. Risks
- **Hash source mismatch:** the `reviewed_hash` must be the *same* content hash
  the registry anchor uses (`Anchor.content_hash`), not a re-derived one. Stamp
  from the registry anchor, never recompute.
- **Format migration:** ensure v1 round-trips (load → save) without data loss and
  that the `#[serde(default)]` + version bump is exercised by a test.
- **Edge/level confusion:** keep `CommitDelta.needs_review` (edge) and
  `needs_review()`/`status` (level) clearly named and documented, or future
  readers will reintroduce the bug.
- **`reconcile_scoped` scope:** confirm the level read doesn't depend on scope —
  it reads anchor hash + record, both revision-stable, so it's scope-independent
  (a property the edge trigger lacked).
