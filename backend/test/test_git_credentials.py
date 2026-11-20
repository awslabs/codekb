"""Tests for per-user git-provider connections: adapters, the encrypted store,
and the connections API.

KMS is not covered by the suite's boto3 guard, so a small in-memory fake stands
in for it. The fake faithfully enforces the KMS encryption-context contract:
``decrypt`` refuses when the context does not byte-match what ``generate_data_key``
sealed — which is exactly the cross-user protection the store relies on.
"""

import asyncio
import base64
import json
import os
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from src.api.auth import UserIdentity
from src.api.routes import git_credentials as gc_routes
from src.gitcreds.providers import (
    BitbucketProvider,
    GitHubProvider,
    GitLabProvider,
    ProviderError,
    TokenInvalid,
    VisibilityError,
    _parse_self_hosted,
    enabled_provider_ids,
    get_provider,
    get_visibility,
    provider_by_id,
    provider_for_host,
    saved_token_matches_repo,
    validate_provider_config,
)
from src.gitcreds.resolver import resolve_repo_credential
from src.gitcreds.store import GitCredentialStore
from src.core.logging_utils import ConfigurationError, StorageError


# ── a faithful in-memory KMS fake ───────────────────────────────────────────


class FakeKMS:
    """Minimal KMS that honours the encryption-context binding.

    ``generate_data_key`` returns a random 32-byte plaintext key and a
    ``CiphertextBlob`` that embeds the key plus the sealing context.
    ``decrypt`` returns the key only when the caller's context matches the
    sealed one (and the KeyId matches), mirroring real KMS.
    """

    def __init__(self, key_arn: str = "arn:aws:kms:us-east-1:111122223333:key/abc"):
        self.key_arn = key_arn

    def generate_data_key(self, KeyId, KeySpec, EncryptionContext):
        assert KeyId == self.key_arn
        assert KeySpec == "AES_256"
        plaintext = os.urandom(32)
        blob = base64.b64encode(
            json.dumps(
                {"k": base64.b64encode(plaintext).decode(), "ctx": EncryptionContext}
            ).encode()
        )
        return {"Plaintext": plaintext, "CiphertextBlob": blob}

    def decrypt(self, CiphertextBlob, EncryptionContext, KeyId):
        from botocore.exceptions import ClientError

        if KeyId != self.key_arn:
            raise ClientError({"Error": {"Code": "IncorrectKeyException"}}, "Decrypt")
        payload = json.loads(base64.b64decode(CiphertextBlob).decode())
        if payload["ctx"] != EncryptionContext:
            # This is what stops user B from opening user A's blob.
            raise ClientError(
                {"Error": {"Code": "InvalidCiphertextException"}}, "Decrypt"
            )
        return {"Plaintext": base64.b64decode(payload["k"])}


@pytest.fixture
def store(mock_s3):
    """A GitCredentialStore backed by moto S3 and the fake KMS, with the KMS ARN
    configured so seal/unseal run."""
    kms = FakeKMS()
    with patch(
        "src.gitcreds.store.get_config",
        side_effect=lambda key, default=None: {
            "storage.s3.region": "us-east-1",
            "gitcreds.kms_key_arn": kms.key_arn,
        }.get(key, default),
    ):
        yield GitCredentialStore(kms_client=kms)


# ── providers ───────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


def test_github_git_auth_header_is_basic_x_access_token():
    header = GitHubProvider().git_auth_header("ghp_secret")
    assert header.startswith("Authorization: Basic ")
    decoded = base64.b64decode(header.split(" ", 2)[2]).decode()
    assert decoded == "x-access-token:ghp_secret"


def test_gitlab_and_bitbucket_headers_use_their_pseudo_users():
    gl = base64.b64decode(GitLabProvider().git_auth_header("t").split(" ", 2)[2]).decode()
    bb = base64.b64decode(BitbucketProvider().git_auth_header("t").split(" ", 2)[2]).decode()
    assert gl == "oauth2:t"
    assert bb == "x-token-auth:t"


