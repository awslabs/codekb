"""Dart through the service layer, not just the parsers.

Every other Dart test drives CodeAnalyzer or FlowAnalyzer directly. This one
goes through AnalysisOrchestrator, so a Dart space is built, persisted and
reloaded the way the running service does it. The AWS clients are the faked
ones from conftest.
"""

from pathlib import Path

import pytest

try:  # pragma: no cover - import guard, mirrors the other Dart modules
    import tree_sitter_dart  # noqa: F401

    DART_AVAILABLE = True
except ImportError:  # pragma: no cover
    DART_AVAILABLE = False

from src.analysis.orchestrator import AnalysisOrchestrator
from src.persistence.knowledge_base_manager import KnowledgeBaseManager

# Without the grammar, `.dart` files are reported as skipped rather than parsed,
# so these assertions would fail hard instead of skipping.
pytestmark = pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart not installed")

FLUTTER_APP = {
    "lib/repository.dart": (
        "class NoteRepository {\n"
        "  final Database _db;\n"
        "  NoteRepository(this._db);\n"
        "  Future<void> persist(Note n) async { _db.write(n); }\n"
        "  Note? findById(String id) => _db.read(id);\n"
        "}\n"
        "class Database {\n"
        "  void write(Note n) {}\n"
        "  Note? read(String id) => null;\n"
        "}\n"
        "class Note {}\n"
    ),
    "lib/service.dart": (
        "import 'repository.dart';\n"
        "class NoteService {\n"
        "  final NoteRepository _repo;\n"
        "  NoteService(this._repo);\n"
        "  void save(Note n) { _repo.persist(n); }\n"
        "}\n"
    ),
    # A split library: the part's components must carry the parent's prefix.
    "lib/models/models.dart": "part 'state.dart';\n",
    "lib/models/state.dart": ("part of 'models.dart';\nclass Store {\n  void reset() {}\n}\n"),
    # Generated output, excluded at discovery.
    "lib/models/models.g.dart": "class GeneratedJunk {}\n",
}


@pytest.fixture
def flutter_repo(tmp_path: Path) -> Path:
    for rel, body in FLUTTER_APP.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return tmp_path


@pytest.fixture
def orchestrator() -> AnalysisOrchestrator:
    # KnowledgeBaseManager persists to S3; conftest's autouse fixtures supply
    # the faked client, so this exercises the real persistence path.
    return AnalysisOrchestrator(KnowledgeBaseManager())


class TestDartThroughTheService:
    def test_a_dart_space_analyses_and_reloads_with_the_same_components(
        self, orchestrator, flutter_repo
    ):
        """A reload must not lose or rename what the analysis produced."""
        analysed = orchestrator.analyze_space(flutter_repo, "dart-e2e")
        first = {c.id for c in analysed.knowledge_base.components}

        reloaded = orchestrator.load_space("dart-e2e")
        assert {c.id for c in reloaded.knowledge_base.components} == first

    def test_dart_is_reported_as_a_supported_language(self, orchestrator):
        assert "dart" in orchestrator.get_supported_languages()

    def test_the_space_holds_dart_components_and_no_parse_failures(
        self, orchestrator, flutter_repo
    ):
        ctx = orchestrator.analyze_space(flutter_repo, "dart-clean")
        dart = [c for c in ctx.knowledge_base.components if c.language == "dart"]

        assert dart, "no Dart components reached the space"
        summary = (ctx.knowledge_base.analysis_metadata or {}).get("parse_failure_summary", {})
        assert summary.get("by_language", {}).get("dart", {}).get("files_with_defects") == 0

    def test_a_field_receiver_resolves_through_the_services_call_graph(
        self, orchestrator, flutter_repo
    ):
        """`_repo.persist(n)` is the dominant Flutter idiom; it must bind.

        The persisted graph stores ``(callee_id, line)`` pairs rather than bare ids.
        """
        ctx = orchestrator.analyze_space(flutter_repo, "dart-edges")
        graph = ctx.knowledge_base.call_graph

        # Scheme-agnostic: the pipeline indexes new spaces under the V2 path-
        # hashed scheme, so the file-prefix segment is "<stem>_<hash>", not the
        # bare stem. Match on the class-qualified suffix, which both schemes share.
        callers = [k for k in graph if k.endswith("NoteService.save")]
        assert callers, "NoteService.save missing from the call graph"
        callees = [callee for callee, _line in graph[callers[0]]]
        assert any(c.endswith("NoteRepository.persist") for c in callees)

    def test_a_split_librarys_part_is_prefixed_in_the_space(self, orchestrator, flutter_repo):
        ctx = orchestrator.analyze_space(flutter_repo, "dart-parts")
        # V2 path-hashes the file prefix, so match the class suffix, not the stem.
        store = [c.id for c in ctx.knowledge_base.components if c.id.endswith(".Store")]

        assert store, "Store missing from the space"
        assert all("$-" in i for i in store), f"part not prefixed: {store}"

    def test_generated_output_never_reaches_the_space(self, orchestrator, flutter_repo):
        ctx = orchestrator.analyze_space(flutter_repo, "dart-generated")

        assert not [c for c in ctx.knowledge_base.components if "GeneratedJunk" in c.id]
