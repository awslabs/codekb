"""Tests for the parse-diagnostic model shapes.

A result carrying no diagnostics serializes exactly as it did before the field
existed, a result carrying them round-trips, ``has_errors`` is untouched by
their presence, and the three report counts stay separate.
"""

from pathlib import Path

import pytest

from src.core.models import (
    Component,
    ComponentType,
    ParseDiagnostic,
    ParseDiagnosticKind,
    ParseFailureReport,
    ParseResult,
)


def _result(**kwargs) -> ParseResult:
    return ParseResult(file_path=Path("src/thing.java"), language="java", **kwargs)


def _component() -> Component:
    return Component(
        id="Thing.doWork",
        type=ComponentType.METHOD,
        file_path=Path("src/thing.java"),
        line_number=1,
        end_line_number=4,
        language="java",
    )


# ── Serialization is additive ────────────────────────────────────────────────


def test_result_without_diagnostics_serializes_as_before():
    """No diagnostics -> the key is absent, so every language's shape is unchanged."""
    data = _result(components=[_component()]).to_dict()

    assert "diagnostics" not in data
    assert sorted(data) == ["components", "file_path", "language", "metadata", "parse_errors"]


def test_result_with_diagnostics_round_trips():
    original = _result(
        diagnostics=[
            ParseDiagnostic(Path("src/thing.java"), ParseDiagnosticKind.ERROR, 3, 7, 41),
            ParseDiagnostic(Path("src/thing.java"), ParseDiagnosticKind.MISSING, 9, 0, 120),
        ]
    )

    restored = ParseResult.from_dict(original.to_dict())

    assert restored.diagnostics == original.diagnostics
    assert restored.diagnostics[0].kind is ParseDiagnosticKind.ERROR
    assert restored.diagnostics[1].kind is ParseDiagnosticKind.MISSING
    assert restored.diagnostics[1].byte_offset == 120


# ── The existing failed-file tally is untouched ──────────────────────────────


def test_has_errors_is_unchanged_by_diagnostics():
    """A file with structural defects but no raised exception is not a failed file."""
    defective = _result(
        diagnostics=[ParseDiagnostic(Path("src/thing.java"), ParseDiagnosticKind.ERROR, 1, 0, 0)]
    )

    assert defective.has_errors() is False

    raised = _result(parse_errors=["Component extraction failed: boom"])
    assert raised.has_errors() is True

    # And the two are independent: diagnostics neither add to nor mask it.
    both = _result(
        parse_errors=["Component extraction failed: boom"],
        diagnostics=[ParseDiagnostic(Path("src/thing.java"), ParseDiagnosticKind.ERROR, 1, 0, 0)],
    )
    assert both.has_errors() is True


# ── Identity keeps two defects at one position distinct ──────────────────────


def test_two_missing_diagnostics_at_one_position_stay_two():
    """Byte offset is part of the identity, and the carrier is a list, not a set."""
    first = ParseDiagnostic(Path("src/thing.java"), ParseDiagnosticKind.MISSING, 4, 12, 88)
    second = ParseDiagnostic(Path("src/thing.java"), ParseDiagnosticKind.MISSING, 4, 12, 89)

    assert first != second
    assert len({first, second}) == 2

    result = _result(diagnostics=[first, second])
    assert len(result.diagnostics) == 2
    assert result.failure_report().missing_node_count == 2

    # Even an exactly-identical pair is carried as two, because the list is the
    # carrier: a run that saw two defects reports two.
    duplicated = _result(diagnostics=[first, first])
    assert duplicated.failure_report().missing_node_count == 2


# ── The three counts stay separate ───────────────────────────────────────────


def test_failure_report_keeps_three_diagnostic_counts_separate():
    """The report never sums an absorbed exception with a structural defect."""
    result = _result(
        parse_errors=["Component extraction failed: boom"],
        diagnostics=[
            ParseDiagnostic(Path("src/thing.java"), ParseDiagnosticKind.ERROR, 1, 0, 0),
            ParseDiagnostic(Path("src/thing.java"), ParseDiagnosticKind.ERROR, 2, 0, 20),
            ParseDiagnostic(Path("src/thing.java"), ParseDiagnosticKind.MISSING, 3, 4, 44),
        ],
    )

    report = result.failure_report()

    assert report.parse_error_count == 1
    assert report.error_node_count == 2
    assert report.missing_node_count == 1
    assert report.language == "java"
    assert report.is_structurally_clean() is False
    assert ParseFailureReport.from_dict(report.to_dict()) == report


