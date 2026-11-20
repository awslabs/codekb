"""Round-trip tests for KnowledgeBaseManager against mocked S3."""

from pathlib import Path

from src.core.models import (
    Component,
    ComponentAttributes,
    ComponentType,
    KnowledgeBase,
    Visibility,
)
from src.persistence.knowledge_base_manager import KnowledgeBaseManager


def _make_kb(space_id: str = "test-space") -> KnowledgeBase:
    """Build a tiny in-memory KnowledgeBase for round-trip tests."""
    comp_a = Component(
        id=f"{space_id}::foo.bar",
        type=ComponentType.FUNCTION,
        file_path=Path("foo.py"),
        line_number=1,
        end_line_number=5,
        language="python",
        space_id=space_id,
        attributes=ComponentAttributes(
            signature="def bar()",
            visibility=Visibility.PUBLIC,
            line_count=5,
        ),
    )
    comp_b = Component(
        id=f"{space_id}::foo.Baz",
        type=ComponentType.CLASS,
        file_path=Path("foo.py"),
        line_number=10,
        end_line_number=30,
        language="python",
        space_id=space_id,
    )
    return KnowledgeBase(
        space_id=space_id,
        source_directory="/tmp/foo",
        components=[comp_a, comp_b],
        call_graph={f"{space_id}::foo.Baz": [f"{space_id}::foo.bar"]},
    )


class TestKnowledgeBaseManager:
    def test_export_returns_success_payload(self):
        kb = _make_kb("export-space")
        mgr = KnowledgeBaseManager()

        result = mgr.export_knowledge_base(kb)
        assert result["status"] == "success"
        assert "location" in result
        assert result.get("components_exported") == 2

    def test_export_and_import_roundtrip(self):
        kb = _make_kb("roundtrip-space")
        mgr = KnowledgeBaseManager()
        mgr.export_knowledge_base(kb)

        reloaded = mgr.import_knowledge_base("roundtrip-space")
        assert reloaded is not None
        assert reloaded.space_id == "roundtrip-space"
        assert reloaded.get_component_count() == 2
        assert any(c.type == ComponentType.FUNCTION for c in reloaded.components)
        assert any(c.type == ComponentType.CLASS for c in reloaded.components)

    def test_import_missing_space_returns_none(self):
        # The manager treats missing spaces as a miss (returns None) rather
        # than raising, so callers can decide whether to analyze or fail.
        mgr = KnowledgeBaseManager()
        assert mgr.import_knowledge_base("definitely-not-a-space") is None

    def test_s3_key_layout(self):
        mgr = KnowledgeBaseManager()
        key = mgr._get_s3_key("my-space")
        assert key.startswith("spaces/my-space/")
        assert key.endswith(".json")
