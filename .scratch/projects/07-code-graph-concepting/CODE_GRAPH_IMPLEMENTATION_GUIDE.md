# Code Graph Implementation Guide

Step-by-step instructions for implementing the Intelligent Code Graph as described in `INTELLIGENT_CODE_GRAPH_CONCEPT.md`. Each step includes the exact files to create or modify, the code to write, and the tests to add.

**Prerequisites:** Read `INTELLIGENT_CODE_GRAPH_CONCEPT.md` first. Understand the existing TyO3 codebase — especially `session.py`, `rust_project.py`, `project.rs`, and the `dto/` + `convert/` Rust layers.

**Convention notes:**
- All Rust DTOs use `#[pyclass(name = "NativeXxx", frozen, module = "tyo3._native_impl")]`
- All Rust converters follow the pattern: private `_to_dto` mapper + public `convert_*` function
- All Python bridge methods call `self._check_open()` first, wrap Rust calls in `try/except`, and validate with `Model.model_validate(_to_python(result))`
- All Python models use `ConfigDict(from_attributes=True)`
- Integration tests use `needs_native = pytest.mark.skipif(not _HAS_NATIVE, ...)` and construct `RustProject` directly with `fixture_path("fixture_name")`

---

## Phase 1: Graph Foundation (Python only — no Rust changes)

This phase builds a working code graph using only existing TyO3 APIs. It will be slow for large projects (O(symbols) cursor calls per file) but validates the data model before investing in Rust work.

### Step 1.1: Add `rustworkx` dependency

**File: `pyproject.toml`**

Add `rustworkx` to the runtime dependencies alongside `pydantic`:

```toml
dependencies = [
    "pydantic>=2.12.5",
    "rustworkx>=0.16.0",
]
```

Run `uv sync` (or equivalent) to install.

**Verification:** `python -c "import rustworkx; print(rustworkx.__version__)"` succeeds.

---

### Step 1.2: Create graph models

**File: `src/tyo3/graph/__init__.py`** (new)

```python
"""TyO3 Code Graph — semantic code intelligence graph."""

from __future__ import annotations

from tyo3.graph.models import EdgeData, EdgeKind, ReferenceRole, SymbolNode
from tyo3.graph.graph import CodeGraph

__all__ = [
    "CodeGraph",
    "EdgeData",
    "EdgeKind",
    "ReferenceRole",
    "SymbolNode",
]
```

**File: `src/tyo3/graph/models.py`** (new)

```python
"""Data models for graph node and edge payloads."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from tyo3.models.analysis import Range
from tyo3.models.symbols import SymbolKind


# ── Node payload (Pydantic — consistency + serialization) ──


class SymbolNode(BaseModel):
    """A symbol in the code graph. Stored as a RustworkX node payload."""

    model_config = ConfigDict(frozen=True)

    symbol_id: str
    name: str
    qualified_name: str
    kind: SymbolKind
    file: str
    range: Range
    selection_range: Range | None = None
    documentation: str | None = None
    signature: str | None = None
    external: bool = False
    package: str | None = None


# ── Edge payload (dataclass — construction speed) ──


class EdgeKind(StrEnum):
    """Classification of a relationship between two symbols."""

    DEFINES = "defines"
    CONTAINS = "contains"
    REFERENCES = "references"
    IMPORTS = "imports"
    INHERITS = "inherits"
    OVERRIDES = "overrides"
    TYPE_OF = "type_of"
    RETURNS = "returns"
    INSTANTIATES = "instantiates"


class ReferenceRole(StrEnum):
    """How a symbol is used at a reference site."""

    READ = "read"
    WRITE = "write"
    IMPORT = "import"
    DEFINITION = "definition"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class EdgeData:
    """Payload stored on every graph edge."""

    kind: EdgeKind
    file: str | None = None
    range: Range | None = None
    role: ReferenceRole | None = None
```

**Verification:** `python -c "from tyo3.graph.models import SymbolNode, EdgeData, EdgeKind"` succeeds.

---

### Step 1.3: Create symbol identity helpers

**File: `src/tyo3/graph/identity.py`** (new)

```python
"""Symbol identity construction for the code graph."""

from __future__ import annotations

from tyo3.models.symbols import Symbol


def make_symbol_id(file: str, qualified_name: str) -> str:
    """Create a canonical symbol ID from a file path and qualified name."""
    return f"{file}::{qualified_name}"


def make_external_id(package: str, qualified_name: str) -> str:
    """Create a canonical symbol ID for an external (dependency) symbol."""
    return f"{package}::{qualified_name}"


def symbol_id_from_symbol(file: str, symbol: Symbol) -> str:
    """Derive a symbol_id from a TyO3 Symbol model.

    Uses qualified_name if available, otherwise falls back to
    name@line for uniqueness.
    """
    if symbol.qualified_name:
        return make_symbol_id(file, symbol.qualified_name)
    return make_symbol_id(file, f"{symbol.name}@{symbol.location.range.start.line}")


def file_from_symbol_id(symbol_id: str) -> str:
    """Extract the file path (or package name) from a symbol_id."""
    return symbol_id.split("::", 1)[0]


def qualified_name_from_symbol_id(symbol_id: str) -> str:
    """Extract the qualified name from a symbol_id."""
    return symbol_id.split("::", 1)[1]
```

---

### Step 1.4: Build the `CodeGraph` class — construction

This is the core class. Build it incrementally. Start with construction from existing APIs.

**File: `src/tyo3/graph/graph.py`** (new)

