"""CodeGraph — the semantic code intelligence graph."""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any

import rustworkx as rx

from tyo3.graph.dependency import DependencyGraph
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

        # Dependency graph cache for external packages
        self._dependency_cache: dict[str, DependencyGraph] = {}

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

        # 2. Build reference edges using semantic tokens + goto_definition
        #    (Phase 2: replaces the slow per-symbol find_references loop)
        self._resolve_references_via_tokens(session, file_str)

        # 3. Get diagnostics
        try:
            result = session.check_file(file_str)
            self._diagnostics[file_str] = result.diagnostics
        except Exception:
            logger.warning("Failed to check %s, skipping diagnostics", file_str)

        # 4. Resolve inheritance for CLASS nodes
        self._resolve_inheritance(session, file_str, symbols)

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
            # Use flexible lookup to handle both "name" and "name@line" formats
            candidate_id = self._find_symbol_in_file(file_str, symbol.container_name)
            if candidate_id is not None:
                return candidate_id
        return module_id

    def _find_symbol_in_file(self, file_path: str, name: str) -> str | None:
        """Find a symbol ID for *name* in *file_path*.

        Tries exact match first, then matches by extracting the
        name portion from symbol IDs that use ``name@line`` format.
        """
        exact = f"{file_path}::{name}"
        if exact in self._id_to_index:
            return exact
        # Try matching any symbol_id that starts with file::name@
        prefix = f"{file_path}::{name}@"
        for sid in self._id_to_index:
            if sid.startswith(prefix):
                return sid
        return None

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
        from tyo3.models.advanced import SemanticTokenType, SemanticTokenModifier

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
            if target.symbol and target.symbol.qualified_name:
                target_sid = f"{target_file}::{target.symbol.qualified_name}"
            elif target.symbol:
                target_sid = f"{target_file}::{target.symbol.name}@{target.range.start.line}"
            else:
                # No symbol info — skip this reference
                continue

            # Ensure the target node exists — create a stub if external
            if target_sid not in self._id_to_index:
                target_sid = self._ensure_target_node(
                    target_file, target_sid, target
                )
                if target_sid is None:
                    continue

            # Determine the enclosing symbol at this token's location
            enclosing_id = self._find_enclosing_symbol(file_str, token.range)
            if enclosing_id is None:
                continue

            # Don't create self-references for definition sites
            if enclosing_id == target_sid:
                continue

            # Determine role from token modifiers
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

    def _ensure_target_node(
        self,
        target_file: str,
        target_sid: str,
        target: Any,
    ) -> str | None:
        """Ensure a reference target exists as a node.

        If the target is external (not in project files), creates a stub
        node for it. Returns the effective symbol_id to use for edge
        creation, or None if the target cannot be represented.
        """
        # If it's already indexed in this session's files, it may be
        # from a file that just hasn't been processed yet — skip for now.
        # Only create stubs for truly external paths (not in any project file).
        if target_file not in self._file_to_nodes:
            package = self._infer_package(target_file)
            kind = SymbolKind.UNKNOWN
            name = target_sid.split("::")[-1]
            qn = name
            if hasattr(target, "symbol") and target.symbol:
                kind = target.symbol.kind
                name = target.symbol.name
                qn = target.symbol.qualified_name or name

            ext_sid = f"{package}::{qn}" if package else target_sid
            self._add_stub_node(
                symbol_id=ext_sid,
                name=name,
                qualified_name=qn,
                kind=kind,
                package=package or "unknown",
            )
            return ext_sid
        # In-project but not yet indexed — will be picked up later
        return None

    def _infer_package(self, file_path: str) -> str | None:
        """Infer the package name from an external file path.

        Heuristic: look for common patterns like site-packages/foo/...,
        or lib/python3.x/... for stdlib.
        """
        if "site-packages/" in file_path:
            parts = file_path.split("site-packages/")[1].split("/")
            if parts:
                return parts[0]
        if "/lib/python" in file_path or "typeshed" in file_path:
            return "stdlib"
        # Look for .venv or venv patterns
        if "/.venv/" in file_path or "/venv/" in file_path:
            parts = file_path.split("/.venv/" if "/.venv/" in file_path else "/venv/")[1].split("/")
            if len(parts) > 2 and parts[0] == "lib":
                # .venv/lib/python3.x/site-packages/foo/...
                for i, part in enumerate(parts):
                    if part == "site-packages" and i + 1 < len(parts):
                        return parts[i + 1]
        return None

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

            start = symbol.selection_range.start if symbol.selection_range else symbol.location.range.start
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
                super_sid = self._find_symbol_in_file(super_file, supertype.name)

                # If not found locally, create an external stub node
                if super_sid is None:
                    package = self._infer_package(super_file)
                    if package is None and super_file not in self._file_to_nodes:
                        # Truly external, use the file stem as package hint
                        package = "unknown"
                    if package:
                        ext_sid = f"{package}::{supertype.name}"
                        kind = SymbolKind.CLASS  # supertypes are classes
                        super_sid = ext_sid
                        if ext_sid not in self._id_to_index:
                            self._add_stub_node(
                                symbol_id=ext_sid,
                                name=supertype.name,
                                qualified_name=f"{package}.{supertype.name}",
                                kind=kind,
                                package=package,
                            )
                    else:
                        continue

                edge = EdgeData(kind=EdgeKind.INHERITS)
                self._add_edge(sid, super_sid, edge, file_str)

            # Derive OVERRIDES: collect methods from the child class,
            # then walk the full supertype chain to find overridden methods.
            METHOD_KINDS = {SymbolKind.METHOD, SymbolKind.CONSTRUCTOR}
            child_methods = [
                s for s in symbols
                if s.kind in METHOD_KINDS
                and s.container_name == symbol.name
            ]
            if not child_methods:
                continue

            # Collect ancestor methods by walking the INHERITS chain
            ancestor_methods: dict[str, str] = {}  # name -> symbol_id
            visited: set[str] = set()
            queue: list[str] = [sid]
            while queue:
                current_sid = queue.pop(0)
                for succ_idx in self._graph.neighbors(self._id_to_index[current_sid]):
                    try:
                        edge_data = self._graph.get_edge_data(self._id_to_index[current_sid], succ_idx)
                    except Exception:
                        continue
                    if edge_data is not None and edge_data.kind == EdgeKind.INHERITS:
                        parent_sid = self._graph[succ_idx].symbol_id
                        if parent_sid not in visited:
                            visited.add(parent_sid)
                            queue.append(parent_sid)
                        # Collect this parent's methods
                        for c in self.children(parent_sid):
                            if c.kind in METHOD_KINDS and c.name not in ancestor_methods:
                                ancestor_methods[c.name] = c.symbol_id

            for method in child_methods:
                if method.name in ancestor_methods:
                    method_sid = symbol_id_from_symbol(file_str, method)
                    parent_method_sid = ancestor_methods[method.name]
                    edge = EdgeData(kind=EdgeKind.OVERRIDES)
                    self._add_edge(method_sid, parent_method_sid, edge, file_str)

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
        for pred_idx in self._graph.predecessor_indices(idx):
            # Check all edges from pred to idx
            try:
                edge_data = self._graph.get_edge_data(pred_idx, idx)
            except Exception:
                continue
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
            try:
                edge_data = self._graph.get_edge_data(idx, succ_idx)
            except Exception:
                continue
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
            try:
                edge_data = self._graph.get_edge_data(idx, succ_idx)
            except Exception:
                continue
            if edge_data is not None and edge_data.kind in (EdgeKind.DEFINES, EdgeKind.CONTAINS):
                result.append(self._graph[succ_idx])
        return result

    def parent(self, symbol_id: str) -> SymbolNode | None:
        """Enclosing symbol (incoming DEFINES/CONTAINS edge)."""
        idx = self._id_to_index.get(symbol_id)
        if idx is None:
            return None
        for pred_idx in self._graph.predecessor_indices(idx):
            try:
                edge_data = self._graph.get_edge_data(pred_idx, idx)
            except Exception:
                continue
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
            for pred in self._graph.predecessor_indices(idx)
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

    # ── External symbol resolution ───────────────────────────

    def external_symbols(self) -> list[SymbolNode]:
        """All stub nodes for external (dependency) symbols."""
        return [
            self._graph[i]
            for i in self._graph.node_indices()
            if self._graph[i].external
        ]

    def external_symbols_by_package(self) -> dict[str, list[SymbolNode]]:
        """Group external symbol stub nodes by package."""
        result: dict[str, list[SymbolNode]] = {}
        for node in self.external_symbols():
            pkg = node.package or "unknown"
            if pkg not in result:
                result[pkg] = []
            result[pkg].append(node)
        return result

    def resolve_external(self, symbol_id: str) -> SymbolNode | None:
        """Resolve an external symbol by loading its dependency graph.

        If the symbol is external and its dependency graph is cached
        on disk, loads it and returns the fully-resolved node.
        Otherwise returns the stub node as-is.
        """
        node = self.symbol(symbol_id)
        if node is None or not node.external or not node.package:
            return node

        if node.package not in self._dependency_cache:
            # Try loading from disk cache
            dep = DependencyGraph.load(node.package, "unknown")
            if dep is not None:
                self._dependency_cache[node.package] = dep

        dep = self._dependency_cache.get(node.package)
        if dep is None:
            return node  # Return the stub

        resolved = dep.lookup(symbol_id)
        return resolved if resolved is not None else node

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
