"""Regression tests for Git URL SSRF mitigations.

Controls in validate_git_url:
- Scheme allowlist (https/ssh) — blocks ext:: command execution, file:// reads,
  and plaintext transports.
- Host allowlist (CODEKB_ALLOWED_GIT_HOSTS) — FAIL-CLOSED and REQUIRED. With no
  allowlist configured, every host is rejected. Otherwise the host must match an
  entry exactly or be a dotted subdomain.
- Post-resolution address guard — the allowlisted host is resolved and the URL
  is rejected if it lands on a private, loopback or link-local/metadata address
  (CODEKB_ALLOW_PRIVATE_GIT_ADDRESSES opts private/loopback back in).

Two autouse fixtures keep these tests deterministic and network-free: one sets a
default allowlist so generic accept-tests have something to match, the other
makes every hostname resolve to a public address. Tests that exercise the
allowlist or the address guard override them.
"""

import ipaddress
import re
from unittest.mock import patch

import pytest

from src.service.codekb_service import (
    ALLOWED_GIT_URL_SCHEMES,
    CodeKBService,
    ServiceError,
    _GIT_SUBPROCESS_ENV,
    _blocked_address_category,
    _embedded_url_token,
    _host_in_allowlist,
    _resolve_host_addresses,
    lookup_repo_visibility,
    validate_git_url,
)

_PUBLIC_IP = ipaddress.ip_address("93.184.216.34")

_DEFAULT_ALLOWLIST = "github.com example.com git.internal.corp"


@pytest.fixture(autouse=True)
def _default_allowlist(monkeypatch):
    """Set a default allowlist so generic accept-tests match.

    Allowlist-focused tests override or clear it.
    """
    monkeypatch.setenv("CODEKB_ALLOWED_GIT_HOSTS", _DEFAULT_ALLOWLIST)


@pytest.fixture(autouse=True)
def _resolves_public(monkeypatch):
    """Resolve every hostname to a public address by default.

    Keeps the scheme/allowlist/credential tests off real DNS. Tests that
    exercise the post-resolution guard override this with their own addresses.
    """
    monkeypatch.setattr(
        "src.service.codekb_service._resolve_host_addresses",
        lambda hostname: [_PUBLIC_IP],
    )


class TestAccepts:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/org/repo.git",
            "ssh://git@github.com/org/repo.git",
            "ssh://git@example.com/org/repo.git",
        ],
    )
    def test_allowlisted_hosts_pass(self, url):
        assert validate_git_url(url) == url

    def test_allowlisted_private_ip_literal_passes(self, monkeypatch):
        """A customer-VPC Git server addressed by RFC-1918 literal passes when allowlisted."""
        monkeypatch.setenv("CODEKB_ALLOWED_GIT_HOSTS", "10.0.1.50")
        assert (
            validate_git_url("https://10.0.1.50/org/repo.git") == "https://10.0.1.50/org/repo.git"
        )

    def test_scheme_is_case_insensitive(self):
        assert validate_git_url("HTTPS://github.com/org/repo.git")

    def test_pasted_whitespace_is_trimmed(self):
        assert validate_git_url("  https://github.com/x.git  ") == "https://github.com/x.git"

    def test_allowlist_is_https_and_ssh(self):
        assert ALLOWED_GIT_URL_SCHEMES == frozenset({"https", "ssh"})