def test_github_match_only_github_com():
    p = GitHubProvider()
    assert p.match("github.com")
    assert not p.match("gitlab.com")


def test_github_validate_token_returns_username():
    with patch("httpx.get", return_value=_Resp(200, {"login": "octocat"})):
        assert GitHubProvider().validate_token("tok") == {"username": "octocat"}


def test_github_validate_token_401_raises_token_invalid():
    with patch("httpx.get", return_value=_Resp(401)):
        with pytest.raises(TokenInvalid):
            GitHubProvider().validate_token("bad")


def test_github_validate_token_5xx_raises_provider_error():
    with patch("httpx.get", return_value=_Resp(503)):
        with pytest.raises(ProviderError):
            GitHubProvider().validate_token("tok")


def test_github_validate_token_rate_limited_403_is_not_token_invalid():
    # A 403 that is a rate limit must not be reported as a bad token.
    resp = _Resp(403, headers={"x-ratelimit-remaining": "0"})
    with patch("httpx.get", return_value=resp):
        with pytest.raises(ProviderError) as ei:
            GitHubProvider().validate_token("tok")
        assert not isinstance(ei.value, TokenInvalid)


def test_github_validate_token_custom_host_hits_v3():
    with _with_config(**{"gitcreds.github_custom_host": "https://github.xyz.com:8443"}):
        with patch("httpx.get", return_value=_Resp(200, {"login": "ent-user"})) as http:
            assert GitHubProvider().validate_token("tok", host="github.xyz.com") == {
                "username": "ent-user"
            }
        called = http.call_args.args[0] if http.call_args.args else http.call_args.kwargs["url"]
        assert called == "https://github.xyz.com:8443/api/v3/user"


def test_gitlab_validate_token_custom_host_hits_v4():
    with _with_config(**{"gitcreds.gitlab_custom_host": "https://gitlab.xyz.com"}):
        with patch("httpx.get", return_value=_Resp(200, {"username": "gl-user"})) as http:
            assert GitLabProvider().validate_token("tok", host="gitlab.xyz.com") == {
                "username": "gl-user"
            }
        called = http.call_args.args[0] if http.call_args.args else http.call_args.kwargs["url"]
        assert called == "https://gitlab.xyz.com/api/v4/user"


def test_bitbucket_validate_token_server_uses_inbox_endpoint():
    # Server/DC validates via the auth-only inbox count; X-AUSERNAME fills the
    # username when the instance provides it.
    with _with_config(**{"gitcreds.bitbucket_custom_host": "https://bb.xyz.com"}):
        resp = _Resp(200, {"count": 0}, headers={"X-AUSERNAME": "bb-user"})
        with patch("httpx.get", return_value=resp) as http:
            assert BitbucketProvider().validate_token("tok", host="bb.xyz.com") == {
                "username": "bb-user"
            }
        called = http.call_args.args[0] if http.call_args.args else http.call_args.kwargs["url"]
        assert called == "https://bb.xyz.com/rest/api/1.0/inbox/pull-requests/count"


def test_bitbucket_validate_token_server_401_is_invalid():
    with _with_config(**{"gitcreds.bitbucket_custom_host": "https://bb.xyz.com"}):
        with patch("httpx.get", return_value=_Resp(401)):
            with pytest.raises(TokenInvalid):
                BitbucketProvider().validate_token("bad", host="bb.xyz.com")


def test_bitbucket_validate_token_server_blank_username_when_no_header():
    # No official username source; a missing header yields "" for the route to fill.
    with _with_config(**{"gitcreds.bitbucket_custom_host": "https://bb.xyz.com"}):
        with patch("httpx.get", return_value=_Resp(200, {"count": 3})):
            assert BitbucketProvider().validate_token("tok", host="bb.xyz.com") == {
                "username": ""
            }


