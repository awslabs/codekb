"""Tests for the startup sweep of leftover source-cache scratch.

The sweep runs once from the FastAPI lifespan and removes only entries that are
scratch *by construction*: pipeline working trees, staging archives, zip-flatten
temporaries, and trees that ``discard_directory`` detached but never finished
deleting. It deliberately does **not** touch the per-space ``<space_id>`` trees,
which are a legitimate warm cache bounded by eviction rather than by the sweep.

It exists because a container killed mid-analysis leaves scratch behind that
nothing else will ever reclaim. Eviction refuses to touch working trees by
design, and the space is left in a non-terminal state, so ``refresh_space``
(which requires READY) will not run the pipeline again to trigger its pre-clean.
Without the sweep those orphans persist for the life of the volume.

Deleting without an in-use check is safe purely because of *when* it runs: the
lifespan executes before the first request is served, and pipeline threads are
only ever spawned by requests, so this process has nothing in flight.

The load-bearing assumption is that a real space id can never look like scratch,
since classification is by name alone. That is checked here against adversarial
names rather than assumed.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.file_utils import DISCARDING_PREFIX, discard_directory
from src.service.codekb_service import generate_space_id_from_name
from src.service.source_provider import (
    WORK_DIR_ANALYSIS,
    WORK_DIR_REFRESH,
    is_scratch_entry,
    sweep_source_cache_scratch,
)


def _populated(path: Path) -> Path:
    """A directory with contents, so a no-op sweep is distinguishable."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "file.py").write_text("x = 1\n")
    return path


class TestSweepRemovesScratch:
    def test_removes_a_leftover_refresh_clone(self, tmp_path):
        leftover = _populated(tmp_path / f"left-over-11111111-{WORK_DIR_REFRESH}")

        assert sweep_source_cache_scratch(tmp_path) == 1

        assert not leftover.exists()

    def test_removes_a_leftover_analysis_working_tree(self, tmp_path):
        leftover = _populated(tmp_path / f"left-over-11111111-{WORK_DIR_ANALYSIS}")

        assert sweep_source_cache_scratch(tmp_path) == 1

        assert not leftover.exists()

    def test_removes_staging_archives(self, tmp_path):
        tarball = tmp_path / "space-a-11111111-source.tar.gz"
        upload = tmp_path / "space-b-22222222-upload.zip"
        tarball.write_bytes(b"tar")
        upload.write_bytes(b"zip")

        assert sweep_source_cache_scratch(tmp_path) == 2

        # Files, not directories: the sweep has to handle both kinds of entry.
        assert not tarball.exists()
        assert not upload.exists()

    def test_removes_a_zip_flatten_temporary(self, tmp_path):
        flatten_tmp = _populated(tmp_path / "space-a-11111111__tmp")

        assert sweep_source_cache_scratch(tmp_path) == 1

        assert not flatten_tmp.exists()

    def test_removes_a_directory_left_detached_by_discard_directory(self, tmp_path, monkeypatch):
        import shutil

        victim = _populated(tmp_path / "space-a-11111111")
        # Detach succeeds, delete does not — the process died in between.
        monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)
        discard_directory(victim)
        monkeypatch.undo()

        staged = list(tmp_path.glob(f"{DISCARDING_PREFIX}*"))
        assert len(staged) == 1

        assert sweep_source_cache_scratch(tmp_path) == 1

        assert list(tmp_path.glob(f"{DISCARDING_PREFIX}*")) == []

    def test_counts_everything_it_reclaimed(self, tmp_path):
        _populated(tmp_path / f"a-11111111-{WORK_DIR_REFRESH}")
        _populated(tmp_path / f"b-22222222-{WORK_DIR_ANALYSIS}")
        _populated(tmp_path / "c-33333333__tmp")
        (tmp_path / "d-44444444-source.tar.gz").write_bytes(b"tar")
        (tmp_path / "e-55555555-upload.zip").write_bytes(b"zip")
        live = _populated(tmp_path / "live-space-66666666")

        # The count is what the log line reports, so operators reading it to
        # judge whether a restart reclaimed anything depend on it being real.
        assert sweep_source_cache_scratch(tmp_path) == 5

        assert live.exists()


