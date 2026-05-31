# TyO3 Rust Backend Implementation — Detailed Review

**Document reviewed:** `.scratch/projects/02-tyo3-backend-wiring/RUST_BACKEND_IMPLEMENTATION.md` (v0.1.0, 2026-05-30)
**Reviewer:** AI coding agent
**Review date:** 2026-05-30
**Severity key:** 🔴 Blocker · 🟠 Major · 🟡 Minor · 🔵 Observation · ✅ Commendation

---

## Executive Summary

The guide is a **solid, well-structured plan** with a sensible layered architecture, strong traceability to the Allium specs, and a pragmatic phased approach. However, it contains **5 blockers** and **8 major issues** that will prevent a clean Phase 0 spike from succeeding as written. The blockers are all concentrated in three areas: (1) the `devenv.nix` configuration has a Nix syntax error and some toolchain redundancy, (2) the PyO3 module naming is inconsistent across three different places in the document, and (3) the conversion layer (`convert/`) is scaffolded in the file listing but completely undescribed — every DTO conversion call in `project.rs` references functions that don't exist. The major issues span coordinate conversion bugs, missing `py.allow_threads()` infrastructure, and the hover model definition.

If these are addressed before starting Phase 0, the spike and subsequent phases should proceed smoothly.

---

## Issue Register

### 🔴 B-01: `devenv.nix` — `languages` defined twice (Nix attribute collision)

**Location:** §2.1, `devenv.nix` example

The recommended configuration sets `languages.rust.enable = true;` on a standalone line, then redefines `languages = { python = { ... }; };` as a separate attribute set. In Nix, this is a **duplicate attribute error** — the second definition overwrites the first wholesale, which means `rust.enable` will be silently dropped:

```nix
languages.rust.enable = true;    # line 10 — WILL BE OVERWRITTEN

languages = {                     # line 12 — overrides all of `languages`
    python = { ... };
};
```

The correct syntax in devenv is to merge them into the same block:

```nix
languages = {
    rust.enable = true;
    python = {
        enable = true;
        version = "3.13";
        venv.enable = true;
        uv.enable = true;
    };
};
```

Additionally, `packages = [ pkgs.rustup pkgs.maturin ... ]` is unnecessary when `languages.rust.enable = true` is set — devenv's Rust module provisions `rustup`, `cargo`, and `rustc` automatically. Installing both risks version conflicts. Keep `maturin` in packages, drop `rustup`.

---

### 🔴 B-02: PyO3 module name is inconsistent across three locations

**Location:** §3.3 (lib.rs), §6.1 (RustProject), §9.2 (pyproject.toml)

Three different module paths are used:

| Source | Module/import path |
|---|---|
| `lib.rs` — `#[pymodule] fn tyo3(...)` | Creates a top-level module named `tyo3` (the native `.so`) |
| `pyproject.toml` — `module-name = "tyo3.rust_backend"` | Tells maturin to name the compiled `.so` file `rust_backend` and nest it as `tyo3.rust_backend` |
| `rust_project.py` — `from tyo3 import rust_backend` | Expects the native module at `tyo3.rust_backend` as a Python submodule |

The problem: `#[pymodule] fn tyo3(...)` creates a module named `tyo3`, not `rust_backend`. maturin uses the `module-name` config as the install destination, but the compiled `.so` will still expose its **internal** name as `tyo3`. When Python imports `tyo3.rust_backend`, it will find a module whose internal PyO3 name is `tyo3`, which will work, but the mismatch between the Python-facing name and the Rust-internal name will cause confusion in tracebacks and error messages.

**Fix:** Rename the PyO3 module to match `rust_backend`:

```rust
#[pymodule]
#[pyo3(name = "rust_backend")]
fn rust_backend(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<project::PyTyProject>()?;
    m.add_function(wrap_pyfunction!(backend_info, m)?)?;
    Ok(())
}
```

Then in `rust_project.py`, confirm the import: `from tyo3 import rust_backend` → accesses `rust_backend.TyProject`.

---

### 🔴 B-03: Conversion functions referenced but never defined

**Location:** §5.1 (`project.rs`) references `dto::convert_diagnostics()`, `dto::convert_symbol()`

The `project.rs` code in §5.1 calls these functions:

```rust
let diagnostics = dto::convert_diagnostics(&state.db, &result);
// ...
let dtos: Vec<dto::SymbolDto> = symbols.into_iter()
    .map(|s| dto::convert_symbol(&state.db, &s))
    .collect();
```

