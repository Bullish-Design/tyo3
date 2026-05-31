# TyO3 Concept

**Revision target:** `ty` 0.0.40 / Ruff submodule `3cb09eba689ebb49e799131092121928cc789c18`

## One-line concept

**TyO3** is a Pythonic, Pydantic-first interface to Astral's Rust-based `ty` semantic engine, exposed through PyO3. It wraps `ty_project::ProjectDatabase` and selected `ty_ide` operations to provide a stable Python API for type checking, code navigation, symbol search, semantic analysis, and optional downstream indexing workflows.

Pronounced: **"tie-oh-three."**

## Status of this concept

This concept has been revised and re-verified against the latest public `ty` release at the time of review:

```text
ty version:              0.0.40
ty release date:         2026-05-27
ty repository commit:    7b95bc219d1dcebc3ce39d222c66c14a3825c9a0
ruff submodule commit:   3cb09eba689ebb49e799131092121928cc789c18
review date:             2026-05-29
```

`astral-sh/ty` is currently a distribution/documentation repository that includes Ruff as a submodule. The reusable Rust crates for `ty` are therefore reviewed in `astral-sh/ruff` at the `ruff` submodule commit pinned by `ty` 0.0.40.

Relevant crates reviewed:

```text
crates/ty
crates/ty_project
crates/ty_ide
crates/ty_server
crates/ty_python_semantic
crates/ty_module_resolver
crates/ruff_db
```

The previous concept version was based on an older Ruff source snapshot. Its overall architecture remains correct, but this revision updates version metadata and corrects several API details for `ty_ide` as of `ty` 0.0.40:

- `ty_ide` exports type-hierarchy support as `prepare_type_hierarchy`, `type_hierarchy_supertypes`, and `type_hierarchy_subtypes`, not as one direct `type_hierarchy` function.
- `ty_ide` hover content directly distinguishes signature, type, typed-dict-key, and docstring content; parameter-specific hover is not a direct upstream hover variant in 0.0.40.
- `call_hierarchy` is not currently exported from `ty_ide` in 0.0.40 and should not be claimed as an available backend operation.
- `all_symbols` requires an importing-context `File` and a `QueryPattern` internally; TyO3 can still expose a Python-friendly string query and optional `importing_from` parameter.

The main architectural revision remains that TyO3 should explicitly own and wrap a `ty_project::ProjectDatabase`, then call `ty_ide` functions against that database. `ty_ide` is not itself the project/session object; it is the library of semantic operations.

## Purpose

`ty` is a fast Rust implementation of Python type checking and language-server-style semantic analysis. Its internal crate structure includes reusable components such as:

```text
ty_project             # project/config/file database
ty_python_semantic     # semantic model, types, definitions
ty_ide                 # IDE-style APIs: goto, refs, hover, symbols, tokens, hierarchy
ty_module_resolver     # module/search-path resolution
ruff_db                # files, source text, ranges, diagnostics, Salsa database plumbing
ruff_python_ast        # parsed Python AST infrastructure
```

These components can answer many code-intelligence questions that Python tools need:

- What Python files belong to this project?
- What diagnostics does the type checker report?
- What symbols are defined in this file?
- What symbols match this workspace query?
- What symbols are importable from this project or environment?
- Where is this symbol defined?
- Where is this symbol declared?
- Where is this symbol's type definition?
- Where is this symbol referenced?
- What is the inferred type at this location?
- What signature, type, typed-dict-key, or docstring information should be shown on hover?
- What semantic token classification applies to this range?
- What are this class's supertypes or subtypes?

Today, these capabilities are primarily exposed through ty's CLI, LSP server, and Rust crates. TyO3 should expose a focused, stable Python API over a pinned ty/Ruff commit or release.

TyO3 should not attempt to wrap every internal ty concept. It should provide a curated Python interface for semantic code intelligence.

## Scope

TyO3 is a general-purpose Python library, not a Scippy-specific adapter.

Scippy can use TyO3 for SCIP generation, code graph construction, and incremental semantic updates, but TyO3 should also be useful for:

- editor tooling;
- static analysis;
- documentation tooling;
- codebase search;
- refactoring tools;
- import analysis;
- API surface extraction;
- test impact analysis;
- architectural graphing;
- notebook-based code exploration;
- AI code-context retrieval;
- custom Python linting and review workflows.

## Design principle

TyO3 should wrap **ty as a semantic project engine**, not merely `ty_ide` as a bag of cursor commands.

The practical Rust dependency stack is:

```text
TyO3 Python API
    ↓
Pydantic public models
    ↓
Python wrapper methods
    ↓
PyO3 boundary
    ↓
TyO3 Rust facade crate
    ↓
ty_project::ProjectDatabase
    ↓
ty_ide operations
    ↓
ty_python_semantic / ty_module_resolver / ruff_db / Ruff AST
```

`ty_ide` already exports most of the operations TyO3 needs in `ty` 0.0.40, including:

```text
document_symbols
workspace_symbols
all_symbols
goto_definition
goto_declaration
goto_type_definition
find_references
hover
semantic_tokens
prepare_type_hierarchy
type_hierarchy_supertypes
type_hierarchy_subtypes
signature_help
completion
rename
document_highlights
inlay_hints
folding_ranges
code_actions
```

`call_hierarchy` should be treated as a future/non-confirmed feature for this backend version. If TyO3 later exposes call hierarchy, it should first verify an upstream `ty_ide` export or implement a separate semantic traversal using `ty_python_semantic`.