class TestRejectsScheme:
    """Scheme is checked before the allowlist, so these fail on scheme regardless."""

    def test_file_scheme(self):
        with pytest.raises(ServiceError, match="scheme 'file' is not supported"):
            validate_git_url("file:///etc/passwd")

    @pytest.mark.parametrize("url", ["git://evil.example/repo", "http://evil.example/repo"])
    def test_plaintext_transports(self, url):
        with pytest.raises(ServiceError, match="is not supported"):
            validate_git_url(url)

    def test_ext_scheme(self):
        with pytest.raises(ServiceError, match="scheme 'ext' is not supported"):
            validate_git_url("ext::sh -c 'id'")

    def test_scp_style_remote(self):
        with pytest.raises(ServiceError, match="must start with"):
            validate_git_url("git@github.com:org/repo.git")

    def test_leading_dash_would_be_read_by_git_as_an_option(self):
        with pytest.raises(ServiceError, match="must start with"):
            validate_git_url("--upload-pack=touch /tmp/pwned")

    def test_missing_host(self):
        with pytest.raises(ServiceError, match="must include a host"):
            validate_git_url("https://")

    @pytest.mark.parametrize("url", ["", "   ", None])
    def test_empty(self, url):
        with pytest.raises(ServiceError, match="required"):
            validate_git_url(url)


class TestFailClosed:
    """With no allowlist configured, every otherwise-valid host is rejected."""

    def test_unset_allowlist_rejects_public_host(self, monkeypatch):
        monkeypatch.delenv("CODEKB_ALLOWED_GIT_HOSTS", raising=False)
        with pytest.raises(ServiceError, match="No Git hosts are permitted"):
            validate_git_url("https://github.com/org/repo.git")

    def test_empty_allowlist_rejects_public_host(self, monkeypatch):
        monkeypatch.setenv("CODEKB_ALLOWED_GIT_HOSTS", "   ")
        with pytest.raises(ServiceError, match="No Git hosts are permitted"):
            validate_git_url("https://github.com/org/repo.git")


class TestHostAllowlist:
    """CODEKB_ALLOWED_GIT_HOSTS matching — the authoritative SSRF control."""

    def test_host_not_in_allowlist_is_rejected(self, monkeypatch):
        monkeypatch.setenv("CODEKB_ALLOWED_GIT_HOSTS", "git.example.org")
        with pytest.raises(ServiceError, match="not in the allowed Git hosts list"):
            validate_git_url("https://github.com/org/repo.git")

    def test_exact_host_match_passes(self, monkeypatch):
        monkeypatch.setenv("CODEKB_ALLOWED_GIT_HOSTS", "github.com")
        assert validate_git_url("https://github.com/org/repo.git")

    def test_subdomain_of_allowlisted_host_passes(self, monkeypatch):
        monkeypatch.setenv("CODEKB_ALLOWED_GIT_HOSTS", "example.org")
        assert validate_git_url("https://git.example.org/pkg/repo.git")

    def test_allowlist_accepts_comma_or_whitespace_separators(self, monkeypatch):
        monkeypatch.setenv("CODEKB_ALLOWED_GIT_HOSTS", "git.example.org, github.com  gitlab.com")
        assert validate_git_url("https://gitlab.com/org/repo.git")

    def test_lookalike_suffix_is_not_a_subdomain_match(self, monkeypatch):
        """'evilexample.org' must not match an 'example.org' allowlist entry."""
        monkeypatch.setenv("CODEKB_ALLOWED_GIT_HOSTS", "example.org")
        with pytest.raises(ServiceError, match="not in the allowed Git hosts list"):
            validate_git_url("https://evilexample.org/org/repo.git")

    def test_internal_ip_literal_not_on_allowlist_is_rejected_by_allowlist(self):
        """A raw internal address is rejected by the allowlist, which runs first.

        The post-resolution address guard (TestResolvedAddressGuard) is the
        second line of defence for an allowlisted NAME that resolves internally;
        this literal never reaches it because the allowlist rejects it up front.
        """
        with pytest.raises(ServiceError, match="not in the allowed Git hosts list"):
            validate_git_url("https://169.254.169.254/latest/meta-data/")

    def test_host_in_allowlist_helper(self):
        allow = frozenset({"example.org", "github.com"})
        assert _host_in_allowlist("github.com", allow)
        assert _host_in_allowlist("git.example.org", allow)
        assert not _host_in_allowlist("evilexample.org", allow)
        assert not _host_in_allowlist("gitlab.com", allow)