But **no conversion functions are defined anywhere in the document.** The DTO structs are defined in §4, and Appendix C lists a `convert/` directory, but:

- No `convert/mod.rs`, `convert/diagnostics.rs`, `convert/symbols.rs`, `convert/navigation.rs`, or `convert/hover.rs` are described.
- No function signatures or implementations are given.
- The DTO-to-ty-internal mapping (e.g., what ty/Ruff types each DTO field corresponds to) is undocumented.

Every DTO struct needs a corresponding `convert_*()` function that takes the raw ty/Ruff internal types and produces the stable DTO. Without these, the `project.rs` facade will not compile. This is the most labor-intensive and error-prone part of the implementation, and the guide omits it entirely.

**What's needed** (minimum for Phase 0 spike):
- `convert_diagnostics(db, &ty_check_result) -> Vec<DiagnosticDto>`
- `convert_symbol(db, &ty_symbol) -> SymbolDto`
- `convert_definition_target(db, &ty_nav_target) -> DefinitionTargetDto`
- `convert_reference(db, &ty_reference) -> ReferenceDto`
- `convert_hover(db, &ty_hover) -> HoverDto`

---

### 🔴 B-04: The `languages` Nix block in the "final" configuration still duplicates attribute definitions

**Location:** §9.5, "devenv.nix final configuration"

This snippet repeats the same Nix error as B-01, but worse:

```nix
languages.rust.enable = true;      // line 12 — standalone

languages.python = {                // line 14 — partial merge
    enable = true;
    version = "3.13";
    ...
};
```

Setting `languages.python = {...}` overrides the entire `languages.python` key. Unlike the first occurrence, this one isn't an outright syntax error (at least in some Nix versions), but it **still silently drops `languages.rust.enable`** because `languages.rust.enable` set through dotted notation is overridden when `languages.python` is set separately. Both Rust and Python must be in the same attribute set.

---

### 🔴 B-05: `BackendInfoDto` JSON serialisation vs Python expectations for `backend_info()`

**Location:** §3.3 (lib.rs)

`backend_info()` is defined as a standalone `#[pyfunction]` returning `PyResult<String>` (a JSON-encoded string). But the Python `ProjectService.query_backend_info()` in the existing codebase (§7 of the concept doc, confirmed in the current `project_service.py`) returns a **Pydantic `BackendInfo` model**, not a raw JSON string. In the rewired service (§6.2), `BackendInfo` is read directly from configuration constants, not from the Rust extension.

If the intent is to query the Rust extension for `BackendInfo`, the wrapper layer needs a `backend_info()` method. As written, `backend_info()` is a standalone function attached to the top-level module, not a method on `PyTyProject`. The Python `RustProject` class doesn't wrap it. This creates an orphan function that nothing calls.

**Decision needed:** Either (a) make `backend_info` a method on `PyTyProject` (consistent with everything else), or (b) drop the Rust-side function entirely and hardcode the info in Python (as currently done). The spec's `QueryBackendInfo` rule doesn't require a Rust round-trip — it's static metadata.

---

## Major Issues

### 🟠 M-01: `char_len_to_byte_offset` has a silent overflow risk

**Location:** §4.2, `coordinates.rs`

```rust
let mut byte_pos = 0u32;
for (i, c) in text.chars().enumerate() {
    if i >= char_offset {
        break;
    }
    byte_pos += c.len_utf8() as u32;
}
TextSize::from(byte_pos)
```

- `byte_pos` is a `u32` that counts bytes. On a very long line with multi-byte characters, `byte_pos` could overflow `u32` (max line length of ~4GB is unlikely in practice but not impossible with generated files).
- More importantly, `c.len_utf8()` returns a `usize`, which is 64-bit on modern systems. Casting to `u32` silently truncates. Use `u32::try_from(c.len_utf8())` or keep `byte_pos` as `usize` and convert at the end.
- The `TextSize::from()` call at the end takes the potentially overflowed `u32` and wraps it. Consider using `TextSize::try_from` with proper error handling, or keep the accumulator as `usize`.

### 🟠 M-02: `position_to_offset` does not validate column bounds for the line

**Location:** §4.2

The function checks `line >= 1` and `column >= 1` via `checked_sub`, but doesn't verify that the column actually exists on that line. A `column=99999` on a 10-character line will silently produce a byte offset pointing past the end of the line (or into the next line), and ty will return garbage results or panic. The navigation spec (§5 of `tyo3-navigation.allium`) explicitly defines `GotoDefinitionInvalidPosition`, `FindReferencesInvalidPosition`, etc. — position validation should be as strict as the spec requires.