However, TyO3 should not expose `ty_ide` directly. `ty_ide` APIs are Rust-facing and typically accept internal `File` handles and `TextSize` offsets. TyO3 should provide a higher-level `TyProject` abstraction that accepts Python paths and line/column positions, then returns stable Pydantic models.

## Non-goals

TyO3 v0.1 should not attempt to:

- replace ty's CLI;
- replace ty's LSP server;
- expose arbitrary ty internals directly;
- provide a complete Python implementation of all ty/Ruff data structures;
- guarantee compatibility across unpinned ty versions;
- provide a full refactoring engine;
- provide a complete SCIP generator as its primary purpose;
- expose low-level Salsa database machinery to Python;
- expose raw Rust lifetimes, `File` handles, `TextSize`, or internal IDs as stable user-facing objects;
- implement efficient whole-project reference export unless the Rust traversal design is clear.

## Version-pinning model

TyO3 should explicitly pin to a specific ty/Ruff commit or release.

This is not a weakness. It is the correct architecture for an early PyO3 semantic wrapper. Ty's internal APIs are not designed as a stable Python ABI. TyO3 should own the stability boundary.

Recommended policy:

```text
TyO3 version     pinned ty/Ruff backend
0.1.x            one exact ty/Ruff commit or release
0.2.x            explicit upgrade to a newer ty/Ruff commit or release
0.3.x            explicit upgrade to a newer ty/Ruff commit or release
```

TyO3's Python API should remain stable where possible. The Rust adapter layer absorbs changes in ty internals.

TyO3 should expose backend metadata:

```python
import tyo3

print(tyo3.__version__)
print(tyo3.ty_version)
print(tyo3.backend_info())
```

Example:

```python
BackendInfo(
    tyo3_version="0.1.0",
    ty_version="0.0.40",
    ty_commit="7b95bc219d1dcebc3ce39d222c66c14a3825c9a0",
    ruff_submodule_commit="3cb09eba689ebb49e799131092121928cc789c18",
    backend_source="astral-sh/ty@0.0.40",
)
```

## Core user experience

The main object is `TyProject`.

```python
from pathlib import Path

from tyo3 import TyProject

project = TyProject.open(Path("."))

check = project.check()
for diagnostic in check.diagnostics:
    print(diagnostic.message)

symbols = project.document_symbols("src/app.py")
refs = project.find_references("src/app.py", line=42, column=17)
defs = project.goto_definition("src/app.py", line=42, column=17)
hover = project.hover("src/app.py", line=42, column=17)
```

The user should not need to know about Rust files, Salsa queries, `TextSize`, `File`, module handles, or internal ty/Ruff IDs.

## Public API layers

TyO3 should have three API layers.

### 1. Simple project API

This is the default user-facing API.

```python
project = TyProject.open(".")
project.files()
project.check()
project.document_symbols("pkg/mod.py")
project.workspace_symbols("User")
project.goto_definition("pkg/mod.py", line=10, column=4)
project.find_references("pkg/mod.py", line=10, column=4)
project.hover("pkg/mod.py", line=10, column=4)
```

This layer should return Pydantic models.

### 2. Extended IDE API

The actual `ty_ide` codebase already supports more than the original v0.1 concept. TyO3 can expose these incrementally:

```python
project.all_symbols("DataFrame", importing_from="pkg/mod.py")
project.goto_declaration("pkg/mod.py", line=10, column=4)
project.goto_type_definition("pkg/mod.py", line=10, column=4)
project.semantic_tokens("pkg/mod.py")
project.type_hierarchy("pkg/models.py", line=12, column=6)
project.signature_help("pkg/calls.py", line=20, column=15)
```

These operations are present on the Rust side in `ty` 0.0.40, with one important adapter detail: Python-facing `project.type_hierarchy(...)` should compose upstream `prepare_type_hierarchy`, `type_hierarchy_supertypes`, and `type_hierarchy_subtypes`. They should still be introduced carefully so the Python model layer remains stable.

### 3. Batch analysis and indexing API

This layer supports whole-repository workflows.

```python
project.symbol_index()
project.reference_index()
project.semantic_index()
project.export_symbol_index()
```

The Rust code already supports batch-ish symbol operations through `workspace_symbols` and `all_symbols`, but efficient whole-project reference export is not directly available as a simple public `ty_ide` function. It likely requires custom Rust traversal over `ty_python_semantic`.

This should be a major post-v0.1 design area.

### 4. Incremental/session API

This layer supports long-lived processes.

```python
project.reload()
project.apply_file_changes(...)
project.update_file("pkg/mod.py", new_source)
project.close_file("pkg/mod.py")
project.changed_files()
project.recheck_changed()
```

The actual `ProjectDatabase` has change-application machinery through file/watch events. However, a Python `update_file(path, source)` API requires a clear in-memory overlay or write-through-to-disk strategy.

This should remain post-v0.1 unless the implementation can reuse existing server document snapshot machinery cleanly.

## Revised architecture

### Python package

```text
tyo3/
  __init__.py
  project.py
  models.py
  enums.py
  exceptions.py
  paths.py
  coordinates.py
  py.typed
```

### Rust extension crate

```text
rust/
  Cargo.toml
  src/
    lib.rs              # PyO3 module
    project.rs          # PyTyProject wrapper over ProjectDatabase
    config.rs           # project/config DTOs
    files.rs            # path ↔ File resolution
    coordinates.rs      # line/column ↔ TextSize conversion
    models.rs           # Rust DTOs for Python payloads
    diagnostics.rs      # diagnostic conversion
    symbols.rs          # symbol conversion
    navigation.rs       # goto conversion
    references.rs       # reference conversion
    hover.rs            # structured hover conversion
    tokens.rs           # semantic token conversion
    hierarchy.rs        # type hierarchy conversion
    errors.rs           # error mapping and panic handling
```

