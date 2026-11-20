"""Tests for the bounded local source cache in ``service/source_provider``.

The cache holds extracted repository trees under one root, mixed in with the
working trees of running pipelines. Eviction has to reclaim the former and never
touch the latter, and it decides which is which purely from the directory name —
there is no lease table and no reference counting. These tests therefore pin two
different kinds of property:

*Safety* — what must never be deleted: a pipeline working tree, a staging
archive, the space currently being written, or a tree touched moments ago by a
reader that is probably still using it.

*Liveness* — that pressure actually reclaims something, oldest first, and that
every failure path degrades to "nothing was reclaimed" rather than raising into
the caller's request.

There is exactly one bound: the extracted cache stays under
``source_cache.max_size_gb``. Tests set it in bytes via ``_provider(max_bytes=...)``,
because the setting is denominated in GiB while these fixtures are a few dozen bytes.
``_ceiling_keeping(...)`` derives a ceiling from the entries a test expects to
survive, so "evict exactly two" is expressed as "leave room for the third".

Two bounds that once lived here were removed, and no test simulates free disk space
any more:

*A "keep N% of the volume free" floor.* On a disk CodeKB does not own, it is
permanently in breach and turns every read into evict-everything-then-re-download.

*A per-download fit check.* Same error in a narrower form — it read free space on a
shared volume and responded by deleting CodeKB's own data. A repository that will
not fit now fails the extraction with a disk-full error, which is a clear result.
"""

import os
import tarfile
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.config import ConfigError
from src.service.codekb_service import generate_space_id_from_name
from src.service.errors import SourceNotAvailableError
from src.service.source_provider import (
    WORK_DIR_ANALYSIS,
    WORK_DIR_REFRESH,
    SourceCodeProvider,
    is_scratch_entry,
)
from src.service import source_provider as sp


def _entry(root: Path, name: str, *, age_seconds: float = 3600, files: int = 2) -> Path:
    """Create a populated directory under ``root`` with a backdated mtime.

    Age matters: the provider refuses to evict anything idle for less than
    ``_MIN_IDLE_SECONDS``, so a test that wants a candidate has to backdate it.
    """
    path = root / name
    path.mkdir(parents=True)
    for i in range(files):
        (path / f"f{i}.py").write_text(f"# file {i}\n")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def _tarball(files: dict[str, str]) -> bytes:
    """Build an in-memory ``.tar.gz`` shaped like an uploaded source archive."""
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=f"./{name}")
            info.size = len(data)
            tar.addfile(info, BytesIO(data))
    return buf.getvalue()


def _kb_manager(archive: bytes | None = None) -> MagicMock:
    """A stand-in KnowledgeBaseManager exposing only what hydration uses.

    Deliberately not moto: the provider needs exactly ``get_object`` on one key, and
    a fake makes the call *count* assertable, which is what the no-pointless-retry
    test turns on.
    """
    manager = MagicMock()
    manager._get_s3_bucket.return_value = "codekb-knowledgebase"
    if archive is None:
        manager.s3_client.get_object.side_effect = FileNotFoundError("no such key")
    else:
        # A fresh stream per call: the provider streams the body to disk with
        # copyfileobj (reading to EOF), so one reused BytesIO would be exhausted on a
        # re-download. call_count is still tracked through side_effect.
        manager.s3_client.get_object.side_effect = lambda *a, **k: {"Body": BytesIO(archive)}
    return manager


# A ceiling that any populated cache exceeds, so eviction is under pressure. Used
# where a test cares about *which* entry goes rather than about the threshold.
_OVER_BUDGET = 1


def _provider(tmp_path: Path, *, archive: bytes | None = None, max_bytes: int | None = None):
    """A provider rooted in ``tmp_path`` so tests never touch the real cache root.

    ``max_bytes`` sets the byte ceiling directly rather than going through
    ``max_size_gb``, which is denominated in GiB while these fixtures are a few dozen
    bytes. Even the smallest sensible configured ceiling would leave every cache in
    this file permanently under budget and every eviction test vacuous.
    """
    provider = SourceCodeProvider(_kb_manager(archive), cache_root=tmp_path, max_size_gb=0)
    if max_bytes is not None:
        provider._max_cache_bytes = max_bytes
    return provider


