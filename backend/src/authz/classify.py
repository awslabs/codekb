"""Space-kind and visibility helpers for the gate.

Live classification at create is ``service.lookup_repo_visibility``. Here we only
need the zip test and, for legacy spaces with no stored ``visibility``, failing
closed to ``private``.
"""

from __future__ import annotations

from typing import Any


def is_zip_space(record: dict[str, Any]) -> bool:
    """True only for a space positively marked as an uploaded zip."""
    return record.get("source_type") == "upload"


def effective_visibility(record: dict[str, Any]) -> str:
    """``"public"`` or ``"private"`` for a git space.

    A stored ``visibility`` always wins, and every git space created since the
    authz layer landed records one at create (``service.lookup_repo_visibility``)
    — including a private repo cloned with a saved per-user token and a clean
    stored URL. So this fallback runs only for LEGACY, pre-authz spaces that have
    no ``visibility`` recorded at all.

    For that legacy set there is no reliable public/private signal to recover.
    The stored URL is normally clean regardless of visibility (URL-embedded
    credentials are rejected at create and real tokens live in the gitcreds
    store), so a clean URL does not mark a repo as public. With neither a stored
    value nor a trustworthy URL signal, we fail closed to ``private``.

    This also makes every transport consistent: ssh / scp-style already defaulted
    to ``private`` here, and http(s) now matches, closing the path where a clean
    http(s) URL with no stored visibility was read as ``public`` and a private
    repository leaked to every signed-in caller. A url-less record (an uploaded
    zip, a failed clone, a cleared field) likewise never derives as public —
    uploaded zips are owner-only in the read gate's zip branch, not here.

    This keeps every transport consistent. The ssh / scp-style branch already
    defaulted to ``private`` here; http(s) now does the same, closing the path
    where a clean http(s) URL with no stored visibility was read as ``public``
    and a private repository leaked to every signed-in caller. A url-less record
    (an uploaded zip, a failed clone, a cleared field) likewise never derives as
    public — uploaded zips are owner-only in the read gate's zip branch, not
    here.
    """
    stored = record.get("visibility")
    if stored:
        return stored
    return "private"