### Rust state

The Rust wrapper should own a `ProjectDatabase`.

```rust
#[pyclass]
pub struct PyTyProject {
    inner: Arc<Mutex<TyProjectState>>,
}

struct TyProjectState {
    db: ProjectDatabase,
    root: SystemPathBuf,
}
```

A separate `Project` field is not necessary because `db.project()` returns the stable project handle.

A path index may still be useful for faster path lookup and better error messages, but it should be treated as adapter state, not as the semantic source of truth.

### Rust facade shape

```rust
impl TyProjectState {
    fn open(config: ProjectConfigDto) -> Result<Self>;

    fn check(&self) -> Result<CheckResultDto>;
    fn files(&self) -> Result<Vec<PathDto>>;

    fn document_symbols(&self, path: &Path) -> Result<Vec<SymbolDto>>;
    fn workspace_symbols(&self, query: &str) -> Result<Vec<SymbolDto>>;
    fn all_symbols(
        &self,
        query: &str,
        importing_from: Option<&Path>,
    ) -> Result<Vec<SymbolDto>>;

    fn goto_definition(
        &self,
        path: &Path,
        position: PositionDto,
    ) -> Result<Vec<DefinitionTargetDto>>;

    fn goto_declaration(
        &self,
        path: &Path,
        position: PositionDto,
    ) -> Result<Vec<DefinitionTargetDto>>;

    fn goto_type_definition(
        &self,
        path: &Path,
        position: PositionDto,
    ) -> Result<Vec<DefinitionTargetDto>>;

    fn find_references(
        &self,
        path: &Path,
        position: PositionDto,
        include_declaration: bool,
    ) -> Result<Vec<ReferenceDto>>;

    fn hover(
        &self,
        path: &Path,
        position: PositionDto,
    ) -> Result<Option<HoverDto>>;

    fn semantic_tokens(
        &self,
        path: &Path,
        range: Option<RangeDto>,
    ) -> Result<Vec<SemanticTokenDto>>;

    fn type_hierarchy(
        &self,
        path: &Path,
        position: PositionDto,
    ) -> Result<Option<TypeHierarchyDto>>;
}
```

### Common operation pipeline

Most cursor-style operations follow the same conversion pipeline:

```text
Python path + 1-based line/column
    ↓
resolve Python path to ruff_db::files::File
    ↓
convert line/column to ruff_text_size::TextSize
    ↓
call ty_ide operation
    ↓
convert File/TextRange/enums/type payload into stable Rust DTO
    ↓
return Python dict/list/scalar payload through PyO3
    ↓
validate into Pydantic models
```

## DTO-first Rust design

Prefer Rust DTOs that are independent of ty's borrowed/lifetime-heavy internal types.

```rust
#[derive(Debug, Clone, Serialize)]
pub struct SymbolDto {
    pub name: String,
    pub qualified_name: Option<String>,
    pub kind: String,
    pub location: FileRangeDto,
    pub selection_range: Option<RangeDto>,
    pub container_name: Option<String>,
    pub deprecated: bool,
}
```

The PyO3 layer can convert DTOs into Python dictionaries. Python then validates into Pydantic models.

Recommended implementation pattern:

```text
Rust ty internals
    ↓ convert into stable Rust DTOs
serde-compatible DTOs
    ↓ PyO3 conversion
Python dict/list/scalar payloads
    ↓ Pydantic validation
TyO3 public models
```

This reduces coupling between Python classes and Rust internals.

## Core Pydantic models

All public data models should be Pydantic models. They should be stable, serializable, and friendly for downstream tools.

### Position and range

```python
from pathlib import Path

from pydantic import BaseModel, Field


class Position(BaseModel):
    line: int = Field(ge=1)
    column: int = Field(ge=1)


class Range(BaseModel):
    start: Position
    end: Position


class FileRange(BaseModel):
    path: Path
    range: Range
```

Use 1-based line and 1-based column positions in Python. Internally, TyO3 converts to ty/Ruff byte offsets.

### Diagnostics

```python
from enum import Enum
from pathlib import Path

from pydantic import BaseModel


class DiagnosticSeverity(str, Enum):
    fatal = "fatal"
    error = "error"
    warning = "warning"
    information = "information"
    hint = "hint"


class Diagnostic(BaseModel):
    path: Path | None = None
    range: Range | None = None
    severity: DiagnosticSeverity
    code: str | None = None
    message: str
    details: list[str] = []
```

### Symbols

The actual `ty_ide::SymbolKind` includes `Import`, so TyO3 should include it instead of dropping it.

```python
class SymbolKind(str, Enum):
    module = "module"
    class_ = "class"
    function = "function"
    method = "method"
    constructor = "constructor"
    variable = "variable"
    constant = "constant"
    field = "field"
    parameter = "parameter"
    property = "property"
    type_parameter = "type_parameter"
    import_ = "import"
    unknown = "unknown"


class Symbol(BaseModel):
    name: str
    qualified_name: str | None = None
    kind: SymbolKind
    location: FileRange
    selection_range: Range | None = None
    container_name: str | None = None
    deprecated: bool = False
```

### Navigation

```python
class DefinitionTarget(BaseModel):
    path: Path
    range: Range
    selection_range: Range | None = None
    symbol: Symbol | None = None
    module_name: str | None = None
```

