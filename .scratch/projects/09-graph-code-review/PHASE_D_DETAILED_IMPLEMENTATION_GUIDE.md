# Phase D — Detailed Implementation Guide

**Goal:** Replace the fragile, slow Python-side `dir()` introspection and suffix-matching enum detection with a clean, zero-maintenance Rust-driven architecture. Every PyO3 type crossing the FFI boundary explicitly declares its own structure, eliminating all heuristics.

**Architecture overview:**

```
┌─────────────────────────────────────────────────────────┐
│  Rust (single source of truth)                          │
│                                                         │
│  #[derive(PyFields)]        ← proc macro reads          │
│  struct SymbolDto {            #[pyo3(get)] fields       │
│      #[pyo3(get)]              and generates             │
│      name: String,             __fields__ classattr      │
│      ...                       with type tags            │
│  }                                                       │
│                                                         │
│  _TYO3_ENUM_TYPES = [...]   ← module-level list of      │
│                                all enum types             │
└────────────────────────┬────────────────────────────────┘
                         │ FFI boundary
┌────────────────────────▼────────────────────────────────┐
│  Python (_to_python)                                     │
│                                                         │
│  obj.__fields__  → iterate only real fields              │
│  type tags       → skip recursion for primitives         │
│  _TYO3_ENUM_TYPES → direct set lookup, no name matching │
└─────────────────────────────────────────────────────────┘
```

**Phases within this guide:**

| Step | What | Where |
|------|------|-------|
| D.1 | Create `tyo3-derive` proc-macro crate | `rust/tyo3-derive/` |
| D.2 | Apply `#[derive(PyFields)]` to all 14 struct DTOs | `rust/src/dto/*.rs` |
| D.3 | Add `_TYO3_ENUM_TYPES` module attribute | `rust/src/lib.rs` |
| D.4 | Rewrite `_to_python()` to use `__fields__` + type tags | `src/tyo3/rust_project.py` |
| D.5 | Rewrite `_build_enum_cache()` to use `_TYO3_ENUM_TYPES` | `src/tyo3/rust_project.py` |
| D.6 | Add validation tests | `src/tyo3/tests/` |
| D.7 | Clean up dead code | `src/tyo3/rust_project.py` |

---

## D.1 — Create the `tyo3-derive` proc-macro crate

The proc macro reads `#[pyo3(get)]` annotations from struct fields and generates a `__fields__` classattr that returns typed field metadata. This is the "do it once, never maintain it" solution — adding a new `#[pyo3(get)]` field to any DTO automatically updates `__fields__` at compile time.

### D.1.1 Create the crate directory and Cargo.toml

```bash
mkdir -p rust/tyo3-derive/src
```

Create `rust/tyo3-derive/Cargo.toml`:

```toml
[package]
name = "tyo3-derive"
version = "0.1.0"
edition = "2021"

[lib]
proc-macro = true

[dependencies]
syn = { version = "2", features = ["full"] }
quote = "1"
proc-macro2 = "1"
```

### D.1.2 Implement the `PyFields` derive macro

Create `rust/tyo3-derive/src/lib.rs`:

```rust
//! Derive macro for generating PyO3 `__fields__` classattrs.
//!
//! Applied to any `#[pyclass(frozen)]` struct, `#[derive(PyFields)]` reads
//! fields annotated with `#[pyo3(get)]` and generates a `#[classattr]`
//! method returning `Vec<(&str, &str)>` — field name + type tag pairs.
//!
//! Type tags let the Python-side `_to_python()` skip recursive conversion
//! for primitive types:
//!
//! | Rust type              | Tag          | Python treatment        |
//! |------------------------|--------------|-------------------------|
//! | `String`               | `"str"`      | passthrough             |
//! | `bool`                 | `"bool"`     | passthrough             |
//! | `u32`, `u64`, `i32`... | `"int"`      | passthrough             |
//! | `f32`, `f64`           | `"float"`    | passthrough             |
//! | `Option<T>`            | `"opt:<tag>"`| None passthrough, else recurse by inner tag |
//! | `Vec<T>`               | `"list:<tag>"`| recurse each element by inner tag |
//! | any other struct/enum  | `"obj"`      | full recursive conversion |

use proc_macro::TokenStream;
use quote::quote;
use syn::{parse_macro_input, Data, DeriveInput, Fields, Type, PathSegment};