def _ceiling_keeping(*survivors: Path) -> int:
    """A ceiling that leaves room for exactly ``survivors``.

    Eviction runs ``while footprint > ceiling``, so setting the ceiling to the total
    size of the entries a test expects to survive makes it evict precisely the rest.
    Derived from the fixtures rather than hardcoded, so it holds if ``_entry``
    changes what it writes.
    """
    return sum(SourceCodeProvider._directory_size(path) for path in survivors)


class TestEvictsLeastRecentlyUsedFirst:
    def test_the_oldest_entry_goes_first(self, tmp_path, monkeypatch):
        oldest = _entry(tmp_path, "space-old", age_seconds=9000)
        middle = _entry(tmp_path, "space-mid", age_seconds=6000)
        newest = _entry(tmp_path, "space-new", age_seconds=3000)
        ceiling = _ceiling_keeping(middle, newest)

        assert _provider(tmp_path, max_bytes=ceiling).make_room() == 1

        assert not oldest.exists()
        assert middle.exists() and newest.exists()

    def test_keeps_evicting_until_the_cache_is_under_its_ceiling(self, tmp_path):
        oldest = _entry(tmp_path, "space-old", age_seconds=9000)
        middle = _entry(tmp_path, "space-mid", age_seconds=6000)
        newest = _entry(tmp_path, "space-new", age_seconds=3000)
        ceiling = _ceiling_keeping(newest)

        assert _provider(tmp_path, max_bytes=ceiling).make_room() == 2

        # Oldest-first ordering has to hold across successive evictions, not just
        # for the first pick.
        assert not oldest.exists()
        assert not middle.exists()
        assert newest.exists()

    def test_a_cache_hit_makes_an_entry_the_newest(self, tmp_path):
        stale = _entry(tmp_path, "space-stale", age_seconds=9000)
        hot = _entry(tmp_path, "space-hot", age_seconds=9500)
        provider = _provider(tmp_path, max_bytes=_ceiling_keeping(hot))

        # Reading `hot` should reorder it ahead of `stale` despite starting older.
        provider.source_root("space-hot")
        provider.make_room()

        assert not stale.exists()
        assert hot.exists()


class TestNeverEvictsWhatItMustNot:
    def test_pipeline_working_trees_are_untouchable(self, tmp_path, monkeypatch):
        analysis = _entry(tmp_path, f"space-a-{WORK_DIR_ANALYSIS}", age_seconds=9000)
        refresh = _entry(tmp_path, f"space-b-{WORK_DIR_REFRESH}", age_seconds=9000)

        # Nothing else is present, so an evictor that ignored the naming rule
        # would take one of these and break a running pipeline.
        assert _provider(tmp_path, max_bytes=_OVER_BUDGET).make_room() == 0

        assert analysis.exists()
        assert refresh.exists()

    def test_staging_and_detached_entries_are_untouchable(self, tmp_path, monkeypatch):
        flatten_tmp = _entry(tmp_path, "space-a__tmp", age_seconds=9000)
        detached = _entry(tmp_path, ".discarding-space-a-1234abcd", age_seconds=9000)
        (tmp_path / "space-a-source.tar.gz").write_bytes(b"staging")

        assert _provider(tmp_path, max_bytes=_OVER_BUDGET).make_room() == 0

        assert flatten_tmp.exists()
        assert detached.exists()
        assert (tmp_path / "space-a-source.tar.gz").exists()

    def test_the_space_being_written_is_excluded(self, tmp_path, monkeypatch):
        incoming = _entry(tmp_path, "space-incoming", age_seconds=9000)

        # Without the exclusion the incoming space is the only candidate, so it
        # would evict the very tree it is about to populate.
        assert _provider(tmp_path).make_room(incoming_space_id="space-incoming") == 0

        assert incoming.exists()

    def test_a_freshly_touched_tree_is_protected_by_the_idle_window(self, tmp_path, monkeypatch):
        fresh = _entry(tmp_path, "space-fresh", age_seconds=0)

        # This is the in-flight-read guard: a reader stamps the tree on hydrate,
        # so anything stamped within the window may still be being read.
        assert _provider(tmp_path, max_bytes=_OVER_BUDGET).make_room() == 0

        assert fresh.exists()

    def test_files_are_never_chosen_only_directories(self, tmp_path, monkeypatch):
        loose = tmp_path / "not-a-space-dir"
        loose.write_text("stray file")
        stamp = time.time() - 9000
        os.utime(loose, (stamp, stamp))

        assert _provider(tmp_path, max_bytes=_OVER_BUDGET).make_room() == 0

        assert loose.exists()

    def test_no_generated_space_id_can_look_like_a_working_tree(self):
        # The safety of the whole scheme rests on this: eviction trusts the name,
        # so a real space id must never match a scratch pattern. Checked with
        # adversarial names rather than assumed.
        for name in (
            "my repo analysis",
            "my-refresher",
            "refresh-tool",
            "analysis",
            "thing__tmp",
            "archive.tar.gz",
        ):
            assert not is_scratch_entry(generate_space_id_from_name(name))