`ty_ide::NavigationTarget` already has a full range and focus range. TyO3 should map:

```text
NavigationTarget.full_range  -> DefinitionTarget.range
NavigationTarget.focus_range -> DefinitionTarget.selection_range
```

### References

The actual Rust reference kind currently has `Read`, `Write`, and `Other`. Declaration inclusion is controlled by the `include_declaration` argument, not by a distinct `ReferenceKind::Declaration`.

For v0.1, avoid synthesizing a declaration kind unless it can be done reliably.

```python
class ReferenceKind(str, Enum):
    read = "read"
    write = "write"
    other = "other"


class Reference(BaseModel):
    path: Path
    range: Range
    kind: ReferenceKind
```

A future version may add:

```python
declaration = "declaration"
```

if TyO3 can reliably distinguish declaration ranges from other references.

### Hover

In `ty` 0.0.40, the actual `ty_ide` hover content includes signature, type, typed-dict-key, and docstring variants. The LSP server can render these into Markdown or plain text, but TyO3 should prefer structured conversion.

Parameter-specific help is available through `signature_help`; it is not a direct `ty_ide::HoverContent` variant in 0.0.40. TyO3 should not expose a `parameter` hover kind unless it explicitly derives that information from another upstream API.

```python
class HoverContentKind(str, Enum):
    type = "type"
    signature = "signature"
    docstring = "docstring"
    typed_dict_key = "typed_dict_key"
    markdown = "markdown"
    plain_text = "plain_text"


class HoverContent(BaseModel):
    kind: HoverContentKind
    value: str


class Hover(BaseModel):
    location: FileRange
    contents: list[HoverContent]
```

For `HoverContentKind.type`, TyO3 should render ty's internal `Type` to a stable string using ty's display settings.

For `typed_dict_key`, TyO3 may render a concise string such as:

```text
(key of Owner) key: Type
```

or introduce a richer future model.

### Semantic tokens

The actual `ty_ide` semantic token types closely match this model.

```python
class SemanticTokenType(str, Enum):
    namespace = "namespace"
    class_ = "class"
    parameter = "parameter"
    self_parameter = "self_parameter"
    cls_parameter = "cls_parameter"
    variable = "variable"
    property = "property"
    function = "function"
    method = "method"
    keyword = "keyword"
    string = "string"
    number = "number"
    decorator = "decorator"
    builtin_constant = "builtin_constant"
    type_parameter = "type_parameter"


class SemanticTokenModifier(str, Enum):
    definition = "definition"
    readonly = "readonly"
    async_ = "async"
    documentation = "documentation"


class SemanticToken(BaseModel):
    path: Path
    range: Range
    token_type: SemanticTokenType
    modifiers: set[SemanticTokenModifier] = set()
```

Known upstream limitations should be documented if TyO3 exposes this API:

- quoted annotations are not fully handled;
- properties/descriptors may be classified imprecisely;
- special forms may be classified as variables;
- type aliases do not currently have a dedicated token type;
- additional modifiers may be added upstream.

### Type hierarchy

In `ty` 0.0.40, type hierarchy is exposed through three upstream operations: `prepare_type_hierarchy`, `type_hierarchy_supertypes`, and `type_hierarchy_subtypes`. TyO3 can expose a single Python `type_hierarchy(...)` method by composing those operations.

The actual Rust type hierarchy item model includes name, detail, file, full range, and selection range.

```python
class TypeHierarchyItem(BaseModel):
    name: str
    detail: str | None = None
    path: Path
    full_range: Range
    selection_range: Range


class TypeHierarchy(BaseModel):
    item: TypeHierarchyItem
    supertypes: list[TypeHierarchyItem] = []
    subtypes: list[TypeHierarchyItem] = []
```

### Project check result

```python
class CheckResult(BaseModel):
    diagnostics: list[Diagnostic]
    files_checked: int | None = None
    elapsed_ms: float | None = None
```

The Rust check API returns diagnostics. `files_checked` may need to be computed separately from project files or reporter data.

### Backend information

```python
class BackendInfo(BaseModel):
    tyo3_version: str
    ty_version: str | None = None
    ty_commit: str | None = None
    ruff_submodule_commit: str | None = None
    backend_source: str | None = None
```

## Main Python API sketch

```python
from pathlib import Path
from pydantic import BaseModel


class TyProjectConfig(BaseModel):
    root: Path
    python_version: str | None = None
    config_path: Path | None = None
    extra_search_paths: list[Path] = []
    respect_gitignore: bool = True
    force_exclude: bool = False
    check_all_files: bool = True
    coordinate_mode: str = "python"


class TyProject:
    @classmethod
    def open(
        cls,
        root: str | Path,
        config: TyProjectConfig | None = None,
    ) -> "TyProject":
        ...

    def check(self) -> CheckResult:
        ...

    def files(self) -> list[Path]:
        ...

    def document_symbols(self, path: str | Path) -> list[Symbol]:
        ...

    def workspace_symbols(self, query: str) -> list[Symbol]:
        ...

    def all_symbols(
        self,
        query: str,
        *,
        importing_from: str | Path | None = None,
    ) -> list[Symbol]:
        ...

    def goto_definition(
        self,
        path: str | Path,
        line: int,
        column: int,
    ) -> list[DefinitionTarget]:
        ...

    def goto_declaration(
        self,
        path: str | Path,
        line: int,
        column: int,
    ) -> list[DefinitionTarget]:
        ...

    def goto_type_definition(
        self,
        path: str | Path,
        line: int,
        column: int,
    ) -> list[DefinitionTarget]:
        ...

    def find_references(
        self,
        path: str | Path,
        line: int,
        column: int,
        *,
        include_declaration: bool = True,
    ) -> list[Reference]:
        ...

    def hover(
        self,
        path: str | Path,
        line: int,
        column: int,
    ) -> Hover | None:
        ...

    def semantic_tokens(
        self,
        path: str | Path,
        *,
        range: Range | None = None,
    ) -> list[SemanticToken]:
        ...

    def type_hierarchy(
        self,
        path: str | Path,
        line: int,
        column: int,
    ) -> TypeHierarchy | None:
        ...

    def reload(self) -> None:
        ...
```

