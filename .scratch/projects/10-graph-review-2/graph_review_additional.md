TyO3 Code Review

What TyO3 Is

TyO3 is a Python semantic engine that exposes Astral's ty type-checker (the Ruff/ty family of crates) as a Python
library through PyO3. It gives Python programs programmatic access to IDE-grade features — type checking,
go-to-definition, find references, hover information, semantic tokens, type hierarchy, and file occurrences — against
real Python projects. On top of that raw semantic layer, it builds a CodeGraph: a rustworkx-backed directed graph of
symbols, containment, references, inheritance, and overrides, enabling dependency analysis, cycle detection, coupling
metrics, and centrality queries.

The architecture is three layers:
1. Rust core (rust/src/): A PyO3 extension module wrapping ty_project::ProjectDatabase, with DTO types exposed as frozen
#[pyclass] structs and a custom #[derive(PyFields)] proc macro for efficient Rust-to-Python conversion.
2. Python bridge (rust_project.py): Translates native PyO3 DTOs to Pydantic models via a recursive _to_python()
converter.
3. Python domain (session.py, models/, graph/): A clean session API, Pydantic domain models, and the CodeGraph
intelligence layer.

This is an ambitious and well-structured project. The design shows real sophistication in how it handles the Rust/Python
boundary. That said, there are several issues ranging from a critical logic bug to performance concerns and API design
rough edges.

---
Critical Bug

Dead code in _resolve_inheritance (graph.py:563-573)

This is the most serious issue in the codebase. There is an indentation error that makes the OVERRIDES detection
completely non-functional:

# graph.py:562-573
for _src, succ_idx, edge_data in self._graph.out_edges(self._id_to_index[current_sid]):
if edge_data.kind != EdgeKind.INHERITS:
    continue
    parent_sid = self._graph[succ_idx].symbol_id  # DEAD CODE
    if parent_sid not in visited:                   # DEAD CODE
        visited.add(parent_sid)                     # DEAD CODE
        queue.append(parent_sid)                    # DEAD CODE
    # Collect this parent's methods                 # DEAD CODE
    for c in self.children(parent_sid):             # DEAD CODE
        if c.kind in METHOD_KINDS and c.name not in ancestor_methods:
            ancestor_methods[c.name] = c.symbol_id  # DEAD CODE

The continue on line 565 causes all the code below it (lines 566-573) to be unreachable. The intent was clearly to skip
non-INHERITS edges and process INHERITS edges, but the logic is inverted — continue fires when the edge isn't INHERITS,
and then the code that should run for INHERITS edges comes after the continue. The fix is to invert the condition:

if edge_data.kind == EdgeKind.INHERITS:
parent_sid = self._graph[succ_idx].symbol_id
...

This means ancestor_methods is always empty, so the OVERRIDES edges are never created. Any test that verifies override
detection would have to be passing vacuously.

---
Architectural Concerns

1. Double serialization at the Rust/Python boundary

Every API call goes through this pipeline:
ty_ide result → Rust DTO (PyO3 frozen struct) → _to_python() recursive dict conversion → Pydantic model_validate()

The _to_python() function (rust_project.py:128-163) traverses every field of every PyO3 object recursively, building
Python dicts, then Pydantic parses those dicts again with its own validation logic. For large results (hundreds of
diagnostics, thousands of semantic tokens, workspace symbol searches), this is doing two full traversals where one
should suffice.

The PyFields derive macro and type-tag system is clever — it generates __fields__ metadata so _to_python() can skip
recursion on primitives. But the fundamental issue remains: you're materializing an intermediate dict that Pydantic
immediately re-parses. A more efficient approach would be to either:
- Implement __getattr__ on the PyO3 types so Pydantic's from_attributes=True works directly (you already have
ConfigDict(from_attributes=True) on all models but can't use it because PyO3 frozen structs lack __dict__)
- Or skip Pydantic validation for trusted Rust output and construct models directly

2. check_file runs a full project check

project.rs:279-308: check_file calls state.db.check() (the full project check) then filters diagnostics in Rust. The
comment says "Salsa-cached if unchanged" which is true for the second call, but the first check_file call for any
session will type-check the entire project just to return diagnostics for one file. The API name check_file implies it
would be cheaper than check, but it isn't.

3. CodeGraph.build() does N+2 Rust calls per file

For each file, _index_file makes:
- 1 document_symbols() call
- 1 file_occurrences() call
- 1 check_file() call (which internally does a full project check)
- 1 type_hierarchy() call per class symbol

The check_file call per file is particularly wasteful since each one triggers a full project check. A single check()
call followed by client-side filtering would be dramatically faster.

4. The Mutex in PyTyProject holds across all operations

project.rs:34-36: The Mutex<Option<TyProjectState>> is held for the duration of every operation (check,
document_symbols, etc.). Since these are CPU-bound operations that can take hundreds of milliseconds, this means the
entire project is serialized — no concurrent queries are possible from multiple Python threads. Given that
ProjectDatabase itself uses Salsa (which has its own internal concurrency story), the outer Mutex may be overly
conservative.

---
Code Quality Issues

5. Unused warnings import pattern

