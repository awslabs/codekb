"""authz.repo_access gate — the heart of the feature (plan §2.1, §6.1.2)."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from src.authz import repo_access
from src.authz.cache import get_cache
from src.authz.context import AuthContext
from src.authz.errors import (
    AuthorizationUnavailable,
    CredentialExpired,
    ReadDenied,
    WriteDenied,
)

PRIVATE = {"git_url": "https://github.com/o/r.git", "visibility": "private"}
PUBLIC = {"git_url": "https://github.com/o/r.git", "visibility": "public"}
PUBLIC_OWNED = {**PUBLIC, "created_by": "u@x.com"}
ZIP = {"source_type": "upload"}
ZIP_OWNED = {"source_type": "upload", "created_by": "u@x.com"}
MISSING_VIS_CLEAN = {"git_url": "https://github.com/o/r.git"}  # no visibility → private (fail closed)
MISSING_VIS_USERINFO = {"git_url": "https://tok@github.com/o/r.git"}  # no visibility, token → private
UNATTRIBUTABLE = {"git_url": "https://example.com/o/r.git", "visibility": "private"}

READ_PERM = {"pull": True}
MAINTAIN_PERM = {"maintain": True, "push": True, "pull": True}
WRITE_PERM = {"push": True, "pull": True}


class FakeStore:
    def __init__(self, token="tok", invalid=False, host=None):
        self._token = token
        self._invalid = invalid
        self._host = host  # the host the token was saved under (None = cloud)
        self.state_calls = 0  # get_credential_state reads (one per cache miss)
        self.marked = False

    def get_credential_state(self, sub, provider):
        self.state_calls += 1
        if self._invalid:
            return True, self._host, None
        return False, self._host, self._token

    def mark_invalid(self, sub, provider):
        self.marked = True
        return True


def _ctx():
    return AuthContext(sub="u1", email="u@x.com")


@pytest.fixture(autouse=True)
def _clear_cache():
    get_cache().clear()
    yield
    get_cache().clear()


_CLOUD_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "bitbucket.org": "bitbucket"}


@contextmanager
def gate(
    listed=None,
    store=None,
    on=True,
    kms=True,
    providers=("github", "gitlab", "bitbucket"),
):
    """Patch the gate's seams: switch, KMS, provider-for-host, store, listing.

    ``provider_for_host`` is modeled to return a provider only for a cloud host of
    an enabled provider — so an unconfigured/self-hosted host (or no providers)
    reads as None, matching the real registry.
    """
    store = store or FakeStore()
    list_mock = MagicMock(return_value=dict(listed or {}))

    def _pfh(host):
        pid = _CLOUD_HOSTS.get((host or "").lower())
        return object() if (pid and pid in providers) else None

    with (
        patch("src.authz.repo_access.require_authorization", return_value=on),
        patch("src.authz.repo_access._kms_configured", return_value=kms),
        patch("src.authz.repo_access.provider_for_host", side_effect=_pfh),
        patch("src.authz.repo_access.GitCredentialStore", return_value=store),
        patch("src.authz.repo_access.list_accessible_repos", list_mock),
    ):
        yield store, list_mock


# ── switch off ───────────────────────────────────────────────────────────────


def test_switch_off_is_a_noop():
    with gate(on=False) as (store, list_mock):
        repo_access.can_read(_ctx(), PRIVATE)
        repo_access.can_write(_ctx(), PRIVATE)
        assert store.state_calls == 0
        list_mock.assert_not_called()


# ── public & zip need no provider call to read ─────────────────────────────


def test_public_read_allows_without_provider_call():
    with gate() as (store, list_mock):
        repo_access.can_read(_ctx(), PUBLIC)
        list_mock.assert_not_called()


def test_zip_read_is_owner_only():
    # An uploaded zip is visible only to its uploader. No provider call either way.
    with gate() as (store, list_mock):
        repo_access.can_read(_ctx(), ZIP_OWNED)  # ctx is the uploader → allowed
        list_mock.assert_not_called()
    with gate() as (store, list_mock):
        with pytest.raises(ReadDenied):
            repo_access.can_read(_ctx(), ZIP)  # not the uploader → denied
        list_mock.assert_not_called()


# ── private read ─────────────────────────────────────────────────────────────


def test_private_read_allowed_with_read_level():
    with gate(listed={"github.com/o/r": READ_PERM}):
        repo_access.can_read(_ctx(), PRIVATE)


def test_private_read_denied_without_access():
    with gate(listed={}):
        with pytest.raises(ReadDenied):
            repo_access.can_read(_ctx(), PRIVATE)


def test_missing_visibility_clean_url_reads_as_private():
    # Legacy space, no stored visibility → private (fail closed), so a clean URL
    # now needs git read access instead of an open allow: denied without it,
    # allowed with it — same as any private space.
    with gate(listed={}):
        with pytest.raises(ReadDenied):
            repo_access.can_read(_ctx(), MISSING_VIS_CLEAN)
    get_cache().clear()
    with gate(listed={"github.com/o/r": READ_PERM}):
        repo_access.can_read(_ctx(), MISSING_VIS_CLEAN)  # with access → allowed


def test_missing_visibility_with_userinfo_reads_as_private():
    # No stored visibility but the URL carried a token → private → needs git access.
    with gate(listed={}):
        with pytest.raises(ReadDenied):
            repo_access.can_read(_ctx(), MISSING_VIS_USERINFO)
    get_cache().clear()
    with gate(listed={"github.com/o/r": READ_PERM}):
        repo_access.can_read(_ctx(), MISSING_VIS_USERINFO)  # with access → allowed


def test_unattributable_denied_without_provider_call():
    with gate(listed={"github.com/o/r": MAINTAIN_PERM}) as (store, list_mock):
        with pytest.raises(ReadDenied):
            repo_access.can_read(_ctx(), UNATTRIBUTABLE)
        list_mock.assert_not_called()


def test_url_less_non_upload_record_fails_closed_read():
    # A record with no git_url that is NOT a positively-marked upload (a failed
    # clone, a cleared field, a pre-authz record) must not earn the zip early
    # allow nor read as public — it falls through to unattributable → denied.
    for rec in (
        {"space_id": "s"},
        {"space_id": "s", "git_url": ""},
        {"space_id": "s", "git_url": None},
        {"space_id": "s", "source_type": "git"},
    ):
        get_cache().clear()
        with gate(listed={}) as (store, list_mock):
            with pytest.raises(ReadDenied):
                repo_access.can_read(_ctx(), rec)
            list_mock.assert_not_called()


def test_unresolved_legacy_space_fails_closed():
    # A legacy space with no status.json (authz_unresolved) must deny instead of
    # the no-git_url heuristic reading it as a readable zip.
    rec = {"space_id": "legacy", "authz_unresolved": True}
    with gate(listed={}) as (store, list_mock):
        with pytest.raises(ReadDenied):
            repo_access.can_read(_ctx(), rec)
        list_mock.assert_not_called()


# ── write ────────────────────────────────────────────────────────────────────


def test_private_write_needs_maintainer_tier():
    with gate(listed={"github.com/o/r": MAINTAIN_PERM}):
        repo_access.can_write(_ctx(), PRIVATE)
    get_cache().clear()
    with gate(listed={"github.com/o/r": WRITE_PERM}):
        with pytest.raises(WriteDenied):
            repo_access.can_write(_ctx(), PRIVATE)


def test_public_creator_writes_without_provider_call():
    with gate(listed={}) as (store, list_mock):
        repo_access.can_write(_ctx(), PUBLIC_OWNED)
        list_mock.assert_not_called()


def test_public_non_creator_maintainer_may_write():
    with gate(listed={"github.com/o/r": MAINTAIN_PERM}):
        repo_access.can_write(_ctx(), PUBLIC)


def test_public_non_creator_writer_is_denied():
    with gate(listed={"github.com/o/r": WRITE_PERM}):
        with pytest.raises(WriteDenied):
            repo_access.can_write(_ctx(), PUBLIC)


def test_zip_write_owner_vs_other():
    with gate():
        repo_access.can_write(_ctx(), ZIP_OWNED)  # uploader → ok
    with gate():
        # A non-uploader can't even read the zip, so write fails at the READ
        # check with ReadDenied (401) before reaching the write owner check.
        with pytest.raises(ReadDenied):
            repo_access.can_write(_ctx(), ZIP)


# ── credential / provider failures (plan §6.1.2) ───────────────────────────


def test_flagged_token_raises_expired_with_zero_provider_calls():
    with gate(store=FakeStore(invalid=True)) as (store, list_mock):
        with pytest.raises(CredentialExpired):
            repo_access.can_read(_ctx(), PRIVATE)
        list_mock.assert_not_called()


def test_provider_401_marks_invalid_and_raises_expired_not_cached():
    from src.gitcreds.providers import TokenInvalid

    store = FakeStore()
    list_mock = MagicMock(side_effect=TokenInvalid("bad"))
    with (
        patch("src.authz.repo_access.require_authorization", return_value=True),
        patch("src.authz.repo_access._kms_configured", return_value=True),
        patch("src.authz.repo_access.GitCredentialStore", return_value=store),
        patch("src.authz.repo_access.list_accessible_repos", list_mock),
    ):
        with pytest.raises(CredentialExpired):
            repo_access.can_read(_ctx(), PRIVATE)
        assert store.marked is True
    assert len(get_cache()) == 0  # nothing cached


def test_provider_error_raises_unavailable_not_cached():
    from src.gitcreds.providers import ProviderError

    list_mock = MagicMock(side_effect=ProviderError("down"))
    with (
        patch("src.authz.repo_access.require_authorization", return_value=True),
        patch("src.authz.repo_access._kms_configured", return_value=True),
        patch("src.authz.repo_access.GitCredentialStore", return_value=FakeStore()),
        patch("src.authz.repo_access.list_accessible_repos", list_mock),
    ):
        with pytest.raises(AuthorizationUnavailable):
            repo_access.can_read(_ctx(), PRIVATE)
    assert len(get_cache()) == 0


def test_listing_unsupported_denies_not_503_and_is_negatively_cached():
    # Can't-list host (Bitbucket Server/DC): a private repo is a flat denial
    # (ReadDenied), not a 503, and negatively cached (not re-probed per space).
    from src.gitcreds.providers import ProviderListingUnsupported

    list_mock = MagicMock(side_effect=ProviderListingUnsupported("bb server/dc"))
    with (
        patch("src.authz.repo_access.require_authorization", return_value=True),
        patch("src.authz.repo_access._kms_configured", return_value=True),
        patch("src.authz.repo_access.GitCredentialStore", return_value=FakeStore()),
        patch("src.authz.repo_access.list_accessible_repos", list_mock),
    ):
        ctx = _ctx()
        with pytest.raises(ReadDenied):
            repo_access.can_read(ctx, PRIVATE)
        with pytest.raises(ReadDenied):
            repo_access.can_read(ctx, PRIVATE)  # served from the negative cache
        assert list_mock.call_count == 1  # not re-attempted
    assert len(get_cache()) == 1


def test_no_kms_allows_public_denies_private_without_provider_call():
    # KMS unset: only public spaces are reachable; private is denied, not 503.
    with gate(kms=False) as (store, list_mock):
        repo_access.can_read(_ctx(), PUBLIC)  # public still fine
        with pytest.raises(ReadDenied):
            repo_access.can_read(_ctx(), PRIVATE)
        list_mock.assert_not_called()


def test_no_providers_enabled_is_public_only():
    # No git providers enabled: public reachable, private denied, no provider call.
    with gate(providers=()) as (store, list_mock):
        repo_access.can_read(_ctx(), PUBLIC)
        with pytest.raises(ReadDenied):
            repo_access.can_read(_ctx(), PRIVATE)
        list_mock.assert_not_called()


def test_unconfigured_self_hosted_host_is_denied_not_503():
    # A space on a self-hosted host with no CUSTOM_HOST configured is
    # unattributable → denied (no provider call, no 503).
    rec = {
        "git_url": "https://git.internal.corp/o/r.git",
        "provider": "github",
        "visibility": "private",
    }
    with gate() as (store, list_mock):
        with pytest.raises(ReadDenied):
            repo_access.can_read(_ctx(), rec)
        list_mock.assert_not_called()


def test_token_saved_for_a_different_host_is_not_used():
    # Token saved for a self-hosted host must not be sent to a github.com repo.
    # It's treated as no token (denied), and the valid token is left untouched.
    with gate(store=FakeStore(host="github.example.com")) as (store, list_mock):
        with pytest.raises(ReadDenied):
            repo_access.can_read(_ctx(), PRIVATE)  # PRIVATE is on github.com
        list_mock.assert_not_called()
        assert store.marked is False


def test_no_token_is_no_access_and_negatively_cached():
    # No usable token → denied, and the empty result is negatively cached so a
    # second private-space read on the same host is served from cache, not the store.
    with gate(store=FakeStore(token=None), listed={}) as (store, list_mock):
        ctx = _ctx()
        with pytest.raises(ReadDenied):
            repo_access.can_read(ctx, PRIVATE)
        with pytest.raises(ReadDenied):
            repo_access.can_read(ctx, PRIVATE)
        list_mock.assert_not_called()
        assert store.state_calls == 1  # second read hit the negative cache
    assert len(get_cache()) == 1


# ── cache behaviour (plan §6.1.2) ───────────────────────────────────────────


def test_second_read_is_a_cache_hit_with_one_listing_and_no_store_reads():
    store = FakeStore()
    list_mock = MagicMock(return_value={"github.com/o/r": READ_PERM})
    with (
        patch("src.authz.repo_access.require_authorization", return_value=True),
        patch("src.authz.repo_access._kms_configured", return_value=True),
        patch("src.authz.repo_access.GitCredentialStore", return_value=store),
        patch("src.authz.repo_access.list_accessible_repos", list_mock),
    ):
        ctx = _ctx()
        repo_access.can_read(ctx, PRIVATE)  # miss → lists, reads store
        repo_access.can_read(ctx, PRIVATE)  # hit → no listing, no store read
        assert list_mock.call_count == 1
        assert store.state_calls == 1  # single store read, only on the miss