The fix: after computing the byte offset, clamp or reject if `line_start + byte_col > source.text_len()`.

### 🟠 M-03: `py.allow_threads()` is mentioned but the method signatures can't use it

**Location:** §3.3, §5.1, §5.3, and Appendix F

The GIL deadlock section in §F says:

```rust
fn check(&self, py: Python<'_>) -> PyResult<String> {
    let state = self.inner.lock()...;
    let result = py.allow_threads(|| { state.db.check() })?;
    ...
}
```

But **all method signatures in §5.1 omit the `py: Python<'_>` parameter**:

```rust
fn check(&self) -> PyResult<String> {
    let state = self.inner.lock()...;
    let result = state.db.check()...;
    ...
}
```

Without the `Python<'_>` token, `py.allow_threads()` cannot be called. This means every method that acquires the Mutex lock will (a) hold the GIL during the entire Rust operation, and (b) hold the Mutex across the GIL. For `project.check()` — which can scan hundreds of files — this blocks all Python threads from making progress. For a library that may be called from an async LSP event loop, this is a serious concurrency regression.

**Fix:** Every method that does heavy work should accept `py: Python<'_>` and wrap the Rust call in `py.allow_threads(|| ...)`. This releases the GIL during the Rust operation, letting other Python threads run.

### 🟠 M-04: `HoverContentKind` class is not a valid enum

**Location:** §7.3, hover model addition

```python
class HoverContentKind(str):
    TYPE = "type"
    SIGNATURE = "signature"
    DOCSTRING = "docstring"
    TYPED_DICT_KEY = "typed_dict_key"
    MARKDOWN = "markdown"
    PLAIN_TEXT = "plain_text"
```

This extends `str` with class-level attributes, but does **not** use `enum.StrEnum` or `Enum`. The result:
- `HoverContentKind.TYPE` is just a string `"type"`, not a distinct enum member.
- `isinstance("type", HoverContentKind)` is `False`.
- Pydantic won't coerce or validate against these constants.
- The `kind: str` field on `HoverContent` accepts any arbitrary string.

**Fix:** Use `from enum import StrEnum` or a `Literal` type:

```python
from enum import StrEnum

class HoverContentKind(StrEnum):
    TYPE = "type"
    SIGNATURE = "signature"
    DOCSTRING = "docstring"
    TYPED_DICT_KEY = "typed_dict_key"
    MARKDOWN = "markdown"
    PLAIN_TEXT = "plain_text"
```

Note: `HoverResult` and `HoverContent` are correctly defined as Pydantic models.

### 🟠 M-05: `schemars` is a hard dependency, not optional

**Location:** §3.2, `Cargo.toml`

```toml
schemars = "0.8"   # Optional: for JSON schema generation
```

The comment says "Optional" but it's declared as a non-optional dependency. This means `schemars` will be compiled and linked even if unused. If genuinely optional, wrap it in a feature:

```toml
[features]
json-schema = ["schemars"]

[dependencies]
schemars = { version = "0.8", optional = true }
```

If it's not going to be used in v0.1, remove it entirely from the dependency list and add it back when needed.

### 🟠 M-06: `ruff_python_ast` and `ty_module_resolver` are listed as dependencies but never used

**Location:** §3.2, `Cargo.toml`

The following crates are declared but no code example in the entire document uses them:

- `ruff_python_ast` — Python AST types (likely needed for symbol kinds? Not shown.)
- `ty_module_resolver` — Module resolution (needed for `all_symbols` resolving imports? Not shown.)

If they're genuinely needed by the DTO conversion layer, document where. If they're speculative, remove them now and add when needed — they add 5–10 minutes each to a first-time `cargo build`.

### 🟠 M-07: `unwrap()` on non-UTF-8 path will panic

**Location:** §5.1, `PyTyProject::open()`

```rust
let system_path = SystemPathBuf::from(absolute.to_str().unwrap());
```

`Path::to_str()` returns `None` for non-UTF-8 paths (valid on Linux). `unwrap()` will panic the entire Python interpreter. Use `to_string_lossy()` or propagate an error:

```rust
let s = absolute.to_str()
    .ok_or_else(|| PyRuntimeError::new_err(
        format!("Path '{}' contains non-UTF-8 characters", absolute.display())
    ))?;
let system_path = SystemPathBuf::from(s);
```