def test_diagnostic_free_result_with_no_components_is_structurally_clean():
    report = _result(components=[]).failure_report()

    assert report.is_structurally_clean() is True
    assert (report.parse_error_count, report.error_node_count, report.missing_node_count) == (
        0,
        0,
        0,
    )


@pytest.mark.parametrize("kind", list(ParseDiagnosticKind))
def test_diagnostic_round_trips_every_kind(kind):
    """Positions come from node offsets, so every kind serializes unvalidated."""
    diagnostic = ParseDiagnostic(Path("src/thing.java"), kind, 1, 0, 0)

    assert ParseDiagnostic.from_dict(diagnostic.to_dict()) == diagnostic


# ── Collection against real grammars ────────────────────────────────────────
# A mocked tree proves nothing about the nodes a grammar actually emits. Java
# carries most cases: a non-Dart language is what tests the uniformity claim.


@pytest.fixture(scope="module")
def wrapper():
    from src.core.tree_sitter_wrapper import TreeSitterWrapper

    return TreeSitterWrapper()


def _parse(wrapper, tmp_path, name: str, source: str, language: str) -> ParseResult:
    from src.core.models import IdScheme

    path = tmp_path / name
    path.write_text(source)
    # These tests assert readable, stable V1 ids; V2 id construction is covered
    # by test/test_id_scheme.py. Set on every parse so no stale singleton state.
    wrapper.set_id_context(None, IdScheme.V1_STEM)
    return wrapper.parse_file(path, language)


def test_error_node_yields_one_error_diagnostic(wrapper, tmp_path):
    """The diagnostic is recorded at the error node's own position."""
    result = _parse(
        wrapper, tmp_path, "Broken.java", "class A { void f() { int x = ; } }\n", "java"
    )

    errors = [d for d in result.diagnostics if d.kind is ParseDiagnosticKind.ERROR]
    assert len(errors) == 1
    assert errors[0].line == 1
    assert errors[0].column == 27
    assert errors[0].byte_offset == 27
    assert errors[0].file_path == tmp_path / "Broken.java"
    assert result.failure_report().error_node_count == 1


def test_missing_node_yields_one_missing_diagnostic(wrapper, tmp_path):
    """A MISSING node is recorded as its own kind, not as an error."""
    result = _parse(
        wrapper, tmp_path, "Missing.java", "class A { void f() { int x = 1 } }\n", "java"
    )

    kinds = [d.kind for d in result.diagnostics]
    assert ParseDiagnosticKind.MISSING in kinds
    report = result.failure_report()
    assert report.missing_node_count >= 1
    # The two kinds are counted apart, never summed.
    assert report.error_node_count + report.missing_node_count == len(result.diagnostics)


def test_structurally_clean_tree_yields_no_diagnostics(wrapper, tmp_path):
    """A clean tree yields none — in Java and in Python alike."""
    java = _parse(
        wrapper,
        tmp_path,
        "Clean.java",
        "class A {\n    int f() {\n        return 1;\n    }\n}\n",
        "java",
    )
    python = _parse(wrapper, tmp_path, "clean.py", "def f():\n    return 1\n", "python")

    assert java.diagnostics == []
    assert python.diagnostics == []
    assert java.failure_report().is_structurally_clean() is True
    assert python.failure_report().is_structurally_clean() is True


def test_clean_tree_with_no_components_is_structurally_clean_and_diagnostic_free(wrapper, tmp_path):
    result = _parse(wrapper, tmp_path, "imports.py", "import os\nimport sys\n", "python")

    assert result.components == []
    assert result.diagnostics == []
    report = result.failure_report()
    assert report.is_structurally_clean() is True
    assert result.has_errors() is False


