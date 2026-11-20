"""Tests for FileScanner exclusion matching.

Covers the path-aware (gitignore-subset) exclusion patterns in
codekb/core/file_utils.py:
  - basename patterns (no slash)      -> match at any depth
  - path patterns (contain a slash)   -> match relative to the scan root
  - "**"                              -> spans directories; "*"/"?" do not
  - trailing "/"                      -> directory-only rules

The scanner also filters by supported language, so every fixture file uses a
.py extension and Python is registered as an available language; exclusion is
therefore the only thing that removes a file from the result.
"""

from pathlib import Path
from typing import List

import pytest

from src.core.file_utils import FileScanner, _glob_to_regex


def _make_tree(root: Path, rel_paths: List[str]) -> None:
    for rel in rel_paths:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n")


def _scan(root: Path, patterns: List[str]) -> set:
    scanner = FileScanner(exclude_patterns=patterns)
    # Restrict to a language we can definitely parse so language filtering
    # doesn't remove fixtures for reasons unrelated to exclusion.
    scanner.set_available_languages({"python"})
    return {p.relative_to(root).as_posix() for p in scanner.scan_directory(root)}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    _make_tree(
        tmp_path,
        [
            "app/main.py",
            "app/static/js/lib.min.py",  # .py stands in for a bundled asset
            "app/static/js/app.py",
            "vendor/lib/thing.py",
            "src/generated/models.py",
            "src/handwritten/models.py",
            "node_modules/pkg/index.py",
            "build/out.py",
            "keep/regular.py",
            "toplevel_only/x.py",
            "sub/toplevel_only/y.py",
        ],
    )
    return tmp_path


class TestBasenamePatterns:
    def test_bare_name_prunes_directory_subtree_at_any_depth(self, tree):
        found = _scan(tree, ["node_modules"])
        assert not any(f.startswith("node_modules/") for f in found)
        assert "keep/regular.py" in found

    def test_suffix_glob_matches_basename_at_any_depth(self, tree):
        found = _scan(tree, ["*.min.py"])
        assert "app/static/js/lib.min.py" not in found
        assert "app/static/js/app.py" in found

    def test_default_like_patterns_still_work(self, tree):
        # Mirrors the shipped defaults: bare dir names match by basename.
        found = _scan(tree, ["build", "node_modules"])
        assert "build/out.py" not in found
        assert "node_modules/pkg/index.py" not in found
        assert "app/main.py" in found


class TestPathPatterns:
    def test_path_pattern_matches_relative_path(self, tree):
        found = _scan(tree, ["app/static/js/*.min.py"])
        assert "app/static/js/lib.min.py" not in found
        assert "app/static/js/app.py" in found

    def test_path_pattern_is_anchored_to_root(self, tree):
        # "src/generated" matches only at the root, not a same-named nested dir.
        _make_tree(tree, ["deep/src/generated/other.py"])
        found = _scan(tree, ["src/generated"])
        assert "src/generated/models.py" not in found
        assert "deep/src/generated/other.py" in found  # not anchored here
        assert "src/handwritten/models.py" in found

    def test_leading_slash_anchors_to_root(self, tree):
        found = _scan(tree, ["/toplevel_only"])
        assert "toplevel_only/x.py" not in found  # excluded at root
        assert "sub/toplevel_only/y.py" in found  # kept: nested, not anchored

    def test_double_star_spans_directories(self, tree):
        found = _scan(tree, ["**/generated/**"])
        assert "src/generated/models.py" not in found
        assert "src/handwritten/models.py" in found


class TestDirOnlyPatterns:
    def test_trailing_slash_excludes_directory(self, tree):
        found = _scan(tree, ["vendor/"])
        assert not any(f.startswith("vendor/") for f in found)

    def test_trailing_slash_does_not_match_a_file(self, tmp_path):
        # A file literally named "vendor" must survive a "vendor/" rule.
        _make_tree(tmp_path, ["vendor", "keep.py"])
        # "vendor" has no .py extension so it is never a source file anyway;
        # assert the directory-only rule doesn't error and keep.py remains.
        found = _scan(tmp_path, ["vendor/"])
        assert "keep.py" in found