rust_project.py:497-512: The __del__ method on RustProject issues a ResourceWarning if the project isn't closed. This is
good defensive coding, but the ResourceWarning is suppressed by default in Python. More importantly, __del__ is not
guaranteed to run (and won't during interpreter shutdown), so this is unreliable as a safety net.

6. _convert_struct_fallback uses dir() introspection

rust_project.py:195-207: This fallback path iterates over dir(obj) and catches all exceptions silently. While it's
documented as a fallback for structs without __fields__, the except Exception: pass means any bug in attribute access is
silently swallowed.

7. Enum registry in lib.rs uses wrong names

lib.rs:53-61: The enum type registry references names like "NativeSymbolKind" via m.getattr("NativeSymbolKind"), but the
pyclass names are actually things like NativeSymbolKind (from #[pyclass(name = "NativeSymbolKind")]). However, looking
more carefully, I see the #[pyclass(eq, name = "NativeSeverity"...)] pattern — the names used in getattr must match
exactly. The problem is that these names are not the same as the Rust type names (e.g., SeverityDto vs NativeSeverity),
so this is a maintenance trap. If someone changes the name attribute in the pyclass annotation, the registry silently
breaks.

8. SeverityDto::Hint has no Rust-side mapping

dto/diagnostics.rs:14: SeverityDto defines a Hint variant, but convert/diagnostics.rs:111-118 maps
ruff_db::diagnostic::Severity, which only has Fatal/Error/Warning/Info. The Hint variant is unreachable from the Rust
conversion code, meaning it exists only for hypothetical future use or Python-side construction.

9. SymbolKindDto::Unknown has no Rust-side mapping

dto/symbols.rs:23: SymbolKindDto includes an Unknown variant that convert/symbols.rs:7-22 never produces. The match
statement on ty_ide::SymbolKind covers all known variants. Unknown is only used for external stub nodes created by the
Python graph module. This isn't a bug per se, but it means the Rust DTO carries a variant that the Rust code never
produces.

10. Serde rename on SymbolKindDto is inconsistent

dto/symbols.rs:10-11:
#[serde(rename_all = "snake_case")]
// ...
#[serde(rename = "class_")]
Class,
// ...
#[serde(rename = "import_")]
Import,

The rename = "class_" and rename = "import_" are there because class and import are Python reserved words. But the
rename_all = "snake_case" would already produce class and import (lowercase). The explicit renames add trailing
underscores. This works but it means the serialization format uses class_ and import_ rather than the more standard
class/import. It's a deliberate choice (matching the Python StrEnum values) but worth noting as it makes the serialized
format Python-centric rather than language-neutral.

---
Graph Module Issues

11. Range-size sort key is incorrect for multi-line ranges

graph.py:157:
key=lambda t: (t[2] - t[0], t[3] - t[1]),  # sort by range size ascending

This sorts by (end_line - start_line, end_col - start_col). But end_col - start_col is only meaningful for single-line
ranges. For a multi-line range, the "width" comparison of columns is meaningless — a range spanning lines 1-10 with
columns (1, 50) is not necessarily larger than one spanning lines 1-10 with columns (5, 80). The correct metric would be
to compute the character offset or use (end_line - start_line, end_line, end_col) as a proxy.

12. Duplicate module-graph construction

import_cycles() (line 848) and import_cycle_groups() (line 949) both construct a module-level dependency graph from
scratch by scanning all edges. This is duplicated logic that should be extracted into a shared _build_module_graph()
method.

13. _infer_package uses fragile string splitting

graph.py:473-493: The package inference relies on string patterns like "site-packages/" and "/lib/python". This is
brittle — it won't work for conda environments, pyenv, or other non-standard Python installation layouts. A more robust
approach would be to use importlib.metadata or parse the path against known Python path conventions.

14. DependencyGraph.load has a silent fallback for old format

dependency.py:142-146: The old format fallback if "src_id" not in edge_data and "src" in edge_data does an unchecked
integer comparison (src_val < graph.num_nodes()). Since graph.num_nodes() only reflects nodes added so far, and the old
format used sequential indices, this could silently drop edges if nodes were reindexed.

15. The coupling_between method double-counts directed edges

graph.py:1097-1119: This counts A->B edges and B->A edges, which gives a bidirectional coupling score. This is
reasonable for a coupling metric, but the docstring just says "Count REFERENCES edges between two files" without
specifying that it's bidirectional. A caller might expect coupling_between(A, B) == coupling_between(B, A) (which is
true, but not obviously so from reading the code).

---
API Design Observations

16. workspace_symbols("") validated in two places

session.py:90-91 checks len(query) < 1, and rust_project.py:308-309 checks if not query. Both return []. This is
redundant — the guard should be in one place only (preferably the public-facing TyO3Session).

17. Position validation is also duplicated

session.py:156-158 validates line >= 1, column >= 1. The Rust code (coordinates.rs:32-37) also validates this. The
Python validation will raise PositionError while the Rust validation returns an error string that gets wrapped in
PositionError. Both paths produce similar errors but the messages differ. The dual validation means the Rust path is
never actually reached for zero/negative values.

18. The TyProject Pydantic model (models/core.py) is unused

The TyProject model has fields like status, coordinate_mode, opened_at, etc., but TyO3Session never constructs or
returns a TyProject instance. It appears to be a spec model that predates the actual implementation. The same is true
for ProjectFile, BackendInfo, and TyProjectConfig.

19. SemanticToken.modifiers is set on Python side but Vec on Rust side

models/advanced.py:60: modifiers: set[SemanticTokenModifier]. But the Rust DTO (dto/tokens.rs:90) produces
Vec<SemanticTokenModifierDto>, and the _to_python() converter will produce a list. Pydantic's model_validate will coerce
the list to a set, but this means modifiers are ordered on the Rust side and unordered on the Python side. This isn't a
correctness issue but it's a subtle type mismatch.

20. convert_hover_markdown is dead code

convert/hover.rs:33-41: The convenience function convert_hover_markdown (without _with_index) is never called — all
callers use convert_hover_markdown_with_index directly.

---
Derive Macro

21. PyFields relies on visibility as proxy for #[pyo3(get)]

tyo3-derive/src/lib.rs:69-71: The derive macro uses pub visibility as a proxy for #[pyo3(get)] because pyclass consumes
the helper attributes before derive macros run. This is a reasonable workaround, but it means any pub field without
#[pyo3(get)] would be incorrectly included in __fields__. Currently all DTOs have #[pyo3(get)] on all pub fields, so
this isn't an active problem, but it's a fragile invariant.

22. tyo3-derive dev-dependency uses PyO3 0.23 while the main crate uses 0.28

tyo3-derive/Cargo.toml:15: pyo3 = { version = "0.23" } in dev-dependencies. The main crate uses 0.28. Since this is only
a dev-dependency (for the derive test), it's not a runtime issue, but it means the derive test builds against a
different PyO3 version than the actual usage, which could mask compatibility issues.

---
Strengths

To be clear, this is a well-built project with several strong design decisions:

- The PyFields derive macro is a smart solution to the PyO3-Pydantic impedance mismatch. The type-tag system
(str/int/float/bool passthrough, opt:, list: prefixes) avoids unnecessary recursion.
- The exception hierarchy with parallel Rust/Python exception types (_NativeClosedError -> ProjectClosedError) is clean
and avoids string-matching.
- The batch file_occurrences API is a good optimization — O(1) FFI calls per file instead of O(tokens).
- The CodeGraph secondary indexes (_id_to_index, _file_to_nodes, _file_node_ranges, _name_prefix_index) show thoughtful
optimization of hot paths.
- The navigate_to_targets helper in project.rs cleanly abstracts the shared pattern across goto_definition,
goto_declaration, and goto_type_definition.
- The demo CLI is a nice inclusion for making the library tangible.

---
Summary of Priorities

┌─────────────┬────────────────────────────────────────────────────────────────┬─────────────────────────────────┐
│  Priority   │                             Issue                              │            Location             │
├─────────────┼────────────────────────────────────────────────────────────────┼─────────────────────────────────┤
│ P0 - Bug    │ continue makes OVERRIDES detection dead code                   │ graph.py:565                    │
├─────────────┼────────────────────────────────────────────────────────────────┼─────────────────────────────────┤
│ P1 - Perf   │ check_file per file in graph build triggers N full checks      │ graph.py:138-139                │
├─────────────┼────────────────────────────────────────────────────────────────┼─────────────────────────────────┤
│ P1 - Perf   │ Double serialization (PyO3 → dict → Pydantic)                  │ rust_project.py:128-193         │
├─────────────┼────────────────────────────────────────────────────────────────┼─────────────────────────────────┤
│ P2 - Design │ Unused spec models (TyProject, ProjectFile, BackendInfo)       │ models/core.py, models/_spec.py │
├─────────────┼────────────────────────────────────────────────────────────────┼─────────────────────────────────┤
│ P2 - Maint  │ Enum registry in lib.rs is a maintenance trap                  │ lib.rs:53-61                    │
├─────────────┼────────────────────────────────────────────────────────────────┼─────────────────────────────────┤
│ P2 - Maint  │ Module-graph construction duplicated across two methods        │ graph.py:848-920, 949-1011      │
├─────────────┼────────────────────────────────────────────────────────────────┼─────────────────────────────────┤
│ P3 - Minor  │ Range-size sort key incorrect for multi-line ranges            │ graph.py:157                    │
├─────────────┼────────────────────────────────────────────────────────────────┼─────────────────────────────────┤
│ P3 - Minor  │ Duplicate validation (Python + Rust) for positions and queries │ session.py, rust_project.py     │
├─────────────┼────────────────────────────────────────────────────────────────┼─────────────────────────────────┤
│ P3 - Minor  │ convert_hover_markdown is dead code                            │ convert/hover.rs:33-41          │
├─────────────┼────────────────────────────────────────────────────────────────┼─────────────────────────────────┤
│ P3 - Minor  │ PyO3 version mismatch in derive crate dev-deps                 │ tyo3-derive/Cargo.toml:15       │
└─────────────┴────────────────────────────────────────────────────────────────┴─────────────────────────────────┘