### 🟠 M-08: `env.CARGO_HOME = "$HOME/.cargo"` is evaluated at Nix eval time, not shell time

**Location:** §9.3, devenv.nix

```nix
env.CARGO_HOME = "$HOME/.cargo";
```

Nix evaluates `$HOME` at **build time** (likely `/root` or `/homeless-shelter`), not when the user enters the shell. The correct approach for dynamic environment variables is to use `lib.mkDefault` with a shell hook, or set it in `enterShell`:

```nix
enterShell = ''
    export CARGO_HOME="$HOME/.cargo"
    rustc --version
    cargo --version
'';
```

Or use devenv's `env` with `lib.getEnv`-style functionality. A simpler fix: since Cargo already defaults to `$HOME/.cargo`, you may not need to set this at all — it's the default. You only need it if you want to override the default path.

---

## Minor Issues

### 🟡 m-01: `Allium spec ↔ Rust operation mapping` table uses speculative `ty_project::check(db)`

**Location:** Appendix A

The mapping lists `CheckProject` as mapping to `ty_project::check(db)`, but §5.1 calls `state.db.check()` (a method on `ProjectDatabase`). The appendix entry for `ty_project::check(db)` is misleading — it should reference `ProjectDatabase::check(self)` or `db.check()` to match the facade code.

### 🟡 m-02: No test for `ReloadProject` or `CloseProject` in the test plan

**Location:** §8.2, test list

All tests shown cover `open`, `files`, `check`, `symbols`, `navigation`, and `hover`. There are no tests for reload or close, which are spec-defined rules with invariants (diagnostics cleared on reload, project cannot be reopened while open, files belong to open project). The existing Python test suite (`test_invariants.py`, `test_state_machine.py`) likely covers these, but no Rust-backed integration test verifies what happens to the `ProjectDatabase` on close or reload.

### 🟡 m-03: `ProjectFile` entity has no Rust-side representation but appears in DTOs

**Location:** §4.4 diagnostics DTO vs Allium spec

`DiagnosticDto { file: Option<String> }` stores the file path as a bare string with no reference to the `ProjectFile` entity. The Allium spec ties `Diagnostic.file` to `ProjectFile`. When Python reconstructs the `Diagnostic` Pydantic model, it will need to look up the `ProjectFile` by path — this lookup is never described. Either the Rust DTO should embed enough info to reconstruct the relationship, or the lookup strategy should be documented.

### 🟡 m-04: `CoordinateMode` ("python" vs "rust") is defined in the spec but never implemented in the Rust facade

**Location:** §5.1

The spec's `config.default_coordinate_mode` sets `"python"` (1-based line/col). The Rust coordinate converter assumes 1-based input and converts to 0-based for ty. If a future version adds a `"rust"` mode (0-based), the converter would need a branch. This is deferred scope but worth a comment in the `coordinates.rs` file stating the assumption.

### 🟡 m-05: `semantic_tokens()` and `type_hierarchy()` appear in the method reference table but not in the Phase 5 test plan

**Location:** §5.3 table vs §8.2

The reference table lists `semantic_tokens(path, range?)` and `type_hierarchy(path, line, col)` as methods, but Appendix C says tokens and hierarchy DTOs are deferred, and the test plan skips them. This is fine per the v0.1 scope statement, but the table should mark these rows as "deferred" to avoid confusion.

### 🟡 m-06: `find_references` method has no declaration kind but the spec's `ReferenceKind` includes `read | write | other`

**Location:** §5.3 table

The guide correctly notes (Appendix B, item 3) that `include_declaration` is a boolean flag and we shouldn't synthesise a `"declaration"` reference kind. However, the table shows the return type as `Vec<ReferenceDto>`, and `ReferenceDto { kind: "read" | "write" | "other" }`. The spec's `ReferenceKind` has `read`, `write`, `other` — good. But what does "other" mean upstream? If ty_ide never returns "other", the Python model will never exercise that branch. Document the expected mapping.

### 🟡 m-07: `devenv.nix` package list inconsistently includes `pkgs.rustup` in §2.1 but drops it in §9.5

**Location:** Compare §2.1 and §9.5

The Phase 0/2.1 example includes `pkgs.rustup` and `pkgs.maturin` in packages. The final §9.5 config drops `pkgs.rustup` but keeps `pkgs.maturin`. This is the correct final state (since `languages.rust.enable = true` provides the toolchain), but the inconsistency between the "first thing to fix" example and the "final" version will confuse someone following the guide sequentially.