## v0.1 recommended scope

TyO3 v0.1 should still be intentionally narrow, even though the Rust backend supports more.

### Include

- `TyProject.open`
- `TyProject.files`
- `TyProject.check`
- `TyProject.document_symbols`
- `TyProject.workspace_symbols`
- `TyProject.all_symbols`
- `TyProject.goto_definition`
- `TyProject.goto_declaration`
- `TyProject.goto_type_definition`
- `TyProject.find_references`
- `TyProject.hover`
- stable Pydantic models for diagnostics, symbols, definitions, references, hover, ranges
- robust path and offset conversion
- Python exceptions for project/config/path/position/analysis failures
- backend metadata
- basic tests against tiny fixture projects

### Optional in v0.1 if low-friction

- `semantic_tokens`
- `type_hierarchy`

These are already implemented in `ty_ide`, but they add model and compatibility surface area. For type hierarchy, TyO3 should compose the upstream prepare/supertypes/subtypes functions into one Python-friendly result.

### Defer

- incremental `update_file`;
- in-memory document overlays;
- call hierarchy, unless and until confirmed/exported in the pinned backend;
- signature help;
- completion;
- rename/edit API;
- code actions;
- SCIP export;
- graph export;
- async API;
- long-running server mode;
- package metadata inspection;
- rich import graph analysis;
- whole-project reference index export.

## v0.2 recommended scope

Add richer IDE operations:

- semantic tokens, if not shipped in v0.1;
- type hierarchy, if not shipped in v0.1;
- call hierarchy, unless and until confirmed/exported in the pinned backend;
- signature help;
- document highlights;
- inlay hints;
- selected completion APIs;
- importable symbol search refinements;
- module resolution helpers;
- project-wide symbol index export;
- optional cached analysis session helpers.

## v0.3 recommended scope

Add live/incremental workflows:

- `update_file`;
- `close_file`;
- `reload_config`;
- changed-file tracking;
- recheck changed files;
- partial symbol index refresh;
- optional SCIP export adapter;
- optional graph adapter.

## Why PyO3 instead of subprocess CLI

A subprocess CLI is enough for batch checking. It is not enough for TyO3's broader goal.

PyO3 enables:

- persistent in-memory project state;
- direct semantic queries from Python;
- lower latency for repeated calls;
- incremental file updates later;
- richer Python object model;
- integration into notebooks, servers, test frameworks, and AI tooling;
- composability with Pydantic, SQLModel, FastAPI, NetworkX, rustworkx, and custom analysis pipelines.

The value of TyO3 is not just "run ty from Python." It is "use ty as a Python semantic engine."

## Why not expose raw ty internals

Raw ty internals are optimized for Rust, Salsa, and editor/server performance. They are not stable or ergonomic Python concepts.

TyO3 should avoid exposing:

- raw `File` handles;
- raw `TextSize` offsets;
- raw Salsa database objects;
- ty-internal lifetime-bearing references;
- internal definition IDs without stable conversion;
- ty-specific enum variants that are not meaningful to Python users.

Instead, TyO3 should expose stable DTOs:

```text
Path
Range
Symbol
Reference
DefinitionTarget
Diagnostic
Hover
SemanticToken
TypeHierarchyItem
```

## Path and coordinate policy

This is critical.

Python users expect line/column positions. ty/Ruff internally uses byte offsets and text ranges. The LSP server also deals with zero-based LSP positions and encoding modes such as UTF-16.

TyO3 policy for v0.1:

- Python API uses 1-based line and 1-based column.
- Columns are Python-friendly Unicode-codepoint columns unless otherwise specified.
- Internally, TyO3 converts to ty/Ruff `TextSize`.
- Returned ranges use the same Python coordinate mode as inputs.
- All returned paths are absolute by default.
- Project-relative path output may be an option later.
- Vendored, stub, dependency, notebook, and virtual files need explicit representation if they cannot be represented as ordinary local paths.

Potential future option:

```python
class CoordinateMode(str, Enum):
    python = "python"      # 1-based line/column, Python-friendly
    lsp_utf16 = "lsp_utf16"
    utf8_byte = "utf8_byte"
```

v0.1 should choose one default and document it clearly.

## File resolution policy

Most `ty_ide` functions accept an internal `File`. TyO3 must resolve Python paths into those file handles.

Recommended policy:

- Accept absolute paths and project-relative paths.
- Normalize paths against `TyProject.root`.
- Reject paths outside the project unless they resolve to known dependency, vendored, or search-path files.
- Return a clear `FileNotFoundError` or `PathResolutionError` when the path cannot be resolved.
- Provide an escape hatch later for backend file identifiers only if absolutely necessary.

## Threading and concurrency

TyO3 should initially use a conservative threading model:

