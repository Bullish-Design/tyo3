"""Generators — produce derived artifacts from inputs (SPEC §9.2.6).

Three dispatch paths:
- ``python`` — import ``module:callable``, call in-process.
- ``command`` — spawn argv; entity payload on stdin, artifact on stdout.
- ``http`` — POST to endpoint with model/dim; credentials from env.

All generators are batched: ``generate(inputs) -> list[bytes]``.
Failure raises ``GeneratorFailed``; the caller guarantees the prior artifact
is untouched.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Protocol

from tyo3.config import GeneratorConfig
from tyo3.exceptions import GeneratorFailed

# ── GenInput ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GenInput:
    """One input to a generator call."""

    durable_id: str
    source: str  # entity source text (code-derived) or upstream artifact bytes decoded
    kind: str = ""  # entity kind (e.g. "function")
    meta: dict[str, Any] = field(default_factory=dict)


# ── Generator protocol ──────────────────────────────────────────────────


class Generator(Protocol):
    """A callable that produces artifacts for a batch of inputs."""

    def generate(self, inputs: list[GenInput]) -> list[bytes]:
        """Produce one artifact per input. Length must match inputs."""
        ...


# ── Legacy → Producer adapter (AB2) ──────────────────────────────────────
#
# The AB2 produce path runs every code-derived layer through the recording
# ``Producer`` protocol (``tyo3.extend``). A layer declared with a legacy
# ``Generator`` (the ``python``/``command``/``http`` built-ins, or any object
# exposing ``generate(inputs)``) rides this thin adapter unchanged: it builds the
# ``GenInput`` batch from each recording context and notes ``{durable_id}`` as the
# read-set, so a traced layer wrapping a legacy generator keys **byte-identically
# to ``local``** (the degenerate single-own-id read-set, ``dag.py``). The lifecycle
# hooks are no-ops — a dotted-string callable has no client to open or close.


class _GeneratorProducer:
    """Adapt a legacy :class:`Generator` to the AB2 ``Producer`` contract.

    ``produce(ctxs)`` rebuilds the batched ``Generator.generate(inputs)`` call
    from the recording contexts and records each context's read-set as exactly
    ``{durable_id}`` — the legacy generator only ever saw the entity's own text
    (``GenInput.source``), so its data dependency is its own content and nothing
    else. That makes a traced layer wrapping it recompute on its own-body change
    only, identical to ``local``."""

    def __init__(self, generator: Generator) -> None:
        self._generator = generator

    def produce(self, ctxs: list) -> list[bytes]:
        inputs = [GenInput(durable_id=c.durable_id, source=c.source, kind=c.kind) for c in ctxs]
        for c in ctxs:
            # Record the read-set as the entity itself — keys like `local`.
            c.note_read([c.durable_id])
        return self._generator.generate(inputs)

    def setup(self) -> None:
        """No-op: a legacy generator has no client to open."""

    def teardown(self) -> None:
        """No-op: a legacy generator has no client to close."""


# ── Dispatcher ───────────────────────────────────────────────────────────


def make_generator(gen_cfg: GeneratorConfig, /, *, name: str = "") -> Generator:
    """Dispatch to the registered generator factory for ``gen_cfg.type``.

    The built-in ``python``/``command``/``http`` types self-register their
    factories at this module's import time (below). A custom type registered via
    ``tyo3.extend.register_generator`` resolves here with no edit to this
    function (AB1). An unknown type raises ``ValueError``."""
    from tyo3.extend import _GENERATORS

    gen_type = gen_cfg.type or "python"
    factory = _GENERATORS.get(gen_type)
    if factory is None:
        raise ValueError(f"Unknown generator type: {gen_type}")
    return factory(gen_cfg, name=name)


# ── Python generator ────────────────────────────────────────────────────


class PythonGenerator:
    """Import ``module:callable`` and call it in-process, batched."""

    def __init__(self, cfg: GeneratorConfig, *, name: str = "") -> None:
        self._name = name
        if not cfg.callable:
            raise ValueError("python generator requires 'callable'")
        module_name, _, func_name = cfg.callable.partition(":")
        if not module_name or not func_name:
            raise ValueError(f"python generator callable must be 'module:function', got '{cfg.callable}'")
        mod = importlib.import_module(module_name)
        self._func = getattr(mod, func_name)
        self._batch_size = cfg.batch_size or 64

    def generate(self, inputs: list[GenInput]) -> list[bytes]:
        results: list[bytes] = []
        for i in range(0, len(inputs), self._batch_size):
            batch = inputs[i : i + self._batch_size]
            try:
                batch_results = self._func(batch)
            except Exception as exc:
                raise GeneratorFailed(
                    f"generator '{self._name}' failed: {exc}",
                    layer=self._name,
                    input_ids=[inp.durable_id for inp in batch],
                ) from exc
            if len(batch_results) != len(batch):
                raise GeneratorFailed(
                    f"generator '{self._name}' returned {len(batch_results)} results for {len(batch)} inputs",
                    layer=self._name,
                    input_ids=[inp.durable_id for inp in batch],
                )
            for item in batch_results:
                if isinstance(item, str):
                    results.append(item.encode("utf-8"))
                elif isinstance(item, bytes):
                    results.append(item)
                else:
                    results.append(str(item).encode("utf-8"))
        return results


# ── Command generator ───────────────────────────────────────────────────


class CommandGenerator:
    """Spawn a subprocess per batch; entity payload on stdin, artifact on stdout."""

    def __init__(self, cfg: GeneratorConfig, *, name: str = "") -> None:
        self._name = name
        if not cfg.command:
            raise ValueError("command generator requires 'command'")
        self._argv = list(cfg.command)
        self._timeout_ms = cfg.timeout_ms or 60_000
        self._concurrency = max(1, cfg.concurrency or 1)
        self._batch_size = max(1, cfg.batch_size or 1)

    def generate(self, inputs: list[GenInput]) -> list[bytes]:
        batches = [inputs[i : i + self._batch_size] for i in range(0, len(inputs), self._batch_size)]
        if len(batches) == 1 or self._concurrency == 1:
            results: list[bytes] = []
            for batch in batches:
                results.extend(self._run_batch(batch))
            return results

        # Concurrent batching.
        results_map: dict[int, list[bytes]] = {}
        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            futures = {pool.submit(self._run_batch, batch): idx for idx, batch in enumerate(batches)}
            for fut in as_completed(futures):
                idx = futures[fut]
                results_map[idx] = fut.result()

        merged: list[bytes] = []
        for idx in sorted(results_map):
            merged.extend(results_map[idx])
        return merged

    def _run_batch(self, batch: list[GenInput]) -> list[bytes]:
        payload_lines = [json.dumps({"id": inp.durable_id, "source": inp.source, "kind": inp.kind}) for inp in batch]
        payload = "\n".join(payload_lines).encode("utf-8")
        try:
            proc = subprocess.run(
                self._argv,
                input=payload,
                capture_output=True,
                timeout=self._timeout_ms / 1000.0,
            )
        except subprocess.TimeoutExpired as exc:
            raise GeneratorFailed(
                f"generator '{self._name}' timed out after {self._timeout_ms}ms",
                layer=self._name,
                input_ids=[inp.durable_id for inp in batch],
            ) from exc
        except Exception as exc:
            raise GeneratorFailed(
                f"generator '{self._name}' failed: {exc}",
                layer=self._name,
                input_ids=[inp.durable_id for inp in batch],
            ) from exc

        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")[:500]
            raise GeneratorFailed(
                f"generator '{self._name}' exited {proc.returncode}: {stderr}",
                layer=self._name,
                input_ids=[inp.durable_id for inp in batch],
            )

        # One line per input expected.
        out_lines = proc.stdout.decode("utf-8").strip().split("\n")
        if len(out_lines) != len(batch):
            raise GeneratorFailed(
                f"generator '{self._name}' returned {len(out_lines)} lines for {len(batch)} inputs",
                layer=self._name,
                input_ids=[inp.durable_id for inp in batch],
            )
        return [line.encode("utf-8") for line in out_lines]


# ── HTTP generator ──────────────────────────────────────────────────────


class HttpGenerator:
    """POST to an embedding/summary endpoint. Credentials from env only."""

    def __init__(self, cfg: GeneratorConfig, *, name: str = "") -> None:
        self._name = name
        self._endpoint = cfg.endpoint
        if not self._endpoint:
            raise ValueError("http generator requires 'endpoint'")
        self._model = cfg.model or ""
        self._dim = cfg.dim
        self._timeout_ms = cfg.timeout_ms or 30_000
        self._concurrency = max(1, cfg.concurrency or 1)
        self._batch_size = max(1, cfg.batch_size or 32)

    def generate(self, inputs: list[GenInput]) -> list[bytes]:
        import urllib.request

        all_results: list[bytes] = []

        for i in range(0, len(inputs), self._batch_size):
            batch = inputs[i : i + self._batch_size]
            texts = [inp.source for inp in batch]
            body = json.dumps(
                {
                    "model": self._model,
                    "input": texts,
                }
            ).encode("utf-8")

            req = urllib.request.Request(
                self._endpoint,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            # Resolve credentials from env — never from config.
            api_key = os.environ.get("OPENAI_API_KEY", "")
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")

            try:
                with urllib.request.urlopen(req, timeout=self._timeout_ms / 1000.0) as resp:
                    raw = resp.read()
                    data = json.loads(raw)
            except Exception as exc:
                raise GeneratorFailed(
                    f"generator '{self._name}' HTTP request failed: {exc}",
                    layer=self._name,
                    input_ids=[inp.durable_id for inp in batch],
                ) from exc

            # Extract embeddings from response.
            try:
                embeddings = data.get("data", [])
                for emb in embeddings:
                    vec = emb.get("embedding", [])
                    # Pack as JSON bytes for storage.
                    all_results.append(json.dumps(vec).encode("utf-8"))
            except Exception as exc:
                raise GeneratorFailed(
                    f"generator '{self._name}' bad response: {exc}",
                    layer=self._name,
                    input_ids=[inp.durable_id for inp in batch],
                ) from exc

            if len(all_results) - (i // self._batch_size) * self._batch_size != len(batch):
                raise GeneratorFailed(
                    f"generator '{self._name}' returned unexpected number of embeddings",
                    layer=self._name,
                    input_ids=[inp.durable_id for inp in batch],
                )

        return all_results


# ── Built-in generator factories (self-register into tyo3.extend) ────────────
#
# Seeded into the ``_GENERATORS`` registry at *this module's* import time, so
# ``tyo3.extend`` carries no import-time dependency on this module (no cycle —
# AB1_DESIGN_NOTE.md §2). Each factory is ``(cfg, *, name="") -> Generator`` so
# ``make_generator``'s signature is unchanged.


def _python_factory(cfg: GeneratorConfig, *, name: str = "") -> Generator:
    return PythonGenerator(cfg, name=name)


def _command_factory(cfg: GeneratorConfig, *, name: str = "") -> Generator:
    return CommandGenerator(cfg, name=name)


def _http_factory(cfg: GeneratorConfig, *, name: str = "") -> Generator:
    return HttpGenerator(cfg, name=name)


def _register_builtins() -> None:
    from tyo3.extend import register_generator

    for _type, _factory in (("python", _python_factory), ("command", _command_factory), ("http", _http_factory)):
        # ``override=True`` keeps re-import idempotent (e.g. test reloads) without
        # the dup-raise that protects *user* registrations.
        register_generator(_type, _factory, override=True)


_register_builtins()
