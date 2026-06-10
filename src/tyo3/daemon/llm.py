"""LLM seam for the daemon ``explain`` verb (proj 26 spike).

**Hermetic by default.** ``_llm`` returns a deterministic offline stub so the
test suite and the demo run with no network and no API key. The real Anthropic
call is gated behind three conditions, *all* of which must hold, or the stub is
used:

1. ``TYO3_LLM=anthropic`` in the environment,
2. ``ANTHROPIC_API_KEY`` present, and
3. the ``anthropic`` SDK importable.

This keeps the spike's tests deterministic — they assert against the stub and
never touch the network. The real path uses the current Anthropic Messages API
shape (per the ``claude-api`` skill): a cached system block + a single user
message, defaulting to a current Claude model. Override the model with
``TYO3_LLM_MODEL``.

A third **pluggable** backend lets you point the seam at any
``f(prompt, *, system) -> str`` callable: ``TYO3_LLM=callable`` +
``TYO3_LLM_CALLABLE=mod:fn``. The demo uses it for curated offline prose
(``tyo3.demo.explanations:explain``); a plugin could route it at its own model
wrapper. Same hermetic guarantee — tests leave ``TYO3_LLM`` unset (stub).
"""

from __future__ import annotations

import importlib
import os

# A current Claude model (claude-api skill). Override with ``TYO3_LLM_MODEL``.
DEFAULT_MODEL = "claude-opus-4-8"

# Prefix every stub answer so tests/humans can tell an offline answer apart from
# a real model's. The real path never emits this.
STUB_MARKER = "[tyo3-offline-stub]"


def llm_backend() -> str:
    """Which backend ``_llm`` will use right now: ``"anthropic"``, ``"callable"``,
    or ``"stub"``.

    Pure function of the environment + import availability, so handlers can
    stamp the chosen backend into the stored record without making a call."""
    sel = os.environ.get("TYO3_LLM")
    if sel == "callable":
        return "callable" if os.environ.get("TYO3_LLM_CALLABLE") else "stub"
    if sel == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return "stub"
        return "anthropic"
    return "stub"


def llm_model() -> str:
    """The model/backend label the next ``_llm`` call will use, or ``"stub"``.

    Stored on the authored ``explain`` record so a reader knows whether an
    explanation came from a real model and which one."""
    backend = llm_backend()
    if backend == "anthropic":
        return os.environ.get("TYO3_LLM_MODEL", DEFAULT_MODEL)
    if backend == "callable":
        return os.environ.get("TYO3_LLM_CALLABLE", "callable")
    return "stub"


def _llm(prompt: str, *, system: str | None = None) -> str:
    """Run *prompt* through the active backend and return the answer text."""
    backend = llm_backend()
    if backend == "anthropic":
        return _anthropic_call(prompt, system=system)
    if backend == "callable":
        return _callable_call(prompt, system=system)
    return _stub(prompt, system=system)


def _callable_call(prompt: str, *, system: str | None = None) -> str:
    """Dispatch to the ``TYO3_LLM_CALLABLE=mod:fn`` callable (the pluggable seam).

    Any failure (bad spec, import error, raising callable) degrades to the stub
    so a misconfigured plugin can never break the ``explain`` verb."""
    spec = os.environ.get("TYO3_LLM_CALLABLE", "")
    mod_name, _, fn_name = spec.partition(":")
    if not mod_name or not fn_name:
        return _stub(prompt, system=system)
    try:
        fn = getattr(importlib.import_module(mod_name), fn_name)
        return str(fn(prompt, system=system))
    except Exception:
        return _stub(prompt, system=system)


def _stub(prompt: str, *, system: str | None = None) -> str:
    """Deterministic offline answer — echoes the prompt's subject line.

    The ``explain`` handler puts ``Entity: <qualified_name> (<kind>)`` as the
    first non-blank line of the prompt, so the stub's answer references the
    entity by name without any model. Stable across runs (no randomness, no
    clock) so tests can assert on it."""
    subject = next((ln for ln in prompt.splitlines() if ln.strip()), "")
    return f"{STUB_MARKER} {subject.strip()} — offline explanation (no model called)."


def _anthropic_call(prompt: str, *, system: str | None = None) -> str:
    """The real Messages API call (only reached when ``llm_backend`` is
    ``anthropic``). Caches the system block so repeated calls over a stable
    system prompt read the prefix instead of re-billing it."""
    import anthropic

    client = anthropic.Anthropic()
    model = os.environ.get("TYO3_LLM_MODEL", DEFAULT_MODEL)
    kwargs: dict[str, object] = {
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
    resp = client.messages.create(**kwargs)  # type: ignore[arg-type]
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
