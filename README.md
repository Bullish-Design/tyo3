# TyO3

Python semantic engine powered by [ty](https://github.com/astral-sh/ty),
the extremely fast Python type checker from the creators of Ruff.

TyO3 wraps ty's Rust backend via [PyO3](https://pyo3.rs/), providing a
Pydantic-validated Python API for type checking, symbol discovery, code
navigation, and hover information.

Beyond stateless reads, TyO3 is an **incremental, transactional semantic
engine**:

- **Editable sessions** — overlay edits in memory (`session.edit` /
  `edit_many`) without writing to disk; each edit is one atomic commit.
- **MVCC snapshots** — `session.snapshot()` pins an immutable, thread-shareable
  view of a revision; reads never advance head, and pinned snapshots time-travel.
- **Durable identity** — entities carry stable `DurableId`s that survive cosmetic
  edits and moves; a content hash changes only on a meaningful edit.
- **Id-level commit deltas** — every write yields the transitive,
  container-granular `affected_ids` closure, computed natively at the source.
- **Derived & authored layers** — content-hash-keyed derived artifacts (local or
  semantic key locality) and durable authored records, surfaced per snapshot.
- **Delta bus** — subscribe to ordered, id-level change notifications, with an
  optional async `precision = method` refinement channel.

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

### `Snapshot`

An immutable, revision-pinned, thread-shareable read view. Created via
``session.snapshot()``. Exposes every read method that ``TyO3Session``
does, but no ``reload()`` or ``snapshot()``. Use as a context manager.

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
| `document_highlights(path, line, col)` | `list[Reference]` | Highlight in-file symbol occurrences |
| `hover(path, line, col)` | `HoverResult \| None` | Hover/type information |
| `type_hierarchy(path, line, col)` | `TypeHierarchy \| None` | Type hierarchy at position |
| `file_occurrences(path)` | `list[NameOccurrence]` | Batch-resolve all name occurrences |

#### Refactoring

| Method | Returns | Description |
|--------|---------|-------------|
| `can_rename(path, line, col)` | `Range \| None` | Check if symbol can be renamed |
| `rename(path, line, col, new_name)` | `WorkspaceEdit \| None` | Compute rename workspace edit |

#### Editor Features

| Method | Returns | Description |
|--------|---------|-------------|
| `selection_ranges(path, line, col)` | `list[Range]` | Selection ranges at position |
| `folding_ranges(path)` | `list[FoldingRange]` | Folding ranges for a file |
| `semantic_tokens(path, *, start_line, ...)` | `list[SemanticToken]` | Semantic tokens (optional range) |
| `inlay_hints(path)` | `list[InlayHint]` | Inlay hints for a file |
| `hints(path)` | `list[Hint]` | Unused binding / unreachable code hints |

#### LSP Features

| Method | Returns | Description |
|--------|---------|-------------|
| `signature_help(path, line, col)` | `SignatureHelp \| None` | Function signature help |
| `completions(path, line, col, *, auto_import)` | `list[Completion]` | Completion suggestions |
| `code_actions(path, start_line, start_col, end_line, end_col, code)` | `list[QuickFix]` | Quick fixes for a diagnostic |

All positions are **1-based** (line &ge; 1, column &ge; 1).  Passing a
zero or negative position raises `PositionError`.

### Common re-exported types

For convenience, these types are importable directly from `tyo3`:

```python
from tyo3 import (
    TyO3Session,
    Snapshot,
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

**Session reads are non-blocking.** All read methods (``check()``,
``document_symbols()``, ``hover()``, etc.) release the GIL during the
analysis phase via ``py.detach``, so a long ``check()`` on one thread no
longer freezes other threads or the event loop. This makes ``TyO3Session``
ready for async use — just wrap calls in ``await asyncio.to_thread()``.

For **revision-consistent multi-read operations** (building a ``CodeGraph``,
serving a compound LSP request), use a **Snapshot** — a cheap, immutable,
thread-shareable read view pinned to the database revision at creation time:

```python
from tyo3 import TyO3Session
import asyncio

async def analyze(session: TyO3Session):
    # Non-blocking check — other threads/async tasks make progress
    result = await asyncio.to_thread(session.check)

    # Snapshot: consistent multi-read isolated from reload
    with session.snapshot() as snap:
        symbols = snap.document_symbols("src/main.py")
        refs = snap.find_references("src/main.py", 10, 5)
        # All reads see the exact same revision
```

A **single Snapshot is safe to share across threads** — each read clones
the pinned database under a brief lock, then runs the heavy analysis
GIL-free. Snapshots are terminal: they don't expose ``reload()`` or
``snapshot()``.

> **Why this works:** The underlying ``ProjectDatabase`` (Salsa 0.26) is
> ``Send + Clone`` but not ``Sync``. Instead of sharing a ``&db`` across
> threads (which would be unsound), every read method locks, **clones** the
> database (an ``Arc``-bump — microseconds), drops the lock, and runs on
> the owned clone with the GIL released. All mutation swaps the canonical
> database wholesale (never mutating in place), so outstanding clones stay
> isolated.

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
