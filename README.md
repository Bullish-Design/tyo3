# TyO3

Python semantic engine powered by [ty](https://github.com/astral-sh/ty),
the extremely fast Python type checker from the creators of Ruff.

TyO3 wraps ty's Rust backend via [PyO3](https://pyo3.rs/), providing a
Pydantic-validated Python API for type checking, symbol discovery, code
navigation, and hover information.

## Requirements

- Python &ge; 3.13
- Rust toolchain (for building the native extension)
- [maturin](https://www.maturin.rs/) &ge; 1.7

## Installation

### Recommended: devenv

This project uses [devenv](https://devenv.sh/) to provision the entire
toolchain (Rust, Python 3.13, maturin, uv) via Nix.  **No manual toolchain
setup is needed.**

```bash
# Clone the repository
git clone <repo-url> && cd tyo3

# Enter the development shell (provisions everything automatically)
devenv shell

# Build the Rust extension in debug mode
build
```

### Manual install with maturin

```bash
pip install maturin
cd rust && maturin develop && cd ..
```

### From wheel

```bash
pip install tyo3-*.whl
```

## Quick Start

```python
from tyo3 import TyO3Session

with TyO3Session("/path/to/project") as session:
    # Type-check the project
    result = session.check()
    for diag in result.diagnostics:
        print(f"{diag.severity}: {diag.message}")

    # Discover symbols in a file
    symbols = session.document_symbols("src/main.py")
    for sym in symbols:
        print(f"{sym.kind}: {sym.name}")

    # Navigate to definition
    targets = session.goto_definition("src/main.py", 10, 5)
    for t in targets:
        print(f"  → {t.path}:{t.range.start.line}")

    # Find references
    refs = session.find_references("src/main.py", 10, 5)
    for r in refs:
        print(f"  ← {r.path}:{r.range.start.line} ({r.kind})")

    # Hover information
    hover = session.hover("src/main.py", 10, 5)
    if hover:
        for content in hover.contents:
            print(f"  {content.kind}: {content.value}")
```

### Demo CLI

The package ships with an interactive demo that clones a GitHub repository,
indexes it with TyO3, and lets you explore symbols, references, and hover
information:

```bash
tyo3-demo https://github.com/psf/requests

# Keep the cloned repo after exiting
tyo3-demo https://github.com/psf/requests --keep

# Clone a specific branch
tyo3-demo https://github.com/psf/requests --branch main
```

## API

### `TyO3Session(root)`

The main entry point. Use as a context manager to ensure proper cleanup.

#### Lifecycle

| Method | Returns | Description |
|--------|---------|-------------|
| `reload()` | `None` | Reload project state, clearing cached data |
| `close()` | `None` | Free Rust-side resources |

#### Files

| Method | Returns | Description |
|--------|---------|-------------|
| `files()` | `list[PurePosixPath]` | All files known to the project |

#### Analysis

| Method | Returns | Description |
|--------|---------|-------------|
| `check()` | `CheckResult` | Type-check the entire project |
| `check_file(path)` | `CheckResult` | Type-check project and filter diagnostics to one file |

#### Symbols

| Method | Returns | Description |
|--------|---------|-------------|
| `document_symbols(path)` | `list[Symbol]` | Symbols defined in a file |
| `workspace_symbols(query)` | `list[Symbol]` | Search symbols across the project |

#### Navigation

| Method | Returns | Description |
|--------|---------|-------------|
| `goto_definition(path, line, col)` | `list[DefinitionTarget]` | Go to definition |
| `goto_declaration(path, line, col)` | `list[DefinitionTarget]` | Go to declaration |
| `goto_type_definition(path, line, col)` | `list[DefinitionTarget]` | Go to type definition |
| `find_references(path, line, col)` | `list[Reference]` | Find all references |
| `hover(path, line, col)` | `HoverResult \| None` | Hover/type information |

All positions are **1-based** (line &ge; 1, column &ge; 1).  Passing a
zero or negative position raises `PositionError`.

### Common re-exported types

For convenience, these types are importable directly from `tyo3`:

```python
from tyo3 import (
    TyO3Session,
    CheckResult,
    Diagnostic,
    Symbol,
    DefinitionTarget,
    Reference,
    HoverResult,
)
```

All other model types are available under `tyo3.models`:

```python
from tyo3.models import (
    Position, Range, FileRange,
    DiagnosticSeverity,
    SymbolKind,
    ReferenceKind, HoverContentKind, HoverContent,
)
```

### Exceptions

All exceptions inherit from `tyo3.exceptions.TyO3Error`:

| Exception | Raised when |
|-----------|-------------|
| `ProjectOpenError` | Project cannot be opened (invalid path, corrupt config) |
| `ProjectClosedError` | Operation attempted on a closed project |
| `PathResolutionError` | File path cannot be resolved within the project |
| `PositionError` | Line/column position is out of bounds |
| `AnalysisError` | Type-checking operation fails |
| `InternalTyError` | Underlying ty/Ruff engine encounters an unexpected error |

### Thread Safety

`TyO3Session` is **not thread-safe**.  Do not share a single session across
threads.  Create separate sessions per thread if needed.

## Development

```bash
devenv shell      # enter the development environment

# Build
build             # compile Rust extension (debug)
build-release     # compile Rust extension (release)
build-wheel       # build a distributable maturin wheel

# Test
test              # full test suite with coverage
test-quick        # unit tests only (no Rust extension needed)
test-rust         # Rust backend integration + snapshot tests
test-property     # Hypothesis property-based tests
test-perf         # performance benchmarks
test-ci           # CI-style fail-fast run
test-coverage     # full suite with HTML coverage report

# Utilities
check-so          # verify the native extension loads and works
status            # show toolchain versions and available commands
clean             # remove all build artifacts
```

## License

MIT
