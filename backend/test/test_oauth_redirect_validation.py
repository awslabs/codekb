"""Regression tests for FIX-05 — OAuth redirect_uri open redirect prevention.

The MCP OAuth proxy must reject redirect_uri values that do not point to a
loopback address. Without this, an attacker can steal authorization codes by
injecting their own redirect_uri. Only loopback callbacks are permitted, which
covers every known MCP client while closing the open-redirect hole.
"""

import pytest

from src.api.app import (
    _is_redirect_allowed,
    _LOOPBACK_HOSTS,
)


class TestLoopbackAllowed:
    """Loopback addresses over http or https are permitted."""

    @pytest.mark.parametrize(
        "uri",
        [
            "http://localhost:3000/callback",
            "http://localhost/auth/callback",
            "http://127.0.0.1:8080/callback",
            "http://[::1]:9999/callback",
            "https://localhost:3000/callback",
            "https://127.0.0.1/callback",
        ],
    )
    def test_loopback_allowed(self, uri):
        assert _is_redirect_allowed(uri) is True


class TestRejections:
    """Anything that is not a loopback callback is rejected."""

    @pytest.mark.parametrize(
        "uri",
        [
            "https://evil.example.com/steal",
            "https://attacker.io/callback",
            "https://app.example.com/oauth/callback",  # non-loopback, even over https
            "ftp://localhost/callback",  # wrong scheme
            "",  # empty
            "not-a-url",  # no scheme parsed
            "https://",  # no host
        ],
    )
    def test_unauthorized_rejected(self, uri):
        assert _is_redirect_allowed(uri) is False

    def test_metadata_endpoint_rejected(self):
        """Cloud metadata endpoints should never be redirect targets."""
        assert _is_redirect_allowed("http://169.254.169.254/latest/meta-data/") is False

    def test_subdomain_of_localhost_rejected(self):
        """Only exact loopback hosts pass, not lookalikes."""
        assert _is_redirect_allowed("http://localhost.evil.com/callback") is False


class TestLoopbackHostSet:
    """The _LOOPBACK_HOSTS set contains the expected entries."""

    def test_contains_localhost(self):
        assert "localhost" in _LOOPBACK_HOSTS

    def test_contains_ipv4_loopback(self):
        assert "127.0.0.1" in _LOOPBACK_HOSTS

    def test_contains_ipv6_loopback(self):
        assert "[::1]" in _LOOPBACK_HOSTS or "::1" in _LOOPBACK_HOSTS