class TestSweepLeavesLiveDataAlone:
    def test_a_space_tree_is_not_scratch(self, tmp_path):
        live = _populated(tmp_path / "live-space-22222222")

        assert sweep_source_cache_scratch(tmp_path) == 0

        # Cache entries are bounded by eviction, which understands recency and
        # disk pressure. A sweep that took them would throw away every warm tree
        # on each deploy and push all readers back to S3.
        assert (live / "file.py").is_file()

    def test_space_names_that_merely_mention_refresh_are_left_alone(self, tmp_path):
        # Substring matching would eat all of these.
        decoys = [
            _populated(tmp_path / "my-refresher-a1b2c3d4"),
            _populated(tmp_path / "refresh-tool-a1b2c3d4"),
            _populated(tmp_path / "refresh-a1b2c3d4"),
            _populated(tmp_path / "analysis-helper-a1b2c3d4"),
            _populated(tmp_path / "tmp-thing-a1b2c3d4"),
        ]

        assert sweep_source_cache_scratch(tmp_path) == 0

        for decoy in decoys:
            assert decoy.exists()

    def test_no_generated_space_id_can_match_a_scratch_glob(self):
        """The property the sweep's safety rests on, checked rather than assumed."""
        adversarial = [
            "my repo refresh",
            "my refresher",
            "refresh tool",
            "my repo analysis",
            "analysis",
            "thing__tmp",
            "archive.tar.gz",
            "upload.zip",
            ".discarding-x",
            "  Weird Name!!  ",
        ]
        for name in adversarial:
            space_id = generate_space_id_from_name(name)
            assert not is_scratch_entry(space_id), space_id

    def test_a_missing_cache_root_is_not_an_error(self, tmp_path):
        # First boot on a fresh volume: /tmp/codekb does not exist yet.
        assert sweep_source_cache_scratch(tmp_path / "not-created-yet") == 0

    def test_an_empty_cache_root_reclaims_nothing(self, tmp_path):
        assert sweep_source_cache_scratch(tmp_path) == 0

    def test_an_unremovable_entry_does_not_stop_the_sweep(self, tmp_path, monkeypatch):
        (tmp_path / "a-11111111-source.tar.gz").write_bytes(b"tar")
        good = _populated(tmp_path / f"b-22222222-{WORK_DIR_REFRESH}")

        real_unlink = Path.unlink

        def stubborn_unlink(self, *args, **kwargs):
            if self.name.endswith("-source.tar.gz"):
                raise OSError(13, "Permission denied")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", stubborn_unlink)

        # One bad entry must not abort startup cleanup for the rest.
        assert sweep_source_cache_scratch(tmp_path) == 1
        assert not good.exists()


class TestTheLifespanRunsTheSweep:
    """Wiring, not policy: the sweep is worthless if nothing calls it.

    The MCP session manager is stubbed rather than driving the app through
    TestClient, because a lifespan can only be entered once and
    ``test_api_smoke.py`` already spends that on its own client. Stubbing keeps
    these tests about the sweep rather than about MCP.
    """

    @pytest.mark.anyio
    async def test_booting_reclaims_scratch_and_keeps_live_spaces(self, tmp_path):
        from src.api import app as app_module

        leftover = _populated(tmp_path / f"a-11111111-{WORK_DIR_REFRESH}")
        live = _populated(tmp_path / "live-space-22222222")

        session_manager = MagicMock()
        session_manager.run = _null_async_context

        with (
            patch.object(app_module, "get_mcp_session_manager", lambda: session_manager),
            patch.object(app_module, "sweep_source_cache_scratch", lambda: _sweep(tmp_path)),
        ):
            async with app_module.lifespan(MagicMock()):
                pass

        assert not leftover.exists()
        assert live.exists()

    @pytest.mark.anyio
    async def test_a_failing_sweep_does_not_stop_startup(self, tmp_path):
        from src.api import app as app_module

        session_manager = MagicMock()
        session_manager.run = _null_async_context
        started = {"mcp": False}

        def explode():
            raise OSError("volume is on fire")

        def note_started():
            started["mcp"] = True
            return session_manager

        with (
            patch.object(app_module, "sweep_source_cache_scratch", explode),
            patch.object(app_module, "get_mcp_session_manager", note_started),
        ):
            # A tidy-up failure must never keep the API from serving traffic.
            async with app_module.lifespan(MagicMock()):
                pass

        assert started["mcp"] is True


def _sweep(root: Path) -> int:
    """Call the real sweep against a test root, ignoring the caller's default."""
    return sweep_source_cache_scratch(root)


class _NullAsyncContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc_info):
        return False


def _null_async_context():
    return _NullAsyncContext()