class TestPressureSignals:
    def test_a_cache_within_its_ceiling_is_never_evicted(self, tmp_path):
        """The regression that made CodeKB unusable on a developer's laptop.

        Two earlier versions read free space on the volume — first as a percentage
        floor, then as a per-download fit check — and evicted when it looked tight. A
        workstation disk sitting near capacity is ordinary and almost none of it is
        CodeKB's, so both put eviction permanently in breach: every read cleared the
        cache and re-downloaded it, without ever freeing the space that caused it.

        Now nothing but CodeKB's own footprint is consulted. A well behaved tenant
        under its budget evicts nothing, however full the surrounding disk is — and
        the disk is not even measured, so this test needs no fake to prove it.
        """
        kept = [_entry(tmp_path, f"space-{i}", age_seconds=9000) for i in range(3)]

        assert _provider(tmp_path, max_bytes=10**9).make_room() == 0
        assert all(entry.exists() for entry in kept)

    def test_the_footprint_counts_cache_entries_but_not_working_trees(self, tmp_path):
        cache_entry = _entry(tmp_path, "space-a", files=2)
        _entry(tmp_path, f"space-b-{WORK_DIR_ANALYSIS}", files=20)
        _entry(tmp_path, f"space-c-{WORK_DIR_REFRESH}", files=20)

        provider = _provider(tmp_path, max_bytes=_OVER_BUDGET)

        # Scratch is excluded because eviction cannot reclaim it: counting a live
        # pipeline's working tree would put the ceiling permanently out of reach and
        # evict the warm cache to no purpose.
        assert provider._cache_footprint_bytes() == SourceCodeProvider._directory_size(cache_entry)

    def test_the_footprint_includes_nested_files(self, tmp_path):
        entry = tmp_path / "space-a"
        (entry / "pkg" / "sub").mkdir(parents=True)
        (entry / "top.py").write_text("x" * 100)
        (entry / "pkg" / "mid.py").write_text("y" * 200)
        (entry / "pkg" / "sub" / "deep.py").write_text("z" * 300)

        # A shallow sum would under-count a real repository by orders of magnitude
        # and let the cache grow far past its ceiling.
        assert _provider(tmp_path)._cache_footprint_bytes() == 600


class TestFailuresDegradeToNoEviction:
    """A tidy-up failure must never surface in the request that triggered it."""

    def test_a_failing_delete_stops_eviction_rather_than_looping(self, tmp_path, monkeypatch):
        kept = _entry(tmp_path, "space-a", age_seconds=9000)
        monkeypatch.setattr(sp, "discard_directory", lambda path: False)

        # A victim that survives deletion stays the oldest, so another pass would
        # pick it again. Stopping on a reported failure is what makes the loop
        # terminate — this is the whole reason no eviction counter is needed.
        assert _provider(tmp_path, max_bytes=_OVER_BUDGET).make_room() == 0
        assert kept.exists()

    def test_an_exploding_candidate_scan_does_not_raise(self, tmp_path, monkeypatch):
        _entry(tmp_path, "space-a", age_seconds=9000)
        provider = _provider(tmp_path, max_bytes=_OVER_BUDGET)

        def boom(*args, **kwargs):
            raise RuntimeError("filesystem went away")

        monkeypatch.setattr(provider, "_oldest_evictable", boom)

        assert provider.make_room() == 0

    def test_a_missing_cache_root_is_not_an_error(self, tmp_path):
        provider = _provider(tmp_path / "does-not-exist", max_bytes=_OVER_BUDGET)

        assert provider.make_room() == 0

    def test_a_ceiling_that_can_never_be_met_still_terminates(self, tmp_path):
        # A ceiling below the size of a single entry, so the loop can only stop by
        # running out of candidates. There is no eviction counter to save it.
        for i in range(70):
            _entry(tmp_path, f"space-{i:03d}", age_seconds=9000 + i, files=1)

        evicted = _provider(tmp_path, max_bytes=_OVER_BUDGET).make_room()

        assert evicted == 70
        assert list(tmp_path.iterdir()) == []

    def test_a_failed_stamp_does_not_fail_the_read(self, tmp_path, monkeypatch):
        _entry(tmp_path, "space-a", age_seconds=9000)
        provider = _provider(tmp_path, max_bytes=_OVER_BUDGET)

        def unstampable(path, times=None):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(os, "utime", unstampable)

        # The stamp only informs eviction ordering, so losing it must not turn a
        # working read into an error.
        assert provider.source_root("space-a").is_dir()


