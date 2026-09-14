"""External stubs must not be classified as code entities.

The code layer holds three id populations: real entities (ULIDs), synthetic
module nodes (``"<module>" + file``) and external stubs. Stub ids are ordinary
``"package::name"`` strings and carry **no** distinguishing prefix — only the
node's ``file`` field holds the ``"<external>"`` sentinel, and only the producer
sets ``external``. Classifying by id shape alone therefore reads every stub as
an entity, which is the regression these tests pin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tyo3.graph.identity import is_entity_durable_id, is_entity_node
from tyo3.session import TyO3Session

SOURCE = "import json\n\n\ndef load(text):\n    return json.loads(text)\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A one-file project whose only import is a stdlib module, so the producer
    mints external stubs for the off-project target."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0.1.0"\n')
    (tmp_path / "main.py").write_text(SOURCE)
    return tmp_path


class TestClassifier:
    """Unit coverage for the two predicates."""

    @pytest.mark.parametrize(
        ("durable_id", "external", "expected"),
        [
            ("01M2ER6S3JQ418NNVKPV4FEE2A", False, True),  # real entity (ULID)
            ("01M2ER6S3JQ418NNVKPV4FEE2A::save", False, True),  # nested compound id
            ("<module>main.py", False, False),  # synthetic module node
            ("requests::Session", True, False),  # external stub — no prefix
            ("unknown::<module>", True, False),  # external module stub
            ("stdlib/json/__init__.pyi::loads", True, False),
        ],
    )
    def test_is_entity_node(self, durable_id: str, external: bool, expected: bool) -> None:
        assert is_entity_node(durable_id, external=external) is expected

    def test_is_entity_durable_id_cannot_see_stubs(self) -> None:
        """The id-only predicate is honest about its limit: it excludes module
        ids and nothing else. Callers holding a node must use is_entity_node."""
        assert is_entity_durable_id("<module>main.py") is False
        assert is_entity_durable_id("01M2ER6S3JQ418NNVKPV4FEE2A") is True
        # A stub is indistinguishable from an entity by id alone — documented,
        # not a bug. This is why is_entity_node takes `external`.
        assert is_entity_durable_id("requests::Session") is True


class TestCodeLayerView:
    """The leak this project fixes: `session.code.ids()` yielded stubs."""

    def test_ids_excludes_external_stubs(self, project: Path) -> None:
        snap = TyO3Session(str(project)).snapshot()
        nodes = snap._native().full_code_delta()["nodes_upserted"]
        stub_ids = {n["durable_id"] for n in nodes if n["external"]}
        assert stub_ids, "fixture must produce at least one external stub"

        ids = set(snap.code.ids())
        assert not (ids & stub_ids), f"external stubs leaked into code.ids(): {sorted(ids & stub_ids)}"

    def test_ids_excludes_module_synthetics(self, project: Path) -> None:
        snap = TyO3Session(str(project)).snapshot()
        assert not [did for did in snap.code.ids() if did.startswith("<module>")]

    def test_ids_keeps_real_entities(self, project: Path) -> None:
        snap = TyO3Session(str(project)).snapshot()
        ids = set(snap.code.ids())
        assert ids, "the project's own `load` function must survive filtering"
        for did in ids:
            node = snap.code.value(did)
            assert node is not None
            assert node.external is False
            assert node.file != "<external>"

    def test_value_rejects_an_external_stub(self, project: Path) -> None:
        snap = TyO3Session(str(project)).snapshot()
        nodes = snap._native().full_code_delta()["nodes_upserted"]
        stub_id = next(n["durable_id"] for n in nodes if n["external"])
        assert snap.code.value(stub_id) is None


class TestLayerDiff:
    """A diff across revisions must not report stubs as created or removed."""

    def test_diff_reports_no_external_stub(self, project: Path) -> None:
        session = TyO3Session(str(project))
        before = session.snapshot()
        session.edit("main.py", SOURCE + "\n\ndef dump(obj):\n    return json.dumps(obj)\n")
        after = session.snapshot()

        nodes = after._native().full_code_delta()["nodes_upserted"]
        stub_ids = {n["durable_id"] for n in nodes if n["external"]}

        diff = after.code.diff(before.code)
        reported = set(diff.added) | set(diff.removed)
        assert not (reported & stub_ids), f"stubs in layer diff: {sorted(reported & stub_ids)}"