- one `TyProject` owns one Rust `ProjectDatabase`;
- operations are synchronized internally;
- heavy Rust operations release the GIL;
- Python objects returned from Rust are detached DTOs;
- no borrowed Rust references are exposed to Python.

`ty_ide` itself uses Rayon in some operations, such as workspace symbol scanning. TyO3 should avoid adding unnecessary Python-side parallelism around the same database until the concurrency story is proven.

Future versions can allow parallel project instances or internal parallel operations where ty already supports them.

## Caching and lifetime model

`TyProject` is a stateful object.

```python
project = TyProject.open(".")
```

Opening a project initializes ty's project database and file universe. Repeated calls should reuse the same database where possible.

This enables:

- cheaper repeated lookups;
- future incremental updates;
- cached parsed files;
- cached semantic results;
- lower latency in notebooks and servers.

The API should also support one-shot helpers for simple scripts:

```python
from tyo3 import check_project

result = check_project(".")
```

Potential helpers:

```python
check_project(path)
document_symbols(path, file)
goto_definition(path, file, line, column)
```

These should be thin wrappers over `TyProject`.

## Relationship to ty CLI and LSP

TyO3 should not shell out to ty.

It should depend on ty's Rust crates directly and expose selected semantic capabilities to Python.

However, TyO3 should align with ty CLI/LSP behavior where possible:

- same config discovery;
- same type-checking semantics;
- same module resolution;
- same diagnostic meanings;
- same symbol/navigation behavior;
- same hover/type display where appropriate.

When TyO3 differs, the difference should be documented as a TyO3 API choice.

Example differences:

- TyO3 uses 1-based Python-friendly line/column positions by default.
- TyO3 returns structured Pydantic models instead of LSP JSON.
- TyO3 may expose structured hover contents rather than a single Markdown string.

## Relationship to Scippy

Scippy can be a downstream consumer.

Possible Scippy integrations:

```text
TyO3 document_symbols      -> Scippy symbol table
TyO3 find_references       -> Scippy reference edges
TyO3 semantic_tokens       -> Scippy occurrence roles
TyO3 type_hierarchy        -> Scippy inheritance edges
TyO3 hover                 -> Scippy symbol documentation
TyO3 project files         -> Scippy document set
```

A future `scippy-tyo3` package could expose:

```python
from scippy_tyo3 import build_scip_index, build_code_graph

index = build_scip_index(project)
graph = build_code_graph(project)
```

But TyO3 itself should stay neutral.

## Potential downstream integrations

### Static analysis

```python
project = TyProject.open(".")
for symbol in project.workspace_symbols("Repository"):
    print(symbol.qualified_name, symbol.location)
```

### Documentation generation

```python
for symbol in project.document_symbols("pkg/api.py"):
    hover = project.hover(
        symbol.location.path,
        line=symbol.location.range.start.line,
        column=symbol.location.range.start.column,
    )
    render_api_doc(symbol, hover)
```

### Refactoring tools

```python
refs = project.find_references("pkg/models.py", line=20, column=7)
for ref in refs:
    print(ref.path, ref.range, ref.kind)
```

### Import search

```python
for symbol in project.all_symbols("BaseModel", importing_from="pkg/models.py"):
    print(symbol.qualified_name, symbol.location.path)
```

### Graph construction

```python
symbols = project.symbol_index()
references = project.reference_index()
graph = build_graph(symbols, references)
```

### AI context retrieval

```python
definition = project.goto_definition("app/routes.py", line=88, column=19)
refs = project.find_references(
    definition[0].path,
    definition[0].range.start.line,
    definition[0].range.start.column,
)
context = project.source_slices([*definition, *refs])
```

## Core challenge: batch occurrence export

TyO3 should expose cursor-style APIs first. But many advanced workflows need batch APIs.

Cursor-style:

```python
project.find_references("file.py", line=10, column=5)
```

Batch-style:

```python
project.references_for_file("file.py")
project.references_for_project()
project.symbol_index()
project.semantic_index()
```

The current `ty_ide` crate directly supports cursor-style reference search. It also supports batch-ish symbol search through `workspace_symbols` and `all_symbols`.

However, an efficient whole-project reference index is not currently a simple public `ty_ide` API. Implementing it by repeatedly calling `find_references` for every symbol may be slow, incomplete, or redundant.

Batch reference export likely requires custom Rust traversal that uses `ty_python_semantic` directly rather than repeatedly calling cursor operations. This should be a major post-v0.1 design area.

## Proposed batch index models

```python
class SymbolIndex(BaseModel):
    symbols: list[Symbol]


class ReferenceIndex(BaseModel):
    references: list[ResolvedReference]


class ResolvedReference(BaseModel):
    source: FileRange
    target: DefinitionTarget | None
    kind: ReferenceKind
    name: str


class ProjectSemanticIndex(BaseModel):
    files: list[Path]
    symbols: list[Symbol]
    references: list[ResolvedReference]
    diagnostics: list[Diagnostic]
```

This is not necessarily SCIP. It is a Python-native semantic index that Scippy or other tools can transform into graph/index formats.

## Why Pydantic

Pydantic gives TyO3:

- stable public models;
- validation of Rust-returned payloads;
- JSON serialization;
- compatibility with FastAPI and other Python tooling;
- clear schemas for downstream consumers;
- easy test snapshots;
- ergonomic typed Python usage.

The Rust extension should not be the public model layer. It should be the high-performance semantic backend.

## Packaging concept

TyO3 should be distributed as a Python package with a native Rust extension.