```python
"""CodeGraph — the semantic code intelligence graph."""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import PurePosixPath

import rustworkx as rx

from tyo3.graph.identity import file_from_symbol_id, symbol_id_from_symbol
from tyo3.graph.models import EdgeData, EdgeKind, ReferenceRole, SymbolNode
from tyo3.models.analysis import Diagnostic, Range
from tyo3.models.symbols import Symbol, SymbolKind
from tyo3.session import TyO3Session

logger = logging.getLogger(__name__)


class CodeGraph:
    """A semantic code intelligence graph for a Python project."""

    def __init__(self) -> None:
        self._graph: rx.PyDiGraph = rx.PyDiGraph()

        # Secondary indexes
        self._id_to_index: dict[str, int] = {}
        self._file_to_nodes: dict[str, list[int]] = defaultdict(list)
        self._file_to_edges: dict[str, list[int]] = defaultdict(list)

        # Diagnostics (separate from graph)
        self._diagnostics: dict[str, list[Diagnostic]] = {}

    # ── Construction ──────────────────────────────────────────

    @classmethod
    def build(cls, session: TyO3Session) -> CodeGraph:
        """Build a complete code graph from a TyO3 session.

        Iterates all project files and populates symbols, containment
        edges, reference edges, and diagnostics.
        """
        graph = cls()
        files = session.files()

        for file_path in files:
            file_str = str(file_path)
            graph._index_file(session, file_str)

        return graph

    def _index_file(self, session: TyO3Session, file_str: str) -> None:
        """Index a single file: add symbols, containment, references, diagnostics."""

        # 1. Get symbols and add nodes
        try:
            symbols = session.document_symbols(file_str)
        except Exception:
            logger.warning("Failed to get symbols for %s, skipping", file_str)
            return

        # Create MODULE node for the file
        module_id = f"{file_str}::<module>"
        module_node = SymbolNode(
            symbol_id=module_id,
            name=PurePosixPath(file_str).stem,
            qualified_name="<module>",
            kind=SymbolKind.MODULE,
            file=file_str,
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}
            ),
        )
        self._add_node(module_node)

        # Add symbol nodes and containment edges
        for symbol in symbols:
            self._add_symbol_node(file_str, symbol, module_id)

        # 2. Build reference edges using find_references per symbol
        #    This is the slow path — O(symbols) cursor calls per file.
        #    Phase 2 replaces this with semantic tokens.
        for symbol in symbols:
            self._resolve_references_for_symbol(session, file_str, symbol)

        # 3. Get diagnostics
        try:
            result = session.check_file(file_str)
            self._diagnostics[file_str] = result.diagnostics
        except Exception:
            logger.warning("Failed to check %s, skipping diagnostics", file_str)

    def _add_symbol_node(
        self,
        file_str: str,
        symbol: Symbol,
        module_id: str,
    ) -> None:
        """Create a SymbolNode from a TyO3 Symbol and add it to the graph."""
        sid = symbol_id_from_symbol(file_str, symbol)

        node = SymbolNode(
            symbol_id=sid,
            name=symbol.name,
            qualified_name=symbol.qualified_name or symbol.name,
            kind=symbol.kind,
            file=file_str,
            range=symbol.location.range,
            selection_range=symbol.selection_range,
        )
        self._add_node(node)

        # Containment edge: determine parent
        parent_id = self._resolve_parent_id(file_str, symbol, module_id)
        if parent_id and parent_id in self._id_to_index:
            edge_kind = EdgeKind.DEFINES if parent_id == module_id else EdgeKind.CONTAINS
            edge = EdgeData(kind=edge_kind)
            self._add_edge(parent_id, sid, edge, file_str)

    def _resolve_parent_id(
        self,
        file_str: str,
        symbol: Symbol,
        module_id: str,
    ) -> str | None:
        """Determine the parent symbol_id for containment edges.

        Uses container_name from the Symbol to find the parent.
        Falls back to the module node for top-level symbols.
        """
        if symbol.container_name:
            # Try to find a node matching the container name
            candidate_id = f"{file_str}::{symbol.container_name}"
            if candidate_id in self._id_to_index:
                return candidate_id
        return module_id

    def _resolve_references_for_symbol(
        self,
        session: TyO3Session,
        file_str: str,
        symbol: Symbol,
    ) -> None:
        """Add REFERENCES edges for a symbol using find_references.

        For each reference site, we create an edge from the enclosing
        symbol (at the reference site) to the definition symbol.
        """
        sid = symbol_id_from_symbol(file_str, symbol)
        start = symbol.location.range.start

        try:
            refs = session.find_references(
                file_str, start.line, start.column, include_declaration=False
            )
        except Exception:
            return

        for ref in refs:
            ref_file = str(ref.path)
            # Find the enclosing symbol at the reference site
            enclosing_id = self._find_enclosing_symbol(ref_file, ref.range)
            if enclosing_id is None:
                continue

            # Map ReferenceKind to ReferenceRole
            role = ReferenceRole.OTHER
            if ref.kind.value == "read":
                role = ReferenceRole.READ
            elif ref.kind.value == "write":
                role = ReferenceRole.WRITE

            edge = EdgeData(
                kind=EdgeKind.REFERENCES,
                file=ref_file,
                range=ref.range,
                role=role,
            )
            self._add_edge(enclosing_id, sid, edge, ref_file)

    def _find_enclosing_symbol(self, file_str: str, range: Range) -> str | None:
        """Find the innermost symbol in file_str that contains the given range.

        Returns the symbol_id, or the module node if no enclosing symbol is found.
        """
        node_indices = self._file_to_nodes.get(file_str, [])
        best_id: str | None = None
        best_size: int = float("inf")  # type: ignore[assignment]

        for idx in node_indices:
            node: SymbolNode = self._graph[idx]
            if node.kind == SymbolKind.MODULE:
                continue
            nr = node.range
            # Check if node's range contains the reference range
            if (
                (nr.start.line, nr.start.column) <= (range.start.line, range.start.column)
                and (nr.end.line, nr.end.column) >= (range.end.line, range.end.column)
            ):
                size = (nr.end.line - nr.start.line) * 10000 + (nr.end.column - nr.start.column)
                if size < best_size:
                    best_size = size
                    best_id = node.symbol_id

        if best_id is None:
            # Fall back to module node
            module_id = f"{file_str}::<module>"
            if module_id in self._id_to_index:
                return module_id

        return best_id

    # ── Internal graph mutation ───────────────────────────────

    def _add_node(self, node: SymbolNode) -> int:
        """Add a SymbolNode to the graph and update indexes."""
        if node.symbol_id in self._id_to_index:
            return self._id_to_index[node.symbol_id]
        idx = self._graph.add_node(node)
        self._id_to_index[node.symbol_id] = idx
        self._file_to_nodes[node.file].append(idx)
        return idx

    def _add_edge(
        self, source_id: str, target_id: str, data: EdgeData, file: str
    ) -> int | None:
        """Add an edge between two symbols by their IDs."""
        src_idx = self._id_to_index.get(source_id)
        tgt_idx = self._id_to_index.get(target_id)
        if src_idx is None or tgt_idx is None:
            return None
        edge_idx = self._graph.add_edge(src_idx, tgt_idx, data)
        self._file_to_edges[file].append(edge_idx)
        return edge_idx

    def _add_stub_node(
        self,
        symbol_id: str,
        name: str,
        qualified_name: str,
        kind: SymbolKind,
        package: str,
    ) -> int:
        """Add a stub node for an external symbol."""
        node = SymbolNode(
            symbol_id=symbol_id,
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            file="<external>",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}
            ),
            external=True,
            package=package,
        )
        return self._add_node(node)
```

**Note:** This is the foundation. Query methods are added in Step 1.5. The reference resolution is intentionally naive (per-symbol `find_references`) — Phase 2 replaces it.

---

### Step 1.5: Add query methods to `CodeGraph`

**File: `src/tyo3/graph/graph.py`** — add to the `CodeGraph` class:

```python
    # ── Properties ────────────────────────────────────────────

    @property
    def graph(self) -> rx.PyDiGraph:
        """The underlying RustworkX directed graph."""
        return self._graph

    @property
    def node_count(self) -> int:
        return self._graph.num_nodes()

    @property
    def edge_count(self) -> int:
        return self._graph.num_edges()

    # ── Symbol queries ────────────────────────────────────────

    def symbol(self, symbol_id: str) -> SymbolNode | None:
        """Look up a symbol by its canonical ID."""
        idx = self._id_to_index.get(symbol_id)
        return self._graph[idx] if idx is not None else None

    def symbols_in_file(self, path: str) -> list[SymbolNode]:
        """All symbols defined in a file."""
        return [self._graph[i] for i in self._file_to_nodes.get(path, [])]

    def symbols_of_kind(self, kind: SymbolKind) -> list[SymbolNode]:
        """All symbols of a given kind."""
        return [
            self._graph[i]
            for i in self._graph.node_indices()
            if self._graph[i].kind == kind
        ]

    # ── Reference queries ─────────────────────────────────────

    def references_to(self, symbol_id: str) -> list[EdgeData]:
        """All incoming REFERENCES edges to a symbol."""
        idx = self._id_to_index.get(symbol_id)
        if idx is None:
            return []
        result = []
        for pred_idx in self._graph.predecessors(idx):
            # Check all edges from pred to idx
            edge_data = self._graph.get_edge_data(pred_idx, idx)
            if edge_data is not None and edge_data.kind == EdgeKind.REFERENCES:
                result.append(edge_data)
        return result

    def references_from(self, symbol_id: str) -> list[tuple[SymbolNode, EdgeData]]:
        """All outgoing REFERENCES edges from a symbol."""
        idx = self._id_to_index.get(symbol_id)
        if idx is None:
            return []
        result = []
        for succ_idx in self._graph.neighbors(idx):
            edge_data = self._graph.get_edge_data(idx, succ_idx)
            if edge_data is not None and edge_data.kind == EdgeKind.REFERENCES:
                result.append((self._graph[succ_idx], edge_data))
        return result

    # ── Structural queries ────────────────────────────────────

    def children(self, symbol_id: str) -> list[SymbolNode]:
        """Direct children (outgoing DEFINES/CONTAINS edges)."""
        idx = self._id_to_index.get(symbol_id)
        if idx is None:
            return []
        result = []
        for succ_idx in self._graph.neighbors(idx):
            edge_data = self._graph.get_edge_data(idx, succ_idx)
            if edge_data is not None and edge_data.kind in (EdgeKind.DEFINES, EdgeKind.CONTAINS):
                result.append(self._graph[succ_idx])
        return result

    def parent(self, symbol_id: str) -> SymbolNode | None:
        """Enclosing symbol (incoming DEFINES/CONTAINS edge)."""
        idx = self._id_to_index.get(symbol_id)
        if idx is None:
            return None
        for pred_idx in self._graph.predecessors(idx):
            edge_data = self._graph.get_edge_data(pred_idx, idx)
            if edge_data is not None and edge_data.kind in (EdgeKind.DEFINES, EdgeKind.CONTAINS):
                return self._graph[pred_idx]
        return None

    def module_for(self, symbol_id: str) -> SymbolNode | None:
        """The MODULE node for this symbol's file."""
        file = file_from_symbol_id(symbol_id)
        module_id = f"{file}::<module>"
        return self.symbol(module_id)

    # ── Dependency analysis ───────────────────────────────────

    def dependencies(self, symbol_id: str) -> set[str]:
        """All symbols this one directly depends on."""
        idx = self._id_to_index.get(symbol_id)
        if idx is None:
            return set()
        return {
            self._graph[succ].symbol_id
            for succ in self._graph.neighbors(idx)
        }

    def dependents(self, symbol_id: str) -> set[str]:
        """All symbols that directly depend on this one."""
        idx = self._id_to_index.get(symbol_id)
        if idx is None:
            return set()
        return {
            self._graph[pred].symbol_id
            for pred in self._graph.predecessors(idx)
        }

    def transitive_dependencies(self, symbol_id: str) -> set[str]:
        """All symbols reachable from this one."""
        idx = self._id_to_index.get(symbol_id)
        if idx is None:
            return set()
        reachable = rx.descendants(self._graph, idx)
        return {self._graph[i].symbol_id for i in reachable}

    def transitive_dependents(self, symbol_id: str) -> set[str]:
        """All symbols that transitively depend on this one."""
        idx = self._id_to_index.get(symbol_id)
        if idx is None:
            return set()
        reachable = rx.ancestors(self._graph, idx)
        return {self._graph[i].symbol_id for i in reachable}

    # ── Graph algorithms ──────────────────────────────────────

    def coupling_between(self, file_a: str, file_b: str) -> int:
        """Count REFERENCES edges between two files."""
        count = 0
        nodes_a = set(self._file_to_nodes.get(file_a, []))
        nodes_b = set(self._file_to_nodes.get(file_b, []))
        for edge_idx in self._graph.edge_indices():
            src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
            data: EdgeData = self._graph.get_edge_data_by_index(edge_idx)
            if data.kind != EdgeKind.REFERENCES:
                continue
            if (src in nodes_a and tgt in nodes_b) or (src in nodes_b and tgt in nodes_a):
                count += 1
        return count

    # ── Diagnostics ───────────────────────────────────────────

    def diagnostics_for_file(self, path: str) -> list[Diagnostic]:
        """All diagnostics for a file."""
        return self._diagnostics.get(path, [])

    def diagnostics_for_symbol(self, symbol_id: str) -> list[Diagnostic]:
        """Diagnostics whose range overlaps this symbol's definition."""
        node = self.symbol(symbol_id)
        if node is None:
            return []
        file_diags = self._diagnostics.get(node.file, [])
        return [d for d in file_diags if d.range and _ranges_overlap(d.range, node.range)]

    def all_diagnostics(self) -> list[Diagnostic]:
        """All diagnostics across all files."""
        return [d for diags in self._diagnostics.values() for d in diags]


def _ranges_overlap(a: Range, b: Range) -> bool:
    """Check if two ranges overlap."""
    a_start = (a.start.line, a.start.column)
    a_end = (a.end.line, a.end.column)
    b_start = (b.start.line, b.start.column)
    b_end = (b.end.line, b.end.column)
    return a_start <= b_end and b_start <= a_end
```

---

### Step 1.6: Export from `tyo3` package

**File: `src/tyo3/__init__.py`**

Add the graph import:

```python
from tyo3.graph import CodeGraph
```

Add `"CodeGraph"` to `__all__`.

---

### Step 1.7: Create test fixtures for graph tests

**File: `fixtures/graph_test/` (new directory)**

Create a small multi-file Python project designed to exercise graph construction:

**File: `fixtures/graph_test/models.py`**

```python
class Base:
    def save(self) -> None:
        pass

class User(Base):
    name: str

    def __init__(self, name: str) -> None:
        self.name = name

    def save(self) -> None:
        print(f"Saving {self.name}")

MAX_USERS: int = 100
```

**File: `fixtures/graph_test/app.py`**

```python
from models import User, MAX_USERS

def create_user(name: str) -> User:
    if len(name) > MAX_USERS:
        raise ValueError("Name too long")
    return User(name)

def main() -> None:
    user = create_user("Alice")
    user.save()
```

---

### Step 1.8: Write tests

**File: `src/tyo3/tests/test_graph.py`** (new)

```python
"""Tests for the CodeGraph."""

from __future__ import annotations

from pathlib import Path as StdPath

import pytest

from tyo3.graph import CodeGraph
from tyo3.graph.models import EdgeKind, SymbolNode
from tyo3.graph.identity import make_symbol_id, symbol_id_from_symbol
from tyo3.models.symbols import SymbolKind

try:
    from tyo3 import _HAS_NATIVE
except ImportError:
    _HAS_NATIVE = False

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    return str((FIXTURES_DIR / name).resolve())


needs_native = pytest.mark.skipif(
    not _HAS_NATIVE, reason="Rust native extension not built"
)


# ── Unit tests (no native required) ──


class TestSymbolIdentity:
    def test_make_symbol_id(self) -> None:
        sid = make_symbol_id("src/models.py", "User")
        assert sid == "src/models.py::User"

    def test_make_symbol_id_nested(self) -> None:
        sid = make_symbol_id("src/models.py", "User.save")
        assert sid == "src/models.py::User.save"


class TestEdgeData:
    def test_frozen(self) -> None:
        from tyo3.graph.models import EdgeData

        edge = EdgeData(kind=EdgeKind.REFERENCES)
        with pytest.raises(AttributeError):
            edge.kind = EdgeKind.IMPORTS  # type: ignore[misc]


class TestSymbolNode:
    def test_frozen(self) -> None:
        node = SymbolNode(
            symbol_id="test::Foo",
            name="Foo",
            qualified_name="Foo",
            kind=SymbolKind.CLASS,
            file="test.py",
            range={"start": {"line": 1, "column": 1}, "end": {"line": 10, "column": 1}},
        )
        with pytest.raises(Exception):
            node.name = "Bar"  # type: ignore[misc]


# ── Integration tests (require native extension) ──


@needs_native
class TestGraphConstruction:
    def test_build_simple(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("simple_package")) as session:
            graph = CodeGraph.build(session)
            assert graph.node_count > 0
            assert graph.edge_count >= 0

    def test_nodes_have_module(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("simple_package")) as session:
            graph = CodeGraph.build(session)
            modules = graph.symbols_of_kind(SymbolKind.MODULE)
            assert len(modules) > 0

    def test_containment_edges_exist(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("classes")) as session:
            graph = CodeGraph.build(session)
            # Find any CLASS node and check it has a parent MODULE
            classes = graph.symbols_of_kind(SymbolKind.CLASS)
            if classes:
                parent = graph.parent(classes[0].symbol_id)
                assert parent is not None

    def test_diagnostics_populated(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("diagnostic_targets")) as session:
            graph = CodeGraph.build(session)
            all_diags = graph.all_diagnostics()
            assert len(all_diags) > 0


@needs_native
class TestGraphQueries:
    def test_symbol_lookup(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("classes")) as session:
            graph = CodeGraph.build(session)
            # Find a class by iterating all nodes
            classes = graph.symbols_of_kind(SymbolKind.CLASS)
            assert len(classes) > 0
            # Look it up by ID
            found = graph.symbol(classes[0].symbol_id)
            assert found is not None
            assert found.symbol_id == classes[0].symbol_id

    def test_symbols_in_file(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("classes")) as session:
            graph = CodeGraph.build(session)
            files = session.files()
            assert len(files) > 0
            symbols = graph.symbols_in_file(str(files[0]))
            assert len(symbols) > 0

    def test_children(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("classes")) as session:
            graph = CodeGraph.build(session)
            modules = graph.symbols_of_kind(SymbolKind.MODULE)
            if modules:
                kids = graph.children(modules[0].symbol_id)
                assert len(kids) > 0

    def test_transitive_dependencies(self) -> None:
        from tyo3.session import TyO3Session

        with TyO3Session(fixture_path("imports")) as session:
            graph = CodeGraph.build(session)
            # Any symbol should have a non-None result (possibly empty set)
            for idx in graph.graph.node_indices():
                node = graph.graph[idx]
                deps = graph.transitive_dependencies(node.symbol_id)
                assert isinstance(deps, set)
                break
```

