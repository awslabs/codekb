"""Bounded local cache of extracted repository source trees.

Every source read in CodeKB needs the repository's files on local disk. They are
hydrated from the ``source.tar.gz`` archive in S3 into a per-space directory under
:data:`SOURCE_CACHE_ROOT` and kept there so the next read is cheap. This module
owns that cache: the directory naming, hydration, eviction, and the startup sweep.

The cache root is a fixed location rather than a setting. A knowledge base stores
each component path as ``<root>/<space_id>/<file>``, so the root is part of what
gets persisted: a deployment that moved it would leave every stored path naming a
directory nothing populates. The consequence to be aware of is that on hosts where
``/tmp`` is a tmpfs this is a memory-backed cache, and the OS may clear it at times
CodeKB does not choose.

Two directory kinds live under the cache root, and the distinction is the whole
basis of eviction safety:

``<space_id>``
    A **cache entry** — the extracted source of a READY space. Disposable: it can
    always be rebuilt from S3, so it is what eviction reclaims.

``<space_id>-<kind>`` (``-analysis``, ``-refresh``) and the staging files
    **Scratch** — a working tree owned by a pipeline that is running right now, or
    a transient archive. Never evicted, because losing one would break the writer
    mid-run. Reclaimed by its owner's ``finally`` block, or by
    :func:`sweep_source_cache_scratch` on the next boot if the process died.

Keeping writers in a separate namespace is what lets eviction be stateless. There
is no lease table and no reference counting: a directory is either evictable by
name or it is not, which is visible on disk and survives a restart.

**Eviction policy.** Least-recently-used, triggered only where the cache is about
to grow, never on a timer:

* The clock is the directory's ``mtime``, stamped on every cache hit. It lives on
  disk so it survives a restart, and so there is no in-memory index that could
  disagree with what is actually there.
* One bound, checked wherever the cache is about to grow: the extracted cache stays
  under ``source_cache.max_size_gb``. That bounds *CodeKB's own* use, which is the
  only thing it can answer for — the rest of the disk belongs to whoever else is on
  it. Free space is deliberately not consulted: reacting to a disk CodeKB does not
  own means deleting its own data to no purpose. A repository that will not fit
  fails the extraction with a disk-full error instead.
* A tree idle for less than :data:`_MIN_IDLE_SECONDS` is never a candidate. That
  covers a read that is in flight: readers stamp on hydrate and finish in seconds,
  so a tree in use is continuously the newest entry and therefore the last thing
  eviction would pick.
* Reads retry once. If a tree does vanish between hydrating and opening a file,
  :meth:`SourceCodeProvider.read_text` re-hydrates and tries again, so the caller
  sees a slower response rather than a wrong one.

"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import threading
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from ..core.config import ConfigError, require_config
from ..core.file_utils import DISCARDING_PREFIX, discard_directory
from ..core.logging_utils import get_logger
from .errors import SourceNotAvailableError

logger = get_logger(__name__)


# Where extracted source trees live. Fixed rather than configurable: a knowledge
# base stores each component path as <root>/<space_id>/<file>, so this value is part
# of the persisted record and cannot move once spaces exist without invalidating
# every one of them. Tests redirect it with the ``cache_root`` argument on
# SourceCodeProvider and sweep_source_cache_scratch, not by configuration.
SOURCE_CACHE_ROOT = Path("/tmp/codekb")


# Suffixes marking a directory as a pipeline working tree rather than a cache entry.
WORK_DIR_ANALYSIS = "analysis"
WORK_DIR_REFRESH = "refresh"

# On-disk copy of a space's status.json, with an added "etag" key holding the
# In-tree marker: the space's status.json plus an "etag" key (the hydrated
# knowledge_base.json ETag). codekb_ prefix avoids clashing with a repo file.
_KB_VERSION_MARKER = ".codekb_status.json"

# Entries directly under the cache root that are scratch rather than cache.
#
# This is the single definition of "not a cache entry", and it has two consumers:
# sweep_source_cache_scratch reclaims these at boot, and eviction refuses to touch
# them. Adding a new scratch kind here therefore protects it from eviction and gets
# it swept, with no other change.
_SCRATCH_GLOBS = (
    f"*-{WORK_DIR_ANALYSIS}",  # analysis working tree, if _run_pipeline died
    f"*-{WORK_DIR_REFRESH}",  # refresh clone, if run() died before its finally
    "*-source.tar.gz",  # staging tarball (_upload_source_archive, hydration)
    "*-upload.zip",  # upload staging (_acquire_source_upload)
    "*__tmp",  # zip-flatten staging (_flatten_single_root)
    f"{DISCARDING_PREFIX}*",  # detached by discard_directory but not yet deleted
)


def is_scratch_entry(name: str) -> bool:
    """Whether a direct child of the cache root is scratch rather than a cache entry.

    Args:
        name: Bare directory or file name, not a path.

    Returns:
        True when the name matches any scratch pattern.
    """
    return any(fnmatch(name, pattern) for pattern in _SCRATCH_GLOBS)


def sweep_source_cache_scratch(root: Path | None = None) -> int:
    """Delete leftover scratch entries from the local source cache.

    Called once at API startup. A container killed mid-analysis leaves its scratch
    behind, and nothing else reclaims it: the space is also stranded in ANALYZING or
    REFRESHING, and ``refresh_space`` requires READY, so the owning pipeline's
    ``finally`` never runs again. Without this sweep those orphans persist for the
    life of the volume.

    Safe without an in-use check because of *when* it runs: the FastAPI lifespan
    executes before any request is served, and pipeline threads are only spawned by
    requests, so this process has nothing in flight.

    Cache entries (bare ``<space_id>`` trees) are deliberately left alone — they are
    valid warm cache, and bounding them is eviction's job, not the sweep's.

    Args:
        root: Cache root to sweep. Defaults to :data:`SOURCE_CACHE_ROOT`; passed
            explicitly by tests to redirect it.

    Returns:
        Number of entries reclaimed.
    """
    cache_root = root if root is not None else SOURCE_CACHE_ROOT
    if not cache_root.is_dir():
        return 0

    reclaimed = 0
    for pattern in _SCRATCH_GLOBS:
        for path in sorted(cache_root.glob(pattern)):
            if path.is_dir():
                if discard_directory(path):
                    reclaimed += 1
            else:
                try:
                    path.unlink()
                    reclaimed += 1
                except OSError as e:
                    logger.warning(f"Startup sweep could not remove {path}: {e}")

    if reclaimed:
        logger.info(f"Startup sweep reclaimed {reclaimed} scratch entry(ies) from {cache_root}")
    return reclaimed


class SourceCodeProvider:
    """Hydrates, serves, and bounds the local cache of extracted source trees.

    The single place that knows how a space's source reaches local disk. Callers ask
    for content and never manage the cache themselves, which is what allows the
    bound to be enforced in one spot.
    """

    # A tree touched more recently than this is never an eviction candidate. It only
    # has to outlast one in-flight read: readers stamp the tree when they hydrate,
    # transactions complete in seconds, and the next one stamps it again — so a tree
    # in use stays the newest entry and eviction reaches for the oldest.
    #
    # Not configurable on purpose. Every wrong value is a downside: too low
    # reintroduces the race this prevents, too high stops eviction making progress
    # when few trees exist.
    _MIN_IDLE_SECONDS = 120

    def __init__(
        self,
        kb_manager: object,
        *,
        cache_root: Path | None = None,
        max_size_gb: float | None = None,
        version_of: Callable[[str], str | None] | None = None,
    ) -> None:
        """Build a provider.

        Args:
            kb_manager: KnowledgeBaseManager, used for its S3 client and bucket.
                Untyped to avoid an import cycle through the persistence layer.
            cache_root: Cache root override, for tests. Defaults to
                :data:`SOURCE_CACHE_ROOT`.
            max_size_gb: Cache size ceiling override, in GiB. Fractional values are
                allowed, so 0.5 is a 512 MiB ceiling. Defaults to
                ``source_cache.max_size_gb`` from config. Exactly 0 leaves the cache
                unbounded.
            version_of: Returns a space's current knowledge_base.json ETag; source_root
                uses it to tell if the on-disk tree is behind. None (default) disables
                the check, keeping the provider standalone for tests.

        Raises:
            ConfigError: If a configured value is negative or not a number.
        """
        self.kb_manager = kb_manager
        self._cache_root = cache_root if cache_root is not None else SOURCE_CACHE_ROOT
        self._version_of = version_of

        configured = (
            max_size_gb if max_size_gb is not None else require_config("source_cache.max_size_gb")
        )
        try:
            size_gb = float(configured)
        except (TypeError, ValueError):
            raise ConfigError(
                f"source_cache.max_size_gb must be a number, got {configured!r}"
            ) from None
        if size_gb < 0:
            raise ConfigError(f"source_cache.max_size_gb must not be negative, got {size_gb}")
        self._max_cache_bytes = int(size_gb * 1024**3)

        # One hydration at a time per space. Without this, two readers missing the
        # same cold space both extract into the same directory and interleave their
        # writes. The race predates this module but eviction makes it more reachable,
        # since an evicted tree turns concurrent hits into concurrent misses.
        self._hydration_locks: dict[str, threading.Lock] = {}
        self._hydration_locks_guard = threading.Lock()

    # ── paths ────────────────────────────────────────────────────────────

    @property
    def cache_root(self) -> Path:
        """Root directory holding every cache entry and working tree."""
        return self._cache_root

    def cache_path(self, space_id: str) -> Path:
        """Local path of a space's cache entry, whether or not it exists."""
        return self._cache_root / space_id

    def work_path(self, space_id: str, kind: str) -> Path:
        """Local path of a pipeline working tree, which eviction never touches.

        Args:
            space_id: Space the pipeline is operating on.
            kind: WORK_DIR_ANALYSIS or WORK_DIR_REFRESH. Must keep ``_SCRATCH_GLOBS``
                matching, or the directory becomes evictable mid-run.

        Returns:
            The working-tree path.
        """
        return self._cache_root / f"{space_id}-{kind}"

    @staticmethod
    def source_s3_key(space_id: str) -> str:
        """S3 key of a space's archived source tarball."""
        return f"spaces/{space_id}/source.tar.gz"

    # ── reading ──────────────────────────────────────────────────────────

    def source_root(self, space_id: str) -> Path:
        """Return the space's local source root, hydrating from S3 if absent.

        Stamps the directory so it becomes the most recently used entry, which is
        what keeps a tree that is being read away from eviction.

        Args:
            space_id: Space whose source is needed.

        Returns:
            Path to the extracted source root.

        Raises:
            SourceNotAvailableError: If the archive cannot be fetched from S3.
        """
        local_path = self.cache_path(space_id)
        if local_path.is_dir() and any(local_path.iterdir()):
            if not self._disk_is_stale(space_id):
                self._touch(local_path)
                logger.debug(f"Source cache hit for '{space_id}' at {local_path}")
                return local_path
            # On-disk tree is behind the latest version; drop and re-hydrate.
            with self._hydration_lock(space_id):
                # Re-check: another reader may have re-hydrated while we waited.
                if not self._disk_is_stale(space_id):
                    self._touch(local_path)
                    return local_path
                discard_directory(local_path)
        return self._hydrate(space_id)

    def read_text(self, space_id: str, rel_path: str) -> str:
        """Read one file from a space's source, hydrating first if needed.

        Retries once when the tree disappears between hydrating and opening: that is
        the one race eviction introduces, and re-hydrating is idempotent. A file that
        is genuinely absent is *not* retried, so a miss costs nothing extra.

        Args:
            space_id: Space to read from.
            rel_path: Path relative to the source root.

        Returns:
            File contents, with undecodable bytes replaced.

        Raises:
            ValueError: If rel_path escapes the source root.
            FileNotFoundError: If the file does not exist in the repository.
            SourceNotAvailableError: If the archive cannot be fetched from S3.
        """
        for attempt in (1, 2):
            root = self.source_root(space_id)
            target = self.resolve_within(root, rel_path)
            try:
                return target.read_text(errors="replace")
            except FileNotFoundError:
                # The tree still being present means the file really is absent from
                # the repository, so a second attempt would resolve the same path
                # and fail identically. Skipping it matters because the agent's
                # read_source_file probes every space in turn looking for a file, so
                # a miss is the common case, not the exceptional one.
                if attempt == 2 or root.is_dir():
                    raise
                logger.info(
                    f"Source tree for '{space_id}' vanished mid-read "
                    f"(evicted under disk pressure); re-hydrating and retrying once"
                )
        raise AssertionError("unreachable: the loop above either returns or raises")

    def list_dir(self, space_id: str, rel_path: str = "") -> list[tuple[str, bool]]:
        """List the visible children of a directory in a space's source.

        Hidden entries (leading dot) are omitted. Retries once on a vanished tree,
        for the same reason as :meth:`read_text`.

        Args:
            space_id: Space to list from.
            rel_path: Directory relative to the source root. Empty for the root.

        Returns:
            ``(path relative to the source root, is a directory)`` pairs, sorted
            by name.

        Raises:
            ValueError: If rel_path escapes the source root.
            NotADirectoryError: If the path is not a directory in the repository.
            SourceNotAvailableError: If the archive cannot be fetched from S3.
        """
        for attempt in (1, 2):
            root = self.source_root(space_id)
            target = self.resolve_within(root, rel_path)
            try:
                if not target.is_dir():
                    raise NotADirectoryError(f"not a directory in space '{space_id}': {rel_path}")
                return [
                    (str(child.relative_to(root)), child.is_dir())
                    for child in sorted(target.iterdir())
                    if not child.name.startswith(".")
                ]
            except (FileNotFoundError, NotADirectoryError):
                if attempt == 2 or root.is_dir():
                    raise
                logger.info(
                    f"Source tree for '{space_id}' vanished mid-listing "
                    f"(evicted under disk pressure); re-hydrating and retrying once"
                )
        raise AssertionError("unreachable: the loop above either returns or raises")

    @staticmethod
    def resolve_within(root: Path, rel_path: str) -> Path:
        """Resolve ``rel_path`` under ``root``, refusing to escape it.

        Args:
            root: Directory the result must stay inside.
            rel_path: Untrusted relative path, e.g. from an agent tool argument.

        Returns:
            The resolved absolute path.

        Raises:
            ValueError: If the result would fall outside root.
        """
        root_resolved = root.resolve()
        candidate = (root / rel_path).resolve() if rel_path else root_resolved
        try:
            candidate.relative_to(root_resolved)
        except ValueError:
            raise ValueError(f"path traversal detected in '{rel_path}'") from None
        return candidate

    def invalidate(self, space_id: str) -> None:
        """Drop a space's cache entry so the next read re-hydrates from S3.

        Used after a write changes the source in S3 — a refresh, or the start of a
        re-analysis — where the cached tree would otherwise keep serving the previous
        revision. Never raises.

        Args:
            space_id: Space whose cache entry should be dropped.
        """
        with self._hydration_lock(space_id):
            discard_directory(self.cache_path(space_id))

    # ── eviction ─────────────────────────────────────────────────────────

    def make_room(self, incoming_space_id: str | None = None) -> int:
        """Evict least-recently-used cache entries until the cache is under its ceiling.

        One rule: the extracted cache stays under ``source_cache.max_size_gb``. That
        bounds what CodeKB itself uses, which is the only thing it can answer for — the
        rest of the disk belongs to whoever else is on it. If a repository will not fit
        in the space that remains, the extraction fails with a disk-full error, which is
        a clear result rather than a guess.

        Called where the cache is about to grow — before hydrating, and before a
        pipeline clones — rather than from a timer, which would add a lifecycle that
        can die silently.

        Never raises: on any failure it stops early, having freed whatever it freed.

        Args:
            incoming_space_id: Space about to be written, excluded from eviction so it
                cannot evict itself.

        Returns:
            Number of entries evicted.
        """
        if not self._max_cache_bytes:
            return 0  # A ceiling of 0 means unbounded, not "evict everything".

        evicted = 0
        try:
            # Size every cache entry once here. make_room then subtracts each victim's
            # known size as it evicts instead of re-measuring the tree, so a hydration
            # walks the cache a single time rather than once per eviction.
            entry_sizes = self._cache_entry_sizes()
            footprint = sum(entry_sizes.values())

            while footprint > self._max_cache_bytes:
                victim = self._oldest_evictable(exclude=incoming_space_id)
                if victim is None:
                    logger.warning(
                        f"Source cache is {footprint:,} bytes, over its "
                        f"{self._max_cache_bytes:,} byte ceiling, but nothing is "
                        "evictable (every entry is scratch, in use, or idle < "
                        f"{self._MIN_IDLE_SECONDS}s)."
                    )
                    return evicted

                # Fall back to a direct measure only if the victim appeared after the
                # sizing pass above (e.g. a pipeline finishing mid-eviction).
                victim_bytes = entry_sizes.get(victim)
                if victim_bytes is None:
                    victim_bytes = self._directory_size(victim)
                logger.info(f"Evicting least-recently-used source tree {victim}")
                if not discard_directory(victim):
                    # The victim is still on disk and still the oldest, so another pass
                    # would pick it again. Stopping is what makes the loop terminate.
                    logger.warning(f"Could not reclaim {victim}; stopping eviction")
                    return evicted

                footprint = max(0, footprint - victim_bytes)
                evicted += 1
        except Exception as e:
            # Reclaiming disk must never fail the request that triggered it. Losing
            # this attempt leaves the cache as it was before eviction existed.
            logger.warning(f"Source cache eviction failed: {e}", exc_info=True)
        return evicted

    def _cache_footprint_bytes(self) -> int:
        """Total size of the cache entries, excluding scratch.

        Scratch is excluded because the ceiling governs what eviction can reclaim, and
        a working tree is untouchable while its run is in progress. Counting one would
        make the ceiling unsatisfiable mid-analysis and evict the warm cache to no
        purpose. Peak disk use is therefore the ceiling plus whatever concurrent
        pipelines hold — the concurrency half of which is MEM-5, not this bound.
        """
        return sum(self._cache_entry_sizes().values())

    def _cache_entry_sizes(self) -> dict[Path, int]:
        """Size in bytes of each non-scratch cache entry, keyed by its path.

        The per-entry breakdown behind _cache_footprint_bytes. make_room sums it for
        the footprint and then subtracts a victim's known size as it evicts, so one
        walk here serves both instead of re-measuring each victim.

        Scratch is excluded for the same reason as the footprint: a working tree is
        untouchable while its run is in progress, so counting it would evict warm
        cache to no purpose.
        """
        sizes: dict[Path, int] = {}
        try:
            for entry in self._cache_root.iterdir():
                if entry.is_dir() and not is_scratch_entry(entry.name):
                    sizes[entry] = self._directory_size(entry)
        except OSError as e:
            logger.warning(f"Could not size source cache {self._cache_root}: {e}")
        return sizes

    @staticmethod
    def _directory_size(path: Path) -> int:
        """Apparent size of a directory tree, from stat only — no file reads.

        Errors are swallowed per entry: a tree being deleted underneath the walk
        should yield a smaller number, not raise.
        """
        total = 0
        stack = [path]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
            except OSError:
                continue
        return total

    def _oldest_evictable(self, exclude: str | None = None) -> Path | None:
        """Least-recently-used evictable cache entry, or None if there is none.

        A directory qualifies only if it is a cache entry by name, is not the space
        being written, and has been idle longer than ``_MIN_IDLE_SECONDS``.
        """
        cutoff = time.time() - self._MIN_IDLE_SECONDS
        oldest_mtime: float | None = None
        oldest: Path | None = None

        try:
            entries = list(self._cache_root.iterdir())
        except OSError as e:
            logger.warning(f"Could not list source cache root {self._cache_root}: {e}")
            return None

        for path in entries:
            if path.name == exclude or is_scratch_entry(path.name):
                continue
            try:
                if not path.is_dir():
                    continue
                mtime = path.stat().st_mtime
            except OSError:
                # Vanished between listing and stat — another eviction got there
                # first, which is fine; it is no longer occupying disk.
                continue
            if mtime > cutoff:
                continue
            if oldest_mtime is None or mtime < oldest_mtime:
                oldest_mtime, oldest = mtime, path
        return oldest

    def _touch(self, path: Path) -> None:
        """Mark a cache entry as just used, so eviction sees it as the newest."""
        try:
            os.utime(path)
        except OSError as e:
            # Only affects eviction ordering, so this must not fail the read. The
            # entry looks older than it is and may be evicted early; a reader that
            # loses it re-hydrates via read_text's retry.
            logger.debug(f"Could not stamp source cache entry {path}: {e}")

    # ── freshness ──────────────────────────────────────────────────────────

    def _disk_is_stale(self, space_id: str) -> bool:
        """True when the on-disk tree is behind the latest version.

        Compares the disk marker to the latest ETag (from ``version_of``). No known
        version → trust the tree. A missing marker counts as stale (re-hydrate).
        """
        latest = self._version_of(space_id) if self._version_of else None
        if latest is None:
            return False
        return self._read_marker_etag(space_id) != latest

    def _kb_marker_path(self, space_id: str) -> Path:
        return self.cache_path(space_id) / _KB_VERSION_MARKER

    def _read_marker_etag(self, space_id: str) -> str | None:
        """The ``etag`` recorded in the on-disk status.json, or None if absent."""
        try:
            data = json.loads(self._kb_marker_path(space_id).read_text())
        except (OSError, ValueError):
            return None
        etag = data.get("etag") if isinstance(data, dict) else None
        return etag if isinstance(etag, str) else None

    def _write_status_marker(self, space_id: str, etag: str) -> None:
        """Write the space's status.json plus an ``etag`` key. No-op without a tree."""
        local_path = self.cache_path(space_id)
        if not local_path.is_dir():
            return
        status = self._download_status(space_id) or {}
        status["etag"] = etag
        try:
            self._kb_marker_path(space_id).write_text(json.dumps(status))
        except OSError as e:
            # Non-fatal: a missing marker just forces a re-hydrate on the next read.
            logger.debug(f"Could not write status marker for '{space_id}': {e}")

    def _download_status(self, space_id: str) -> dict | None:
        """Fetch a space's status.json body from S3. None on any miss."""
        try:
            bucket = self.kb_manager._get_s3_bucket()  # type: ignore[attr-defined]
            resp = self.kb_manager.s3_client.get_object(  # type: ignore[attr-defined]
                Bucket=bucket, Key=f"spaces/{space_id}/status.json"
            )
            return json.loads(resp["Body"].read())
        except Exception as e:
            logger.debug(f"Could not download status.json for '{space_id}': {e}")
            return None

    # ── hydration ────────────────────────────────────────────────────────

    def _hydration_lock(self, space_id: str) -> threading.Lock:
        """Per-space lock serialising extraction into one directory."""
        with self._hydration_locks_guard:
            return self._hydration_locks.setdefault(space_id, threading.Lock())

    def _hydrate(self, space_id: str) -> Path:
        """Download and extract a space's source archive from S3.

        Raises:
            SourceNotAvailableError: If the archive cannot be fetched.
        """
        local_path = self.cache_path(space_id)
        with self._hydration_lock(space_id):
            # Another thread may have hydrated this space while we waited.
            if local_path.is_dir() and any(local_path.iterdir()):
                self._touch(local_path)
                return local_path

            bucket = self.kb_manager._get_s3_bucket()  # type: ignore[attr-defined]
            key = self.source_s3_key(space_id)

            # Before the download, so eviction never runs with an open S3 stream
            # waiting on it.
            self.make_room(incoming_space_id=space_id)

            local_path.mkdir(parents=True, exist_ok=True)
            archive_path = local_path.parent / f"{space_id}-source.tar.gz"

            logger.info(f"Downloading source archive from s3://{bucket}/{key}")
            try:
                resp = self.kb_manager.s3_client.get_object(  # type: ignore[attr-defined]
                    Bucket=bucket, Key=key
                )
                # Stream the body to disk in chunks rather than reading the whole
                # compressed archive into memory: a large repo would otherwise sit in
                # RAM on top of the on-disk archive and the extracted tree.
                with open(archive_path, "wb") as archive_file:
                    shutil.copyfileobj(resp["Body"], archive_file)
                logger.debug(f"s3.get_object ok: s3://{bucket}/{key}")
            except Exception as e:
                logger.error(
                    f"s3.get_object failed for source archive s3://{bucket}/{key}: {e}",
                    exc_info=True,
                )
                archive_path.unlink(missing_ok=True)
                discard_directory(local_path)
                raise SourceNotAvailableError(space_id) from e

            try:
                with tarfile.open(archive_path, "r:gz") as tar:
                    tar.extractall(path=local_path, filter="data")
                    # Size the tree from the archive's own member table rather than
                    # walking the extracted files on disk again just for the warning.
                    extracted_bytes = sum(m.size for m in tar.getmembers() if m.isreg())
                logger.info(f"Extracted source to {local_path}")
            except Exception:
                discard_directory(local_path)
                raise
            finally:
                archive_path.unlink(missing_ok=True)

            if self._max_cache_bytes and extracted_bytes > self._max_cache_bytes:
                logger.warning(
                    f"Source tree for '{space_id}' is {extracted_bytes:,} bytes, larger than "
                    f"the entire {self._max_cache_bytes:,} byte source cache ceiling. It will "
                    "be evicted by the next read of another space and re-downloaded on the "
                    "next read of this one. Raise source_cache.max_size_gb above the largest "
                    "repository you index."
                )
            # Tag the fresh tree with its version, so a later read can spot drift.
            etag = self._version_of(space_id) if self._version_of else None
            if etag is not None:
                self._write_status_marker(space_id, etag)
            self._touch(local_path)
            return local_path