class TestSubprocessEnvHardening:
    """Git subprocess env blocks credential prompts, protocol downgrade, redirects."""

    def test_terminal_prompt_disabled(self):
        assert _GIT_SUBPROCESS_ENV["GIT_TERMINAL_PROMPT"] == "0"

    def test_askpass_cleared(self):
        assert _GIT_SUBPROCESS_ENV["GIT_ASKPASS"] == ""

    def test_protocol_restricted_to_https_ssh(self):
        assert _GIT_SUBPROCESS_ENV["GIT_ALLOW_PROTOCOL"] == "https:ssh"

    def test_http_redirects_disabled(self):
        """Same-protocol redirect SSRF (allowed https host -> internal https) is closed."""
        assert _GIT_SUBPROCESS_ENV["GIT_CONFIG_COUNT"] == "1"
        assert _GIT_SUBPROCESS_ENV["GIT_CONFIG_KEY_0"] == "http.followRedirects"
        assert _GIT_SUBPROCESS_ENV["GIT_CONFIG_VALUE_0"] == "false"

    def test_env_is_passed_to_ls_remote(self):
        codekb_service = object.__new__(CodeKBService)
        with patch("src.service.codekb_service.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stderr": "err"})()
            codekb_service._fetch_remote_refs("https://github.com/org/repo.git")
            mock_run.assert_called_once()
            _, kwargs = mock_run.call_args
            assert kwargs["env"] is _GIT_SUBPROCESS_ENV

    def test_env_is_passed_to_clone(self):
        codekb_service = object.__new__(CodeKBService)
        with patch.object(CodeKBService, "_update_pipeline_status"):
            with patch("src.service.codekb_service.subprocess.run") as mock_run:
                mock_run.return_value = type("R", (), {"returncode": 1, "stderr": "err"})()
                codekb_service._acquire_source_git("space-id", "/tmp/x", "https://github.com/o/r.git")
                mock_run.assert_called_once()
                _, kwargs = mock_run.call_args
                assert kwargs["env"] is _GIT_SUBPROCESS_ENV


class TestApiInputParameters:
    """Service entry points reject unsafe URLs before persisting or starting work."""

    @staticmethod
    def _codekb_service() -> CodeKBService:
        return object.__new__(CodeKBService)

    def test_list_remote_branches_rejects_bad_scheme_before_spawning_git(self):
        with patch("src.service.codekb_service.subprocess.run") as mock_run:
            with pytest.raises(ServiceError, match="scheme 'file' is not supported"):
                self._codekb_service().list_remote_branches("file:///etc/passwd")
            mock_run.assert_not_called()

    def test_list_remote_branches_rejects_when_allowlist_unset(self, monkeypatch):
        monkeypatch.delenv("CODEKB_ALLOWED_GIT_HOSTS", raising=False)
        with patch("src.service.codekb_service.subprocess.run") as mock_run:
            with pytest.raises(ServiceError, match="No Git hosts are permitted"):
                self._codekb_service().list_remote_branches("https://github.com/org/repo.git")
            mock_run.assert_not_called()

    def test_list_remote_branches_rejects_host_not_on_allowlist(self, monkeypatch):
        monkeypatch.setenv("CODEKB_ALLOWED_GIT_HOSTS", "git.example.org")
        with patch("src.service.codekb_service.subprocess.run") as mock_run:
            with pytest.raises(ServiceError, match="not in the allowed Git hosts list"):
                self._codekb_service().list_remote_branches("https://github.com/org/repo.git")
            mock_run.assert_not_called()

    def test_list_remote_branches_still_requires_a_url(self):
        with pytest.raises(ServiceError, match="required"):
            self._codekb_service().list_remote_branches("")

    def test_start_analysis_rejects_bad_scheme_without_persisting(self):
        codekb_service = self._codekb_service()
        with patch.object(CodeKBService, "_write_status") as mock_write:
            with patch("threading.Thread") as mock_thread:
                with pytest.raises(ServiceError, match="scheme 'git' is not supported"):
                    codekb_service.start_analysis(git_url="git://evil.example/x")
            mock_write.assert_not_called()
            mock_thread.assert_not_called()

    def test_start_analysis_rejects_when_allowlist_unset_without_persisting(self, monkeypatch):
        monkeypatch.delenv("CODEKB_ALLOWED_GIT_HOSTS", raising=False)
        codekb_service = self._codekb_service()
        with patch.object(CodeKBService, "_write_status") as mock_write:
            with patch("threading.Thread") as mock_thread:
                with pytest.raises(ServiceError, match="No Git hosts are permitted"):
                    codekb_service.start_analysis(git_url="https://github.com/org/repo.git")
            mock_write.assert_not_called()
            mock_thread.assert_not_called()


