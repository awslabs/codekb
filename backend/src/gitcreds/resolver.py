"""Resolve which saved credential to use for a repo.

One place that maps a repo URL + caller to the token to send, so callers (the
clone path and the visibility check) don't re-implement provider lookup, the
enabled-check, and host matching.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from .providers import (
    GitProvider,
    enabled_provider_ids,
    get_provider,
    provider_by_id,
    saved_token_matches_repo,
)
from .store import GitCredentialStore


def resolve_repo_credential(
    sub: Optional[str], git_url: str
) -> tuple[Optional[GitProvider], Optional[str]]:
    """The provider and saved token to use for ``git_url`` as ``sub``.

    ``token`` is ``None`` when there is no usable credential: no identity, no
    provider, the provider is not enabled, nothing saved, or the saved credential
    is for a different host than the repo. ``StorageError`` from decryption
    propagates to the caller.
    """
    host = (urlparse(git_url or "").hostname or "").lower()
    if not host or not sub:
        return None, None

    store = GitCredentialStore()
    enabled = set(enabled_provider_ids())

    # The repo's cloud host or configured custom host.
    pid = get_provider(git_url)
    if pid and pid in enabled:
        provider = provider_by_id(pid)
        saved_host, token = store.get_credential(sub, provider.id)  # one read
        if saved_token_matches_repo(provider.id, saved_host, git_url):
            return provider, token
        return provider, None

    # Fallback: a custom host the caller saved a token under that the server's
    # custom-host config doesn't name.
    for p, stored in store.hosts_by_provider(sub).items():
        if p in enabled and saved_token_matches_repo(p, stored, git_url):
            _, token = store.get_credential(sub, p)
            return provider_by_id(p), token
    return None, None
