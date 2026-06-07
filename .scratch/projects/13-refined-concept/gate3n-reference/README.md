# Gate 3N — Reference Artifacts

> **Recovered from:** `gate1-content-store` branch (commit `d9c2179`, `7b59c68`)  
> **Status:** Not imported into the active codebase. Reference only.

These six files are the complete Gate 3N work — a proposal to re-home code-layer
delta production from Python into the native Rust commit. They were partially
implemented but never compiled or tested, and the project subsequently pivoted to
implement Gates 5–8 directly on top of the existing Python code-graph
architecture.

For a detailed analysis of each file and why it was left behind, see the parent
directory's [`GATE_3N_RECOVERY_ANALYSIS.md`](./GATE_3N_RECOVERY_ANALYSIS.md).

## Contents

| File | Description |
|------|-------------|
| `GATE_3N_NATIVE_CODE_DELTA_GUIDE.md` | Full 8-step implementation guide |
| `GATE_3N_QUICK_IMPLEMENTATION_REPORT.md` | Handoff report: what was done, what was deferred, risk register |
| `code_layer.rs` | Rust `CodeLayer` data model + delta assembly (uncompiled) |
| `code_delta.rs` | Wire contract DTOs: `SymbolNodeDto`, `EdgeDto`, `CodeDelta` |
| `test_code_delta_apply.py` | Pure Python tests for `CodeGraph.apply_code_delta()` |
| `test_native_code_delta.py` | Integration parity tests against native `code_delta_full()` |

## Warning

These files target types and module structures that **no longer exist** on the
current branch (`Entity.file`, `Entity.range`, `HeadState.code_layer`, etc.).
They will not compile and their tests will not run. They are here for context
only — if Gate 3N is resurrected, start from the guide and rewrite against the
current codebase.
