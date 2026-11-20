"""authz.ladder (git role → gate) and authz.repo_key (URL normalizer).

**Guard (plan §6.1.1): keep the repo-key normalizer and these tests as is** —
the gate depends on both sides producing the identical key.
"""

import pytest

from src.authz import ladder
from src.authz.repo_key import parse_repo, repo_key


# ── gate sets ────────────────────────────────────────────────────────────────


def test_read_set_is_everything_but_none():
    for level in (ladder.READ, ladder.WRITE, ladder.MAINTAIN, ladder.ADMIN):
        assert ladder.can_read(level)
    assert not ladder.can_read(ladder.NONE)
    assert not ladder.can_read(None)


def test_write_set_is_maintainer_tier_only():
    assert ladder.can_write(ladder.MAINTAIN)
    assert ladder.can_write(ladder.ADMIN)
    for level in (ladder.READ, ladder.WRITE, ladder.NONE, None):
        assert not ladder.can_write(level)


# ── GitHub ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "perms,expected",
    [
        ({"admin": True, "maintain": True, "push": True, "pull": True}, ladder.ADMIN),
        ({"maintain": True, "push": True, "pull": True}, ladder.MAINTAIN),
        ({"push": True, "pull": True}, ladder.WRITE),
        ({"triage": True, "pull": True}, ladder.READ),
        ({"pull": True}, ladder.READ),
        ({"pull": False}, ladder.NONE),
        ({}, ladder.NONE),
        (None, ladder.NONE),
    ],
)
def test_github_level(perms, expected):
    assert ladder.github_level(perms) == expected


# ── GitLab: one test per level, including 15 and 25 (plan §6.1.3) ─────────────


@pytest.mark.parametrize(
    "access_level,expected",
    [
        (0, ladder.NONE),
        (5, ladder.NONE),  # Minimal
        (10, ladder.NONE),  # Guest
        (15, ladder.READ),  # Planner
        (20, ladder.READ),  # Reporter
        (25, ladder.READ),  # Security Manager
        (30, ladder.WRITE),  # Developer
        (40, ladder.MAINTAIN),  # Maintainer
        (50, ladder.MAINTAIN),  # Owner
        (60, ladder.MAINTAIN),  # Admin
    ],
)
def test_gitlab_level_known(access_level, expected):
    assert ladder.gitlab_level(access_level) == expected


def test_gitlab_level_unknown_is_none_not_an_error():
    assert ladder.gitlab_level(99) == ladder.NONE
    assert ladder.gitlab_level(None) == ladder.NONE
    assert ladder.gitlab_level("not-a-number") == ladder.NONE


# ── Bitbucket ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "perm,expected",
    [
        ("admin", ladder.ADMIN),
        ("write", ladder.WRITE),
        ("read", ladder.READ),
        ("none", ladder.NONE),
        ("", ladder.NONE),
        (None, ladder.NONE),
    ],
)
def test_bitbucket_level(perm, expected):
    assert ladder.bitbucket_level(perm) == expected


# ── repo_key normalizer ──────────────────────────────────────────────────────


def test_repo_key_https_lowercases_host_and_strips_git_suffix():
    assert repo_key("https://GitHub.com/Org/Repo.git") == "github.com/org/repo"


def test_repo_key_ignores_userinfo_and_port_and_trailing_slash():
    assert repo_key("https://user:tok@github.com:443/org/repo/") == "github.com/org/repo"


def test_repo_key_scp_ssh_syntax():
    assert repo_key("git@github.com:org/repo.git") == "github.com/org/repo"


def test_repo_key_ssh_scheme():
    assert repo_key("ssh://git@gitlab.com/group/sub/project.git") == "gitlab.com/group/sub/project"


def test_repo_key_none_for_blank_or_bad_scheme():
    assert repo_key("") is None
    assert repo_key(None) is None
    assert repo_key("ftp://host/x") is None
    assert repo_key("not a url") is None


def test_parse_repo_returns_host_and_path():
    assert parse_repo("https://github.com/org/repo.git") == ("github.com", "org/repo")