class TestEmbeddedCredentials:
    """Credentials in an http(s) git_url are gated by a feature flag; SSH is exempt."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://user:tok@github.com/org/repo.git",
            "https://x-access-token@github.com/org/repo.git",
        ],
    )
    def test_https_userinfo_rejected_by_default(self, url):
        with patch(
            "src.service.codekb_service.git_url_credentials_allowed", return_value=False
        ):
            with pytest.raises(ServiceError, match="Remove the username or token"):
                validate_git_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://user:tok@github.com/org/repo.git",
            "https://x-access-token@github.com/org/repo.git",
        ],
    )
    def test_https_userinfo_allowed_when_flag_on(self, url):
        with patch(
            "src.service.codekb_service.git_url_credentials_allowed", return_value=True
        ):
            assert validate_git_url(url) == url

    def test_ssh_git_user_is_not_treated_as_a_credential(self):
        # Standard SSH clone URL — the "git" user must pass regardless of the flag.
        with patch(
            "src.service.codekb_service.git_url_credentials_allowed", return_value=False
        ):
            assert (
                validate_git_url("ssh://git@github.com/org/repo.git")
                == "ssh://git@github.com/org/repo.git"
            )

    def test_ssh_url_with_password_is_gated(self):
        # A password in SSH userinfo is a secret and must be gated like http(s).
        with patch(
            "src.service.codekb_service.git_url_credentials_allowed", return_value=False
        ):
            with pytest.raises(ServiceError, match="Remove the username or token"):
                validate_git_url("ssh://user:ghp_token@github.com/org/repo.git")

    def test_ssh_url_with_password_allowed_when_flag_on(self):
        # With the flag on, an SSH password is permitted just like http(s) userinfo.
        url = "ssh://user:ghp_token@github.com/org/repo.git"
        with patch(
            "src.service.codekb_service.git_url_credentials_allowed", return_value=True
        ):
            assert validate_git_url(url) == url

    def test_start_analysis_rejects_embedded_credentials_without_persisting(self):
        codekb_service = object.__new__(CodeKBService)
        with patch(
            "src.service.codekb_service.git_url_credentials_allowed", return_value=False
        ):
            with patch.object(CodeKBService, "_write_status") as mock_write:
                with patch("threading.Thread") as mock_thread:
                    with pytest.raises(ServiceError, match="Remove the username or token"):
                        codekb_service.start_analysis(git_url="https://user:tok@github.com/o/r.git")
            mock_write.assert_not_called()
            mock_thread.assert_not_called()


class TestEmbeddedUrlToken:
    """_embedded_url_token pulls the API token out of a clone URL's userinfo."""

    def test_user_colon_token_uses_password(self):
        assert (
            _embedded_url_token("https://user:ghp_tok@github.com/o/r.git") == "ghp_tok"
        )

    def test_bare_userinfo_is_the_token(self):
        assert (
            _embedded_url_token("https://ghp_tok@github.com/o/r.git") == "ghp_tok"
        )

    def test_percent_encoded_token_is_decoded(self):
        # A token with reserved characters is percent-encoded in the URL; the API
        # needs its real value.
        assert (
            _embedded_url_token("https://user:p%40ss%2Fword@github.com/o/r.git")
            == "p@ss/word"
        )

    def test_no_userinfo_returns_none(self):
        assert _embedded_url_token("https://github.com/o/r.git") is None