```text
tyo3
  Python package
  PyO3 extension module
  bundled/pinned Rust dependency on ty/Ruff crates
```

The package should include:

- Python type hints;
- Pydantic models;
- Rust extension module;
- pinned ty/Ruff version metadata;
- basic CLI for debugging.

Example CLI:

```bash
tyo3 check .
tyo3 symbols src/app.py
tyo3 refs src/app.py:42:17
tyo3 hover src/app.py:42:17
```

The CLI is secondary. The Python API is primary.

## Compatibility and stability

TyO3 should expose two kinds of version information:

```python
tyo3.__version__
tyo3.backend_info()
```

The backend info should identify the exact pinned ty/Ruff commit or release.

Users should be able to inspect which ty backend is bundled.

## Error model

TyO3 should define a small hierarchy of Python exceptions:

```python
class TyO3Error(Exception): ...
class ProjectOpenError(TyO3Error): ...
class ConfigError(TyO3Error): ...
class PathResolutionError(TyO3Error): ...
class FileNotFoundError(TyO3Error): ...
class PositionError(TyO3Error): ...
class AnalysisError(TyO3Error): ...
class InternalTyError(TyO3Error): ...
```

Rust panics from ty internals should be caught and converted into `InternalTyError` where possible. Users should not see Rust panic payloads unless debug mode is enabled.

## Testing strategy

TyO3 needs fixture-based tests.

### Unit tests

- path normalization;
- path-to-file resolution;
- range conversion;
- Pydantic validation;
- diagnostic conversion;
- symbol kind conversion;
- reference kind conversion;
- hover content conversion;
- semantic token conversion;
- backend info reporting.

### Fixture tests

Create tiny projects:

```text
fixtures/
  simple_package/
  imports/
  classes/
  inheritance/
  external_package/
  syntax_error/
  pyproject_config/
  stubs/
  unicode_positions/
```

Validate:

- file discovery;
- diagnostics;
- document symbols;
- workspace symbols;
- all symbols;
- goto definition;
- goto declaration;
- goto type definition;
- references;
- hover output;
- semantic tokens;
- type hierarchy.

### Snapshot tests

Use JSON snapshots of Pydantic model dumps.

```python
assert project.document_symbols("pkg/models.py") == snapshot
```

### Cross-check tests

Where useful, compare against ty CLI/LSP behavior on the same fixtures.

### Coordinate tests

Coordinate conversion needs dedicated tests:

- ASCII identifiers;
- Unicode identifiers;
- multi-byte strings;
- emojis in comments/strings;
- CRLF files;
- end-of-line positions;
- invalid line/column inputs.

## Performance goals

TyO3 should preserve ty's performance advantages.

Initial targets:

- project open should be fast enough for CLI-like use;
- repeated queries should benefit from the persistent database;
- workspace symbol search should reuse ty/Ruff caches;
- batch symbol export should avoid unnecessary repeated parsing;
- heavy Rust calls should release the GIL;
- returned Pydantic models should be detached and safe.

Performance should be measured with:

- cold project open time;
- check time;
- repeated goto/reference latency;
- document symbol latency;
- hover latency;
- workspace symbol latency;
- memory usage;
- large-project behavior.

## Safety and failure handling

TyO3 must be defensive around Rust internals.

- Convert Rust errors into Python exceptions.
- Convert ty diagnostics into `Diagnostic` models.
- Catch panics where practical.
- Avoid exposing invalid internal IDs.
- Avoid dangling references into the Rust database.
- Validate all Python-provided paths and positions.
- Provide debug mode for internal error detail.

## Open design questions

### Should TyO3 expose `ty_ide` almost directly?

No.

A direct wrapper would be easier but too Rust-shaped. TyO3 should provide Python-friendly operations and models, even if the internal implementation mostly delegates to `ty_ide`.

### Should TyO3 use Pydantic v2 only?

Yes. The library should target Pydantic v2 and modern Python versions.

### Should TyO3 expose async APIs?

Not initially. Most operations are CPU-bound Rust calls. Async wrappers can come later for server use cases.

### Should TyO3 include SCIP export?

Not in the core v0.1 concept. SCIP export should be an optional downstream package or extension:

```text
tyo3-scip
tyo3-graph
scippy-tyo3
```

### Should TyO3 expose a low-level escape hatch?

Possibly, but only after the high-level model is stable. An escape hatch may be useful for advanced users, but raw ty internals should not leak into normal APIs.

### Should hover be structured or rendered?

Structured by default.

The Rust hover model already distinguishes signatures, types, typed-dict keys, and docstrings. TyO3 should preserve that structure instead of returning only the LSP-rendered Markdown string. Parameter-specific information should come from `signature_help`, not from hover, unless upstream changes.

A rendered helper can be added later:

```python
hover.to_markdown()
hover.to_plain_text()
```

### Should `all_symbols` require `importing_from`?

The Rust API in `ty` 0.0.40 requires an importing-context `File` and a `QueryPattern`, not merely a raw query string. TyO3 should hide that Rust shape by accepting a Python `str` query and exposing `importing_from` as an optional keyword.

Recommended default:

- if omitted, use the first indexed first-party project file as the importing context;
- if no project file exists, return an empty list or raise `AnalysisError`;
- document that results may differ depending on import context;
- convert the Python query string into the appropriate upstream `QueryPattern` in Rust.

### Should references include declarations as a separate kind?

Not in v0.1.

The Rust API controls declaration inclusion with `include_declaration`, but the reference kind is only read/write/other. TyO3 should avoid inventing a declaration kind until it can identify declarations reliably.