**Verification:** Run `test-quick` (unit tests) and `test-rust` (integration tests). All tests pass.

---

## Phase 2: Wire Semantic Tokens (Rust + Python)

This phase adds `semantic_tokens()` to the TyO3 API stack. Semantic tokens are the backbone for identifying all name references in a file without per-symbol `find_references` calls.

### Step 2.1: Add Rust DTOs for semantic tokens

**File: `rust/src/dto/tokens.rs`** (new)

```rust
use crate::dto::RangeDto;
use pyo3::prelude::*;

/// Semantic token type classification.
#[pyclass(eq, name = "NativeSemanticTokenType", module = "tyo3._native_impl")]
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub enum SemanticTokenTypeDto {
    Namespace,
    Class,
    Parameter,
    SelfParameter,
    ClsParameter,
    Variable,
    Property,
    Function,
    Method,
    Keyword,
    String,
    Number,
    Decorator,
    BuiltinConstant,
    TypeParameter,
}

/// Semantic token modifier.
#[pyclass(eq, name = "NativeSemanticTokenModifier", module = "tyo3._native_impl")]
#[derive(Debug, Clone, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub enum SemanticTokenModifierDto {
    Definition,
    Readonly,
    Async,
    Documentation,
}

/// A single classified semantic token.
#[pyclass(name = "NativeSemanticToken", frozen, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SemanticTokenDto {
    #[pyo3(get)]
    pub range: RangeDto,
    #[pyo3(get)]
    pub token_type: SemanticTokenTypeDto,
    #[pyo3(get)]
    pub modifiers: Vec<SemanticTokenModifierDto>,
}
```

**File: `rust/src/dto/mod.rs`** — add:

```rust
mod tokens;
pub use tokens::*;
```

**File: `rust/src/lib.rs`** — register in `native_impl()`:

```rust
m.add_class::<dto::SemanticTokenTypeDto>()?;
m.add_class::<dto::SemanticTokenModifierDto>()?;
m.add_class::<dto::SemanticTokenDto>()?;
```

---

### Step 2.2: Add Rust converter for semantic tokens

**File: `rust/src/convert/tokens.rs`** (new)

You need to inspect `ty_ide::semantic_tokens` to understand the return type. At ty 0.0.40, `ty_ide::semantic_tokens` returns a `Vec` of token entries, each with a `TextRange`, a token type enum, and modifier flags.

```rust
use ruff_source_file::LineIndex;
use crate::coordinates;
use crate::dto::{SemanticTokenDto, SemanticTokenModifierDto, SemanticTokenTypeDto};

/// Map ty_ide::TokenType to our DTO enum.
///
/// IMPORTANT: inspect the actual `ty_ide::TokenType` enum at the pinned
/// Ruff commit to verify variant names. The names below are based on
/// the TyO3 concept doc's mapping and may need minor adjustment.
fn token_type_to_dto(tt: &ty_ide::TokenType) -> SemanticTokenTypeDto {
    match tt {
        ty_ide::TokenType::Namespace => SemanticTokenTypeDto::Namespace,
        ty_ide::TokenType::Class => SemanticTokenTypeDto::Class,
        ty_ide::TokenType::Parameter => SemanticTokenTypeDto::Parameter,
        ty_ide::TokenType::SelfParameter => SemanticTokenTypeDto::SelfParameter,
        ty_ide::TokenType::ClsParameter => SemanticTokenTypeDto::ClsParameter,
        ty_ide::TokenType::Variable => SemanticTokenTypeDto::Variable,
        ty_ide::TokenType::Property => SemanticTokenTypeDto::Property,
        ty_ide::TokenType::Function => SemanticTokenTypeDto::Function,
        ty_ide::TokenType::Method => SemanticTokenTypeDto::Method,
        ty_ide::TokenType::Keyword => SemanticTokenTypeDto::Keyword,
        ty_ide::TokenType::String => SemanticTokenTypeDto::String,
        ty_ide::TokenType::Number => SemanticTokenTypeDto::Number,
        ty_ide::TokenType::Decorator => SemanticTokenTypeDto::Decorator,
        ty_ide::TokenType::BuiltinConstant => SemanticTokenTypeDto::BuiltinConstant,
        ty_ide::TokenType::TypeParameter => SemanticTokenTypeDto::TypeParameter,
    }
}

/// Map ty_ide::TokenModifier flags to our DTO enum variants.
///
/// IMPORTANT: ty_ide::TokenModifier is likely a bitflags type.
/// Inspect the actual type to understand how to iterate modifiers.
fn modifiers_to_dto(modifiers: ty_ide::TokenModifiers) -> Vec<SemanticTokenModifierDto> {
    let mut result = Vec::new();
    if modifiers.contains(ty_ide::TokenModifier::Definition) {
        result.push(SemanticTokenModifierDto::Definition);
    }
    if modifiers.contains(ty_ide::TokenModifier::Readonly) {
        result.push(SemanticTokenModifierDto::Readonly);
    }
    if modifiers.contains(ty_ide::TokenModifier::Async) {
        result.push(SemanticTokenModifierDto::Async);
    }
    if modifiers.contains(ty_ide::TokenModifier::Documentation) {
        result.push(SemanticTokenModifierDto::Documentation);
    }
    result
}

/// Convert a list of ty_ide semantic tokens to DTOs.
pub fn convert_semantic_tokens(
    source: &str,
    line_index: &LineIndex,
    tokens: &[ty_ide::SemanticToken],
) -> Vec<SemanticTokenDto> {
    tokens
        .iter()
        .map(|t| SemanticTokenDto {
            range: coordinates::range_to_dto_with_index(source, line_index, t.range()),
            token_type: token_type_to_dto(&t.token_type()),
            modifiers: modifiers_to_dto(t.modifiers()),
        })
        .collect()
}
```

**IMPORTANT:** The exact `ty_ide` types (`TokenType`, `TokenModifier`, `SemanticToken`, `TokenModifiers`) need to be verified against the pinned Ruff commit. The code above uses the naming from the TyO3 concept doc. You will likely need to:
1. Search the `ty_ide` crate source at the pinned commit for `semantic_tokens`
2. Identify the exact enum variant names, modifier flag type, and token struct fields
3. Adjust the mapper code accordingly

**File: `rust/src/convert/mod.rs`** — add:

```rust
pub mod tokens;
```

---

### Step 2.3: Add `semantic_tokens` method to `PyTyProject`

**File: `rust/src/project.rs`** — add inside `#[pymethods] impl PyTyProject`:

```rust
/// Return semantic tokens for a file.
fn semantic_tokens(&self, path: &str) -> PyResult<Vec<dto::SemanticTokenDto>> {
    let guard = lock_state(&self.inner, "semantic_tokens")?;
    let state = guard.as_ref().unwrap();

    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);

    let tokens = ty_ide::semantic_tokens(&state.db, file);

    Ok(convert::tokens::convert_semantic_tokens(
        &source_str,
        &line_index,
        &tokens,
    ))
}
```