class TestLookupRepoVisibilityUrlTokenFallback:
    """lookup_repo_visibility falls back to a URL-embedded token when no saved
    credential applies and the deployment allows credentials in the clone URL —
    the mode used when there is no git-credentials KMS key.
    """

    def test_url_token_used_when_no_saved_token_and_flag_on(self):
        url = "https://user:ghp_tok@github.com/o/r.git"
        with patch(
            "src.service.codekb_service.resolve_repo_credential",
            return_value=(None, None),
        ):
            with patch(
                "src.service.codekb_service.git_url_credentials_allowed",
                return_value=True,
            ):
                with patch(
                    "src.service.codekb_service.get_visibility", return_value="private"
                ) as gv:
                    assert lookup_repo_visibility(url, "sub") == "private"
        # The embedded token was passed to the provider probe, not an anonymous None.
        passed_token = gv.call_args.args[1] if len(gv.call_args.args) > 1 else gv.call_args.kwargs["token"]
        assert passed_token == "ghp_tok"  # nosec B105  # test fixture token, not a real credential

    def test_saved_token_takes_precedence_over_url_token(self):
        url = "https://user:ghp_url@github.com/o/r.git"
        with patch(
            "src.service.codekb_service.resolve_repo_credential",
            return_value=(object(), "ghp_saved"),
        ):
            with patch(
                "src.service.codekb_service.git_url_credentials_allowed",
                return_value=True,
            ):
                with patch(
                    "src.service.codekb_service.get_visibility", return_value="private"
                ) as gv:
                    assert lookup_repo_visibility(url, "sub") == "private"
        passed_token = gv.call_args.args[1] if len(gv.call_args.args) > 1 else gv.call_args.kwargs["token"]
        assert passed_token == "ghp_saved"  # nosec B105  # test fixture token, not a real credential

    def test_url_token_not_used_when_flag_off(self):
        # Without the flag the URL never carries credentials through validation, so
        # the probe stays anonymous even if a token is somehow present.
        url = "https://user:ghp_tok@github.com/o/r.git"
        with patch(
            "src.service.codekb_service.resolve_repo_credential",
            return_value=(None, None),
        ):
            with patch(
                "src.service.codekb_service.git_url_credentials_allowed",
                return_value=False,
            ):
                with patch(
                    "src.service.codekb_service.get_visibility", return_value="private"
                ) as gv:
                    assert lookup_repo_visibility(url, "sub") == "private"
        passed_token = gv.call_args.args[1] if len(gv.call_args.args) > 1 else gv.call_args.kwargs["token"]
        assert passed_token is None

    def test_anonymous_when_flag_on_but_no_url_token(self):
        url = "https://github.com/o/r.git"
        with patch(
            "src.service.codekb_service.resolve_repo_credential",
            return_value=(None, None),
        ):
            with patch(
                "src.service.codekb_service.git_url_credentials_allowed",
                return_value=True,
            ):
                with patch(
                    "src.service.codekb_service.get_visibility", return_value="public"
                ) as gv:
                    assert lookup_repo_visibility(url, "sub") == "public"
        passed_token = gv.call_args.args[1] if len(gv.call_args.args) > 1 else gv.call_args.kwargs["token"]
        assert passed_token is None