def test_registry_default_enables_only_github():
    # default.yaml ships gitcreds.providers: "github"
    assert enabled_provider_ids() == ["github"]
    assert provider_by_id("github").id == "github"
    assert provider_for_host("github.com").id == "github"


def test_registry_resolves_known_provider_even_if_disabled():
    # Resolves any implemented provider; the enabled-only gate lives in the save route.
    assert provider_by_id("gitlab").id == "gitlab"


def test_registry_rejects_unknown_provider():
    with pytest.raises(ProviderError):
        provider_by_id("nope")


def test_registry_unknown_host_returns_none():
    assert provider_for_host("example.com") is None


# ── provider config: explicit combination, or NONE ──────────────────────────


def _with_providers(value):
    """Patch the resolved ``gitcreds.providers`` config value."""
    return patch(
        "src.gitcreds.providers.get_config",
        side_effect=lambda key, default=None: (
            value if key == "gitcreds.providers" else default
        ),
    )


def test_providers_accepts_a_combination_in_config_order():
    with _with_providers("bitbucket, github"):
        assert enabled_provider_ids() == ["bitbucket", "github"]


def test_providers_collapses_duplicates():
    with _with_providers("github github,github"):
        assert enabled_provider_ids() == ["github"]


def test_providers_none_means_public_repositories_only():
    with _with_providers("NONE"):
        assert enabled_provider_ids() == []
        # The clone path then finds no provider and runs credential-free.
        assert provider_for_host("github.com") is None


def test_providers_empty_value_is_rejected():
    for value in ("", "   ", ","):
        with _with_providers(value):
            with pytest.raises(ConfigurationError, match="is empty"):
                enabled_provider_ids()


def test_providers_unknown_id_is_rejected():
    with _with_providers("github,gitlabs"):
        with pytest.raises(ConfigurationError, match="gitlabs"):
            enabled_provider_ids()


def test_providers_none_cannot_be_combined():
    with _with_providers("none,github"):
        with pytest.raises(ConfigurationError, match="combines NONE"):
            enabled_provider_ids()


def test_validate_provider_config_raises_on_bad_value():
    with _with_providers("nope"):
        with pytest.raises(ConfigurationError):
            validate_provider_config()


def test_validate_provider_config_passes_for_none():
    with _with_providers("none"):
        validate_provider_config()


# ── store round-trip + context binding ──────────────────────────────────────


def test_store_put_get_roundtrip(store):
    store.put("user-a", "github", username="octocat", token="ghp_secret")  # nosec B106
    assert store.has("user-a", "github")
    assert store.get_username("user-a", "github") == "octocat"
    assert store.get_token("user-a", "github") == "ghp_secret"


def test_store_object_contains_no_plaintext_token(store, mock_s3):
    store.put("user-a", "github", username="octocat", token="ghp_TOPSECRET")  # nosec B106
    raw = mock_s3.get_object(Bucket="codekb-knowledgebase", Key="users/user-a/gitcreds.json")[
        "Body"
    ].read().decode()
    assert "ghp_TOPSECRET" not in raw
    assert "octocat" in raw  # username is non-secret and kept in clear


def test_store_list_providers_never_returns_token(store):
    store.put("user-a", "github", username="octocat", token="ghp_secret")  # nosec B106
    listed = store.list_providers("user-a")
    assert listed == [{"provider": "github", "username": "octocat", "status": "connected"}]
    assert "token" not in json.dumps(listed)


def test_store_cross_user_decrypt_is_refused(store):
    """A blob sealed for user-a cannot be unsealed as user-b (KMS context mismatch)."""
    store.put("user-a", "github", username="octocat", token="ghp_secret")  # nosec B106
    obj = store._read("user-a")
    # Simulate user-b reaching user-a's entry: unseal rebuilds ctx from 'user-b'.
    with pytest.raises(StorageError):
        store._unseal(obj["github"], "user-b", "github")


