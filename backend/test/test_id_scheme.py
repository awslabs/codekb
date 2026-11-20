"""Tests for the component-id scheme: construction, read helpers, and the
per-space backward-compatibility contract (legacy V1 vs collision-free V2)."""

from pathlib import Path

import pytest

from src.core.models import (
    Component,
    ComponentType,
    IdScheme,
    DEFAULT_SCHEME,
    KnowledgeBase,
    resolve_id_scheme,
    qualify,
    split_space,
    simple_name,
    path_prefix,
)
from src.parsers.python_parser import PythonParser


def _parser(scheme: IdScheme, root: Path | None) -> PythonParser:
    p = PythonParser()
    p._scheme = scheme
    p._root = root
    return p


# ── Read-side helpers ──────────────────────────────────────────────────────

def test_qualify_is_idempotent():
    assert qualify("sp", "mod.fn") == "sp::mod.fn"
    assert qualify("sp", "sp::mod.fn") == "sp::mod.fn"


def test_split_space_round_trips():
    assert split_space("sp::mod.fn") == ("sp", "sp::mod.fn")


def test_split_space_rejects_unqualified():
    for bad in ("mod.fn", "::mod.fn"):
        try:
            split_space(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_simple_name_is_last_segment():
    assert simple_name("sp::a.b.c") == "c"
    assert simple_name("stem.fn") == "fn"


def test_resolve_id_scheme():
    assert resolve_id_scheme(None) is IdScheme.V1_STEM
    assert resolve_id_scheme("") is IdScheme.V1_STEM
    assert resolve_id_scheme("1.0") is IdScheme.V1_STEM
    assert resolve_id_scheme("v1") is IdScheme.V1_STEM
    assert resolve_id_scheme("unknown") is IdScheme.V1_STEM
    assert resolve_id_scheme("v2") is IdScheme.V2_PATH
    assert resolve_id_scheme("2.0") is IdScheme.V2_PATH


# ── make_id: V1 legacy is byte-identical to the old inline scheme ───────────

def test_v1_top_level_matches_legacy_stem():
    p = _parser(IdScheme.V1_STEM, Path("/repo"))
    assert p.make_id(Path("/repo/pkg/service.py"), "handle") == "service.handle"


def test_v1_parent_chaining_matches_legacy():
    p = _parser(IdScheme.V1_STEM, Path("/repo"))
    assert p.make_id(Path("/repo/a/service.py"), "m", "service.Payment") == "service.Payment.m"


def test_v1_collides_across_directories():
    """The known legacy defect, pinned so V1 behavior is not silently changed."""
    p = _parser(IdScheme.V1_STEM, Path("/repo"))
    a = p.make_id(Path("/repo/foo/service.py"), "handle")
    b = p.make_id(Path("/repo/bar/service.py"), "handle")
    assert a == b == "service.handle"


# ── make_id: V2 disambiguates by repo-relative path ─────────────────────────

def test_v2_disambiguates_same_stem_across_dirs():
    p = _parser(IdScheme.V2_PATH, Path("/repo"))
    a = p.make_id(Path("/repo/foo/service.py"), "handle")
    b = p.make_id(Path("/repo/bar/service.py"), "handle")
    assert a != b
    assert simple_name(a) == simple_name(b) == "handle"


def test_v2_is_deterministic_for_same_path():
    p1 = _parser(IdScheme.V2_PATH, Path("/repo"))
    p2 = _parser(IdScheme.V2_PATH, Path("/repo"))
    f = Path("/repo/foo/service.py")
    assert p1.make_id(f, "handle") == p2.make_id(f, "handle")


def test_v2_parent_chaining_has_no_file_prefix():
    p = _parser(IdScheme.V2_PATH, Path("/repo"))
    # Chaining onto an existing local id must not re-apply the path prefix.
    assert p.make_id(Path("/repo/a/s.py"), "m", "some.Parent") == "some.Parent.m"


def test_top_level_id_passthrough_is_verbatim():
    """Terraform-style addresses must be used verbatim under any scheme."""
    for scheme in (IdScheme.V1_STEM, IdScheme.V2_PATH):
        p = _parser(scheme, Path("/repo"))
        assert p.make_id(Path("/repo/main.tf"), "ignored", top_level_id="aws_s3_bucket.logs") == "aws_s3_bucket.logs"


def test_path_prefix_keeps_readable_stem():
    # V2 prefix stays partially human-readable: "<stem>_<hash>".
    prefix = path_prefix(Path("/repo/foo/service.py"), Path("/repo"))
    assert prefix.startswith("service_")


# ── Component.name: persisted-else-id fallback ──────────────────────────────

def _component(cid: str, name: str | None = None) -> Component:
    return Component(
        id=cid,
        type=ComponentType.FUNCTION,
        file_path=Path("f.py"),
        line_number=1,
        end_line_number=2,
        language="python",
        _name=name,
    )


def test_name_falls_back_to_id():
    assert _component("sp::stem.fn").name == "sp::stem.fn"


def test_name_uses_persisted_display_name():
    assert _component("sp::hash.fn", name="PaymentService.handle").name == "PaymentService.handle"


def test_component_dict_round_trip_without_name():
    c = _component("sp::stem.fn")
    back = Component.from_dict(c.to_dict())
    assert back.name == "sp::stem.fn"
    assert back._name is None  # equal-to-id name is not retained as distinct


def test_component_dict_round_trip_with_distinct_name():
    c = _component("sp::hash.fn", name="Payment.handle")
    back = Component.from_dict(c.to_dict())
    assert back._name == "Payment.handle"
    assert back.name == "Payment.handle"


def test_legacy_component_dict_without_name_key():
    data = {
        "id": "sp::stem.fn",
        "type": "function",
        "file_path": "f.py",
        "line_number": 1,
        "end_line_number": 2,
        "language": "python",
    }
    assert Component.from_dict(data).name == "sp::stem.fn"


# ── KnowledgeBase.id_scheme round-trips and defaults ────────────────────────

def test_kb_id_scheme_round_trips():
    kb = KnowledgeBase(source_directory=Path("/repo"), id_scheme=DEFAULT_SCHEME.value)
    back = KnowledgeBase.from_dict(kb.to_dict())
    assert back.id_scheme == DEFAULT_SCHEME.value


def test_kb_legacy_dict_has_empty_scheme_resolving_to_v1():
    legacy = {
        "source_directory": "/repo",
        "components": [],
        "call_graph": {},
        "analysis_metadata": {},
        "created_at": None,
        "version": "1.0",
        "space_id": "sp",
    }
    kb = KnowledgeBase.from_dict(legacy)
    assert kb.id_scheme == ""
    assert resolve_id_scheme(kb.id_scheme) is IdScheme.V1_STEM


# ── Dart: the scheme reaches the one parser the CR left on the stem ──────────
# DartParser builds ids off a file-prefix like every other parser, but routes
# through file_prefix rather than make_id, so it needs its own coverage that the
# scheme actually flows through. DartLibraryQualifier only disambiguates part
# files; two same-named *standalone* Dart files in different dirs collide under
# V1 and are what V2 must separate.

try:
    import tree_sitter_dart as _tsdart
    from tree_sitter import Language as _Language, Parser as _Parser

    _DART_AVAILABLE = True
except ImportError:  # pragma: no cover
    _DART_AVAILABLE = False

_DART_CLASS = "class Store {\n  void save() {}\n}\n"


def _dart_ids(scheme: IdScheme, file_path: Path, root: Path | None) -> list[str]:
    from src.parsers.dart_parser import DartParser

    parser = DartParser()
    parser._scheme = scheme
    parser._root = root
    source = _DART_CLASS.encode()
    tree = _Parser(_Language(_tsdart.language())).parse(source)
    return [c.id for c in parser.extract_components(tree.root_node, source, file_path)]


@pytest.mark.skipif(not _DART_AVAILABLE, reason="tree_sitter_dart not installed")
def test_dart_v1_collides_across_directories():
    a = _dart_ids(IdScheme.V1_STEM, Path("/repo/alpha/service.dart"), Path("/repo"))
    b = _dart_ids(IdScheme.V1_STEM, Path("/repo/beta/service.dart"), Path("/repo"))
    assert a == b  # the legacy collision, pinned


@pytest.mark.skipif(not _DART_AVAILABLE, reason="tree_sitter_dart not installed")
def test_dart_v2_disambiguates_across_directories():
    a = _dart_ids(IdScheme.V2_PATH, Path("/repo/alpha/service.dart"), Path("/repo"))
    b = _dart_ids(IdScheme.V2_PATH, Path("/repo/beta/service.dart"), Path("/repo"))
    assert set(a).isdisjoint(b)  # distinct ids, no silent drop
    assert [simple_name(i) for i in a] == [simple_name(i) for i in b]