class _VisResp:
    """httpx response stand-in for the real get_visibility path."""

    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class TestUrlTokenReachesEveryProvider:
    """End-to-end: a URL-embedded token flows through lookup_repo_visibility and
    the real get_visibility into the right auth header for GitHub, GitLab, and
    Bitbucket — proving the fallback is not GitHub-only.

    Each case drives the full path with only the outbound HTTP call mocked.
    """

    def _run(self, url, response):
        with patch(
            "src.service.codekb_service.resolve_repo_credential",
            return_value=(None, None),
        ):
            with patch(
                "src.service.codekb_service.git_url_credentials_allowed",
                return_value=True,
            ):
                with patch("httpx.get", return_value=response) as http:
                    verdict = lookup_repo_visibility(url, "sub")
        headers = http.call_args.kwargs["headers"]
        return verdict, headers

    def test_github_cloud_sends_bearer(self):
        verdict, headers = self._run(
            "https://user:ghp_tok@github.com/o/r.git",
            _VisResp(200, {"visibility": "public"}),
        )
        assert verdict == "public"
        assert headers["Authorization"] == "Bearer ghp_tok"

    def test_gitlab_cloud_sends_private_token(self):
        verdict, headers = self._run(
            "https://user:glpat_tok@gitlab.com/group/proj.git",
            _VisResp(200, {"visibility": "public"}),
        )
        assert verdict == "public"
        assert headers["PRIVATE-TOKEN"] == "glpat_tok"

    def test_bitbucket_cloud_sends_bearer(self):
        verdict, headers = self._run(
            "https://user:bb_tok@bitbucket.org/workspace/repo.git",
            _VisResp(200, {"is_private": False}),
        )
        assert verdict == "public"
        assert headers["Authorization"] == "Bearer bb_tok"