class TestHydration:
    def test_a_miss_downloads_and_extracts(self, tmp_path):
        provider = _provider(tmp_path, archive=_tarball({"main.py": "print(1)\n"}))

        root = provider.source_root("space-a")

        assert (root / "main.py").read_text() == "print(1)\n"
        # The staging tarball must not be left behind holding disk.
        assert list(tmp_path.glob("*-source.tar.gz")) == []

    def test_a_hit_does_not_download_again(self, tmp_path):
        archive = _tarball({"main.py": "print(1)\n"})
        provider = _provider(tmp_path, archive=archive)

        provider.source_root("space-a")
        provider.source_root("space-a")

        assert provider.kb_manager.s3_client.get_object.call_count == 1

    def test_room_is_made_before_anything_is_written(self, tmp_path):
        victim = _entry(tmp_path, "space-old", age_seconds=9000)
        archive = _tarball({"main.py": "print(1)\n"})
        provider = _provider(tmp_path, archive=archive, max_bytes=_OVER_BUDGET)

        provider.source_root("space-new")

        # Hydration is the growth point, so the check belongs on this path and
        # not on a timer.
        assert not victim.exists()
        assert (tmp_path / "space-new" / "main.py").is_file()

    def test_a_missing_archive_raises_source_not_available(self, tmp_path):
        provider = _provider(tmp_path, archive=None)

        with pytest.raises(SourceNotAvailableError):
            provider.source_root("space-a")

    def test_a_repository_larger_than_the_whole_ceiling_is_reported(self, tmp_path, monkeypatch):
        # make_room runs before the download, so an oversized repository passes the
        # check and then breaches the ceiling with no way back: the next read of any
        # other space evicts it, and the next read of this one downloads it again. Only
        # configuration fixes that, so it has to be visible when it happens.
        archive = _tarball({"big.py": "x" * 4096})
        log = MagicMock()
        monkeypatch.setattr(sp, "logger", log)

        _provider(tmp_path, archive=archive, max_bytes=1024).source_root("space-big")

        assert any("larger than the entire" in str(c) for c in log.warning.call_args_list)

        # The same tree under a ceiling that can hold it must stay quiet, or the
        # warning fires on every hydration and stops meaning anything.
        log.reset_mock()
        _provider(tmp_path, archive=archive, max_bytes=10 * 1024).source_root("space-small")

        assert not any("larger than the entire" in str(c) for c in log.warning.call_args_list)

    def test_unbounded_mode_stays_quiet_about_size(self, tmp_path, monkeypatch):
        # With the cache unbounded (max_size_gb 0, so the byte ceiling is 0) every
        # tree is trivially "larger than" a zero ceiling. The size warning must stay
        # silent here, or it fires on every hydration and stops meaning anything.
        archive = _tarball({"big.py": "x" * 4096})
        log = MagicMock()
        monkeypatch.setattr(sp, "logger", log)

        _provider(tmp_path, archive=archive).source_root("space-big")

        assert not any("larger than the entire" in str(c) for c in log.warning.call_args_list)

    def test_a_failed_extraction_leaves_no_partial_tree(self, tmp_path, monkeypatch):
        # An extraction that dies partway — ENOSPC on an oversized repo is the
        # designed disk-full path — must not leave a half-written tree behind. The
        # cache-hit check only tests is_dir() and any(iterdir()), so a partial tree
        # would be served forever and never re-hydrated. A failed hydration has to
        # discard it so the next read re-downloads instead of serving half a repo.
        provider = _provider(tmp_path, archive=_tarball({"main.py": "print(1)\n"}))

        class _PartialTar:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extractall(self, path, **kwargs):
                # Write one file, then fail: a partially populated tree.
                Path(path).mkdir(parents=True, exist_ok=True)
                (Path(path) / "half.py").write_text("partial\n")
                raise OSError(28, "No space left on device")

        monkeypatch.setattr(sp.tarfile, "open", lambda *a, **k: _PartialTar())

        with pytest.raises(OSError):
            provider.source_root("space-a")

        assert not provider.cache_path("space-a").exists()


