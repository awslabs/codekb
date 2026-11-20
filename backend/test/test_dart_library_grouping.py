"""Dart split-library grouping.

Every identity assertion runs through ``CodeAnalyzer.analyze_directory``.
``DartLibraryQualifier`` lives inside that method, constructed from the scan's
own file selection and applied between extraction and ``add_component``, so
tests that call ``DartParser`` directly never reach it and would stay green
through a complete re-keying of every Dart identity in the catalog.

``active`` is read off the qualifier directly because ``analyze_directory`` does
not return it; ``_qualifier`` rebuilds it from a real ``FileScanner`` scan.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

try:  # pragma: no cover - import guard, mirrors test_parser_integration.py
    import tree_sitter_dart  # noqa: F401

    DART_AVAILABLE = True
except ImportError:  # pragma: no cover
    DART_AVAILABLE = False

from src.analysis.code_analyzer import DART_UNRESOLVED_PARTS_KEY, CodeAnalyzer
from src.analysis.dart_libraries import (
    REASON_CHAINED_PART,
    REASON_CLAIMED_BY_TWO_PARENTS,
    REASON_NOT_IN_ANALYSED_SET,
    REASON_ORPHANED_PART,
    DartLibraryQualifier,
    UnresolvedPartRecord,
)
from src.catalog.component_catalog import ComponentCatalog
from src.core.models import KnowledgeBase, IdScheme

FIXTURES_DIR = Path(__file__).parent / "fixtures"

SPACE_ID = "dartlib"

# A part file declaring one class with one method. Used wherever a test needs a
# same-named class in two or more part files.
STORE_PART = "part of '{parent}';\n\nclass Store {{\n  void save() {{}}\n}}\n"


def _write_tree(root: Path, files: dict) -> None:
    """Materialise a dict of repo-relative path -> body under ``root``."""
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


def _analyze(tmp_path: Path, files: dict) -> KnowledgeBase:
    """Write the tree and run the real directory-level analysis over it."""
    _write_tree(tmp_path, files)
    return CodeAnalyzer(ComponentCatalog()).analyze_directory(tmp_path, space_id=SPACE_ID, id_scheme=IdScheme.V1_STEM)


def _ids(knowledge_base: KnowledgeBase) -> list:
    """Component ids with the space prefix removed, as the catalog holds them."""
    return sorted(c.id.split("::", 1)[-1] for c in knowledge_base.components)


def _bare(value):
    """Strip the space prefix from one id or parent reference."""
    return None if value is None else value.split("::", 1)[-1]


def _records(knowledge_base: KnowledgeBase) -> list:
    """The run's unresolved part records, as they travel in analysis metadata."""
    return knowledge_base.analysis_metadata[DART_UNRESOLVED_PARTS_KEY]


def _reasons(knowledge_base: KnowledgeBase, reason: str) -> list:
    return [row for row in _records(knowledge_base) if row["reason"] == reason]


def _qualifier(directory: Path) -> DartLibraryQualifier:
    """Build the qualifier exactly as ``analyze_directory`` builds it.

    The scan is the real ``FileScanner`` the analyzer uses, so the Dart subset handed
    to the qualifier is the analysed set, generated-file exclusions and all.
    """
    analyzer = CodeAnalyzer(ComponentCatalog())
    source_files = analyzer._file_scanner.scan_directory(directory)
    dart_files = [f for f in source_files if f.suffix.lower() == ".dart"]
    return DartLibraryQualifier(directory, dart_files)