class TestBlockedAddressCategory:
    """_blocked_address_category classifies a resolved IP, honoring the opt-in."""

    @pytest.mark.parametrize(
        "addr, category",
        [
            # Always blocked — the opt-in does not override these.
            ("169.254.169.254", "link-local/metadata"),  # cloud instance metadata
            ("169.254.0.1", "link-local/metadata"),
            ("fe80::1", "link-local/metadata"),
            ("224.0.0.1", "multicast"),
            ("ff02::1", "multicast"),
            ("240.0.0.1", "reserved"),
            ("0.0.0.0", "unspecified"),
            ("::", "unspecified"),
            # Blocked unless the private opt-in is on.
            ("127.0.0.1", "loopback"),
            ("::1", "loopback"),
            ("10.0.0.5", "private"),
            ("172.16.3.9", "private"),
            ("192.168.1.10", "private"),
            ("fd00::1", "private"),
        ],
    )
    def test_internal_addresses_are_blocked_by_default(self, addr, category):
        ip = ipaddress.ip_address(addr)
        assert _blocked_address_category(ip, allow_private=False) == category

    @pytest.mark.parametrize("addr", ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"])
    def test_public_addresses_pass(self, addr):
        ip = ipaddress.ip_address(addr)
        assert _blocked_address_category(ip, allow_private=False) is None

    @pytest.mark.parametrize(
        "addr", ["127.0.0.1", "::1", "10.0.0.5", "172.16.3.9", "192.168.1.10", "fd00::1"]
    )
    def test_private_and_loopback_pass_with_opt_in(self, addr):
        ip = ipaddress.ip_address(addr)
        assert _blocked_address_category(ip, allow_private=True) is None

    @pytest.mark.parametrize(
        "addr, category",
        [
            ("169.254.169.254", "link-local/metadata"),
            ("fe80::1", "link-local/metadata"),
            ("224.0.0.1", "multicast"),
            ("240.0.0.1", "reserved"),
            ("0.0.0.0", "unspecified"),
        ],
    )
    def test_opt_in_never_unblocks_metadata_or_reserved(self, addr, category):
        ip = ipaddress.ip_address(addr)
        assert _blocked_address_category(ip, allow_private=True) == category

    def test_ipv4_mapped_ipv6_is_unwrapped(self):
        """An internal v4 target wrapped as ::ffff:a.b.c.d must still be caught."""
        ip = ipaddress.ip_address("::ffff:169.254.169.254")
        assert _blocked_address_category(ip, allow_private=False) == "link-local/metadata"
        ip = ipaddress.ip_address("::ffff:10.0.0.5")
        assert _blocked_address_category(ip, allow_private=False) == "private"

    @pytest.mark.parametrize(
        "addr, category",
        [
            # 6to4 (2002::/16) embedding the v4 target in bits 16..47.
            ("2002:0a00:0005::1", "private"),  # 10.0.0.5
            ("2002:a9fe:a9fe::1", "link-local/metadata"),  # 169.254.169.254
            # Teredo (2001:0000::/32): client v4 is the low 32 bits XOR 0xFFFFFFFF.
            # 2001:0000:<server>:<flags><obfport>:<obfclient_hi>:<obfclient_lo>
            ("2001:0000:0808:0808:8000:ffff:f5ff:fffa", "private"),  # client 10.0.0.5
            ("2001:0000:0808:0808:8000:ffff:5601:5601", "link-local/metadata"),  # 169.254.169.254
            # NAT64 Well-Known Prefix (64:ff9b::/96) with the v4 target in the low 32 bits.
            ("64:ff9b::0a00:0005", "private"),  # 10.0.0.5
            ("64:ff9b::a9fe:a9fe", "link-local/metadata"),  # 169.254.169.254
        ],
    )
    def test_ipv6_wrappers_around_internal_v4_are_unwrapped(self, addr, category):
        """6to4, Teredo and NAT64 wrappers must not smuggle an internal v4 target
        past the guard. A regression dropping any unwrap flips these to None."""
        ip = ipaddress.ip_address(addr)
        assert _blocked_address_category(ip, allow_private=False) == category


class TestResolvedAddressGuard:
    """Post-resolution SSRF guard in validate_git_url.

    The host allowlist matches on name only, so an allowlisted NAME (or a dotted
    subdomain of one, or a name whose DNS answer an attacker influences) can
    still resolve onto an internal address. These tests drive validate_git_url
    with github.com (on the default allowlist) while overriding what it resolves
    to.
    """

    def _resolves_to(self, monkeypatch, *addrs):
        ips = [ipaddress.ip_address(a) for a in addrs]
        monkeypatch.setattr(
            "src.service.codekb_service._resolve_host_addresses",
            lambda hostname: ips,
        )

    @pytest.mark.parametrize(
        "addr",
        ["169.254.169.254", "127.0.0.1", "10.0.0.5", "192.168.1.1", "::1", "fe80::1", "fd00::1"],
    )
    def test_allowlisted_name_resolving_internally_is_rejected(self, monkeypatch, addr):
        self._resolves_to(monkeypatch, addr)
        with pytest.raises(ServiceError, match="disallowed"):
            validate_git_url("https://github.com/org/repo.git")

    def test_any_internal_address_in_the_set_rejects(self, monkeypatch):
        """A host that returns one public and one internal A record is rejected."""
        self._resolves_to(monkeypatch, "93.184.216.34", "10.0.0.5")
        with pytest.raises(ServiceError, match="disallowed private"):
            validate_git_url("https://github.com/org/repo.git")

    def test_public_resolution_passes(self, monkeypatch):
        self._resolves_to(monkeypatch, "93.184.216.34")
        assert validate_git_url("https://github.com/org/repo.git") == (
            "https://github.com/org/repo.git"
        )

    def test_private_resolution_rejected_without_opt_in(self, monkeypatch):
        self._resolves_to(monkeypatch, "10.0.0.5")
        with pytest.raises(ServiceError, match="CODEKB_ALLOW_PRIVATE_GIT_ADDRESSES"):
            validate_git_url("https://github.com/org/repo.git")

    def test_private_resolution_allowed_with_opt_in(self, monkeypatch):
        self._resolves_to(monkeypatch, "10.0.0.5")
        with patch(
            "src.service.codekb_service.git_private_addresses_allowed", return_value=True
        ):
            assert validate_git_url("https://github.com/org/repo.git")

    def test_metadata_rejected_even_with_opt_in(self, monkeypatch):
        """The opt-in covers private/loopback only; metadata stays blocked."""
        self._resolves_to(monkeypatch, "169.254.169.254")
        with patch(
            "src.service.codekb_service.git_private_addresses_allowed", return_value=True
        ):
            with pytest.raises(ServiceError, match="link-local/metadata"):
                validate_git_url("https://github.com/org/repo.git")

    def test_unresolvable_host_is_rejected_fail_closed(self, monkeypatch):
        def _unresolvable(hostname):
            raise ServiceError(f"git_url host '{hostname}' could not be resolved.")

        monkeypatch.setattr(
            "src.service.codekb_service._resolve_host_addresses", _unresolvable
        )
        with pytest.raises(ServiceError, match="could not be resolved"):
            validate_git_url("https://github.com/org/repo.git")

    def test_allowlisted_ip_literal_is_trusted_without_resolution(self, monkeypatch):
        """An IP literal placed on the allowlist by exact value is trusted as-is:
        one unambiguous target, no DNS indirection to rebind. Resolution is not
        even attempted."""
        monkeypatch.setenv("CODEKB_ALLOWED_GIT_HOSTS", "10.0.1.50")

        def _must_not_resolve(hostname):
            raise AssertionError("an allowlisted IP literal must not be resolved")

        monkeypatch.setattr(
            "src.service.codekb_service._resolve_host_addresses", _must_not_resolve
        )
        assert (
            validate_git_url("https://10.0.1.50/org/repo.git")
            == "https://10.0.1.50/org/repo.git"
        )

    @pytest.mark.parametrize(
        "addr, category",
        [
            ("169.254.169.254", "link-local/metadata"),  # cloud instance metadata
            ("169.254.0.1", "link-local/metadata"),
            ("224.0.0.1", "multicast"),
            ("0.0.0.0", "unspecified"),
            ("240.0.0.1", "reserved"),
        ],
    )
    def test_allowlisted_literal_in_always_blocked_category_is_rejected(
        self, monkeypatch, addr, category
    ):
        """An exact-match IP literal on the allowlist trusts only the
        private/loopback opt-in; the always-blocked categories still apply, so a
        literal like 169.254.169.254 can never be waved through by allowlisting
        it. Resolution is not attempted for a literal, blocked or not."""
        monkeypatch.setenv("CODEKB_ALLOWED_GIT_HOSTS", addr)

        def _must_not_resolve(hostname):
            raise AssertionError("an allowlisted IP literal must not be resolved")

        monkeypatch.setattr(
            "src.service.codekb_service._resolve_host_addresses", _must_not_resolve
        )
        with pytest.raises(ServiceError, match=re.escape(category)):
            validate_git_url(f"https://{addr}/org/repo.git")


class TestResolveHostAddresses:
    """_resolve_host_addresses itself: de-dupe, zone-strip, fail-closed. socket is
    mocked so the suite never touches real DNS."""

    @staticmethod
    def _addrinfo(*ips):
        # (family, type, proto, canonname, sockaddr) — only sockaddr[0] is read.
        return [(0, 0, 0, "", (ip, 0)) for ip in ips]

    def test_collects_and_dedupes_addresses(self):
        with patch(
            "src.service.codekb_service.socket.getaddrinfo",
            return_value=self._addrinfo("93.184.216.34", "93.184.216.34", "2606:4700::1111"),
        ):
            result = _resolve_host_addresses("example.com")
        assert result == [
            ipaddress.ip_address("93.184.216.34"),
            ipaddress.ip_address("2606:4700::1111"),
        ]

    def test_strips_ipv6_zone_suffix(self):
        with patch(
            "src.service.codekb_service.socket.getaddrinfo",
            return_value=self._addrinfo("fe80::1%eth0"),
        ):
            result = _resolve_host_addresses("example.com")
        assert result == [ipaddress.ip_address("fe80::1")]

    def test_resolution_failure_raises(self):
        import socket as _socket

        with patch(
            "src.service.codekb_service.socket.getaddrinfo",
            side_effect=_socket.gaierror("name or service not known"),
        ):
            with pytest.raises(ServiceError, match="could not be resolved"):
                _resolve_host_addresses("nope.invalid")

    def test_empty_resolution_raises(self):
        with patch(
            "src.service.codekb_service.socket.getaddrinfo", return_value=[]
        ):
            with pytest.raises(ServiceError, match="could not be resolved"):
                _resolve_host_addresses("example.com")
