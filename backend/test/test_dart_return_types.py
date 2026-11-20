"""A Dart declaration records the return type as written, not its first node.

`_return_type` read only the first matching child, so `Future<void>?` was
recorded as `Future` while a parameter of the same shape in the same signature
kept `Map<String,int>?` in full. Three of the four type-recording sites in the
parser join the written nodes; this one did not.

`return_type` is metadata rather than a lookup key — the flow analyzer never
reads it, and resolution reduces types separately through `_simple_type_name` —
so recording what was written costs nothing and removes the inconsistency.
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

SOURCE = """class Repo {
  Future<void>? run(List<String> names, Map<String, int>? counts) { return null; }
  Note? findById(String id) { return null; }
  Note findOne(String id) { return Note(); }
  void clear() {}
  Map<String, int> counts() { return {}; }
  T echo<T>(T value) { return value; }
  void Function(String) handler() { return (s) {}; }
  (int, String) pair() { return (1, 'a'); }
  Repo.named(this.id);
  final String id = '';
}
class Note {}
"""


@pytest.fixture(scope="module")
def return_types() -> dict[str, str | None]:
    directory = Path(tempfile.mkdtemp())
    (directory / "r.dart").write_text(SOURCE)
    catalog = ComponentCatalog(SimpleBusinessBehaviorAnalyzer())
    knowledge_base = CodeAnalyzer(catalog).analyze_directory(directory, "space", id_scheme=IdScheme.V1_STEM)
    return {
        component.id.split("::")[-1]: component.attributes.return_type
        for component in knowledge_base.components
        if component.language == "dart"
    }


class TestTheWrittenTypeIsRecordedWhole:
    @pytest.mark.parametrize(
        ("member", "expected"),
        [
            ("r.Repo.run", "Future<void>?"),
            ("r.Repo.findById", "Note?"),
            ("r.Repo.counts", "Map<String, int>"),
            ("r.Repo.handler", "void Function(String)"),
            ("r.Repo.pair", "(int, String)"),
        ],
    )
    def test_generics_and_nullability_survive(self, return_types, member, expected):
        assert return_types[member] == expected

    @pytest.mark.parametrize(
        ("member", "expected"),
        [("r.Repo.findOne", "Note"), ("r.Repo.clear", "void")],
    )
    def test_a_plain_type_is_unchanged(self, return_types, member, expected):
        assert return_types[member] == expected


class TestWhatMustNotBeCollected:
    def test_a_methods_own_type_parameters_are_not_glued_on(self, return_types):
        """`T echo<T>(T value)` returns `T`, not `T<T>`."""
        assert return_types["r.Repo.echo"] == "T"

    def test_a_declaration_with_no_written_type_records_nothing(self, return_types):
        """A constructor annotates no return type. Absent beats guessing `dynamic`."""
        assert return_types["r.Repo.named"] is None


class TestConsistencyWithTheOtherTypeSites:
    def test_a_parameter_and_the_return_type_agree_in_shape(self):
        """The inconsistency this fixes: both are the written type, joined."""
        directory = Path(tempfile.mkdtemp())
        (directory / "r.dart").write_text(SOURCE)
        catalog = ComponentCatalog(SimpleBusinessBehaviorAnalyzer())
        components = {
            c.id.split("::")[-1]: c
            for c in CodeAnalyzer(catalog).analyze_directory(directory, "space", id_scheme=IdScheme.V1_STEM).components
        }
        run = components["r.Repo.run"]
        hints = {p.name: p.type_hint for p in run.attributes.parameters}

        assert run.attributes.return_type == "Future<void>?"
        assert hints["names"] == "List<String>"
        assert hints["counts"] == "Map<String, int>?"