def test_file_with_diagnostics_still_returns_its_components(wrapper, tmp_path):
    """A diagnostic is never an exception — the parse completes."""
    source = (
        "class A {\n"
        "    int good() {\n"
        "        return 1;\n"
        "    }\n"
        "    void broken() { int x = ; }\n"
        "}\n"
    )
    result = _parse(wrapper, tmp_path, "Mixed.java", source, "java")

    assert result.diagnostics, "expected the deliberate defect to be recorded"
    assert result.components, "a defective file must still yield its components"
    assert any(c.get_simple_name() == "good" for c in result.components)
    # And the defect did not enter the existing failed-file tally.
    assert result.has_errors() is False


def test_existing_language_diagnostic_free_output_is_unchanged(wrapper, tmp_path):
    """A clean existing-language file keeps its components, tally and shape."""
    result = _parse(
        wrapper,
        tmp_path,
        "Service.java",
        "class Service {\n    int add(int a, int b) {\n        return a + b;\n    }\n}\n",
        "java",
    )

    assert [c.id for c in result.components] == ["Service.Service", "Service.Service.add"]
    assert result.has_errors() is False
    assert result.failure_report().parse_error_count == 0

    serialized = result.to_dict()
    assert "diagnostics" not in serialized

    # The node count is the number the recursive walk this collection replaced
    # produced, so joining the two did not move a reported value.
    def recursive_count(node):
        return 1 + sum(recursive_count(child) for child in node.children)

    assert result.metadata["node_count"] == recursive_count(result.syntax_tree)


# ── Where the reports travel ────────────────────────────────────────────────


def test_parse_failure_metadata_keeps_diagnostic_counts_separate():
    """The run's metadata carries each count apart, per language and in total."""
    from src.analysis.code_analyzer import collect_parse_failure_metadata

    metadata = collect_parse_failure_metadata(
        [
            ParseFailureReport(Path("a.java"), "java"),
            ParseFailureReport(Path("b.java"), "java", error_node_count=2),
            ParseFailureReport(Path("c.dart"), "dart", parse_error_count=1, missing_node_count=3),
        ]
    )

    summary = metadata["parse_failure_summary"]
    assert summary["scope"] == "full-run"
    assert summary["files_reported"] == 3
    assert summary["structurally_clean_files"] == 1
    assert summary["files_with_defects"] == 2
    assert summary["parse_error_count"] == 1
    assert summary["error_node_count"] == 2
    assert summary["missing_node_count"] == 3
    assert summary["by_language"]["dart"] == {
        "files_reported": 1,
        "structurally_clean_files": 0,
        "files_with_defects": 1,
        "parse_error_count": 1,
        "error_node_count": 0,
        "missing_node_count": 3,
        "untrusted_name_count": 0,
    }

    # Per-file rows only for the files that are not structurally clean, exactly
    # as analysis_errors only lists files that errored.
    rows = metadata["parse_failure_reports"]
    assert [row["file_path"] for row in rows] == ["b.java", "c.dart"]


def test_a_refused_name_is_summarised_not_only_tallied():
    """A file whose only defect is a refused name must say so in the counts.

    `is_structurally_clean` factors the refusal in, so without this the summary
    reports a defective file with every tracked count at zero.
    """
    from src.analysis.code_analyzer import collect_parse_failure_metadata

    metadata = collect_parse_failure_metadata(
        [ParseFailureReport(Path("d.dart"), "dart", untrusted_name_count=2)]
    )

    summary = metadata["parse_failure_summary"]
    assert summary["files_with_defects"] == 1
    assert summary["untrusted_name_count"] == 2
    assert summary["by_language"]["dart"]["untrusted_name_count"] == 2


def test_parse_failure_metadata_is_present_with_no_diagnostic_reports():
    """A reader never has to tell "no key" from "zero defects"."""
    from src.analysis.code_analyzer import collect_parse_failure_metadata

    metadata = collect_parse_failure_metadata([])

    assert metadata["parse_failure_reports"] == []
    assert metadata["parse_failure_summary"]["files_reported"] == 0
    assert metadata["parse_failure_summary"]["by_language"] == {}