**Note:** Verify the signature of `ty_ide::semantic_tokens`. It may return a `Vec<SemanticToken>` or similar. Adjust the call and the converter's input type accordingly.

---

### Step 2.4: Add Python bridge and session methods

**File: `src/tyo3/rust_project.py`** — add method to `RustProject`:

```python
def semantic_tokens(self, path: str | StdPath) -> list[SemanticToken]:
    """Return semantic tokens for a file."""
    self._check_open()
    try:
        native_result = self._inner.semantic_tokens(str(path))
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except _NativePathError as e:
        raise PathResolutionError(str(e)) from e
    except Exception as e:
        raise InternalTyError(f"Unexpected error in semantic_tokens(): {e}") from e

    return [SemanticToken.model_validate(_to_python(t)) for t in native_result]
```

Add the import at the top of `rust_project.py`:

```python
from tyo3.models.advanced import SemanticToken
```

**File: `src/tyo3/session.py`** — add method to `TyO3Session`:

```python
def semantic_tokens(self, path: str | StdPath) -> list[SemanticToken]:
    """Return semantic tokens for a file."""
    return self._rp.semantic_tokens(path)
```

Add the import:

```python
from tyo3.models.advanced import SemanticToken
```

---

### Step 2.5: Update `_to_python()` enum cache

**File: `src/tyo3/rust_project.py`**

The `_build_enum_cache()` function detects enums by checking for names ending in `"Kind"` or equal to `"NativeSeverity"`. The new `NativeSemanticTokenType` and `NativeSemanticTokenModifier` enums end in `"Type"` and `"Modifier"`, so they won't be detected automatically.

Update `_build_enum_cache()` to also include names ending in `"Type"` and `"Modifier"` — or better, update it to detect any PyO3 enum class. The simplest fix is to add the new type names to the detection logic:

```python
def _build_enum_cache() -> set[type]:
    """Find all PyO3 enum types in the native module."""
    if _native is None:
        return set()
    result = set()
    for name in dir(_native):
        obj = getattr(_native, name, None)
        if obj is None or not isinstance(obj, type):
            continue
        # Detect PyO3 enums by name convention or by checking for enum-like behavior
        if (
            name.endswith("Kind")
            or name.endswith("Type")
            or name.endswith("Modifier")
            or name == "NativeSeverity"
        ):
            result.add(obj)
    return result
```

**Alternative (more robust):** Check if the type's instances have a `__str__` that returns a variant name and no `__dict__`. This is more fragile though. The name convention approach is sufficient.

---

### Step 2.6: Write semantic tokens tests

**File: `src/tyo3/tests/test_semantic_tokens.py`** (new)

```python
"""Tests for semantic_tokens API."""

from __future__ import annotations

from pathlib import Path as StdPath

import pytest

try:
    from tyo3 import _HAS_NATIVE
except ImportError:
    _HAS_NATIVE = False

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    return str((FIXTURES_DIR / name).resolve())


needs_native = pytest.mark.skipif(
    not _HAS_NATIVE, reason="Rust native extension not built"
)


@needs_native
class TestSemanticTokens:
    def test_returns_list(self) -> None:
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("simple_package"))
        try:
            files = rp.files()
            assert len(files) > 0
            tokens = rp.semantic_tokens(str(files[0]))
            assert isinstance(tokens, list)
        finally:
            rp.close()

    def test_tokens_have_range_and_type(self) -> None:
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("classes"))
        try:
            files = rp.files()
            tokens = rp.semantic_tokens(str(files[0]))
            if tokens:
                t = tokens[0]
                assert t.range is not None
                assert t.token_type is not None
                assert isinstance(t.modifiers, (set, list, frozenset))
        finally:
            rp.close()

    def test_classes_fixture_has_class_tokens(self) -> None:
        from tyo3.models.advanced import SemanticTokenType
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("classes"))
        try:
            files = rp.files()
            tokens = rp.semantic_tokens(str(files[0]))
            token_types = {t.token_type for t in tokens}
            # The classes fixture should have at least class or function tokens
            assert len(token_types) > 1
        finally:
            rp.close()

    def test_bad_path_raises(self) -> None:
        from tyo3.exceptions import PathResolutionError
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("simple_package"))
        try:
            with pytest.raises(PathResolutionError):
                rp.semantic_tokens("nonexistent.py")
        finally:
            rp.close()

    def test_after_close_raises(self) -> None:
        from tyo3.exceptions import ProjectClosedError
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("simple_package"))
        rp.close()
        with pytest.raises(ProjectClosedError):
            rp.semantic_tokens("anything.py")
```

**Verification:** Rebuild Rust (`build`), run `test-rust`. All tests pass.

---

### Step 2.7: Update graph construction to use semantic tokens

**File: `src/tyo3/graph/graph.py`** — replace the reference resolution in `_index_file`:

Replace the loop `for symbol in symbols: self._resolve_references_for_symbol(...)` with a new method that uses semantic tokens + `goto_definition`:

```python
def _resolve_references_via_tokens(
    self, session: TyO3Session, file_str: str
) -> None:
    """Resolve references using semantic tokens + goto_definition.

    For each name-like token in the file, call goto_definition to
    find what symbol it refers to. Create a REFERENCES edge from
    the enclosing symbol to the target symbol.
    """
    try:
        tokens = session.semantic_tokens(file_str)
    except Exception:
        logger.warning("Failed to get semantic tokens for %s", file_str)
        return

    # Filter to name-like tokens (not keywords, strings, numbers)
    from tyo3.models.advanced import SemanticTokenType

    NAME_TYPES = {
        SemanticTokenType.NAMESPACE,
        SemanticTokenType.CLASS_,
        SemanticTokenType.PARAMETER,
        SemanticTokenType.SELF_PARAMETER,
        SemanticTokenType.CLS_PARAMETER,
        SemanticTokenType.VARIABLE,
        SemanticTokenType.PROPERTY,
        SemanticTokenType.FUNCTION,
        SemanticTokenType.METHOD,
        SemanticTokenType.DECORATOR,
        SemanticTokenType.BUILTIN_CONSTANT,
        SemanticTokenType.TYPE_PARAMETER,
    }

    for token in tokens:
        if token.token_type not in NAME_TYPES:
            continue

        start = token.range.start
        try:
            targets = session.goto_definition(file_str, start.line, start.column)
        except Exception:
            continue

        if not targets:
            continue

        target = targets[0]
        target_file = str(target.path)

        # Build the target's symbol_id
        # Try to use the target's symbol info if available
        if target.symbol and target.symbol.qualified_name:
            target_sid = f"{target_file}::{target.symbol.qualified_name}"
        elif target.symbol:
            target_sid = f"{target_file}::{target.symbol.name}@{target.range.start.line}"
        else:
            # No symbol info — skip this reference
            continue

        # Ensure the target node exists (may be in another file or external)
        if target_sid not in self._id_to_index:
            # Could be an external symbol or a file not yet indexed
            continue

        # Determine the enclosing symbol at this token's location
        enclosing_id = self._find_enclosing_symbol(file_str, token.range)
        if enclosing_id is None:
            continue

        # Don't create self-references for definition sites
        if enclosing_id == target_sid:
            continue

        # Determine role from token modifiers
        from tyo3.models.advanced import SemanticTokenModifier
        role = ReferenceRole.READ
        if SemanticTokenModifier.DEFINITION in token.modifiers:
            role = ReferenceRole.DEFINITION

        edge = EdgeData(
            kind=EdgeKind.REFERENCES,
            file=file_str,
            range=token.range,
            role=role,
        )
        self._add_edge(enclosing_id, target_sid, edge, file_str)
```

Then update `_index_file` to call `_resolve_references_via_tokens` instead of the old per-symbol loop:

```python
# In _index_file, replace:
#   for symbol in symbols:
#       self._resolve_references_for_symbol(session, file_str, symbol)
# With:
self._resolve_references_via_tokens(session, file_str)
```

Keep the old `_resolve_references_for_symbol` method as a fallback — it can be used when semantic tokens are not available (e.g., if the Rust extension was built without semantic token support).

---

## Phase 3: Wire Type Hierarchy (Rust + Python)

### Step 3.1: Add Rust DTOs for type hierarchy

**File: `rust/src/dto/hierarchy.rs`** (new)

```rust
use crate::dto::{FileRangeDto, RangeDto};
use pyo3::prelude::*;

/// A single item in a type hierarchy.
#[pyclass(name = "NativeTypeHierarchyItem", frozen, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct TypeHierarchyItemDto {
    #[pyo3(get)]
    pub name: String,
    #[pyo3(get)]
    pub detail: Option<String>,
    #[pyo3(get)]
    pub path: String,
    #[pyo3(get)]
    pub full_range: RangeDto,
    #[pyo3(get)]
    pub selection_range: RangeDto,
}

/// Result of a type hierarchy query.
#[pyclass(name = "NativeTypeHierarchy", frozen, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct TypeHierarchyDto {
    #[pyo3(get)]
    pub item: TypeHierarchyItemDto,
    #[pyo3(get)]
    pub supertypes: Vec<TypeHierarchyItemDto>,
    #[pyo3(get)]
    pub subtypes: Vec<TypeHierarchyItemDto>,
}
```

**File: `rust/src/dto/mod.rs`** — add:

```rust
mod hierarchy;
pub use hierarchy::*;
```

**File: `rust/src/lib.rs`** — register:

```rust
m.add_class::<dto::TypeHierarchyItemDto>()?;
m.add_class::<dto::TypeHierarchyDto>()?;
```

---

### Step 3.2: Add Rust converter for type hierarchy

**File: `rust/src/convert/hierarchy.rs`** (new)

```rust
use ruff_db::Db;
use ruff_db::source::source_text;
use ruff_source_file::LineIndex;
use ruff_db::files::File;
use crate::coordinates;
use crate::dto::{TypeHierarchyItemDto, TypeHierarchyDto};

/// Convert a ty_ide type hierarchy item to a DTO.
///
/// IMPORTANT: Inspect ty_ide::TypeHierarchyItem at the pinned commit
/// to verify field names and types. Adjust as needed.
pub fn convert_hierarchy_item(
    db: &dyn Db,
    item: &ty_ide::TypeHierarchyItem,
) -> TypeHierarchyItemDto {
    let file = item.file();
    let source = source_text(db, file);
    let src = source.as_str();
    let line_index = LineIndex::from_source_text(src);
    let path = file.path(db).as_str().to_string();

    TypeHierarchyItemDto {
        name: item.name().to_string(),
        detail: item.detail().map(|s| s.to_string()),
        path,
        full_range: coordinates::range_to_dto_with_index(src, &line_index, item.full_range()),
        selection_range: coordinates::range_to_dto_with_index(src, &line_index, item.selection_range()),
    }
}

pub fn convert_hierarchy_items(
    db: &dyn Db,
    items: &[ty_ide::TypeHierarchyItem],
) -> Vec<TypeHierarchyItemDto> {
    items.iter().map(|i| convert_hierarchy_item(db, i)).collect()
}
```

**File: `rust/src/convert/mod.rs`** — add:

```rust
pub mod hierarchy;
```

---

### Step 3.3: Add `type_hierarchy` method to `PyTyProject`

**File: `rust/src/project.rs`** — add inside `#[pymethods] impl PyTyProject`:

```rust
/// Query type hierarchy at a position: returns the item with supertypes and subtypes.
fn type_hierarchy(
    &self,
    path: &str,
    line: u32,
    column: u32,
) -> PyResult<Option<dto::TypeHierarchyDto>> {
    let guard = lock_state(&self.inner, "type_hierarchy")?;
    let state = guard.as_ref().unwrap();

    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(|e| PositionError::new_err(e))?;

    // Step 1: Prepare the hierarchy item at this position
    let prepared = ty_ide::prepare_type_hierarchy(&state.db, file, offset);
    let item = match prepared {
        Some(item) => item,
        None => return Ok(None),
    };

    // Step 2: Resolve supertypes and subtypes
    let supertypes = ty_ide::type_hierarchy_supertypes(&state.db, &item);
    let subtypes = ty_ide::type_hierarchy_subtypes(&state.db, &item);

    let item_dto = convert::hierarchy::convert_hierarchy_item(&state.db, &item);
    let supertypes_dto = convert::hierarchy::convert_hierarchy_items(&state.db, &supertypes);
    let subtypes_dto = convert::hierarchy::convert_hierarchy_items(&state.db, &subtypes);

    Ok(Some(dto::TypeHierarchyDto {
        item: item_dto,
        supertypes: supertypes_dto,
        subtypes: subtypes_dto,
    }))
}
```

**IMPORTANT:** Verify the signatures of `ty_ide::prepare_type_hierarchy`, `ty_ide::type_hierarchy_supertypes`, and `ty_ide::type_hierarchy_subtypes` at the pinned commit. They may take `&dyn Db`, `File`, `TextSize` and return `Option<TypeHierarchyItem>` / `Vec<TypeHierarchyItem>`, or the signatures may differ.

---

### Step 3.4: Add Python models for type hierarchy

**File: `src/tyo3/models/navigation.py`** — add:

```python
class TypeHierarchyItem(BaseModel):
    """An item in a type hierarchy (class with its supertypes/subtypes)."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    detail: str | None = None
    path: PurePosixPath
    full_range: Range
    selection_range: Range


class TypeHierarchy(BaseModel):
    """Result of a type hierarchy query."""

    model_config = ConfigDict(from_attributes=True)

    item: TypeHierarchyItem
    supertypes: list[TypeHierarchyItem] = []
    subtypes: list[TypeHierarchyItem] = []
```

Update `__all__` in `navigation.py` and `models/__init__.py` to include the new types.

---

### Step 3.5: Add Python bridge and session methods

**File: `src/tyo3/rust_project.py`** — add method:

```python
def type_hierarchy(
    self, path: str | StdPath, line: int, column: int
) -> TypeHierarchy | None:
    """Query type hierarchy at a position."""
    self._check_open()
    try:
        native_result = self._inner.type_hierarchy(str(path), line, column)
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except _NativePathError as e:
        raise PathResolutionError(str(e)) from e
    except _NativePositionError as e:
        raise PositionError(str(e)) from e
    except OverflowError as e:
        raise PositionError(str(e)) from e
    except Exception as e:
        raise InternalTyError(f"Unexpected error in type_hierarchy(): {e}") from e

    if native_result is None:
        return None
    return TypeHierarchy.model_validate(_to_python(native_result))
```

Add the import: `from tyo3.models.navigation import TypeHierarchy`

**File: `src/tyo3/session.py`** — add method:

```python
def type_hierarchy(
    self, path: str | StdPath, line: int, column: int
) -> TypeHierarchy | None:
    """Query type hierarchy at a position."""
    self._validate_position(line, column)
    return self._rp.type_hierarchy(path, line, column)
```

Add the import: `from tyo3.models.navigation import TypeHierarchy`

---

### Step 3.6: Add INHERITS/OVERRIDES edges to graph construction

**File: `src/tyo3/graph/graph.py`** — add to `_index_file`, after symbol nodes are added:

```python
# 4. Resolve inheritance for CLASS nodes
self._resolve_inheritance(session, file_str, symbols)
```

Add the new method:

```python
def _resolve_inheritance(
    self,
    session: TyO3Session,
    file_str: str,
    symbols: list[Symbol],
) -> None:
    """Add INHERITS and OVERRIDES edges for class symbols."""
    for symbol in symbols:
        if symbol.kind not in (SymbolKind.CLASS, SymbolKind.CLASS_):
            continue

        start = symbol.location.range.start
        try:
            hierarchy = session.type_hierarchy(file_str, start.line, start.column)
        except Exception:
            continue

        if hierarchy is None:
            continue

        sid = symbol_id_from_symbol(file_str, symbol)

        # Add INHERITS edges to supertypes
        for supertype in hierarchy.supertypes:
            super_file = str(supertype.path)
            super_sid = f"{super_file}::{supertype.name}"

            # Ensure supertype node exists (may need stub)
            if super_sid not in self._id_to_index:
                # Check if it might be in a different qualified form
                # For now, skip if not found
                continue

            edge = EdgeData(kind=EdgeKind.INHERITS)
            self._add_edge(sid, super_sid, edge, file_str)

        # Derive OVERRIDES: if child class has a method with the same
        # name as a parent class method, add an OVERRIDES edge.
        child_methods = [
            s for s in symbols
            if s.kind in (SymbolKind.METHOD,)
            and s.container_name == symbol.name
        ]
        for supertype in hierarchy.supertypes:
            super_file = str(supertype.path)
            # Get parent's methods from the graph
            super_sid = f"{super_file}::{supertype.name}"
            parent_children = self.children(super_sid)
            parent_method_names = {
                c.name: c.symbol_id for c in parent_children
                if c.kind == SymbolKind.METHOD
            }
            for method in child_methods:
                if method.name in parent_method_names:
                    method_sid = symbol_id_from_symbol(file_str, method)
                    parent_method_sid = parent_method_names[method.name]
                    edge = EdgeData(kind=EdgeKind.OVERRIDES)
                    self._add_edge(method_sid, parent_method_sid, edge, file_str)
```

---

### Step 3.7: Write type hierarchy tests

**File: `src/tyo3/tests/test_type_hierarchy.py`** (new)

```python
"""Tests for type_hierarchy API."""

from __future__ import annotations

from pathlib import Path as StdPath

import pytest

try:
    from tyo3 import _HAS_NATIVE
except ImportError:
    _HAS_NATIVE = False

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    return str((FIXTURES_DIR / name).resolve())


needs_native = pytest.mark.skipif(
    not _HAS_NATIVE, reason="Rust native extension not built"
)


@needs_native
class TestTypeHierarchy:
    def test_returns_none_for_non_class(self) -> None:
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("simple_package"))
        try:
            files = rp.files()
            # Try a position that's not a class — should return None
            result = rp.type_hierarchy(str(files[0]), 1, 1)
            # Result may be None or a hierarchy, depending on what's at 1:1
            assert result is None or result.item is not None
        finally:
            rp.close()

    def test_class_has_hierarchy(self) -> None:
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("classes"))
        try:
            files = rp.files()
            symbols = rp.document_symbols(str(files[0]))
            # Find a class symbol
            classes = [s for s in symbols if s.kind.value in ("class_",)]
            if classes:
                cls = classes[0]
                start = cls.location.range.start
                result = rp.type_hierarchy(str(files[0]), start.line, start.column)
                if result is not None:
                    assert result.item.name == cls.name
        finally:
            rp.close()

    def test_bad_path_raises(self) -> None:
        from tyo3.exceptions import PathResolutionError
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("classes"))
        try:
            with pytest.raises(PathResolutionError):
                rp.type_hierarchy("nonexistent.py", 1, 1)
        finally:
            rp.close()

    def test_after_close_raises(self) -> None:
        from tyo3.exceptions import ProjectClosedError
        from tyo3.rust_project import RustProject

        rp = RustProject(fixture_path("classes"))
        rp.close()
        with pytest.raises(ProjectClosedError):
            rp.type_hierarchy("anything.py", 1, 1)
```

**Verification:** Rebuild Rust, run all tests.

---

## Phase 4: External Symbols and Caching

### Step 4.1: Create stub nodes during graph construction

**File: `src/tyo3/graph/graph.py`** — update `_resolve_references_via_tokens`:

When a `goto_definition` target points to a file outside the project root, create a stub node:

```python
# Inside _resolve_references_via_tokens, after goto_definition returns targets:

# Check if target is external (not in project files)
if target_file not in self._file_to_nodes and target_sid not in self._id_to_index:
    # Determine package from the file path
    package = self._infer_package(target_file)
    kind = SymbolKind.UNKNOWN
    if target.symbol:
        kind = target.symbol.kind
    name = target.symbol.name if target.symbol else target_sid.split("::")[-1]
    qn = target.symbol.qualified_name if target.symbol and target.symbol.qualified_name else name

    # Create an external symbol_id
    ext_sid = f"{package}::{qn}" if package else target_sid
    self._add_stub_node(
        symbol_id=ext_sid,
        name=name,
        qualified_name=qn,
        kind=kind,
        package=package or "unknown",
    )
    target_sid = ext_sid
```

Add the package inference helper:

```python
def _infer_package(self, file_path: str) -> str | None:
    """Infer the package name from an external file path.

    Heuristic: look for common patterns like site-packages/foo/...,
    or lib/python3.x/... for stdlib.
    """
    if "site-packages/" in file_path:
        parts = file_path.split("site-packages/")[1].split("/")
        return parts[0] if parts else None
    if "/lib/python" in file_path or "typeshed" in file_path:
        return "stdlib"
    return None
```

---

### Step 4.2: Create `DependencyGraph` class

**File: `src/tyo3/graph/dependency.py`** (new)

```python
"""Cached dependency graphs for external packages."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import rustworkx as rx

from tyo3.graph.models import SymbolNode

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".cache" / "tyo3" / "deps"


class DependencyGraph:
    """Cached code graph for an external package."""

    def __init__(
        self,
        package: str,
        version: str,
        graph: rx.PyDiGraph,
        id_to_index: dict[str, int],
    ) -> None:
        self.package = package
        self.version = version
        self.graph = graph
        self._id_to_index = id_to_index

    def lookup(self, symbol_id: str) -> SymbolNode | None:
        """Find a symbol in this dependency graph."""
        idx = self._id_to_index.get(symbol_id)
        if idx is None:
            return None
        return self.graph[idx]

    def save(self) -> None:
        """Cache this dependency graph to disk."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = CACHE_DIR / f"{self.package}-{self.version}.json"

        nodes = []
        for idx in self.graph.node_indices():
            node: SymbolNode = self.graph[idx]
            nodes.append({"idx": idx, "data": node.model_dump(mode="json")})

        edges = []
        for edge_idx in self.graph.edge_indices():
            src, tgt = self.graph.get_edge_endpoints_by_index(edge_idx)
            edges.append({"src": src, "tgt": tgt})

        data = {
            "package": self.package,
            "version": self.version,
            "nodes": nodes,
            "edges": edges,
        }
        cache_path.write_text(json.dumps(data))
        logger.info("Cached dependency graph: %s", cache_path)

    @classmethod
    def load(cls, package: str, version: str) -> DependencyGraph | None:
        """Load a cached dependency graph from disk."""
        cache_path = CACHE_DIR / f"{package}-{version}.json"
        if not cache_path.exists():
            return None

        try:
            data = json.loads(cache_path.read_text())
            graph = rx.PyDiGraph()
            id_to_index: dict[str, int] = {}

            for node_data in data["nodes"]:
                node = SymbolNode.model_validate(node_data["data"])
                idx = graph.add_node(node)
                id_to_index[node.symbol_id] = idx

            for edge_data in data["edges"]:
                graph.add_edge(edge_data["src"], edge_data["tgt"], None)

            return cls(package, version, graph, id_to_index)
        except Exception:
            logger.warning("Failed to load cached graph for %s-%s", package, version)
            return None
```

---

### Step 4.3: Add dependency graph resolution to `CodeGraph`

**File: `src/tyo3/graph/graph.py`** — add to `CodeGraph.__init__`:

```python
self._dependency_cache: dict[str, DependencyGraph] = {}
```

Add method:

