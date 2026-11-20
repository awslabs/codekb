"""authz.cache (per-container LRU+TTL) and the four authz config keys (plan §4, §6.1.2)."""

from unittest.mock import patch

from src.authz import startup
from src.authz.cache import MISS, RepoAccessCache, build_key


# ── cache ────────────────────────────────────────────────────────────────────


def test_put_then_get_is_a_hit():
    c = RepoAccessCache(max_age_ms=10_000, maxsize=10)
    key = build_key("sub", "github", "github.com")
    c.put(key, {"github.com/o/r": "x"})
    assert c.get(key) == {"github.com/o/r": "x"}


def test_entry_past_max_age_is_a_miss():
    # Negative max-age forces every entry to read as expired.
    c = RepoAccessCache(max_age_ms=-1, maxsize=10)
    key = build_key("sub", "github", "github.com")
    c.put(key, {"x": 1})
    assert c.get(key) is MISS


def test_lru_eviction_at_capacity():
    c = RepoAccessCache(max_age_ms=10_000, maxsize=2)
    a = build_key("s", "github", "ha")
    b = build_key("s", "github", "hb")
    d = build_key("s", "github", "hd")
    c.put(a, 1)
    c.put(b, 2)
    c.get(a)  # touch a so b is now least-recently-used
    c.put(d, 3)
    assert c.get(b) is MISS  # b evicted
    assert c.get(a) == 1
    assert c.get(d) == 3


def test_explicit_sub_one_maxsize_raises():
    # An injected bad capacity is a caller bug — fail loud, don't silently coerce.
    import pytest

    with pytest.raises(ValueError):
        RepoAccessCache(max_age_ms=10_000, maxsize=0)


def test_config_sub_one_maxsize_raises():
    # A bad override fails loud rather than silently disabling the cache; the
    # authoritative default lives in default.yaml, not a Python constant.
    import pytest

    from src.core.logging_utils import ConfigurationError

    c = RepoAccessCache(max_age_ms=10_000)  # capacity read from config
    with patch("src.core.config.get_config", return_value=0):
        with pytest.raises(ConfigurationError):
            c._capacity()


def test_github_com_and_ghe_host_get_separate_entries():
    c = RepoAccessCache(max_age_ms=10_000, maxsize=10)
    cloud = build_key("sub", "github", "github.com")
    ghe = build_key("sub", "github", "ghe.corp")
    c.put(cloud, {"cloud": 1})
    c.put(ghe, {"ghe": 1})
    assert c.get(cloud) == {"cloud": 1}
    assert c.get(ghe) == {"ghe": 1}


def test_invalidate_user_drops_every_host_for_that_provider():
    c = RepoAccessCache(max_age_ms=10_000, maxsize=10)
    c.put(build_key("sub", "github", "github.com"), 1)
    c.put(build_key("sub", "github", "ghe.corp"), 2)
    c.put(build_key("sub", "gitlab", "gitlab.com"), 3)  # other provider, untouched
    c.put(build_key("other", "github", "github.com"), 4)  # other user, untouched
    removed = c.invalidate_user("sub", "github")
    assert removed == 2
    assert c.get(build_key("sub", "github", "github.com")) is MISS
    assert c.get(build_key("sub", "github", "ghe.corp")) is MISS
    assert c.get(build_key("sub", "gitlab", "gitlab.com")) == 3
    assert c.get(build_key("other", "github", "github.com")) == 4


def test_build_key_lowercases_host():
    assert build_key("s", "github", "GitHub.Com") == ("s", "github", "github.com")


def test_ttl_reads_config_hours_and_converts_to_ms():
    # Default config is 4 hours → 14,400,000 ms internally.
    assert RepoAccessCache()._ttl_ms() == 4 * 3_600_000
    with patch("src.core.config.get_config", return_value=2):
        assert RepoAccessCache()._ttl_ms() == 2 * 3_600_000


def test_ttl_and_capacity_resolved_once_not_per_access():
    # Memoized: repeated get/put don't re-read config (one resolution each).
    c = RepoAccessCache()
    key = build_key("s", "github", "github.com")
    with patch(
        "src.authz.cache.get_positive_int",
        side_effect=lambda k: 4 if "ttl" in k else 2000,
    ) as gp:
        c.put(key, {"x": 1})
        c.get(key)
        c.get(key)
        c.put(key, {"y": 2})
    assert gp.call_count == 2  # not once-per-access


# ── config ─────────────────────────────────────────────────────────────────


def test_defaults_load_from_yaml():
    # conftest loads default.yaml; the shipped defaults match plan §4.
    from src.core.config import get_config

    assert get_config("authz.require_authorization", "sentinel") is True
    assert int(get_config("authz.repo_cache_ttl_hours")) == 4
    assert int(get_config("authz.repo_cache_maxsize")) == 2000


def test_require_authorization_reads_bool():
    with patch("src.authz.startup.get_config", return_value=True):
        assert startup.require_authorization() is True
    with patch("src.authz.startup.get_config", return_value=False):
        assert startup.require_authorization() is False
    with patch("src.authz.startup.get_config", return_value="true"):
        assert startup.require_authorization() is True
    with patch("src.authz.startup.get_config", return_value="off"):
        assert startup.require_authorization() is False


# ── startup validation ───────────────────────────────────────────────────


from contextlib import ExitStack  # noqa: E402


def _startup_env(authz=True, auth=True, providers=("github",), kms="arn"):
    cfg = {"gitcreds.kms_key_arn": kms}
    stack = ExitStack()
    stack.enter_context(patch("src.authz.startup.require_authorization", return_value=authz))
    stack.enter_context(patch("src.authz.startup._auth_required", return_value=auth))
    stack.enter_context(
        patch("src.authz.startup.enabled_provider_ids", return_value=list(providers))
    )
    stack.enter_context(
        patch("src.authz.startup.get_config", side_effect=lambda k, d=None: cfg.get(k, d))
    )
    return stack


def test_startup_noop_when_switch_off():
    with _startup_env(authz=False):
        startup.validate_authz_startup()  # no raise


def test_startup_allows_authz_on_with_auth_off():
    # Auth off → single local-dev identity. Allowed (warns) for local testing.
    with _startup_env(auth=False):
        startup.validate_authz_startup()  # no raise


def test_startup_allows_no_providers_public_internal_only():
    # Public/internal-only deployment: no providers needed, warn not refuse.
    with _startup_env(providers=()):
        startup.validate_authz_startup()  # no raise


def test_startup_allows_no_kms_public_internal_only():
    with _startup_env(kms=""):
        startup.validate_authz_startup()  # no raise


def test_startup_ok_when_fully_configured():
    with _startup_env():
        startup.validate_authz_startup()  # no raise


def test_startup_fails_fast_on_bad_cache_config():
    # A bad authz.repo_cache_* override fails at startup, not as a later 500.
    import pytest

    from src.core.logging_utils import ConfigurationError

    def bad(k, d=None):
        if k == "authz.repo_cache_ttl_hours":
            return 0  # invalid (< 1)
        if k == "authz.repo_cache_maxsize":
            return 2000
        return d

    with _startup_env():
        # get_positive_int resolves these via core.config.get_config.
        with patch("src.core.config.get_config", side_effect=bad):
            with pytest.raises(ConfigurationError):
                startup.validate_authz_startup()
