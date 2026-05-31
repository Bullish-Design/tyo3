# TyO3 Rust Backend Wiring Guide

**Version:** 0.1.0
**Target backend:** `ty` 0.0.40 — commit `7b95bc219d1dcebc3ce39d222c66c14a3825c9a0`
**Ruff submodule:** commit `3cb09eba689ebb49e799131092121928cc789c18`
**Last updated:** 2026-05-30

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Prerequisites](#2-prerequisites)
3. [Phase 0 — Rust Feasibility Spike](#3-phase-0--rust-feasibility-spike)
4. [Phase 1 — Core DTO Layer](#4-phase-1--core-dto-layer)
5. [Phase 2 — TyProject Rust Facade](#5-phase-2--typroject-rust-facade)
6. [Phase 3 — PyO3 Python Bindings](#6-phase-3--pyo3-python-bindings)
7. [Phase 4 — Python Service Integration](#7-phase-4--python-service-integration)
8. [Phase 5 — Testing and Fixtures](#8-phase-5--testing-and-fixtures)
9. [Build, Packaging, and CI](#9-build-packaging-and-ci)
10. [Appendices](#10-appendices)

---

## 1. Architecture Overview

### The stack

```
┌─────────────────────────────────────────────────┐
│  Python user code / tests                       │
│  (from TyO3 Python package)                     │
├─────────────────────────────────────────────────┤
│  Pydantic models (src/tyo3/models/)              │
│  ─── input validation, serialization            │
├─────────────────────────────────────────────────┤
│  Python service layer (src/tyo3/services/)       │
│  ─── orchestrates calls, converts DTOs to models │
├─────────────────────────────────────────────────┤
│  PyO3 boundary (rust/src/lib.rs)                │
│  ─── GIL boundary, method dispatch              │
├─────────────────────────────────────────────────┤
│  Rust DTO conversion layer (rust/src/dto/)       │
│  ─── ty internals → stable Rust DTOs            │
├─────────────────────────────────────────────────┤
│  Rust TyProject facade (rust/src/project.rs)     │
│  ─── owns ProjectDatabase, coordinates ops      │
├─────────────────────────────────────────────────┤
│  ty_ide / ty_project / ruff_db crates            │
│  ─── the actual Rust semantic engine            │
└─────────────────────────────────────────────────┘
```

### Key principles

- **The Rust extension is a facade, not the API.** It owns a `ProjectDatabase` and delegates to `ty_ide` functions, but the public API surface is Pydantic models defined in Python.
- **DTO-first conversion.** Rust ty internals (lifetime-heavy, internal enums, Salsa handles) are converted into stable `serde`-compatible DTOs before crossing the PyO3 boundary. Python validates those DTOs into Pydantic models.
- **`devenv.nix` manages everything.** The Nix shell provides Rust toolchain, Python environment, and the pinned ty/Ruff crate dependency. No manual system setup.
- **The Allium specs are the source of truth.** Every Rust DTO, Python model, and test obligation is traceable to the Allium spec files in `.scratch/specs/`.

### What gets wired (v0.1 scope)

| Allium spec | Operations | Status |
|---|---|---|
| `tyo3-core.allium` | `OpenProject`, `ReloadProject`, `CloseProject`, `ListFiles`, `QueryBackendInfo` | Python stubs exist; need Rust backend |
| `tyo3-analysis.allium` | `CheckProject`, `CheckFile`, `FilterBySeverity`, `FilterByCode`, `ClearDiagnosticsOnReload` | Python stubs exist; need Rust backend |
| `tyo3-symbols.allium` | `GetDocumentSymbols`, `SearchWorkspaceSymbols`, `SearchAllSymbols` | Python stubs exist; need Rust backend |
| `tyo3-navigation.allium` | `GotoDefinition`, `GotoDeclaration`, `GotoTypeDefinition`, `FindReferences`, `GetHover` | Python stubs exist; need Rust backend |
| `tyo3-advanced.allium` | `GetSemanticTokens`, `ExploreTypeHierarchy` | Deferred to v0.2+ |

---

## 2. Prerequisites

### 2.1 devenv.nix configuration

The current `devenv.nix` does NOT enable Rust. This is the **first thing to fix**.

**Edit `devenv.nix`:**

```nix
{ pkgs, lib, config, inputs, ... }:

{
  env.GREET = "devenv";

  packages = [ 
    pkgs.git 
    pkgs.uv
    pkgs.rustup          # Rust toolchain manager
    pkgs.maturin         # Build Python extensions with PyO3
  ];

  # ── Add Rust toolchain ─────────────────────────────────────
  languages.rust.enable = true;

  languages = {
      python = {
          enable = true;
          version = "3.13";
          venv.enable = true;
          uv.enable = true;
        };
    };

  # ── Environment variables for Cargo ─────────────────────────
  env.CARGO_NET_GIT_FETCH_WITH_CLI = "true";  # Use system git for crate fetching
  env.RUST_BACKTRACE = "1";                    # Debug Rust panics

  scripts.hello.exec = ''
    echo hello from $GREET
  '';

  enterShell = ''
    hello
    git --version
    rustc --version
    cargo --version
  '';

  enterTest = ''
    echo "Running tests"
    git --version | grep --color=auto "${pkgs.git.version}"
  '';
}
```

Then rebuild the shell:

```bash
devenv shell
```

### 2.2 Verify toolchain

```bash
# Inside devenv shell:
rustc --version          # Should show stable
cargo --version          # Should match
maturin --version        # Should show maturin
python --version         # Should show 3.13
uv --version             # Should show uv
```

### 2.3 Install Python test dependencies

```bash
uv add --dev pytest pytest-cov hypothesis
```

---

## 3. Phase 0 — Rust Feasibility Spike

Before building the full facade, prove the PyO3→ty pipeline works end-to-end on one operation.

### 3.1 Create the Rust extension crate

```bash
cd /home/andrew/Documents/Projects/tyo3
mkdir -p rust/src/dto
cd rust
```

### 3.2 `rust/Cargo.toml`

This is the most critical file. The ty/Ruff crates are **not on crates.io** — they must be pulled as git dependencies from the Ruff repository at the exact commit pinned by ty 0.0.40.

```toml
[package]
name = "tyo3"
version = "0.1.0"
edition = "2021"

[lib]
name = "tyo3"
crate-type = ["cdylib"]

[dependencies]
# ── PyO3 ───────────────────────────────────────────────
pyo3 = { version = "0.23", features = ["extension-module"] }

# ── Serialisation ──────────────────────────────────────
serde = { version = "1", features = ["derive"] }
serde_json = "1"

# ── ty/Ruff crates (pinned via Ruff submodule commit) ──
# The ruff submodule commit pinned by ty 0.0.40:
ruff_db = { git = "https://github.com/astral-sh/ruff", rev = "3cb09eba689ebb49e799131092121928cc789c18" }
ruff_text_size = { git = "https://github.com/astral-sh/ruff", rev = "3cb09eba689ebb49e799131092121928cc789c18" }
ruff_python_ast = { git = "https://github.com/astral-sh/ruff", rev = "3cb09eba689ebb49e799131092121928cc789c18" }
ruff_source_file = { git = "https://github.com/astral-sh/ruff", rev = "3cb09eba689ebb49e799131092121928cc789c18" }

ty_project = { git = "https://github.com/astral-sh/ruff", rev = "3cb09eba689ebb49e799131092121928cc789c18" }
ty_ide = { git = "https://github.com/astral-sh/ruff", rev = "3cb09eba689ebb49e799131092121928cc789c18" }
ty_python_semantic = { git = "https://github.com/astral-sh/ruff", rev = "3cb09eba689ebb49e799131092121928cc789c18" }
ty_module_resolver = { git = "https://github.com/astral-sh/ruff", rev = "3cb09eba689ebb49e799131092121928cc789c18" }

# ── System path handling ─────────────────────────────
schemars = "0.8"   # Optional: for JSON schema generation
```

### 3.3 `rust/src/lib.rs` — Minimal PyO3 module

```rust
use pyo3::prelude::*;

mod dto;
mod project;
mod coordinates;
mod files;

#[pymodule]
fn tyo3(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<project::PyTyProject>()?;
    m.add_function(wrap_pyfunction!(backend_info, m)?)?;
    Ok(())
}

#[pyfunction]
fn backend_info() -> PyResult<String> {
    Ok(serde_json::to_string(&dto::BackendInfoDto {
        tyo3_version: "0.1.0".to_string(),
        ty_version: Some("0.0.40".to_string()),
        ty_commit: Some("7b95bc219d1dcebc3ce39d222c66c14a3825c9a0".to_string()),
        ruff_submodule_commit: Some("3cb09eba689ebb49e799131092121928cc789c18".to_string()),
        backend_source: Some("astral-sh/ty@0.0.40".to_string()),
    })?)
}
```

### 3.4 Spike success criteria

```bash
# Build the extension
cd /home/andrew/Documents/Projects/tyo3/rust
cargo build

# Create a tiny fixture project
mkdir -p /tmp/tyo3-fixture
cat > /tmp/tyo3-fixture/main.py << 'EOF'
def greet(name: str) -> str:
    return f"Hello, {name}!"
EOF

# Try a Python inline test
cd /home/andrew/Documents/Projects/tyo3
python -c "
from tyo3 import TyProject
p = TyProject.open('/tmp/tyo3-fixture')
print(p.document_symbols('main.py'))
"
```

**If this prints a list of symbols, the pipeline works.** If not, debug in this order:

1. Does `cargo build` succeed? (Dependency resolution is the most common failure.)
2. Does `ruff_db::SourceText` work with the fixture file?
3. Does `ty_project::ProjectDatabase::new()` succeed for the fixture?
4. Does `ty_ide::document_symbols()` return results?

---

## 4. Phase 1 — Core DTO Layer

The DTO layer converts ty/Ruff internal types into stable Rust structs that can be serialised to JSON and sent to Python.

### 4.1 Position and Range DTOs

```rust
// rust/src/dto/mod.rs
mod coordinates;
mod diagnostics;
mod symbols;
mod navigation;
mod hover;
mod tokens;
mod hierarchy;

pub use coordinates::*;
pub use diagnostics::*;
pub use symbols::*;
pub use navigation::*;
pub use hover::*;
pub use tokens::*;
pub use hierarchy::*;

// ── Backend Info ────────────────────────────────────

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct BackendInfoDto {
    pub tyo3_version: String,
    pub ty_version: Option<String>,
    pub ty_commit: Option<String>,
    pub ruff_submodule_commit: Option<String>,
    pub backend_source: Option<String>,
}
```

```rust
// rust/src/dto/coordinates.rs
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct PositionDto {
    pub line: u32,    // 1-based
    pub column: u32,  // 1-based, Unicode codepoints
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct RangeDto {
    pub start: PositionDto,
    pub end: PositionDto,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct FileRangeDto {
    pub path: String,
    pub range: RangeDto,
}
```

### 4.2 Coordinate conversion (`rust/src/coordinates.rs`)

This is the trickiest part. ty/Ruff uses `TextSize` (byte offset) internally. The Python API uses 1-based line/column (Unicode codepoints).

```rust
use ruff_db::files::File;
use ruff_db::Db;
use ruff_source_file::SourceText;
use ruff_text_size::{TextSize, TextLen};
use ruff_source_file::LineIndex;

use crate::dto::{PositionDto, RangeDto};

/// Convert a 1-based Python Position to a ruff TextSize byte offset.
pub fn position_to_offset(
    db: &dyn Db,
    file: File,
    pos: &PositionDto,
) -> Result<TextSize, String> {
    let source = SourceText::from(db.source_text(file)?);
    let line_index = LineIndex::from_source_text(&source);
    
    let line = pos.line.checked_sub(1).ok_or("Line must be >= 1")? as usize;
    let col = pos.column.checked_sub(1).ok_or("Column must be >= 1")? as usize;
    
    let line_start = line_index.line_start(line, &source);
    
    // Convert column (Unicode codepoints) to byte offset
    let line_text = &source[line_start..];
    let byte_col = char_len_to_byte_offset(line_text, col);
    
    Ok(line_start + byte_col)
}

/// Rough conversion: count chars from the start of the line text.
/// For v0.1, this handles ASCII and multi-byte UTF-8.
/// A full implementation needs proper Unicode codepoint → byte offset.
fn char_len_to_byte_offset(text: &str, char_offset: usize) -> TextSize {
    let mut byte_pos = 0u32;
    for (i, c) in text.chars().enumerate() {
        if i >= char_offset {
            break;
        }
        byte_pos += c.len_utf8() as u32;
    }
    TextSize::from(byte_pos)
}

/// Convert a ruff TextRange to a Python-friendly RangeDto.
pub fn range_to_dto(
    db: &dyn Db,
    file: File,
    range: ruff_text_size::TextRange,
) -> Result<RangeDto, String> {
    let source = SourceText::from(db.source_text(file)?);
    let line_index = LineIndex::from_source_text(&source);
    
    let start_line_col = line_index.line_index(range.start())?;
    let end_line_col = line_index.line_index(range.end())?;
    
    // ruff LineIndex is 0-based; convert to 1-based
    Ok(RangeDto {
        start: PositionDto {
            line: (start_line_col.line + 1) as u32,
            column: (start_line_col.column + 1) as u32,
        },
        end: PositionDto {
            line: (end_line_col.line + 1) as u32,
            column: (end_line_col.column + 1) as u32,
        },
    })
}
```

> **⚠️ Important:** The `LineIndex::line_index()` API may differ between ruff versions. Check the actual API at the pinned commit. You may need to use `ruff_source_file::line_index::LineIndex::line_index` or a similar method.

### 4.3 File resolution (`rust/src/files.rs`)

Resolve a Python path string to a ruff `File` handle.

```rust
use ruff_db::files::File;
use ruff_db::Db;
use std::path::PathBuf;

/// Resolve a Python path to a ruff File. Accepts absolute paths
/// and project-relative paths.
pub fn resolve_file(
    db: &dyn Db,
    root: &std::path::Path,
    path_str: &str,
) -> Result<File, String> {
    let path = PathBuf::from(path_str);
    let absolute = if path.is_absolute() {
        path
    } else {
        root.join(&path)
    };
    
    // Normalise
    let canonical = absolute.canonicalize()
        .map_err(|e| format!("Cannot resolve path '{}': {}", path_str, e))?;
    
    // Try to find a File in the database that matches this path
    ruff_db::files::File::from_path(db, &canonical)
        .ok_or_else(|| format!("File not in project: {}", path_str))
}
```

### 4.4 Remaining DTOs

**Diagnostics:**

```rust
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DiagnosticDto {
    pub file: Option<String>,
    pub range: Option<RangeDto>,
    pub severity: String,   // "fatal" | "error" | "warning" | "information" | "hint"
    pub code: Option<String>,
    pub message: String,
    pub details: Vec<String>,
}
```

**Symbols:**

```rust
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SymbolDto {
    pub name: String,
    pub qualified_name: Option<String>,
    pub kind: String,          // matches SymbolKind enum in Python
    pub location: FileRangeDto,
    pub selection_range: Option<RangeDto>,
    pub container_name: Option<String>,
    pub deprecated: bool,
}
```

**Navigation:**

```rust
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DefinitionTargetDto {
    pub path: String,
    pub range: RangeDto,
    pub selection_range: Option<RangeDto>,
    pub symbol: Option<SymbolDto>,
    pub module_name: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ReferenceDto {
    pub path: String,
    pub range: RangeDto,
    pub kind: String,         // "read" | "write" | "other"
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct HoverContentDto {
    pub kind: String,         // "type" | "signature" | "docstring" | "typed_dict_key" | "markdown" | "plain_text"
    pub value: String,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct HoverDto {
    pub location: FileRangeDto,
    pub contents: Vec<HoverContentDto>,
}
```

**Check result:**

```rust
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct CheckResultDto {
    pub diagnostics: Vec<DiagnosticDto>,
    pub files_checked: Option<u32>,
    pub elapsed_ms: Option<u64>,
}
```

---

## 5. Phase 2 — TyProject Rust Facade

### 5.1 Core state holder (`rust/src/project.rs`)

```rust
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use pyo3::prelude::*;
use pyo3::exceptions::PyRuntimeError;

use ruff_db::Db;
use ruff_db::files::{self, File};
use ruff_db::system::SystemPathBuf;

use ty_project::ProjectDatabase;

use crate::coordinates;
use crate::files as file_resolver;
use crate::dto;

/// Internal mutable state of a TyO3 project session.
struct TyProjectState {
    db: ProjectDatabase,
    root: SystemPathBuf,
}

/// Python-facing wrapper around TyProjectState.
#[pyclass(name = "TyProject")]
pub struct PyTyProject {
    inner: Arc<Mutex<TyProjectState>>,
}

#[pymethods]
impl PyTyProject {
    #[staticmethod]
    fn open(root: String) -> PyResult<Self> {
        let root_path = PathBuf::from(&root);
        let absolute = root_path.canonicalize()
            .map_err(|e| PyRuntimeError::new_err(format!("Cannot resolve root '{}': {}", root, e)))?;
        let system_path = SystemPathBuf::from(absolute.to_str().unwrap());
        
        // ── Construct ProjectDatabase ──
        // This follows ty's project loading path.
        // The exact API for ProjectDatabase::new() depends on the pinned commit.
        let db = ProjectDatabase::new(&system_path)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to open project: {}", e)))?;
        
        Ok(PyTyProject {
            inner: Arc::new(Mutex::new(TyProjectState {
                db,
                root: system_path,
            })),
        })
    }
    
    // ── Methods ─────────────────────────────────
    // Each method locks the state, calls a ty_ide function,
    // converts results to DTOs, serialises to JSON,
    // and returns a Python dict/list.
    
    fn files(&self) -> PyResult<Vec<String>> {
        let state = self.inner.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;
        
        // List files in the project database
        let paths: Vec<String> = state.db.iter_files()
            .filter_map(|f| {
                let path = files::file_path(&state.db, f);
                path.as_str().map(|s| s.to_string())
            })
            .collect();
        
        Ok(paths)
    }
    
    fn check(&self) -> PyResult<String> {
        let state = self.inner.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;
        
        // Run ty's checker
        let result = state.db.check()
            .map_err(|e| PyRuntimeError::new_err(format!("Check failed: {}", e)))?;
        
        // Convert diagnostics to DTOs
        let diagnostics = dto::convert_diagnostics(&state.db, &result);
        
        let check_result = dto::CheckResultDto {
            diagnostics,
            files_checked: Some(state.db.file_count() as u32),
            elapsed_ms: None, // compute from timing data
        };
        
        serde_json::to_string(&check_result)
            .map_err(|e| PyRuntimeError::new_err(format!("Serialisation failed: {}", e)))
    }
    
    fn document_symbols(&self, path: String) -> PyResult<String> {
        let state = self.inner.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;
        
        let file = file_resolver::resolve_file(&state.db, state.root.as_std_path(), &path)
            .map_err(|e| PyRuntimeError::new_err(e))?;
        
        let symbols = ty_ide::document_symbols(&state.db, file)
            .map_err(|e| PyRuntimeError::new_err(format!("document_symbols failed: {}", e)))?;
        
        let dtos: Vec<dto::SymbolDto> = symbols.into_iter()
            .map(|s| dto::convert_symbol(&state.db, &s))
            .collect();
        
        serde_json::to_string(&dtos)
            .map_err(|e| PyRuntimeError::new_err(format!("Serialisation failed: {}", e)))
    }
    
    // ── Add remaining methods ────────────────────
    // workspace_symbols
    // all_symbols
    // goto_definition
    // goto_declaration
    // goto_type_definition
    // find_references
    // hover
}
```

### 5.2 Operation pattern

Every `ty_ide` operation follows the same pattern:

```rust
fn some_operation(&self, py_args...) -> PyResult<String> {
    // 1. Lock state
    let state = self.inner.lock()?;
    
    // 2. Resolve File handle from path
    let file = file_resolver::resolve_file(&state.db, &state.root, &path)?;
    
    // 3. Convert line/column to TextSize
    let offset = coordinates::position_to_offset(&state.db, file, &position)?;
    
    // 4. Call ty_ide
    let result = ty_ide::some_function(&state.db, file, offset)
        .map_err(|e| PyRuntimeError::new_err(...))?;
    
    // 5. Convert to DTOs
    let dtos = convert_results(&state.db, result);
    
    // 6. Serialise to JSON
    serde_json::to_string(&dtos).map_err(|e| ...)
}
```

### 5.3 Method signature reference

All methods return `PyResult<String>` (JSON-encoded DTOs). The Python side parses these into Pydantic models.

| PyTyProject method | ty_ide function | Returns |
|---|---|---|
| `files()` | `state.db.iter_files()` | `Vec<String>` (paths) |
| `check()` | `state.db.check()` | `CheckResultDto` |
| `document_symbols(path)` | `ty_ide::document_symbols(db, file)` | `Vec<SymbolDto>` |
| `workspace_symbols(query)` | `ty_ide::workspace_symbols(db, query)` | `Vec<SymbolDto>` |
| `all_symbols(query, importing_from?)` | `ty_ide::all_symbols(db, query, context_file)` | `Vec<SymbolDto>` |
| `goto_definition(path, line, col)` | `ty_ide::goto_definition(db, file, offset)` | `Vec<DefinitionTargetDto>` |
| `goto_declaration(path, line, col)` | `ty_ide::goto_declaration(db, file, offset)` | `Vec<DefinitionTargetDto>` |
| `goto_type_definition(path, line, col)` | `ty_ide::goto_type_definition(db, file, offset)` | `Vec<DefinitionTargetDto>` |
| `find_references(path, line, col, include_decl)` | `ty_ide::find_references(db, file, offset, include_declaration)` | `Vec<ReferenceDto>` |
| `hover(path, line, col)` | `ty_ide::hover(db, file, offset)` | `Option<HoverDto>` |
| `semantic_tokens(path, range?)` | `ty_ide::semantic_tokens(db, file, range)` | `Vec<SemanticTokenDto>` |
| `type_hierarchy(path, line, col)` | `ty_ide::prepare_type_hierarchy + resolve_supertypes + resolve_subtypes` | `TypeHierarchyDto` |

---

## 6. Phase 3 — PyO3 Python Bindings

### 6.1 Python-side TyProject wrapper

The Rust extension provides `tyo3.TyProject` as a PyO3 class. The Python service layer wraps it with Pydantic validation.

```python
# src/tyo3/rust_project.py
"""Python wrapper around the Rust PyTyProject extension."""

import json
from pathlib import Path
from typing import Optional

from tyo3 import rust_backend  # The PyO3 extension module

from tyo3.models.core import Path as TyPath, TyProject as TyProjectModel
from tyo3.models.analysis import (
    CheckResult,
    Diagnostic,
    DiagnosticSeverity,
    Position,
    Range,
    FileRange,
)
from tyo3.models.symbols import Symbol, SymbolKind
from tyo3.models.navigation import DefinitionTarget, Reference, ReferenceKind


class RustProject:
    """Wraps the Rust PyTyProject for Pydantic-validated access."""

    def __init__(self, root: str | Path) -> None:
        self._inner = rust_backend.TyProject.open(str(root))
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    # ── Files ──────────────────────────────────────

    def files(self) -> list[Path]:
        raw: list[str] = self._inner.files()
        return [Path(p) for p in raw]

    # ── Check ──────────────────────────────────────

    def check(self) -> CheckResult:
        raw_json: str = self._inner.check()
        data = json.loads(raw_json)
        return CheckResult.model_validate(data)

    # ── Symbols ────────────────────────────────────

    def document_symbols(self, path: str | Path) -> list[Symbol]:
        raw_json: str = self._inner.document_symbols(str(path))
        data = json.loads(raw_json)
        return [Symbol.model_validate(s) for s in data]

    def workspace_symbols(self, query: str) -> list[Symbol]:
        raw_json: str = self._inner.workspace_symbols(query)
        data = json.loads(raw_json)
        return [Symbol.model_validate(s) for s in data]

    def all_symbols(
        self,
        query: str,
        importing_from: Optional[str | Path] = None,
    ) -> list[Symbol]:
        raw_json: str = self._inner.all_symbols(
            query,
            importing_from=str(importing_from) if importing_from else None,
        )
        data = json.loads(raw_json)
        return [Symbol.model_validate(s) for s in data]

    # ── Navigation ─────────────────────────────────

    def goto_definition(
        self, path: str | Path, line: int, column: int
    ) -> list[DefinitionTarget]:
        raw_json: str = self._inner.goto_definition(str(path), line, column)
        data = json.loads(raw_json)
        return [DefinitionTarget.model_validate(t) for t in data]

    def goto_declaration(
        self, path: str | Path, line: int, column: int
    ) -> list[DefinitionTarget]:
        raw_json: str = self._inner.goto_declaration(str(path), line, column)
        data = json.loads(raw_json)
        return [DefinitionTarget.model_validate(t) for t in data]

    def goto_type_definition(
        self, path: str | Path, line: int, column: int
    ) -> list[DefinitionTarget]:
        raw_json: str = self._inner.goto_type_definition(str(path), line, column)
        data = json.loads(raw_json)
        return [DefinitionTarget.model_validate(t) for t in data]

    def find_references(
        self,
        path: str | Path,
        line: int,
        column: int,
        include_declaration: bool = True,
    ) -> list[Reference]:
        raw_json: str = self._inner.find_references(
            str(path), line, column, include_declaration
        )
        data = json.loads(raw_json)
        return [Reference.model_validate(r) for r in data]

    def hover(
        self, path: str | Path, line: int, column: int
    ) -> Optional[HoverResult]:
        raw_json: str | None = self._inner.hover(str(path), line, column)
        if raw_json is None:
            return None
        data = json.loads(raw_json)
        return HoverResult.model_validate(data)
```

> **Note:** The `HoverResult` model needs to be added to `tyo3/models/navigation.py`. See the concept doc for the hover model definition.

### 6.2 Update the existing services

The existing `ProjectService`, `AnalysisService`, `SymbolService`, `NavigationService` currently use stub black-box helpers. Replace those helpers with calls to `RustProject`.

Example for `ProjectService`:

```python
# src/tyo3/services/project_service.py

from tyo3.rust_project import RustProject


class ProjectService:
    """Implements project lifecycle rules backed by the Rust engine."""

    def __init__(self) -> None:
        self._projects: dict[str, RustProject] = {}

    def open_project(self, root: str | Path) -> tuple[TyProject, list[ProjectFile]]:
        rust_proj = RustProject.open(root)
        self._projects[str(root)] = rust_proj
        
        # Create our Pydantic model from Rust data
        project = TyProject(
            root=Path(components=list(rust_proj.root.parts)),
            status="open",
            opened_at=datetime.now(timezone.utc),
        )
        return project, []
    
    # ... rest of the methods delegate to rust_proj
```

### 6.3 Exception mapping

```python
# src/tyo3/exceptions.py

class TyO3Error(Exception): ...
class ProjectOpenError(TyO3Error): ...
class PathResolutionError(TyO3Error): ...
class PositionError(TyO3Error): ...
class AnalysisError(TyO3Error): ...
class InternalTyError(TyO3Error): ...
```

---

## 7. Phase 4 — Python Service Integration

### 7.1 Gradual replacement plan

Replace the black-box stubs one service at a time:

| Phase | Service | Black-box replaced by |
|---|---|---|
| 4a | `project_service.py` | `RustProject.files()`, `RustProject.__init__` |
| 4b | `analysis_service.py` | `RustProject.check()` |
| 4c | `symbol_service.py` | `RustProject.document_symbols()`, `workspace_symbols()`, `all_symbols()` |
| 4d | `navigation_service.py` | `RustProject.goto_definition()`, `find_references()`, `hover()`, etc. |

### 7.2 Model alignment

The Allium specs and the existing Python models already align with the concept doc. However, some field types differ:

| Spec field | Current model | Should be | Action |
|---|---|---|---|
| `core.Path` | `components: list[str]` | Use `pathlib.Path` in the Python API | Keep `Path` for internal use, expose `pathlib.Path` publicly |
| `Symbol.location: FileRange` | `location: FileRange` | Correct ✓ | — |
| `Diagnostic.severity` | `str` default `"error"` | `DiagnosticSeverity` enum | Already works |
| `DefinitionTarget.symbol` | `Optional[Symbol]` | Correct ✓ | — |

### 7.3 Adding the Hover model

The current `tyo3/models/navigation.py` doesn't include hover models. Add:

```python
# src/tyo3/models/navigation.py

class HoverContentKind(str):
    TYPE = "type"
    SIGNATURE = "signature"
    DOCSTRING = "docstring"
    TYPED_DICT_KEY = "typed_dict_key"
    MARKDOWN = "markdown"
    PLAIN_TEXT = "plain_text"


class HoverContent(BaseModel):
    kind: str  # HoverContentKind
    value: str


class HoverResult(BaseModel):
    location: FileRange
    contents: list[HoverContent]
```

---

## 8. Phase 5 — Testing and Fixtures

### 8.1 Fixture projects

Create small Python projects under a `fixtures/` directory:

```
fixtures/
  simple_package/
    __init__.py
    math_ops.py           # Functions with types
  imports/
    __init__.py
    main.py               # Imports from other files
  classes/
    models.py             # Class hierarchy for goto/hierarchy tests
  diagnostic_targets/
    errors.py             # Known type errors for diagnostic tests
  unicode_positions/
    unicode.py            # Unicode identifiers, comments with emoji
```

### 8.2 Test types

**Unit tests (no Rust backend needed):**

```python
# test_coordinates.py
# Test 1-based ↔ TextSize conversion logic

# test_dto_conversion.py
# Test rust DTO → Pydantic model validation

# test_exceptions.py
# Test error wrapping
```

**Integration tests (require Rust backend and fixtures):**

```python
# test_rust_project_open.py
def test_open_simple_package():
    project = RustProject.open("fixtures/simple_package")
    files = project.files()
    assert len(files) >= 1
    assert any("math_ops.py" in str(f) for f in files)

# test_rust_check.py
def test_check_finds_diagnostics():
    project = RustProject.open("fixtures/diagnostic_targets")
    result = project.check()
    assert len(result.diagnostics) > 0

# test_rust_symbols.py
def test_document_symbols():
    project = RustProject.open("fixtures/classes")
    symbols = project.document_symbols("models.py")
    assert any(s.name == "MyClass" for s in symbols)

# test_rust_navigation.py
def test_goto_definition():
    project = RustProject.open("fixtures/simple_package")
    targets = project.goto_definition("main.py", 5, 10)
    assert len(targets) > 0

# test_rust_hover.py
def test_hover():
    project = RustProject.open("fixtures/simple_package")
    hover = project.hover("main.py", 5, 10)
    assert hover is not None
    assert len(hover.contents) > 0
```

### 8.3 Snapshot tests

```bash
pip install syrupy  # or use inline JSON snapshots
```

```python
def test_symbol_snapshot(snapshot):
    project = RustProject.open("fixtures/simple_package")
    symbols = project.document_symbols("math_ops.py")
    assert snapshot == [s.model_dump(mode="json") for s in symbols]
```

### 8.4 Coordinate tests

```python
# Test edge cases
@pytest.mark.parametrize("line, col", [
    (1, 1),       # Start of file
    (1, 0),       # Invalid: column < 1
    (0, 1),       # Invalid: line < 1
    (999, 1),     # Beyond file
])
def test_position_validation(line, col):
    if line < 1 or col < 1:
        with pytest.raises(PositionError):
            validate_position(line, col)
    else:
        validate_position(line, col)
```

### 8.5 Test recording

For quick smoke tests:

```python
@pytest.mark.skipif(
    not importlib.util.find_spec("tyo3.rust_backend"),
    reason="Rust extension not built",
)
class TestRustBackend:
    """Run only when the Rust extension is available."""
    ...
```

---

## 9. Build, Packaging, and CI

### 9.1 Build with maturin

```bash
# Inside the devenv shell:
cd /home/andrew/Documents/Projects/tyo3/rust
maturin develop --release

# Or build a wheel:
maturin build --release --out dist/
```

### 9.2 Update pyproject.toml for maturin

Add a maturin build section to `pyproject.toml`:

```toml
[build-system]
requires = ["maturin>=1.7"]
build-backend = "maturin"

[tool.maturin]
features = ["pyo3/extension-module"]
module-name = "tyo3.rust_backend"
manifest-path = "rust/Cargo.toml"
```

The maturin build replaces the hatchling build. The full `pyproject.toml` update:

```toml
[build-system]
requires = ["maturin>=1.7"]
build-backend = "maturin"

[tool.maturin]
features = ["pyo3/extension-module"]
module-name = "tyo3.rust_backend"
manifest-path = "rust/Cargo.toml"

[project]
name = "tyo3"
version = "0.1.0"
description = "TyO3: Python semantic engine powered by ty/Ruff"
requires-python = ">=3.13"
dependencies = ["pydantic>=2.12.5"]

[project.optional-dependencies]
dev = ["pytest>=7.0", "pytest-cov>=4.1"]

[tool.pytest.ini_options]
addopts = "-q --cov=tyo3 --cov-report=term-missing"
testpaths = ["src/tyo3/tests"]
```

> **⚠️ Important:** Switching from hatchling to maturin as the build backend is a one-way decision. maturin handles both building the Rust extension and packaging the Python source. The existing hatchling config must be removed.

### 9.3 Cargo build caching

To avoid re-fetching and recompiling the ty/Ruff crates on every build:

```bash
# Set CARGO_HOME to a persistent location
export CARGO_HOME="$HOME/.cargo"
```

Add to `devenv.nix`:

```nix
env.CARGO_HOME = "$HOME/.cargo";
```

### 9.4 CI integration

In CI (GitHub Actions), use `devenv` to set up the environment, then:

```yaml
- name: Build Rust extension
  run: |
    cd rust
    cargo build --release
  
- name: Install Python package
  run: |
    uv pip install -e .
  
- name: Run tests
  run: |
    uv run pytest src/tyo3/tests/
```

### 9.5 devenv.nix final configuration

```nix
{ pkgs, lib, config, inputs, ... }:

{
  env.GREET = "devenv";

  packages = [ 
    pkgs.git 
    pkgs.uv
    pkgs.maturin
  ];

  languages.rust.enable = true;

  languages.python = {
    enable = true;
    version = "3.13";
    venv.enable = true;
    uv.enable = true;
  };

  env.CARGO_NET_GIT_FETCH_WITH_CLI = "true";
  env.RUST_BACKTRACE = "1";
  env.CARGO_HOME = "$HOME/.cargo";

  scripts.hello.exec = ''
    echo hello from $GREET
  '';

  enterShell = ''
    hello
    rustc --version
    cargo --version
  '';
}
```

### 9.6 First-time build notes

Building the ty/Ruff crates from source is **slow** (20–60 minutes on first compile). Plan for this:

- Run `cargo build` in `rust/` and go make tea.
- Incremental builds after the first compile are much faster (30s–2min).
- The `CARGO_HOME` cache preserves compiled dependencies across `devenv shell` sessions.

---

## 10. Appendices

### A. Allium spec ↔ Rust operation mapping

| Allium rule | ty_ide Rust function |
|---|---|
| `OpenProject` | `ProjectDatabase::new(path)` |
| `ReloadProject` | `db.reload()` or config reload |
| `CloseProject` | Drop `ProjectDatabase` |
| `ListFiles` | `db.iter_files()` |
| `QueryBackendInfo` | Compile-time constants |
| `CheckProject` | `ty_project::check(db)` |
| `CheckFile` | Filter `check()` results by file |
| `FilterBySeverity` | Filter `check()` results |
| `FilterByCode` | Filter `check()` results |
| `ClearDiagnosticsOnReload` | `check()` replaces diagnostics |
| `GetDocumentSymbols` | `ty_ide::document_symbols(db, file)` |
| `SearchWorkspaceSymbols` | `ty_ide::workspace_symbols(db, query)` |
| `SearchAllSymbols` | `ty_ide::all_symbols(db, query, context)` |
| `GotoDefinition` | `ty_ide::goto_definition(db, file, offset)` |
| `GotoDeclaration` | `ty_ide::goto_declaration(db, file, offset)` |
| `GotoTypeDefinition` | `ty_ide::goto_type_definition(db, file, offset)` |
| `FindReferences` | `ty_ide::find_references(db, file, offset, include_decl)` |
| `GetHover` | `ty_ide::hover(db, file, offset)` |

### B. Known ty_ide API quirks (ty 0.0.40)

1. **`all_symbols` requires an importing context internally.** The Rust function signature accepts a `File` handle for the importing file, not `None`. TyO3 must default to the first project file if none is provided by the user.

2. **Type hierarchy is three separate functions.** `prepare_type_hierarchy`, `type_hierarchy_supertypes`, `type_hierarchy_subtypes`. TyO3's Python API should compose them into a single `type_hierarchy()` method.

3. **`find_references` includes declarations via a boolean flag.** `include_declaration: bool` controls this. Do not synthesise a `declaration` reference kind.

4. **Hover content has structured kinds.** The upstream hover result distinguishes `Signature`, `Type`, `TypedDictKey`, and `Docstring` content. TyO3 should preserve this structure.

5. **`call_hierarchy` is not exported.** Do not claim it as available.

6. **`SemanticTokenType` and `SemanticTokenModifier` are upstream enums.** Map them directly to Python string enums. Document known upstream limitations (quoted annotations, property imprecision, special form classification).

### C. Key files to create

```
rust/
  Cargo.toml
  src/
    lib.rs                 # PyO3 module entry
    project.rs             # PyTyProject class
    coordinates.rs         # Position/TextSize conversion
    files.rs               # Path → File resolution
    dto/
      mod.rs               # Re-exports
      coordinates.rs       # PositionDto, RangeDto
      diagnostics.rs       # DiagnosticDto
      symbols.rs           # SymbolDto
      navigation.rs        # DefinitionTargetDto, ReferenceDto
      hover.rs             # HoverDto, HoverContentDto
      tokens.rs            # SemanticTokenDto (deferred)
      hierarchy.rs         # TypeHierarchyDto (deferred)
    convert/
      mod.rs               # ty internal → DTO converters
      diagnostics.rs
      symbols.rs
      navigation.rs
      hover.rs
    errors.rs              # Error conversion

fixtures/
  simple_package/
    __init__.py
    math_ops.py
  imports/
    __init__.py
    main.py
  classes/
    models.py
  diagnostic_targets/
    errors.py
```

### D. Key files to modify

```
devenv.nix                # Enable Rust, add maturin, env vars
pyproject.toml            # Switch build backend to maturin
src/tyo3/models/navigation.py  # Add HoverResult, HoverContent models
src/tyo3/services/project_service.py    # Wire to RustProject
src/tyo3/services/analysis_service.py   # Wire to RustProject
src/tyo3/services/symbol_service.py     # Wire to RustProject
src/tyo3/services/navigation_service.py # Wire to RustProject
```

### E. Execution order

```
Phase 0:  devenv.nix changes → Cargo.toml → cargo build → Rust spike test
Phase 1:  DTO structs → conversion functions
Phase 2:  TyProjectState → PyTyProject methods
Phase 3:  rust_backend.py → update services → add hover models
Phase 4:  Wire services to RustProject → replace stubs
Phase 5:  Create fixture projects → integration tests → snapshot tests
```

### F. Troubleshooting

#### Cargo build fails with "failed to resolve git dependency"

```
error: failed to resolve git dependency `ty_project`

Cause: The Cargo.lock was generated with a stale reference.
Fix: `cargo update` or delete Cargo.lock and rebuild.

Or: The git revision hash is incorrect.
Fix: Verify the ruff submodule commit at astral-sh/ty 0.0.40.
```

#### `ProjectDatabase::new()` doesn't exist or has different signature

The exact API for opening a project may differ. Check the `ty_project` crate at the pinned commit:

```bash
# Navigate to the actual crate source
# Find the ProjectDatabase struct and its constructors
cargo doc --open
```

Alternative: Use ty's project loading path directly:

```rust
use ty_project::ProjectDatabase;

// In ty 0.0.40, the constructor may be:
let db = ProjectDatabase::open(path)?;
// or:
let db = ProjectDatabase::new_with_config(path, config)?;
```

#### `file_path()` or `iter_files()` doesn't exist

The ruff `Db` trait changes between versions. Check the actual API:

```rust
// May be:
db.iter_files()
// Or:
db.files().iter()
// Or via a method on the System trait
```

#### GIL deadlocks

If you hold the `Mutex` lock across a Python callback or a long Rust operation, you may deadlock. Use `py.allow_threads()` for operations that release the GIL:

```rust
fn check(&self, py: Python<'_>) -> PyResult<String> {
    let state = self.inner.lock().map_err(|e| ...)?;
    
    // Release GIL during long Rust operations
    let result = py.allow_threads(|| {
        state.db.check()
    }).map_err(|e| ...)?;
    
    // Convert result (GIL is held again)
    ...
}
```

---

*This guide was generated from the TyO3 Allium specifications in `.scratch/specs/` and the TyO3 concept document at `.scratch/projects/01-tyo3-concepting/TyO3_CONCEPT.md`.*