### 🟡 m-08: Hover DTO uses `String` for `kind` instead of an enum

**Location:** §4.4, hover DTOs

```rust
pub struct HoverContentDto {
    pub kind: String,   // "type" | "signature" | "docstring" | ...
    pub value: String,
}
```

Using `String` instead of a Rust enum means invalid kinds can pass through to Python, where Pydantic will reject them. A Rust `enum HoverContentKind` with `#[derive(Serialize, Deserialize)]` using `serde = "lowercase"` would catch errors earlier and make the DTO self-documenting.

---

## Observations

### 🔵 O-01: The architecture's "facade, not API" principle is excellent

The guide correctly positions the Rust extension as an **implementation detail** behind Pydantic models. This means the public API (Pydantic) stays stable even if ty's internals change dramatically. This is the single best architectural decision in the document and should be preserved aggressively.

### 🔵 O-02: The `serde_json` round-trip is deliberate, not wasteful

Serialising Rust DTOs to JSON strings and deserialising them into Pydantic models in Python involves an extra allocation per call. A more performant approach would be to construct Python dicts/lists directly in PyO3, avoiding JSON entirely. However, the JSON round-trip provides:
- Clean separation between Rust and Python type systems
- Easy snapshot testing (dump JSON, compare)
- Debuggability (you can print the JSON before it hits Pydantic)

For v0.1, this is the right trade-off. Maintain a watch item: if profiling shows JSON overhead is significant (unlikely for symbol-sized payloads), revisit.

### 🔵 O-03: Fixture project design is well-thought-out but missing `__init__.py` as package marker

**Location:** §8.1

All fixture projects include `__init__.py`, which makes them proper packages and allows ty to treat them as first-party code with relative imports. Good. However, no fixture explicitly tests **empty projects** or **single-file projects without `__init__.py`**. These are common real-world scenarios and should be test cases.

### 🔵 O-04: The `DIRECTORY_LISTING` in Appendix C omits `rust/src/convert/` from the tree

**Location:** Appendix C

The listing shows:

```
rust/
  src/
    dto/
      ...
    convert/       ← listed but no contents shown
      mod.rs
      diagnostics.rs
      symbols.rs
      navigation.rs
      hover.rs
```

The files are named but no code is provided. This is the same gap as B-03 — the convert layer is the bridge between ty internals and DTOs, and it's the most complex code in the entire extension.

### 🔵 O-05: maturin `module-name = "tyo3.rust_backend"` plus `crate-type = ["cdylib"]` naming considerations

When maturin builds with `module-name = "tyo3.rust_backend"`, it produces a shared object named `rust_backend.*.so` (platform-specific extension) and installs it at the Python path `tyo3/rust_backend.*.so`. The `crate-type = ["cdylib"]` in Cargo.toml is correct for this. The native `.so` will export a `PyInit_rust_backend` symbol (derived from the PyO3 module name, not the crate name). If the `#[pymodule]` name is `tyo3` (as in B-02), the exported symbol will be `PyInit_tyo3`, which won't match what Python's import machinery expects. This is the root cause of B-02.

### 🔵 O-06: No `[profile.release]` section in Cargo.toml

**Location:** §3.2

For production builds, a `[profile.release]` section with `lto = true` and `codegen-units = 1` can significantly reduce binary size and improve performance. The ty/Ruff crates include a lot of code; release optimisations matter.

### 🔵 O-07: The concept document's `01-tyo3-concepting` is referenced but not cross-linked

**Location:** Footer

The footer references `TyO3_CONCEPT.md` but gives no path to navigate from the concept doc to the backend wiring doc and vice versa. A reader new to the project would benefit from explicit cross-references in a note block at the top of each document.

### 🔵 O-08: `devenv.nix` final config removes the `enterShell` version check for rustc/cargo

**Location:** §9.5

The Phase 0 config (§2.1) includes `rustc --version` and `cargo --version` in `enterShell`. The final config (§9.5) drops them, keeping only `git --version`. Keeping the Rust version checks in `enterShell` is useful for diagnosing broken environments.

### 🔵 O-09: No error type for when `ProjectDatabase::new()` fails due to an empty or non-Python directory

**Location:** §5.1

`ProjectDatabase::new(&system_path)` may fail if the directory contains no Python files, or if the path exists but isn't a valid Python project. The Allium spec doesn't define an error for this case — `OpenProject` only handles `path_not_found`. Either extend the spec or document the expected Rust-level error behaviour.