def test_store_delete_removes_entry(store):
    store.put("user-a", "github", username="octocat", token="ghp_secret")  # nosec B106
    assert store.delete("user-a", "github") is True
    assert not store.has("user-a", "github")
    assert store.delete("user-a", "github") is False  # already gone


def test_store_missing_kms_arn_fails_loud(mock_s3):
    with patch(
        "src.gitcreds.store.get_config",
        side_effect=lambda key, default=None: default,  # nothing configured
    ):
        s = GitCredentialStore(kms_client=FakeKMS())
        with pytest.raises(StorageError):
            s.put("user-a", "github", username="o", token="t")  # nosec B106


def test_store_multiple_providers_coexist(store):
    store.put("user-a", "github", username="octocat", token="gh")  # nosec B106
    store.put("user-a", "gitlab", username="octo", token="gl", host="gitlab.example.com")  # nosec B106
    assert store.get_token("user-a", "github") == "gh"
    assert store.get_token("user-a", "gitlab") == "gl"
    assert store.get_host("user-a", "gitlab") == "gitlab.example.com"
    providers = {e["provider"] for e in store.list_providers("user-a")}
    assert providers == {"github", "gitlab"}


def test_store_hosts_by_provider_maps_custom_host(store):
    store.put("hbp-user", "github", username="octocat", token="gh")  # nosec B106
    store.put("hbp-user", "gitlab", username="octo", token="gl", host="gitlab.example.com")  # nosec B106
    hosts = store.hosts_by_provider("hbp-user")
    assert hosts == {"github": None, "gitlab": "gitlab.example.com"}


# ── API route logic (handlers called directly; the app's auth middleware is
#    exercised elsewhere and returns 503 without Cognito configured) ──────────


def _as_user(monkeypatch, store, sub):
    monkeypatch.setattr(gc_routes, "require_current_user", lambda req: UserIdentity(email="e", sub=sub))
    monkeypatch.setattr(gc_routes, "_get_store", lambda: store)


def test_route_save_scopes_to_caller_sub_and_hides_token(store, monkeypatch):
    _as_user(monkeypatch, store, "save-user")
    body = gc_routes.GitCredentialRequest(username="octocat", token="ghp_x")  # nosec B106
    with patch("httpx.get", return_value=_Resp(200, {"login": "octocat"})):
        res = asyncio.run(gc_routes.save_git_credential("github", body, MagicMock()))
    assert res == {"provider": "github", "username": "octocat", "status": "connected"}
    assert "token" not in json.dumps(res)
    assert store.get_token("save-user", "github") == "ghp_x"


def test_route_list_returns_only_callers_connections(store, monkeypatch):
    # Distinct subs: moto S3 is session-scoped, so avoid subs other tests touch.
    store.put("list-a", "github", "octocat", "t")
    store.put("list-b", "github", "someone-else", "t2")
    _as_user(monkeypatch, store, "list-a")
    res = asyncio.run(gc_routes.list_git_credentials(MagicMock()))
    assert res == [{"provider": "github", "username": "octocat", "status": "connected"}]


def test_route_requires_a_verified_sub(store, monkeypatch):
    _as_user(monkeypatch, store, None)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(gc_routes.list_git_credentials(MagicMock()))
    assert ei.value.status_code == 403


def test_route_save_invalid_token_is_400(store, monkeypatch):
    _as_user(monkeypatch, store, "user-a")
    body = gc_routes.GitCredentialRequest(username="u", token="bad")  # nosec B106
    with patch("httpx.get", return_value=_Resp(401)):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(gc_routes.save_git_credential("github", body, MagicMock()))
    assert ei.value.status_code == 400


def test_route_save_and_test_reject_disabled_provider(store, monkeypatch):
    # gitlab is implemented but not enabled by default — both paths 400 before
    # touching the network, matching the UI which never lists it.
    _as_user(monkeypatch, store, "user-a")
    body = gc_routes.GitCredentialRequest(username="u", token="t")  # nosec B106
    with pytest.raises(HTTPException) as ei:
        asyncio.run(gc_routes.save_git_credential("gitlab", body, MagicMock()))
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei2:
        asyncio.run(gc_routes.test_git_credential("gitlab", MagicMock()))
    assert ei2.value.status_code == 400


