# Test Suite Performance Optimization Plan

## Context

The test suite (374 tests) takes ~34 seconds. The primary bottleneck is **redundant RustProject/TyO3Session creation** — each call to `RustProject(path)` invokes Rust FFI to scan files and build an index (~100-400ms). There are **88 `RustProject()` instantiations** across 9 test files, plus **~27 `TyO3Session` + `CodeGraph.build()`** calls in `test_graph.py`. Many of these are redundant — tests using the same fixture could share a single project instance.

## Changes

### 1. `test_property_based.py` — CRITICAL (est. 5-10s savings)

Each Hypothesis example (up to 100 per test) creates a **new RustProject**. With 8 test functions, that's 300-800 project opens.

**Fix**: Cache projects per fixture name using a module-level dict with a finalizer:

```python
_project_cache: dict[str, RustProject] = {}

@pytest.fixture(autouse=True, scope="module")
def _cleanup_project_cache():
    yield
    for rp in _project_cache.values():
        rp.close()
    _project_cache.clear()

def get_project(fixture_name: str) -> RustProject:
    if fixture_name not in _project_cache:
        _project_cache[fixture_name] = RustProject(fixture_path(fixture_name))
    return _project_cache[fixture_name]
```

Then replace all `rp = RustProject(fixture_path(...))` + try/finally/close patterns with `rp = get_project(fixture_name)`.

### 2. `test_graph.py` — HIGH (est. 3-5s savings)

27 integration tests each create `TyO3Session` + `CodeGraph.build()`. Many use the same fixture repeatedly:
- `graph_test`: ~10 tests
- `classes`: ~5 tests
- `simple_package`: ~6 tests

**Fix**: Add module-level cached session/graph fixtures in the file:

```python
_graph_cache: dict[str, CodeGraph] = {}
_session_cache: dict[str, TyO3Session] = {}

def get_graph(fixture_name: str) -> CodeGraph:
    if fixture_name not in _graph_cache:
        session = TyO3Session(fixture_path(fixture_name))
        _session_cache[fixture_name] = session
        _graph_cache[fixture_name] = CodeGraph.build(session)
    return _graph_cache[fixture_name]
```

Replace each test's `with TyO3Session(...) as session: graph = CodeGraph.build(session)` with `graph = get_graph("fixture_name")`. These tests are read-only queries on the graph so sharing is safe.

### 3. `test_rust_snapshots.py` — MEDIUM (est. 1-2s savings)

5 classes use `autouse=True` function-scoped fixture + `teardown_method` — opens/closes per test method (5 classes × 2-3 tests = ~12 opens).

**Fix**: Change fixture scope to `class` and remove `teardown_method`:

```python
@pytest.fixture(autouse=True, scope="class")
def setup(self) -> Generator:
    self.__class__.rp = RustProject(fixture_path("simple_package"))
    yield
    self.__class__.rp.close()
```

This reduces to 5 opens total (one per class).

### 4. `test_rust_integration.py`, `test_check_file.py`, `test_semantic_tokens.py`, `test_file_occurrences.py`, `test_type_hierarchy.py` — MEDIUM (est. 2-3s combined)

Same pattern: per-test `RustProject()` + close. Apply the same caching approach — group by fixture and share a single project instance within each file.

Use a module-level cache + cleanup fixture, same pattern as #1.

### 5. `test_coordinate_conversion.py` — LOW-MEDIUM (est. 0.5-1s)

Similar pattern with Hypothesis tests. Apply same caching.

## Files to Modify

1. `src/tyo3/tests/test_property_based.py` — cache RustProject per fixture
2. `src/tyo3/tests/test_graph.py` — cache TyO3Session/CodeGraph per fixture
3. `src/tyo3/tests/test_rust_snapshots.py` — class-scoped fixtures
4. `src/tyo3/tests/test_rust_integration.py` — cache RustProject per fixture
5. `src/tyo3/tests/test_check_file.py` — cache RustProject per fixture
6. `src/tyo3/tests/test_semantic_tokens.py` — cache RustProject per fixture
7. `src/tyo3/tests/test_file_occurrences.py` — cache RustProject per fixture
8. `src/tyo3/tests/test_type_hierarchy.py` — cache RustProject per fixture
9. `src/tyo3/tests/test_coordinate_conversion.py` — cache RustProject per fixture

## Safety

- All cached projects are **read-only** in tests (no mutation of project state)
- `test_rust_integration.py` has some tests that explicitly test `close()` or `reload()` — those tests must keep their own dedicated project instances
- Caches are cleaned up via module-scoped finalizer fixtures

## Verification

Run `devenv shell -- tests` and confirm:
- All 374 tests still pass
- Runtime drops significantly (target: ~15-20s)
