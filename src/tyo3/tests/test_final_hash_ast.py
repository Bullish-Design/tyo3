"""Final invariant tests — AST-canonical hashing (V2 Phase 10).

Encodes §5.6: the content hash is computed over a canonical rendering of the
entity's AST subtree — insensitive to formatting, sensitive to identifiers,
literals (including the exact text *inside* string literals), structure,
signatures, annotations, decorators, bases, and (per policy) docstrings.

As of Phase 10 the renderer walks the entity's AST subtree (not source text
line-by-line), so whitespace inside string literals is significant
(``"a  b"`` != ``"a b"``) and docstrings are identified positionally. All of
these are live regression guards.
"""

from __future__ import annotations

from pathlib import Path

from tyo3 import TyO3Session


def _open(root: Path, files: dict[str, str], *, config: str | None = None) -> TyO3Session:
    (root / "pyproject.toml").write_text("[project]\nname = 'hash'\nversion = '0.1.0'\n")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    if config is not None:
        cfg = root / ".tyo3"
        cfg.mkdir(exist_ok=True)
        (cfg / "config.toml").write_text(config)
    s = TyO3Session(str(root))
    s.sync_all()
    return s


def _hash(session, path: str, name: str, *, profile: str | None = None) -> str:
    sym = next(s for s in session.document_symbols(path) if s.name == name)
    if profile is None:
        assert sym.content_hash is not None, "entity must carry a content hash"
        return sym.content_hash
    return sym.content_hashes[profile]


def _hash_after_edit(session, path: str, name: str, new_text: str, *, profile: str | None = None) -> str:
    session.edit(path, new_text)
    return _hash(session, path, name, profile=profile)


# ── 1. Formatting-only variations hash the same ──────────────────────────────


def test_formatting_only_hashes_same(tmp_path):
    s = _open(tmp_path, {"m.py": "def foo():\n    return 1+2\n"})
    try:
        h1 = _hash(s, "m.py", "foo")
        h2 = _hash_after_edit(s, "m.py", "foo", "def foo():\n    return 1  +  2\n")
        h3 = _hash_after_edit(s, "m.py", "foo", "def foo():\n\n    return 1 + 2\n")
        assert h1 == h2 == h3, "whitespace-only changes must not change the hash"
    finally:
        s.close()


# ── 2. Whitespace inside a string literal hashes differently ─────────────────


def test_string_literal_whitespace_hashes_differently(tmp_path):
    s = _open(tmp_path, {"m.py": 'def foo():\n    return "a  b"\n'})
    try:
        h1 = _hash(s, "m.py", "foo")
        h2 = _hash_after_edit(s, "m.py", "foo", 'def foo():\n    return "a b"\n')
        assert h1 != h2, "whitespace inside a string literal is significant"
    finally:
        s.close()


# ── 3. Identifier, literal, and control-flow changes hash differently ────────


def test_meaningful_changes_hash_differently(tmp_path):
    s = _open(tmp_path, {"m.py": "def foo():\n    x = 1\n    return x\n"})
    try:
        base = _hash(s, "m.py", "foo")
        literal = _hash_after_edit(s, "m.py", "foo", "def foo():\n    x = 2\n    return x\n")
        ident = _hash_after_edit(s, "m.py", "foo", "def foo():\n    y = 2\n    return y\n")
        control = _hash_after_edit(
            s, "m.py", "foo", "def foo():\n    x = 2\n    while x:\n        return y\n"
        )
        assert base != literal, "a literal change must change the hash"
        assert literal != ident, "an identifier change must change the hash"
        assert ident != control, "a control-flow change must change the hash"
    finally:
        s.close()


# ── 4. Docstring policy is respected; non-docstrings are never stripped ───────


_PROFILES_CFG = (
    "schema_version = 1\n\n"
    "[hashing.profiles.structure]\n"
    "whitespace_insensitive = true\n"
    "normalize_trailing_commas = true\n"
    "include_comments = false\n"
    "include_docstrings = false\n\n"
    "[hashing.profiles.semantic]\n"
    "whitespace_insensitive = true\n"
    "normalize_trailing_commas = true\n"
    "include_comments = true\n"
    "include_docstrings = true\n"
)


def test_docstring_policy_and_non_docstring_strings(tmp_path):
    s = _open(
        tmp_path,
        {"m.py": 'def foo():\n    """doc A"""\n    return 1\n'},
        config=_PROFILES_CFG,
    )
    try:
        struct_a = _hash(s, "m.py", "foo", profile="structure")
        sem_a = _hash(s, "m.py", "foo", profile="semantic")

        # Change only the docstring text.
        s.edit("m.py", 'def foo():\n    """doc B"""\n    return 1\n')
        struct_b = _hash(s, "m.py", "foo", profile="structure")
        sem_b = _hash(s, "m.py", "foo", profile="semantic")

        assert struct_a == struct_b, "structure profile must exclude the docstring"
        assert sem_a != sem_b, "semantic profile must include the docstring"

        # A *non-docstring* string statement (not in docstring position) must
        # never be stripped, even under include_docstrings = false.
        s.edit("m.py", 'def foo():\n    """doc B"""\n    "side"\n    return 1\n')
        struct_c = _hash(s, "m.py", "foo", profile="structure")
        assert struct_c != struct_b, "a non-docstring string statement is significant"
    finally:
        s.close()


# ── 5. Signatures, decorators, annotations, defaults, and bases are part of the hash ─


def test_signature_surface_is_part_of_hash(tmp_path):
    s = _open(tmp_path, {"m.py": "def foo(x):\n    return x\n", "c.py": "class C(A):\n    pass\n"})
    try:
        base = _hash(s, "m.py", "foo")
        annotated = _hash_after_edit(s, "m.py", "foo", "def foo(x: int):\n    return x\n")
        defaulted = _hash_after_edit(s, "m.py", "foo", "def foo(x: int = 1):\n    return x\n")
        decorated = _hash_after_edit(
            s, "m.py", "foo", "import functools\n\n@functools.cache\ndef foo(x: int = 1):\n    return x\n"
        )
        assert base != annotated, "a parameter annotation must change the hash"
        assert annotated != defaulted, "a default value must change the hash"
        assert defaulted != decorated, "a decorator must change the hash"

        base_cls = _hash(s, "c.py", "C")
        rebased = _hash_after_edit(s, "c.py", "C", "class C(B):\n    pass\n")
        assert base_cls != rebased, "a class base must change the hash"
    finally:
        s.close()