## Recommended implementation plan

### Phase 0: Rust feasibility spike

Goal: prove PyO3 can open a ty project and answer one semantic query.

Tasks:

1. Create minimal PyO3 crate.
2. Pin ty/Ruff commit.
3. Reuse ty CLI project loading path where practical:
   - resolve project path;
   - discover/load `ProjectMetadata`;
   - apply configuration;
   - construct `ProjectDatabase`.
4. Resolve one Python path to `File`.
5. Convert one Python line/column position to `TextSize`.
6. Call `document_symbols` or `goto_definition`.
7. Return JSON-like payload to Python.
8. Validate into a Pydantic model.

Success criteria:

- `TyProject.open(".")` works on a tiny fixture.
- `project.document_symbols("main.py")` returns at least functions/classes.
- Returned objects are Pydantic models.
- No raw Rust internals are visible in Python.

### Phase 1: v0.1 core API

Goal: stable basic Python semantic interface.

Implement:

- project opening;
- file listing;
- checking;
- document symbols;
- workspace symbols;
- all symbols;
- goto definition;
- goto declaration;
- goto type definition;
- find references;
- hover;
- Pydantic models;
- exceptions;
- path and coordinate conversion;
- fixture tests.

Success criteria:

- fixture suite passes;
- all public returns are Pydantic models;
- no raw Rust internals exposed;
- package builds as a Python extension;
- backend commit/version is inspectable.

### Phase 2: richer IDE APIs

Add:

- semantic tokens;
- type hierarchy;
- call hierarchy, unless and until confirmed/exported in the pinned backend;
- signature help;
- document highlights;
- inlay hints;
- selected completion APIs;
- rename preview, if practical.

### Phase 3: batch semantic index

Add:

- symbol index export;
- reference index export;
- project semantic index export;
- optional graph-friendly edge models;
- efficient semantic traversal instead of repeated cursor calls.

### Phase 4: incremental project sessions

Add:

- update file;
- close file;
- reload config;
- changed file tracking;
- partial recheck;
- partial semantic index refresh.

This phase should be designed around either:

- an in-memory file overlay system;
- ty_server-style document snapshots;
- or explicit disk-backed sync semantics.

## Example future API

```python
from pathlib import Path

from tyo3 import TyProject

project = TyProject.open(Path("."))

# Type checking
check = project.check()
print(f"{len(check.diagnostics)} diagnostics")

# Files
for path in project.files():
    print(path)

# Symbols
for symbol in project.document_symbols("src/models.py"):
    print(symbol.kind, symbol.name, symbol.location.range.start)

# Workspace symbol search
for symbol in project.workspace_symbols("Repository"):
    print(symbol.qualified_name, symbol.location.path)

# Importable/all symbol search
for symbol in project.all_symbols("BaseModel", importing_from="src/models.py"):
    print(symbol.qualified_name)

# Navigation
definitions = project.goto_definition("src/routes.py", line=44, column=21)
for target in definitions:
    print(target.path, target.range)

# References
references = project.find_references("src/models.py", line=12, column=7)
for ref in references:
    print(ref.kind, ref.path, ref.range)

# Hover/type/doc information
hover = project.hover("src/routes.py", line=44, column=21)
if hover:
    for item in hover.contents:
        print(item.kind, item.value)

# Optional richer APIs
tokens = project.semantic_tokens("src/models.py")
hierarchy = project.type_hierarchy("src/models.py", line=10, column=7)

# Future batch semantic export
semantic_index = project.semantic_index()
print(len(semantic_index.symbols), len(semantic_index.references))
```


## Source references for this revision

This concept revision is based on the following upstream facts checked for `ty` 0.0.40:

- `ty` 0.0.40 release: <https://github.com/astral-sh/ty/releases/tag/0.0.40>
- PyPI `ty` project metadata: <https://pypi.org/project/ty/>
- `astral-sh/ty` commit for 0.0.40: `7b95bc219d1dcebc3ce39d222c66c14a3825c9a0`
- Ruff submodule commit pinned by `ty` 0.0.40: `3cb09eba689ebb49e799131092121928cc789c18`
- Rust source repository: <https://github.com/astral-sh/ruff/tree/3cb09eba689ebb49e799131092121928cc789c18/crates>

## Revised concept summary

TyO3 is a Python-native semantic interface to ty's Rust engine.

It should expose a `TyProject` object backed by `ty_project::ProjectDatabase`, with Pydantic models for diagnostics, symbols, definitions, references, hover, tokens, and hierarchy. It should call selected `ty_ide` operations through a Rust PyO3 facade, not shell out to ty's CLI. It should pin ty/Ruff versions and provide a stable Python API over a controlled Rust adapter layer.

The actual `ty_ide` codebase in `ty` 0.0.40 already supports most of the desired v0.1 operations and several post-v0.1 operations. The exceptions and caveats are documented above: call hierarchy is not currently confirmed as a `ty_ide` export, type hierarchy is split into prepare/supertypes/subtypes functions, and `all_symbols` requires an importing context internally. The hardest parts are not basic semantic query availability; they are API stabilization, path resolution, coordinate conversion, structured DTO conversion, incremental in-memory updates, and efficient whole-project reference indexing.

The initial goal is not full parity with ty's LSP server or Scippy's SCIP needs. The initial goal is a clean, general-purpose semantic analysis library for Python codebases.

Once that core exists, TyO3 can support SCIP generation, code graph construction, incremental analysis, documentation tooling, refactoring workflows, and AI-oriented codebase context extraction.
