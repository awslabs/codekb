"""Turn a git URL into a stable ``host/path`` key for permission matching.

Both the space's URL and the provider's repo listing must produce the same key.
Don't change the normalization without updating both sides.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse


def parse_repo(git_url: Optional[str]) -> Optional[tuple[str, str]]:
    """``(host, path)`` for an https/ssh URL, else ``None``.

    Host is lowercased; path drops userinfo, port, surrounding slashes and ``.git``.
    """
    raw = (git_url or "").strip()
    if not raw:
        return None

    # scp-style SSH: git@host:owner/repo(.git)
    if "://" not in raw and "@" in raw and ":" in raw.split("@", 1)[1]:
        after = raw.split("@", 1)[1]
        host, _, path = after.partition(":")
    else:
        parsed = urlparse(raw)
        if parsed.scheme.lower() not in ("https", "http", "ssh"):
            return None
        host = parsed.hostname or ""
        path = parsed.path

    host = host.strip().lower()
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")
    if not host or not path:
        return None
    return host, path


def repo_key(git_url: Optional[str]) -> Optional[str]:
    """Stable ``host/path`` key, lowercased, or ``None`` if unparseable."""
    parsed = parse_repo(git_url)
    if parsed is None:
        return None
    host, path = parsed
    return f"{host}/{path.lower()}"