def test_route_save_takes_self_hosted_host_from_config_not_client(store, monkeypatch):
    # The client no longer supplies a host; it comes from the provider's
    # *_CUSTOM_HOST config. With one set, the credential is stored under it.
    _as_user(monkeypatch, store, "ghe-user")
    body = gc_routes.GitCredentialRequest(username="u", token="t")  # nosec B106
    with _with_config(**{"gitcreds.github_custom_host": "https://github.xyz.com"}):
        with patch("httpx.get", return_value=_Resp(200, {"login": "u"})):
            asyncio.run(gc_routes.save_git_credential("github", body, MagicMock()))
    assert store.get_host("ghe-user", "github") == "github.xyz.com"


def test_route_save_defaults_to_cloud_when_no_custom_host(store, monkeypatch):
    # No custom host configured → stored host is None (cloud).
    _as_user(monkeypatch, store, "cloud-user")
    body = gc_routes.GitCredentialRequest(username="u", token="t")  # nosec B106
    with patch("httpx.get", return_value=_Resp(200, {"login": "u"})):
        asyncio.run(gc_routes.save_git_credential("github", body, MagicMock()))
    assert store.get_host("cloud-user", "github") is None


def test_route_test_without_connection_is_404(store, monkeypatch):
    _as_user(monkeypatch, store, "notest-user")  # never connected
    with pytest.raises(HTTPException) as ei:
        asyncio.run(gc_routes.test_git_credential("github", MagicMock()))
    assert ei.value.status_code == 404


def test_route_delete_missing_is_404(store, monkeypatch):
    _as_user(monkeypatch, store, "nodelete-user")  # nothing to delete
    with pytest.raises(HTTPException) as ei:
        asyncio.run(gc_routes.delete_git_credential("github", MagicMock()))
    assert ei.value.status_code == 404


# ── self-hosted host recognition + visibility mapping ───────────────────────


def _with_config(**overrides):
    """Patch providers.get_config with a dict of overrides (default fallback)."""
    base = {"gitcreds.http_timeout": 10.0}
    base.update(overrides)
    return patch(
        "src.gitcreds.providers.get_config",
        side_effect=lambda key, default=None: base.get(key, default),
    )


class _VisResp:
    """httpx response stand-in for get_visibility (adds .headers)."""

    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


def test_parse_self_hosted_requires_scheme_and_keeps_port():
    # Unset -> None.
    assert _parse_self_hosted("", "VAR") is None
    assert _parse_self_hosted(None, "VAR") is None
    # Full URL, no port.
    sh = _parse_self_hosted("https://gitlab.xyz.com", "VAR")
    assert sh.host == "gitlab.xyz.com"
    assert sh.base_url == "https://gitlab.xyz.com"
    # Scheme and port are preserved; host is lowercased; path is dropped.
    sh = _parse_self_hosted("http://GitLab.XYZ.com:8091/path", "VAR")
    assert sh.host == "gitlab.xyz.com"
    assert sh.base_url == "http://gitlab.xyz.com:8091"


def test_parse_self_hosted_rejects_bare_host():
    # A bare host has no scheme and must be rejected.
    with pytest.raises(ConfigurationError):
        _parse_self_hosted("gitlab.xyz.com", "VAR")
    with pytest.raises(ConfigurationError):
        _parse_self_hosted("gitlab.xyz.com:8443", "VAR")


def test_parse_self_hosted_rejects_out_of_range_port():
    with pytest.raises(ConfigurationError):
        _parse_self_hosted("https://gitlab.xyz.com:148217", "VAR")


