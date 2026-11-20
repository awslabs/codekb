"""Tests for AnalysisOrchestrator behavior on missing / present knowledge bases.

The orchestrator.load_space path used to return an AnalysisContext built from a
None KnowledgeBase, which then crashed later with AttributeError. It now raises
FileNotFoundError for missing spaces, and analyze_space's catch block falls
through to a fresh analysis — so this file locks that behavior in.
"""

from pathlib import Path

import pytest

from src.analysis.orchestrator import AnalysisOrchestrator
from src.persistence.knowledge_base_manager import KnowledgeBaseManager


class TestLoadSpace:
    def test_raises_filenotfound_when_kb_missing(self):
        orch = AnalysisOrchestrator(KnowledgeBaseManager())
        with pytest.raises(FileNotFoundError):
            orch.load_space("never-analyzed-space")


class TestAnalyzeSpace:
    def test_rejects_missing_directory(self):
        orch = AnalysisOrchestrator(KnowledgeBaseManager())
        with pytest.raises(ValueError):
            orch.analyze_space(Path("/tmp/does-not-exist-really"), "some-space")

    def test_rejects_file_instead_of_directory(self, tmp_path):
        some_file = tmp_path / "a.txt"
        some_file.write_text("hello")
        orch = AnalysisOrchestrator(KnowledgeBaseManager())
        with pytest.raises(ValueError):
            orch.analyze_space(some_file, "some-space")

    def test_analyzes_when_no_existing_kb(self, tmp_path):
        # This is the regression: previously analyze_space's "check for
        # existing KB" call raised AttributeError via a None knowledge_base,
        # and the fallback logger wasn't defined. Now it should cleanly fall
        # through to fresh analysis.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tiny.py").write_text("def hello():\n    return 1\n")

        orch = AnalysisOrchestrator(KnowledgeBaseManager())
        ctx = orch.analyze_space(src, "new-space-regression")

        assert ctx is not None
        assert ctx.knowledge_base is not None
        # The tiny fixture should yield at least one component (the function).
        assert ctx.knowledge_base.get_component_count() >= 1


class TestPersistedComponentPaths:
    """Stored paths must name the durable source root, not the analysed tree.

    Analysis runs in a working tree the pipeline deletes when it finishes, so a
    path under that tree is a dead pointer the moment the run ends. Paths are
    stored against the location the source is hydrated to instead. That location
    is fixed (``SOURCE_CACHE_ROOT``), which is what makes an absolute
    stored path valid on any host running the same configuration — and what a
    newly analysed space has in common with every space analysed before this.
    """

    def test_persisted_paths_name_the_durable_source_root(self, tmp_path):
        analysed = tmp_path / "myrepo-analysis"
        analysed.mkdir()
        (analysed / "kms.py").write_text("def sign_csr():\n    return 1\n")
        # Where the source is hydrated to afterwards. It need not exist yet,
        # which is the whole reason the stored path cannot name the tree above.
        durable_root = tmp_path / "myrepo"

        orch = AnalysisOrchestrator(KnowledgeBaseManager())
        ctx = orch.analyze_space(
            analysed, "path-convention-space", persisted_source_root=durable_root
        )

        # In memory the paths still name the tree on disk: summarization runs on
        # this context next and opens the files.
        assert ctx.knowledge_base.components
        assert {c.file_path for c in ctx.knowledge_base.components} == {analysed / "kms.py"}
        assert all(c.file_path.is_file() for c in ctx.knowledge_base.components)

        reloaded = KnowledgeBaseManager().import_knowledge_base("path-convention-space")
        assert reloaded is not None
        assert reloaded.components
        # Stored form names the durable root, so deleting the analysed tree does
        # not invalidate it.
        assert {c.file_path for c in reloaded.components} == {durable_root / "kms.py"}
        assert Path(reloaded.source_directory) == durable_root

    def test_source_root_defaults_to_the_analysed_directory(self, tmp_path):
        """Callers whose analysed directory is durable can omit the argument."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "kms.py").write_text("def sign_csr():\n    return 1\n")

        orch = AnalysisOrchestrator(KnowledgeBaseManager())
        orch.analyze_space(src, "default-source-root-space")

        reloaded = KnowledgeBaseManager().import_knowledge_base("default-source-root-space")
        assert reloaded is not None
        assert Path(reloaded.source_directory) == src
        assert {c.file_path for c in reloaded.components} == {src / "kms.py"}