class TestUnresolvedPartRecord:
    """The record is plain data with a closed reason set."""

    def test_reason_constants_are_the_closed_set(self):
        assert REASON_NOT_IN_ANALYSED_SET == "not-in-analysed-set"
        assert REASON_CLAIMED_BY_TWO_PARENTS == "claimed-by-two-parents"
        assert REASON_CHAINED_PART == "chained-part"
        assert REASON_ORPHANED_PART == "orphaned-part"

    def test_to_dict_carries_every_field(self):
        record = UnresolvedPartRecord(
            part_path="lib/shared.dart",
            reason=REASON_CLAIMED_BY_TWO_PARENTS,
            parent_path="lib/one.dart",
            second_parent_path="lib/two.dart",
        )
        assert record.to_dict() == {
            "part_path": "lib/shared.dart",
            "reason": REASON_CLAIMED_BY_TWO_PARENTS,
            "parent_path": "lib/one.dart",
            "second_parent_path": "lib/two.dart",
            "declared_parent_path": None,
        }

    def test_to_dict_carries_the_declared_target_of_an_orphan(self):
        """None of the other four fields can hold what an orphan says it belongs to, so
        a reader of the run's metadata could not otherwise tell a typo from an excluded
        file from a ``package:`` URI.
        """
        record = UnresolvedPartRecord(
            part_path="one/state.dart",
            reason=REASON_ORPHANED_PART,
            declared_parent_path="package:foo/gone.dart",
        )
        assert record.to_dict() == {
            "part_path": "one/state.dart",
            "reason": REASON_ORPHANED_PART,
            "parent_path": None,
            "second_parent_path": None,
            "declared_parent_path": "package:foo/gone.dart",
        }

    def test_record_is_frozen(self):
        record = UnresolvedPartRecord(part_path="a.dart", reason=REASON_ORPHANED_PART)
        with pytest.raises(Exception):
            record.reason = REASON_CHAINED_PART  # type: ignore[misc]


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
class TestDartLibraryGrouping:
    """Grouping, collision, inactivity, and every recorded condition."""

    # ── Grouping ─────────────────────────────────────────────────────────────

    def test_two_file_library_prefixes_only_the_part(self, tmp_path):
        kb = _analyze(
            tmp_path,
            {
                "src/a/b.dart": "part 'state.dart';\n\nclass Widget {}\n",
                "src/a/state.dart": STORE_PART.format(parent="b.dart"),
            },
        )
        ids = _ids(kb)

        # Written literally, not composed from the production helper: if the
        # encoding changes, this line is what has to change with it.
        assert "src$-a$-b.state.Store" in ids
        assert "src$-a$-b.state.Store.save" in ids

        # The parent is an ordinary file with its own stem.
        assert "b.Widget" in ids
        assert "src$-a$-b.b.Widget" not in ids

    def test_two_libraries_sharing_a_part_file_name_no_longer_collide(self, tmp_path):
        """Without the qualifier both part files yield ``state.Store`` and the catalog's
        last-write-wins at insert drops one, so the COUNT is asserted and not only the
        ids.
        """
        kb = _analyze(
            tmp_path,
            {
                "lib/models/models.dart": "part 'state.dart';\n",
                "lib/models/state.dart": STORE_PART.format(parent="models.dart"),
                "lib/widgets/widgets.dart": "part 'state.dart';\n",
                "lib/widgets/state.dart": STORE_PART.format(parent="widgets.dart"),
            },
        )
        ids = _ids(kb)

        assert "lib$-models$-models.state.Store" in ids
        assert "lib$-widgets$-widgets.state.Store" in ids
        assert len([i for i in ids if i.endswith("state.Store")]) == 2
        assert "state.Store" not in ids

    def test_a_mixed_case_extension_does_not_collide_with_its_dart_sibling(self, tmp_path):
        """``CodeAnalyzer`` admits a file to the Dart set by lower-casing its suffix, so
        ``b.DART`` is analysed; a strip of only a lower-case ``.dart`` encoded
        ``src/a/b.DART`` and ``src/a/b.DART.dart`` to the one prefix.
        """
        kb = _analyze(
            tmp_path,
            {
                "src/a/b.DART": "part 'p/state.dart';\n",
                "src/a/p/state.dart": STORE_PART.format(parent="../b.DART"),
                "src/a/b.DART.dart": "part 'q/state.dart';\n",
                "src/a/q/state.dart": STORE_PART.format(parent="../b.DART.dart"),
            },
        )
        ids = _ids(kb)

        # The non-canonical spelling is written back after the escaping, so it
        # cannot be spelled by a segment's own content.
        assert "src$-a$-b$.DART.state.Store" in ids
        assert "src$-a$-b.DART.state.Store" in ids
        # Both survived: under the old strip one replaced the other at insert.
        assert len([i for i in ids if i.endswith("state.Store")]) == 2

    def test_a_uri_with_an_escaped_quote_groups_the_file_it_names(self, tmp_path):
        """``part 'od\\'d.dart';`` is a legal directive naming ``od'd.dart``. While the
        backslash was kept the URI matched no analysed file, and a sound library was
        recorded as an unresolved part.
        """
        kb = _analyze(
            tmp_path,
            {
                "src/a/b.dart": "part 'od\\'d.dart';\n\nclass Widget {}\n",
                "src/a/od'd.dart": STORE_PART.format(parent="b.dart"),
            },
        )
        ids = _ids(kb)

        assert "src$-a$-b.od'd.Store" in ids
        assert "src$-a$-b.od'd.Store.save" in ids
        # Not left to its own stem, and no longer recorded as unresolved.
        assert "od'd.Store" not in ids
        assert _records(kb) == []

    def test_three_separator_forms_stay_distinct(self, tmp_path):
        """A dotted join would collapse ``src/a/b`` and ``src/a.b`` to one prefix, and
        without the ``$$`` doubling a segment could spell the ``$-`` separator itself —
        which is what makes this an injectivity test rather than a path test.
        """
        kb = _analyze(
            tmp_path,
            {
                "src/a/b.dart": "part 'sub/state.dart';\n",
                "src/a/sub/state.dart": STORE_PART.format(parent="../b.dart"),
                "src/a.b.dart": "part 'ab/state.dart';\n",
                "src/ab/state.dart": STORE_PART.format(parent="../a.b.dart"),
                "src/a$b.dart": "part 'dollar/state.dart';\n",
                "src/dollar/state.dart": STORE_PART.format(parent="../a$b.dart"),
            },
        )
        ids = _ids(kb)

        assert "src$-a$-b.state.Store" in ids
        assert "src$-a.b.state.Store" in ids
        assert "src$-a$$b.state.Store" in ids
        assert len([i for i in ids if i.endswith("state.Store")]) == 3

    def test_a_dollar_directory_and_a_dollar_filename_both_survive(self, tmp_path):
        """A single-``$`` join gave ``src/a$/b.dart`` and ``src/a/$b.dart`` the one
        prefix ``src$a$$$b``, so one of the two ``state.Store`` components was dropped at
        insert.
        """
        kb = _analyze(
            tmp_path,
            {
                "src/a$/b.dart": "part 'state.dart';\n",
                "src/a$/state.dart": STORE_PART.format(parent="b.dart"),
                "src/a/$b.dart": "part 'state.dart';\n",
                "src/a/state.dart": STORE_PART.format(parent="$b.dart"),
            },
        )
        ids = _ids(kb)

        assert "src$-a$$$-b.state.Store" in ids
        assert "src$-a$-$$b.state.Store" in ids
        assert len([i for i in ids if i.endswith("state.Store")]) == 2

    def test_a_dollar_directory_and_a_dollar_filename_survive_at_depth(self, tmp_path):
        """``src/x$/y/z.dart`` and ``src/x/$y/z.dart`` also encoded alike under the old
        join, so the ambiguity is not an artefact of a two-segment pair.
        """
        kb = _analyze(
            tmp_path,
            {
                "src/x$/y/z.dart": "part 'state.dart';\n",
                "src/x$/y/state.dart": STORE_PART.format(parent="z.dart"),
                "src/x/$y/z.dart": "part 'state.dart';\n",
                "src/x/$y/state.dart": STORE_PART.format(parent="z.dart"),
            },
        )
        ids = _ids(kb)

        assert "src$-x$$$-y$-z.state.Store" in ids
        assert "src$-x$-$$y$-z.state.Store" in ids
        assert len([i for i in ids if i.endswith("state.Store")]) == 2

    def test_member_parent_reference_is_rewritten_with_its_class(self, tmp_path):
        """``parent_component`` is prefixed too, so the link survives."""
        kb = _analyze(
            tmp_path,
            {
                "src/a/b.dart": "part 'state.dart';\n",
                "src/a/state.dart": STORE_PART.format(parent="b.dart"),
            },
        )
        by_id = {_bare(c.id): c for c in kb.components}

        member = by_id["src$-a$-b.state.Store.save"]
        assert _bare(member.parent_component) == "src$-a$-b.state.Store"
        # The parent it points at is really in the catalog under that id.
        assert _bare(member.parent_component) in by_id

    # ── Inactivity and no regression ─────────────────────────────────────────

    def test_no_part_directive_anywhere_leaves_every_identity_alone(self, tmp_path):
        """Concrete identity strings, not a count: a re-keying that preserved the
        component count would pass a count assertion.
        """
        files = {
            "lib/shapes.dart": (
                "class Shape {\n  void area() {}\n}\n\n"
                "mixin Auditable {\n  void record() {}\n}\n\n"
                "extension ShapeReport on Shape {\n  void summarise() {}\n}\n\n"
                "void topLevel() {}\n"
            ),
            "lib/nested/other.dart": "class Other {}\n",
        }
        kb = _analyze(tmp_path, files)
        ids = _ids(kb)

        assert _qualifier(tmp_path).active is False
        for expected in (
            "shapes.Shape",
            "shapes.Shape.area",
            "shapes.Auditable",
            "shapes.Auditable.record",
            "shapes.ShapeReport",
            "shapes.ShapeReport.summarise",
            "shapes.topLevel",
            "other.Other",
        ):
            assert expected in ids, f"{expected} was re-keyed"
        assert not any("$" in i for i in ids)
        assert _records(kb) == []

    def test_parent_whose_only_part_is_an_excluded_generated_file(self, tmp_path):
        """``thing.g.dart`` matches Dart's configured ``generated_file_patterns``, so
        the scanner leaves it out of the analysed set and the directive resolves to
        nothing. A directive exists; nothing is rewritten.
        """
        kb = _analyze(
            tmp_path,
            {
                "lib/thing.dart": "part 'thing.g.dart';\n\nclass Thing {}\n",
                "lib/thing.g.dart": "part of 'thing.dart';\n\nclass ThingGenerated {}\n",
            },
        )
        ids = _ids(kb)

        assert _qualifier(tmp_path).active is False
        assert "thing.Thing" in ids
        assert not any("$" in i for i in ids)
        # The generated file was never analysed at all.
        assert "thing.g.ThingGenerated" not in ids

        unresolved = _reasons(kb, REASON_NOT_IN_ANALYSED_SET)
        assert len(unresolved) == 1
        assert unresolved[0]["part_path"] == "thing.g.dart"
        assert unresolved[0]["parent_path"] == "lib/thing.dart"
        assert len(_records(kb)) == 1

    # ── The recorded conditions ──────────────────────────────────────────────

    def test_absent_part_is_recorded_and_the_rest_still_groups(self, tmp_path):
        """A bad relationship never blocks a sound one."""
        kb = _analyze(
            tmp_path,
            {
                "lib/thing.dart": "part 'missing.dart';\npart 'real.dart';\n",
                "lib/real.dart": STORE_PART.format(parent="thing.dart"),
            },
        )
        ids = _ids(kb)

        unresolved = _reasons(kb, REASON_NOT_IN_ANALYSED_SET)
        assert len(unresolved) == 1
        assert unresolved[0]["part_path"] == "missing.dart"
        assert unresolved[0]["parent_path"] == "lib/thing.dart"

        assert "lib$-thing.real.Store" in ids

    def test_a_part_claimed_by_two_parents_is_recorded_not_resolved(self, tmp_path):
        """Conflicted, unprefixed, and siblings unaffected."""
        kb = _analyze(
            tmp_path,
            {
                "lib/one.dart": "part 'shared.dart';\npart 'one_only.dart';\n",
                "lib/two.dart": "part 'shared.dart';\npart 'two_only.dart';\n",
                "lib/shared.dart": "part of 'one.dart';\n\nclass Shared {}\n",
                "lib/one_only.dart": "part of 'one.dart';\n\nclass OneOnly {}\n",
                "lib/two_only.dart": "part of 'two.dart';\n\nclass TwoOnly {}\n",
            },
        )
        ids = _ids(kb)

        conflicts = _reasons(kb, REASON_CLAIMED_BY_TWO_PARENTS)
        assert len(conflicts) == 1
        assert conflicts[0]["part_path"] == "lib/shared.dart"
        assert {conflicts[0]["parent_path"], conflicts[0]["second_parent_path"]} == {
            "lib/one.dart",
            "lib/two.dart",
        }

        # The conflicted part keeps the identity the parser produced.
        assert "shared.Shared" in ids
        assert not any(i.endswith("shared.Shared") and "$" in i for i in ids)

        # Each parent's own other part is still grouped.
        assert "lib$-one.one_only.OneOnly" in ids
        assert "lib$-two.two_only.TwoOnly" in ids

        # A conflicted part is NOT also recorded as an orphan: its parent IS in
        # the analysed set.
        assert _reasons(kb, REASON_ORPHANED_PART) == []

    def test_a_chained_part_leaves_the_whole_chain_unprefixed(self, tmp_path):
        kb = _analyze(
            tmp_path,
            {
                "chain/a.dart": "part 'b.dart';\n",
                "chain/b.dart": "part of 'a.dart';\npart 'c.dart';\n\nclass Middle {}\n",
                "chain/c.dart": "part of 'b.dart';\n\nclass Inner {}\n",
            },
        )
        ids = _ids(kb)

        chained = _reasons(kb, REASON_CHAINED_PART)
        # Both ends are named: the chained file itself, and the claim it made.
        assert {(row["part_path"], row["parent_path"]) for row in chained} == {
            ("chain/b.dart", "chain/a.dart"),
            ("chain/c.dart", "chain/b.dart"),
        }

        assert "b.Middle" in ids
        assert "c.Inner" in ids
        assert not any("$" in i for i in ids)
        # Neither chain member is an orphan; both have a parent in the set.
        assert _reasons(kb, REASON_ORPHANED_PART) == []

    def test_a_four_file_chain_records_each_dropped_claim_exactly_once(self, tmp_path):
        """A removal that walks outwards from each chained file re-records a member it
        has already removed: ``a -> b -> c -> d`` emits ``c`` twice, the second row with
        no parent at all. One row per dropped claim, each naming its own parent.
        """
        kb = _analyze(
            tmp_path,
            {
                "chain/a.dart": "part 'b.dart';\n",
                "chain/b.dart": "part of 'a.dart';\npart 'c.dart';\n\nclass Middle {}\n",
                "chain/c.dart": "part of 'b.dart';\npart 'd.dart';\n\nclass Inner {}\n",
                "chain/d.dart": "part of 'c.dart';\n\nclass Leaf {}\n",
            },
        )
        ids = _ids(kb)

        chained = _reasons(kb, REASON_CHAINED_PART)
        assert len(chained) == 3
        assert {(row["part_path"], row["parent_path"]) for row in chained} == {
            ("chain/b.dart", "chain/a.dart"),
            ("chain/c.dart", "chain/b.dart"),
            ("chain/d.dart", "chain/c.dart"),
        }
        assert all(row["parent_path"] is not None for row in chained)

        # No member of the chain is prefixed, however deep it runs.
        for expected in ("b.Middle", "c.Inner", "d.Leaf"):
            assert expected in ids
        assert not any("$" in i for i in ids)
        assert _reasons(kb, REASON_ORPHANED_PART) == []

    def test_a_two_file_cycle_records_each_claim_once_with_its_parent(self, tmp_path):
        """A cycle is a chain that closes, and records the same way."""
        kb = _analyze(
            tmp_path,
            {
                "cycle/a.dart": "part of 'b.dart';\npart 'b.dart';\n\nclass First {}\n",
                "cycle/b.dart": "part of 'a.dart';\npart 'a.dart';\n\nclass Second {}\n",
            },
        )
        ids = _ids(kb)

        chained = _reasons(kb, REASON_CHAINED_PART)
        assert len(chained) == 2
        assert {(row["part_path"], row["parent_path"]) for row in chained} == {
            ("cycle/b.dart", "cycle/a.dart"),
            ("cycle/a.dart", "cycle/b.dart"),
        }
        assert all(row["parent_path"] is not None for row in chained)

        assert "a.First" in ids
        assert "b.Second" in ids
        assert not any("$" in i for i in ids)

    def test_a_self_declaring_part_records_one_claim(self, tmp_path):
        """A file naming itself as its own part: one row, naming itself."""
        kb = _analyze(
            tmp_path,
            {
                "self/a.dart": "part of 'a.dart';\npart 'a.dart';\n\nclass Only {}\n",
            },
        )
        chained = _reasons(kb, REASON_CHAINED_PART)
        assert [(row["part_path"], row["parent_path"]) for row in chained] == [
            ("self/a.dart", "self/a.dart")
        ]
        assert not any("$" in i for i in _ids(kb))

    def test_a_chain_never_blocks_an_unrelated_sound_library(self, tmp_path):
        """The chain removal walks every surviving claim, so a sound library in the same
        run must come through it untouched, and be recorded nowhere.
        """
        kb = _analyze(
            tmp_path,
            {
                "chain/a.dart": "part 'b.dart';\n",
                "chain/b.dart": "part of 'a.dart';\npart 'c.dart';\n\nclass Middle {}\n",
                "chain/c.dart": "part of 'b.dart';\n\nclass Inner {}\n",
                "ok/parent.dart": "part 'state.dart';\n",
                "ok/state.dart": STORE_PART.format(parent="parent.dart"),
            },
        )
        ids = _ids(kb)

        assert "ok$-parent.state.Store" in ids
        assert "b.Middle" in ids
        assert "c.Inner" in ids
        assert len(_reasons(kb, REASON_CHAINED_PART)) == 2
        assert len(_records(kb)) == 2

    def test_two_orphaned_parts_with_the_same_name_still_collide(self, tmp_path):
        """Prefixing an orphan would mean trusting a ``part of`` path to a parent this
        run cannot see, so the records make the loss visible rather than prevent it.
        """
        kb = _analyze(
            tmp_path,
            {
                "one/state.dart": STORE_PART.format(parent="gone_one.dart"),
                "two/state.dart": STORE_PART.format(parent="gone_two.dart"),
            },
        )
        ids = _ids(kb)

        orphans = _reasons(kb, REASON_ORPHANED_PART)
        assert {row["part_path"] for row in orphans} == {"one/state.dart", "two/state.dart"}
        assert all(row["parent_path"] is None for row in orphans)
        # The declared target is carried, which is how a reader tells a typo
        # from an excluded file.
        assert {row["declared_parent_path"] for row in orphans} == {
            "gone_one.dart",
            "gone_two.dart",
        }

        # Nothing is prefixed, so last-write-wins still drops one of the pair.
        assert not any("$" in i for i in ids)
        assert len([i for i in ids if i == "state.Store"]) == 1

    # ── Isolation ────────────────────────────────────────────────────────────

    def test_other_languages_are_never_dart_prefixed(self, tmp_path):
        """The mixed-tree case: an active Dart qualifier running over a tree that also
        holds Ruby and Python.
        """
        kb = _analyze(
            tmp_path,
            {
                "src/a/b.dart": "part 'state.dart';\n",
                "src/a/state.dart": STORE_PART.format(parent="b.dart"),
                "src/a/user.rb": "class User\n  def save\n    1\n  end\nend\n",
                "src/a/tool.py": "def run():\n    return 1\n",
            },
        )
        ids = _ids(kb)

        assert "src$-a$-b.state.Store" in ids  # the Dart qualifier really ran
        assert "user.User" in ids
        assert "user.User.save" in ids
        assert "tool.run" in ids
        assert "src$-a$-b.user.User" not in ids
        assert "src$-a$-b.tool.run" not in ids

    def test_the_standing_dart_fixture_declares_no_part(self):
        """The qualifier is inactive over the fixtures directory and records nothing, so
        it cannot have re-keyed anything there. The fixture itself is not read, modified,
        or copied.
        """
        qualifier = _qualifier(FIXTURES_DIR)
        assert qualifier.active is False
        assert qualifier.unresolved_records == []


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
class TestQualifiedIdentityIsAddressable:
    """A qualified component is still requestable, which is why ``$`` and not ``/``.

    The query API declares ``/{space_id}/components/{name}`` with ``{name}`` as ONE
    path segment and ``/flow`` and ``/deps`` as suffixes of it, so an id containing
    ``/`` could be stored and never asked for. The request goes through real URL
    routing, the thing a ``/`` separator would break.
    """

    @pytest.fixture
    def indexed(self, tmp_path, monkeypatch):
        """Index a two-file library and serve it through the real query router."""
        starlette = pytest.importorskip("starlette.testclient")
        from fastapi import FastAPI

        import src.api.auth as auth_mod
        import src.api.gates as gates_mod
        from src.api.routes import query as query_routes
        from src.flow.flow_analyzer import FlowAnalyzer
        from src.service.codekb_service import CodeKBService

        kb = _analyze(
            tmp_path,
            {
                "src/a/b.dart": "part 'state.dart';\n",
                "src/a/state.dart": STORE_PART.format(parent="b.dart"),
            },
        )

        # Rebuilt from the knowledge base the way the service builds a context,
        # so the catalog is keyed by the persisted (space-prefixed) identity.
        catalog = ComponentCatalog()
        for component in kb.components:
            catalog.add_component(component)

        # Bare instance: only _load_context is exercised, and it is patched
        # below. object.__new__ steps past the singleton guard, which exists to
        # stop a second service being built for real, not to block a double.
        service = object.__new__(CodeKBService)
        monkeypatch.setattr(
            CodeKBService,
            "_load_context",
            lambda self, space_id: SimpleNamespace(
                catalog=catalog,
                knowledge_base=kb,
                flow_analyzer=FlowAnalyzer(catalog),
            ),
        )
        monkeypatch.setattr(
            query_routes,
            "require_current_user",
            lambda request: SimpleNamespace(email="test@localhost", sub="test"),
        )
        # Routing is what is under test, so both access seams are stood down:
        # the read gate returns before it reads any space record, and auth
        # falls back to its local-dev identity instead of answering 401.
        monkeypatch.setattr(auth_mod, "_REQUIRE_AUTH", False)
        monkeypatch.setattr(gates_mod, "require_authorization", lambda: False)
        # The routes reach the service through CodeKBService.instance(), so that
        # is what a double replaces here.
        monkeypatch.setattr(CodeKBService, "instance", classmethod(lambda cls: service))

        app = FastAPI()
        app.include_router(query_routes.router, prefix="/api/spaces")
        with starlette.TestClient(app) as client:
            yield SimpleNamespace(
                client=client,
                ids={_bare(c.id): c.id for c in kb.components},
            )

    def test_qualified_component_is_requestable(self, indexed):
        # Requested by its OWN stored identity, so a separator that cannot sit
        # in one path segment produces a real routing miss here.
        stored = indexed.ids["src$-a$-b.state.Store"]
        response = indexed.client.get(f"/api/spaces/{SPACE_ID}/components/{stored}")
        assert response.status_code == 200, response.text
        assert response.json()["id"] == stored

    def test_qualified_component_flow_suffix_is_requestable(self, indexed):
        stored = indexed.ids["src$-a$-b.state.Store.save"]
        response = indexed.client.get(f"/api/spaces/{SPACE_ID}/components/{stored}/flow")
        assert response.status_code == 200, response.text
        assert response.json()["start_component"] == stored

    def test_the_qualified_identity_carries_no_path_separator(self, indexed):
        for identity in indexed.ids:
            assert "/" not in identity

    def test_an_unknown_qualified_name_is_a_clean_404_not_a_routing_miss(self, indexed):
        """A 404 from the handler proves the path reached the handler at all."""
        name = f"{SPACE_ID}::src$-a$-b.state.Gone"
        response = indexed.client.get(f"/api/spaces/{SPACE_ID}/components/{name}")
        assert response.status_code == 404
        assert "src$-a$-b.state.Gone" in response.json()["detail"]


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
class TestDirectiveReadingEdges:
    """File-and-format conditions, since the directive reader opens files itself."""

    def test_byte_order_mark_before_the_first_directive(self, tmp_path):
        kb = _analyze(
            tmp_path,
            {
                "src/a/b.dart": "\ufeffpart 'state.dart';\n",
                "src/a/state.dart": STORE_PART.format(parent="b.dart"),
            },
        )
        assert "src$-a$-b.state.Store" in _ids(kb)

    def test_crlf_line_endings(self, tmp_path):
        kb = _analyze(
            tmp_path,
            {
                "src/a/b.dart": "library thing;\r\npart 'state.dart';\r\n\r\nclass Widget {}\r\n",
                "src/a/state.dart": "part of 'b.dart';\r\n\r\nclass Store {}\r\n",
            },
        )
        assert "src$-a$-b.state.Store" in _ids(kb)

    def test_bytes_that_are_not_valid_utf8_do_not_raise(self, tmp_path):
        _write_tree(tmp_path, {"src/a/state.dart": STORE_PART.format(parent="b.dart")})
        parent = tmp_path / "src" / "a" / "b.dart"
        parent.parent.mkdir(parents=True, exist_ok=True)
        parent.write_bytes(b"// \xff\xfe not utf-8\npart 'state.dart';\n")

        # The directive read must survive the invalid bytes, and the surviving
        # directive must still group the part.
        kb = CodeAnalyzer(ComponentCatalog()).analyze_directory(tmp_path, space_id=SPACE_ID, id_scheme=IdScheme.V1_STEM)
        assert "src$-a$-b.state.Store" in _ids(kb)

    def test_a_part_in_a_line_or_block_comment_is_not_a_directive(self, tmp_path):
        kb = _analyze(
            tmp_path,
            {
                "src/a/b.dart": (
                    "// part 'commented.dart';\n"
                    "/* part 'blocked.dart'; */\n"
                    "/*\npart 'multiline.dart';\n*/\n"
                    "part 'state.dart';\n"
                ),
                "src/a/state.dart": STORE_PART.format(parent="b.dart"),
                "src/a/commented.dart": "class Commented {}\n",
                "src/a/blocked.dart": "class Blocked {}\n",
                "src/a/multiline.dart": "class Multiline {}\n",
            },
        )
        ids = _ids(kb)

        assert "src$-a$-b.state.Store" in ids
        # None of the commented-out names was ever read as a directive, so none
        # of those files was grouped and none was recorded as unresolved.
        assert "commented.Commented" in ids
        assert "blocked.Blocked" in ids
        assert "multiline.Multiline" in ids
        assert not any(i.startswith("src$-a$-b.commented") for i in ids)
        assert _records(kb) == []

    def test_a_nested_block_comment_closes_only_at_its_outer_marker(self, tmp_path):
        """Dart block comments nest, so ending the block at the first ``*/`` read the
        tail of ``/* outer /* inner */ part 'evil.dart'; */`` as a live directive, whose
        phantom claim turned a sound relationship into a two-parent conflict.
        """
        kb = _analyze(
            tmp_path,
            {
                "src/a/b.dart": (
                    "/* outer /* inner */ part 'evil.dart'; */\n"
                    "/* outer\n/* inner */\npart 'also_evil.dart';\n*/\n"
                    "part 'state.dart';\n"
                ),
                "src/a/state.dart": STORE_PART.format(parent="b.dart"),
                "src/a/other.dart": "part 'evil.dart';\n",
                "src/a/evil.dart": "part of 'other.dart';\n\nclass Evil {}\n",
                "src/a/also_evil.dart": "class AlsoEvil {}\n",
            },
        )
        ids = _ids(kb)

        # b.dart's only real directive still groups its own part.
        assert "src$-a$-b.state.Store" in ids
        # other.dart's claim on evil.dart is sound and survives untouched: no
        # phantom second claimant, so no conflict.
        assert "src$-a$-other.evil.Evil" in ids
        assert _reasons(kb, REASON_CLAIMED_BY_TWO_PARENTS) == []
        # The commented-out second name was never read either.
        assert "also_evil.AlsoEvil" in ids
        assert _reasons(kb, REASON_NOT_IN_ANALYSED_SET) == []

    def test_a_raw_string_uri_is_read(self, tmp_path):
        """``part r'a.dart';`` is legal Dart."""
        kb = _analyze(
            tmp_path,
            {
                "src/a/b.dart": "part r'state.dart';\n",
                "src/a/state.dart": STORE_PART.format(parent="b.dart"),
            },
        )
        assert "src$-a$-b.state.Store" in _ids(kb)

    def test_adjacent_string_literals_are_joined_not_garbled(self, tmp_path):
        """``part 'a' '.dart';`` concatenates in Dart. The old pattern matched this form
        with the target ``a' '.dart``, which names no file.
        """
        kb = _analyze(
            tmp_path,
            {
                "src/a/b.dart": "part 'state' '.dart';\n",
                "src/a/state.dart": STORE_PART.format(parent="b.dart"),
            },
        )
        ids = _ids(kb)
        assert "src$-a$-b.state.Store" in ids
        assert _records(kb) == []

    def test_an_unreadable_uri_form_records_nothing_rather_than_a_garbled_target(self, tmp_path):
        """An interpolated URI is not legal Dart in a directive, and a half-read target
        would name some other library's file and turn that sound claim into a two-parent
        conflict. Nothing is guessed at.
        """
        parent = tmp_path / "b.dart"
        parent.write_text("part 'pre$name.dart'.toString();\npart 'state.dart';\n")
        qualifier = DartLibraryQualifier(tmp_path, [])
        parts, _, _ = qualifier._read_directives(parent)
        assert parts == ["state.dart"]

    def test_an_unterminated_uri_literal_records_nothing(self, tmp_path):
        """A quote that never closes names no file, so it claims none."""
        parent = tmp_path / "b.dart"
        parent.write_text("part 'unterminated.dart;\npart 'state.dart';\n")
        parts, _, _ = DartLibraryQualifier(tmp_path, [])._read_directives(parent)
        assert "unterminated.dart" not in parts
        assert not any("'" in uri for uri in parts)

    def test_a_bare_part_of_is_an_orphan_naming_nothing(self, tmp_path):
        """``part of;`` is a ``part of`` with no target: recorded, nothing named."""
        orphan = tmp_path / "state.dart"
        orphan.write_text("part of;\n\nclass Store {}\n")
        records = DartLibraryQualifier(tmp_path, [orphan]).unresolved_records
        assert len(records) == 1
        assert records[0].reason == REASON_ORPHANED_PART
        assert records[0].declared_parent_path is None

    def test_a_leading_script_line_does_not_end_the_directive_scan(self, tmp_path):
        """A ``#!`` line is not a statement, so it must not stop the scan."""
        kb = _analyze(
            tmp_path,
            {
                "src/a/b.dart": "#!/usr/bin/env dart\npart 'state.dart';\n\nclass Widget {}\n",
                "src/a/state.dart": STORE_PART.format(parent="b.dart"),
            },
        )
        ids = _ids(kb)
        assert "src$-a$-b.state.Store" in ids
        assert "b.Widget" in ids
        assert _records(kb) == []

    def test_an_orphan_naming_a_package_uri_carries_that_uri(self, tmp_path):
        """The declared target distinguishes a package URI from a typo."""
        kb = _analyze(
            tmp_path,
            {
                "lib/state.dart": "part of 'package:foo/bar.dart';\n\nclass Store {}\n",
            },
        )
        orphans = _reasons(kb, REASON_ORPHANED_PART)
        assert len(orphans) == 1
        assert orphans[0]["part_path"] == "lib/state.dart"
        assert orphans[0]["declared_parent_path"] == "package:foo/bar.dart"

    def test_an_orphan_naming_a_library_rather_than_a_uri_carries_the_name(self, tmp_path):
        """The legacy form ``part of my.lib;`` names a library, not a path."""
        parent = tmp_path / "state.dart"
        parent.write_text("part of my.lib.name;\n\nclass Store {}\n")
        qualifier = DartLibraryQualifier(tmp_path, [parent])
        records = qualifier.unresolved_records
        assert len(records) == 1
        assert records[0].reason == REASON_ORPHANED_PART
        assert records[0].declared_parent_path == "my.lib.name"

    def test_a_parent_resolving_outside_the_source_root_is_recorded(self, tmp_path):
        """``_prefix_for`` has no repo-relative path to build from when the parent
        resolves outside the analysed root, so the sound claim cannot be used. It is
        recorded as ``not-in-analysed-set`` rather than widening the closed reason set.
        """
        root = tmp_path / "repo"
        root.mkdir()
        part = root / "state.dart"
        part.write_text("part of '../outside.dart';\n\nclass Store {}\n")
        outside = tmp_path / "outside.dart"
        outside.write_text("part 'repo/state.dart';\n")

        qualifier = DartLibraryQualifier(root, [outside, part])

        assert qualifier.active is False
        records = qualifier.unresolved_records
        assert len(records) == 1
        assert records[0].reason == REASON_NOT_IN_ANALYSED_SET
        assert records[0].part_path == "state.dart"
        # The parent keeps its full path, having none relative to the root.
        assert records[0].parent_path == str(outside.resolve())
        # The part WAS analysed, so its own `part of` is on record and says
        # which end left the analysed set.
        assert records[0].declared_parent_path == "../outside.dart"
        # Not reported as an orphan: a parent in the analysed set did claim it.
        assert all(r.reason != REASON_ORPHANED_PART for r in records)

    def test_an_out_of_root_parent_is_distinguishable_from_an_excluded_part(self, tmp_path):
        """Both rows read ``not-in-analysed-set``; which end of the relationship left
        the set differs, and ``declared_parent_path`` carries that difference — always
        set on the out-of-root row, never on the ordinary one.
        """
        root = tmp_path / "repo"
        root.mkdir()
        part = root / "state.dart"
        part.write_text("part of '../outside.dart';\n\nclass Store {}\n")
        outside = tmp_path / "outside.dart"
        outside.write_text("part 'repo/state.dart';\n")
        ordinary = root / "models.dart"
        ordinary.write_text("part 'models.g.dart';\n")

        records = DartLibraryQualifier(root, [outside, part, ordinary]).unresolved_records

        rows = [r for r in records if r.reason == REASON_NOT_IN_ANALYSED_SET]
        assert len(rows) == 2
        out_of_root = [r for r in rows if r.declared_parent_path is not None]
        excluded = [r for r in rows if r.declared_parent_path is None]
        assert len(out_of_root) == 1
        assert len(excluded) == 1
        # The out-of-root row names the parent that left the set.
        assert out_of_root[0].part_path == "state.dart"
        assert out_of_root[0].declared_parent_path == "../outside.dart"
        # The ordinary row names the part that was never analysed, as the
        # directive wrote it, and names no parent beyond the one declaring it.
        assert excluded[0].part_path == "models.g.dart"
        assert excluded[0].parent_path == "models.dart"

    def test_an_out_of_root_parent_whose_part_declares_nothing_still_names_it(self, tmp_path):
        """A parent may claim a file that declares no ``part of`` of its own; the field
        then falls back to the parent path the run resolved.
        """
        root = tmp_path / "repo"
        root.mkdir()
        part = root / "state.dart"
        part.write_text("class Store {}\n")
        outside = tmp_path / "outside.dart"
        outside.write_text("part 'repo/state.dart';\n")

        records = DartLibraryQualifier(root, [outside, part]).unresolved_records

        assert len(records) == 1
        assert records[0].reason == REASON_NOT_IN_ANALYSED_SET
        assert records[0].declared_parent_path == str(outside.resolve())

    def test_an_empty_file_and_a_directives_only_file(self, tmp_path):
        kb = _analyze(
            tmp_path,
            {
                "src/empty.dart": "",
                "src/a/b.dart": "library thing;\nimport 'dart:async'\n    show Future;\npart 'state.dart';\n",
                "src/a/state.dart": STORE_PART.format(parent="b.dart"),
            },
        )
        # A multi-line import statement does not end the directive section, and
        # an empty file contributes nothing and raises nothing.
        assert "src$-a$-b.state.Store" in _ids(kb)

    def test_a_part_after_the_first_declaration_is_not_read(self, tmp_path):
        """Dart requires directives to precede declarations; the scan stops there."""
        kb = _analyze(
            tmp_path,
            {
                "src/a/b.dart": "class Widget {}\n\npart 'state.dart';\n",
                "src/a/state.dart": "class Store {}\n",
            },
        )
        ids = _ids(kb)
        assert "state.Store" in ids
        assert not any("$" in i for i in ids)
        assert _records(kb) == []

    def test_package_and_dart_uris_are_recorded_as_not_in_the_analysed_set(self, tmp_path):
        kb = _analyze(
            tmp_path,
            {
                "lib/thing.dart": "part 'package:foo/bar.dart';\npart 'dart:core';\n",
            },
        )
        unresolved = _reasons(kb, REASON_NOT_IN_ANALYSED_SET)
        assert {row["part_path"] for row in unresolved} == {"package:foo/bar.dart", "dart:core"}
        assert not any("$" in i for i in _ids(kb))

    def test_an_explicit_library_name_is_recorded_but_never_used(self, tmp_path):
        """The prefix always comes from the path."""
        _write_tree(
            tmp_path,
            {
                "src/a/b.dart": "library my.lib.name;\npart 'state.dart';\n",
                "src/a/state.dart": STORE_PART.format(parent="b.dart"),
            },
        )
        qualifier = _qualifier(tmp_path)
        assert "my.lib.name" in qualifier.library_names.values()

        kb = CodeAnalyzer(ComponentCatalog()).analyze_directory(tmp_path, space_id=SPACE_ID, id_scheme=IdScheme.V1_STEM)
        ids = _ids(kb)
        assert "src$-a$-b.state.Store" in ids
        assert not any("my$-lib$-name" in i or "my.lib.name" in i for i in ids)

    def test_no_dart_files_means_no_metadata_key(self, tmp_path):
        """The qualifier is gated, so a non-Dart repository pays nothing."""
        kb = _analyze(tmp_path, {"src/tool.py": "def run():\n    return 1\n"})
        assert DART_UNRESOLVED_PARTS_KEY not in kb.analysis_metadata