def test_github_match_includes_configured_custom_host():
    with _with_config(**{"gitcreds.github_custom_host": "https://github.xyz.com"}):
        p = GitHubProvider()
        assert p.match("github.com")  # cloud still matches
        assert p.match("github.xyz.com")  # configured enterprise host matches
        assert not p.match("github.other.com")


def test_custom_host_unconfigured_is_not_matched():
    # No config -> only the cloud host matches; self-hosted stays unknown.
    with _with_config():
        assert not GitLabProvider().match("gitlab.xyz.com")
        assert get_provider("https://gitlab.xyz.com/group/proj.git") is None


def test_get_provider_resolves_configured_custom_host():
    with _with_config(**{"gitcreds.gitlab_custom_host": "https://gitlab.xyz.com"}):
        assert get_provider("https://gitlab.xyz.com/group/proj.git") == "gitlab"


def test_unconfigured_custom_host_visibility_fails():
    # Unknown host -> can't verify -> fail the create, with no network call.
    with _with_config():
        with patch("httpx.get") as http:
            with pytest.raises(VisibilityError):
                get_visibility("https://gitlab.xyz.com/g/p.git", "tok")
            http.assert_not_called()


def test_non_repo_url_visibility_fails():
    # A URL that isn't a repo (owner but no repo) -> fail, don't guess.
    with _with_config():
        with patch("httpx.get") as http:
            with pytest.raises(VisibilityError):
                get_visibility("https://github.com/owner", "tok")
            http.assert_not_called()


def test_gitlab_internal_is_recorded_public():
    with _with_config():
        with patch("httpx.get", return_value=_VisResp(200, {"visibility": "internal"})):
            assert get_visibility("https://gitlab.com/group/proj.git") == "public"


def test_gitlab_private_is_recorded_private():
    with _with_config():
        with patch("httpx.get", return_value=_VisResp(200, {"visibility": "private"})):
            assert get_visibility("https://gitlab.com/group/proj.git") == "private"


def test_gitlab_custom_host_visibility_uses_instance_api():
    with _with_config(**{"gitcreds.gitlab_custom_host": "https://gitlab.xyz.com"}):
        with patch(
            "httpx.get", return_value=_VisResp(200, {"visibility": "internal"})
        ) as http:
            assert get_visibility("https://gitlab.xyz.com/group/proj.git", "tok") == "public"
        called_url = http.call_args.args[0] if http.call_args.args else http.call_args.kwargs["url"]
        assert called_url.startswith("https://gitlab.xyz.com/api/v4/projects/")


def test_custom_host_visibility_honors_configured_scheme_and_port():
    # A non-443 port and http scheme in the config reach the instance verbatim.
    with _with_config(**{"gitcreds.gitlab_custom_host": "http://gitlab.xyz.com:8091"}):
        with patch(
            "httpx.get", return_value=_VisResp(200, {"visibility": "internal"})
        ) as http:
            assert get_visibility("https://gitlab.xyz.com/group/proj.git", "tok") == "public"
        called_url = http.call_args.args[0] if http.call_args.args else http.call_args.kwargs["url"]
        assert called_url.startswith("http://gitlab.xyz.com:8091/api/v4/projects/")


def test_github_custom_host_visibility_uses_v3_api():
    with _with_config(**{"gitcreds.github_custom_host": "https://github.xyz.com"}):
        with patch(
            "httpx.get", return_value=_VisResp(200, {"visibility": "internal"})
        ) as http:
            assert get_visibility("https://github.xyz.com/org/repo.git", "tok") == "public"
        called_url = http.call_args.args[0] if http.call_args.args else http.call_args.kwargs["url"]
        assert called_url == "https://github.xyz.com/api/v3/repos/org/repo"


def test_bitbucket_server_public_flag_maps_public():
    with _with_config(**{"gitcreds.bitbucket_custom_host": "https://bitbucket.xyz.com"}):
        with patch(
            "httpx.get", return_value=_VisResp(200, {"public": True})
        ) as http:
            assert get_visibility("https://bitbucket.xyz.com/KEY/repo.git", "tok") == "public"
        called_url = http.call_args.args[0] if http.call_args.args else http.call_args.kwargs["url"]
        assert called_url == "https://bitbucket.xyz.com/rest/api/1.0/projects/KEY/repos/repo"


