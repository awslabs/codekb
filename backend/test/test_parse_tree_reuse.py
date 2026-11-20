"""Tests for the phase-1 -> phase-2 parsed-tree reuse (ticket P523895151, Option A).

Proves two things end-to-end on the real multi-language fixtures:
  1. Correctness: the call graph built WITH the handed-over tree cache is
     identical to the one built WITHOUT it (which re-parses from disk).
  2. Savings: seeding the cache eliminates the redundant second parse — zero
     parse_file calls when the cache is fully warm, vs one-per-file otherwise.
"""

from pathlib import Path

from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog
from src.core.tree_sitter_wrapper import TreeSitterWrapper, tree_cache_key
from src.flow.flow_analyzer import FlowAnalyzer

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _analyze():
    """Analyze the fixtures once; return (components, parsed_trees)."""
    analyzer = CodeAnalyzer(ComponentCatalog())
    kb = analyzer.analyze_directory(FIXTURES_DIR, space_id="reuse-test")
    trees = analyzer.take_parsed_trees()
    return kb.components, trees


def test_tree_cache_key_embeds_full_path_no_collision():
    # Same basename in different directories must produce distinct keys.
    k1 = tree_cache_key(Path("/repo/pkg_a/utils.py"), "python")
    k2 = tree_cache_key(Path("/repo/pkg_b/utils.py"), "python")
    assert k1 != k2


def test_call_graph_identical_with_and_without_tree_cache():
    components, trees = _analyze()
    assert trees, "fixtures should have produced at least one parsed tree"

    cg_no_cache = FlowAnalyzer(ComponentCatalog()).build_call_graph(components)
    cg_with_cache = FlowAnalyzer(ComponentCatalog()).build_call_graph(
        components, tree_cache=dict(trees)
    )

    assert cg_with_cache == cg_no_cache


def test_handed_over_cache_eliminates_second_parse():
    components, trees = _analyze()

    original = TreeSitterWrapper.parse_file
    calls = {"n": 0}

    def counting_parse_file(self, file_path, language):
        calls["n"] += 1
        return original(self, file_path, language)

    TreeSitterWrapper.parse_file = counting_parse_file
    try:
        # No cache: phase 2 must re-parse every file that has callables.
        calls["n"] = 0
        FlowAnalyzer(ComponentCatalog()).build_call_graph(components)
        parses_without_cache = calls["n"]

        # Warm cache: no re-parse at all.
        calls["n"] = 0
        FlowAnalyzer(ComponentCatalog()).build_call_graph(
            components, tree_cache=dict(trees)
        )
        parses_with_cache = calls["n"]
    finally:
        TreeSitterWrapper.parse_file = original

    assert parses_without_cache > 0, "sanity: the no-cache path should parse files"
    assert parses_with_cache == 0, (
        f"warm cache should avoid all re-parses, but parse_file ran {parses_with_cache} time(s)"
    )


def test_file_cache_cleared_after_build():
    components, trees = _analyze()
    flow = FlowAnalyzer(ComponentCatalog())
    flow.build_call_graph(components, tree_cache=dict(trees))
    # Trees are released once the build is done (lifetime scoped to the build).
    assert flow._file_cache == {}
