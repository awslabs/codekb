"""Tests for ComponentCatalog.search_components wildcard matching (E-07 / FIX-10).

Two things are being pinned down here:

1. The pathological patterns that used to trigger catastrophic backtracking now
   complete immediately. The old implementation translated the pattern to a
   regex by replacing each '*' with '.*', so '**********a' became
   '.*.*.*.*.*.*.*.*.*.*a' and took tens of seconds against a short
   non-matching name.

2. Match results are unchanged for ordinary patterns. The replacement is a
   linear segment scan rather than a regex, so the differential test below
   compares it against the exact regex translation it replaced.
"""

import re
import time
from pathlib import Path

import pytest

from src.catalog.component_catalog import MAX_SEARCH_PATTERN_LENGTH, ComponentCatalog
from src.core.models import Component, ComponentType


def _make_component(name: str) -> Component:
    return Component(
        id=name,
        type=ComponentType.FUNCTION,
        file_path=Path("synthetic.py"),
        line_number=1,
        end_line_number=2,
        language="python",
    )


def _catalog(*names: str) -> ComponentCatalog:
    catalog = ComponentCatalog()
    for name in names:
        catalog.add_component(_make_component(name))
    return catalog


def _legacy_match(pattern: str, name: str) -> bool:
    """The pre-fix matcher, kept as the reference for the differential test."""
    regex_pattern = re.escape(pattern).replace(r"\*", ".*")
    try:
        return re.compile(regex_pattern, re.IGNORECASE).search(name) is not None
    except re.error:
        return False


def _matches(pattern: str, name: str) -> bool:
    segments = ComponentCatalog._pattern_segments(pattern)
    return ComponentCatalog._matches_segments(segments, name)


# Patterns kept deliberately free of long wildcard runs so the legacy reference
# implementation stays fast enough to run.
DIFFERENTIAL_PATTERNS = [
    "",
    "*",
    "**",
    "User",
    "user",
    "USER",
    "User*",
    "*Service",
    "*User*",
    "User**Service",
    "**User",
    "User**",
    "a*b",
    "a*a*a",
    "get_*_by_id",
    "Service*Impl",
    "auth",
    "x.y",
    "a+b",
    "C++Class",
    "ns::Class",
    "a b",
    r"\*",
    "[abc]",
    "(group)",
    "a?b",
    "^anchor",
    "dollar$",
]

DIFFERENTIAL_NAMES = [
    "",
    "User",
    "user",
    "USER",
    "UserService",
    "MyUserService",
    "ServiceUser",
    "Service",
    "authenticate",
    "AUTHENTICATE",
    "get_user_by_id",
    "get__by_id",
    "ServiceAImpl",
    "ServiceImpl",
    "aaa",
    "aba",
    "ab",
    "x.y",
    "xzy",
    "a+b",
    "aXb",
    "C++Class",
    "ns::Class",
    "a b",
    "ab",
    r"has\backslash",
    r"a\*b",
    "[abc]",
    "(group)",
    "^anchor",
    "dollar$",
]


class TestSemanticsUnchanged:
    @pytest.mark.parametrize("pattern", DIFFERENTIAL_PATTERNS)
    def test_matches_legacy_regex_translation(self, pattern):
        """Every (pattern, name) pair must agree with the old implementation."""
        for name in DIFFERENTIAL_NAMES:
            assert _matches(pattern, name) == _legacy_match(pattern, name), (
                f"divergence for pattern={pattern!r} name={name!r}"
            )

    def test_match_is_unanchored(self):
        catalog = _catalog("MyUserService", "Unrelated")
        assert [c.name for c in catalog.search_components("User")] == ["MyUserService"]

    def test_wildcard_spans_arbitrary_text(self):
        catalog = _catalog("get_user_by_id", "get_id")
        assert [c.name for c in catalog.search_components("get_*_by_id")] == ["get_user_by_id"]

    def test_match_is_case_insensitive(self):
        catalog = _catalog("UserService")
        assert len(catalog.search_components("userservice")) == 1
        assert len(catalog.search_components("USER*")) == 1

    def test_segments_must_appear_in_order(self):
        catalog = _catalog("ServiceUser")
        assert catalog.search_components("User*Service") == []
        assert len(catalog.search_components("Service*User")) == 1

    def test_consecutive_wildcards_behave_as_one(self):
        catalog = _catalog("UserService", "Unrelated")
        single = [c.name for c in catalog.search_components("*Service")]
        collapsed = [c.name for c in catalog.search_components("**********Service")]
        assert single == collapsed == ["UserService"]

    def test_exact_match_ranks_first(self):
        catalog = _catalog("UserServiceFactory", "User", "UserService")
        assert [c.name for c in catalog.search_components("User")] == [
            "User",
            "UserService",
            "UserServiceFactory",
        ]

    def test_no_match_returns_empty(self):
        catalog = _catalog("UserService")
        assert catalog.search_components("Order*") == []


class TestRedosVectorsAreBounded:
    """Each of these hung for tens of seconds under the regex translation."""

    @pytest.mark.parametrize(
        "pattern",
        [
            "*" * 10 + "a",  # the reported vector
            "*" * 20 + "a",
            "*" * 40 + "a",
            "a" + "*" * 20 + "b",
            "*a" * 20,
        ],
    )
    def test_pathological_pattern_completes_promptly(self, pattern):
        # A 30-char name with no 'a' or 'b' forces the full failing scan.
        catalog = _catalog("z" * 30, "z" * 60, "UserService")

        start = time.perf_counter()
        catalog.search_components(pattern)
        elapsed = time.perf_counter() - start

        assert elapsed < 1.0, f"pattern={pattern!r} took {elapsed:.2f}s"


class TestPatternLengthCap:
    def test_pattern_at_the_limit_is_accepted(self):
        catalog = _catalog("UserService")
        pattern = "User" + "*" * (MAX_SEARCH_PATTERN_LENGTH - 4)
        assert len(pattern) == MAX_SEARCH_PATTERN_LENGTH
        assert len(catalog.search_components(pattern)) == 1

    def test_over_long_pattern_returns_empty_without_raising(self):
        catalog = _catalog("UserService")
        pattern = "a" * (MAX_SEARCH_PATTERN_LENGTH + 1)
        assert catalog.search_components(pattern) == []

    def test_over_long_pattern_is_rejected_not_truncated(self):
        """A rejected pattern must not fall back to matching a shorter prefix."""
        catalog = _catalog("UserService")
        pattern = "User" + "x" * MAX_SEARCH_PATTERN_LENGTH
        assert catalog.search_components(pattern) == []