### 🔵 O-10: `unwrap()`s in example code will panic the interpeter, but the document `PANTS` correctly does not feature as a dependency

**Location:** §3.4 spike test

```python
python -c "
from tyo3 import TyProject
p = TyProject.open('/tmp/tyo3-fixture')
print(p.document_symbols('main.py'))
"
```

If `open()` or `document_symbols()` panics (Rust `unwrap()` or similar), the entire Python process crashes with no Python traceback. The document should include a note: "If you get `SIGABRT` or a Rust panic message, check the Rust code for `unwrap()` calls on `None` or `Err` values." This is particularly relevant for the first-time spike.

---

## Commendations

### ✅ C-01: Spec traceability is thorough

Every Rust operation in §5.3 is mapped to an Allium rule in Appendix A, and every Allium rule in the specs has a black-box helper that maps to a specific `ty_ide` function. This is exemplary spec-driven development.

### ✅ C-02: The `known ty_ide API quirks` appendix (B) is invaluable

Documenting upstream API quirks — especially `all_symbols` requiring a context file, type hierarchy being split across three functions, and hover having structured content kinds — is the kind of tribal knowledge that usually gets lost. Having it in the guide saves hours of debugging.

### ✅ C-03: Gradual replacement plan (Phase 4) is low-risk and well-structured

Replacing black-box stubs one service at a time, with a clear matrix mapping phases to service files, makes the integration testable at each step. The existing test suite can validate each service independently as it's rewired.

### ✅ C-04: First-compile time warning

The "go make tea" note in §9.6 is honest and sets expectations. Building the ty/Ruff dependency tree from source is genuinely 20–60 minutes on cold cache. The `CARGO_HOME` caching note ensures subsequent builds are fast.

### ✅ C-05: Coordinate conversion is handled explicitly and carefully

Despite the issues noted in M-01 and M-02, the overall approach — explicit `position_to_offset` and `range_to_dto` functions with clear documentation of 1-based vs 0-based — is exactly what's needed. This is the most error-prone part of any LSP-like binding, and the guide gives it appropriate attention.

### ✅ C-06: `Arc<Mutex<TyProjectState>>` is the correct threading primitive

Using `Arc<Mutex<>>` (not `Rc<RefCell<>>`) correctly anticipates that Python may call methods from different threads (e.g., an async LSP loop). The poison error handling on `lock()` is also correct — a Rust panic during an operation should propagate as a Python exception, not poison the mutex silently.

---

## Summary of Required Fixes Before Phase 0

| ID | Severity | What to fix | Where |
|---|---|---|---|
| B-01 | 🔴 | Merge `languages.rust` and `languages.python` into single `languages = { ... }` block | §2.1 `devenv.nix` |
| B-02 | 🔴 | Rename `#[pymodule] fn tyo3` → `#[pymodule] fn rust_backend` with `#[pyo3(name = "rust_backend")]` | §3.3 `lib.rs` |
| B-03 | 🔴 | Define `convert_diagnostics()`, `convert_symbol()`, and other DTO conversion functions | New section between §4 and §5 |
| B-04 | 🔴 | Fix duplicate `languages` block in final devenv.nix | §9.5 |
| B-05 | 🔴 | Decide: make `backend_info` a method on PyTyProject, or remove the Rust-side function | §3.3, §6.1 |
| M-01 | 🟠 | Use `usize` for byte accumulator in `char_len_to_byte_offset` | §4.2 |
| M-02 | 🟠 | Validate column bounds against actual line length in `position_to_offset` | §4.2 |
| M-03 | 🟠 | Add `py: Python<'_>` parameter to all PyTyProject methods that do heavy work, wrap in `py.allow_threads()` | §5.1 |
| M-04 | 🟠 | Change `HoverContentKind` to `StrEnum` | §7.3 |
| M-05 | 🟠 | Make `schemars` optional or remove it | §3.2 |
| M-06 | 🟠 | Justify or remove `ruff_python_ast` and `ty_module_resolver` deps | §3.2 |
| M-07 | 🟠 | Replace `.to_str().unwrap()` with proper error handling | §5.1 |
| M-08 | 🟠 | Move `CARGO_HOME` to `enterShell` or drop it (it's the default) | §9.3, §9.5 |

**Estimated rework effort:** ~4–6 hours to address all blockers and majors before Phase 0 can proceed cleanly.
