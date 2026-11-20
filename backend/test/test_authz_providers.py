"""Per-user permission listing on the gitcreds providers (plan §6.1.3).

Only a 401 is TokenInvalid on this path; a 403/429/5xx/network error is a
ProviderError (non-answer). The key is ``host/full_name`` lowercased, matching
authz.repo_key.
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from src.gitcreds.providers import (
    ProviderError,
    ProviderListingUnsupported,
    TokenInvalid,
    list_accessible_repos,
)


class _Resp:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


@contextmanager
def _config(**overrides):
    base = {"gitcreds.http_timeout": 10.0}
    base.update(overrides)
    with patch(
        "src.gitcreds.providers.get_config",
        side_effect=lambda key, default=None: base.get(key, default),
    ):
        yield


# ── GitHub ───────────────────────────────────────────────────────────────────


def test_github_lists_and_builds_lowercased_keys():
    page = [
        {"full_name": "Org/Repo", "permissions": {"push": True, "pull": True}},
        {"full_name": "org/other", "permissions": {"pull": True}},
    ]
    with patch("httpx.get", return_value=_Resp(200, page)):
        repos = list_accessible_repos("github", "tok")
    assert repos["github.com/org/repo"] == {"push": True, "pull": True}
    assert repos["github.com/org/other"] == {"pull": True}


def test_github_paginates_until_short_page():
    full = [{"full_name": f"org/r{i}", "permissions": {"pull": True}} for i in range(100)]
    short = [{"full_name": "org/last", "permissions": {"pull": True}}]
    with patch("httpx.get", side_effect=[_Resp(200, full), _Resp(200, short)]) as g:
        repos = list_accessible_repos("github", "tok")
    assert g.call_count == 2
    assert "github.com/org/last" in repos
    assert len(repos) == 101


def test_github_401_is_token_invalid():
    with patch("httpx.get", return_value=_Resp(401)):
        with pytest.raises(TokenInvalid):
            list_accessible_repos("github", "bad")


def test_github_rate_limited_403_is_provider_error_not_token_invalid():
    resp = _Resp(403, headers={"x-ratelimit-remaining": "0"})
    with patch("httpx.get", return_value=resp):
        with pytest.raises(ProviderError) as ei:
            list_accessible_repos("github", "tok")
        assert not isinstance(ei.value, TokenInvalid)


def test_github_non_ratelimit_403_is_token_invalid():
    # SSO/SAML not authorized, or a token missing a scope: a permanent 403 → reconnect,
    # not a stuck 503.
    with patch("httpx.get", return_value=_Resp(403)):
        with pytest.raises(TokenInvalid):
            list_accessible_repos("github", "tok")


def test_github_5xx_is_provider_error():
    with patch("httpx.get", return_value=_Resp(503)):
        with pytest.raises(ProviderError):
            list_accessible_repos("github", "tok")


# ── GitLab ───────────────────────────────────────────────────────────────────


def test_gitlab_sends_min_access_level_15_and_maps_level():
    page = [
        {
            "path_with_namespace": "Group/Proj",
            "permissions": {"project_access": {"access_level": 30}},
        },
        {"path_with_namespace": "group/grp", "permissions": {"group_access": {"access_level": 25}}},
    ]
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        return _Resp(200, page)

    with patch("httpx.get", side_effect=fake_get):
        repos = list_accessible_repos("gitlab", "tok")
    assert captured["params"]["min_access_level"] == 15
    assert repos["gitlab.com/group/proj"] == 30  # native access level
    assert repos["gitlab.com/group/grp"] == 25


def test_gitlab_takes_max_of_project_and_group_access():
    page = [
        {
            "path_with_namespace": "g/p",
            "permissions": {
                "project_access": {"access_level": 20},
                "group_access": {"access_level": 40},
            },
        }
    ]
    with patch("httpx.get", return_value=_Resp(200, page)):
        repos = list_accessible_repos("gitlab", "tok")
    assert repos["gitlab.com/g/p"] == 40


def test_gitlab_null_permissions_floor_to_min_access_level():
    # Inherited access (ancestor group / project sharing) returns null permission
    # fields; the membership filter guarantees ≥ 15, so it maps to the floor, not NONE.
    page = [{"path_with_namespace": "g/inherited", "permissions": {}}]
    with patch("httpx.get", return_value=_Resp(200, page)):
        repos = list_accessible_repos("gitlab", "tok")
    assert repos["gitlab.com/g/inherited"] == 15


# ── Bitbucket ──────────────────────────────────────────────────────────────


def test_bitbucket_cloud_lists_permissions():
    body = {
        "values": [
            {"repository": {"full_name": "WS/Repo"}, "permission": "write"},
            {"repository": {"full_name": "ws/other"}, "permission": "read"},
        ],
        "next": None,
    }
    with patch("httpx.get", return_value=_Resp(200, body)):
        repos = list_accessible_repos("bitbucket", "tok")
    assert repos["bitbucket.org/ws/repo"] == "write"
    assert repos["bitbucket.org/ws/other"] == "read"


def test_bitbucket_server_listing_is_unsupported():
    # Permanent → ProviderListingUnsupported (gate denies, not 503s).
    with _config(**{"gitcreds.bitbucket_custom_host": "https://bb.xyz.com"}):
        with pytest.raises(ProviderListingUnsupported):
            list_accessible_repos("bitbucket", "tok", host="bb.xyz.com")