def test_bitbucket_server_non_public_maps_private():
    with _with_config(**{"gitcreds.bitbucket_custom_host": "https://bitbucket.xyz.com"}):
        with patch("httpx.get", return_value=_VisResp(200, {"public": False})):
            assert get_visibility("https://bitbucket.xyz.com/KEY/repo.git", "tok") == "private"


def test_visibility_401_with_token_raises_visibility_error():
    # A rejected saved token is actionable -> VisibilityError (maps to 400).
    with _with_config():
        with patch("httpx.get", return_value=_VisResp(401)):
            with pytest.raises(VisibilityError):
                get_visibility("https://github.com/o/r.git", "tok")


def test_visibility_403_with_token_raises_visibility_error():
    # A non-rate-limit 403 with a token = rejected token -> fail, don't guess.
    with _with_config():
        with patch("httpx.get", return_value=_VisResp(403)):
            with pytest.raises(VisibilityError):
                get_visibility("https://github.com/o/r.git", "tok")


def test_visibility_github_rate_limited_403_is_provider_error_not_token():
    # A GitHub rate-limit 403 is a non-answer (retryable), not a bad token.
    resp = _VisResp(403, headers={"x-ratelimit-remaining": "0"})
    with _with_config():
        with patch("httpx.get", return_value=resp):
            with pytest.raises(ProviderError) as ei:
                get_visibility("https://github.com/o/r.git", "tok")
            assert not isinstance(ei.value, VisibilityError)


def test_visibility_5xx_raises_provider_error():
    # Provider gave no usable answer -> fail the create (503), never "private".
    with _with_config():
        with patch("httpx.get", return_value=_VisResp(503)):
            with pytest.raises(ProviderError):
                get_visibility("https://github.com/o/r.git", "tok")


def test_visibility_network_error_raises_provider_error():
    with _with_config():
        with patch("httpx.get", side_effect=httpx.HTTPError("boom")):
            with pytest.raises(ProviderError):
                get_visibility("https://github.com/o/r.git", "tok")


def test_visibility_follows_same_origin_redirect():
    # A renamed/transferred repo answers 301 to a new location on the same host.
    moved = _VisResp(301, headers={"location": "https://api.github.com/repositories/123"})
    final = _VisResp(200, {"visibility": "public"})
    with _with_config():
        with patch("httpx.get", side_effect=[moved, final]) as http:
            assert get_visibility("https://github.com/old/name.git") == "public"
        assert http.call_count == 2


def test_visibility_does_not_follow_cross_origin_redirect():
    # A redirect to another origin must not be followed (no token replay).
    moved = _VisResp(301, headers={"location": "https://evil.example.com/x"})
    with _with_config():
        with patch("httpx.get", side_effect=[moved]) as http:
            with pytest.raises(ProviderError):
                get_visibility("https://github.com/old/name.git", "tok")
        assert http.call_count == 1


def test_bitbucket_server_strips_scm_prefix():
    # Server/DC HTTPS clone URLs carry a /scm context prefix -> project/repo.
    with _with_config(**{"gitcreds.bitbucket_custom_host": "https://bb.xyz.com"}):
        with patch("httpx.get", return_value=_VisResp(200, {"public": True})) as http:
            assert get_visibility("https://bb.xyz.com/scm/PROJ/repo.git", "tok") == "public"
        called = http.call_args.args[0] if http.call_args.args else http.call_args.kwargs["url"]
        assert called == "https://bb.xyz.com/rest/api/1.0/projects/PROJ/repos/repo"


