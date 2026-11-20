"""Process-wide cap on concurrent background pipelines.

Every long-running background job the API starts (space analysis from git or a
zip upload, space refresh, space overview, hyperspace overview) needs one of a
fixed number of slots, ``service.pipeline_pool_size``. At most that many jobs
run at once; when every slot is taken, a new job is refused with
PipelineCapacityError (429) instead of being queued. The limit is per process: a
deployment running several API processes runs at most the pool size times the
number of processes. Sizing guidance is with the setting in default.yaml.

The design is a semaphore holding one permit per slot. Taking a permit is the
admission check; each admitted job then runs on its own short-lived daemon
thread, which gives the permit back when the job ends. So there are never more
than N job threads, and none at all while nothing is running.

Usage — reserve before the first status write, so a refused request leaves no
ANALYZING/REFRESHING/in_progress record behind::

    with pipeline_pool().reserve("analysis", space_id) as slot:
        self._write_status(...)
        slot.submit(self._run_pipeline, ...)

If the ``with`` block exits without a successful ``submit`` (an exception, a lost
claim, a thread that failed to start), the slot is returned. Once submitted, the
job's thread returns it when the job ends, however it ends.

The pool returns only the slot. When the block raises after the caller's
status write (for example ``submit`` failing because the thread could not
start), that status is still in progress with no job behind it, so the caller
must undo the write. CodeKBService does this with ``_undo_unless_submitted``.

A slot is valid only inside its ``with`` block: ``submit`` after the block
exits raises instead of starting a job whose slot was already returned.

Visibility. Each job logs "Pipeline started" and "Pipeline finished" with its
kind, target, duration and the slots in use, so a job that holds a slot without
finishing shows up as a start with no finish. While a job runs past
``summarization.warn_after_stale_s``, a watchdog logs "Pipeline still running"
once per interval until it ends. Python cannot stop a thread, so a job that
never returns keeps its slot until the process restarts; these logs are how
that is noticed.

Turning it off. A pool size of 0 disables the limit, as an emergency switch if
the limit itself misbehaves. ``reserve()`` then never refuses and never touches
the semaphore, and ``submit()`` starts the job directly on its own daemon
thread: the same path as before the limit existed, with no counting, no
start/finish logs and no watchdog. Jobs are then unlimited again, so startup
logs a warning.

Job threads are daemons, like the per-request threads this replaces, so a
process exit does not wait for running pipelines.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from ..core.config import ConfigError, require_config
from .errors import PipelineCapacityError

logger = logging.getLogger(__name__)

# Pipelines run for minutes, so a client retrying sooner would only be refused again.
RETRY_AFTER_SECONDS = 60


class PipelineSlot:
    """A reserved slot. Call ``submit`` at most once to start the job."""

    def __init__(self, pool: "PipelinePool", kind: str, target: str | None) -> None:
        self._pool = pool
        self.kind = kind
        self.target = target
        self.submitted = False
        # Set when reserve() exits. An unsubmitted slot's permit has then been
        # returned, so a late submit would release it a second time.
        self.closed = False

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        if self.submitted:
            raise RuntimeError("pipeline slot already submitted")
        if self.closed:
            raise RuntimeError("pipeline slot used after its reserve() block ended")
        if self._pool.enabled:
            target, target_args = self._pool._run, (self.kind, self.target, fn, args, kwargs)
            target_kwargs: dict[str, Any] = {}
        else:
            # Limit off: run the job directly, exactly as before the limit existed.
            target, target_args, target_kwargs = fn, args, kwargs
        threading.Thread(
            target=target,
            args=target_args,
            kwargs=target_kwargs,
            name=self._thread_name(),
            daemon=True,
        ).start()
        # Set only after start() succeeds: if the thread cannot start, reserve()
        # still owns the slot and returns it.
        self.submitted = True

    def _thread_name(self) -> str:
        """The target makes each job identifiable in a thread dump."""
        if self.target:
            return f"codekb-pipeline-{self.kind}-{self.target}"
        return f"codekb-pipeline-{self.kind}"


class PipelinePool:
    """At most ``size`` background jobs at once; refuses the rest immediately.

    ``size`` 0 turns the limit off (see the module docstring). ``warn_after_s``
    is how long a job may run before the watchdog starts logging "Pipeline still
    running" (and how often it repeats); 0 disables the watchdog.
    """

    def __init__(self, size: int, warn_after_s: float = 0) -> None:
        if size < 0:
            raise ValueError(f"pool size must be >= 0, got {size}")
        self.size = size
        self.warn_after_s = warn_after_s
        self._slots = threading.BoundedSemaphore(size) if size > 0 else None
        self._count_lock = threading.Lock()
        self._in_use = 0

    @property
    def enabled(self) -> bool:
        """False when the pool size is 0 and jobs are not limited."""
        return self._slots is not None

    @property
    def in_use(self) -> int:
        """Slots taken right now: reserved but not yet started, or running."""
        with self._count_lock:
            return self._in_use

    @contextmanager
    def reserve(self, kind: str, target: str | None = None) -> Iterator[PipelineSlot]:
        """Claim a slot or raise PipelineCapacityError immediately.

        ``target`` names what the job works on (a space or hyperspace ID) so the
        logs say which job holds which slot.
        """
        if self._slots is None:
            # Limit off: never refuse, nothing to count or give back.
            slot = PipelineSlot(self, kind, target)
            try:
                yield slot
            finally:
                slot.closed = True
            return
        if not self._slots.acquire(blocking=False):
            logger.warning(
                f"Pipeline refused: kind={kind}, target={target}, all {self.size} slots in use"
            )
            raise PipelineCapacityError(self.size, RETRY_AFTER_SECONDS)
        with self._count_lock:
            self._in_use += 1
        slot = PipelineSlot(self, kind, target)
        try:
            yield slot
        finally:
            slot.closed = True
            if not slot.submitted:
                self._release()

    def _release(self) -> None:
        with self._count_lock:
            self._in_use -= 1
        self._slots.release()

    def _run(
        self, kind: str, target: str | None, fn: Callable[..., Any], args: tuple, kwargs: dict
    ) -> None:
        label = f"kind={kind}, target={target}"
        started = time.monotonic()
        done = threading.Event()
        outcome = "interrupted"
        try:
            logger.info(f"Pipeline started: {label}, slots in use {self.in_use}/{self.size}")
            self._start_watchdog(label, started, done)
            fn(*args, **kwargs)
            outcome = "ok"
        except Exception:
            outcome = "raised"
            # Jobs write their own FAILED status; this only makes a failure that
            # escapes the job visible in the application log.
            logger.exception(f"Pipeline job raised: {label}")
        finally:
            done.set()
            self._release()
            logger.info(
                f"Pipeline finished: {label}, outcome={outcome}, "
                f"{time.monotonic() - started:.0f}s, slots in use {self.in_use}/{self.size}"
            )

    def _start_watchdog(self, label: str, started: float, done: threading.Event) -> None:
        """Log while the job runs past warn_after_s. Never fails the job."""
        if self.warn_after_s <= 0:
            return

        def watch() -> None:
            while not done.wait(self.warn_after_s):
                logger.warning(
                    f"Pipeline still running: {label}, {time.monotonic() - started:.0f}s so "
                    f"far, slots in use {self.in_use}/{self.size}. It keeps its slot "
                    f"until it returns or the process restarts."
                )

        try:
            threading.Thread(target=watch, name="codekb-pipeline-watchdog", daemon=True).start()
        except RuntimeError:
            # The job matters more than its watchdog: run it unwatched.
            logger.warning(f"Pipeline watchdog could not start: {label}")


_pool: PipelinePool | None = None
_pool_lock = threading.Lock()


def _configured_size() -> int:
    raw = require_config("service.pipeline_pool_size")
    try:
        size = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"service.pipeline_pool_size must be an integer, got {raw!r}") from None
    # 0 is the deliberate off switch; anything below it is a mistake.
    if size < 0:
        raise ConfigError(
            f"service.pipeline_pool_size must be >= 0 (0 turns the limit off), got {size}"
        )
    return size


def _configured_warn_after_s() -> int:
    # Same threshold as the "taking longer than usual" status warning, so one
    # setting decides when a long run is worth a look. 0 disables the watchdog.
    # Unlike the new pool size, this setting predates the pool, and the status
    # check in get_analysis_status already treats any value <= 0 as off. A
    # negative value therefore means off here too, rather than failing a startup
    # that worked before this setting gained a second reader.
    raw = require_config("summarization.warn_after_stale_s")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        raise ConfigError(
            f"summarization.warn_after_stale_s must be an integer, got {raw!r}"
        ) from None


def pipeline_pool() -> PipelinePool:
    """The process-wide pipeline limit, created on first use from config."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = PipelinePool(_configured_size(), _configured_warn_after_s())
                if _pool.enabled:
                    logger.info(
                        f"Pipeline pool ready: at most {_pool.size} concurrent jobs, "
                        f"long-run warning after {_pool.warn_after_s}s"
                    )
                else:
                    logger.warning(
                        "Pipeline pool disabled (service.pipeline_pool_size=0): background "
                        "jobs are not limited, and none are refused, counted or logged. "
                        "Set it back to a positive value once the limit is fixed."
                    )
    return _pool
