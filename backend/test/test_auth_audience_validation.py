"""Regression tests for FIX-12 — JWT audience validation must be fail-closed.

`codekb.api.auth` reads its configuration from the environment at import time,
so each case reloads the module under a patched environment and restores the
original module state afterwards.

The control under test: when Cognito auth is active but
CODEKB_COGNITO_APP_CLIENT_IDS is unset, the service must refuse to serve
protected traffic rather than run with audience validation silently disabled.
A token signed by the user pool but issued for a different app client would
otherwise be accepted.
"""

import importlib
import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest

POOL_ID = "us-east-1_TestPool"
CLIENT_IDS = "client-aaa,client-bbb"

# Every auth-relevant variable is cleared before applying a case's overrides so
# a developer's real environment cannot influence the outcome.
_AUTH_ENV_VARS = (
    "CODEKB_REQUIRE_AUTHENTICATION",
    "CODEKB_REQUIRE_AUTH",
    "CODEKB_COGNITO_USER_POOL_ID",
    "CODEKB_COGNITO_APP_CLIENT_IDS",
    "CODEKB_COGNITO_REGION",
    "CODEKB_COGNITO_DOMAIN_URL",
    "CODEKB_COGNITO_MCP_CLIENT_ID",
)


def _reload_config_from_env():
    """Rebuild the config singleton from the current os.environ.

    ``codekb.api.auth`` now derives its require-authentication flag from
    ``get_config``, which caches env resolution. Resetting the singleton and
    reloading makes the reloaded auth module see this case's environment,
    mirroring a fresh process start.
    """
    import src.core.config as config_module

    config_module._config_manager = None
    config_module.get_config_manager().load_configuration()


@contextmanager
def auth_module(**env):
    """Reload codekb.api.auth with the given environment, then restore it."""
    import src.api.auth as auth_module_ref

    clean = {var: None for var in _AUTH_ENV_VARS}
    clean.update(env)
    # patch.dict can't delete keys, so filter Nones out and remove them by hand.
    to_set = {k: v for k, v in clean.items() if v is not None}
    to_clear = [k for k, v in clean.items() if v is None]

    with patch.dict(os.environ, to_set, clear=False):
        for var in to_clear:
            os.environ.pop(var, None)
        try:
            _reload_config_from_env()
            yield importlib.reload(auth_module_ref)
        finally:
            # Restore config and the module to whatever the ambient environment
            # implies so later tests (and the already-imported app module) aren't
            # left holding state from a case above.
            _reload_config_from_env()
            importlib.reload(auth_module_ref)


@pytest.fixture(autouse=True)
def _require_jwt():
    """Skip the whole module if PyJWT isn't installed in this environment."""
    pytest.importorskip("jwt")


def test_missing_client_ids_fails_closed_when_auth_required():
    """Pool configured, client IDs absent, auth required → misconfigured."""
    with auth_module(CODEKB_COGNITO_USER_POOL_ID=POOL_ID) as auth:
        assert auth.is_auth_enforced() is True, "JWKS client should still initialize"
        assert auth.is_auth_misconfigured() is True, (
            "missing CODEKB_COGNITO_APP_CLIENT_IDS must fail closed, not warn"
        )


def test_missing_client_ids_fails_closed_when_require_auth_defaulted():
    """The default (env var absent entirely) must be fail-closed, not fail-open."""
    with auth_module(
        CODEKB_COGNITO_USER_POOL_ID=POOL_ID,
        # CODEKB_REQUIRE_AUTH deliberately not set — default is "true"
    ) as auth:
        assert auth.is_auth_misconfigured() is True


def test_client_ids_present_is_healthy():
    with auth_module(
        CODEKB_COGNITO_USER_POOL_ID=POOL_ID,
        CODEKB_COGNITO_APP_CLIENT_IDS=CLIENT_IDS,
    ) as auth:
        assert auth.is_auth_enforced() is True
        assert auth.is_auth_misconfigured() is False
        assert auth._COGNITO_APP_CLIENT_IDS == ["client-aaa", "client-bbb"]


def test_local_dev_escape_hatch_still_starts():
    """CODEKB_REQUIRE_AUTH=false keeps the unset-client-IDs case serviceable."""
    with auth_module(
        CODEKB_COGNITO_USER_POOL_ID=POOL_ID,
        CODEKB_REQUIRE_AUTH="false",
    ) as auth:
        assert auth.is_auth_enforced() is True, "signatures are still verified"
        assert auth.is_auth_misconfigured() is False, "explicit opt-out must not block"


def test_missing_pool_id_still_fails_closed():
    """Pre-existing behaviour must be unchanged by this fix."""
    with auth_module() as auth:
        assert auth.is_auth_enforced() is False
        assert auth.is_auth_misconfigured() is True


def test_no_auth_mode_still_permitted():
    """Pre-existing behaviour: no pool + explicit opt-out is local dev mode."""
    with auth_module(CODEKB_REQUIRE_AUTH="false") as auth:
        assert auth.is_auth_enforced() is False
        assert auth.is_auth_misconfigured() is False


def test_blank_and_comma_only_client_ids_are_treated_as_unset():
    """Whitespace or bare commas must not be mistaken for a configured list."""
    for value in (" ", ",", " , , "):
        with auth_module(
            CODEKB_COGNITO_USER_POOL_ID=POOL_ID,
            CODEKB_COGNITO_APP_CLIENT_IDS=value,
        ) as auth:
            assert auth._COGNITO_APP_CLIENT_IDS == []
            assert auth.is_auth_misconfigured() is True, (
                f"{value!r} should not satisfy the audience requirement"
            )


def test_verify_token_refuses_when_audience_unvalidatable():
    """Defense in depth: verify_token itself must not accept an unchecked audience.

    Unreachable over HTTP because the middleware returns 503 first, but it
    guarantees no other caller can bypass the control.
    """
    with auth_module(CODEKB_COGNITO_USER_POOL_ID=POOL_ID) as auth:
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        class _SigningKey:
            def __init__(self, k):
                self.key = k

        claims = {"token_use": "access", "username": "someone", "sub": "abc"}

        with (
            patch.object(
                auth._jwks_client,
                "get_signing_key_from_jwt",
                return_value=_SigningKey(key.public_key()),
            ),
            patch("jwt.decode", return_value=claims),
        ):
            assert auth.verify_token("header.payload.signature") is None
