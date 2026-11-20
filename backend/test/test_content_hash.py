"""Tests for per-component source hashing used by method-level delta refresh."""

from pathlib import Path

from src.analysis.content_hash import (
    backfill_component_hashes,
    compute_component_hash,
)
from src.core.models import Component, ComponentType


def _component(file_path: Path, start: int, end: int, cid: str = "space::m") -> Component:
    return Component(
        id=cid,
        type=ComponentType.METHOD,
        file_path=file_path,
        line_number=start,
        end_line_number=end,
        language="java",
        space_id="space",
    )


def _write(tmp_path: Path, name: str, lines: list[str]) -> Path:
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_hash_is_content_sensitive(tmp_path: Path):
    lines = ["class A {", "  int f() { return 1; }", "}"]
    f = _write(tmp_path, "A.java", lines)
    comp = _component(f, 2, 2)
    h1 = compute_component_hash(tmp_path, comp)

    # Change the method body -> hash must change
    _write(tmp_path, "A.java", ["class A {", "  int f() { return 2; }", "}"])
    h2 = compute_component_hash(tmp_path, comp)

    assert h1 and h2 and h1 != h2


def test_hash_is_position_independent(tmp_path: Path):
    method = ["  int f() {", "    return 1;", "  }"]

    # Same method text, different line offsets in two files.
    fa = _write(tmp_path, "A.java", ["class A {", *method, "}"])
    fb = _write(
        tmp_path,
        "B.java",
        ["// leading comment", "", "class B {", "  int x;", *method, "}"],
    )

    comp_a = _component(fa, 2, 4, "space::a")  # method at lines 2-4
    comp_b = _component(fb, 5, 7, "space::b")  # same method text at lines 5-7

    assert compute_component_hash(tmp_path, comp_a) == compute_component_hash(tmp_path, comp_b)


def test_backfill_sets_hashes_and_counts(tmp_path: Path):
    f = _write(tmp_path, "A.java", ["class A {", "  int f() { return 1; }", "}"])
    comps = [_component(f, 1, 3, "space::a"), _component(f, 2, 2, "space::b")]

    count = backfill_component_hashes(tmp_path, comps)

    assert count == 2
    assert all(c.content_hash for c in comps)


def test_backfill_no_overwrite_preserves_existing(tmp_path: Path):
    f = _write(tmp_path, "A.java", ["class A {", "  int f() { return 1; }", "}"])
    comp = _component(f, 2, 2)
    comp.content_hash = "sentinel"

    count = backfill_component_hashes(tmp_path, [comp], overwrite=False)

    assert count == 0
    assert comp.content_hash == "sentinel"


def test_backfill_missing_file_leaves_hash_none(tmp_path: Path):
    comp = _component(tmp_path / "does_not_exist.java", 1, 3)

    count = backfill_component_hashes(tmp_path, [comp])

    assert count == 0
    assert comp.content_hash is None


def test_log_line_edit_changes_only_that_method(tmp_path: Path):
    """Mirror the real scenario: adding a log line to save() changes only save()'s
    hash; the sibling count() shifts down a line but its hash is unchanged."""
    v1 = [
        "package demo;",
        "class R {",
        "    public User save(User u) {",
        "        users.add(u);",
        "        return u;",
        "    }",
        "    public int count() {",
        "        return users.size();",
        "    }",
        "}",
    ]
    f = _write(tmp_path, "R.java", v1)
    save_v1 = _component(f, 3, 6, "s::save")
    count_v1 = _component(f, 7, 9, "s::count")
    backfill_component_hashes(tmp_path, [save_v1, count_v1])

    # Add a log line inside save(); count() shifts down by one line.
    v2 = [
        "package demo;",
        "class R {",
        "    public User save(User u) {",
        '        System.out.println("Save user");',
        "        users.add(u);",
        "        return u;",
        "    }",
        "    public int count() {",
        "        return users.size();",
        "    }",
        "}",
    ]
    _write(tmp_path, "R.java", v2)
    save_v2 = _component(f, 3, 7, "s::save")  # now spans one more line
    count_v2 = _component(f, 8, 10, "s::count")  # shifted down, same text
    backfill_component_hashes(tmp_path, [save_v2, count_v2])

    assert save_v1.content_hash != save_v2.content_hash  # edited method changed
    assert count_v1.content_hash == count_v2.content_hash  # shifted sibling unchanged


def test_component_roundtrip_preserves_hash():
    comp = Component(
        id="space::m",
        type=ComponentType.METHOD,
        file_path=Path("src/A.java"),
        line_number=1,
        end_line_number=3,
        language="java",
        space_id="space",
        content_hash="deadbeef",
    )
    restored = Component.from_dict(comp.to_dict())
    assert restored.content_hash == "deadbeef"


def test_component_from_dict_defaults_hash_none_for_legacy():
    # A legacy serialized component (no content_hash key) -> None, no crash.
    legacy = {
        "id": "space::m",
        "type": "method",
        "file_path": "src/A.java",
        "line_number": 1,
        "end_line_number": 3,
        "language": "java",
        "space_id": "space",
    }
    restored = Component.from_dict(legacy)
    assert restored.content_hash is None