#[proc_macro_derive(PyFields)]
pub fn derive_py_fields(input: TokenStream) -> TokenStream {
    let input = parse_macro_input!(input as DeriveInput);
    let name = &input.ident;

    let fields = match &input.data {
        Data::Struct(data) => match &data.fields {
            Fields::Named(fields) => &fields.named,
            _ => panic!("PyFields only supports structs with named fields"),
        },
        _ => panic!("PyFields can only be derived for structs"),
    };

    // Collect fields that have #[pyo3(get)]
    let field_entries: Vec<_> = fields
        .iter()
        .filter(|f| has_pyo3_get(f))
        .map(|f| {
            let field_name = f.ident.as_ref().unwrap().to_string();
            let type_tag = rust_type_to_tag(&f.ty);
            quote! { (#field_name, #type_tag) }
        })
        .collect();

    let expanded = quote! {
        #[pymethods]
        impl #name {
            /// Field metadata for Python-side conversion.
            /// Each entry is `(field_name, type_tag)`.
            #[classattr]
            fn __fields__() -> Vec<(&'static str, &'static str)> {
                vec![#(#field_entries),*]
            }
        }
    };

    TokenStream::from(expanded)
}

/// Check if a field has `#[pyo3(get)]` attribute.
fn has_pyo3_get(field: &syn::Field) -> bool {
    field.attrs.iter().any(|attr| {
        if !attr.path().is_ident("pyo3") {
            return false;
        }
        // Parse the attribute tokens to check for "get"
        let tokens = attr.meta.require_list().ok().map(|list| {
            list.tokens.to_string()
        });
        matches!(tokens, Some(t) if t.contains("get"))
    })
}

/// Map a Rust type to a type tag string for Python-side dispatch.
fn rust_type_to_tag(ty: &Type) -> String {
    match ty {
        Type::Path(type_path) => {
            let segment = type_path.path.segments.last().unwrap();
            segment_to_tag(segment)
        }
        _ => "obj".to_string(),
    }
}

fn segment_to_tag(segment: &PathSegment) -> String {
    let name = segment.ident.to_string();
    match name.as_str() {
        "String" => "str".to_string(),
        "bool" => "bool".to_string(),
        "u8" | "u16" | "u32" | "u64" | "usize"
        | "i8" | "i16" | "i32" | "i64" | "isize" => "int".to_string(),
        "f32" | "f64" => "float".to_string(),
        "Option" => {
            let inner = extract_generic_arg(segment);
            format!("opt:{}", inner)
        }
        "Vec" => {
            let inner = extract_generic_arg(segment);
            format!("list:{}", inner)
        }
        _ => "obj".to_string(),
    }
}

/// Extract the type tag of the first generic argument (e.g., `T` from `Option<T>`).
fn extract_generic_arg(segment: &PathSegment) -> String {
    match &segment.arguments {
        syn::PathArguments::AngleBracketed(args) => {
            if let Some(syn::GenericArgument::Type(inner_ty)) = args.args.first() {
                rust_type_to_tag(inner_ty)
            } else {
                "obj".to_string()
            }
        }
        _ => "obj".to_string(),
    }
}
```

### D.1.3 What the macro generates — concrete examples

For `PositionDto`:

```rust
// Input:
#[derive(PyFields)]
#[pyclass(name = "NativePosition", frozen, module = "tyo3._native_impl")]
pub struct PositionDto {
    #[pyo3(get)]
    pub line: u32,
    #[pyo3(get)]
    pub column: u32,
}

// Generated:
#[pymethods]
impl PositionDto {
    #[classattr]
    fn __fields__() -> Vec<(&'static str, &'static str)> {
        vec![("line", "int"), ("column", "int")]
    }
}
```

For `DiagnosticDto`:

```rust
// Input:
#[derive(PyFields)]
pub struct DiagnosticDto {
    #[pyo3(get)]
    pub file: Option<String>,        // → ("file", "opt:str")
    #[pyo3(get)]
    pub range: Option<RangeDto>,     // → ("range", "opt:obj")
    #[pyo3(get)]
    pub severity: SeverityDto,       // → ("severity", "obj")  (enum, handled by _is_native_enum)
    #[pyo3(get)]
    pub code: Option<String>,        // → ("code", "opt:str")
    #[pyo3(get)]
    pub message: String,             // → ("message", "str")
    #[pyo3(get)]
    pub details: Vec<String>,        // → ("details", "list:str")
}

// Generated:
#[pymethods]
impl DiagnosticDto {
    #[classattr]
    fn __fields__() -> Vec<(&'static str, &'static str)> {
        vec![
            ("file", "opt:str"),
            ("range", "opt:obj"),
            ("severity", "obj"),
            ("code", "opt:str"),
            ("message", "str"),
            ("details", "list:str"),
        ]
    }
}
```

### D.1.4 Complete type tag reference

Generated from the actual Rust source. Every `#[pyo3(get)]` field across all 14 struct DTOs:

| DTO | Field | Rust Type | Tag |
|-----|-------|-----------|-----|
| **PositionDto** | `line` | `u32` | `int` |
| | `column` | `u32` | `int` |
| **RangeDto** | `start` | `PositionDto` | `obj` |
| | `end` | `PositionDto` | `obj` |
| **FileRangeDto** | `path` | `String` | `str` |
| | `range` | `RangeDto` | `obj` |
| **SymbolDto** | `name` | `String` | `str` |
| | `qualified_name` | `Option<String>` | `opt:str` |
| | `kind` | `SymbolKindDto` | `obj` |
| | `location` | `FileRangeDto` | `obj` |
| | `selection_range` | `Option<RangeDto>` | `opt:obj` |
| | `container_name` | `Option<String>` | `opt:str` |
| | `deprecated` | `bool` | `bool` |
| **DiagnosticDto** | `file` | `Option<String>` | `opt:str` |
| | `range` | `Option<RangeDto>` | `opt:obj` |
| | `severity` | `SeverityDto` | `obj` |
| | `code` | `Option<String>` | `opt:str` |
| | `message` | `String` | `str` |
| | `details` | `Vec<String>` | `list:str` |
| **CheckResultDto** | `diagnostics` | `Vec<DiagnosticDto>` | `list:obj` |
| | `files_checked` | `Option<u32>` | `opt:int` |
| | `elapsed_ms` | `Option<u64>` | `opt:int` |
| **DefinitionTargetDto** | `path` | `String` | `str` |
| | `range` | `RangeDto` | `obj` |
| | `selection_range` | `Option<RangeDto>` | `opt:obj` |
| | `symbol` | `Option<SymbolDto>` | `opt:obj` |
| | `module_name` | `Option<String>` | `opt:str` |
| **ReferenceDto** | `path` | `String` | `str` |
| | `range` | `RangeDto` | `obj` |
| | `kind` | `ReferenceKindDto` | `obj` |
| **HoverContentDto** | `kind` | `HoverContentKindDto` | `obj` |
| | `value` | `String` | `str` |
| **HoverDto** | `location` | `FileRangeDto` | `obj` |
| | `contents` | `Vec<HoverContentDto>` | `list:obj` |
| **TypeHierarchyItemDto** | `name` | `String` | `str` |
| | `detail` | `Option<String>` | `opt:str` |
| | `path` | `String` | `str` |
| | `full_range` | `RangeDto` | `obj` |
| | `selection_range` | `RangeDto` | `obj` |
| **TypeHierarchyDto** | `item` | `TypeHierarchyItemDto` | `obj` |
| | `supertypes` | `Vec<TypeHierarchyItemDto>` | `list:obj` |
| | `subtypes` | `Vec<TypeHierarchyItemDto>` | `list:obj` |
| **NameOccurrenceDto** | `range` | `RangeDto` | `obj` |
| | `target_file` | `Option<String>` | `opt:str` |
| | `target_name` | `Option<String>` | `opt:str` |
| | `target_qualified_name` | `Option<String>` | `opt:str` |
| | `role` | `ReferenceRoleDto` | `obj` |
| **SemanticTokenDto** | `range` | `RangeDto` | `obj` |
| | `token_type` | `SemanticTokenTypeDto` | `obj` |
| | `modifiers` | `Vec<SemanticTokenModifierDto>` | `list:obj` |

**Observation:** 18 of 56 total fields are primitives (`str`, `int`, `bool`) or optional primitives (`opt:str`, `opt:int`) — roughly a third of all field accesses can skip recursive `_to_python()` entirely. Another 4 are `list:str`, bringing it close to 40%. This is a meaningful hot-path optimization.

---

## D.2 — Apply `#[derive(PyFields)]` to all 14 struct DTOs

### D.2.1 Add `tyo3-derive` as a dependency

**File:** `rust/Cargo.toml`

Add under `[dependencies]`:

```toml
tyo3-derive = { path = "tyo3-derive" }
```

### D.2.2 Add derive to each struct DTO

For each of the 14 struct DTOs listed below, add `PyFields` to the derive list and add the `use tyo3_derive::PyFields;` import to the file.

**Important constraint:** The macro generates a new `#[pymethods] impl` block. PyO3 allows multiple `#[pymethods]` blocks on the same struct, so this works alongside existing `#[pymethods]` blocks that contain `__new__`, `__repr__`, etc. No existing code needs to change.

Here are all 14 structs, by file:

**`rust/src/dto/coordinates.rs`** — add `use tyo3_derive::PyFields;` at top

| Struct | Line | Current derives | Add |
|--------|------|-----------------|-----|
| `PositionDto` | 7 | `Debug, Clone, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize` | `PyFields` |
| `RangeDto` | 40 | `Debug, Clone, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize` | `PyFields` |
| `FileRangeDto` | 74 | `Debug, Clone, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize` | `PyFields` |

Example — `PositionDto` becomes:

```rust
#[pyclass(name = "NativePosition", frozen, module = "tyo3._native_impl")]
#[derive(Debug, Clone, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize, PyFields)]
pub struct PositionDto {
```

**`rust/src/dto/diagnostics.rs`** — add `use tyo3_derive::PyFields;` at top

| Struct | Line | Add |
|--------|------|-----|
| `DiagnosticDto` | 34 | `PyFields` |

Note: `SeverityDto` is an **enum** — do NOT apply `PyFields` to enums. Enums are handled by D.3.

**`rust/src/dto/symbols.rs`** — add `use tyo3_derive::PyFields;` at top

| Struct | Line | Add |
|--------|------|-----|
| `SymbolDto` | 52 | `PyFields` |

**`rust/src/dto/navigation.rs`** — add `use tyo3_derive::PyFields;` at top

| Struct | Line | Add |
|--------|------|-----|
| `DefinitionTargetDto` | 6 | `PyFields` |
| `ReferenceDto` | 73 | `PyFields` |

**`rust/src/dto/hover.rs`** — add `use tyo3_derive::PyFields;` at top

| Struct | Line | Add |
|--------|------|-----|
| `HoverContentDto` | 36 | `PyFields` |
| `HoverDto` | 61 | `PyFields` |

**`rust/src/dto/hierarchy.rs`** — add `use tyo3_derive::PyFields;` at top

| Struct | Line | Add |
|--------|------|-----|
| `TypeHierarchyItemDto` | 7 | `PyFields` |
| `TypeHierarchyDto` | 23 | `PyFields` |

**`rust/src/dto/occurrences.rs`** — add `use tyo3_derive::PyFields;` at top

| Struct | Line | Add |
|--------|------|-----|
| `NameOccurrenceDto` | 39 | `PyFields` |

**`rust/src/dto/tokens.rs`** — add `use tyo3_derive::PyFields;` at top

| Struct | Line | Add |
|--------|------|-----|
| `SemanticTokenDto` | 83 | `PyFields` |

**`rust/src/dto/mod.rs`** — add `use tyo3_derive::PyFields;` at top

| Struct | Line | Add |
|--------|------|-----|
| `CheckResultDto` | 34 | `PyFields` |

### D.2.3 Verify the build

```bash
devenv shell -- build
```

This compiles the proc macro crate first, then the main crate. Every struct DTO now has a `__fields__` classattr accessible from Python.

### D.2.4 Quick smoke test from Python

```bash
devenv shell -- python -c "
from tyo3._native_impl import NativePosition, NativeSymbol, NativeDiagnostic
print('Position:', NativePosition.__fields__)
print('Symbol:', NativeSymbol.__fields__)
print('Diagnostic:', NativeDiagnostic.__fields__)
"
```

Expected output:

```
Position: [('line', 'int'), ('column', 'int')]
Symbol: [('name', 'str'), ('qualified_name', 'opt:str'), ('kind', 'obj'), ('location', 'obj'), ('selection_range', 'opt:obj'), ('container_name', 'opt:str'), ('deprecated', 'bool')]
Diagnostic: [('file', 'opt:str'), ('range', 'opt:obj'), ('severity', 'obj'), ('code', 'opt:str'), ('message', 'str'), ('details', 'list:str')]
```

---

## D.3 — Add `_TYO3_ENUM_TYPES` module attribute

### D.3.1 Modify `lib.rs`

**File:** `rust/src/lib.rs`

After the existing `m.add_class` registrations (line 48) and before the exception types (line 50), add:

```rust
    // Enum type registry — Python uses this to identify PyO3 enum variants
    // for str() conversion instead of dir()-based introspection.
    // When adding a new PyO3 enum, add it here too.
    m.add("_TYO3_ENUM_TYPES", vec![
        m.getattr("NativeSymbolKind")?,
        m.getattr("NativeSeverity")?,
        m.getattr("NativeReferenceKind")?,
        m.getattr("NativeReferenceRole")?,
        m.getattr("NativeSemanticTokenType")?,
        m.getattr("NativeSemanticTokenModifier")?,
        m.getattr("NativeHoverContentKind")?,
    ])?;
```

That's all 7 enum types (not 8 — `ReferenceRoleDto` is registered as `NativeReferenceRole`, there are exactly 7 distinct Python-visible enum classes).

The complete enum inventory, verified against `lib.rs` lines 31–48:

| Rust enum | Python name | Source file |
|-----------|-------------|-------------|
| `SymbolKindDto` | `NativeSymbolKind` | `symbols.rs` |
| `SeverityDto` | `NativeSeverity` | `diagnostics.rs` |
| `ReferenceKindDto` | `NativeReferenceKind` | `navigation.rs` |
| `ReferenceRoleDto` | `NativeReferenceRole` | `occurrences.rs` |
| `SemanticTokenTypeDto` | `NativeSemanticTokenType` | `tokens.rs` |
| `SemanticTokenModifierDto` | `NativeSemanticTokenModifier` | `tokens.rs` |
| `HoverContentKindDto` | `NativeHoverContentKind` | `hover.rs` |

### D.3.2 Rebuild

```bash
devenv shell -- build
```

### D.3.3 Verify from Python

```bash
devenv shell -- python -c "
from tyo3 import _native_impl as _native
print('Enum types:', _native._TYO3_ENUM_TYPES)
print('Count:', len(_native._TYO3_ENUM_TYPES))
"
```

Expected: a list of 7 type objects.

---

## D.4 — Rewrite `_to_python()` to use `__fields__` + type tags

### D.4.1 The new `_to_python()` implementation

**File:** `src/tyo3/rust_project.py`

Replace lines ~129–159 (the entire `_to_python` function) with:

```python
def _to_python(obj: Any) -> Any:
    """Recursively convert PyO3 native objects to plain Python types.

    Uses ``__fields__`` classattrs (generated by ``#[derive(PyFields)]``)
    for struct conversion and ``_TYO3_ENUM_TYPES`` for enum detection.
    Type tags on fields allow skipping recursion for primitives.

    Type tag semantics:
        "str", "int", "float", "bool" → passthrough (no recursion)
        "obj"                         → full recursive _to_python()
        "opt:<tag>"                   → None passthrough, else convert by <tag>
        "list:<tag>"                  → convert each element by <tag>
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj

    if isinstance(obj, (list, tuple)):
        return [_to_python(item) for item in obj]

    if _is_native_enum(obj):
        return str(obj)

    # PyO3 frozen struct with __fields__ classattr
    fields = getattr(type(obj), "__fields__", None)
    if fields is not None:
        return _convert_struct(obj, fields)

    # Unreachable for known DTOs. Log a warning so new unregistered types
    # are caught during development rather than silently degrading.
    logger.warning(
        "Unknown native type %s — falling back to dir() introspection. "
        "Add #[derive(PyFields)] to the Rust struct.",
        type(obj).__name__,
    )
    return _convert_struct_fallback(obj)


def _convert_struct(
    obj: Any, fields: list[tuple[str, str]]
) -> dict[str, Any]:
    """Convert a PyO3 struct using its __fields__ metadata."""
    result: dict[str, Any] = {}
    for name, tag in fields:
        val = getattr(obj, name)
        result[name] = _convert_by_tag(val, tag)
    return result


def _convert_by_tag(val: Any, tag: str) -> Any:
    """Convert a value according to its type tag."""
    # Primitives — no recursion needed
    if tag in _PASSTHROUGH_TAGS:
        return val

    # Optional wrapper
    if tag.startswith("opt:"):
        if val is None:
            return None
        return _convert_by_tag(val, tag[4:])

    # List wrapper
    if tag.startswith("list:"):
        inner_tag = tag[5:]
        if inner_tag in _PASSTHROUGH_TAGS:
            # Fast path: list of primitives, return as-is
            # (PyO3 already returns Python list of str/int/etc.)
            return list(val) if not isinstance(val, list) else val
        return [_convert_by_tag(item, inner_tag) for item in val]

    # "obj" — full recursive conversion (struct or enum)
    return _to_python(val)


# Tags that need no conversion
_PASSTHROUGH_TAGS = frozenset({"str", "int", "float", "bool"})


def _convert_struct_fallback(obj: Any) -> dict[str, Any]:
    """Legacy fallback for structs without __fields__. Logs warning on first use."""
    result: dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            val = getattr(obj, name)
            if not callable(val):
                result[name] = _to_python(val)
        except Exception:
            pass
    return result
```

### D.4.2 Key design decisions

**Why `type(obj).__fields__` not `obj.__fields__`:**
`#[classattr]` creates a class-level attribute. Using `type(obj).__fields__` is explicit about this — it won't pick up instance attributes. It also avoids an extra descriptor protocol invocation.

**Why `_convert_by_tag` instead of always recursing:**
The type tag dispatch eliminates the `isinstance()` check cascade on primitive values. For a `SymbolDto` with 7 fields, 4 are `str`/`opt:str`/`bool` — those skip `_to_python()` entirely. Over thousands of symbols during `build()`, this adds up.

**Why keep the fallback:**
During development, someone might add a new DTO struct and forget `#[derive(PyFields)]`. The fallback prevents a crash, and the `logger.warning()` makes it immediately visible. In production this path should never execute — if it does, it's a bug to fix.

**Why no `except Exception: pass`:**
With `__fields__`, every field name is guaranteed to exist on the frozen struct (the getter is compiled by PyO3). If `getattr` fails, something is fundamentally broken and should raise, not be silently swallowed.

---

## D.5 — Rewrite `_build_enum_cache()` to use `_TYO3_ENUM_TYPES`

### D.5.1 The new implementation

**File:** `src/tyo3/rust_project.py`

Replace lines ~94–126 (the `_ENUM_TYPES` globals, `_build_enum_cache`, and `_is_native_enum`) with:

```python
# ── Conversion helpers ────────────────────────────────────────────────────
# PyO3 enums (defined with #[pyclass(eq)]) are not Python str subclasses,
# so Pydantic StrEnum fields reject them.  PyO3 frozen structs lack
# __dict__, so Pydantic v2.13 from_attributes=True rejects them.
# We recursively convert the PyO3 object graph to plain Python types
# (dict, list, str) that Pydantic validates natively.


# Cache of known PyO3 enum types from the native extension.
# Populated from the Rust-side _TYO3_ENUM_TYPES module attribute.
_ENUM_TYPES: frozenset[type] = frozenset()
_ENUM_TYPES_BUILT: bool = False


def _build_enum_cache() -> None:
    """Cache the PyO3 enum types from the native extension module."""
    global _ENUM_TYPES, _ENUM_TYPES_BUILT
    if _ENUM_TYPES_BUILT or _native is None:
        return
    enum_list = getattr(_native, "_TYO3_ENUM_TYPES", None)
    if enum_list is not None:
        _ENUM_TYPES = frozenset(enum_list)
    else:
        logger.warning(
            "_TYO3_ENUM_TYPES not found on native module. "
            "Enum detection will not work. Rebuild the Rust extension."
        )
        _ENUM_TYPES = frozenset()
    _ENUM_TYPES_BUILT = True


def _is_native_enum(obj: Any) -> bool:
    """Return True if *obj* is a PyO3 native enum variant."""
    _build_enum_cache()
    return type(obj) in _ENUM_TYPES
```

### D.5.2 What this removes

- All suffix-matching heuristics (`name.endswith("Kind")`, `name.endswith("Type")`, etc.)
- The `for name in dir(_native)` iteration
- The hardcoded `name == "NativeSeverity"` special case
- The mutable `set()` — replaced with `frozenset()` for immutability after initialization

### D.5.3 What this gains

- **Zero drift risk from naming conventions.** Adding a new enum `FooBarBaz` (no matching suffix) just requires adding it to the `_TYO3_ENUM_TYPES` vec in `lib.rs` — right next to the `m.add_class::<FooBarBaz>()` call.
- **Faster cache build.** Direct list assignment vs. iterating `dir(_native)` (which returns ~30+ entries including exceptions, the project class, and struct DTOs).
- **Immutable after init.** `frozenset` makes it clear the set never changes after build.

---

## D.6 — Add validation tests

### D.6.1 Test: all struct DTOs have `__fields__`

**File:** `src/tyo3/tests/test_native_bridge.py` (new file)

```python
"""Tests for the Rust-Python bridge layer (native type conversion)."""

from __future__ import annotations

import pytest

try:
    from tyo3 import _native_impl as _native
    from tyo3 import _HAS_NATIVE
except ImportError:
    _HAS_NATIVE = False
    _native = None

needs_native = pytest.mark.skipif(
    not _HAS_NATIVE, reason="Rust native extension not built"
)


# All struct DTOs that should have __fields__
EXPECTED_STRUCT_TYPES = [
    "NativePosition",
    "NativeRange",
    "NativeFileRange",
    "NativeSymbol",
    "NativeDiagnostic",
    "NativeCheckResult",
    "NativeDefinitionTarget",
    "NativeReference",
    "NativeHoverContent",
    "NativeHover",
    "NativeTypeHierarchyItem",
    "NativeTypeHierarchy",
    "NativeNameOccurrence",
    "NativeSemanticToken",
]

# All enum types that should be in _TYO3_ENUM_TYPES
EXPECTED_ENUM_TYPES = [
    "NativeSymbolKind",
    "NativeSeverity",
    "NativeReferenceKind",
    "NativeReferenceRole",
    "NativeSemanticTokenType",
    "NativeSemanticTokenModifier",
    "NativeHoverContentKind",
]


@needs_native
class TestFieldsClassattr:
    """Verify every struct DTO exposes __fields__ with correct type tags."""

    @pytest.mark.parametrize("type_name", EXPECTED_STRUCT_TYPES)
    def test_struct_has_fields(self, type_name: str) -> None:
        cls = getattr(_native, type_name)
        fields = cls.__fields__
        assert isinstance(fields, list), f"{type_name}.__fields__ should be a list"
        assert len(fields) > 0, f"{type_name}.__fields__ should not be empty"
        for entry in fields:
            assert isinstance(entry, tuple) and len(entry) == 2, (
                f"{type_name}.__fields__ entries should be (name, tag) tuples"
            )
            name, tag = entry
            assert isinstance(name, str) and isinstance(tag, str)

    @pytest.mark.parametrize("type_name", EXPECTED_STRUCT_TYPES)
    def test_fields_match_getattrs(self, type_name: str) -> None:
        """Every field listed in __fields__ must be gettable on instances.

        We don't construct instances here — just verify field names are
        valid Python identifiers and tags are from the known set.
        """
        cls = getattr(_native, type_name)
        valid_tag_prefixes = {"str", "int", "float", "bool", "obj", "opt:", "list:"}
        for name, tag in cls.__fields__:
            assert name.isidentifier(), f"{type_name}.{name} is not a valid identifier"
            assert any(tag == p or tag.startswith(p) for p in valid_tag_prefixes), (
                f"{type_name}.{name} has unknown tag: {tag}"
            )

    def test_no_struct_missing_fields(self) -> None:
        """Catch new struct DTOs that forgot #[derive(PyFields)].

        Any frozen pyclass in the module that has #[pyo3(get)] fields
        should have __fields__. This test detects omissions.
        """
        known = set(EXPECTED_STRUCT_TYPES) | set(EXPECTED_ENUM_TYPES)
        # Also exclude non-DTO types
        skip = {"TyProject", "ProjectClosedError", "PathResolutionError",
                "PositionError", "AnalysisError"}
        for name in dir(_native):
            if name.startswith("_"):
                continue
            obj = getattr(_native, name)
            if not isinstance(obj, type):
                continue
            if name in skip:
                continue
            if name not in known:
                # Unknown type — should have __fields__ if it's a struct DTO
                assert hasattr(obj, "__fields__"), (
                    f"New native type {name} found without __fields__. "
                    f"Add #[derive(PyFields)] to its Rust definition and add it "
                    f"to EXPECTED_STRUCT_TYPES in this test."
                )


@needs_native
class TestEnumTypeRegistry:
    """Verify the _TYO3_ENUM_TYPES module attribute."""

    def test_enum_types_exists(self) -> None:
        assert hasattr(_native, "_TYO3_ENUM_TYPES"), (
            "_TYO3_ENUM_TYPES not found on native module"
        )

    def test_enum_types_complete(self) -> None:
        enum_types = set(_native._TYO3_ENUM_TYPES)
        for name in EXPECTED_ENUM_TYPES:
            cls = getattr(_native, name)
            assert cls in enum_types, (
                f"{name} missing from _TYO3_ENUM_TYPES in lib.rs"
            )

    def test_enum_types_count(self) -> None:
        assert len(_native._TYO3_ENUM_TYPES) == len(EXPECTED_ENUM_TYPES), (
            f"Expected {len(EXPECTED_ENUM_TYPES)} enum types, "
            f"got {len(_native._TYO3_ENUM_TYPES)}. Update this test if a "
            f"new enum was intentionally added."
        )

    def test_no_enum_missing(self) -> None:
        """Catch new enum types that were added to the module but not to
        _TYO3_ENUM_TYPES."""
        enum_types = set(_native._TYO3_ENUM_TYPES)
        skip = {"TyProject", "ProjectClosedError", "PathResolutionError",
                "PositionError", "AnalysisError", "_TYO3_ENUM_TYPES"}
        for name in dir(_native):
            if name.startswith("_"):
                continue
            obj = getattr(_native, name)
            if not isinstance(obj, type):
                continue
            if name in skip:
                continue
            # If it has __str__ but NOT __fields__, it's likely an enum
            if hasattr(obj, "__fields__"):
                continue  # It's a struct
            if obj not in enum_types:
                # Check if it smells like an enum (has variants accessible via __members__ or similar)
                pytest.fail(
                    f"Native type {name} is not in _TYO3_ENUM_TYPES and has no "
                    f"__fields__. If it's an enum, add it to _TYO3_ENUM_TYPES in "
                    f"lib.rs and EXPECTED_ENUM_TYPES in this test."
                )


@needs_native
class TestToPythonConversion:
    """Integration tests for _to_python with real native objects."""

    def test_position_roundtrip(self) -> None:
        from tyo3.rust_project import _to_python
        pos = _native.NativePosition(line=1, column=5)
        result = _to_python(pos)
        assert result == {"line": 1, "column": 5}

    def test_range_roundtrip(self) -> None:
        from tyo3.rust_project import _to_python
        r = _native.NativeRange(
            start=_native.NativePosition(1, 1),
            end=_native.NativePosition(1, 10),
        )
        result = _to_python(r)
        assert result == {
            "start": {"line": 1, "column": 1},
            "end": {"line": 1, "column": 10},
        }

    def test_enum_becomes_string(self) -> None:
        from tyo3.rust_project import _to_python
        kind = _native.NativeSymbolKind.Function
        result = _to_python(kind)
        assert result == "function"

    def test_optional_none(self) -> None:
        from tyo3.rust_project import _to_python
        diag = _native.NativeDiagnostic(
            severity=_native.NativeSeverity.Error,
            message="test",
        )
        result = _to_python(diag)
        assert result["file"] is None
        assert result["code"] is None
        assert result["message"] == "test"
        assert result["severity"] == "error"

    def test_list_field(self) -> None:
        from tyo3.rust_project import _to_python
        diag = _native.NativeDiagnostic(
            severity=_native.NativeSeverity.Warning,
            message="test",
            details=["detail1", "detail2"],
        )
        result = _to_python(diag)
        assert result["details"] == ["detail1", "detail2"]
```

---

## D.7 — Clean up dead code

After all the above is working and tests pass:

### D.7.1 Remove from `rust_project.py`

1. **Delete the old `_convert_struct_fallback` function** once you've confirmed no warnings are being logged. Alternatively, keep it but annotate with a comment that it's the safety net — your choice depending on how confident you are that all DTOs have `#[derive(PyFields)]`.

2. **Delete the old comment block** about suffix conventions (lines ~108–109 in the current code: `# PyO3 enums have names ending in 'Kind', 'Type', 'Modifier'`).

### D.7.2 Remove from `rust/src/dto/mod.rs`

The `BackendInfoDto` struct (lines 22–30) is `#[allow(dead_code)]` and not registered with the Python module. It has no `#[pyclass]` annotation. If it's still not wired to anything, delete it. If you want to keep the shape for future use, move it to a `_draft.rs` file — dead code in production modules is noise.

---

## Appendix A: Adding a new DTO in the future

With this architecture in place, the process for adding a new type is:

**For a new struct DTO:**

1. Define the struct in `rust/src/dto/` with `#[pyclass(..., frozen)]` and `#[pyo3(get)]` fields
2. Add `PyFields` to the derive list: `#[derive(Debug, Clone, ..., PyFields)]`
3. Register it in `lib.rs`: `m.add_class::<NewDto>()?;`
4. Add it to `EXPECTED_STRUCT_TYPES` in `test_native_bridge.py`
5. Done. `__fields__` is auto-generated. `_to_python()` handles it automatically.

**For a new enum DTO:**

1. Define the enum with `#[pyclass(eq)]` and `__str__`/`__repr__` methods
2. Register it in `lib.rs`: `m.add_class::<NewEnum>()?;`
3. Add it to the `_TYO3_ENUM_TYPES` vec in `lib.rs` (same function, 2 lines below)
4. Add it to `EXPECTED_ENUM_TYPES` in `test_native_bridge.py`
5. Done. `_is_native_enum()` picks it up automatically.

If you forget step 2 or 3, `test_no_struct_missing_fields` or `test_no_enum_missing` will catch it.

---

## Appendix B: Proc macro edge cases and testing

### B.1 Fields without `#[pyo3(get)]`

Some structs might have internal fields (no `#[pyo3(get)]`). The macro correctly skips these — only fields with the `#[pyo3(get)]` attribute appear in `__fields__`. Currently none of the 14 DTOs have non-get fields, but the macro handles this case.

### B.2 Testing the proc macro itself

The proc macro is tested implicitly through the integration tests in D.6 — if the macro generates incorrect code, the Rust compiler will reject it (wrong field names → compile error) or the Python tests will fail (wrong tags → conversion error).

For explicit macro-level tests, add `rust/tyo3-derive/tests/`:

```rust
// rust/tyo3-derive/tests/derive_test.rs
use pyo3::prelude::*;
use tyo3_derive::PyFields;

#[pyclass(frozen)]
#[derive(PyFields)]
struct TestStruct {
    #[pyo3(get)]
    pub name: String,
    #[pyo3(get)]
    pub count: u32,
    #[pyo3(get)]
    pub tags: Vec<String>,
    #[pyo3(get)]
    pub parent: Option<String>,
    // No #[pyo3(get)] — should be excluded
    pub internal: bool,
}

#[test]
fn test_fields_generated() {
    let fields = TestStruct::__fields__();
    assert_eq!(fields, vec![
        ("name", "str"),
        ("count", "int"),
        ("tags", "list:str"),
        ("parent", "opt:str"),
    ]);
}
```

Note: This test requires a PyO3 build environment. Run it via `cargo test -p tyo3-derive`.

### B.3 Multiple `#[pymethods]` blocks

PyO3 allows multiple `#[pymethods]` blocks on the same type. The `PyFields` derive generates a separate `#[pymethods]` block from the one containing `__new__`, `__repr__`, etc. This is intentional and works correctly. The PyO3 documentation explicitly supports this pattern.

---

## Appendix C: Performance characteristics

### Before (current `dir()` approach)

For a single `SymbolDto` conversion:

```
dir(obj)           → ~30 attribute names (includes __class__, __doc__, etc.)
  filter _         → ~10 remain
  getattr() × 10   → 10 FFI calls + 10 callable() checks
  _to_python() × 7 → recursive for each non-callable field
```

Total: ~17 Python→Rust FFI transitions + ~10 `callable()` checks + 1 `dir()` call.

### After (`__fields__` + type tags)

```
type(obj).__fields__  → 1 class attribute lookup (cached by Python)
  getattr() × 7       → 7 FFI calls (only real fields)
  tag dispatch × 7     → 4 passthrough (str/bool), 3 recursive
```

Total: ~10 Python→Rust FFI transitions. Zero `callable()` checks. Zero `dir()` calls. Primitives skip recursion entirely.

For deeply nested types like `CheckResultDto` (which contains `Vec<DiagnosticDto>`, each containing `Option<RangeDto>` containing `PositionDto`), the savings compound — every level of the tree gets the same tag-based fast path.

---

## Verification checklist

Run after completing all steps:

```bash
# 1. Rust builds cleanly
devenv shell -- build

# 2. Proc macro tests pass
devenv shell -- cargo test -p tyo3-derive

# 3. Python smoke test
devenv shell -- python -c "
from tyo3._native_impl import NativePosition, NativeSymbol
print('Position fields:', NativePosition.__fields__)
print('Symbol fields:', NativeSymbol.__fields__)
from tyo3._native_impl import _TYO3_ENUM_TYPES
print('Enum types:', len(_TYO3_ENUM_TYPES))
"

# 4. Full test suite
devenv shell -- tests

# 5. Verify no dir() fallback warnings during test run
devenv shell -- tests 2>&1 | grep "falling back to dir"
# (should produce no output)
```