def test_bitbucket_server_personal_repo_scm_tilde_user():
    with _with_config(**{"gitcreds.bitbucket_custom_host": "https://bb.xyz.com"}):
        with patch("httpx.get", return_value=_VisResp(200, {"public": False})) as http:
            assert get_visibility("https://bb.xyz.com/scm/~jane/repo.git", "tok") == "private"
        called = http.call_args.args[0] if http.call_args.args else http.call_args.kwargs["url"]
        assert called == "https://bb.xyz.com/rest/api/1.0/projects/~jane/repos/repo"


def test_validate_token_rejects_host_not_configured_for_provider():
    # A non-cloud host that isn't the configured custom host must not get the token.
    with _with_config(**{"gitcreds.github_custom_host": "https://github.xyz.com"}):
        with patch("httpx.get") as http:
            with pytest.raises(ProviderError):
                GitHubProvider().validate_token("tok", host="other.example.com")
            http.assert_not_called()


def test_saved_token_matches_repo_cloud_credential():
    # Cloud credential (no saved host): used for cloud repos, not custom hosts.
    assert saved_token_matches_repo("github", None, "https://github.com/o/r.git")
    assert not saved_token_matches_repo("github", None, "https://github.xyz.com/o/r.git")


def test_saved_token_matches_repo_custom_host_credential():
    # Custom-host credential: used for its own host, not the cloud host.
    assert saved_token_matches_repo("github", "github.xyz.com", "https://github.xyz.com/o/r.git")
    assert not saved_token_matches_repo("github", "github.xyz.com", "https://github.com/o/r.git")


def test_saved_token_matches_repo_normalizes_url_and_port():
    # A stored value with scheme/port still matches the repo's bare host.
    assert saved_token_matches_repo(
        "github", "https://github.xyz.com:8443", "https://github.xyz.com/o/r.git"
    )


def test_saved_token_matches_repo_unknown_provider_is_false():
    assert not saved_token_matches_repo("nope", None, "https://github.com/o/r.git")


# ── resolve_repo_credential: one place that picks the token for a repo ───────


class _FakeStore:
    """Minimal stand-in for GitCredentialStore: entries are {provider: (host, token)}."""

    def __init__(self, entries):
        self._e = entries

    def get_credential(self, sub, provider):
        return self._e.get(provider, (None, None))

    def hosts_by_provider(self, sub):
        return {p: h for p, (h, _t) in self._e.items()}


def _with_store(entries):
    return patch("src.gitcreds.resolver.GitCredentialStore", return_value=_FakeStore(entries))


def test_resolve_cloud_token_for_cloud_repo():
    with _with_store({"github": (None, "gh")}):
        provider, token = resolve_repo_credential("u", "https://github.com/o/r.git")
        assert provider.id == "github" and token == "gh"  # nosec B105  # test fixture token, not a real credential


def test_resolve_custom_token_for_custom_repo_via_fallback():
    # Host not in server config, but the user saved a token under it.
    with _with_store({"github": ("github.xyz.com", "ent")}):
        provider, token = resolve_repo_credential("u", "https://github.xyz.com/o/r.git")
        assert provider.id == "github" and token == "ent"  # nosec B105  # test fixture token, not a real credential


def test_resolve_does_not_send_cloud_token_to_custom_host():
    with _with_store({"github": (None, "gh")}):
        _, token = resolve_repo_credential("u", "https://github.xyz.com/o/r.git")
        assert token is None


def test_resolve_does_not_send_custom_token_to_cloud_host():
    with _with_store({"github": ("github.xyz.com", "ent")}):
        provider, token = resolve_repo_credential("u", "https://github.com/o/r.git")
        assert provider.id == "github" and token is None


def test_resolve_without_sub_is_none():
    with _with_store({"github": (None, "gh")}):
        assert resolve_repo_credential(None, "https://github.com/o/r.git") == (None, None)


def test_resolve_no_saved_credential_yields_no_token():
    with _with_store({}):
        provider, token = resolve_repo_credential("u", "https://github.com/o/r.git")
        assert provider.id == "github" and token is None
