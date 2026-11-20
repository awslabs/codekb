"""Tests for discard_directory, the source-cache deletion primitive.

The invariant under test is visibility, not reclamation. ``shutil.rmtree``
unlinks entries one at a time, so a reader that probes a directory mid-delete
finds it present but nearly empty -- and ``ensure_source_local`` treats
exists-and-non-empty as a valid warm cache entry. ``discard_directory`` renames
the tree to a sibling first, so the caller's path is either complete or absent,
and absent is the case readers already handle by re-hydrating from S3.

These tests therefore assert *when* unlinking begins relative to the rename,
not merely that the bytes eventually go away.
"""

import shutil
from pathlib import Path

from src.core.file_utils import discard_directory
from src.service.codekb_service import generate_space_id_from_name

_STAGING_GLOB = ".discarding-*"


def _tree(root: Path, files: int = 3) -> Path:
    """Create a small populated directory tree and return its root."""
    (root / "nested").mkdir(parents=True)
    for i in range(files):
        (root / f"f{i}.py").write_text(f"# file {i}\n")
    (root / "nested" / "deep.py").write_text("# deep\n")
    return root


class TestDiscardDirectory:
    def test_removes_a_populated_tree(self, tmp_path):
        victim = _tree(tmp_path / "space-a")

        assert discard_directory(victim) is True

        assert not victim.exists()
        # No staging leftovers on the happy path.
        assert list(tmp_path.glob(_STAGING_GLOB)) == []

    def test_missing_path_counts_as_discarded(self, tmp_path):
        # Callers invoke this on cleanup paths where the tree may never have
        # existed, so "nothing there" is success rather than an error.
        assert discard_directory(tmp_path / "never-existed") is True

    def test_leaves_sibling_trees_alone(self, tmp_path):
        victim = _tree(tmp_path / "space-a")
        bystander = _tree(tmp_path / "space-b")

        discard_directory(victim)

        assert not victim.exists()
        assert (bystander / "nested" / "deep.py").is_file()


class TestNoPartiallyDeletedTreeIsEverVisible:
    """The reason this helper exists: unlinking must start after the detach."""

    def test_original_path_is_gone_before_any_file_is_unlinked(self, tmp_path, monkeypatch):
        victim = _tree(tmp_path / "space-a")
        observed: dict = {}
        real_rmtree = shutil.rmtree

        def spying_rmtree(path, *args, **kwargs):
            observed["target"] = Path(path)
            observed["victim_still_present"] = victim.exists()
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(shutil, "rmtree", spying_rmtree)

        assert discard_directory(victim) is True

        # If this flipped to True, a concurrent reader could observe `victim`
        # as an existing directory whose files are already being removed.
        assert observed["victim_still_present"] is False
        assert observed["target"] != victim

    def test_staged_copy_is_a_sibling_so_the_rename_stays_on_one_filesystem(
        self, tmp_path, monkeypatch
    ):
        victim = _tree(tmp_path / "space-a")
        # Skip the delete so the staged path is still there to inspect.
        monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)

        discard_directory(victim)

        staged = list(tmp_path.glob(_STAGING_GLOB))
        assert len(staged) == 1
        # Same parent keeps it on the same filesystem; a cross-device rename
        # would fail with EXDEV. The leading dot keeps it out of the way of
        # space ids, so a later sweep can glob for it unambiguously.
        assert staged[0].parent == victim.parent
        assert staged[0].name.startswith(".discarding-space-a-")

    def test_a_staging_name_can_never_collide_with_a_real_space_id(self):
        # The sweep-safety assumption behind the ".discarding-" prefix: generated
        # space ids are slugified to [a-z0-9-], so none can start with a dot.
        for name in ("space-a", ".hidden", "..", "  Weird Name!!  ", ".discarding-x"):
            assert not generate_space_id_from_name(name).startswith(".")


class TestDegradedPaths:
    def test_leftover_staged_copy_still_counts_as_success(self, tmp_path, monkeypatch):
        victim = _tree(tmp_path / "space-a")
        # Detach succeeds, delete does nothing -- e.g. the process dies here.
        monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)

        # The caller's contract is about `victim`, which is gone. The orphaned
        # staged copy holds disk until a sweep, but readers are already safe.
        assert discard_directory(victim) is True
        assert not victim.exists()
        assert len(list(tmp_path.glob(_STAGING_GLOB))) == 1

    def test_falls_back_to_in_place_delete_when_the_rename_fails(self, tmp_path, monkeypatch):
        victim = _tree(tmp_path / "space-a")

        def unrenameable(self, target):
            raise OSError(18, "Invalid cross-device link")

        monkeypatch.setattr(Path, "rename", unrenameable)

        # Atomicity is lost, but an unreclaimed tree is the worse outcome.
        assert discard_directory(victim) is True
        assert not victim.exists()
        assert list(tmp_path.glob(_STAGING_GLOB)) == []

    def test_reports_failure_when_the_tree_cannot_be_removed_at_all(self, tmp_path, monkeypatch):
        victim = _tree(tmp_path / "space-a")

        def unrenameable(self, target):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(Path, "rename", unrenameable)
        monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)

        # Callers that report cleanup errors depend on this being False rather
        # than an exception, since they run after the point of no return.
        assert discard_directory(victim) is False
        assert (victim / "nested" / "deep.py").is_file()
