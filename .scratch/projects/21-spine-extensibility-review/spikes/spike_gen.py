"""THROWAWAY SPIKE generator module (project 21 review).

Importable by the engine's PythonGenerator via `callable = "spike_gen:<fn>"`.
Not engine/plugin code — a review probe only. Each generator records its calls
so spikes can prove recompute-vs-reuse by invocation count.
"""

from __future__ import annotations

import ast

COMPLEXITY_CALLS: list[str] = []
REFCOUNT_CALLS: list[str] = []


def complexity(inputs):
    """Cyclomatic-ish complexity from the entity source text (the only input a
    today-generator gets: GenInput.source)."""
    COMPLEXITY_CALLS.extend(i.durable_id for i in inputs)
    out = []
    for i in inputs:
        try:
            n = 1 + sum(
                isinstance(x, (ast.If, ast.For, ast.While, ast.BoolOp, ast.Try))
                for x in ast.walk(ast.parse(i.source))
            )
        except Exception:
            n = -1
        out.append(f'{{"score": {n}}}')
    return out


def refcount(inputs):
    """A stand-in 'references' generator. It CANNOT see the snapshot (today's
    Generator contract only hands it text), so it just echoes — the point of the
    spike is the *invalidation* of this layer, not its content."""
    REFCOUNT_CALLS.extend(i.durable_id for i in inputs)
    return [f'{{"echo": "{i.durable_id[-6:]}"}}' for i in inputs]