def test_refresh_keeps_prior_diagnostic_rows_for_files_it_did_not_reparse():
    """A refresh's metadata describes the merged KB, not the previous run alone."""
    from src.analysis.code_analyzer import merge_parse_failure_metadata

    prior = {
        "parse_failure_reports": [
            ParseFailureReport(Path("kept.java"), "java", error_node_count=1).to_dict(),
            ParseFailureReport(Path("reparsed.java"), "java", error_node_count=9).to_dict(),
            ParseFailureReport(Path("gone.java"), "java", error_node_count=4).to_dict(),
        ]
    }

    metadata = merge_parse_failure_metadata(
        prior,
        [ParseFailureReport(Path("reparsed.java"), "java")],
        {"reparsed.java", "gone.java"},
    )

    rows = {row["file_path"]: row for row in metadata["parse_failure_reports"]}
    # The reparsed file is now clean, so it keeps no defect row; the removed
    # file's row is gone; the untouched file's row survives.
    assert sorted(rows) == ["kept.java"]
    summary = metadata["parse_failure_summary"]
    assert summary["scope"] == "changed-files"
    assert summary["error_node_count"] == 1
    assert summary["files_with_defects"] == 1


def test_unreadable_prior_diagnostic_row_is_skipped_not_raised():
    from src.analysis.code_analyzer import merge_parse_failure_metadata

    prior = {
        "parse_failure_reports": [
            {"file_path": "broken.java"},  # no language key
            ParseFailureReport(Path("kept.java"), "java", missing_node_count=2).to_dict(),
        ]
    }

    metadata = merge_parse_failure_metadata(prior, [], set())

    assert [row["file_path"] for row in metadata["parse_failure_reports"]] == ["kept.java"]
    assert metadata["parse_failure_summary"]["missing_node_count"] == 2


def test_refresh_result_counts_upgrade_files_separately():
    """The added-file figure stays the commit range's own count."""
    from src.core.models import RefreshResult

    plain = RefreshResult(files_added=3)
    assert "files_added_for_new_languages" not in plain.to_dict()

    upgraded = RefreshResult(files_added=3, files_added_for_new_languages=12)
    data = upgraded.to_dict()
    assert data["files_added"] == 3
    assert data["files_added_for_new_languages"] == 12
    assert RefreshResult.from_dict(data).files_added_for_new_languages == 12


# ── Full-index rows must be supersedable on refresh ─────────────────────────


def _defective_tree() -> Path:
    import tempfile

    directory = Path(tempfile.mkdtemp())
    (directory / "lib").mkdir()
    (directory / "lib" / "bad.dart").write_text(
        "class const Broken({required this.x}) {\n  final int x;\n}\n"
    )
    return directory


def test_a_full_index_records_its_defect_rows_repo_relative():
    """Both paths must agree on the form, or neither can supersede the other.

    Refresh builds its superseded set from git-diff paths and writes its own rows
    repo-relative, so an absolute row from the full index can never match.
    """
    from src.analysis.code_analyzer import CodeAnalyzer
    from src.catalog.component_catalog import ComponentCatalog, SimpleBusinessBehaviorAnalyzer

    directory = _defective_tree()
    catalog = ComponentCatalog(SimpleBusinessBehaviorAnalyzer())
    knowledge_base = CodeAnalyzer(catalog).analyze_directory(directory, "space")

    rows = (knowledge_base.analysis_metadata or {}).get("parse_failure_reports") or []
    assert rows, "the deliberate defect should earn a row"
    assert [row["file_path"] for row in rows] == ["lib/bad.dart"]


def test_a_refresh_supersedes_a_row_the_full_index_wrote():
    """The defect itself: a file fixed after indexing must lose its stale row."""
    from src.analysis.code_analyzer import CodeAnalyzer, merge_parse_failure_metadata
    from src.catalog.component_catalog import ComponentCatalog, SimpleBusinessBehaviorAnalyzer

    directory = _defective_tree()
    catalog = ComponentCatalog(SimpleBusinessBehaviorAnalyzer())
    prior = CodeAnalyzer(catalog).analyze_directory(directory, "space").analysis_metadata or {}

    # The refresh reparses that file and finds it clean this time.
    merged = merge_parse_failure_metadata(
        prior,
        [ParseFailureReport(Path("lib/bad.dart"), "dart")],
        {"lib/bad.dart"},
    )

    assert merged["parse_failure_reports"] == []
    assert merged["parse_failure_summary"]["files_with_defects"] == 0
