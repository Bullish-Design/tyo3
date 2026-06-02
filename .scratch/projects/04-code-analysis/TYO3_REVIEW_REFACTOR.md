# TyO3 Refactoring Guide

This document is a step-by-step implementation guide for refactoring the tyo3 library.
Each step includes the rationale, exact file changes, and verification commands.

---

## IMPORTANT: All work must happen inside the devenv shell

This project uses [devenv](https://devenv.sh/) to provision the entire toolchain
(Rust, Python 3.13, maturin, uv, etc.) via Nix. **Do not install tools manually
or use system Python/Rust.** Everything is defined in `devenv.nix`.

### Entering the shell

```bash
cd /path/to/tyo3
devenv shell
```

On entry you'll see a status banner listing versions and available commands.
If you don't see this, something is wrong — do not proceed.

### Key devenv commands

All of these are shell scripts defined in `devenv.nix`. Use them instead of
running raw `cargo`, `pytest`, or `maturin` commands directly.

| Command | What it does |
|---------|-------------|
| `build` | Compile Rust extension (debug) and copy `.so` into `src/tyo3/` |
| `build-release` | Compile Rust extension (release) and copy `.so` |
| `build-wheel` | Build a distributable maturin wheel in `dist/` |
| `test` | Full test suite with coverage (`-v --tb=short --cov`) |
| `test-quick` | Unit tests only — skips Rust integration, property, and perf tests |
| `test-rust` | Rust backend integration + snapshot + coordinate tests only |
| `test-property` | Hypothesis property-based tests only |
| `test-perf` | Performance benchmarks only |
| `test-ci` | CI-style fail-fast run (`-x -q --tb=short --cov`) |
| `test-coverage` | Full suite with HTML coverage report |
| `check-so` | Verify the native `.so` extension loads and works |
| `clean` | Remove all build artifacts (cargo clean + .so + dist + htmlcov) |
| `status` | Show toolchain versions and available commands |

### Why this matters

- The devenv scripts set `PYTHONPATH=src` and `cd` to the correct directories.
  Running `pytest` manually without this will fail with import errors.
- The `build` script copies the `.so` to the exact filename Python expects
  (`_native_impl.cpython-313-x86_64-linux-gnu.so`). Running `cargo build`
  alone produces `lib_native_impl.so` which Python cannot import.
- After **any Rust change**, you must re-run `build` before testing.

---

## Prerequisites

1. Enter the devenv shell: `devenv shell`
2. Build the Rust extension: `build`
3. Verify it works: `check-so`
4. Confirm all existing tests pass: `test`

All four must succeed before starting the refactoring.

---

## Ground rules

- **Stay in the devenv shell** for the entire refactoring session.
- Complete each step fully before moving to the next.
- Run verification commands after each step.
- Use `test` (or `test-quick` for fast iteration) to confirm no regressions.
- Commit after each step passes verification.
- Do not combine steps — each is designed to be independently reviewable.

---

## Step 1: Fix the `check_file()` bug

**Priority:** Critical — this is a real bug in the public API.

**Problem:** `TyO3Session.check_file()` accepts a `path` argument but never uses it
to filter diagnostics. It runs a full project check and filters only by
`d.file is not None`, which is unrelated to the requested path.

**File:** `src/tyo3/session.py`

### 1.1 — Fix the implementation

Replace the current `check_file` method:

```python
# BEFORE (lines 67-79 of session.py)
def check_file(self, path: str | StdPath) -> CheckResult:
    """Run the type checker and filter to a single file.

    Note: Currently runs a full project check. File-level
    filtering depends on Rust diagnostics including file info.
    """
    result = self._rp.check()
    filtered = [d for d in result.diagnostics if d.file is not None]
    return CheckResult(
        diagnostics=filtered,
        files_checked=1,
        elapsed_ms=result.elapsed_ms,
    )
```

```python
# AFTER
def check_file(self, path: str | StdPath) -> CheckResult:
    """Run the type checker and return diagnostics for a single file.

    Runs a full project check, then filters diagnostics to those
    whose file path matches *path*. The path is compared as a
    POSIX path suffix so both absolute and project-relative paths work.
    """
    from pathlib import PurePosixPath

    target = PurePosixPath(path)
    result = self._rp.check()

    def _matches(d: Diagnostic) -> bool:
        if d.file is None or d.file.path is None:
            return False
        # Support both exact match and suffix match (relative vs absolute)
        return d.file.path == target or str(d.file.path).endswith(str(target))

    filtered = [d for d in result.diagnostics if _matches(d)]
    return CheckResult(
        diagnostics=filtered,
        files_checked=1,
        elapsed_ms=result.elapsed_ms,
    )
```

You will also need to add the import at the top of `session.py`:

```python
from tyo3.models.analysis import CheckResult, Diagnostic
```

(Add `Diagnostic` to the existing `CheckResult` import.)

### 1.2 — Add tests

Add to `src/tyo3/tests/test_surfaces.py` (or create a new `test_check_file.py`):

```python
from tyo3.models.analysis import CheckResult, Diagnostic, DiagnosticSeverity, Position, Range
from tyo3.models.core import ProjectFile, TyProject, ProjectStatus, FileCategory
from pathlib import PurePosixPath
from datetime import datetime


def _make_project():
    return TyProject(
        root=PurePosixPath("/proj"),
        status=ProjectStatus.OPEN,
        opened_at=datetime.now(),
    )


def _make_diagnostic(file_path: str) -> Diagnostic:
    proj = _make_project()
    pf = ProjectFile(
        path=PurePosixPath(file_path),
        project=proj,
        file_category=FileCategory.FIRST_PARTY,
    )
    return Diagnostic(
        file=pf,
        range=Range(
            start=Position(line=1, column=1),
            end=Position(line=1, column=10),
        ),
        severity=DiagnosticSeverity.ERROR,
        message="test error",
    )


class TestCheckFileFiltering:
    """Verify that check_file actually filters by the given path."""

    def test_filter_matches_target_file(self):
        """Diagnostics for the target file should be included."""
        d1 = _make_diagnostic("src/main.py")
        d2 = _make_diagnostic("src/other.py")
        result = CheckResult(diagnostics=[d1, d2], files_checked=2)

        # Simulate what check_file does: filter by path
        target = PurePosixPath("src/main.py")
        filtered = [
            d for d in result.diagnostics
            if d.file is not None and (
                d.file.path == target
                or str(d.file.path).endswith(str(target))
            )
        ]
        assert len(filtered) == 1
        assert filtered[0].file.path == PurePosixPath("src/main.py")

    def test_filter_excludes_other_files(self):
        """Diagnostics for other files should be excluded."""
        d1 = _make_diagnostic("src/other.py")
        result = CheckResult(diagnostics=[d1], files_checked=1)

        target = PurePosixPath("src/main.py")
        filtered = [
            d for d in result.diagnostics
            if d.file is not None and (
                d.file.path == target
                or str(d.file.path).endswith(str(target))
            )
        ]
        assert len(filtered) == 0

    def test_filter_handles_no_file_diagnostics(self):
        """Diagnostics with file=None should be excluded."""
        d = Diagnostic(file=None, message="generic error")
        result = CheckResult(diagnostics=[d])

        target = PurePosixPath("src/main.py")
        filtered = [
            d for d in result.diagnostics
            if d.file is not None and (
                d.file.path == target
                or str(d.file.path).endswith(str(target))
            )
        ]
        assert len(filtered) == 0
```

### 1.3 — Verify

```bash
# Confirm the path argument is used in the filtering logic
grep -n "target" src/tyo3/session.py | grep -i "path\|posix\|match"

# Confirm "d.file is not None" is no longer the only filter
grep -n "check_file" src/tyo3/session.py

# Run the new tests (from devenv shell — sets PYTHONPATH=src automatically)
PYTHONPATH=src python -m pytest src/tyo3/tests/test_check_file.py -v

# Run full suite to confirm no regressions (use devenv command)
test
```

---

## Step 2: Add catch-all exception mapping in RustProject

**Priority:** High — unhandled Rust panics surface as raw `RuntimeError` to users.

**Problem:** `RustProject` methods catch specific native exceptions
(`_NativeClosedError`, `_NativePathError`, etc.) but have no catch-all for
unexpected Rust errors. A Rust panic would propagate as a generic `Exception`
with no tyo3 context.

**File:** `src/tyo3/rust_project.py`

### 2.1 — Add the InternalTyError import

Add `InternalTyError` to the existing import from `tyo3.exceptions`:

```python
from tyo3.exceptions import (
    AnalysisError,
    InternalTyError,       # ← add this
    PathResolutionError,
    PositionError,
    ProjectClosedError,
    ProjectOpenError,
)
```

### 2.2 — Add catch-all `except Exception` blocks

For every method that calls `self._inner.*`, add a final `except Exception`
clause that wraps unexpected errors in `InternalTyError`. Apply this pattern
to each method: `files()`, `check()`, `document_symbols()`, `workspace_symbols()`,
`_goto()`, `find_references()`, `hover()`, and `reload()`.

Example for `files()`:

```python
# BEFORE
def files(self) -> list[str]:
    try:
        return self._inner.files()
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e

# AFTER
def files(self) -> list[str]:
    try:
        return self._inner.files()
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except Exception as e:
        raise InternalTyError(f"Unexpected error in files(): {e}") from e
```

Example for `_goto()`:

```python
# AFTER
def _goto(self, method: str, path: str | StdPath, line: int, column: int) -> list[DefinitionTarget]:
    try:
        raw_json: str = getattr(self._inner, method)(str(path), line, column)
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except _NativePositionError as e:
        raise PositionError(str(e)) from e
    except _NativePathError as e:
        raise PathResolutionError(str(e)) from e
    except Exception as e:
        raise InternalTyError(f"Unexpected error in {method}(): {e}") from e

    return [self._parse_definition_target(t) for t in json.loads(raw_json)]
```

Apply this same pattern to **all 8 methods** listed above. The catch-all must
always be the **last** except clause.

### 2.3 — Add tests

Add to `src/tyo3/tests/test_exceptions.py`:

```python
import pytest
from tyo3.exceptions import InternalTyError, TyO3Error


class TestInternalTyError:
    def test_inherits_from_base(self):
        assert issubclass(InternalTyError, TyO3Error)

    def test_wraps_unexpected_error(self):
        original = RuntimeError("rust panic")
        wrapped = InternalTyError("Unexpected error: rust panic")
        wrapped.__cause__ = original
        assert "rust panic" in str(wrapped)
        assert wrapped.__cause__ is original
```

### 2.4 — Verify

```bash
# Confirm every try block has a catch-all except Exception clause
grep -c "except Exception as e" src/tyo3/rust_project.py
# Expected: 8 (one per method that calls self._inner)

# Confirm InternalTyError is imported
grep "InternalTyError" src/tyo3/rust_project.py

# Run targeted test then full suite (use devenv commands)
PYTHONPATH=src python -m pytest src/tyo3/tests/test_exceptions.py -v
test
```


---

## Step 3: Convert `coordinate_mode` to a `StrEnum`

**Priority:** Medium — prevents invalid string values in a public model field.

**Problem:** `TyProject.coordinate_mode` is typed as `str` with a default of
`"python"`, but only specific values are valid. A typo like `"pythno"` would
silently pass validation.

**File:** `src/tyo3/models/core.py`

### 3.1 — Add the enum

Add a new `StrEnum` class in `core.py`, in the `# ── Enums` section
(after `FileCategory`, before `# ── Entities`):

```python
class CoordinateMode(StrEnum):
    """Coordinate system used for position calculations."""

    PYTHON = "python"
```

If additional modes exist in the Rust backend (check `rust/src/coordinates.rs`),
add them here. For now, `PYTHON` is the only known mode.

### 3.2 — Update the field

In `TyProject`, change:

```python
# BEFORE
coordinate_mode: str = "python"

# AFTER
coordinate_mode: CoordinateMode = CoordinateMode.PYTHON
```

### 3.3 — Update tests

Any test that constructs a `TyProject` with `coordinate_mode="python"` should
still work (because `StrEnum` accepts the string value). Add a test that
rejects invalid values:

```python
# In test_models_core.py
import pytest
from tyo3.models.core import TyProject, ProjectStatus, CoordinateMode
from pathlib import PurePosixPath
from datetime import datetime
from pydantic import ValidationError


class TestCoordinateMode:
    def test_default_is_python(self):
        proj = TyProject(
            root=PurePosixPath("/proj"),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(),
        )
        assert proj.coordinate_mode == CoordinateMode.PYTHON
        assert proj.coordinate_mode == "python"  # StrEnum is still a str

    def test_rejects_invalid_mode(self):
        with pytest.raises(ValidationError):
            TyProject(
                root=PurePosixPath("/proj"),
                status=ProjectStatus.OPEN,
                coordinate_mode="invalid_mode",
                opened_at=datetime.now(),
            )
```

### 3.4 — Update exports

Add `CoordinateMode` to `models/core.py`'s public API. If there's an
`__all__` in `models/__init__.py`, add it there too.

### 3.5 — Verify

```bash
# Confirm the enum exists
grep -n "class CoordinateMode" src/tyo3/models/core.py

# Confirm the field uses the enum type
grep -n "coordinate_mode: CoordinateMode" src/tyo3/models/core.py

# Confirm no raw string "python" is used as the field default
grep -n 'coordinate_mode.*=.*"python"' src/tyo3/models/core.py
# Expected: 0 matches (the default should use the enum member)

# Run model tests then full suite (use devenv commands)
PYTHONPATH=src python -m pytest src/tyo3/tests/test_models_core.py -v
test
```


---

## Step 4: Make `files()` return `list[PurePosixPath]`

**Priority:** Medium — aligns the return type with the rest of the model layer.

**Problem:** `TyO3Session.files()` and `RustProject.files()` return `list[str]`,
but every other part of the codebase uses `PurePosixPath` for file paths
(e.g. `ProjectFile.path`, `DefinitionTarget.path`, `Reference.path`).

**Files:** `src/tyo3/rust_project.py`, `src/tyo3/session.py`

### 4.1 — Update `RustProject.files()`

```python
# BEFORE (rust_project.py)
def files(self) -> list[str]:
    """Return the file paths known to this project."""
    try:
        return self._inner.files()
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e

# AFTER
def files(self) -> list[PurePosixPath]:
    """Return the file paths known to this project."""
    try:
        raw: list[str] = self._inner.files()
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except Exception as e:
        raise InternalTyError(f"Unexpected error in files(): {e}") from e
    return [PurePosixPath(p) for p in raw]
```

Note: `PurePosixPath` is already imported in `rust_project.py`.

### 4.2 — Update `TyO3Session.files()`

```python
# BEFORE (session.py)
def files(self) -> list[str]:
    """Return all file paths known to the project."""
    return self._rp.files()

# AFTER
def files(self) -> list[PurePosixPath]:
    """Return all file paths known to the project."""
    return self._rp.files()
```

Add the import at the top of `session.py`:

```python
from pathlib import PurePosixPath
```

(Add alongside the existing `from pathlib import Path as StdPath`.)

### 4.3 — Update any callers

Search for uses of `.files()` that assume `list[str]`:

```bash
grep -rn "\.files()" src/tyo3/ --include="*.py" | grep -v __pycache__ | grep -v test
```

If any code does string operations on file paths (e.g., `f.endswith(".py")`),
update to use `PurePosixPath` methods (e.g., `f.suffix == ".py"`).

The demo CLI (`demo/cli.py`) likely uses `files()` — update its formatting
to call `str(f)` where needed for display.

### 4.4 — Update tests

Any test that asserts `files()` returns strings should be updated:

```python
# In tests, change:
assert isinstance(result[0], str)
# To:
assert isinstance(result[0], PurePosixPath)
```

### 4.5 — Verify

```bash
# Confirm return type annotation is PurePosixPath
grep -n "def files.*PurePosixPath" src/tyo3/session.py src/tyo3/rust_project.py

# Confirm no list[str] return type remains for files()
grep -n "def files.*list\[str\]" src/tyo3/session.py src/tyo3/rust_project.py
# Expected: 0 matches

# Run full suite (use devenv command)
test
```


---

## Step 5: Delete the deprecated service layer

**Priority:** Medium — removes dead code and simplifies maintenance.

**Problem:** The `src/tyo3/services/` directory contains 5 deprecated service
classes that duplicate `TyO3Session` functionality. They're marked deprecated
since Phase 6 but still present with tests. Keeping them adds maintenance
burden and confuses new contributors.

**Files to delete:**
- `src/tyo3/services/__init__.py`
- `src/tyo3/services/project_service.py`
- `src/tyo3/services/analysis_service.py`
- `src/tyo3/services/symbol_service.py`
- `src/tyo3/services/navigation_service.py`
- `src/tyo3/services/advanced_service.py`

**Test files to delete:**
- `src/tyo3/tests/test_project_service.py`
- `src/tyo3/tests/test_analysis_service.py`
- `src/tyo3/tests/test_symbol_service.py`
- `src/tyo3/tests/test_navigation_service.py`
- `src/tyo3/tests/test_advanced_service.py`

### 5.1 — Check for non-test imports of services

Before deleting, confirm nothing outside the service layer imports from it:

```bash
# Search for imports of services (excluding tests and the services dir itself)
grep -rn "from tyo3.services" src/tyo3/ --include="*.py" | grep -v __pycache__ | grep -v "/services/" | grep -v "/tests/"
```

If `__init__.py` (the package root) re-exports services, remove those exports.

Also check `conftest.py`:

```bash
grep -n "service" src/tyo3/tests/conftest.py
```

Remove any service-related fixtures from `conftest.py`.

### 5.2 — Delete the files

```bash
rm -rf src/tyo3/services/
rm -f src/tyo3/tests/test_project_service.py
rm -f src/tyo3/tests/test_analysis_service.py
rm -f src/tyo3/tests/test_symbol_service.py
rm -f src/tyo3/tests/test_navigation_service.py
rm -f src/tyo3/tests/test_advanced_service.py
```

### 5.3 — Clean up conftest.py

Remove any fixtures that instantiate service classes. These will look like:

```python
@pytest.fixture
def project_service(...):
    return ProjectService(...)
```

Remove the imports and fixture functions, but **keep** all model-related
fixtures (they're used by other tests).

### 5.4 — Clean up any remaining references

```bash
# Find any remaining references to the deleted modules
grep -rn "service" src/tyo3/ --include="*.py" | grep -v __pycache__ | grep -vi "# " | grep -i "Service"
```

Remove or update any stale references. The session.py docstring mentions
"Replaces the separate ProjectService, AnalysisService..." — keep this as
historical context, it's still accurate.

### 5.5 — Verify

```bash
# Confirm services directory is gone
ls src/tyo3/services/ 2>&1
# Expected: "No such file or directory"

# Confirm no service test files remain
ls src/tyo3/tests/test_*_service.py 2>&1
# Expected: "No such file or directory"

# Confirm no broken imports
python -c "import tyo3; print('OK')"
python -c "from tyo3 import TyO3Session; print('OK')"

# Run full test suite (use devenv command)
test

# Confirm test count decreased (services had ~5 test files)
PYTHONPATH=src python -m pytest src/tyo3/tests/ --co -q | tail -1
```


---

## Step 6: Define `__all__` exports across all public modules

**Priority:** Medium — makes the public API explicit and prevents accidental
exposure of internals.

**Problem:** Only `src/tyo3/__init__.py` has `__all__`. The model modules,
`session.py`, `rust_project.py`, and `exceptions.py` do not, so
`from tyo3.models.core import *` would pull in everything.

**Files:** All public Python modules under `src/tyo3/`.

### 6.1 — Add `__all__` to each module

**`src/tyo3/exceptions.py`:**

```python
__all__ = [
    "TyO3Error",
    "ProjectOpenError",
    "ProjectClosedError",
    "PathResolutionError",
    "PositionError",
    "AnalysisError",
    "InternalTyError",
]
```

**`src/tyo3/models/core.py`:**

```python
__all__ = [
    "TyProjectConfig",
    "BackendInfo",
    "ProjectStatus",
    "FileCategory",
    "CoordinateMode",
    "TyProject",
    "ProjectFile",
]
```

**`src/tyo3/models/analysis.py`:**

```python
__all__ = [
    "Position",
    "Range",
    "FileRange",
    "CheckResult",
    "DiagnosticSeverity",
    "Diagnostic",
]
```

**`src/tyo3/models/symbols.py`:**

```python
__all__ = [
    "SymbolKind",
    "Symbol",
]
```

**`src/tyo3/models/navigation.py`:**

```python
__all__ = [
    "ReferenceKind",
    "HoverContentKind",
    "DefinitionTarget",
    "Reference",
    "HoverContent",
    "HoverResult",
]
```

**`src/tyo3/models/advanced.py`:**

```python
__all__ = [
    "SemanticTokenType",
    "SemanticTokenModifier",
    "SemanticToken",
]
```

**`src/tyo3/models/__init__.py`:** Add re-exports of all public model classes
so users can do `from tyo3.models import Symbol, Diagnostic, ...`:

```python
"""TyO3 domain models."""

from tyo3.models.advanced import *  # noqa: F401,F403
from tyo3.models.analysis import *  # noqa: F401,F403
from tyo3.models.core import *  # noqa: F401,F403
from tyo3.models.navigation import *  # noqa: F401,F403
from tyo3.models.symbols import *  # noqa: F401,F403
```

**`src/tyo3/__init__.py`:** Expand to export key types alongside `TyO3Session`:

```python
__all__ = [
    "TyO3Session",
    # Re-export commonly used types for convenience
    "CheckResult",
    "Diagnostic",
    "Symbol",
    "DefinitionTarget",
    "Reference",
    "HoverResult",
]
```

Do **not** add `__all__` to `rust_project.py` — it's an internal module
(not part of the public API). Users should interact via `TyO3Session`.

### 6.2 — Verify

```bash
# Confirm __all__ is defined in all public modules
for f in src/tyo3/exceptions.py src/tyo3/models/core.py src/tyo3/models/analysis.py \
         src/tyo3/models/symbols.py src/tyo3/models/navigation.py src/tyo3/models/advanced.py \
         src/tyo3/__init__.py; do
    echo -n "$f: "
    grep -c "__all__" "$f"
done
# Expected: each file shows 1

# Confirm rust_project.py does NOT have __all__
grep "__all__" src/tyo3/rust_project.py
# Expected: 0 matches

# Confirm imports still work
python -c "from tyo3 import TyO3Session; print('OK')"
python -c "from tyo3.models import Symbol, Diagnostic, CheckResult; print('OK')"
python -c "from tyo3.exceptions import TyO3Error, InternalTyError; print('OK')"

# Run full suite (use devenv command)
test
```


---

## Step 7: Add README with installation and usage docs

**Priority:** Medium — the README is currently empty.

**File:** `README.md`

### 7.1 — Write the README

Replace the empty `README.md` with the following:

```markdown
# TyO3

Python semantic engine powered by [ty](https://github.com/astral-sh/ty),
the extremely fast Python type checker from the creators of Ruff.

TyO3 wraps ty's Rust backend via PyO3, providing a Pydantic-validated
Python API for type checking, symbol discovery, code navigation, and
hover information.

## Requirements

- Python >= 3.13
- Rust toolchain (for building the native extension)
- [maturin](https://www.maturin.rs/) >= 1.7

## Installation

### Development (from source)

```bash
# Clone the repository
git clone <repo-url> && cd tyo3

# If using devenv (recommended):
devenv shell
build          # builds the Rust extension in debug mode

# Or manually with maturin:
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

## API

### `TyO3Session(root)`

The main entry point. Use as a context manager.

| Method | Returns | Description |
|--------|---------|-------------|
| `files()` | `list[PurePosixPath]` | All files known to the project |
| `check()` | `CheckResult` | Type-check the entire project |
| `check_file(path)` | `CheckResult` | Type-check and filter to one file |
| `document_symbols(path)` | `list[Symbol]` | Symbols defined in a file |
| `workspace_symbols(query)` | `list[Symbol]` | Search symbols across project |
| `goto_definition(path, line, col)` | `list[DefinitionTarget]` | Go to definition |
| `goto_declaration(path, line, col)` | `list[DefinitionTarget]` | Go to declaration |
| `goto_type_definition(path, line, col)` | `list[DefinitionTarget]` | Go to type definition |
| `find_references(path, line, col)` | `list[Reference]` | Find all references |
| `hover(path, line, col)` | `HoverResult \| None` | Hover/type info |
| `reload()` | `None` | Reload project state |
| `close()` | `None` | Free Rust-side resources |

All positions are **1-based** (line >= 1, column >= 1).

### Thread Safety

`TyO3Session` is **not thread-safe**. Do not share a single session across
threads. Create separate sessions per thread if needed.

## Development

```bash
devenv shell     # enter the development environment
build            # compile Rust extension (debug)
test             # run full test suite with coverage
test-quick       # run tests without coverage
```

## License

MIT
```

### 7.2 — Verify

```bash
# Confirm README has content
wc -l README.md
# Expected: ~100+ lines

# Confirm key sections exist
grep -c "## Quick Start" README.md
grep -c "## API" README.md
grep -c "Thread Safety" README.md
grep -c "TyO3Session" README.md
```


---

## Step 8: Make `close()` idempotent and add finalizer warning

**Priority:** Low — defensive improvement to resource cleanup.

**Problem:** `RustProject.close()` calls `self._inner.close()` unconditionally.
If the Rust side raises on double-close, this is fragile. Also, `__del__`
silently swallows errors, so users who forget `close()` or `with` get no warning.

**File:** `src/tyo3/rust_project.py`

### 8.1 — Add a `_closed` flag and make `close()` idempotent

```python
# In RustProject.__init__, after self._root = ..., add:
self._closed = False

# Replace close():
def close(self) -> None:
    """Close the project and free Rust-side resources.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    if self._closed:
        return
    self._inner.close()
    self._closed = True
```

### 8.2 — Add a finalizer warning in `__del__`

```python
import warnings

def __del__(self) -> None:
    if not self._closed:
        warnings.warn(
            "RustProject was not closed explicitly. "
            "Use 'with RustProject(...) as rp:' or call rp.close().",
            ResourceWarning,
            stacklevel=2,
        )
        try:
            self.close()
        except Exception:
            pass
```

### 8.3 — Add tests

```python
# In test_exceptions.py or a new test_lifecycle.py
import warnings
from unittest.mock import MagicMock


class TestCloseIdempotent:
    def test_double_close_no_error(self):
        """Calling close() twice should not raise."""
        # We can test the _closed flag logic without the Rust backend
        # by mocking _inner
        from tyo3.rust_project import RustProject

        rp = object.__new__(RustProject)
        rp._inner = MagicMock()
        rp._closed = False
        rp._root = None

        rp.close()
        assert rp._closed is True
        rp._inner.close.assert_called_once()

        # Second close should be a no-op
        rp.close()
        rp._inner.close.assert_called_once()  # still only once
```

### 8.4 — Verify

```bash
# Confirm _closed flag is set in __init__
grep -n "_closed" src/tyo3/rust_project.py

# Confirm close() checks the flag
grep -A5 "def close" src/tyo3/rust_project.py | grep "_closed"

# Confirm ResourceWarning in __del__
grep -n "ResourceWarning" src/tyo3/rust_project.py

# Run full suite (use devenv command)
test
```


---

## Step 9: Add Range validation to models

**Priority:** Low — reinforces data integrity at the model layer.

**Problem:** `Range(start, end)` doesn't validate that `start <= end`.
While the Rust backend should always return valid ranges, adding a
Pydantic validator catches bugs early if models are constructed manually.

**File:** `src/tyo3/models/analysis.py`

### 9.1 — Add a model validator

```python
from pydantic import BaseModel, Field, model_validator


class Range(BaseModel):
    """A range between two positions."""

    start: Position
    end: Position

    @model_validator(mode="after")
    def _start_before_end(self) -> Range:
        s, e = self.start, self.end
        if (s.line, s.column) > (e.line, e.column):
            raise ValueError(
                f"Range start ({s.line}:{s.column}) must not be "
                f"after end ({e.line}:{e.column})"
            )
        return self
```

Add `model_validator` to the pydantic imports at the top of the file.

### 9.2 — Add a Position validator

Positions must be >= 1 (1-based):

```python
class Position(BaseModel):
    """A 1-based position in a file."""

    line: int  # 1-based
    column: int  # 1-based, Unicode codepoints

    @model_validator(mode="after")
    def _positive(self) -> Position:
        if self.line < 1 or self.column < 1:
            raise ValueError(
                f"Position must be 1-based: got line={self.line}, column={self.column}"
            )
        return self
```

### 9.3 — Add tests

```python
# In test_models_analysis.py
import pytest
from pydantic import ValidationError
from tyo3.models.analysis import Position, Range


class TestPositionValidation:
    def test_valid_position(self):
        p = Position(line=1, column=1)
        assert p.line == 1

    def test_rejects_zero_line(self):
        with pytest.raises(ValidationError, match="1-based"):
            Position(line=0, column=1)

    def test_rejects_negative_column(self):
        with pytest.raises(ValidationError, match="1-based"):
            Position(line=1, column=-1)


class TestRangeValidation:
    def test_valid_range(self):
        r = Range(
            start=Position(line=1, column=1),
            end=Position(line=1, column=10),
        )
        assert r.start.column == 1

    def test_same_position_is_valid(self):
        """A zero-width range (cursor position) is allowed."""
        r = Range(
            start=Position(line=5, column=3),
            end=Position(line=5, column=3),
        )
        assert r.start == r.end

    def test_rejects_inverted_range(self):
        with pytest.raises(ValidationError, match="must not be after"):
            Range(
                start=Position(line=10, column=1),
                end=Position(line=1, column=1),
            )

    def test_rejects_inverted_columns_same_line(self):
        with pytest.raises(ValidationError, match="must not be after"):
            Range(
                start=Position(line=5, column=20),
                end=Position(line=5, column=10),
            )
```

### 9.4 — Check existing tests for breakage

Some existing tests may construct `Range` or `Position` with test values
that violate these new constraints (e.g., `line=0` in edge-case tests).
Search for these:

```bash
grep -rn "Position(line=0" src/tyo3/tests/ --include="*.py"
grep -rn "Position(line=-" src/tyo3/tests/ --include="*.py"
```

If found, update those tests to use valid positions or wrap them in
`pytest.raises(ValidationError)` if they're testing rejection.

### 9.5 — Verify

```bash
# Confirm model_validator is imported
grep "model_validator" src/tyo3/models/analysis.py

# Confirm validators exist on both Position and Range
grep -A3 "_positive\|_start_before_end" src/tyo3/models/analysis.py

# Run model tests then full suite (use devenv commands)
PYTHONPATH=src python -m pytest src/tyo3/tests/test_models_analysis.py -v

# Run full suite — validators may cause failures in other tests
test
```


---

## Step 10: Final verification and cleanup

**Priority:** Required — confirms the full refactoring is complete and consistent.

### 10.1 — Run the full test suite

```bash
# Use the devenv command (runs with coverage, verbose, short tracebacks)
test
```

All tests must pass. If any fail, fix them before proceeding.

### 10.2 — Run type checking

```bash
# mypy (strict mode is configured in pyproject.toml)
# Run from devenv shell — Python 3.13 and mypy are provisioned by devenv
PYTHONPATH=src mypy src/tyo3/ --ignore-missing-imports

# Lint with ruff (also provisioned by devenv)
ruff check src/tyo3/
```

Fix any type errors or lint violations introduced by the refactoring.

### 10.3 — Run the comprehensive verification checklist

Execute each of these commands and confirm expected results:

```bash
echo "=== 1. check_file uses path argument ==="
grep -n "target" src/tyo3/session.py | head -5
# Expected: PurePosixPath(path) or similar path matching logic

echo "=== 2. InternalTyError catch-all in every RustProject method ==="
grep -c "except Exception as e" src/tyo3/rust_project.py
# Expected: 8

echo "=== 3. CoordinateMode enum exists ==="
grep "class CoordinateMode" src/tyo3/models/core.py
# Expected: 1 match

echo "=== 4. files() returns PurePosixPath ==="
grep "def files.*PurePosixPath" src/tyo3/session.py src/tyo3/rust_project.py
# Expected: 2 matches

echo "=== 5. No service layer ==="
ls src/tyo3/services/ 2>&1
# Expected: "No such file or directory"

echo "=== 6. __all__ in public modules ==="
for f in src/tyo3/__init__.py src/tyo3/exceptions.py \
         src/tyo3/models/core.py src/tyo3/models/analysis.py \
         src/tyo3/models/symbols.py src/tyo3/models/navigation.py \
         src/tyo3/models/advanced.py; do
    echo -n "  $f: "; grep -c "__all__" "$f"
done
# Expected: each shows 1

echo "=== 7. README has content ==="
wc -l README.md
# Expected: 80+ lines

echo "=== 8. close() is idempotent ==="
grep "_closed" src/tyo3/rust_project.py | head -3
# Expected: _closed flag usage

echo "=== 9. Range/Position validators ==="
grep "model_validator" src/tyo3/models/analysis.py
# Expected: 2 matches (one per validator)

echo "=== 10. All tests pass ==="
test-ci
```

### 10.4 — Review the commit history

After all steps, the git log should show ~9 clean commits (one per step).
Review with:

```bash
git log --oneline -15
```

Each commit message should reference the step number, e.g.:
- `refactor: fix check_file() path filtering bug (step 1)`
- `refactor: add InternalTyError catch-all in RustProject (step 2)`
- etc.

---

## Summary of changes

| Step | Priority | What changed | Files affected |
|------|----------|-------------|----------------|
| 1 | Critical | Fix `check_file()` — filter by path argument | `session.py`, new test file |
| 2 | High | Add `InternalTyError` catch-all to all `RustProject` methods | `rust_project.py` |
| 3 | Medium | Convert `coordinate_mode` from `str` to `CoordinateMode` enum | `models/core.py`, tests |
| 4 | Medium | Change `files()` return type to `list[PurePosixPath]` | `session.py`, `rust_project.py` |
| 5 | Medium | Delete deprecated service layer and its tests | `services/`, 5 test files, `conftest.py` |
| 6 | Medium | Add `__all__` to all public modules | All model files, `exceptions.py`, `__init__.py` |
| 7 | Medium | Write README with usage, API docs, thread safety note | `README.md` |
| 8 | Low | Make `close()` idempotent, add `ResourceWarning` in `__del__` | `rust_project.py` |
| 9 | Low | Add Pydantic validators for `Position` and `Range` | `models/analysis.py`, tests |
| 10 | Required | Final verification of all changes | N/A |

## What we deliberately did NOT do

These items from the original review were evaluated and deferred:

| Suggestion | Why deferred |
|-----------|--------------|
| Add `frozen=True` to models | No demonstrated need — models aren't used as dict keys or in sets |
| Switch to `orjson` | Premature optimization — JSON parsing is not a bottleneck |
| Separate client/model layers in `RustProject` | Adds abstraction without solving a real problem at ~360 lines |
| Lazy/streaming diagnostics | No evidence of memory issues at current scale |
| `ProjectFile.project` circular ref | Pydantic v2 handles this fine; no serialization issues observed |
| Add `black`/`isort` | Project already uses Ruff for both linting and formatting |
| Multi-platform CI matrix | One platform is fine for v0.1; expand when needed |

---

## Appendix: devenv workflow cheat sheet

This refactoring only changes Python files, so you should **not** need to
rebuild the Rust extension during these steps. However, if you ever touch
files under `rust/`, the workflow is:

```bash
# 1. Rebuild the native extension (from devenv shell)
build

# 2. Verify it loads
check-so

# 3. Run tests
test
```

### Typical iteration loop for this refactoring

```bash
# Edit Python files...
# Then:
test-quick    # fast feedback — skips Rust integration tests
test          # full suite once test-quick passes
git add <files>
git commit -m "refactor: <description> (step N)"
```

### If something goes wrong

```bash
status        # check toolchain versions and .so presence
check-so      # verify native extension loads
clean         # nuclear option: wipe all artifacts
build         # rebuild from scratch
test          # re-run full suite
```

### Common mistakes to avoid

1. **Running `pytest` directly** — always use `test` / `test-quick` / `test-ci`
   or prefix with `PYTHONPATH=src` for targeted runs.
2. **Running `cargo build` directly** — use `build` instead; it copies the `.so`
   to the correct location with the correct filename.
3. **Forgetting to rebuild after Rust changes** — Python will import a stale `.so`.
4. **Working outside the devenv shell** — system Python/Rust versions will differ
   and imports will fail.

