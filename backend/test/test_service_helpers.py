"""Unit tests for the id/slug helpers in codekb.service.codekb_service.

All three helpers append an 8-char random suffix (see _slugify_to_space_id),
so assertions below check the stable prefix plus the suffix shape rather than
the full string.
"""

import re

from src.service.codekb_service import (
    _slugify_to_space_id,
    generate_space_id,
    generate_space_id_from_name,
)

_SUFFIX_RE = re.compile(r"-[0-9a-f]{8}$")


class TestSlugifyToSpaceId:
    def test_lowercases_and_slugifies_whitespace(self):
        result = _slugify_to_space_id("My Space Name")
        assert result.startswith("my-space-name-")
        assert _SUFFIX_RE.search(result)

    def test_strips_unsupported_characters(self):
        result = _slugify_to_space_id("foo/bar.baz@qux")
        assert result.startswith("foo-bar-baz-qux-")
        assert _SUFFIX_RE.search(result)

    def test_collapses_multiple_separators(self):
        result = _slugify_to_space_id("foo   ---   bar")
        assert result.startswith("foo-bar-")
        assert _SUFFIX_RE.search(result)

    def test_adds_random_suffix(self):
        # Two calls with the same input should disagree only in the suffix,
        # proving the suffix is the only source of nondeterminism.
        a = _slugify_to_space_id("same input")
        b = _slugify_to_space_id("same input")
        # Strip suffix to compare prefixes.
        assert _SUFFIX_RE.sub("", a) == _SUFFIX_RE.sub("", b) == "same-input"
        # Suffix is 8 hex chars.
        assert _SUFFIX_RE.search(a) and _SUFFIX_RE.search(b)

    def test_truncates_long_inputs(self):
        # Slug is capped at 54 chars before the dash+suffix, so total <= 63.
        very_long = "x" * 200
        result = _slugify_to_space_id(very_long)
        assert len(result) <= 63


class TestGenerateSpaceIdFromName:
    def test_uses_slugify(self):
        result = generate_space_id_from_name("Finance Platform")
        assert result.startswith("finance-platform-")
        assert _SUFFIX_RE.search(result)

    def test_non_ascii_is_stripped(self):
        # Non-ASCII characters are replaced, leaving the ASCII letters intact.
        result = generate_space_id_from_name("Café Menu")
        assert " " not in result
        assert result == result.lower()
        assert _SUFFIX_RE.search(result)


class TestGenerateSpaceIdFromGitUrl:
    def test_https_url_extracts_repo_name(self):
        result = generate_space_id("https://github.com/acme/widgets.git")
        assert result.startswith("widgets-")
        assert _SUFFIX_RE.search(result)

    def test_ssh_url_extracts_repo_name(self):
        result = generate_space_id("git@github.com:acme/My_Cool_App.git")
        assert result.startswith("my-cool-app-")
        assert _SUFFIX_RE.search(result)

    def test_suffix_makes_output_unique(self):
        # By design each call produces a fresh suffix, so two calls with the
        # same URL should differ. (If this ever becomes flaky, consider it a
        # signal that the design changed.)
        url = "https://github.com/acme/widgets.git"
        a = generate_space_id(url)
        b = generate_space_id(url)
        assert _SUFFIX_RE.sub("", a) == _SUFFIX_RE.sub("", b) == "widgets"
