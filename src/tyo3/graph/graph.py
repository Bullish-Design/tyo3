"""CodeGraph — the semantic code intelligence graph."""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath
from typing import Any

import rustworkx as rx

from tyo3.graph.dependency import DependencyGraph
from tyo3.graph.identity import file_from_symbol_id, symbol_id_from_symbol
from tyo3.graph.models import (
    EdgeData,
    EdgeKind,
    GraphBuildFailure,
    GraphBuildReport,
    SymbolNode,
)
from tyo3.models.advanced import SemanticTokenModifier, SemanticTokenType
from tyo3.models.analysis import Diagnostic, Range
from tyo3.models.navigation import ReferenceRole
from tyo3.models.symbols import Symbol, SymbolKind
from tyo3.session import TyO3Session

logger = logging.getLogger(__name__)


def _to_relative(root: Path, path: str) -> str:
    """Convert an absolute path to a project-relative POSIX string.

    If *path* is not under *root* (e.g. an external path), returns it
    unchanged so external references stay explicitly external.
    """
    try:
        return str(
            PurePosixPath(Path(path).resolve().relative_to(root.resolve()))
        )
    except ValueError:
        return path  # External path — return as-is


def _normalize_result_path(
    root: Path, path: str, project_files: set[str]
) -> str:
    """Normalize a Rust-returned path to match graph-path format.

    Rust APIs return absolute paths.  When the path refers to a
    project file, convert it to a project-relative graph path.
    Otherwise leave it as-is (external).
    """
    candidate = _to_relative(root, path)
    if candidate in project_files:
        return candidate
    return path


def _range_size(t: tuple[int, int, int, int, str]) -> tuple[int, int]:
    """Sort key: (line span, end column).  Smallest ranges first.

    The first element (line span) dominates; the second element
    (end column) only matters for single-line ranges where line
    span is 0, which is the one case where comparing columns is
    meaningful.
    """
    start_line, _start_col, end_line, end_col, _sid = t
    return (end_line - start_line, end_col)


_METHOD_KINDS = frozenset({SymbolKind.METHOD, SymbolKind.CONSTRUCTOR})

DEPENDENCY_EDGE_KINDS: frozenset[EdgeKind] = frozenset({
    EdgeKind.REFERENCES,
    EdgeKind.IMPORTS,
    EdgeKind.INHERITS,
    EdgeKind.OVERRIDES,
    EdgeKind.TYPE_OF,
    EdgeKind.RETURNS,
    EdgeKind.INSTANTIATES,
})


