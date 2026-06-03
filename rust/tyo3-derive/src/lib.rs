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

    // Collect all public fields (which are the #[pyo3(get)] fields — pyclass
    // consumes those helper attributes before derive macros run, so we use
    // visibility as the proxy: only pub fields are accessible from Python).
    let field_entries: Vec<_> = fields
        .iter()
        .filter(|f| is_pub(f))
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

/// Check if a field is `pub` (publicly accessible from Python via pyo3(get)).
/// We use visibility rather than checking `#[pyo3(get)]` directly because
/// `#[pyclass]` (an attribute proc macro) consumes helper attributes like
/// `#[pyo3(get)]` before derive macros run, making them invisible here.
fn is_pub(field: &syn::Field) -> bool {
    matches!(field.vis, syn::Visibility::Public(_))
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
