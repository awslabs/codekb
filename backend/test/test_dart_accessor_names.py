"""`get_simple_name` must not mistake a member named `get`/`set` for an accessor.

Dart is the one language whose identity can carry a kind suffix, so a getter for
`label` reads `file.Class.label.get`. But Dart also permits `get` and `set` as
ordinary method names — this package's own fixture declares
`Future<String?> get(String key)` — so a trailing `get` segment is ambiguous
from the id string alone.

It matters because the flow analyzer keys `component_lookup` on the simple name:
a method registered under its class's name is unreachable from its own calls.
"""

import tempfile
from pathlib import Path

import pytest

try:  # pragma: no cover - import guard, mirrors the other Dart modules
    import tree_sitter_dart  # noqa: F401

    DART_AVAILABLE = True
except ImportError:  # pragma: no cover
    DART_AVAILABLE = False

from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog, SimpleBusinessBehaviorAnalyzer
from src.core.models import IdScheme

pytestmark = pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart not installed")

SOURCE = """class KeyValueStore {
  Future<String?> get(String key) async { return null; }
  void set(String key, String value) {}
  String get label => 'x';
  set label(String value) {}
}
String get topLevel => 'y';
void get() {}
"""


@pytest.fixture(scope="module")
def simple_names() -> dict[str, str]:
    directory = Path(tempfile.mkdtemp())
    (directory / "kv.dart").write_text(SOURCE)
    catalog = ComponentCatalog(SimpleBusinessBehaviorAnalyzer())
    knowledge_base = CodeAnalyzer(catalog).analyze_directory(directory, "space", id_scheme=IdScheme.V1_STEM)
    return {
        component.id.split("::")[-1]: component.get_simple_name()
        for component in knowledge_base.components
        if component.language == "dart"
    }


class TestAMemberNamedLikeAnAccessorKeepsItsOwnName:
    @pytest.mark.parametrize("identity", ["kv.KeyValueStore.get", "kv.KeyValueStore.set"])
    def test_a_method_named_get_or_set_is_not_read_as_a_suffix(self, simple_names, identity):
        expected = identity.rsplit(".", 1)[-1]

        assert simple_names[identity] == expected

    def test_a_top_level_function_named_get_keeps_its_name(self, simple_names):
        """The sharpest case: stepping back one segment here reaches the space id."""
        assert simple_names["kv.get"] == "get"


class TestARealAccessorStillReportsItsMember:
    @pytest.mark.parametrize(
        ("identity", "expected"),
        [
            ("kv.KeyValueStore.label.get", "label"),
            ("kv.KeyValueStore.label.set", "label"),
            ("kv.topLevel.get", "topLevel"),
        ],
    )
    def test_the_kind_suffix_is_stepped_over(self, simple_names, identity, expected):
        assert simple_names[identity] == expected

    def test_the_class_itself_is_unaffected(self, simple_names):
        assert simple_names["kv.KeyValueStore"] == "KeyValueStore"


class TestTheAccessorKindIsRecordedRatherThanGuessed:
    def test_an_accessor_records_its_kind_and_a_method_does_not(self):
        directory = Path(tempfile.mkdtemp())
        (directory / "kv.dart").write_text(SOURCE)
        catalog = ComponentCatalog(SimpleBusinessBehaviorAnalyzer())
        components = {
            c.id.split("::")[-1]: c
            for c in CodeAnalyzer(catalog).analyze_directory(directory, "space", id_scheme=IdScheme.V1_STEM).components
        }

        assert components["kv.KeyValueStore.label.get"].attributes.accessor_kind == "get"
        assert components["kv.KeyValueStore.label.set"].attributes.accessor_kind == "set"
        assert components["kv.KeyValueStore.get"].attributes.accessor_kind is None

    def test_the_field_is_omitted_when_absent_so_old_output_is_unchanged(self):
        from src.core.models import ComponentAttributes

        assert "accessor_kind" not in ComponentAttributes().to_dict()
        assert ComponentAttributes(accessor_kind="get").to_dict()["accessor_kind"] == "get"


class TestAlreadyPersistedComponentsStillResolve:
    """Old data carries no recorded kind, so the id shape has to answer."""

    def test_a_stored_accessor_without_the_field_still_reports_its_member(self):
        from src.core.models import Component, ComponentType

        stored = Component(
            id="space::kv.KeyValueStore.label.get",
            type=ComponentType.METHOD,
            file_path=Path("kv.dart"),
            line_number=1,
            end_line_number=1,
            language="dart",
            parent_component="space::kv.KeyValueStore",
        )

        assert stored.get_simple_name() == "label"

    def test_a_stored_method_named_get_without_the_field_keeps_its_name(self):
        from src.core.models import Component, ComponentType

        stored = Component(
            id="space::kv.KeyValueStore.get",
            type=ComponentType.METHOD,
            file_path=Path("kv.dart"),
            line_number=1,
            end_line_number=1,
            language="dart",
            parent_component="space::kv.KeyValueStore",
        )

        assert stored.get_simple_name() == "get"