class TestChainRemovalRecordShape:
    """The chain pass's record shape, driven in a FIXED claim order.

    The end-to-end chain tests read whatever order the scanner hands the qualifier,
    and that order decided whether an outward-walking removal re-recorded a member it
    had already popped. This asserts the record shape only.
    """

    @staticmethod
    def _claims(tmp_path, pairs):
        return {tmp_path / part: tmp_path / parent for part, parent in pairs}

    def test_a_four_file_chain_in_chain_order_records_one_row_per_claim(self, tmp_path):
        qualifier = DartLibraryQualifier(tmp_path, [])
        claims = self._claims(
            tmp_path,
            [("b.dart", "a.dart"), ("c.dart", "b.dart"), ("d.dart", "c.dart")],
        )
        parents = {tmp_path / name for name in ("a.dart", "b.dart", "c.dart")}

        qualifier._drop_chains(claims, parents)

        assert claims == {}
        rows = qualifier.unresolved_records
        assert len(rows) == 3
        assert all(row.reason == REASON_CHAINED_PART for row in rows)
        # No row carries an absent parent: that shape belongs to an orphan.
        assert all(row.parent_path is not None for row in rows)
        assert {(row.part_path, row.parent_path) for row in rows} == {
            ("b.dart", "a.dart"),
            ("c.dart", "b.dart"),
            ("d.dart", "c.dart"),
        }

    def test_a_cycle_in_claim_order_records_one_row_per_claim(self, tmp_path):
        qualifier = DartLibraryQualifier(tmp_path, [])
        claims = self._claims(tmp_path, [("b.dart", "a.dart"), ("a.dart", "b.dart")])
        parents = {tmp_path / name for name in ("a.dart", "b.dart")}

        qualifier._drop_chains(claims, parents)

        assert claims == {}
        rows = qualifier.unresolved_records
        assert len(rows) == 2
        assert all(row.parent_path is not None for row in rows)
        assert {(row.part_path, row.parent_path) for row in rows} == {
            ("b.dart", "a.dart"),
            ("a.dart", "b.dart"),
        }

    def test_a_run_with_no_chain_records_nothing_and_keeps_every_claim(self, tmp_path):
        qualifier = DartLibraryQualifier(tmp_path, [])
        claims = self._claims(tmp_path, [("b.dart", "a.dart"), ("c.dart", "a.dart")])
        before = dict(claims)

        qualifier._drop_chains(claims, {tmp_path / "a.dart"})

        assert claims == before
        assert qualifier.unresolved_records == []