class TestBackwardCompatibility:
    """Every shipped default must compile to an equivalent effective match."""

    @pytest.mark.parametrize(
        "name,pattern,should_match",
        [
            ("node_modules", "node_modules", True),
            ("nodes", "node_modules", False),
            ("foo.egg-info", "*.egg-info", True),
            ("foo.egg", "*.egg-info", False),
            ("._hidden", "._*", True),
            ("visible", "._*", False),
            ("test", "test*", True),
            ("test_utils", "test*", True),
            ("latest", "test*", False),
        ],
    )
    def test_basename_defaults_match_as_before(self, name, pattern, should_match):
        scanner = FileScanner(exclude_patterns=[pattern])
        assert scanner._is_excluded_directory(Path("/some/root") / name) is should_match


class TestIsExcludedDirectoryCallback:
    """RubyAppBoundaryQualifier consumes _is_excluded_directory as a
    Callable[[Path], bool]; it must exist and match by basename."""

    def test_method_exists_and_matches_by_basename(self):
        scanner = FileScanner(exclude_patterns=["node_modules"])
        assert callable(scanner._is_excluded_directory)
        assert scanner._is_excluded_directory(Path("/a/b/node_modules")) is True
        assert scanner._is_excluded_directory(Path("/a/b/src")) is False


class TestGlobToRegex:
    @pytest.mark.parametrize(
        "glob,text,matches",
        [
            ("*.min.js", "a.min.js", True),
            ("*.min.js", "a/b.min.js", False),  # * does not cross '/'
            ("a/*/c", "a/b/c", True),
            ("a/*/c", "a/b/d/c", False),  # single segment only
            ("**/x", "x", True),
            ("**/x", "a/b/x", True),
            ("a/**", "a/b/c", True),
            ("?", "a", True),
            ("?", "ab", False),
        ],
    )
    def test_translation_is_segment_aware(self, glob, text, matches):
        import re

        assert bool(re.match(f"^{_glob_to_regex(glob)}$", text)) is matches


class TestIsExcludedPath:
    """is_excluded_path checks a repo-relative path and its parent directories."""

    @pytest.mark.parametrize(
        "rel_path,excluded",
        [
            ("node_modules/pkg/index.js", True),  # excluded parent directory
            ("src/node_modules/pkg/index.js", True),  # basename rule at any depth
            ("docs/generated/api.md", True),  # path rule
            ("src/app.py", False),
            ("src/generated/api.md", False),  # path rule is root-anchored
        ],
    )
    def test_matches_path_and_parents(self, rel_path, excluded):
        scanner = FileScanner(exclude_patterns=["node_modules", "docs/generated/"])
        assert scanner.is_excluded_path(rel_path) is excluded


class TestExplicitEmptyPatterns:
    """An explicit empty list means "exclude nothing", not "use the config"."""

    def test_empty_list_excludes_nothing(self):
        scanner = FileScanner(exclude_patterns=[])
        assert scanner.exclude_patterns == []
        assert scanner.is_excluded_path("node_modules/pkg/index.js") is False

    def test_none_uses_configured_patterns(self):
        from src.core.config import get_config

        assert FileScanner().exclude_patterns == get_config("analysis.exclude_patterns")


class TestDefaultPatterns:
    """default.yaml excludes test suites and AI-agent workspaces, not test* prefixes."""

    @pytest.mark.parametrize(
        "rel_path,excluded",
        [
            ("tests/test_app.py", True),
            ("src/test/AppTest.java", True),
            ("web/__tests__/app.test.ts", True),
            (".claude/agents/helper.py", True),
            (".kiro/specs/plan.ts", True),
            (".codex/notes.py", True),
            (".opencode/run.ts", True),
            (".cursor/rules.ts", True),
            ("backend/testing/policy.py", False),
            ("app/services/test_connection.py", False),
        ],
    )
    def test_default_exclusions(self, rel_path, excluded):
        assert FileScanner().is_excluded_path(rel_path) is excluded
