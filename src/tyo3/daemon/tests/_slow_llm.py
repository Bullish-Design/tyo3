"""A deliberately slow ``_llm`` callable for the non-serialization test.

Wired via ``TYO3_LLM=callable`` + ``TYO3_LLM_CALLABLE=tyo3.daemon.tests._slow_llm:slow_explain``
in a daemon subprocess. It sleeps for ``TYO3_TEST_LLM_SLEEP`` seconds (default 2)
so a concurrent fast request (``ping``) can be observed returning *before* the
slow ``explain`` completes — proving the dispatch pool isn't serializing the
connection. Signature matches the ``_llm`` seam: ``f(prompt, *, system) -> str``.
"""

from __future__ import annotations

import os
import time


def slow_explain(prompt: str, *, system: str | None = None) -> str:
    time.sleep(float(os.environ.get("TYO3_TEST_LLM_SLEEP", "2")))
    subject = next((ln for ln in prompt.splitlines() if ln.strip()), "")
    return f"slow explanation for {subject.strip()}"