class TestReadsSurviveEviction:
    """The one race eviction introduces, and why readers do not lose data to it."""

    def test_read_text_re_hydrates_when_the_tree_vanishes_mid_read(self, tmp_path, monkeypatch):
        archive = _tarball({"main.py": "print(1)\n"})
        provider = _provider(tmp_path, archive=archive)
        provider.source_root("space-a")

        real_read_text = Path.read_text
        calls = {"n": 0}

        def vanishing_read_text(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                # Simulate another thread's eviction landing between the
                # hydrate and the open.
                sp.discard_directory(tmp_path / "space-a")
                raise FileNotFoundError(str(self))
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", vanishing_read_text)

        assert provider.read_text("space-a", "main.py") == "print(1)\n"
        assert calls["n"] == 2

    def test_a_genuinely_absent_file_is_not_retried(self, tmp_path, monkeypatch):
        archive = _tarball({"main.py": "print(1)\n"})
        provider = _provider(tmp_path, archive=archive)
        provider.source_root("space-a")

        real_read_text = Path.read_text
        attempts = {"n": 0}

        def counting_read_text(self, *args, **kwargs):
            attempts["n"] += 1
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", counting_read_text)

        with pytest.raises(FileNotFoundError):
            provider.read_text("space-a", "nope.py")

        # Exactly one attempt. The retry exists for an evicted tree, and the tree
        # is plainly still here, so a second attempt would resolve the same path
        # and fail the same way.
        #
        # Asserting the attempt count rather than the S3 call count: an earlier
        # version of this test checked get_object, which cannot detect the bug at
        # all, because the retry's source_root call is a cache hit on the
        # still-present tree and never reaches S3. Mutation testing caught it.
        assert attempts["n"] == 1
        assert provider.kb_manager.s3_client.get_object.call_count == 1

    def test_list_dir_re_hydrates_when_the_tree_vanishes_mid_listing(self, tmp_path, monkeypatch):
        archive = _tarball({"pkg/b.py": "2\n", "pkg/c.py": "3\n"})
        provider = _provider(tmp_path, archive=archive)
        provider.source_root("space-a")

        real_iterdir = Path.iterdir
        vanished = {"done": False}

        # Targets the subdirectory being listed rather than a call count, so the
        # test does not depend on how many times source_root probes the root.
        def vanishing_iterdir(self):
            if self.name == "pkg" and not vanished["done"]:
                vanished["done"] = True
                # Simulate another thread's eviction landing between the
                # hydrate and the listing.
                sp.discard_directory(tmp_path / "space-a")
                raise FileNotFoundError(str(self))
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", vanishing_iterdir)

        entries = provider.list_dir("space-a", "pkg")

        assert vanished["done"] is True
        assert ("pkg/b.py", False) in entries
        assert ("pkg/c.py", False) in entries

    def test_hidden_entries_are_omitted(self, tmp_path):
        archive = _tarball({"a.py": "1\n", ".secret": "x\n"})
        provider = _provider(tmp_path, archive=archive)

        names = [rel for rel, _ in provider.list_dir("space-a")]

        assert "a.py" in names
        assert ".secret" not in names

    def test_listing_a_file_raises_rather_than_returning_nothing(self, tmp_path):
        archive = _tarball({"a.py": "1\n"})
        provider = _provider(tmp_path, archive=archive)

        # Silently returning [] would look identical to an empty directory.
        with pytest.raises(NotADirectoryError):
            provider.list_dir("space-a", "a.py")


class TestPathsStayInsideTheSourceRoot:
    @pytest.mark.parametrize(
        "escape",
        ["../outside.py", "pkg/../../outside.py", "/etc/passwd", "pkg/../.."],
    )
    def test_traversal_attempts_are_refused(self, tmp_path, escape):
        root = _entry(tmp_path, "space-a", age_seconds=0)
        (root / "pkg").mkdir()

        with pytest.raises(ValueError):
            SourceCodeProvider.resolve_within(root, escape)

    def test_legitimate_relative_paths_resolve(self, tmp_path):
        root = _entry(tmp_path, "space-a", age_seconds=0)
        (root / "pkg").mkdir()
        (root / "pkg" / "mod.py").write_text("x\n")

        assert SourceCodeProvider.resolve_within(root, "pkg/mod.py").is_file()
        assert SourceCodeProvider.resolve_within(root, "") == root.resolve()


class TestInvalidate:
    def test_it_removes_the_cache_entry_so_the_next_read_re_downloads(self, tmp_path):
        archive = _tarball({"main.py": "print(1)\n"})
        provider = _provider(tmp_path, archive=archive)
        provider.source_root("space-a")

        provider.invalidate("space-a")

        assert not (tmp_path / "space-a").exists()
        provider.source_root("space-a")
        assert provider.kb_manager.s3_client.get_object.call_count == 2

    def test_it_leaves_working_trees_alone(self, tmp_path):
        # A refresh invalidates the cache entry while its own clone is still in
        # use, so this must not reach across to the working tree.
        work = _entry(tmp_path, f"space-a-{WORK_DIR_REFRESH}", age_seconds=0)
        _entry(tmp_path, "space-a", age_seconds=0)
        provider = _provider(tmp_path, max_bytes=_OVER_BUDGET)

        provider.invalidate("space-a")

        assert not (tmp_path / "space-a").exists()
        assert work.exists()

    def test_invalidating_an_absent_space_is_not_an_error(self, tmp_path):
        _provider(tmp_path).invalidate("never-existed")

    def test_it_keeps_the_hydration_lock_rather_than_popping_it(self, tmp_path):
        # Popping the per-space lock on invalidate let a reader mid-extraction hold a
        # lock no longer in the map, so the next reader's setdefault minted a fresh
        # one and both extracted into the same directory. Keeping the same lock object
        # is what preserves one-hydration-at-a-time across an invalidate.
        provider = _provider(tmp_path)
        lock_before = provider._hydration_lock("space-a")

        provider.invalidate("space-a")

        assert provider._hydration_lock("space-a") is lock_before


class TestConfiguration:
    def test_a_negative_ceiling_is_rejected_at_construction(self, tmp_path):
        # Fail loudly at startup rather than carrying a nonsensical ceiling that
        # would put the cache permanently over budget.
        with pytest.raises(ConfigError):
            SourceCodeProvider(_kb_manager(), cache_root=tmp_path, max_size_gb=-1)

    def test_a_fractional_ceiling_is_honoured_rather_than_truncated(self, tmp_path):
        # Truncating to int made 0.5 collapse to 0 -- which is the "unbounded"
        # sentinel -- so asking for a 512 MiB cache silently removed the bound
        # altogether. Sub-GiB is the normal case on a workstation, so this is the
        # value that must not round away.
        provider = SourceCodeProvider(_kb_manager(), cache_root=tmp_path, max_size_gb=0.5)

        assert provider._max_cache_bytes == 512 * 1024**2

    def test_a_zero_ceiling_leaves_the_cache_unbounded(self, tmp_path):
        # Zero is a sentinel, not a budget of nothing: the loop is skipped entirely.
        # Reading it as a literal ceiling would evict every entry on every call.
        entry = _entry(tmp_path, "space-a", age_seconds=9000)
        provider = SourceCodeProvider(_kb_manager(), cache_root=tmp_path, max_size_gb=0)

        assert provider.make_room() == 0
        assert entry.exists()

    def test_the_default_comes_from_config(self, tmp_path):
        # No explicit ceiling: source_cache.max_size_gb must exist in default.yaml,
        # since it is read with require_config.
        provider = SourceCodeProvider(_kb_manager(), cache_root=tmp_path)

        assert provider._max_cache_bytes > 0

    def test_the_cache_root_is_a_fixed_location(self):
        # Deliberately a tripwire rather than a description of behaviour. A
        # knowledge base stores each component path as <root>/<space_id>/<file>,
        # so this value is part of the persisted record: changing it invalidates
        # every space that already exists. If this assertion has to be edited,
        # that is the signal to think about migration first.
        provider = SourceCodeProvider(_kb_manager())

        assert sp.SOURCE_CACHE_ROOT == Path("/tmp/codekb")
        assert provider.cache_root == Path("/tmp/codekb")
