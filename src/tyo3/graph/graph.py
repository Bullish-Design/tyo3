"""CodeGraph — the semantic code intelligence graph."""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import rustworkx as rx

from tyo3.graph.dependency import DependencyGraph
from tyo3.graph.identity import derive_durable_id, file_from_durable_id, make_module_durable_id
from tyo3.graph.models import (
    EdgeData,
    EdgeDiff,
    EdgeKind,
    GraphBuildFailure,
    GraphBuildReport,
    GraphDiff,
    SymbolNode,
)
from tyo3.models.advanced import SemanticTokenModifier, SemanticTokenType
from tyo3.models.analysis import Diagnostic, Range, SyncResult
from tyo3.models.navigation import ReferenceRole
from tyo3.models.symbols import Symbol, SymbolKind

if TYPE_CHECKING:
    from tyo3.session import TyO3Session

logger = logging.getLogger(__name__)


def _to_relative(root: Path, path: str) -> str:
    """Convert an absolute path to a project-relative POSIX string.

    *root* must already be resolved (call ``root.resolve()`` once
    before passing it).

    If *path* is not under *root* (e.g. an external path), returns it
    unchanged so external references stay explicitly external.
    """
    try:
        return str(PurePosixPath(Path(path).resolve().relative_to(root)))
    except ValueError:
        return path  # External path — return as-is


def _normalize_result_path(root: Path, path: str, project_files: set[str]) -> str:
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
    start_line, _start_col, end_line, end_col, _did = t
    return (end_line - start_line, end_col)


_METHOD_KINDS = frozenset({SymbolKind.METHOD, SymbolKind.CONSTRUCTOR})

DEPENDENCY_EDGE_KINDS: frozenset[EdgeKind] = frozenset(
    {
        EdgeKind.REFERENCES,
        EdgeKind.IMPORTS,
        EdgeKind.INHERITS,
        EdgeKind.OVERRIDES,
        EdgeKind.TYPE_OF,
        EdgeKind.RETURNS,
        EdgeKind.INSTANTIATES,
    }
)


