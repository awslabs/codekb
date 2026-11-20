"""Tests for FileScanner discovery behaviour.

The generated-file exclusion class below drives the scanner against a real
``tmp_path`` tree rather than a mock: the behaviour under test is the ORDER in
which one discovered file is classified.

No file in these trees sits under a directory the shipped
``analysis.exclude_patterns`` list prunes (``test*``, ``build``, ``dist``), or
it would never reach the code under test.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pytest

from src.core import file_utils
from src.core.file_utils import FileScanner


# Directory/file prune list used by these tests: the shipped entries that
# matter here, without `test*`, so a tmp_path under pytest's own `test_...`
# directory names is not pruned out from under the assertions.
_EXCLUDE_PATTERNS = [".git", "node_modules", "__pycache__", "build", "dist"]

_UNSUPPORTED_LANGUAGES = {
    "swift": {"extensions": [".swift"]},
    "scala": {"extensions": [".scala", ".sc"]},
}


def _scanner(
    monkeypatch: pytest.MonkeyPatch,
    supported_languages: Dict[str, Dict[str, Any]],
    available_languages: Optional[Set[str]] = None,
) -> FileScanner:
    """Build a FileScanner whose configuration is exactly what a test declares.

    Patches the ``get_config`` the scanner module imported, so ``__init__``'s real
    wiring — including the per-language pattern map — is what gets exercised.
    """
    values: Dict[str, Any] = {
        "analysis.exclude_patterns": _EXCLUDE_PATTERNS,
        "analysis.supported_languages": supported_languages,
        "analysis.unsupported_languages": _UNSUPPORTED_LANGUAGES,
        "analysis.max_file_size_mb": 10,
        "analysis.max_files_per_directory": 10000,
    }
    monkeypatch.setattr(
        file_utils, "get_config", lambda key, default=None: values.get(key, default)
    )

    scanner = FileScanner()
    if available_languages is not None:
        scanner.set_available_languages(available_languages)
    return scanner


def _dart(patterns: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """A supported-language registry holding dart, python and no pattern list for python."""
    dart: Dict[str, Any] = {"extensions": [".dart"], "parser": "tree-sitter-dart"}
    if patterns is not None:
        dart["generated_file_patterns"] = patterns
    return {
        "dart": dart,
        "python": {"extensions": [".py"], "parser": "tree-sitter-python"},
    }


def _write(root: Path, *names: str) -> None:
    """Create each named file (relative to root) with trivial content."""
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// content\n", encoding="utf-8")


class TestGeneratedFileExclusion:
    """Generated-file exclusion at discovery."""

    _DART_DEFAULTS = ["*.g.dart", "*.freezed.dart", "*.gr.dart", "*.mocks.dart"]

    def test_matched_file_is_excluded_and_counted_in_one_tally_only(self, tmp_path, monkeypatch):
        _write(tmp_path, "model.g.dart", "user.freezed.dart", "main.dart")
        scanner = _scanner(monkeypatch, _dart(self._DART_DEFAULTS))

        discovered = scanner.scan_directory(tmp_path)
        names = {path.name for path in discovered}

        # Excluded from the analysable set, so never handed to a parser.
        assert names == {"main.dart"}
        assert scanner.generated_files == {"dart": 2}
        assert scanner.generated_files_reason == "generated"
        # And NOT also counted as an unsupported-language skip.
        assert scanner.skipped_files == {}
        assert "dart" not in scanner.skipped_files

    def test_unmatched_file_of_the_same_language_stays_analysable(self, tmp_path, monkeypatch):
        _write(tmp_path, "main.dart", "lib/widgets/home_page.dart", "generator.dart")
        scanner = _scanner(monkeypatch, _dart(self._DART_DEFAULTS))

        discovered = scanner.scan_directory(tmp_path)

        assert {path.name for path in discovered} == {
            "main.dart",
            "home_page.dart",
            "generator.dart",
        }
        assert scanner.generated_files == {}
        assert scanner.skipped_files == {}

    def test_language_with_no_pattern_list_is_untouched(self, tmp_path, monkeypatch):
        """Same analysable set and same skip counts as before the change."""
        _write(tmp_path, "app.py", "model.g.py", "lib/helper.py", "Thing.swift")
        # python carries no generated_file_patterns key at all.
        scanner = _scanner(monkeypatch, _dart(self._DART_DEFAULTS))

        discovered = scanner.scan_directory(tmp_path)

        assert {path.name for path in discovered} == {"app.py", "model.g.py", "helper.py"}
        assert scanner.generated_files == {}
        # The existing unsupported-language tally still reports swift.
        assert scanner.skipped_files == {"swift": 1}

    def test_empty_pattern_list_behaves_exactly_like_an_absent_one(self, tmp_path, monkeypatch):
        """An empty pattern list means no exclusion, not exclude-everything."""
        _write(tmp_path, "model.g.dart", "main.dart")

        with_empty = _scanner(monkeypatch, _dart([]))
        empty_discovered = {path.name for path in with_empty.scan_directory(tmp_path)}
        empty_generated = dict(with_empty.generated_files)
        empty_skipped = dict(with_empty.skipped_files)

        with_absent = _scanner(monkeypatch, _dart(None))
        absent_discovered = {path.name for path in with_absent.scan_directory(tmp_path)}

        assert empty_discovered == {"model.g.dart", "main.dart"}
        assert empty_discovered == absent_discovered
        assert empty_generated == with_absent.generated_files == {}
        assert empty_skipped == with_absent.skipped_files == {}

    def test_unconfigured_extension_matching_a_pattern_is_skipped_not_generated(
        self, tmp_path, monkeypatch
    ):
        """The generated decision only applies to an analysable language."""
        _write(tmp_path, "model.g.swift", "main.dart")
        # dart carries a pattern that a .swift file's name would match, but
        # .swift is not a configured extension, so the file is a skip.
        scanner = _scanner(monkeypatch, _dart(["*.g.swift", "*.g.dart"]))

        discovered = scanner.scan_directory(tmp_path)

        assert {path.name for path in discovered} == {"main.dart"}
        assert scanner.generated_files == {}
        assert scanner.skipped_files == {"swift": 1}

    def test_configured_language_without_a_loaded_grammar_is_skipped_not_generated(
        self, tmp_path, monkeypatch
    ):
        _write(tmp_path, "model.g.dart", "main.dart", "app.py")
        # dart is configured and carries patterns, but has no working parser.
        scanner = _scanner(monkeypatch, _dart(self._DART_DEFAULTS), available_languages={"python"})

        discovered = scanner.scan_directory(tmp_path)

        assert {path.name for path in discovered} == {"app.py"}
        assert scanner.generated_files == {}
        assert scanner.skipped_files == {"dart": 2}

    def test_both_tallies_are_reported_separately(self, tmp_path, monkeypatch, caplog):
        """Each omission keeps its own reason and count, never summed."""
        _write(tmp_path, "model.g.dart", "main.dart", "Thing.swift", "Other.scala")
        scanner = _scanner(monkeypatch, _dart(self._DART_DEFAULTS))

        with caplog.at_level(logging.INFO, logger="src.core.file_utils"):
            scanner.scan_directory(tmp_path)

        completion = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("Discovered ")
        ]
        assert len(completion) == 1
        message = completion[0]

        assert "skipped 2 file(s) in unsupported languages: scala, swift" in message
        assert "excluded 1 generated file(s): dart 1 (generated)" in message
        # Never summed: the three omitted files are reported as 2 + 1, not as 3.
        assert "3 file(s)" not in message
        assert scanner.skipped_files == {"swift": 1, "scala": 1}
        assert scanner.generated_files == {"dart": 1}

    def test_a_leading_double_star_still_matches_the_name(self, tmp_path, monkeypatch):
        """Pins the matcher's real form: the same glob translation as exclusions.

        Patterns are translated by ``_glob_to_regex`` and matched against the
        bare file name, so a leading ``**/`` contributes nothing and the
        suffix decides.
        """
        _write(tmp_path, "model.g.dart", "main.dart")
        scanner = _scanner(monkeypatch, _dart(["**/*.g.dart"]))

        discovered = scanner.scan_directory(tmp_path)

        assert {path.name for path in discovered} == {"main.dart"}
        assert scanner.generated_files == {"dart": 1}

    def test_a_pattern_naming_a_directory_matches_nothing(self, tmp_path, monkeypatch):
        """Matching is against the name alone, so a path pattern cannot match.

        A user who writes one gets silence, not an error.
        """
        _write(tmp_path, "model.g.dart", "main.dart")
        scanner = _scanner(monkeypatch, _dart(["lib/*.g.dart"]))

        discovered = scanner.scan_directory(tmp_path)

        assert {path.name for path in discovered} == {"model.g.dart", "main.dart"}
        assert scanner.generated_files == {}

    def test_empty_generated_tally_adds_nothing_to_the_report(self, tmp_path, monkeypatch, caplog):
        """Silent when nothing was excluded, matching the skip line."""
        _write(tmp_path, "main.dart")
        scanner = _scanner(monkeypatch, _dart(self._DART_DEFAULTS))

        with caplog.at_level(logging.INFO, logger="src.core.file_utils"):
            scanner.scan_directory(tmp_path)

        message = next(
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("Discovered ")
        )
        assert message == "Discovered 1 source files"