```python
def resolve_external(self, symbol_id: str) -> SymbolNode | None:
    """Resolve an external symbol by loading its dependency graph."""
    node = self.symbol(symbol_id)
    if node is None or not node.external or not node.package:
        return node

    if node.package not in self._dependency_cache:
        from tyo3.graph.dependency import DependencyGraph
        # Try loading from disk cache
        dep = DependencyGraph.load(node.package, "unknown")
        if dep is not None:
            self._dependency_cache[node.package] = dep

    dep = self._dependency_cache.get(node.package)
    if dep is None:
        return node  # Return the stub

    resolved = dep.lookup(symbol_id)
    return resolved if resolved is not None else node
```

---

## Phase 5: Batch Occurrence API (Performance Optimization)

This phase is deferred until profiling shows that Phase 2's per-token `goto_definition` approach is a bottleneck. When needed:

### Step 5.1: Profile Phase 2

Run `CodeGraph.build()` on a medium-sized real project (10k-50k lines). Measure:
- Total build time
- Time per file
- Number of `goto_definition` calls per file
- Time in Rust vs. time in Python

If build time exceeds acceptable thresholds (e.g., >30s for a 50k-line project), proceed to Step 5.2.

### Step 5.2: Design `file_occurrences` Rust API

This requires exploring `ty_python_semantic` at the pinned commit to understand how to walk a file's AST and resolve every name binding. Key questions:

- What types represent name bindings in the semantic model?
- How to iterate all references in a file?
- How to resolve each reference to its definition (file + qualified name)?
- How to determine read/write/import role?

This is a significant Rust engineering effort. Document findings before implementing.

### Step 5.3: Implement `file_occurrences` in Rust

Follow the standard pattern: DTO in `dto/occurrences.rs`, converter in `convert/occurrences.rs`, method on `PyTyProject`, bridge method on `RustProject`, session method on `TyO3Session`.

### Step 5.4: Update graph construction

Replace `_resolve_references_via_tokens` with a call to `session.file_occurrences(file_str)`. The graph construction logic remains the same — it receives a list of occurrences and builds edges.

---

## Phase 6: Advanced Queries and Incremental Updates

### Step 6.1: Add import cycle detection

**File: `src/tyo3/graph/graph.py`** — add method:

```python
def import_cycles(self) -> list[list[str]]:
    """Detect circular import chains in the IMPORTS subgraph."""
    # Build a subgraph of only MODULE nodes connected by IMPORTS edges
    module_indices = [
        i for i in self._graph.node_indices()
        if self._graph[i].kind == SymbolKind.MODULE
    ]
    if not module_indices:
        return []

    # Use rx.simple_cycles or manual DFS on the filtered subgraph
    # This is application-specific logic built on top of RustworkX
    ...
```

### Step 6.2: Add incremental `update_file`

**File: `src/tyo3/graph/graph.py`** — add method:

```python
def update_file(self, session: TyO3Session, path: str) -> None:
    """Re-index a single file and update the graph."""
    # 1. Remove all nodes defined in this file
    old_indices = list(self._file_to_nodes.get(path, []))
    old_ids = [self._graph[i].symbol_id for i in old_indices]
    for idx in old_indices:
        self._graph.remove_node(idx)
    for sid in old_ids:
        self._id_to_index.pop(sid, None)
    self._file_to_nodes.pop(path, None)

    # 2. Remove all edges originating from this file
    old_edge_indices = list(self._file_to_edges.get(path, []))
    for edge_idx in reversed(sorted(old_edge_indices)):
        try:
            self._graph.remove_edge_from_index(edge_idx)
        except Exception:
            pass
    self._file_to_edges.pop(path, None)

    # 3. Re-index the file
    self._index_file(session, path)
```

### Step 6.3: Add visualization export

**File: `src/tyo3/graph/export.py`** (new)

```python
"""Export CodeGraph to various formats."""

from __future__ import annotations

from tyo3.graph.graph import CodeGraph
from tyo3.graph.models import EdgeData, SymbolNode


def to_dot(graph: CodeGraph) -> str:
    """Export the graph in DOT format for Graphviz visualization."""
    lines = ["digraph CodeGraph {"]
    lines.append("  rankdir=LR;")

    for idx in graph.graph.node_indices():
        node: SymbolNode = graph.graph[idx]
        label = f"{node.name}\\n({node.kind.value})"
        color = _kind_color(node.kind.value)
        lines.append(f'  n{idx} [label="{label}", color="{color}"];')

    for edge_idx in graph.graph.edge_indices():
        src, tgt = graph.graph.get_edge_endpoints_by_index(edge_idx)
        data: EdgeData = graph.graph.get_edge_data_by_index(edge_idx)
        lines.append(f'  n{src} -> n{tgt} [label="{data.kind.value}"];')

    lines.append("}")
    return "\n".join(lines)


def _kind_color(kind: str) -> str:
    colors = {
        "module": "blue",
        "class_": "red",
        "function": "green",
        "method": "darkgreen",
        "variable": "gray",
        "constant": "orange",
    }
    return colors.get(kind, "black")
```

---

## Testing Checklist

After each phase, verify:

| Check | Command |
|---|---|
| Rust builds | `build` |
| Rust integration tests | `test-rust` |
| Python unit tests | `test-quick` |
| Smoke test | `check-so` |
| Graph tests | `pytest src/tyo3/tests/test_graph.py -v` |
| Semantic token tests | `pytest src/tyo3/tests/test_semantic_tokens.py -v` |
| Type hierarchy tests | `pytest src/tyo3/tests/test_type_hierarchy.py -v` |

After Phase 3, also run:
- `test-perf` — verify no performance regression
- `test-property` — property-based tests still pass

---

## File Summary

### New files created

| File | Phase | Purpose |
|---|---|---|
| `src/tyo3/graph/__init__.py` | 1 | Graph package init |
| `src/tyo3/graph/models.py` | 1 | SymbolNode, EdgeData, EdgeKind, ReferenceRole |
| `src/tyo3/graph/identity.py` | 1 | Symbol ID construction helpers |
| `src/tyo3/graph/graph.py` | 1 | CodeGraph class |
| `src/tyo3/graph/dependency.py` | 4 | DependencyGraph class |
| `src/tyo3/graph/export.py` | 6 | DOT/JSON export |
| `fixtures/graph_test/models.py` | 1 | Test fixture |
| `fixtures/graph_test/app.py` | 1 | Test fixture |
| `src/tyo3/tests/test_graph.py` | 1 | Graph tests |
| `src/tyo3/tests/test_semantic_tokens.py` | 2 | Semantic token tests |
| `src/tyo3/tests/test_type_hierarchy.py` | 3 | Type hierarchy tests |
| `rust/src/dto/tokens.rs` | 2 | Semantic token DTOs |
| `rust/src/dto/hierarchy.rs` | 3 | Type hierarchy DTOs |
| `rust/src/convert/tokens.rs` | 2 | Semantic token converter |
| `rust/src/convert/hierarchy.rs` | 3 | Type hierarchy converter |

### Existing files modified

| File | Phase | Change |
|---|---|---|
| `pyproject.toml` | 1 | Add `rustworkx` dependency |
| `src/tyo3/__init__.py` | 1 | Export `CodeGraph` |
| `rust/src/dto/mod.rs` | 2, 3 | Add `tokens` and `hierarchy` modules |
| `rust/src/convert/mod.rs` | 2, 3 | Add `tokens` and `hierarchy` modules |
| `rust/src/lib.rs` | 2, 3 | Register new DTO classes |
| `rust/src/project.rs` | 2, 3 | Add `semantic_tokens` and `type_hierarchy` methods |
| `src/tyo3/rust_project.py` | 2, 3 | Add bridge methods, update enum cache |
| `src/tyo3/session.py` | 2, 3 | Add session methods |
| `src/tyo3/models/navigation.py` | 3 | Add TypeHierarchyItem, TypeHierarchy |
| `src/tyo3/models/__init__.py` | 3 | Export new types |