class CodeGraph:
    """A semantic code intelligence graph for a Python project."""

    def __init__(self) -> None:
        self._graph: rx.PyDiGraph = rx.PyDiGraph()

        # Secondary indexes
        self._id_to_index: dict[str, int] = {}
        self._file_to_nodes: dict[str, list[int]] = defaultdict(list)
        self._file_to_edges: dict[str, list[int]] = defaultdict(list)

        # Reverse-dependency index (Phase 6): target_file -> {source_files that import it}.
        # Maintained in _add_import_edge (incremental) and rebuilt authoritatively in
        # _rebuild_indexes. Drives inbound edge revalidation in apply_delta without a
        # global rescan (architecture §5.1).
        self._file_importers: dict[str, set[str]] = defaultdict(set)

        # The resolved project root, set at build() time. apply_delta needs it to map
        # the delta's absolute paths to project-relative graph paths, and `source` may
        # be a Snapshot (no .root). None until build() runs.
        self._root: Path | None = None

        # Pre-materialized range cache for _find_enclosing_symbol (B1)
        #   file_str -> list of (start_line, start_col, end_line, end_col, durable_id)
        #   sorted by range size ascending (smallest first)
        self._file_node_ranges: dict[str, list[tuple[int, int, int, int, str]]] = {}

        # Secondary index: (file, qualified_name) -> durable_id
        # Populated during node materialisation and used by _find_symbol_in_file
        # to resolve engine-returned names (which lack DurableIds) to graph nodes.
        self._name_to_id: dict[tuple[str, str], str] = {}

        # Secondary index for fast name@line lookups (B4)
        #   (file, name_prefix) -> durable_id
        self._name_prefix_index: dict[tuple[str, str], str] = {}

        # Diagnostics (separate from graph)
        self._diagnostics: dict[str, list[Diagnostic]] = {}

        # Dependency graph cache for external packages
        self._dependency_cache: dict[str, DependencyGraph] = {}

        # Memoized semantic subgraphs keyed by filtered edge-kinds
        #   frozenset[EdgeKind] -> rx.PyDiGraph
        self._semantic_subgraph_cache: dict[frozenset[EdgeKind], rx.PyDiGraph] = {}

        # MVCC graph state (Phase 7). HEAD graphs are mutable and advance via
        # apply_delta; pinned graphs are immutable copies tied to one revision.
        self._revision: int | None = None
        self._frozen = False

    # ── Construction ──────────────────────────────────────────

    @classmethod
    def build(
        cls,
        session,
        *,
        report: GraphBuildReport | None = None,
        root: Path | None = None,
    ) -> CodeGraph:
        """Build a complete code graph from a TyO3 session or Snapshot.

        Six-pass deterministic construction: all project nodes and all
        range caches exist before any reference is resolved.  This
        guarantees references attach to innermost enclosing symbols
        (not module fallback) and project-local targets are never
        externalized because of file ordering.

        First-party paths are normalized to project-relative POSIX
        paths so symbol IDs are portable and snapshot-friendly.

        *root* overrides the project root when *session* is a Snapshot
        (which has no ``.root`` attribute). Defaults to ``session.root``
        for TyO3Session.

        If *report* is provided, failures are recorded on it so
        callers can programmatically inspect whether the graph is
        complete.
        """
        graph = cls()
        if root is not None:
            root_resolved = root
        else:
            root_resolved = session.root.resolve()
        graph._root = root_resolved
        native_paths = [str(p) for p in session.files()]
        graph_paths = [_to_relative(root_resolved, p) for p in native_paths]
        native_by_graph = dict(zip(graph_paths, native_paths, strict=True))
        project_files = set(graph_paths)

        if report is not None:
            report.files_total = len(graph_paths)

        # ── Pass 1: collect symbols ───────────────────────────
        symbols_by_file: dict[str, list[Symbol]] = {}
        for graph_path in graph_paths:
            native_path = native_by_graph[graph_path]
            symbols = graph._collect_symbols_for_file(session, native_path, report=report)
            if symbols is not None:
                symbols_by_file[graph_path] = symbols

        if report is not None:
            report.files_indexed = len(symbols_by_file)

        # ── Pass 2: materialize all project nodes ─────────────
        for file_str, symbols in symbols_by_file.items():
            graph._materialize_file_nodes(session, file_str, symbols)

        # ── Pass 3: structural edges + range caches ───────────
        for file_str, symbols in symbols_by_file.items():
            graph._add_containment_edges_for_file(session, file_str, symbols)
            graph._build_range_cache_for_file(file_str)

        # ── Pass 4: semantic references ───────────────────────
        for file_str in symbols_by_file:
            graph._resolve_references_via_occurrences(
                session,
                file_str,
                project_files,
                report=report,
                root=root_resolved,
                native_by_graph=native_by_graph,
            )

        # ── Pass 5: inheritance — two passes (§6.4) ─────
        # Pass I: all INHERITS edges for all classes in the project
        graph._inherits_pass_I(
            session,
            symbols_by_file,
            report=report,
            root=root_resolved,
            native_by_graph=native_by_graph,
            project_files=project_files,
        )
        # Pass II: all OVERRIDES edges (BFS walks complete INHERITS chains)
        graph._overrides_pass_II(
            symbols_by_file,
            report=report,
            native_by_graph=native_by_graph,
        )

        # ── Pass 6: diagnostics (single check, distribute per-file)
        graph._collect_all_diagnostics(
            session,
            report=report,
            root=root_resolved,
            project_files=project_files,
        )

        graph._revision = getattr(session, "revision", getattr(session, "head", None))

        return graph

    @classmethod
    def build_with_report(
        cls,
        session: TyO3Session,
    ) -> tuple[CodeGraph, GraphBuildReport]:
        """Build a graph and return it alongdide a build report.

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
            logger.warning("Failed to get symbols for %s, skipping: %s", file_str, e)
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
        self,
        session,
        file_str: str,
        symbols: list[Symbol],
    ) -> None:
        """Create module and symbol nodes for one file in the graph.

        Two sub-passes: first top-level entities get their DurableId from
        ``session.id_for()``, then nested entities derive compound ids from
        the parent's DurableId — stable across moves and renames.
        """
        self._assert_mutable()
        module_id = make_module_durable_id(file_str)
        module_node = SymbolNode(
            durable_id=module_id,
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
            self._name_to_id[(file_str, "<module>")] = module_id

        # ── Sub-pass A: top-level entities (no container_name) ─────
        # Use session.id_for() to get the real DurableId from Gate 2.
        local_name_to_did: dict[str, str] = {}
        for symbol in symbols:
            if symbol.container_name:
                continue  # nested — deferred to pass B
            did = derive_durable_id(session, file_str, symbol)
            if did is None:
                continue
            local_name_to_did[symbol.name] = did
            if did not in self._id_to_index:
                qn = symbol.qualified_name or symbol.name
                node = SymbolNode(
                    durable_id=did,
                    name=symbol.name,
                    qualified_name=qn,
                    kind=symbol.kind,
                    file=file_str,
                    range=symbol.location.range,
                    selection_range=symbol.selection_range,
                )
                new_nodes.append(node)
                self._name_to_id[(file_str, qn)] = did
                self._name_to_id[(file_str, symbol.name)] = did

        # ── Sub-pass B: nested entities (methods, inner classes) ───
        # Compound id: parent_durable_id::qualified_name
        for symbol in symbols:
            if not symbol.container_name:
                continue  # already handled in pass A
            parent_did = local_name_to_did.get(symbol.container_name)
            if parent_did is None:
                # Parent not in this file — try cross-file lookup
                parent_did = self._find_symbol_in_file(file_str, symbol.container_name)
            did = derive_durable_id(session, file_str, symbol, parent_durable_id=parent_did)
            if did is not None and did not in self._id_to_index:
                qn = symbol.qualified_name or symbol.name
                node = SymbolNode(
                    durable_id=did,
                    name=symbol.name,
                    qualified_name=qn,
                    kind=symbol.kind,
                    file=file_str,
                    range=symbol.location.range,
                    selection_range=symbol.selection_range,
                )
                new_nodes.append(node)
                self._name_to_id[(file_str, qn)] = did
                self._name_to_id[(file_str, symbol.name)] = did

        if new_nodes:
            indices = self._graph.add_nodes_from(new_nodes)
            for node, idx in zip(new_nodes, indices, strict=True):
                self._id_to_index[node.durable_id] = idx
                self._file_to_nodes[node.file].append(idx)

    def _add_containment_edges_for_file(
        self,
        session,
        file_str: str,
        symbols: list[Symbol],
    ) -> None:
        """Add DEFINES/CONTAINS edges for all symbols in one file."""
        module_id = make_module_durable_id(file_str)
        for symbol in symbols:
            # For nested symbols, derive the compound id (parent::qualified_name)
            # to match the DurableId assigned in _materialize_file_nodes.
            parent_did = None
            if symbol.container_name:
                parent_did = self._find_symbol_in_file(file_str, symbol.container_name)
            did = derive_durable_id(session, file_str, symbol, parent_durable_id=parent_did)
            if did is None:
                continue
            parent_id = self._resolve_parent_id(file_str, symbol, module_id)
            if parent_id and parent_id in self._id_to_index:
                edge_kind = EdgeKind.DEFINES if parent_id == module_id else EdgeKind.CONTAINS
                edge = EdgeData(kind=edge_kind)
                self._add_edge(parent_id, did, edge, file_str)

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
                    node.durable_id,
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
            logger.warning("Failed to run project check, skipping diagnostics: %s", e)
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
                    _normalize_result_path(root, diagnostic.file, project_files)
                    if root is not None and project_files is not None
                    else diagnostic.file
                )
                self._diagnostics.setdefault(norm_file, []).append(diagnostic)

    def _refresh_diagnostics(
        self,
        session,
        *,
        root: Path | None = None,
        project_files: set[str] | None = None,
    ) -> None:
        """Clear and recompute all diagnostics from one project-wide check().

        apply_delta uses this instead of touching only dirty files: a change in one
        file can add/remove diagnostics in importers and dependents, so a partial
        update would be incorrect. One check() over the pinned source is consistent
        with the structural update's revision.
        """
        self._diagnostics.clear()
        self._collect_all_diagnostics(session, root=root, project_files=project_files)

    # ── Parent resolution for containment ─────────────────────

    def _resolve_parent_id(
        self,
        file_str: str,
        symbol: Symbol,
        module_id: str,
    ) -> str | None:
        """Determine the parent durable_id for containment edges.

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
        """Find a durable_id for *name* in *file_path*.

        Uses the ``_name_to_id`` map (populated during materialisation) as
        the primary lookup — this maps both qualified_name and short name to
        the DurableId. Falls back to node-name scan for pre-existing nodes
        from earlier builds.
        """
        # Primary: name_to_id map (populated during materialisation)
        cached = self._name_to_id.get((file_path, name))
        if cached is not None:
            return cached
        # Secondary: name_prefix_index (legacy, for @line ids)
        cached = self._name_prefix_index.get((file_path, name))
        if cached is not None:
            return cached
        # Fallback: search by short name within the file's nodes
        for idx in self._file_to_nodes.get(file_path, []):
            node: SymbolNode = self._graph[idx]
            if node.name == name or node.qualified_name == name:
                return node.durable_id
        return None

    # ── Reference resolution ──────────────────────────────────

    def _resolve_references_via_occurrences(
        self,
        session: TyO3Session,
        file_str: str,
        project_files: set[str],
        *,
        report: GraphBuildReport | None = None,
        root: Path | None = None,
        native_by_graph: dict[str, str] | None = None,
        restrict_targets: set[str] | None = None,
    ) -> None:
        """Resolve references using the batch file_occurrences API.

        Makes a single Rust call per file that resolves every name-like
        token to its definition target.  Replaces the per-token
        ``goto_definition`` approach with O(1) FFI calls.

        *project_files* distinguishes project-local targets (which
        already exist as nodes from Pass 2) from external targets
        (which need stub nodes).
        """
        native_file = native_by_graph.get(file_str, file_str) if native_by_graph else file_str
        try:
            occurrences = session.file_occurrences(native_file)
        except Exception as e:
            logger.warning(
                "Failed to get file occurrences for %s, falling back: %s",
                file_str,
                e,
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
            # Handle import occurrences that lack resolved targets.
            # The file_occurrences API reports import-role tokens but may not
            # resolve their definition targets (target_file is None). Fall back
            # to goto_definition for these to build IMPORTS edges.
            if occ.role == ReferenceRole.IMPORT and occ.target_file is None:
                self._resolve_import_via_goto(
                    session, native_file, occ.range, file_str,
                    project_files, root, native_by_graph,
                )
                continue

            if occ.target_file is None or occ.target_name is None:
                continue

            target_file_raw = occ.target_file
            target_file = (
                _normalize_result_path(root, target_file_raw, project_files) if root is not None else target_file_raw
            )

            # Phase 6 inbound revalidation: only (re-)add edges INTO the dirty set.
            if restrict_targets is not None and target_file not in restrict_targets:
                continue

            # Resolve the target to its DurableId via the name→id map.
            # The Rust engine returns file + name but not the DurableId,
            # so we look it up in the graph's name→id index.
            target_name = occ.target_name
            target_qn = occ.target_qualified_name

            # Primary: lookup by qualified_name, then by short name
            target_did = None
            if target_qn:
                target_did = self._find_symbol_in_file(target_file, target_qn)
            if target_did is None:
                target_did = self._find_symbol_in_file(target_file, target_name)

            # Ensure the target node exists — create a stub if external
            if target_did is None:
                if target_file in project_files:
                    # Same-file local variables and parameters are valid
                    # occurrence targets but are not graph nodes. Cross-file
                    # misses, however, indicate path or identity drift.
                    if target_file != file_str:
                        msg = (
                            "project-local occurrence target did not map to a graph node: "
                            f"{file_str} -> {target_file}::{target_qn or target_name}"
                        )
                        logger.warning(msg)
                        if report is not None:
                            report.failures.append(
                                GraphBuildFailure(
                                    file=file_str,
                                    phase="references",
                                    error_type="ProjectLocalTargetMismatch",
                                    message=msg,
                                )
                            )
                    else:
                        logger.debug(
                            "Skipping non-graph local occurrence target %s in %s",
                            target_qn or target_name,
                            target_file,
                        )
                    # Project-local but not found — identity mismatch, skip.
                    continue
                # External: create a stub node
                target_did = self._ensure_target_node_simple(
                    target_file,
                    f"{target_file}::{target_qn or target_name}",
                    target_name,
                    project_files,
                )
                if target_did is None:
                    continue

            # Determine the enclosing symbol at this occurrence's location
            enclosing_id = self._find_enclosing_symbol(file_str, occ.range)
            if enclosing_id is None:
                continue

            # Don't create self-references for definition sites
            if enclosing_id == target_did:
                continue

            role = occ.role  # Already a ReferenceRole

            # An import binding (`from models import User`) is a module-level
            # dependency, not a reference from the enclosing symbol to the
            # imported entity. Emit only the IMPORTS edge — never a REFERENCES
            # edge that would attach the imported symbol to the module node.
            if role == ReferenceRole.IMPORT:
                if target_file and target_file != file_str:
                    self._add_import_edge(file_str, target_file, occ.range, project_files)
                continue

            edge = EdgeData(
                kind=EdgeKind.REFERENCES,
                file=file_str,
                range=occ.range,
                role=role,
            )
            self._add_edge(enclosing_id, target_did, edge, file_str)

            # A resolved cross-file reference implies a module-level import
            # dependency; record it so the dependency graph is complete.
            if target_file and target_file != file_str:
                self._add_import_edge(file_str, target_file, occ.range, project_files)

    def _add_import_edge(
        self,
        source_file: str,
        target_file: str,
        range: Range,
        project_files: set[str],
    ) -> None:
        """Add a module-level IMPORTS edge between two files."""
        source_module = make_module_durable_id(source_file)
        target_module = make_module_durable_id(target_file)

        if source_module not in self._id_to_index:
            return

        if target_module not in self._id_to_index:
            if target_file in project_files:
                return  # Project file without a module node — skip
            package = self._infer_package(target_file) or "unknown"
            target_module = f"{package}::<module>"
            if target_module not in self._id_to_index:
                self._add_stub_node(
                    durable_id=target_module,
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

        # Phase 6: reverse-dependency bookkeeping. Only record project→project
        # imports; external targets (package::<module>) are never queried as dirty
        # files.
        if target_file in project_files and target_file != source_file:
            self._file_importers[target_file].add(source_file)

    def _resolve_import_via_goto(
        self,
        session,
        native_file: str,
        occ_range: Range,
        file_str: str,
        project_files: set[str],
        root: Path | None,
        native_by_graph: dict[str, str] | None,
    ) -> None:
        """Resolve an import occurrence by falling back to goto_definition.

        When file_occurrences reports an import with target_file=None, use
        goto_definition at the import position to find the target module.
        Creates an IMPORTS edge if the target is a project file.
        """
        try:
            targets = session.goto_definition(
                native_file, occ_range.start.line, occ_range.start.column
            )
        except Exception:
            return
        if not targets:
            return
        target = targets[0]
        target_file_raw = str(target.path)
        target_file = (
            _normalize_result_path(root, target_file_raw, project_files)
            if root is not None and project_files is not None
            else target_file_raw
        )
        if target_file and target_file != file_str:
            self._add_import_edge(file_str, target_file, occ_range, project_files)

    def _importers_of(self, files: set[str]) -> set[str]:
        """Graph paths of project files that import any file in *files*.

        Reads the reverse-dependency index built from IMPORTS edges. Used by
        apply_delta to find inbound cross-file edges that must be revalidated when
        *files* (the dirty set) change. Excludes the dirty files themselves — a file's
        own out-edges are rebuilt by re-indexing it, not by inbound revalidation.
        """
        importers: set[str] = set()
        for f in files:
            importers |= self._file_importers.get(f, set())
        return importers - files

    def _ensure_target_node_simple(
        self,
        target_file: str,
        target_did: str,
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
        ext_did = f"{package}::{target_name}" if package else target_did
        if ext_did not in self._id_to_index:
            self._add_stub_node(
                durable_id=ext_did,
                name=target_name,
                qualified_name=target_name,
                kind=SymbolKind.UNKNOWN,
                package=package or "unknown",
            )
        return ext_did

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
        native_file = native_by_graph.get(file_str, file_str) if native_by_graph else file_str
        try:
            tokens = session.semantic_tokens(native_file)
        except Exception as e:
            logger.warning("Failed to get semantic tokens for %s: %s", file_str, e)
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
                targets = session.goto_definition(native_file, start.line, start.column)
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

            # Resolve target by name lookup into the graph's name→id map
            target_did = None
            if target.symbol:
                if target.symbol.qualified_name:
                    target_did = self._find_symbol_in_file(target_file, target.symbol.qualified_name)
                if target_did is None:
                    target_did = self._find_symbol_in_file(target_file, target.symbol.name)

            if target_did is None:
                # No symbol info or not found — try legacy format
                if target.symbol and target.symbol.qualified_name:
                    target_did = f"{target_file}::{target.symbol.qualified_name}"
                elif target.symbol:
                    target_did = f"{target_file}::{target.symbol.name}@{target.range.start.line}"
                else:
                    continue

            # Ensure the target node exists — create a stub if external
            if target_did not in self._id_to_index:
                target_did = self._ensure_target_node(target_file, target_did, target)
                if target_did is None:
                    continue

            # Determine the enclosing symbol at this token's location
            enclosing_id = self._find_enclosing_symbol(file_str, token.range)
            if enclosing_id is None:
                continue

            # Don't create self-references for definition sites
            if enclosing_id == target_did:
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
            self._add_edge(enclosing_id, target_did, edge, file_str)

    def _ensure_target_node(
        self,
        target_file: str,
        target_did: str,
        target: Any,
    ) -> str | None:
        """Ensure a reference target exists as a node.

        If the target is external (not in project files), creates a stub
        node for it. Returns the effective durable_id to use for edge
        creation, or None if the target cannot be represented.
        """
        # Only create stubs for truly external paths
        if target_file not in self._file_to_nodes:
            package = self._infer_package(target_file)
            kind = SymbolKind.UNKNOWN
            name = target_did.split("::")[-1]
            qn = name
            if hasattr(target, "symbol") and target.symbol:
                kind = target.symbol.kind
                name = target.symbol.name
                qn = target.symbol.qualified_name or name

            ext_did = f"{package}::{qn}" if package else target_did
            self._add_stub_node(
                durable_id=ext_did,
                name=name,
                qualified_name=qn,
                kind=kind,
                package=package or "unknown",
            )
            return ext_did
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

    # ── Inheritance resolution (two-pass, §6.4) ───────────────

    def _inherits_pass_I(
        self,
        session: TyO3Session,
        symbols_by_file: dict[str, list[Symbol]],
        *,
        report: GraphBuildReport | None = None,
        root: Path | None = None,
        native_by_graph: dict[str, str] | None = None,
        project_files: set[str] | None = None,
    ) -> None:
        """Pass I: Add INHERITS edges for ALL classes in the dirty set.

        MUST complete for the entire set before _overrides_pass_II runs
        anywhere (§6.4). Every class in every file in the set gets its
        direct INHERITS edge to each supertype. After this pass, the
        INHERITS chain is complete for BFS walks.
        """
        for file_str, symbols in symbols_by_file.items():
            native_file = native_by_graph.get(file_str, file_str) if native_by_graph else file_str
            for symbol in symbols:
                if symbol.kind != SymbolKind.CLASS:
                    continue

                start = symbol.selection_range.start if symbol.selection_range else symbol.location.range.start
                try:
                    supertypes = session.class_supertypes(native_file, start.line, start.column)
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

                did = derive_durable_id(session, file_str, symbol)

                for supertype in supertypes:
                    super_file_raw = str(supertype.path)
                    super_file = (
                        _normalize_result_path(root, super_file_raw, project_files)
                        if root is not None and project_files is not None
                        else super_file_raw
                    )
                    super_did = self._find_symbol_in_file(super_file, supertype.name)

                    if super_did is None:
                        package = self._infer_package(super_file)
                        if package is None and super_file not in self._file_to_nodes:
                            package = "unknown"
                        if package:
                            ext_did = f"{package}::{supertype.name}"
                            super_did = ext_did
                            if ext_did not in self._id_to_index:
                                self._add_stub_node(
                                    durable_id=ext_did,
                                    name=supertype.name,
                                    qualified_name=(f"{package}.{supertype.name}"),
                                    kind=SymbolKind.CLASS,
                                    package=package,
                                )
                        else:
                            continue

                    edge = EdgeData(kind=EdgeKind.INHERITS)
                    self._add_edge(did, super_did, edge, file_str)

    def _overrides_pass_II(
        self,
        symbols_by_file: dict[str, list[Symbol]],
        *,
        report: GraphBuildReport | None = None,
        native_by_graph: dict[str, str] | None = None,
    ) -> None:
        """Pass II: Add OVERRIDES edges AFTER all INHERITS edges exist.

        Runs only after _inherits_pass_I has completed for the entire
        dirty set (§6.4). BFS-walks the now-complete INHERITS chain for
        each class to collect ancestor methods, then adds OVERRIDES edges
        for child methods that match ancestor methods by name.
        """
        for file_str, symbols in symbols_by_file.items():
            for symbol in symbols:
                if symbol.kind != SymbolKind.CLASS:
                    continue

                did = self._find_symbol_in_file(
                    file_str,
                    symbol.qualified_name or symbol.name,
                )
                if did is None:
                    continue

                # Collect methods from the child class
                child_methods = [
                    s for s in symbols
                    if s.kind in _METHOD_KINDS and s.container_name == symbol.name
                ]
                if not child_methods:
                    continue

                # BFS walk the INHERITS chain (all edges exist from Pass I)
                ancestor_methods: dict[str, str] = {}  # name -> durable_id
                visited: set[str] = {did}
                queue: deque[str] = deque([did])

                while queue:
                    current_did = queue.popleft()
                    current_idx = self._id_to_index.get(current_did)
                    if current_idx is None:
                        continue

                    for _src, succ_idx, edge_data in self._graph.out_edges(current_idx):
                        if edge_data.kind != EdgeKind.INHERITS:
                            continue

                        parent_did = self._graph[succ_idx].durable_id
                        if parent_did not in visited:
                            visited.add(parent_did)
                            queue.append(parent_did)

                        for c in self.children(parent_did):
                            if c.kind in _METHOD_KINDS and c.name not in ancestor_methods:
                                ancestor_methods[c.name] = c.durable_id

                for method in child_methods:
                    if method.name in ancestor_methods:
                        # Compound id: class_durable_id::qualified_name
                        qn = method.qualified_name or method.name
                        method_did = f"{did}::{qn}"
                        parent_method_did = ancestor_methods[method.name]
                        edge = EdgeData(kind=EdgeKind.OVERRIDES)
                        self._add_edge(method_did, parent_method_did, edge, file_str)

    def _find_enclosing_symbol(self, file_str: str, range: Range) -> str | None:
        """Find the innermost symbol in *file_str* that contains *range*.

        Uses the pre-materialized range cache (B1) to avoid per-node
        FFI calls.  The cache is sorted by range size ascending, so the
        first containment match is the smallest enclosing symbol.

        Returns the durable_id, or the module node if no enclosing symbol
        is found.
        """
        cached = self._file_node_ranges.get(file_str, [])
        for start_line, start_col, end_line, end_col, did in cached:
            # Check containment: node range must fully contain the target range
            if (start_line, start_col) <= (
                range.start.line,
                range.start.column,
            ) and (end_line, end_col) >= (range.end.line, range.end.column):
                # First match is smallest due to sort order
                return did

        # Fall back to module node
        module_id = make_module_durable_id(file_str)
        if module_id in self._id_to_index:
            return module_id
        return None

    # ── Internal graph mutation ───────────────────────────────

    def _add_node(self, node: SymbolNode) -> int:
        """Add a SymbolNode to the graph and update indexes."""
        self._assert_mutable()
        if node.durable_id in self._id_to_index:
            return self._id_to_index[node.durable_id]
        idx = self._graph.add_node(node)
        self._id_to_index[node.durable_id] = idx
        self._file_to_nodes[node.file].append(idx)
        # Name→id map for cross-reference resolution
        if node.qualified_name:
            self._name_to_id[(node.file, node.qualified_name)] = node.durable_id
        self._name_to_id[(node.file, node.name)] = node.durable_id
        # Index for fast name@line lookups (B4)
        if "@" in node.durable_id:
            parts = node.durable_id.split("::", 1)
            if len(parts) == 2:
                name_part = parts[1].split("@", 1)[0]
                self._name_prefix_index[(parts[0], name_part)] = node.durable_id
        self._semantic_subgraph_cache.clear()
        return idx

    def _add_edge(
        self,
        source_id: str,
        target_id: str,
        data: EdgeData,
        file: str,
    ) -> int | None:
        """Add an edge between two symbols by their IDs."""
        self._assert_mutable()
        src_idx = self._id_to_index.get(source_id)
        tgt_idx = self._id_to_index.get(target_id)
        if src_idx is None or tgt_idx is None:
            return None
        edge_idx = self._graph.add_edge(src_idx, tgt_idx, data)
        self._file_to_edges[file].append(edge_idx)
        self._semantic_subgraph_cache.clear()
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
        self._name_to_id.clear()
        self._name_prefix_index.clear()
        self._file_importers.clear()
        self._semantic_subgraph_cache.clear()
        for idx in self._graph.node_indices():
            node: SymbolNode = self._graph[idx]
            self._id_to_index[node.durable_id] = idx
            self._file_to_nodes[node.file].append(idx)
            # Rebuild name→id map
            if node.qualified_name:
                self._name_to_id[(node.file, node.qualified_name)] = node.durable_id
            self._name_to_id[(node.file, node.name)] = node.durable_id
            # Rebuild name_prefix_index (B4)
            if "@" in node.durable_id:
                parts = node.durable_id.split("::", 1)
                if len(parts) == 2:
                    name_part = parts[1].split("@", 1)[0]
                    self._name_prefix_index[(parts[0], name_part)] = node.durable_id
        for edge_idx in self._graph.edge_indices():
            data = self._graph.get_edge_data_by_index(edge_idx)
            if data is not None and hasattr(data, "file") and data.file is not None:
                self._file_to_edges[data.file].append(edge_idx)
            if data is not None and data.kind == EdgeKind.IMPORTS:
                src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
                src_file = self._graph[src].file
                tgt_file = self._graph[tgt].file
                if tgt_file != "<external>" and src_file != "<external>" and tgt_file != src_file:
                    self._file_importers[tgt_file].add(src_file)
        # Rebuild range cache (B1)
        for file_str in self._file_to_nodes:
            self._file_node_ranges[file_str] = sorted(
                [
                    (
                        node.range.start.line,
                        node.range.start.column,
                        node.range.end.line,
                        node.range.end.column,
                        node.durable_id,
                    )
                    for idx in self._file_to_nodes[file_str]
                    for node in [self._graph[idx]]
                    if node.kind != SymbolKind.MODULE
                ],
                key=_range_size,
            )

    def _assert_mutable(self) -> None:
        """Reject structural mutations on a revision-pinned graph."""
        if self._frozen:
            raise RuntimeError("Cannot mutate a revision-pinned CodeGraph")

    def _pin_at(self, revision: int) -> CodeGraph:
        """Return an immutable copy of this graph pinned at *revision*."""
        pinned = CodeGraph()
        pinned._graph = self._graph.copy()
        pinned._id_to_index = dict(self._id_to_index)
        pinned._file_to_nodes = defaultdict(list, {k: list(v) for k, v in self._file_to_nodes.items()})
        pinned._file_to_edges = defaultdict(list, {k: list(v) for k, v in self._file_to_edges.items()})
        pinned._file_importers = defaultdict(set, {k: set(v) for k, v in self._file_importers.items()})
        pinned._root = self._root
        pinned._file_node_ranges = {k: list(v) for k, v in self._file_node_ranges.items()}
        pinned._name_to_id = dict(self._name_to_id)
        pinned._name_prefix_index = dict(self._name_prefix_index)
        pinned._diagnostics = {k: list(v) for k, v in self._diagnostics.items()}
        pinned._dependency_cache = dict(self._dependency_cache)
        pinned._semantic_subgraph_cache = {}
        pinned._revision = revision
        pinned._frozen = True
        return pinned

    @staticmethod
    def _range_key(range_: Range | None) -> tuple[int, int, int, int] | None:
        if range_ is None:
            return None
        return (
            range_.start.line,
            range_.start.column,
            range_.end.line,
            range_.end.column,
        )

    def _edge_map(self) -> dict[tuple[Any, ...], EdgeDiff]:
        edges: dict[tuple[Any, ...], EdgeDiff] = {}
        counts: dict[tuple[Any, ...], int] = defaultdict(int)
        for edge_idx in self._graph.edge_indices():
            data = self._graph.get_edge_data_by_index(edge_idx)
            src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
            source_id = self._graph[src].durable_id
            target_id = self._graph[tgt].durable_id
            base_key = (
                source_id,
                target_id,
                data.kind,
                data.file,
                self._range_key(data.range),
                data.role,
            )
            ordinal = counts[base_key]
            counts[base_key] += 1
            key = (*base_key, ordinal)
            edges[key] = EdgeDiff(source_id, target_id, data)
        return edges

    def diff(self, before: CodeGraph) -> GraphDiff:
        """Return the structural graph delta from *before* to ``self``."""
        before_nodes = {before._graph[idx].durable_id: before._graph[idx] for idx in before._graph.node_indices()}
        after_nodes = {self._graph[idx].durable_id: self._graph[idx] for idx in self._graph.node_indices()}

        before_edges = before._edge_map()
        after_edges = self._edge_map()

        return GraphDiff(
            added_nodes=[after_nodes[did] for did in sorted(after_nodes.keys() - before_nodes.keys())],
            removed_nodes=[before_nodes[did] for did in sorted(before_nodes.keys() - after_nodes.keys())],
            added_edges=[after_edges[key] for key in sorted(after_edges.keys() - before_edges.keys(), key=repr)],
            removed_edges=[before_edges[key] for key in sorted(before_edges.keys() - after_edges.keys(), key=repr)],
        )

    # ── Incremental update helpers (Phase 6) ──────────────────

    def _index_files(
        self,
        session,
        graph_paths: list[str],
        project_files: set[str],
        *,
        root: Path,
        native_by_graph: dict[str, str],
        report: GraphBuildReport | None = None,
    ) -> None:
        """Run passes 1–5 over *graph_paths* (a subset of the project).

        Mirrors CodeGraph.build's pass ordering for a subset: collect symbols for all
        of them, materialize all their nodes, then structural edges + range caches,
        then references, then inheritance. Keeping the sub-pass split (rather than a
        per-file loop) is what lets two simultaneously-changed files that reference
        each other resolve correctly — every dirty node exists before any reference is
        resolved. Targets in *non*-dirty files already exist in the graph (they were
        never removed), so cross-file references out of the dirty set resolve too.
        """
        symbols_by_file: dict[str, list[Symbol]] = {}
        for graph_path in graph_paths:
            native_path = native_by_graph.get(graph_path, graph_path)
            symbols = self._collect_symbols_for_file(session, native_path, report=report)
            if symbols is not None:
                symbols_by_file[graph_path] = symbols

        for file_str, symbols in symbols_by_file.items():
            self._materialize_file_nodes(session, file_str, symbols)

        for file_str, symbols in symbols_by_file.items():
            self._add_containment_edges_for_file(session, file_str, symbols)
            self._build_range_cache_for_file(file_str)

        for file_str in symbols_by_file:
            self._resolve_references_via_occurrences(
                session,
                file_str,
                project_files,
                report=report,
                root=root,
                native_by_graph=native_by_graph,
            )

        # Inheritance — two passes (§6.4)
        self._inherits_pass_I(
            session,
            symbols_by_file,
            report=report,
            root=root,
            native_by_graph=native_by_graph,
            project_files=project_files,
        )
        self._overrides_pass_II(
            symbols_by_file,
            report=report,
            native_by_graph=native_by_graph,
        )

    def _add_stub_node(
        self,
        durable_id: str,
        name: str,
        qualified_name: str,
        kind: SymbolKind,
        package: str,
    ) -> int:
        """Add a stub node for an external symbol."""
        node = SymbolNode(
            durable_id=durable_id,
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
        durable_id: str,
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
        all parallel edges in a single efficient Rust-dide call.
        """
        idx = self._id_to_index.get(durable_id)
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
        if self._frozen:
            return self._graph.copy()
        return self._graph

    @property
    def revision(self) -> int | None:
        """The application revision this graph describes, if known."""
        return self._revision

    @property
    def frozen(self) -> bool:
        """Whether this graph is pinned and structurally immutable."""
        return self._frozen

    @property
    def node_count(self) -> int:
        return self._graph.num_nodes()

    @property
    def edge_count(self) -> int:
        return self._graph.num_edges()

    # ── Symbol queries ────────────────────────────────────────

    def symbol(self, durable_id: str) -> SymbolNode | None:
        """Look up a symbol by its canonical ID."""
        idx = self._id_to_index.get(durable_id)
        return self._graph[idx] if idx is not None else None

    def symbols_in_file(self, path: str) -> list[SymbolNode]:
        """All symbols defined in a file."""
        return [self._graph[i] for i in self._file_to_nodes.get(path, [])]

    def symbols_of_kind(self, kind: SymbolKind) -> list[SymbolNode]:
        """All symbols of a given kind."""
        result: list[SymbolNode] = []
        for i in self._graph.node_indices():
            node = self._graph[i]
            if node.kind == kind:
                result.append(node)
        return result

    # ── Reference queries ─────────────────────────────────────

    def references_to(self, durable_id: str) -> list[EdgeData]:
        """All incoming REFERENCES edges to a symbol."""
        return [data for _, data in self._edges_of_kind(durable_id, {EdgeKind.REFERENCES}, incoming=True)]

    def references_from(self, durable_id: str) -> list[tuple[SymbolNode, EdgeData]]:
        """All outgoing REFERENCES edges from a symbol."""
        return [(self._graph[tgt_idx], data) for tgt_idx, data in self._edges_of_kind(durable_id, {EdgeKind.REFERENCES})]

    # ── Structural queries ────────────────────────────────────

    def children(self, durable_id: str) -> list[SymbolNode]:
        """Direct children (outgoing DEFINES/CONTAINS edges)."""
        seen: set[int] = set()
        result: list[SymbolNode] = []
        for tgt_idx, _ in self._edges_of_kind(durable_id, {EdgeKind.DEFINES, EdgeKind.CONTAINS}):
            if tgt_idx not in seen:
                seen.add(tgt_idx)
                result.append(self._graph[tgt_idx])
        return result

    def parent(self, durable_id: str) -> SymbolNode | None:
        """Enclosing symbol (incoming DEFINES/CONTAINS edge)."""
        edges = self._edges_of_kind(
            durable_id,
            {EdgeKind.DEFINES, EdgeKind.CONTAINS},
            incoming=True,
        )
        if edges:
            return self._graph[edges[0][0]]
        return None

    def module_for(self, durable_id: str) -> SymbolNode | None:
        """The MODULE node for this symbol's file."""
        # Look up the node to find its file (more robust than parsing the id)
        node = self.symbol(durable_id)
        if node is not None:
            module_id = make_module_durable_id(node.file)
            return self.symbol(module_id)
        # Fallback: try extracting file from the id text
        file = file_from_durable_id(durable_id)
        module_id = make_module_durable_id(file)
        return self.symbol(module_id)

    # ── Dependency analysis ───────────────────────────────────

    def _semantic_subgraph(self, kinds: frozenset[EdgeKind]) -> rx.PyDiGraph:
        """Build a subgraph containing only edges of the given kinds.

        Node indices are preserved (same as the main graph) so callers can
        use ``self._id_to_index`` for lookups.

        Results are memoized per *kinds* set and invalidated on any graph
        mutation (node/edge addition, index rebuild, full rebuild).
        """
        cached = self._semantic_subgraph_cache.get(kinds)
        if cached is not None:
            return cached
        sub = self._graph.copy()
        to_remove = [
            sub.get_edge_endpoints_by_index(edge_idx)
            for edge_idx in sub.edge_indices()
            if sub.get_edge_data_by_index(edge_idx).kind not in kinds
        ]
        sub.remove_edges_from(to_remove)
        self._semantic_subgraph_cache[kinds] = sub
        return sub

    def dependencies(
        self,
        durable_id: str,
        *,
        kinds: frozenset[EdgeKind] | None = None,
    ) -> set[str]:
        """All symbols this one directly depends on (semantic edges only)."""
        edge_kinds = DEPENDENCY_EDGE_KINDS if kinds is None else kinds
        return {self._graph[tgt_idx].durable_id for tgt_idx, _data in self._edges_of_kind(durable_id, edge_kinds)}

    def dependents(
        self,
        durable_id: str,
        *,
        kinds: frozenset[EdgeKind] | None = None,
    ) -> set[str]:
        """All symbols that directly depend on this one (semantic edges only)."""
        edge_kinds = DEPENDENCY_EDGE_KINDS if kinds is None else kinds
        return {
            self._graph[src_idx].durable_id
            for src_idx, _data in self._edges_of_kind(durable_id, edge_kinds, incoming=True)
        }

    def transitive_dependencies(
        self,
        durable_id: str,
        *,
        kinds: frozenset[EdgeKind] | None = None,
    ) -> set[str]:
        """All symbols reachable via semantic edges from this one."""
        edge_kinds = DEPENDENCY_EDGE_KINDS if kinds is None else kinds
        filtered = self._semantic_subgraph(edge_kinds)
        idx = self._id_to_index.get(durable_id)
        if idx is None:
            return set()
        # filtered uses the same indices as the main graph
        reachable = rx.descendants(filtered, idx)
        return {self._graph[i].durable_id for i in reachable}

    def transitive_dependents(
        self,
        durable_id: str,
        *,
        kinds: frozenset[EdgeKind] | None = None,
    ) -> set[str]:
        """All symbols that transitively depend on this one (semantic edges only)."""
        edge_kinds = DEPENDENCY_EDGE_KINDS if kinds is None else kinds
        filtered = self._semantic_subgraph(edge_kinds)
        idx = self._id_to_index.get(durable_id)
        if idx is None:
            return set()
        # filtered uses the same indices as the main graph
        reachable = rx.ancestors(filtered, idx)
        return {self._graph[i].durable_id for i in reachable}

    # ── Graph algorithms ──────────────────────────────────────

    def _build_module_graph(self) -> tuple[rx.PyDiGraph, dict[str, int]]:
        """Build a module-level graph from IMPORTS edges.

        Returns ``(module_graph, did_to_index)`` where *module_graph*
        nodes are module ``durable_id`` strings and *did_to_index*
        maps ``durable_id`` → node index in the module graph.
        """
        module_indices = [i for i in self._graph.node_indices() if self._graph[i].kind == SymbolKind.MODULE]
        if len(module_indices) < 2:
            return rx.PyDiGraph(), {}

        mod_graph = rx.PyDiGraph()
        did_to_midx: dict[str, int] = {}
        for i in module_indices:
            did = self._graph[i].durable_id
            midx = mod_graph.add_node(did)
            did_to_midx[did] = midx

        # Map every node to its module for aggregation
        node_to_module: dict[int, str] = {}
        for mi in module_indices:
            module_did = self._graph[mi].durable_id
            file = file_from_durable_id(module_did)
            for ni in self._file_to_nodes.get(file, []):
                node_to_module[ni] = module_did

        for edge_idx in self._graph.edge_indices():
            data = self._graph.get_edge_data_by_index(edge_idx)
            if getattr(data, "kind", None) != EdgeKind.IMPORTS:
                continue
            src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
            src_mod = node_to_module.get(src)
            tgt_mod = node_to_module.get(tgt)
            if src_mod and tgt_mod and src_mod != tgt_mod:
                mi_src = did_to_midx.get(src_mod)
                mi_tgt = did_to_midx.get(tgt_mod)
                if mi_src is not None and mi_tgt is not None:
                    mod_graph.add_edge(mi_src, mi_tgt, None)

        return mod_graph, did_to_midx

    def import_cycles(self) -> list[list[str]]:
        """Detect circular import chains in the module dependency graph.

        Builds a module-level dependency graph from IMPORTS edges only.
        Two modules are adjacent if one imports the other.

        Returns a list of cycles, where each cycle is a list of
        module durable_ids in order.
        """
        mod_graph, _did_to_midx = self._build_module_graph()
        if mod_graph.num_nodes() < 2:
            return []

        # Build adjacency dict from the module graph
        adj: dict[str, set[str]] = {mod_graph[i]: set() for i in mod_graph.node_indices()}

        for edge_idx in mod_graph.edge_indices():
            src, tgt = mod_graph.get_edge_endpoints_by_index(edge_idx)
            adj[mod_graph[src]].add(mod_graph[tgt])

        # DFS-based cycle detection
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {did: WHITE for did in adj}
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

        for did in adj:
            if color[did] == WHITE:
                dfs(did, [], set())

        return cycles

    def hub_symbols(self, top_n: int = 10) -> list[tuple[str, float]]:
        """Find "hub" symbols using betweenness centrality.

        Hub symbols are those that bridge disconnected parts of
        the graph — they sit on many shortest paths between
        other symbols. High centrality often indicates a symbol
        that would cause widespread breakage if changed.

        Computes betweenness centrality over the full directed
        graph and returns the top *top_n* (durable_id, score)
        pairs, sorted descending by score.
        """
        try:
            # rustworkx betweenness_centrality returns a dict-like
            # mapping node_index -> float
            centrality = rx.betweenness_centrality(self._graph)
        except Exception:
            return []

        scored = [(self._graph[i].durable_id, score) for i, score in centrality.items() if score > 0.0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]

    def import_cycle_groups(self) -> list[set[str]]:
        """Groups of mutually-dependent modules (strongly connected components).

        Unlike :meth:`import_cycles`, which enumerates every cycle path,
        this returns connected components in the module dependency graph —
        each group contains all modules that are reachable from each other
        through import edges.

        Returns a list of sets of module durable_ids (one set per SCC
        with more than one module).  Singles (files with no cross-file
        deps) are excluded.
        """
        mod_graph, _did_to_midx = self._build_module_graph()
        if mod_graph.num_nodes() < 2:
            return []

        sccs = rx.strongly_connected_components(mod_graph)
        return [{mod_graph[i] for i in scc} for scc in sccs if len(scc) > 1]

    def is_reachable(self, from_did: str, to_did: str) -> bool:
        """Check if there is a directed path from one symbol to another.

        Uses RustworkX's ``has_path`` which performs BFS/DFS to determine
        connectivity.
        """
        src = self._id_to_index.get(from_did)
        tgt = self._id_to_index.get(to_did)
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
        return [self._graph[i].durable_id for i in order]

    def subgraph_for_file(self, file_path: str) -> rx.PyDiGraph:
        """Extract a subgraph containing all symbols defined in a file
        and their immediate reference neighbours (within the project).

        Uses ``subgraph_with_nodemap`` (B2) for a single Rust call that
        copies nodes and all edges between included nodes, replacing the
        O(E_total) edge scan with Rust-dide filtering.

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
        sub, _node_map = self._graph.subgraph_with_nodemap(sorted(included))
        return sub

    # ── Incremental updates ───────────────────────────────────

    def _replace_with(self, fresh: CodeGraph) -> None:
        """Replace all internal state with *fresh*'s (used by rescan / rebuild)."""
        self._assert_mutable()
        self.__dict__.update(fresh.__dict__)
        self._frozen = False

    def rebuild(self, session: TyO3Session, path: str) -> None:
        """Full rebuild (the rescan fallback). *path* is accepted for API
        compatibility but unused; prefer apply_delta for incremental updates."""
        self._assert_mutable()
        self._replace_with(CodeGraph.build(session))

    def _handle_moved_entities(
        self,
        source,
        moved: set[str],
        root: Path,
        native_by_graph: dict[str, str],
    ) -> None:
        """Update location payloads for moved entities (§6.3).

        Moved entities keep the same DurableId but have a new location.
        Update their file and range fields without dropping/re-adding nodes,
        preserving out-edges and avoiding unnecessary edge churn.
        """
        if not moved:
            return
        for file_str in moved:
            native_path = native_by_graph.get(file_str, file_str)
            try:
                symbols = source.document_symbols(native_path)
            except Exception:
                continue
            for symbol in symbols:
                start = (symbol.selection_range or symbol.location.range).start
                # Resolve the DurableId for this entity
                durable_id = source.id_for(native_path, start.line, start.column) if hasattr(source, 'id_for') else None
                if durable_id is None:
                    continue
                # Find the existing node by DurableId
                idx = self._id_to_index.get(durable_id)
                if idx is None:
                    continue
                node: SymbolNode = self._graph[idx]
                if node.file == file_str:
                    continue  # Already at the right location
                # Update location — create a new node with updated fields and
                # replace in-place using rustworkx's node substitution.
                updated = SymbolNode(
                    durable_id=node.durable_id,
                    name=symbol.name,
                    qualified_name=symbol.qualified_name or symbol.name,
                    kind=symbol.kind,
                    file=file_str,
                    range=symbol.location.range,
                    selection_range=symbol.selection_range,
                    external=node.external,
                    package=node.package,
                )
                self._graph[idx] = updated
                # Update file→node index
                old_file = node.file
                if old_file in self._file_to_nodes and idx in self._file_to_nodes[old_file]:
                    self._file_to_nodes[old_file].remove(idx)
                self._file_to_nodes[file_str].append(idx)

    def apply_delta(
        self,
        source,
        delta: SyncResult,
        *,
        report: GraphBuildReport | None = None,
    ) -> None:
        """Incrementally update the HEAD graph from a write's SyncResult.

        *source* is a consistent read surface for ``delta.revision`` — pass the
        ``Snapshot`` pinned at that revision (``session.snapshot()`` right after the
        edit) for true MVCC consistency; a ``TyO3Session`` also works ("latest", fine
        because nothing mutates HEAD during one apply_delta).

        Drops nodes owned by changed+deleted files, re-indexes created+changed files,
        and revalidates inbound cross-file edges into the changed set. On a rescan
        (``delta.rescan``) the delta is unknown, so it full-rebuilds. The result is
        structurally equal to ``CodeGraph.build(source)`` over the same revision.

        Requires ``self._root`` (set by build). Build the graph once with
        ``CodeGraph.build`` before applying deltas.
        """
        self._assert_mutable()
        if self._root is None:
            raise RuntimeError("apply_delta requires a graph built via CodeGraph.build()")

        # Rescan: delta unknown ⇒ rebuild wholesale (architecture §5.1).
        if delta.rescan:
            self._replace_with(CodeGraph.build(source, report=report, root=self._root))
            self._revision = delta.revision
            return

        root = self._root
        # Project-wide path maps from the CURRENT file set (includes created files,
        # excludes deleted ones). Native (absolute) paths feed read calls; graph paths
        # are project-relative. Mirrors build (graph.py:142-146).
        native_paths = [str(p) for p in source.files()]
        native_by_graph = {_to_relative(root, p): p for p in native_paths}
        project_files = set(native_by_graph.keys())

        created = {_to_relative(root, p) for p in delta.created}
        changed = {_to_relative(root, p) for p in delta.changed}
        deleted = {_to_relative(root, p) for p in delta.deleted}

        dirty = changed | deleted  # nodes to drop
        to_index = sorted(created | changed)  # files to (re-)extract

        # 0a. Handle moved entities: update location payload without changing
        #     the DurableId or churning edges — preserves §5.5.1 at the graph
        #     level and avoids needless edge churn.
        moved = {_to_relative(root, p) for p in delta.moved}
        self._handle_moved_entities(source, moved, root, native_by_graph)

        # 0b. Snapshot inbound dependencies BEFORE removal — step 1 deletes the edges
        #    that encode them (they are incident to dirty nodes).
        importers = self._importers_of(dirty)
        importers -= set(to_index)  # re-indexed files rebuild their own out-edges
        importers &= project_files  # only files that still exist

        # 1. Drop every node owned by a dirty file. rustworkx removes incident edges
        #    (including inbound cross-file edges) automatically.
        doomed = [idx for f in dirty for idx in self._file_to_nodes.get(f, [])]
        if doomed:
            self._graph.remove_nodes_from(doomed)
        # swap-and-pop invalidated stored indices: rebuild every secondary index
        # (incl. _file_importers) from the surviving graph.
        self._rebuild_indexes()

        # 2. Re-extract created + changed files (passes 1–5).
        self._index_files(
            source,
            to_index,
            project_files,
            root=root,
            native_by_graph=native_by_graph,
            report=report,
        )

        # 3. Revalidate INBOUND edges: re-resolve each importer's references INTO the
        #    dirty set only (its edges into non-dirty files survived step 1 untouched).
        for importer in sorted(importers):
            self._resolve_references_via_occurrences(
                source,
                importer,
                project_files,
                report=report,
                root=root,
                native_by_graph=native_by_graph,
                restrict_targets=dirty,
            )

        # 4. Diagnostics: one project-wide check(), redistributed. A change in one file
        #    can alter diagnostics in others, so refresh wholesale (architecture §5 the
        #    snapshot's check() is the same revision as the structural update).
        self._refresh_diagnostics(source, root=root, project_files=project_files)

        # Drop stale memoized subgraphs (defensive; _add_* already clear it).
        self._semantic_subgraph_cache.clear()
        self._revision = delta.revision

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

    def resolve_external(self, durable_id: str) -> SymbolNode | None:
        """Resolve an external symbol by loading its dependency graph.

        If the symbol is external and its dependency graph is cached
        on disk, loads it and returns the fully-resolved node.
        Otherwise returns the stub node as-is.
        """
        node = self.symbol(durable_id)
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

        resolved = dep.lookup(durable_id)
        return resolved if resolved is not None else node

    # ── Diagnostics ───────────────────────────────────────────

    def diagnostics_for_file(self, path: str) -> list[Diagnostic]:
        """All diagnostics for a file."""
        return self._diagnostics.get(path, [])

    def diagnostics_for_symbol(self, durable_id: str) -> list[Diagnostic]:
        """Diagnostics whose range overlaps this symbol's definition."""
        node = self.symbol(durable_id)
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
