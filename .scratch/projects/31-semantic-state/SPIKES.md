# Project 31 spikes

Run on 2026-09-14 from trunk `1d75f602f220a74f342db38d53ec5bd1af93354d` in
lane `31-spikes`. Baseline before the spikes: `check-rust` exit 0, clippy exit
0, parity oracle 8 passed, Rust 171 passed, and Python 833 passed.

## Spike 1 — duplicated serve path

Command:

```sh
diff <(sed -n '61,76p' rust/src/project/snapshot.rs) \
     <(sed -n '675,690p' rust/src/project/methods.rs)
```

Result: exit 0 with no diff output. The anchors still point to the two
`full_code_delta` bodies: `snapshot.rs:58-76` and `methods.rs:669-690`.

Conclusion: the premise holds; proceed with the extraction in Step 1.

## Spike 2 — head and snapshot graph agreement

Added `test_head_and_snapshot_graphs_agree_before_and_after_commit` to
`tests/test_graph_snapshots.py`. It asserts non-empty node sets and equal
`(source, target, kind)` edge relation sets for `session.graph` and
`session.snapshot().graph()` before and after one edit.

Command:

```sh
SECRETSPEC_REASON="Run Spike 2 regression test against trunk" \
  devenv shell -- tests tests/test_graph_snapshots.py -q
```

Result: exit 0. The full managed test command ran 171 Rust tests and 834
Python tests, all passed; the new test passed in both the pre-commit fallback
and post-commit carried-layer states.

Conclusion: no divergence was found, so this remains the Step 1 regression
test. The post-commit project-30 carried layer is consistent between the head
and snapshot accessors.

## Spike 3 — empty-layer miss predicate

Copied the repository `pyproject.toml` into a fresh temporary directory with
no Python files. The probe called `session.graph`,
`session.snapshot().graph()`, `snap.code.ids()`, and the corresponding native
full deltas before and after `session.sync_all()`.

Result:

```text
pre 0 0 0 0 []
pre_delta_equal True
commit_revision 2
post 0 0 0 0 []
post_delta_equal True
before_after_delta_equal True
```

No error occurred. The revision field was excluded from the before/after
comparison because a real commit advances it; all structural delta fields
were equal.

Conclusion: treating `CodeLayer::is_empty()` as a cache miss is benign for a
genuinely empty project. Keep `HeadState.code_layer` non-optional and document
the fallback as planned in Step 0.

## Spike 4 — shared helper location

Evidence: `rust/src/project.rs:239-251` already houses shared read-path
helpers, every project submodule imports the parent with `use super::*`
(`project/snapshot.rs:6`, `project/methods.rs:6`), and `project.rs:275-279`
re-exports sibling-module items through `pub(crate) use snapshot::*`.

Decision: put `full_code_delta_for` in `project.rs`, next to
`clone_locked_state`, rather than making a helper shared indirectly from
`snapshot.rs`. This is more discoverable for a helper used by two sibling
accessors and adds only a small amount to the already central shared-state
module; `project.rs` growing from 1161 lines is not a strong counter-signal.
The Step 1 type-check will verify both call sites before its tests are run.

Conclusion: this changes the proposed helper location, not the design or
behavior. No new type is needed.

## Spike 5 — timing noise floor

Command, run three times on unmodified trunk:

```sh
SECRETSPEC_REASON="Measure Project 31 timing noise floor" devenv shell -- \
  bash -c 'cd "$DEVENV_ROOT" && PYTHONPATH=src python \
  .scratch/projects/31-semantic-state/probe_timing.py .'
```

Measured min/max spread:

| Probe row | Min (s) | Max (s) | Relative spread |
|---|---:|---:|---:|
| Pre-commit head full delta | 4.504 | 4.720 | 4.8% |
| Pre-commit warm head full delta | 2.846 | 3.074 | 8.0% |
| Pre-commit snapshot full delta | 4.008 | 4.221 | 5.3% |
| Commit | 2.911 | 2.943 | 1.1% |
| Post-commit carried head full delta | 0.152 | 0.171 | 12.5% |
| Post-commit carried snapshot full delta | 0.144 | 0.158 | 9.7% |
| Time-travel snapshot full delta | 4.089 | 4.257 | 4.1% |
| Head `orphaned()` ×200 | 0.223 ms | 0.258 ms | 15.7% |
| `session.snapshot()` ×200 | 8.583 ms | 9.137 ms | 6.1% |

All rows are below the requested 20% band. Use the band as the Step 3
acceptance criterion. The memory probe's distinct-revision figure remains the
confounded upper bound documented in INVESTIGATION §14.2; it was not used as
the timing noise measurement.

## Decision

All five spikes are positive. No contingent type or behavior change is
required. The agreed three implementation lanes survive unchanged:

1. correct the four comment blocks;
2. extract the shared full-delta path and retain the graph regression test;
3. extract the shared servable-layer predicate, with the snapshot `is_head`
   guard left at its call site; then re-measure.