class TestPrefixEncodingUnit:
    """The encoding itself, independent of the grammar being installed."""

    def test_escaping_happens_before_joining(self, tmp_path):
        """A literal ``$`` in a segment is doubled; the segments are joined with the
        two-character ``$-``. Written out rather than composed, so a change to the
        encoding has to change this list.
        """
        qualifier = DartLibraryQualifier(tmp_path, [])
        prefix_for = qualifier._prefix_for

        assert prefix_for(tmp_path / "src" / "a" / "b.dart") == "src$-a$-b"
        assert prefix_for(tmp_path / "src" / "a.b.dart") == "src$-a.b"
        assert prefix_for(tmp_path / "src" / "a$b.dart") == "src$-a$$b"
        assert prefix_for(tmp_path / "src" / "a$" / "b.dart") == "src$-a$$$-b"
        assert prefix_for(tmp_path / "src" / "a" / "$b.dart") == "src$-a$-$$b"
        assert prefix_for(tmp_path / "src" / "a" / "b.DART") == "src$-a$-b$.DART"

    def test_a_mixed_case_extension_keeps_its_own_prefix(self, tmp_path):
        """The analyzer admits a Dart file by lower-casing its suffix, so all four
        spellings below are analysed. Stripping only a lower-case ``.dart`` collapses
        ``b.DART`` onto ``b.DART.dart``; discarding the spelling, onto ``b.dart``.
        """
        prefix_for = DartLibraryQualifier(tmp_path, [])._prefix_for

        assert prefix_for(tmp_path / "src/a/b.DART") == "src$-a$-b$.DART"
        assert prefix_for(tmp_path / "src/a/b.DART.dart") == "src$-a$-b.DART"
        assert prefix_for(tmp_path / "src/a/b.DART") != prefix_for(tmp_path / "src/a/b.DART.dart")
        assert prefix_for(tmp_path / "src/a/b.DART") != prefix_for(tmp_path / "src/a/b.dart")
        assert prefix_for(tmp_path / "src/a/b.Dart") != prefix_for(tmp_path / "src/a/b.Dart.dart")
        assert prefix_for(tmp_path / "src/a/b.Dart") != prefix_for(tmp_path / "src/a/b.dart")
        # The marker is not content: a real dollar in the stem still doubles, so
        # `b$.DART.dart` cannot spell what `b.DART` encodes to.
        assert prefix_for(tmp_path / "src/a/b$.DART.dart") == "src$-a$-b$$.DART"

    def test_a_dollar_in_a_directory_and_at_a_filename_start_do_not_collide(self, tmp_path):
        """Under a single-``$`` join a run of three ``$`` reads either as a literal
        ``$`` ending a segment plus the separator, or as the separator plus a literal
        ``$`` beginning the next; a two-character separator cannot be spelled by content.
        """
        prefix_for = DartLibraryQualifier(tmp_path, [])._prefix_for

        assert prefix_for(tmp_path / "src/a$/b.dart") != prefix_for(tmp_path / "src/a/$b.dart")
        assert prefix_for(tmp_path / "src/x$/y/z.dart") != prefix_for(tmp_path / "src/x/$y/z.dart")

    def test_the_encoding_is_injective_over_a_dollar_heavy_sample(self, tmp_path):
        """Every sample differs only in where its ``$`` and its separators sit, so a
        single collapsed pair fails the assertion and names itself.
        """
        prefix_for = DartLibraryQualifier(tmp_path, [])._prefix_for
        paths = [
            "src/a/b.dart",
            "src/a.b.dart",
            "src/a$b.dart",
            "src/a$/b.dart",
            "src/a/$b.dart",
            "src/a$$/b.dart",
            "src/a/$$b.dart",
            "src/a$-/b.dart",
            "src/a/$-b.dart",
            "src/x$/y/z.dart",
            "src/x/$y/z.dart",
            "src/a$b/c.dart",
            "src/a/b$c.dart",
            # The extension's own spelling is part of the path: every one of
            # these is admitted to the Dart set by the analyzer, and the first
            # three differ from `src/a/b.dart` only in that spelling.
            "src/a/b.DART",
            "src/a/b.Dart",
            "src/a/b.dArt",
            "src/a/b.DART.dart",
            "src/a/b.Dart.dart",
            "src/a/b$.DART.dart",
            "src/a/b$.DART",
            "src/a$/b.DART",
            "src/a/$b.DART",
        ]
        prefixes = {}
        for path in paths:
            prefix = prefix_for(tmp_path / path)
            assert prefix not in prefixes, f"{path} collides with {prefixes.get(prefix)}"
            prefixes[prefix] = path
        assert len(prefixes) == len(paths)

    def test_only_the_final_dart_extension_is_dropped(self, tmp_path):
        qualifier = DartLibraryQualifier(tmp_path, [])
        assert qualifier._prefix_for(tmp_path / "lib" / "thing.g.dart") == "lib$-thing.g"

    def test_a_path_outside_the_source_directory_has_no_prefix(self, tmp_path):
        qualifier = DartLibraryQualifier(tmp_path, [])
        assert qualifier._prefix_for(Path("/elsewhere/b.dart").resolve()) is None

    def test_an_unreadable_file_yields_no_directives(self, tmp_path):
        qualifier = DartLibraryQualifier(tmp_path, [])
        assert qualifier._read_directives(tmp_path / "does-not-exist.dart") == ([], None, None)

    def test_qualify_is_a_no_op_when_inactive(self, tmp_path):
        qualifier = DartLibraryQualifier(tmp_path, [])
        assert qualifier.active is False
        # Nothing to mutate, and nothing raised on an empty component list.
        qualifier.qualify([])

    def test_an_annotation_may_precede_the_library_directive(self, tmp_path):
        """Real Dart: `@Deprecated(...)` is legal above `library`."""
        parent = tmp_path / "b.dart"
        parent.write_text("@Deprecated('old')\nlibrary legacy;\npart 'state.dart';\n")
        qualifier = DartLibraryQualifier(tmp_path, [])
        assert qualifier._read_directives(parent) == (["state.dart"], None, "legacy")

    def test_a_bare_library_directive_records_no_name(self, tmp_path):
        parent = tmp_path / "b.dart"
        parent.write_text("library;\npart 'state.dart';\n")
        qualifier = DartLibraryQualifier(tmp_path, [])
        assert qualifier._read_directives(parent) == (["state.dart"], None, None)

    def test_an_escaped_quote_inside_a_uri_resolves_to_the_file_dart_names(self, tmp_path):
        """The URI is compared against a path on disk, so it is the RESOLVED text that
        has to match: ``od\\'d.dart`` with its backslash still in it names a file that
        cannot exist.
        """
        parent = tmp_path / "b.dart"
        parent.write_text("part 'od\\'d.dart';\npart 'state.dart';\n")
        parts, _, _ = DartLibraryQualifier(tmp_path, [])._read_directives(parent)
        assert parts == ["od'd.dart", "state.dart"]

    def test_a_backslash_escapes_the_character_after_it(self, tmp_path):
        """The one escape rule a file path can need: a quote or a backslash in a name.

        Dart's control and code-point escapes are not resolved, because no file is
        named by one. ``\\t`` therefore reads as ``t``, not as a tab.
        """
        read_uri = DartLibraryQualifier(tmp_path, [])._read_uri

        assert read_uri("'a\\\\b.dart'") == "a\\b.dart"
        assert read_uri("'a\\$b.dart'") == "a$b.dart"
        assert read_uri('"od\\"d.dart"') == 'od"d.dart'
        assert read_uri("'od\\'d.dart'") == "od'd.dart"
        # Not resolved: a path cannot contain a tab or a code point escape.
        assert read_uri("'a\\tb.dart'") == "atb.dart"
        # A raw literal keeps the backslash, and the quote after it closes the
        # literal, because Dart cannot express a quote inside a raw string.
        assert read_uri("r'od\\'") == "od\\"
        # Unreadable rather than guessed at: a trailing backslash terminates
        # nothing, so the literal is abandoned.
        assert read_uri("'a\\") is None

    def test_a_stray_semicolon_is_not_a_statement(self, tmp_path):
        parent = tmp_path / "b.dart"
        parent.write_text("part 'state.dart';;\n")
        parts, _, _ = DartLibraryQualifier(tmp_path, [])._read_directives(parent)
        assert parts == ["state.dart"]

    def test_a_duplicate_entry_in_the_analysed_set_is_read_once(self, tmp_path):
        parent = tmp_path / "b.dart"
        parent.write_text("part 'missing.dart';\n")
        qualifier = DartLibraryQualifier(tmp_path, [parent, parent])
        assert [r.part_path for r in qualifier.unresolved_records] == ["missing.dart"]

    def test_a_record_path_outside_the_source_directory_keeps_its_full_path(self, tmp_path):
        outside = (tmp_path.parent / "outside.dart").resolve()
        assert DartLibraryQualifier(tmp_path, [])._relative(outside) == str(outside)
