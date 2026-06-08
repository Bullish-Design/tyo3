# Phase 12 (V2) — Single config source

> Execution guide for **Phase 12 of `REFINED_IMPLEMENTATION_PLAN_V2.md`**. Read
> V1 §5.12 (fail loudly) once before starting. This is the V1 "Phase 10" work,
> extended to surface the new precision knobs added in Phase 9.
>
> **Depends on Phases 1–11.** Phases 6 (overflow validation) and 9 (precision
> knobs) added native config validation incrementally; this phase makes the native
> validated config the **single** source and deletes the Python re-read.

---

## 0. Dev environment

All commands via `devenv shell --`. **Native + Python** (`rust/src/config.rs`,
`src/tyo3/config.py`, `src/tyo3/session.py`). **Rebuild** after the Rust change.
Suites are slow.

---

## 1. What Phase 12 changes, and why

Rust loads and validates the config, but Python separately re-reads `config.toml`
for coordination settings and **falls back to defaults on any exception** (V1
§6.3 deviation #9), so invalid config is silently ignored and the two languages
can disagree. Make Python consume the native validated config as JSON and delete
the Python re-read.

### Files in scope

| File | Role |
|---|---|
| `rust/src/config.rs` | **edit** — emit the full validated config (coordination + precision + hash policy) as JSON to Python (12.2) |
| `src/tyo3/config.py` | **edit (core)** — consume the native JSON; delete the TOML re-read + default fallback (12.1) |
| `src/tyo3/session.py` | **edit** — `_read_coordination_config` deleted; settings come from the native config (12.1) |
| `src/tyo3/sidecar.py` | **review** — keep only as a lightweight path facade if still needed |
| config + sidecar tests | the gate (12.3) |

---

## 2. Working rules

1. **One authority: the native validated config.** Python never parses
   `config.toml` for semantics again.
2. **Fail loudly at open.** Invalid coordination/precision/hash config raises a
   typed `ConfigError` at open — never a silent default.
3. **Surface every coordination + precision setting** the Python side needs from
   the native JSON (bus capacity/overflow, watcher enabled/debounce,
   `code_graph.precision`, `code_graph.refinement`, hash policy).

---

## 3. Step-by-step

### Step 12.1 — Python consumes the native config; delete the re-read

Replace the Python TOML parse + `try/except → defaults` with a read of the native
validated config JSON. Delete `_read_coordination_config` (`session.py`) and the
duplicate parsing in `config.py`. Keep a sidecar path helper only if a non-config
path facade is still used.

**Verify.** `devenv shell -- pytest -q --no-cov -k config`

### Step 12.2 — Surface all settings natively

Ensure `rust/src/config.rs` emits coordination, precision (Phase 9), and hash
policy in the JSON it hands Python. Add validation for any not yet validated.
**Rebuild.**

**Verify.** `devenv shell -- build && devenv shell -- cargo test --manifest-path rust/Cargo.toml config`

### Step 12.3 — Loud failure at open

A config with an invalid overflow policy, precision value, or hash policy raises
`tyo3.exceptions.ConfigError` at `TyO3Session(root)` — proven by a test per knob.

> Commit here: `refactor(config): single validated config source; surface precision knobs`.

---

## 4. Acceptance

```bash
devenv shell -- build
devenv shell -- pytest -q --no-cov -k "config or sidecar" -rA
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml config sidecar
```

**Exit criteria:** invalid coordination/precision/hash config fails loudly at
open; Python no longer parses `config.toml` for semantics; config + sidecar tests
green.

## 5. Pitfalls

- **A surviving `try/except → defaults` in Python config.** The exact
  silent-fallback bug this phase removes. Grep for it.
- **Forgetting the rebuild** after the `config.rs` change.

## 6. Leaves for later

- Splitting config modules — Phase 13.