class CodeGraph:
    """A semantic code intelligence graph for a Python project."""

    def __init__(self) -> None:
        self._graph: rx.PyDiGraph = rx.PyDiGraph()

        # Secondary indexes
        self._id_to_index: dict[str, int] = {}
        self._file_to_nodes: dict[str, list[int]] = defaultdict(list)
        self._file_to_edges: dict[str, list[int]] = defaultdict(list)

        # Pre-materialized range cache for _find_enclosing_symbol (B1)
        #   file_str -> list of (start_line, start_col, end_line, end_col, symbol_id)
        #   sorted by range size ascending (smallest first)
        self._file_node_ranges: dict[str, list[tuple[int, int, int, int, str]]] = {}

        # Secondary index for fast name@line lookups (B4)
        #   (file, name_prefix) -> symbol_id
        self._name_prefix_index: dict[tuple[str, str], str] = {}

        # Diagnostics (separate from graph)
        self._diagnostics: dict[str, list[Diagnostic]] = {}

        # Dependency graph cache for external packages
        self._dependency_cache: dict[str, DependencyGraph] = {}

    # ── Construction ──────────────────────────────────────────

    @classmethod
    def build(
        cls,
        session: TyO3Session,
        *,
        report: GraphBuildReport | None = None,
    ) -> CodeGraph:
        """Build a complete code graph from a TyO3 session.

        Six-pass deterministic construction: all project nodes and all
        range caches exist before any reference is resolved.  This
        guarantees references attach to innermost enclosing symbols
        (not module fallback) and project-local targets are never
        externalized because of file ordering.

        First-party paths are normalized to project-relative POSIX
        paths so symbol IDs are portable and snapshot-friendly.

        If *report* is provided, failures are recorded on it so
        callers can programmatically inspect whether the graph is
        complete.
        """
        graph = cls()
        root = session.root
        native_paths = [str(p) for p in session.files()]
        graph_paths = [_to_relative(root, p) for p in native_paths]
        native_by_graph = dict(
            zip(graph_paths, native_paths, strict=True)
        )
        project_files = set(graph_paths)

        if report is not None:
            report.files_total = len(graph_paths)

        # ── Pass 1: collect symbols ───────────────────────────
        symbols_by_file: dict[str, list[Symbol]] = {}
        for graph_path in graph_paths:
            native_path = native_by_graph[graph_path]
            symbols = graph._collect_symbols_for_file(
                session, native_path, report=report
            )
            if symbols is not None:
                symbols_by_file[graph_path] = symbols

        if report is not None:
            report.files_indexed = len(symbols_by_file)

        # ── Pass 2: materialize all project nodes ─────────────
        for file_str, symbols in symbols_by_file.items():
            graph._materialize_file_nodes(file_str, symbols)

        # ── Pass 3: structural edges + range caches ───────────
        for file_str, symbols in symbols_by_file.items():
            graph._add_containment_edges_for_file(file_str, symbols)
            graph._build_range_cache_for_file(file_str)

        # ── Pass 4: semantic references ───────────────────────
        for file_str in symbols_by_file:
            graph._resolve_references_via_occurrences(
                session,
                file_str,
                project_files,
                report=report,
                root=root,
                native_by_graph=native_by_graph,
            )

        # ── Pass 5: inheritance and overrides ─────────────────
        for file_str, symbols in symbols_by_file.items():
            graph._resolve_inheritance(
                session,
                file_str,
                symbols,
                report=report,
                root=root,
                native_by_graph=native_by_graph,
                project_files=project_files,
            )

        # ── Pass 6: diagnostics (single check, distribute per-file)
        graph._collect_all_diagnostics(
            session,
            report=report,
            root=root,
            project_files=project_files,
        )

        return graph

    @classmethod
    def build_with_report(
        cls,
        session: TyO3Session,
    ) -> tuple[CodeGraph, GraphBuildReport]:
        """Build a graph and return it alongside a build report.

        Convenience wrapper around :meth:`build` that always returns
        a report, so callers can inspect failures without threading
        their own report instance.
        """
        report = GraphBuildReport()
        graph = cls.build(session, report=report)
        return graph, report

    # ── Pass helpers ──────────────────────────────────────────

    def _collect_symbols_for_file(
        self,
        session: TyO3Session,
        file_str: str,
        *,
        report: GraphBuildReport | None = None,
    ) -> list[Symbol] | None:
        """Collect symbols for one file from the session.

        Returns ``None`` when the file cannot be symbol-collected,
        so the caller can skip it for the remaining passes.
        """
        try:
            return session.document_symbols(file_str)
        except Exception as e:
            logger.warning(
                "Failed to get symbols for %s, skipping", file_str
            )
            if report is not None:
                report.failures.append(
                    GraphBuildFailure(
                        file=file_str,
                        phase="symbols",
                        error_type=type(e).__name__,
                        message=str(e),
                    )
                )
            return None

    def _materialize_file_nodes(
        self, file_str: str, symbols: list[Symbol]
    ) -> None:
        """Create module and symbol nodes for one file in the graph."""
        module_id = f"{file_str}::<module>"
        module_node = SymbolNode(
            symbol_id=module_id,
            name=PurePosixPath(file_str).stem,
            qualified_name="<module>",
            kind=SymbolKind.MODULE,
            file=file_str,
            range=Range.model_validate(
                {
                    "start": {"line": 1, "column": 1},
                    "end": {"line": 1, "column": 1},
                }
            ),
        )

        new_nodes: list[SymbolNode] = []
        if module_id not in self._id_to_index:
            new_nodes.append(module_node)

        for symbol in symbols:
            sid = symbol_id_from_symbol(file_str, symbol)
            if sid not in self._id_to_index:
                node = SymbolNode(
                    symbol_id=sid,
                    name=symbol.name,
                    qualified_name=symbol.qualified_name or symbol.name,
                    kind=symbol.kind,
                    file=file_str,
                    range=symbol.location.range,
                    selection_range=symbol.selection_range,
                )
                new_nodes.append(node)

        if new_nodes:
            indices = self._graph.add_nodes_from(new_nodes)
            for node, idx in zip(new_nodes, indices, strict=True):
                self._id_to_index[node.symbol_id] = idx
                self._file_to_nodes[node.file].append(idx)
                # Index for fast name@line lookups (B4)
                if "@" in node.symbol_id:
                    parts = node.symbol_id.split("::", 1)
                    if len(parts) == 2:
                        name_part = parts[1].split("@", 1)[0]
                        self._name_prefix_index[
                            (parts[0], name_part)
                        ] = node.symbol_id

    def _add_containment_edges_for_file(
        self, file_str: str, symbols: list[Symbol]
    ) -> None:
        """Add DEFINES/CONTAINS edges for all symbols in one file."""
        module_id = f"{file_str}::<module>"
        for symbol in symbols:
            sid = symbol_id_from_symbol(file_str, symbol)
            parent_id = self._resolve_parent_id(file_str, symbol, module_id)
            if parent_id and parent_id in self._id_to_index:
                edge_kind = (
                    EdgeKind.DEFINES
                    if parent_id == module_id
                    else EdgeKind.CONTAINS
                )
                edge = EdgeData(kind=edge_kind)
                self._add_edge(parent_id, sid, edge, file_str)

    def _build_range_cache_for_file(self, file_str: str) -> None:
        """Pre-materialize the range cache for one file.

        Sorted by total span ascending so the first containment match
        in ``_find_enclosing_symbol`` is the innermost symbol.
        """
        self._file_node_ranges[file_str] = sorted(
            [
                (
                    node.range.start.line,
                    node.range.start.column,
                    node.range.end.line,
                    node.range.end.column,
                    node.symbol_id,
                )
                for idx in self._file_to_nodes[file_str]
                for node in [self._graph[idx]]
                if node.kind != SymbolKind.MODULE
            ],
            key=_range_size,
        )

    def _collect_all_diagnostics(
        self,
        session: TyO3Session,
        *,
        report: GraphBuildReport | None = None,
        root: Path | None = None,
        project_files: set[str] | None = None,
    ) -> None:
        """Collect diagnostics with a single ``check()`` call, distribute per-file.

        Replaces the old per-file ``check_file()`` approach (N FFI
        calls) with a single project-wide check (1 FFI call).

        Normalizes Rust-returned absolute paths to project-relative
        graph paths when *root* and *project_files* are provided.
        """
        try:
            result = session.check()
        except Exception as e:
            logger.warning(
                "Failed to run project check, skipping diagnostics"
            )
            if report is not None:
                report.failures.append(
                    GraphBuildFailure(
                        file="<project>",
                        phase="diagnostics",
                        error_type=type(e).__name__,
                        message=str(e),
                    )
                )
            return
        for diagnostic in result.diagnostics:
            if diagnostic.file:
                norm_file = (
                    _normalize_result_path(
                        root, diagnostic.file, project_files
                    )
                    if root is not None and project_files is not None
                    else diagnostic.file
                )
                self._diagnostics.setdefault(norm_file, []).append(
                    diagnostic
                )

    # ── Parent resolution for containment ─────────────────────

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
            candidate_id = self._find_symbol_in_file(
                file_str, symbol.container_name
            )
            if candidate_id is not None:
                return candidate_id
        return module_id

    def _find_symbol_in_file(self, file_path: str, name: str) -> str | None:
        """Find a symbol ID for *name* in *file_path*.

        Tries exact match first, then uses the ``_name_prefix_index``
        for fast ``name@line`` lookups (B4), then falls back to
        matching by ``node.name`` within the file.
        """
        exact = f"{file_path}::{name}"
        if exact in self._id_to_index:
            return exact
        # Fast name@line lookup via secondary index (B4)
        cached = self._name_prefix_index.get((file_path, name))
        if cached is not None:
            return cached
        # Fallback: search by short name within the file's nodes.
        # Handles qualified-name mismatches where the SID is
        # e.g. "models.py::Models" but we only have "User".
        for idx in self._file_to_nodes.get(file_path, []):
            node: SymbolNode = self._graph[idx]
            if node.name == name:
                return node.symbol_id
        return None

    # ── Reference resolution ──────────────────────────────────

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
                file_str,
                start.line,
                start.column,
                include_declaration=False,
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

    def _resolve_references_via_occurrences(
        self,
        session: TyO3Session,
        file_str: str,
        project_files: set[str],
        *,
        report: GraphBuildReport | None = None,
        root: Path | None = None,
        native_by_graph: dict[str, str] | None = None,
    ) -> None:
        """Resolve references using the batch file_occurrences API.

        Makes a single Rust call per file that resolves every name-like
        token to its definition target.  Replaces the per-token
        ``goto_definition`` approach with O(1) FFI calls.

        *project_files* distinguishes project-local targets (which
        already exist as nodes from Pass 2) from external targets
        (which need stub nodes).
        """
        native_file = (
            native_by_graph.get(file_str, file_str)
            if native_by_graph
            else file_str
        )
        try:
            occurrences = session.file_occurrences(native_file)
        except Exception as e:
            logger.warning(
                "Failed to get file occurrences for %s, falling back",
                file_str,
            )
            if report is not None:
                report.failures.append(
                    GraphBuildFailure(
                        file=file_str,
                        phase="references",
                        error_type=type(e).__name__,
                        message=str(e),
                    )
                )
            # Fall back to the token-based approach
            self._resolve_references_via_tokens(
                session,
                file_str,
                root=root,
                native_by_graph=native_by_graph,
                project_files=project_files,
            )
            return

        for occ in occurrences:
            if occ.target_file is None or occ.target_name is None:
                continue

            target_file_raw = occ.target_file
            target_file = (
                _normalize_result_path(
                    root, target_file_raw, project_files
                )
                if root is not None
                else target_file_raw
            )
            target_name = occ.target_name

            # Build the target's symbol_id.
            # Prefer the qualified name from Rust (e.g. "User.save")
            # which matches the SID format produced by document_symbols:
            #   file::qualified_name  →  "models.py::User.save"
            # Fall back to short name for top-level symbols where
            # qualified_name is None (same as short name).
            if occ.target_qualified_name:
                target_sid = f"{target_file}::{occ.target_qualified_name}"
            else:
                target_sid = f"{target_file}::{target_name}"

            # Ensure the target node exists — create a stub if external
            if target_sid not in self._id_to_index:
                # Try flexible lookup by short name within the target file.
                # Handles qualified-name mismatches (e.g. Rust returns
                # "User" but the node is stored as
                # "models.py::models.User").
                found = self._find_symbol_in_file(target_file, target_name)
                if found:
                    target_sid = found
                else:
                    target_sid_ensured = self._ensure_target_node_simple(
                        target_file, target_sid, target_name, project_files
                    )
                    if target_sid_ensured is None:
                        continue
                    target_sid = target_sid_ensured

            # Determine the enclosing symbol at this occurrence's location
            enclosing_id = self._find_enclosing_symbol(file_str, occ.range)
            if enclosing_id is None:
                continue

            # Don't create self-references for definition sites
            if enclosing_id == target_sid:
                continue

            role = occ.role  # Already a ReferenceRole

            edge = EdgeData(
                kind=EdgeKind.REFERENCES,
                file=file_str,
                range=occ.range,
                role=role,
            )
            self._add_edge(enclosing_id, target_sid, edge, file_str)

            # Add module-level IMPORTS edge for any cross-file reference.
            # The Rust file_occurrences API reports import role occurrences
            # but does not yet resolve their targets (target_file is None).
            # As a pragmatic bridge, every resolved cross-file reference
            # implies a module-level import dependency.
            if target_file and target_file != file_str:
                self._add_import_edge(
                    file_str, target_file, occ.range, project_files
                )

    def _add_import_edge(
        self,
        source_file: str,
        target_file: str,
        range: Range,
        project_files: set[str],
    ) -> None:
        """Add a module-level IMPORTS edge between two files."""
        source_module = f"{source_file}::<module>"
        target_module = f"{target_file}::<module>"

        if source_module not in self._id_to_index:
            return

        if target_module not in self._id_to_index:
            if target_file in project_files:
                return  # Project file without a module node — skip
            package = self._infer_package(target_file) or "unknown"
            target_module = f"{package}::<module>"
            if target_module not in self._id_to_index:
                self._add_stub_node(
                    symbol_id=target_module,
                    name=package,
                    qualified_name="<module>",
                    kind=SymbolKind.MODULE,
                    package=package,
                )

        self._add_edge(
            source_module,
            target_module,
            EdgeData(kind=EdgeKind.IMPORTS, file=source_file, range=range),
            source_file,
        )

    def _ensure_target_node_simple(
        self,
        target_file: str,
        target_sid: str,
        target_name: str,
        project_files: set[str],
    ) -> str | None:
        """Ensure a reference target exists as a node.

        When *target_file* is a project file, all nodes already exist
        (Pass 2 guaranteed this).  If we cannot find the target by ID
        or name lookup, it is a symbol-identity mismatch — log a
        warning, do *not* create a stub.

        When *target_file* is external, create a stub node keyed by
        inferred package name.
        """
        if target_file in project_files:
            # All project nodes exist (Pass 2).  If we can't find it,
            # it's a symbol-identity mismatch.  Log and skip — do not
            # create a stub.
            logger.debug(
                "Could not resolve project-local target %s in %s",
                target_name,
                target_file,
            )
            return None

        package = self._infer_package(target_file)
        ext_sid = f"{package}::{target_name}" if package else target_sid
        if ext_sid not in self._id_to_index:
            self._add_stub_node(
                symbol_id=ext_sid,
                name=target_name,
                qualified_name=target_name,
                kind=SymbolKind.UNKNOWN,
                package=package or "unknown",
            )
        return ext_sid

    def _resolve_references_via_tokens(
        self,
        session: TyO3Session,
        file_str: str,
        *,
        root: Path | None = None,
        native_by_graph: dict[str, str] | None = None,
        project_files: set[str] | None = None,
    ) -> None:
        """Resolve references using semantic tokens + goto_definition.

        For each name-like token in the file, call goto_definition to
        find what symbol it refers to. Create a REFERENCES edge from
        the enclosing symbol to the target symbol.
        """
        native_file = (
            native_by_graph.get(file_str, file_str)
            if native_by_graph
            else file_str
        )
        try:
            tokens = session.semantic_tokens(native_file)
        except Exception:
            logger.warning(
                "Failed to get semantic tokens for %s", file_str
            )
            return

        # Filter to name-like tokens (not keywords, strings, numbers)
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
                targets = session.goto_definition(
                    native_file, start.line, start.column
                )
            except Exception:
                continue

            if not targets:
                continue

            target = targets[0]
            target_file_raw = str(target.path)
            target_file = (
                _normalize_result_path(root, target_file_raw, project_files)
                if root is not None and project_files is not None
                else target_file_raw
            )

            # Build the target's symbol_id
            if target.symbol and target.symbol.qualified_name:
                target_sid = (
                    f"{target_file}::{target.symbol.qualified_name}"
                )
            elif target.symbol:
                target_sid = (
                    f"{target_file}::{target.symbol.name}"
                    f"@{target.range.start.line}"
                )
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
            enclosing_id = self._find_enclosing_symbol(
                file_str, token.range
            )
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
        # Only create stubs for truly external paths
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
        # In-project — already exists or identity mismatch
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
            sep = "/.venv/" if "/.venv/" in file_path else "/venv/"
            parts = file_path.split(sep)[1].split("/")
            if len(parts) > 2 and parts[0] == "lib":
                # .venv/lib/python3.x/site-packages/foo/...
                for i, part in enumerate(parts):
                    if part == "site-packages" and i + 1 < len(parts):
                        return parts[i + 1]
        return None

    # ── Inheritance resolution ────────────────────────────────

    def _resolve_inheritance(
        self,
        session: TyO3Session,
        file_str: str,
        symbols: list[Symbol],
        *,
        report: GraphBuildReport | None = None,
        root: Path | None = None,
        native_by_graph: dict[str, str] | None = None,
        project_files: set[str] | None = None,
    ) -> None:
        """Add INHERITS and OVERRIDES edges for class symbols."""
        native_file = (
            native_by_graph.get(file_str, file_str)
            if native_by_graph
            else file_str
        )
        for symbol in symbols:
            if symbol.kind != SymbolKind.CLASS:
                continue

            start = (
                symbol.selection_range.start
                if symbol.selection_range
                else symbol.location.range.start
            )
            try:
                hierarchy = session.type_hierarchy(
                    native_file, start.line, start.column
                )
            except Exception as e:
                if report is not None:
                    report.failures.append(
                        GraphBuildFailure(
                            file=file_str,
                            phase="inheritance",
                            error_type=type(e).__name__,
                            message=str(e),
                        )
                    )
                continue

            if hierarchy is None:
                continue

            sid = symbol_id_from_symbol(file_str, symbol)

            # Add INHERITS edges to supertypes
            for supertype in hierarchy.supertypes:
                super_file_raw = str(supertype.path)
                super_file = (
                    _normalize_result_path(
                        root, super_file_raw, project_files
                    )
                    if root is not None and project_files is not None
                    else super_file_raw
                )
                super_sid = self._find_symbol_in_file(
                    super_file, supertype.name
                )

                # If not found locally, create an external stub node
                if super_sid is None:
                    package = self._infer_package(super_file)
                    if (
                        package is None
                        and super_file not in self._file_to_nodes
                    ):
                        package = "unknown"
                    if package:
                        ext_sid = f"{package}::{supertype.name}"
                        kind = SymbolKind.CLASS
                        super_sid = ext_sid
                        if ext_sid not in self._id_to_index:
                            self._add_stub_node(
                                symbol_id=ext_sid,
                                name=supertype.name,
                                qualified_name=(
                                    f"{package}.{supertype.name}"
                                ),
                                kind=kind,
                                package=package,
                            )
                    else:
                        continue

                edge = EdgeData(kind=EdgeKind.INHERITS)
                self._add_edge(sid, super_sid, edge, file_str)

            # Derive OVERRIDES: collect methods from the child class,
            # then walk the full supertype chain to find overridden
            # methods.
            child_methods = [
                s
                for s in symbols
                if s.kind in _METHOD_KINDS
                and s.container_name == symbol.name
            ]
            if not child_methods:
                continue

            # Collect ancestor methods by walking the INHERITS chain
            ancestor_methods: dict[str, str] = {}  # name -> symbol_id
            visited: set[str] = {sid}
            queue: deque[str] = deque([sid])

            while queue:
                current_sid = queue.popleft()
                current_idx = self._id_to_index.get(current_sid)
                if current_idx is None:
                    continue

                for _src, succ_idx, edge_data in self._graph.out_edges(
                    current_idx
                ):
                    if edge_data.kind != EdgeKind.INHERITS:
                        continue

                    parent_sid = self._graph[succ_idx].symbol_id
                    if parent_sid not in visited:
                        visited.add(parent_sid)
                        queue.append(parent_sid)

                    # Collect this parent's methods
                    for c in self.children(parent_sid):
                        if (
                            c.kind in _METHOD_KINDS
                            and c.name not in ancestor_methods
                        ):
                            ancestor_methods[c.name] = c.symbol_id

            for method in child_methods:
                if method.name in ancestor_methods:
                    method_sid = symbol_id_from_symbol(file_str, method)
                    parent_method_sid = ancestor_methods[method.name]
                    edge = EdgeData(kind=EdgeKind.OVERRIDES)
                    self._add_edge(
                        method_sid, parent_method_sid, edge, file_str
                    )

    def _find_enclosing_symbol(
        self, file_str: str, range: Range
    ) -> str | None:
        """Find the innermost symbol in *file_str* that contains *range*.

        Uses the pre-materialized range cache (B1) to avoid per-node
        FFI calls.  The cache is sorted by range size ascending, so the
        first containment match is the smallest enclosing symbol.

        Returns the symbol_id, or the module node if no enclosing symbol
        is found.
        """
        cached = self._file_node_ranges.get(file_str, [])
        for start_line, start_col, end_line, end_col, sid in cached:
            # Check containment: node range must fully contain the target range
            if (start_line, start_col) <= (
                range.start.line,
                range.start.column,
            ) and (end_line, end_col) >= (range.end.line, range.end.column):
                # First match is smallest due to sort order
                return sid

        # Fall back to module node
        module_id = f"{file_str}::<module>"
        if module_id in self._id_to_index:
            return module_id
        return None

    # ── Internal graph mutation ───────────────────────────────

    def _add_node(self, node: SymbolNode) -> int:
        """Add a SymbolNode to the graph and update indexes."""
        if node.symbol_id in self._id_to_index:
            return self._id_to_index[node.symbol_id]
        idx = self._graph.add_node(node)
        self._id_to_index[node.symbol_id] = idx
        self._file_to_nodes[node.file].append(idx)
        # Index for fast name@line lookups (B4)
        if "@" in node.symbol_id:
            parts = node.symbol_id.split("::", 1)
            if len(parts) == 2:
                name_part = parts[1].split("@", 1)[0]
                self._name_prefix_index[
                    (parts[0], name_part)
                ] = node.symbol_id
        return idx

    def _add_edge(
        self,
        source_id: str,
        target_id: str,
        data: EdgeData,
        file: str,
    ) -> int | None:
        """Add an edge between two symbols by their IDs."""
        src_idx = self._id_to_index.get(source_id)
        tgt_idx = self._id_to_index.get(target_id)
        if src_idx is None or tgt_idx is None:
            return None
        edge_idx = self._graph.add_edge(src_idx, tgt_idx, data)
        self._file_to_edges[file].append(edge_idx)
        return edge_idx

    def _rebuild_indexes(self) -> None:
        """Reconstruct all secondary indexes from the graph's current state.

        Call this after any bulk node/edge removal (e.g.
        ``remove_nodes_from``) since RustworkX's swap-and-pop
        invalidates stored indices.
        """
        self._id_to_index.clear()
        self._file_to_nodes.clear()
        self._file_to_edges.clear()
        self._file_node_ranges.clear()
        self._name_prefix_index.clear()
        for idx in self._graph.node_indices():
            node: SymbolNode = self._graph[idx]
            self._id_to_index[node.symbol_id] = idx
            self._file_to_nodes[node.file].append(idx)
            # Rebuild name_prefix_index (B4)
            if "@" in node.symbol_id:
                parts = node.symbol_id.split("::", 1)
                if len(parts) == 2:
                    name_part = parts[1].split("@", 1)[0]
                    self._name_prefix_index[
                        (parts[0], name_part)
                    ] = node.symbol_id
        for edge_idx in self._graph.edge_indices():
            data = self._graph.get_edge_data_by_index(edge_idx)
            if (
                data is not None
                and hasattr(data, "file")
                and data.file is not None
            ):
                self._file_to_edges[data.file].append(edge_idx)
        # Rebuild range cache (B1)
        for file_str in self._file_to_nodes:
            self._file_node_ranges[file_str] = sorted(
                [
                    (
                        node.range.start.line,
                        node.range.start.column,
                        node.range.end.line,
                        node.range.end.column,
                        node.symbol_id,
                    )
                    for idx in self._file_to_nodes[file_str]
                    for node in [self._graph[idx]]
                    if node.kind != SymbolKind.MODULE
                ],
                key=_range_size,
            )

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
                {
                    "start": {"line": 1, "column": 1},
                    "end": {"line": 1, "column": 1},
                }
            ),
            external=True,
            package=package,
        )
        return self._add_node(node)

    def _edges_of_kind(
        self,
        symbol_id: str,
        kinds: set[EdgeKind],
        *,
        incoming: bool = False,
    ) -> list[tuple[int, EdgeData]]:
        """Return all edges matching *kinds* for a node.

        When *incoming* is False (default), returns outgoing edges as
        ``(target_index, edge_data)`` pairs.
        When *incoming* is True, returns incoming edges as
        ``(source_index, edge_data)`` pairs.

        Uses RustworkX's ``in_edges`` / ``out_edges`` which return
        all parallel edges in a single efficient Rust-side call.
        """
        idx = self._id_to_index.get(symbol_id)
        if idx is None:
            return []
        result: list[tuple[int, EdgeData]] = []
        if incoming:
            for src, _tgt, data in self._graph.in_edges(idx):
                if data.kind in kinds:
                    result.append((src, data))
        else:
            for _src, tgt, data in self._graph.out_edges(idx):
                if data.kind in kinds:
                    result.append((tgt, data))
        return result

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
        return [
            self._graph[i] for i in self._file_to_nodes.get(path, [])
        ]

    def symbols_of_kind(self, kind: SymbolKind) -> list[SymbolNode]:
        """All symbols of a given kind."""
        result: list[SymbolNode] = []
        for i in self._graph.node_indices():
            node = self._graph[i]
            if node.kind == kind:
                result.append(node)
        return result

    # ── Reference queries ─────────────────────────────────────

    def references_to(self, symbol_id: str) -> list[EdgeData]:
        """All incoming REFERENCES edges to a symbol."""
        return [
            data
            for _, data in self._edges_of_kind(
                symbol_id, {EdgeKind.REFERENCES}, incoming=True
            )
        ]

    def references_from(
        self, symbol_id: str
    ) -> list[tuple[SymbolNode, EdgeData]]:
        """All outgoing REFERENCES edges from a symbol."""
        return [
            (self._graph[tgt_idx], data)
            for tgt_idx, data in self._edges_of_kind(
                symbol_id, {EdgeKind.REFERENCES}
            )
        ]

    # ── Structural queries ────────────────────────────────────

    def children(self, symbol_id: str) -> list[SymbolNode]:
        """Direct children (outgoing DEFINES/CONTAINS edges)."""
        seen: set[int] = set()
        result: list[SymbolNode] = []
        for tgt_idx, _ in self._edges_of_kind(
            symbol_id, {EdgeKind.DEFINES, EdgeKind.CONTAINS}
        ):
            if tgt_idx not in seen:
                seen.add(tgt_idx)
                result.append(self._graph[tgt_idx])
        return result

    def parent(self, symbol_id: str) -> SymbolNode | None:
        """Enclosing symbol (incoming DEFINES/CONTAINS edge)."""
        edges = self._edges_of_kind(
            symbol_id,
            {EdgeKind.DEFINES, EdgeKind.CONTAINS},
            incoming=True,
        )
        if edges:
            return self._graph[edges[0][0]]
        return None

    def module_for(self, symbol_id: str) -> SymbolNode | None:
        """The MODULE node for this symbol's file."""
        file = file_from_symbol_id(symbol_id)
        module_id = f"{file}::<module>"
        return self.symbol(module_id)

    # ── Dependency analysis ───────────────────────────────────

    def _semantic_subgraph(self, kinds: frozenset[EdgeKind]) -> rx.PyDiGraph:
        """Build a subgraph containing only edges of the given kinds.

        Node indices are preserved (same as the main graph) so callers can
        use ``self._id_to_index`` for lookups.
        """
        sub = self._graph.copy()
        to_remove = [
            sub.get_edge_endpoints_by_index(edge_idx)
            for edge_idx in sub.edge_indices()
            if sub.get_edge_data_by_index(edge_idx).kind not in kinds
        ]
        sub.remove_edges_from(to_remove)
        return sub

    def dependencies(
        self,
        symbol_id: str,
        *,
        kinds: frozenset[EdgeKind] | None = None,
    ) -> set[str]:
        """All symbols this one directly depends on (semantic edges only)."""
        edge_kinds = DEPENDENCY_EDGE_KINDS if kinds is None else kinds
        return {
            self._graph[tgt_idx].symbol_id
            for tgt_idx, _data in self._edges_of_kind(symbol_id, edge_kinds)
        }

    def dependents(
        self,
        symbol_id: str,
        *,
        kinds: frozenset[EdgeKind] | None = None,
    ) -> set[str]:
        """All symbols that directly depend on this one (semantic edges only)."""
        edge_kinds = DEPENDENCY_EDGE_KINDS if kinds is None else kinds
        return {
            self._graph[src_idx].symbol_id
            for src_idx, _data in self._edges_of_kind(
                symbol_id, edge_kinds, incoming=True
            )
        }

    def transitive_dependencies(
        self,
        symbol_id: str,
        *,
        kinds: frozenset[EdgeKind] | None = None,
    ) -> set[str]:
        """All symbols reachable via semantic edges from this one."""
        edge_kinds = DEPENDENCY_EDGE_KINDS if kinds is None else kinds
        filtered = self._semantic_subgraph(edge_kinds)
        idx = self._id_to_index.get(symbol_id)
        if idx is None:
            return set()
        # filtered uses the same indices as the main graph
        reachable = rx.descendants(filtered, idx)
        return {self._graph[i].symbol_id for i in reachable}

    def transitive_dependents(
        self,
        symbol_id: str,
        *,
        kinds: frozenset[EdgeKind] | None = None,
    ) -> set[str]:
        """All symbols that transitively depend on this one (semantic edges only)."""
        edge_kinds = DEPENDENCY_EDGE_KINDS if kinds is None else kinds
        filtered = self._semantic_subgraph(edge_kinds)
        idx = self._id_to_index.get(symbol_id)
        if idx is None:
            return set()
        # filtered uses the same indices as the main graph
        reachable = rx.ancestors(filtered, idx)
        return {self._graph[i].symbol_id for i in reachable}

    # ── Graph algorithms ──────────────────────────────────────

    def _build_module_graph(self) -> tuple[rx.PyDiGraph, dict[str, int]]:
        """Build a module-level graph from IMPORTS edges.

        Returns ``(module_graph, sid_to_index)`` where *module_graph*
        nodes are module ``symbol_id`` strings and *sid_to_index*
        maps ``symbol_id`` → node index in the module graph.
        """
        module_indices = [
            i for i in self._graph.node_indices()
            if self._graph[i].kind == SymbolKind.MODULE
        ]
        if len(module_indices) < 2:
            return rx.PyDiGraph(), {}

        mod_graph = rx.PyDiGraph()
        sid_to_midx: dict[str, int] = {}
        for i in module_indices:
            sid = self._graph[i].symbol_id
            midx = mod_graph.add_node(sid)
            sid_to_midx[sid] = midx

        # Map every node to its module for aggregation
        node_to_module: dict[int, str] = {}
        for mi in module_indices:
            module_sid = self._graph[mi].symbol_id
            file = file_from_symbol_id(module_sid)
            for ni in self._file_to_nodes.get(file, []):
                node_to_module[ni] = module_sid

        for edge_idx in self._graph.edge_indices():
            data = self._graph.get_edge_data_by_index(edge_idx)
            if getattr(data, "kind", None) != EdgeKind.IMPORTS:
                continue
            src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
            src_mod = node_to_module.get(src)
            tgt_mod = node_to_module.get(tgt)
            if src_mod and tgt_mod and src_mod != tgt_mod:
                mi_src = sid_to_midx.get(src_mod)
                mi_tgt = sid_to_midx.get(tgt_mod)
                if mi_src is not None and mi_tgt is not None:
                    mod_graph.add_edge(mi_src, mi_tgt, None)

        return mod_graph, sid_to_midx

    def import_cycles(self) -> list[list[str]]:
        """Detect circular import chains in the module dependency graph.

        Builds a module-level dependency graph from IMPORTS edges only.
        Two modules are adjacent if one imports the other.

        Returns a list of cycles, where each cycle is a list of
        module symbol_ids in order.
        """
        mod_graph, _sid_to_midx = self._build_module_graph()
        if mod_graph.num_nodes() < 2:
            return []

        # Build adjacency dict from the module graph
        adj: dict[str, set[str]] = {
            mod_graph[i]: set() for i in mod_graph.node_indices()
        }

        for edge_idx in mod_graph.edge_indices():
            src, tgt = mod_graph.get_edge_endpoints_by_index(edge_idx)
            adj[mod_graph[src]].add(mod_graph[tgt])

        # DFS-based cycle detection
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {sid: WHITE for sid in adj}
        cycles: list[list[str]] = []

        def dfs(u: str, stack: list[str], stack_set: set[str]) -> None:
            color[u] = GRAY
            stack.append(u)
            stack_set.add(u)
            for v in adj.get(u, set()):
                if color.get(v) == GRAY:
                    # Found a cycle: extract from v's position to end
                    cycle_start = stack.index(v)
                    cycles.append(list(stack[cycle_start:]))
                elif color.get(v) == WHITE:
                    dfs(v, stack, stack_set)
            stack.pop()
            stack_set.discard(u)
            color[u] = BLACK

        for sid in adj:
            if color[sid] == WHITE:
                dfs(sid, [], set())

        return cycles

    def hub_symbols(self, top_n: int = 10) -> list[tuple[str, float]]:
        """Find "hub" symbols using betweenness centrality.

        Hub symbols are those that bridge disconnected parts of
        the graph — they sit on many shortest paths between
        other symbols. High centrality often indicates a symbol
        that would cause widespread breakage if changed.

        Computes betweenness centrality over the full directed
        graph and returns the top *top_n* (symbol_id, score)
        pairs, sorted descending by score.
        """
        try:
            # rustworkx betweenness_centrality returns a dict-like
            # mapping node_index -> float
            centrality = rx.betweenness_centrality(self._graph)
        except Exception:
            return []

        scored = [
            (self._graph[i].symbol_id, score)
            for i, score in centrality.items()
            if score > 0.0
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]

    def import_cycle_groups(self) -> list[set[str]]:
        """Groups of mutually-dependent modules (strongly connected components).

        Unlike :meth:`import_cycles`, which enumerates every cycle path,
        this returns connected components in the module dependency graph —
        each group contains all modules that are reachable from each other
        through import edges.

        Returns a list of sets of module symbol_ids (one set per SCC
        with more than one module).  Singles (files with no cross-file
        deps) are excluded.
        """
        mod_graph, _sid_to_midx = self._build_module_graph()
        if mod_graph.num_nodes() < 2:
            return []

        sccs = rx.strongly_connected_components(mod_graph)
        return [
            {mod_graph[i] for i in scc}
            for scc in sccs
            if len(scc) > 1
        ]

    def is_reachable(self, from_sid: str, to_sid: str) -> bool:
        """Check if there is a directed path from one symbol to another.

        Uses RustworkX's ``has_path`` which performs BFS/DFS to determine
        connectivity.
        """
        src = self._id_to_index.get(from_sid)
        tgt = self._id_to_index.get(to_sid)
        if src is None or tgt is None:
            return False
        return rx.has_path(self._graph, src, tgt, as_undirected=False)

    @property
    def has_cycles(self) -> bool:
        """Quick check for any cycles in the graph.

        Returns True if the graph contains at least one directed cycle.
        Uses RustworkX's ``is_directed_acyclic_graph``.
        """
        return not rx.is_directed_acyclic_graph(self._graph)

    def topological_order(self) -> list[str]:
        """Return symbol IDs in dependency order.

        Returns an empty list if the graph has cycles.
        Uses RustworkX's ``topological_sort``.
        """
        try:
            order = rx.topological_sort(self._graph)
        except rx.DAGHasCycle:
            return []
        return [self._graph[i].symbol_id for i in order]

    def subgraph_for_file(self, file_path: str) -> rx.PyDiGraph:
        """Extract a subgraph containing all symbols defined in a file
        and their immediate reference neighbours (within the project).

        Uses ``subgraph_with_nodemap`` (B2) for a single Rust call that
        copies nodes and all edges between included nodes, replacing the
        O(E_total) edge scan with Rust-side filtering.

        Returns a new PyDiGraph that can be queried, exported, or
        visualized independently of the main graph.
        """
        core_indices = set(self._file_to_nodes.get(file_path, []))
        if not core_indices:
            return rx.PyDiGraph()

        # Include immediate neighbours (1-hop)
        included = set(core_indices)
        for ci in core_indices:
            for succ in self._graph.neighbors(ci):
                included.add(succ)
            for pred in self._graph.predecessor_indices(ci):
                included.add(pred)

        # Single Rust call: copies nodes and all edges between included nodes
        sub, _node_map = self._graph.subgraph_with_nodemap(
            sorted(included)
        )
        return sub

    # ── Incremental updates ───────────────────────────────────

    def update_file(self, session: TyO3Session, path: str) -> None:
        """Re-index a file and all files that reference symbols in it.

        Rebuilds the changed file plus its reverse-dependency set to
        preserve cross-file reference edges.  This is cheaper than a
        full rebuild for large projects (|affected| << |total|) while
        remaining correct for all edge types.

        For the initial implementation, this delegates to a full
        rebuild.  Once profiling shows this is a bottleneck, the
        targeted approach (documented above) should be implemented.
        """
        fresh = CodeGraph.build(session)
        # Swap all internal state — the old graph is discarded.
        self.__dict__.update(fresh.__dict__)

    # ── Previously implemented algorithms ─────────────────────

    def coupling_between(self, file_a: str, file_b: str) -> int:
        """Count REFERENCES edges between two files.

        Uses targeted ``out_edges()`` calls (B3) instead of scanning
        all edges in the graph, reducing the operation from O(E_total)
        to O(E_file_a + E_file_b).
        """
        nodes_a = set(self._file_to_nodes.get(file_a, []))
        nodes_b = set(self._file_to_nodes.get(file_b, []))
        if not nodes_a or not nodes_b:
            return 0
        count = 0
        # Count REFERENCES edges from A -> B
        for idx in nodes_a:
            for _src, tgt, data in self._graph.out_edges(idx):
                if data.kind == EdgeKind.REFERENCES and tgt in nodes_b:
                    count += 1
        # Count REFERENCES edges from B -> A
        for idx in nodes_b:
            for _src, tgt, data in self._graph.out_edges(idx):
                if data.kind == EdgeKind.REFERENCES and tgt in nodes_a:
                    count += 1
        return count

    # ── External symbol resolution ───────────────────────────

    def external_symbols(self) -> list[SymbolNode]:
        """All stub nodes for external (dependency) symbols."""
        result: list[SymbolNode] = []
        for i in self._graph.node_indices():
            node = self._graph[i]
            if node.external:
                result.append(node)
        return result

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
        return [
            d
            for d in file_diags
            if d.range and _ranges_overlap(d.range, node.range)
        ]

    def all_diagnostics(self) -> list[Diagnostic]:
        """All diagnostics across all files."""
        return [
            d for diags in self._diagnostics.values() for d in diags
        ]


def _ranges_overlap(a: Range, b: Range) -> bool:
    """Check if two ranges overlap."""
    a_start = (a.start.line, a.start.column)
    a_end = (a.end.line, a.end.column)
    b_start = (b.start.line, b.start.column)
    b_end = (b.end.line, b.end.column)
    return a_start <= b_end and b_start <= a_end
