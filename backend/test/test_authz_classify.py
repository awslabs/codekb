"""authz.classify: zip detection and visibility (fail-closed when not stored)."""

from src.authz.classify import effective_visibility, is_zip_space


def test_is_zip_space_requires_positive_upload_marker():
    # Only the source_type=="upload" marker counts as a zip. An absent/empty
    # git_url is NOT proof of a zip (could be a failed clone or cleared field).
    assert is_zip_space({"source_type": "upload"}) is True
    assert is_zip_space({"source_type": "upload", "git_url": ""}) is True
    assert is_zip_space({}) is False
    assert is_zip_space({"git_url": None}) is False
    assert is_zip_space({"git_url": ""}) is False
    assert is_zip_space({"source_type": "git"}) is False
    assert is_zip_space({"git_url": "https://github.com/o/r.git"}) is False


def test_stored_visibility_always_wins():
    assert (
        effective_visibility({"git_url": "https://github.com/o/r.git", "visibility": "private"})
        == "private"
    )
    # Even a clean URL stays private if that's what's stored.
    assert (
        effective_visibility({"git_url": "https://github.com/o/r.git", "visibility": "public"})
        == "public"
    )


def test_missing_visibility_fails_closed_to_private():
    # No stored visibility (a legacy, pre-authz space) → private, regardless of
    # the URL. A clean http(s) URL carries no proof of public: the repo may be
    # private with its token held elsewhere, so we must not read it as public.
    assert effective_visibility({"git_url": "https://github.com/org/repo.git"}) == "private"
    # userinfo (user:token, lone token, user-only) was already private; still is.
    assert effective_visibility({"git_url": "https://u:tok@github.com/o/r.git"}) == "private"
    assert effective_visibility({"git_url": "https://ghp_tok@github.com/o/r.git"}) == "private"
    assert effective_visibility({"git_url": "https://user@github.com/o/r.git"}) == "private"


def test_url_less_space_is_private():
    # No git source → private, for every url-less record. An uploaded zip is
    # owner-only (enforced by the read gate's zip branch, not here); a failed
    # clone or cleared field fails closed. Neither derives as public.
    assert effective_visibility({"source_type": "upload"}) == "private"
    assert effective_visibility({"source_type": "upload", "git_url": ""}) == "private"
    assert effective_visibility({"git_url": ""}) == "private"
    assert effective_visibility({}) == "private"
    assert effective_visibility({"source_type": "git", "git_url": ""}) == "private"


def test_all_transports_fail_closed_consistently():
    # ssh/scp-style already defaulted to private; http(s) now matches, so the
    # fail-closed stance no longer differs by transport (the finding's defect).
    for url in (
        "ssh://git@github.com/o/r.git",
        "git@github.com:o/r.git",
        "http://github.com/o/r.git",
        "https://github.com/o/r.git",
    ):
        assert effective_visibility({"git_url": url}) == "private"
