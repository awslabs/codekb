"""Refresh orchestrator for the space refresh pipeline.

Drives the space-refresh pipeline for a single Space: re-clone → fetch prior SHA →
git diff → bucket files → reprocess changed subset → merge → rebuild call graph →
stage-and-swap KB → upsert/delete vectors → invalidate caches.

One instance per refresh run. Not reused across runs. Not thread-safe.
"""

from __future__ import annotations

import asyncio
import dataclasses
import subprocess
import tarfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.token_usage import operation_usage, workload_record
from ..core.file_utils import LEGACY_DEFAULT_EXCLUDE_PATTERNS, FileScanner, discard_directory
from ..core.logging_utils import get_logger
from ..core.models import (
    ChangeType,
    Component,
    FileChange,
    KnowledgeBase,
    ParseFailureReport,
    RefreshPlan,
    RefreshResult,
    resolve_id_scheme,
)
from .errors import GitCommandError, RefreshFailed
from .git_ops import redact_git_url

if TYPE_CHECKING:
    from ..persistence.knowledge_base_manager import KnowledgeBaseManager
    from ..service.codekb_service import CodeKBService
    from ..vector.s3_vectors_client import S3VectorsClient
    from . import git_ops as git_ops_module

logger = get_logger(__name__)

_DART_SUFFIX = ".dart"


def _repo_relative_path(file_path: Path) -> Path:
    """Recover the repo-relative form of a component's persisted file path.

    Analysis persists component paths as absolute paths under the source cache
    root: ``<SOURCE_CACHE_ROOT>/<space-dir>/<repo-relative...>``. Space refresh
    needs the repo-relative portion in two places that must agree — comparing
    prior-KB paths against repo-relative git-diff paths (``_step_merge``) and
    re-resolving them against a fresh clone (``_step_rebuild_call_graph``). Both
    call this so a change to the convention can't make the two steps disagree.

    Relative paths (freshly parsed components, or legacy KBs) are returned
    unchanged. Absolute paths outside the cache root fall back to the basename.
    """
    if not file_path.is_absolute():
        return file_path

    # Local import avoids a circular import with the service module.
    from ..service.codekb_service import SOURCE_CACHE_ROOT

    try:
        under_root = file_path.relative_to(SOURCE_CACHE_ROOT)
    except ValueError:
        # Not under the cache root — fall back to the basename so a lookup in
        # the refresh clone root can still be attempted.
        return Path(file_path.name)

    # under_root == <space-dir>/<repo-relative...>; drop the space-dir segment.
    if len(under_root.parts) > 1:
        return Path(*under_root.parts[1:])
    return under_root


class RefreshOrchestrator:
    """Drives the space-refresh pipeline for a single Space.

    One instance per refresh run. Not reused across runs. Not thread-safe.

    The constructor takes the prior status so the orchestrator has the recorded
    git_url, branch, and git_commit_hash without re-reading S3.

    run() is a linear sequence of step methods. Each step method:
      (a) writes its own in_progress → completed | failed transitions to
          status.json via codekb_service._update_pipeline_status,
      (b) returns a typed result on success,
      (c) raises RefreshFailed on failure.

    run() catches RefreshFailed and writes a final FAILED status with
    status.error = "[refresh] <step>: <detail>", leaving the canonical
    knowledge base and vectors untouched.
    """

    def __init__(
        self,
        space_id: str,
        prior_status: dict[str, Any],
        *,
        kb_manager: "KnowledgeBaseManager",
        vectors_client: "S3VectorsClient",
        codekb_service: "CodeKBService",
        git_ops: "git_ops_module | None" = None,
        git_env: dict[str, Any] | None = None,
    ) -> None:
        self.space_id = space_id
        self.prior_status = prior_status
        self.kb_manager = kb_manager
        self.vectors_client = vectors_client
        self.codekb_service = codekb_service
        # Master switch for the business-behavior feature, carried per-space in
        # status.json. When off, the heuristic analyzer is not wired during
        # refresh so re-parsed components keep an empty business_behavior block
        # (and stay omitted from the stored KB), matching the original analysis.
        self._business_behavior_enabled = bool(
            prior_status.get("omit_low_business_relevance", False)
        )
        # Host-scoped git credential env for the refreshing user (or None for
        # the credential-free base env). Passed to every git_ops network call.
        self.git_env = git_env

        # Allow injection for testing; default to the real module
        if git_ops is None:
            from . import git_ops as _git_ops

            self.git_ops = _git_ops
        else:
            self.git_ops = git_ops

        # Pipeline state populated by steps
        self._clone_dir: Path | None = None
        self._new_commit_hash: str | None = None
        self._prior_commit_hash: str | None = prior_status.get("git_commit_hash")
        self._file_changes: list[FileChange] = []
        self._parsed_components: list[Component] = []
        self._prior_kb: KnowledgeBase | None = None
        self._refresh_plan: RefreshPlan | None = None
        self._merged_components: list[Component] = []

        self._parse_failure_reports: list[ParseFailureReport] = []
        # What the KB was committed with, what is analysable now, and the
        # comparison's outcome. Recorded only where the KB swap succeeded
        self._recorded_language_set: list[str] | None = None
        self._current_language_set: list[str] = []
        self._language_set_case: str = "unchanged"
        self._language_upgrade_languages: list[str] = []
        # Files injected as ADDED for newly readable languages. Counted apart
        # from the diff's own added files so the reported added-file figure is
        # not silently inflated.
        self._injected_added_paths: frozenset[str] = frozenset()

        self._dart_qualifier: Any | None = None
        # Part-related files injected as MODIFIED so a parent's changed `part`
        # directives re-key the part files they govern. Counted apart from the
        # diff's own modified files: the commit range did not modify them.
        self._injected_part_paths: frozenset[str] = frozenset()
        self._generated_files_excluded: dict[str, int] = {}
        self._generated_file_paths_excluded: set[str] = set()
        self._preloaded_prior_kb: KnowledgeBase | None = None

        # Descriptions freshly generated during summarize_changed, keyed by
        # component ID. Their count is this run's vector upserts.
        self._summarized_descriptions: dict[str, str] = {}

        # Result tracking
        self._vectors_upserted: int = 0
        self._vectors_deleted: int = 0
        self._vector_errors: list[str] = []
        self._started_at: float = time.perf_counter()
        # Set once the KB swap succeeds; after that the canonical KB holds this run's components.
        self._kb_committed: bool = False
        # Removed functions and methods; classes and fields never had vectors.
        self._removed_callables: frozenset[str] = frozenset()
        # Components that may be re-described as stale callers.
        self._stale_candidates: frozenset[str] = frozenset()
        # Relevance tier of each prior component, before re-scoring.
        self._prior_tiers: dict[str, str | None] = {}
        # Unchanged components re-scored out of the low relevance tier.
        self._promoted_ids: frozenset[str] = frozenset()
        # Prior components re-scored into the low tier, and those of them with a vector.
        self._demoted_ids: frozenset[str] = frozenset()
        self._demoted_with_vectors: frozenset[str] = frozenset()
        # Changed files the parser couldn't read or parse; a full index leaves them out.
        self._parse_dropped_paths: frozenset[str] = frozenset()
        # Set by commit_kb once the read caches are cleared after the swap.
        self._caches_dropped: bool = False
        self._refresh_errors: dict[str, str] = {}
        self._summarization_skipped: bool = False

        # Bedrock token consumption captured during summarize_changed. Persisted
        # as the recurring space_refresh cost record on the terminal status and
        # surfaced in the "Token consumption" UI section.
        self._refresh_token_usage: dict[str, Any] | None = None
        self._refresh_region: str | None = None

    def run(self) -> RefreshResult:
        """Execute the full refresh pipeline. Synchronous; called from background thread.

        Returns:
            RefreshResult summarizing the outcome of the refresh.

        The pipeline steps are executed in order:
            1. clone — re-clone the repo at HEAD
            2. remediate_depth — ensure prior commit is reachable
            3. refresh_diff — compute file-level changes
            4. parse_changed — parse components from added/modified files
            5. merge — merge preserved + fresh components, compute stale callers
            6. rebuild_call_graph — rebuild call graph from merged set
            7. summarize_changed — re-summarize changed + stale callers, writing vectors
            8. vector_deletes — delete vectors for removed components
            9. stage_kb — write staging artifacts to S3
            10. commit_kb — swap staging → canonical
            11. invalidate_caches — clear agent context cache
            12. mark_overviews_stale — flag overview docs stale; write git_commit_hash

        If any step raises, the space ends REFRESH_FAILED and the next refresh
        repeats the same diff.
        """
        self._started_at = time.perf_counter()

        logger.info(
            "refresh.start",
            extra={
                "space_id": self.space_id,
                "prior_commit_hash": self.prior_status.get("git_commit_hash"),
            },
        )

        try:
            self._run_steps()
        except RefreshFailed as exc:
            error_msg = f"[refresh] {exc.step}: {exc.reason}"
            if exc.detail:
                error_msg += f" — {exc.detail}"
            return self._end_failed(exc.step, exc.reason, error_msg)
        except Exception as exc:
            # Same ending as RefreshFailed, so the step shows failed and spent tokens are counted.
            logger.error("refresh.unexpected_error", extra={"space_id": self.space_id}, exc_info=True)
            step = (self.codekb_service._read_status(self.space_id) or {}).get("current_step")
            step = step or "unknown"
            error_msg = f"[refresh] {step}: unexpected error — {exc}"
            return self._end_failed(step, "unexpected_error", error_msg)
        finally:
            if self._clone_dir is not None:
                discard_directory(self._clone_dir)

        duration_s = time.perf_counter() - self._started_at

        logger.info(
            "refresh.end",
            extra={
                "space_id": self.space_id,
                "status": "READY",
                "duration_s": round(duration_s, 2),
            },
        )

        return self._result(duration_s)

    def _run_steps(self) -> None:
        """Execute all pipeline steps in sequence.

        Each step is responsible for writing its own status transitions.
        Raises RefreshFailed if any step cannot complete.

        Vectors are written first, then the KB, then git_commit_hash in the final
        status. Until that last write the recorded commit is the prior one, so after
        a failure at any step the next refresh repeats the same diff and finishes
        the work. A failed run can leave some vectors ahead of the KB until then.
        """
        # Step 1: Re-clone the repo at HEAD to get the latest source tree.
        # Needed because we don't keep prior clones around between analyses.
        self._step_clone()
        # A same-commit diff is empty.
        same_commit = self._new_commit_hash == self._prior_commit_hash

        if not same_commit:
            # Step 2: Make the prior commit reachable in the shallow clone.
            # Required so git diff can compare the two tree states. The shallow
            # clone only has HEAD; we need the prior SHA too.
            self._step_remediate_depth()

            # Step 3: Compute file-level changes (added/modified/removed) between
            # the prior indexed commit and the new HEAD. This is the input that
            # determines what work the rest of the pipeline does.
            self._step_refresh_diff()

        # A grown language set (or no record) injects the newly readable
        # languages' files into the diff's ADDED set, so the existing pipeline
        # reprocesses them. Runs before parse_changed, which parses them.
        self._step_language_set()

        # Nothing to update unless a grammar was added or a failed run's writes need redoing.
        if (
            same_commit
            and not self._injected_added_paths
            and self.prior_status.get("status") != "REFRESH_FAILED"
        ):
            self._finish_unchanged()
            return

        # Step 4: Parse components from added/modified files. Unchanged files
        # are skipped entirely — their components are preserved from the prior KB.
        self._step_parse_changed()

        # Step 5: Merge preserved components (unchanged files) with freshly
        # parsed components. Compute stale callers via BFS over the reverse
        # call graph — these are preserved components whose summaries are
        # outdated because a callee changed.
        self._step_merge()

        # Step 6: Rebuild the call graph from the merged component set.
        # The old call graph is invalid because components were added/removed.
        self._step_rebuild_call_graph()

        # Step 7: Re-invoke Bedrock for changed components + stale callers, writing
        # each description to the vector index. Skipped if creation ran no summarization.
        self._step_summarize_changed()

        # Step 8: Delete vectors for removed components. Partial failures are
        # non-fatal (recorded in refresh_errors).
        self._step_vector_deletes()

        # Step 9: Write knowledge_base.json.staging and source.tar.gz.staging
        # to S3. Nothing canonical is overwritten yet — these are sibling keys.
        self._step_stage_kb()

        # Step 10: Swap staging → canonical for source.tar.gz and knowledge_base.json.
        self._step_commit_kb()

        # Step 11: Clear the agent's per-Space context cache so subsequent
        # queries rebuild from the refreshed KB.
        self._step_invalidate_caches()

        # Step 12: Flag existing overview docs as stale and write the final
        # READY status, the only write of the new git_commit_hash.
        self._step_mark_overviews_stale()

    # ── Step implementations ───────────────────────────────────────────

    def _step_clone(self) -> None:
        """Clone the repository at HEAD and record the new commit hash.

        Uses git_ops.clone_shallow to re-clone the Space's recorded git_url
        at its recorded branch. Records the new HEAD SHA for later diff.

        Raises:
            RefreshFailed: If the clone fails (network error, bad URL, timeout).
        """
        git_url = self.prior_status.get("git_url", "")
        branch = self.prior_status.get("branch") or None

        # Re-validate the stored URL before re-cloning. It passed validation when
        # the space was created, but DNS can change in between: an allowlisted
        # host that resolved to a public address then could later rebind to an
        # internal, loopback or cloud-metadata address. Re-running the full SSRF
        # validation (scheme + host allowlist + post-resolution address guard)
        # here resolves-and-rejects on every refresh clone, not just the first.
        from ..service.codekb_service import ServiceError, validate_git_url

        try:
            git_url = validate_git_url(git_url)
        except ServiceError as exc:
            logger.error(
                "refresh.clone.url_rejected",
                extra={
                    "space_id": self.space_id,
                    "git_url": redact_git_url(git_url),
                    "error": str(exc),
                },
            )
            raise RefreshFailed(
                step="clone",
                reason="url_rejected",
                detail=f"stored git_url failed re-validation before refresh: {exc}",
            )

        self.codekb_service._update_pipeline_status(self.space_id, "REFRESHING", "clone", "in_progress")

        # Prepare a clean clone directory. The "-refresh" suffix puts it outside the
        # source cache's eviction namespace, so nothing reclaims it mid-run; run()'s
        # finally returns it. Asking the provider for the path keeps that naming in
        # one place rather than rebuilt from a literal here.
        from ..service.source_provider import WORK_DIR_REFRESH

        clone_dir = self.codekb_service.source_provider.work_path(self.space_id, WORK_DIR_REFRESH)
        discard_directory(clone_dir)
        self._clone_dir = clone_dir

        # One of the two points where the local cache grows. make_room only enforces
        # the size ceiling against existing cache entries — the incoming clone's size
        # isn't known yet — so peak disk can still exceed the ceiling by one repo.
        # Free disk space is deliberately not consulted. Never raises.
        self.codekb_service.source_provider.make_room(incoming_space_id=self.space_id)

        try:
            new_commit_hash = self.git_ops.clone_shallow(
                git_url, branch, clone_dir, env=self.git_env
            )
        except GitCommandError as exc:
            logger.error(
                "refresh.clone.failed",
                extra={
                    "space_id": self.space_id,
                    "git_url": redact_git_url(git_url),
                    "branch": branch,
                    "error": str(exc),
                },
            )
            raise RefreshFailed(
                step="clone",
                reason="clone_failed",
                detail=f"git clone failed: {exc.stderr or str(exc)}",
            )
        except subprocess.TimeoutExpired:
            logger.error(
                "refresh.clone.timeout",
                extra={
                    "space_id": self.space_id,
                    "git_url": redact_git_url(git_url),
                    "branch": branch,
                },
            )
            raise RefreshFailed(
                step="clone",
                reason="clone_timeout",
                detail="git clone timed out",
            )

        # Kept in memory only: status.json gets it once the refresh has fully applied.
        self._new_commit_hash = new_commit_hash

        self.codekb_service._update_pipeline_status(
            self.space_id, "REFRESHING", "clone", "completed"
        )

        logger.info(
            "refresh.clone.done",
            extra={
                "space_id": self.space_id,
                "new_commit_hash": new_commit_hash,
                "git_url": redact_git_url(git_url),
                "branch": branch,
            },
        )

    def _step_remediate_depth(self) -> None:
        """Fetch the last indexed commit into the shallow clone so refresh_diff can use it.

        Raises:
            RefreshFailed: If no prior commit is recorded or it can't be fetched.
        """
        self.codekb_service._update_pipeline_status(
            self.space_id, "REFRESHING", "remediate_depth", "in_progress"
        )

        prior_sha = self._prior_commit_hash
        clone_dir = self._clone_dir
        assert clone_dir is not None, "clone step must run before remediate_depth"

        if not prior_sha:
            raise RefreshFailed(
                step="remediate_depth",
                reason="no_prior_commit",
                detail=(
                    "Space has no prior git_commit_hash; space refresh requires a prior commit. "
                    "Delete this Space and create it again to re-index it from scratch."
                ),
            )

        try:
            self.git_ops.fetch_specific_sha(clone_dir, "origin", prior_sha, env=self.git_env)
        except GitCommandError as exc:
            logger.error(
                "refresh.remediate_depth.failed",
                extra={"space_id": self.space_id, "prior_commit_hash": prior_sha, "error": str(exc)},
            )
            if "unadvertised object" in (exc.stderr or ""):
                # Protocol v0/v1 servers refuse fetching by SHA unless configured to allow it.
                remedy = (
                    "The git server refuses to fetch a commit by SHA. Allow it on the server "
                    "(uploadpack.allowReachableSHA1InWant) or enable git protocol v2."
                )
            else:
                remedy = "If the branch history was rewritten, delete this Space and create it again."
            raise RefreshFailed(
                step="remediate_depth",
                reason="prior_commit_unavailable",
                detail=(
                    f"Could not fetch the last indexed commit {prior_sha[:12]}: "
                    f"{exc.stderr or exc}. {remedy}"
                ),
            )
        except subprocess.TimeoutExpired:
            logger.error(
                "refresh.remediate_depth.timeout",
                extra={"space_id": self.space_id, "prior_commit_hash": prior_sha},
            )
            raise RefreshFailed(
                step="remediate_depth",
                reason="fetch_timeout",
                detail="Fetching the last indexed commit timed out.",
            )

        self.codekb_service._update_pipeline_status(
            self.space_id, "REFRESHING", "remediate_depth", "completed"
        )

    def _step_refresh_diff(self) -> None:
        """Compute file-level changes between the prior and new commits.

        Calls git_ops.diff_name_status to get the list of changed files,
        classifies them by change type, and writes the refresh_diff pipeline
        step with file counts.

        Populates self._file_changes for use by subsequent steps.

        Raises:
            RefreshFailed: If git diff fails (subprocess error, timeout).
        """
        self.codekb_service._update_pipeline_status(
            self.space_id, "REFRESHING", "refresh_diff", "in_progress"
        )

        clone_dir = self._clone_dir
        prior_sha = self._prior_commit_hash
        new_sha = self._new_commit_hash
        assert clone_dir is not None, "clone step must run before refresh_diff"
        assert prior_sha is not None, "remediate_depth step must run before refresh_diff"
        assert new_sha is not None, "clone step must set _new_commit_hash"

        try:
            file_changes = self.git_ops.diff_name_status(clone_dir, prior_sha, new_sha)
        except GitCommandError as exc:
            logger.error(
                "refresh.diff.failed",
                extra={
                    "space_id": self.space_id,
                    "prior_commit_hash": prior_sha,
                    "new_commit_hash": new_sha,
                    "error": str(exc),
                },
            )
            raise RefreshFailed(
                step="refresh_diff",
                reason="diff_failed",
                detail=f"git diff failed: {exc.stderr or str(exc)}",
            )
        except subprocess.TimeoutExpired:
            logger.error(
                "refresh.diff.failed",
                extra={
                    "space_id": self.space_id,
                    "prior_commit_hash": prior_sha,
                    "new_commit_hash": new_sha,
                    "error": "git diff timed out",
                },
            )
            raise RefreshFailed(
                step="refresh_diff",
                reason="diff_failed",
                detail="git diff timed out",
            )

        # A space keeps the exclusion patterns it was created with for its whole
        # life, so filter the diff with those, not with the current config.
        scanner = self._space_scanner()
        file_changes = [fc for fc in file_changes if not scanner.is_excluded_path(fc.path)]

        # Classify files by change type
        added_count = sum(1 for fc in file_changes if fc.change_type == ChangeType.ADDED)
        modified_count = sum(1 for fc in file_changes if fc.change_type == ChangeType.MODIFIED)
        removed_count = sum(1 for fc in file_changes if fc.change_type == ChangeType.REMOVED)

        # Store for subsequent steps
        self._file_changes = file_changes

        # Write pipeline step with file counts
        self.codekb_service._update_pipeline_status(
            self.space_id,
            "REFRESHING",
            "refresh_diff",
            "completed",
            extra={
                "refresh_diff": {
                    "added_files": added_count,
                    "modified_files": modified_count,
                    "removed_files": removed_count,
                    "unchanged_files": 0,  # computed later in merge step when prior KB is loaded
                    "total_changed_files": added_count + modified_count + removed_count,
                },
            },
        )

        logger.info(
            "refresh.diff.done",
            extra={
                "space_id": self.space_id,
                "prior_commit_hash": prior_sha,
                "new_commit_hash": new_sha,
                "added_files": added_count,
                "modified_files": modified_count,
                "removed_files": removed_count,
            },
        )

    # ── Language-set upgrade ───────────────────────────────────

    #: The language set analysable when this space's KB was last committed.
    #: Named distinctly from the status record's existing ``languages`` list,
    #: which is derived from components found and is not touched here.
    LANGUAGE_SET_FIELD = "analysable_language_set"
    LANGUAGE_SET_RECORDED_AT_FIELD = "analysable_language_set_recorded_at"

    def _space_scanner(self, languages: set[str] | None = None) -> FileScanner:
        """A scanner filtered the way this space was created, not the way config is now.

        A space keeps its exclusion patterns for its whole life. A scanner built
        without them silently applies the current defaults, so a scan can select a
        file the committed KB never analysed — which, for the part map, re-keys
        components a full index would leave alone.
        """
        scanner = FileScanner(
            exclude_patterns=self.prior_status.get(
                "exclude_patterns", LEGACY_DEFAULT_EXCLUDE_PATTERNS
            )
        )
        if languages is not None:
            scanner.set_available_languages(languages)
        return scanner

    def _whole_tree_totals(self, clone_dir: Path, components: list[Component]) -> dict[str, Any]:
        """Whole-clone totals, as a full index records them; the parse saw only changed files."""
        languages = self._current_language_set or self._compute_current_language_set()
        scanner = self._space_scanner(set(languages))
        scanned = scanner.scan_directory(clone_dir)
        prior_metadata = self._prior_kb.analysis_metadata if self._prior_kb else None
        reparsed = {fc.path for fc in self._file_changes}
        # Unchanged files keep the prior run's outcome; older spaces have no record, so count all.
        unparsed = {
            path
            for path in (prior_metadata or {}).get("unparsed_files", [])
            if path not in reparsed
        } | self._parse_dropped_paths
        files_analyzed = 0
        total_lines = 0
        detected: set[str] = set()
        for path in scanned:
            if path.relative_to(clone_dir).as_posix() in unparsed:
                continue
            try:
                # Creation's CodeAnalyzer counts lines this way.
                lines = path.read_text(errors="replace").count("\n") + 1
            except OSError:
                # Creation also leaves out a file it can't read.
                continue
            files_analyzed += 1
            total_lines += lines
            language = scanner.detect_language(path)
            if language:
                detected.add(language)
        skipped = dict(scanner.skipped_files)
        return {
            "files_analyzed": files_analyzed,
            "total_components": len(components),
            "total_lines": total_lines,
            "languages_detected": sorted(detected),
            "skipped_files": skipped,
            "skipped_file_count": sum(skipped.values()),
            "unparsed_files": sorted(unparsed),
        }

    def _step_language_set(self) -> None:
        """Compare the recorded analysable language set with the current one.

        Unchanged or smaller: proceed and record nothing. Larger or absent: inject the
        newly readable languages' files as ADDED, disjoint from the diff's own sets.
        """
        self.codekb_service._update_pipeline_status(
            self.space_id, "REFRESHING", "language_set", "in_progress"
        )

        clone_dir = self._clone_dir
        assert clone_dir is not None, "clone step must run before language_set"

        current = self._compute_current_language_set()
        self._current_language_set = current
        recorded = self._read_recorded_language_set()
        self._recorded_language_set = recorded

        current_set = set(current)
        if recorded is None:
            # The space predates this record. The absent-language set needs the
            # prior KB, which merge loads much later; load it here and hand the object
            # to merge, so merge still sees exactly the KB it would have imported.
            self._language_set_case = "no_record"
            prior_languages = self._prior_kb_languages()
            if prior_languages is None:
                # The prior KB could not be read, so which languages are absent is
                # unknown — and unknown is not 'all of them'. Reconcile nothing; merge
                # raises the canonical failure and the next refresh retries.
                languages = []
            else:
                languages = sorted(current_set - prior_languages)
        else:
            recorded_set = set(recorded)
            if current_set == recorded_set:
                self._language_set_case = "unchanged"
                languages = []
            elif current_set > recorded_set:
                self._language_set_case = "larger"
                languages = sorted(current_set - recorded_set)
            elif current_set < recorded_set:
                self._language_set_case = "smaller"
                languages = []
            else:
                # Neither a superset nor a subset: something was gained and
                # something lost. The gain is an upgrade obligation; the loss
                # is not recorded, exactly as the shrunken case.
                self._language_set_case = "mixed"
                languages = sorted(current_set - recorded_set)

        injected: list[str] = []
        per_language_counts: dict[str, int] = {}
        if languages:
            injected, per_language_counts = self._select_language_upgrade_files(
                clone_dir, languages
            )

        self._language_upgrade_languages = languages
        self._injected_added_paths = frozenset(injected)
        self._file_changes = list(self._file_changes) + [
            FileChange(path, ChangeType.ADDED) for path in injected
        ]

        report = {
            "case": self._language_set_case,
            "recorded_languages": recorded,
            "current_languages": current,
            "reprocessed_languages": languages,
            "files_by_language": per_language_counts,
            "files_injected": len(injected),
        }

        self.codekb_service._update_pipeline_status(
            self.space_id,
            "REFRESHING",
            "language_set",
            "completed",
            extra={"language_set": report},
        )

        # A refresh that grew explains itself, so a run that takes much longer
        # than a normal delta is explained rather than surprising.
        if injected:
            logger.info(
                "refresh.language_set.upgrade",
                extra={"space_id": self.space_id, **report},
            )
        else:
            logger.info(
                "refresh.language_set.done",
                extra={"space_id": self.space_id, **report},
            )

    def _compute_current_language_set(self) -> list[str]:
        """The sorted language keys both configured and backed by a grammar.

        ``TreeSitterWrapper`` only creates a parser for a configured language
        whose grammar loaded, so its parser keys are exactly that set. Sorted
        on the way out so two records compare by value.
        """
        from ..core.tree_sitter_wrapper import TreeSitterWrapper

        try:
            return sorted(TreeSitterWrapper().get_supported_languages())
        except Exception as exc:
            # A wrapper that cannot initialise at all leaves the set empty,
            # which reads as "smaller" and therefore changes nothing — never
            # as an upgrade obligation, and never a failed refresh.
            logger.warning(
                "refresh.language_set.wrapper_unavailable",
                extra={"space_id": self.space_id, "error": str(exc)},
            )
            return []

    def _read_recorded_language_set(self) -> list[str] | None:
        """Read the recorded set from the space's status, or None if absent.

        A malformed value is treated as absent, which routes to the reconciling
        path — bounded, and it rewrites the record at the next commit.
        """
        raw = self.prior_status.get(self.LANGUAGE_SET_FIELD)
        if raw is None:
            return None
        if not isinstance(raw, (list, tuple, set, frozenset)):
            logger.warning(
                "refresh.language_set.malformed_record",
                extra={"space_id": self.space_id, "recorded": str(raw)[:200]},
            )
            return None
        languages = [str(item) for item in raw if isinstance(item, str) and item.strip()]
        if len(languages) != len(list(raw)):
            logger.warning(
                "refresh.language_set.malformed_record",
                extra={"space_id": self.space_id, "recorded": str(raw)[:200]},
            )
            return None
        return sorted(languages)

    def _prior_kb_languages(self) -> set[str] | None:
        """Languages that have components in the prior knowledge base.

        Loaded early and kept for merge to reuse. A load failure returns None, meaning
        unknown rather than "all absent"; an empty set is the different, real answer.
        """
        try:
            prior_kb = self.kb_manager.import_knowledge_base(self.space_id)
        except Exception as exc:
            logger.warning(
                "refresh.language_set.prior_kb_unavailable",
                extra={"space_id": self.space_id, "error": str(exc)},
            )
            return None

        if prior_kb is None:
            logger.warning(
                "refresh.language_set.prior_kb_missing",
                extra={"space_id": self.space_id},
            )
            return None

        self._preloaded_prior_kb = prior_kb
        return {c.language for c in prior_kb.components if getattr(c, "language", None)}

    def _ensure_prior_kb(self) -> KnowledgeBase | None:
        """Return the space's prior KB, importing and caching it once.

        Shared by parse_changed (which needs the recorded id scheme before
        regenerating ids) and merge (which needs the full prior KB), so the KB
        is imported from S3 at most once per refresh. Returns None if it cannot
        be loaded — merge raises on that, while parse_changed falls back to the
        legacy scheme.
        """
        if self._preloaded_prior_kb is None:
            try:
                self._preloaded_prior_kb = self.kb_manager.import_knowledge_base(self.space_id)
            except Exception as exc:
                logger.warning(
                    "refresh.prior_kb_unavailable",
                    extra={"space_id": self.space_id, "error": str(exc)},
                )
                return None
        return self._preloaded_prior_kb

    def _select_language_upgrade_files(
        self, clone_dir: Path, languages: list[str]
    ) -> tuple[list[str], dict[str, int]]:
        """List the clone's files of each named language, through the scanner.

        Through the space's own scanner so its recorded exclusions and limits apply.
        Files the diff already selected are subtracted, keeping the merge's sets
        disjoint.
        """
        already_selected = {fc.path for fc in self._file_changes}

        scanner = self._space_scanner(set(self._current_language_set))
        try:
            scanned = scanner.scan_directory(clone_dir)
        except Exception as exc:
            # Nothing of the normal delta depends on this listing, so a scan
            # that cannot run leaves the refresh to proceed as an ordinary
            # delta; the record stays as it was and the next refresh retries.
            logger.warning(
                "refresh.language_set.scan_failed",
                extra={"space_id": self.space_id, "error": str(exc)},
            )
            return [], {}

        wanted = set(languages)
        selected: list[str] = []
        counts: dict[str, int] = {language: 0 for language in languages}
        for file_path in scanned:
            language = scanner.detect_language(file_path)
            if language not in wanted:
                continue
            try:
                rel_path = str(file_path.relative_to(clone_dir))
            except ValueError:
                continue
            if rel_path in already_selected:
                continue
            selected.append(rel_path)
            counts[language] += 1

        return selected, counts

    # ── Generated-file exclusion on the delta path ─────────────────────

    def _partition_generated_files(
        self, clone_dir: Path, candidates: list[str]
    ) -> tuple[list[str], dict[str, int], set[str]]:
        """Split the changed set into analysable files and generated ones.

        A full index never analyses a generated file; the delta path used to, so the
        two disagreed on what is analysable. Not identity-neutral: an excluded file is
        never prefixed, so two `state.g.dart` under different libraries both became
        `state.g.Store` and one description stood for two files.
        """
        if not candidates:
            return [], {}, set()

        languages = self._current_language_set or self._compute_current_language_set()
        scanner = self._space_scanner(set(languages))

        kept: list[str] = []
        excluded: dict[str, int] = {}
        excluded_paths: set[str] = set()
        for rel_path in candidates:
            language = scanner.generated_language_for(clone_dir / rel_path)
            if language:
                excluded[language] = excluded.get(language, 0) + 1
                excluded_paths.add(rel_path)
                continue
            kept.append(rel_path)

        return kept, excluded, excluded_paths

    # ── Dart split-library qualification on the delta path ─────────────

    def _changed_dart_paths(self) -> list[str]:
        """Repo-relative paths of the Dart files this refresh touched.

        Every change type counts, removals included: a removed parent file
        orphans the part files it used to claim, which re-keys them just as
        surely as adding a directive does.
        """
        return [
            fc.path for fc in self._file_changes if Path(fc.path).suffix.lower() == _DART_SUFFIX
        ]

    def _prepare_dart_library_qualification(self, clone_dir: Path) -> dict[str, Any] | None:
        """Build the Dart library qualifier and widen the changed set for it.

        Without this a refresh wrote part-file IDs back UNPREFIXED and the catalog
        dropped the collision — 540 identities on flutter/packages. The map needs the
        whole Dart set, so the clone is re-listed through FileScanner with a full
        index's exclusions; a changed parent's part files join as MODIFIED.
        """
        if not self._changed_dart_paths():
            return None

        languages = self._current_language_set or self._compute_current_language_set()
        scanner = self._space_scanner(set(languages))
        try:
            scanned = scanner.scan_directory(clone_dir)
        except Exception as exc:
            # The delta itself does not depend on this listing, so a scan that
            # cannot run leaves the refresh to proceed as an ordinary delta
            # rather than failing it. The part files it would have
            # re-keyed keep their prior identities, which is the defect this
            # method exists to fix — so it is logged as a warning, not debug.
            logger.warning(
                "refresh.dart_libraries.scan_failed",
                extra={"space_id": self.space_id, "error": str(exc)},
            )
            return {"scanned": False, "reason": "scan_failed"}

        dart_files = [f for f in scanned if f.suffix.lower() == _DART_SUFFIX]
        if not dart_files:
            # A Dart file changed but the scan selects none of them — every one
            # was excluded, generated or oversized. Nothing to qualify, and the
            # second gate mirrors `analyze_directory`'s `has_dart_files`.
            return {"scanned": True, "dart_files": 0}

        from .dart_libraries import DartLibraryQualifier

        qualifier = DartLibraryQualifier(clone_dir, dart_files)
        self._dart_qualifier = qualifier

        analysed_relative: set[str] = set()
        for file_path in dart_files:
            try:
                analysed_relative.add(str(file_path.relative_to(clone_dir)))
            except ValueError:
                continue

        already_selected = {fc.path for fc in self._file_changes}
        candidates = set(qualifier.qualified_part_paths)
        candidates.update(record.part_path for record in qualifier.unresolved_records)
        injected = sorted((candidates & analysed_relative) - already_selected)

        self._injected_part_paths = frozenset(injected)
        self._file_changes = list(self._file_changes) + [
            FileChange(path, ChangeType.MODIFIED) for path in injected
        ]

        report = {
            "scanned": True,
            "dart_files": len(dart_files),
            "changed_dart_files": len(self._changed_dart_paths()) - len(injected),
            "active": qualifier.active,
            "qualified_part_files": len(qualifier.qualified_part_paths),
            "unresolved_parts": len(qualifier.unresolved_records),
            "files_reprocessed_for_part_libraries": len(injected),
        }

        if injected:
            # A refresh that reprocesses files the commit did not touch explains
            # itself, so a run larger than the diff is explained rather than
            # surprising.
            logger.info(
                "refresh.dart_libraries.widened",
                extra={"space_id": self.space_id, **report},
            )
        else:
            logger.info(
                "refresh.dart_libraries.done",
                extra={"space_id": self.space_id, **report},
            )
        return report

    def _step_parse_changed(self) -> None:
        """Parse components from all ADDED and MODIFIED files.

        Runs creation's CodeAnalyzer.analyze_directory on the changed set
        (added_files ∪ modified_files) and stores the components in
        self._parsed_components for the merge step. Files creation would skip
        (unsupported language, over the size cap, unparseable) are skipped here
        too; they do not fail the pipeline.

        Raises:
            RefreshFailed: If a critical error prevents parsing from completing.
        """
        self.codekb_service._update_pipeline_status(
            self.space_id, "REFRESHING", "parse_changed", "in_progress"
        )

        clone_dir = self._clone_dir
        assert clone_dir is not None, "clone step must run before parse_changed"

        # Dart split libraries, before the changed set is read: a part file's
        # library prefix is a cross-file fact the changed set cannot supply, and a
        # changed parent re-keys part files the diff never mentions. Builds the
        # qualifier over the whole clone and widens self._file_changes, so the
        # selection below and _step_merge both see the widened set.
        dart_report = self._prepare_dart_library_qualification(clone_dir)

        # Collect files to parse: ADDED and MODIFIED only
        files_to_parse: list[str] = [
            fc.path
            for fc in self._file_changes
            if fc.change_type in (ChangeType.ADDED, ChangeType.MODIFIED)
        ]

        # Generated files leave the run here, before any parser sees one, so this
        # path and the full index agree on which files are analysable. Their prior
        # components are still evicted by _step_merge — the diff named them — which
        # is the convergence we want: a touched generated file loses what a
        # pre-exclusion index left behind, landing where a full index would.
        files_to_parse, generated_excluded, generated_paths = self._partition_generated_files(
            clone_dir, files_to_parse
        )
        self._generated_files_excluded = generated_excluded
        self._generated_file_paths_excluded = generated_paths
        generated_total = sum(generated_excluded.values())
        if generated_excluded:
            logger.info(
                "refresh.parse_changed.generated_excluded",
                extra={
                    "space_id": self.space_id,
                    "files_excluded_generated": generated_total,
                    "generated_files_excluded": generated_excluded,
                },
            )

        # Counted as excluded, never as a skip or a failure: three conditions with
        # three causes. Absent from the payload when nothing was excluded,
        # matching the scanner's own silent-when-empty reporting.
        generated_extra: dict[str, Any] = (
            {
                "files_excluded_generated": generated_total,
                "generated_files_excluded": generated_excluded,
            }
            if generated_excluded
            else {}
        )

        if not files_to_parse:
            # No files to parse — this is valid (e.g. only removals)
            self._parsed_components = []
            self.codekb_service._update_pipeline_status(
                self.space_id,
                "REFRESHING",
                "parse_changed",
                "completed",
                extra={
                    "files_parsed": 0,
                    "components_extracted": 0,
                    "files_skipped": 0,
                    **generated_extra,
                    **({"dart_libraries": dart_report} if dart_report is not None else {}),
                },
            )
            logger.info(
                "refresh.parse_changed.done",
                extra={
                    "space_id": self.space_id,
                    "files_parsed": 0,
                    "components_extracted": 0,
                    "files_skipped": 0,
                },
            )
            return

        from ..catalog.component_catalog import ComponentCatalog, SimpleBusinessBehaviorAnalyzer
        from .code_analyzer import CodeAnalyzer

        catalog = ComponentCatalog(
            SimpleBusinessBehaviorAnalyzer() if self._business_behavior_enabled else None
        )
        # The space's own exclusion patterns, as refresh_diff uses; they also scope the Ruby app scan.
        analyzer = CodeAnalyzer(
            catalog,
            exclude_patterns=self.prior_status.get(
                "exclude_patterns", LEGACY_DEFAULT_EXCLUDE_PATTERNS
            ),
        )
        # Regenerate ids under the scheme this space was originally indexed with
        # (resolved from the persisted KB) so freshly parsed ids match the prior
        # ids and the merge stays stable. Existing (pre-scheme) spaces resolve to
        # the legacy V1 scheme; new spaces were indexed under DEFAULT_SCHEME and
        # carry that marker. Root is the clone dir so V2 path hashes are
        # repo-relative and identical to the original full index.
        prior_kb = self._ensure_prior_kb()
        scheme = resolve_id_scheme(prior_kb.id_scheme if prior_kb else None)
        kb = analyzer.analyze_directory(
            clone_dir,
            self.space_id,
            id_scheme=scheme,
            files=[clone_dir / path for path in files_to_parse],
            dart_qualifier=self._dart_qualifier,
        )


        failure_reports = analyzer.take_failure_reports()
        # Paths that reached the parser and came back, for the widening check below.
        parsed_paths = {str(report.file_path) for report in failure_reports}
        self._parse_dropped_paths = frozenset(kb.analysis_metadata.get("unparsed_files", []))

        parsed_components: list[Component] = kb.components
        for component in parsed_components:
            # Repo-relative while the refresh runs; stage_kb stores them under the source root.
            component.file_path = component.file_path.relative_to(clone_dir)
        files_parsed = kb.analysis_metadata["files_analyzed"]
        files_skipped = len(files_to_parse) - files_parsed
        parse_errors = kb.analysis_metadata["analysis_errors"]

        # The widening's precondition, checked not assumed: an injected part file
        # is here only because its identity must be rewritten, and _step_merge
        # evicts its components on that strength. Never reaching the parser means
        # eviction with no replacement. The condition is 'parsed and came back',
        # not 'produced components' — `part of 'x.dart';` alone yields none.
        unparsed_injected = sorted(
            # The excluded PATHS, not the per-language counts: an injected part
            # dropped as generated never reaches the parser by design.
            self._injected_part_paths - parsed_paths - self._generated_file_paths_excluded
        )
        if unparsed_injected:
            logger.error(
                "refresh.parse_changed.widened_file_unparsed",
                extra={
                    "space_id": self.space_id,
                    "files": unparsed_injected[:20],
                    "count": len(unparsed_injected),
                },
            )
            raise RefreshFailed(
                step="parse_changed",
                reason="widened_file_unparsed",
                detail=(
                    f"{len(unparsed_injected)} file(s) reprocessed for Dart split-library "
                    f"re-keying produced no parse result: {', '.join(unparsed_injected[:10])}. "
                    "The commit range did not touch them, so evicting their components with "
                    "nothing to replace them would remove them from search. The clone changed "
                    "under the refresh; retry it."
                ),
            )

        # Compute source hashes for the freshly-parsed components so the merge
        # step can compare them against the prior KB's hashes (method-level
        # change detection). Paths are repo-relative here, resolved vs the clone.
        from .content_hash import backfill_component_hashes

        backfill_component_hashes(clone_dir, parsed_components)

        self._parsed_components = parsed_components
        self._parse_failure_reports = failure_reports

        from .code_analyzer import PARSE_FAILURE_SUMMARY_KEY, collect_parse_failure_metadata

        failure_metadata = collect_parse_failure_metadata(failure_reports)
        failure_summary = failure_metadata[PARSE_FAILURE_SUMMARY_KEY]

        self.codekb_service._update_pipeline_status(
            self.space_id,
            "REFRESHING",
            "parse_changed",
            "completed",
            extra={
                "files_parsed": files_parsed,
                "components_extracted": len(parsed_components),
                "files_skipped": files_skipped,
                # Visible per run, not only inside the staged KB.
                "parse_failure_summary": failure_summary,
                **generated_extra,
                **({"dart_libraries": dart_report} if dart_report is not None else {}),
            },
        )

        logger.info(
            "refresh.parse_changed.done",
            extra={
                "space_id": self.space_id,
                "files_parsed": files_parsed,
                "components_extracted": len(parsed_components),
                "files_skipped": files_skipped,
                "files_excluded_generated": generated_total,
                "parse_errors": len(parse_errors),
                "files_with_defects": failure_summary["files_with_defects"],
                "error_nodes": failure_summary["error_node_count"],
                "missing_nodes": failure_summary["missing_node_count"],
            },
        )

    def _step_merge(self) -> None:
        """Merge preserved components with freshly parsed components and compute stale callers.

        This step:
          1. Loads the prior knowledge base from S3.
          2. Classifies all prior KB files as unchanged (those not in the diff).
          3. Computes preserved_component_ids (components from unchanged files).
          4. Computes evicted_component_ids (components from removed + modified files).
          5. Builds the reverse call graph from the prior KB's call graph.
          6. Computes stale_caller_ids: preserved callers within a prompt's call depth
             of a changed or removed function.
          7. Builds the complete RefreshPlan.

        Raises:
            RefreshFailed: If the prior KB cannot be loaded or merge invariants are violated.
        """
        self.codekb_service._update_pipeline_status(self.space_id, "REFRESHING", "merge", "in_progress")

        # ── Load prior knowledge base ──────────────────────────────────────
        # The reconciling path needs the prior KB's languages before file
        # selection, so it loads it and hands the object over rather than importing
        # the same bytes twice. Merge works from exactly the KB it would have read.
        prior_kb = self._preloaded_prior_kb
        if prior_kb is None:
            try:
                prior_kb = self.kb_manager.import_knowledge_base(self.space_id)
            except Exception as exc:
                logger.error(
                    "refresh.merge.kb_load_failed",
                    extra={"space_id": self.space_id, "error": str(exc)},
                )
                raise RefreshFailed(
                    step="merge",
                    reason="kb_load_failed",
                    detail=f"Failed to load prior knowledge base: {exc}",
                )

        if prior_kb is None:
            raise RefreshFailed(
                step="merge",
                reason="kb_not_found",
                detail="Prior knowledge base not found in S3.",
            )

        self._prior_kb = prior_kb

        # ── Classify files ─────────────────────────────────────────────────
        # Files reported by git diff
        added_files: frozenset[str] = frozenset(
            fc.path for fc in self._file_changes if fc.change_type == ChangeType.ADDED
        )
        modified_files: frozenset[str] = frozenset(
            fc.path for fc in self._file_changes if fc.change_type == ChangeType.MODIFIED
        )
        removed_files: frozenset[str] = frozenset(
            fc.path for fc in self._file_changes if fc.change_type == ChangeType.REMOVED
        )

        # All files that were in the prior KB (derive from component file_paths).
        # Prior components carry absolute paths under the source cache root, so
        # normalize to repo-relative to compare against the git-diff paths below.
        prior_files: set[str] = {str(_repo_relative_path(c.file_path)) for c in prior_kb.components}

        # Changed files (union of all diff-reported files)
        changed_files = added_files | modified_files | removed_files

        # Unchanged files: files in the prior KB that are NOT in the diff
        # Note: added_files are new and were NOT in the prior KB, so they
        # should not appear in unchanged_files.
        unchanged_files: frozenset[str] = frozenset(
            f for f in prior_files if f not in changed_files
        )

        # ── Compute component ID sets ─────────────────────────────────────
        # Build a mapping from file_path -> list of component IDs in the prior KB
        prior_components_by_file: dict[str, list[str]] = {}
        for comp in prior_kb.components:
            file_key = str(_repo_relative_path(comp.file_path))
            prior_components_by_file.setdefault(file_key, []).append(comp.id)

        # Preserved: components from unchanged files
        preserved_component_ids: frozenset[str] = frozenset(
            comp_id
            for file_path in unchanged_files
            for comp_id in prior_components_by_file.get(file_path, [])
        )

        # Evicted: components from removed + modified files (their old versions are gone)
        evicted_component_ids: frozenset[str] = frozenset(
            comp_id
            for file_path in (removed_files | modified_files)
            for comp_id in prior_components_by_file.get(file_path, [])
        )

        # Vectors to delete: evicted components that won't be replaced by fresh parses
        # For modified files, some component IDs may reappear in the fresh parse
        # (same function name in the same file). Those will be upserted, not deleted.
        freshly_parsed_ids: frozenset[str] = frozenset(c.id for c in self._parsed_components)
        vectors_to_delete: frozenset[str] = frozenset(
            cid for cid in evicted_component_ids if cid not in freshly_parsed_ids
        )

        # ── Method-level change detection ──────────────────────────────────
        # A freshly-parsed component whose source hash matches its prior hash did
        # NOT actually change — only a sibling in the same file did — so we keep
        # its existing description and vector instead of re-summarizing it. A
        # missing prior hash (legacy space analyzed before this feature) is
        # treated conservatively as "changed".
        prior_by_id = {c.id: c for c in prior_kb.components}
        self._prior_tiers = {
            c.id: c.business_behavior.business_relevance_tier for c in prior_kb.components
        }
        truly_changed: set[str] = set()
        hash_preserved: set[str] = set()
        for comp in self._parsed_components:
            prior = prior_by_id.get(comp.id)
            prior_hash = prior.content_hash if prior is not None else None
            if prior is not None and prior_hash and comp.content_hash == prior_hash:
                hash_preserved.add(comp.id)
                # Carry the prior AI description forward so stage_kb persists it
                # and the component is neither re-summarized nor re-embedded.
                comp.business_behavior = prior.business_behavior
            else:
                truly_changed.add(comp.id)

        # Changed component IDs: only freshly-parsed components that actually changed.
        changed_component_ids: frozenset[str] = frozenset(truly_changed)

        # ── Build reverse call graph from prior KB ─────────────────────────
        prior_reverse_call_graph = self._build_reverse_call_graph(prior_kb.call_graph)

        # ── Compute stale callers via BFS ──────────────────────────────────
        # Seed the closure ONLY from components that are actually summarized —
        # functions and methods. Non-callable components (classes, interfaces,
        # enums, fields) are parsed and stored but never summarized (the call
        # graph and the summarizer only cover FUNCTION/METHOD), so they carry no
        # description that a caller could reference. A class or field changing —
        # e.g. adding seed data to a list field, or a class annotation — must
        # therefore NOT cascade into re-summarizing everything that uses it.
        #
        # A caller becomes stale only because a *callee's description* it saw
        # changed; only callables have descriptions, so only changed/removed
        # callables can make a caller stale.
        #
        # Candidate callers include both unchanged-file components AND
        # hash-preserved components in changed files: any caller of a changed
        # callable may hold a now-stale description and should be re-summarized,
        # regardless of whether its own file changed.
        from ..core.models import ComponentType

        callable_types = (ComponentType.FUNCTION, ComponentType.METHOD)
        changed_callables: frozenset[str] = frozenset(
            c.id
            for c in self._parsed_components
            if c.id in truly_changed and c.type in callable_types
        )
        removed_callables: frozenset[str] = frozenset(
            cid
            for cid in vectors_to_delete
            if prior_by_id.get(cid) is not None and prior_by_id[cid].type in callable_types
        )
        self._removed_callables = removed_callables
        seed: frozenset[str] = changed_callables | removed_callables
        stale_candidates: frozenset[str] = preserved_component_ids | frozenset(hash_preserved)
        self._stale_candidates = stale_candidates

        # Callers further up never had the changed code in their prompt, so their text still holds.
        stale_caller_ids = frozenset(
            self._reachable_within(prior_reverse_call_graph, seed, self._prompt_hops())
            & stale_candidates
        )

        # ── Build RefreshPlan ──────────────────────────────────────────────
        # RefreshPlan.__post_init__ validates disjointness invariants and will
        # raise RefreshFailed(step="merge", reason="invariant_violation") if violated.
        plan = RefreshPlan(
            added_files=added_files,
            modified_files=modified_files,
            removed_files=removed_files,
            unchanged_files=unchanged_files,
            preserved_component_ids=preserved_component_ids,
            evicted_component_ids=evicted_component_ids,
            vectors_to_delete=vectors_to_delete,
            stale_caller_ids=stale_caller_ids,
            # Only functions and methods are summarized, as at creation.
            changed_component_ids=changed_callables,
        )

        self._refresh_plan = plan

        # ── Write status and log ───────────────────────────────────────────
        self.codekb_service._update_pipeline_status(
            self.space_id,
            "REFRESHING",
            "merge",
            "completed",
            extra={
                "preserved_components": len(preserved_component_ids),
                "evicted_components": len(evicted_component_ids),
                "vectors_to_delete": len(vectors_to_delete),
                "stale_callers": len(stale_caller_ids),
                "changed_components": len(changed_component_ids),
                "changed_callables": len(changed_callables),
                "hash_preserved_components": len(hash_preserved),
                "unchanged_files": len(unchanged_files),
                # Back-patch the refresh_diff summary now that the prior KB is
                # loaded and the unchanged-file count is known (it was written
                # as 0 by _step_refresh_diff, which runs before the KB load).
                # Language-upgrade files and part-related Dart files are counted apart
                # from the diff's own added and modified counts: the commit range did not
                # add or modify them, so folding them in would inflate those figures.
                "refresh_diff": {
                    "added_files": len(added_files - self._injected_added_paths),
                    "added_files_for_new_languages": len(added_files & self._injected_added_paths),
                    "modified_files": len(modified_files - self._injected_part_paths),
                    "modified_files_for_part_libraries": len(
                        modified_files & self._injected_part_paths
                    ),
                    "removed_files": len(removed_files),
                    "unchanged_files": len(unchanged_files),
                    "total_changed_files": len(added_files - self._injected_added_paths)
                    + len(modified_files - self._injected_part_paths)
                    + len(removed_files),
                },
            },
        )

        logger.info(
            "refresh.caller_closure.done",
            extra={
                "space_id": self.space_id,
                "preserved_components": len(preserved_component_ids),
                "evicted_components": len(evicted_component_ids),
                "vectors_to_delete": len(vectors_to_delete),
                "stale_callers": len(stale_caller_ids),
                "changed_components": len(changed_component_ids),
                "changed_callables": len(changed_callables),
                "hash_preserved_components": len(hash_preserved),
                "unchanged_files": len(unchanged_files),
                "components_to_summarize": len(plan.components_to_summarize()),
            },
        )

    def _step_rebuild_call_graph(self) -> None:
        """Rebuild the call graph from the merged component set.

        Constructs the merged component set (preserved from unchanged files +
        freshly parsed from added/modified files), then invokes
        FlowAnalyzer.build_call_graph on the merged set. Persists the rebuilt
        call graph into the updated knowledge base.

        Emits a structured log line with merged_component_count, call_edges,
        and duration_s per Requirement 9.7.

        Raises:
            RefreshFailed: If call graph rebuild fails for any reason.
        """
        self.codekb_service._update_pipeline_status(
            self.space_id, "REFRESHING", "rebuild_call_graph", "in_progress"
        )

        prior_kb = self._prior_kb
        plan = self._refresh_plan
        assert prior_kb is not None, "merge step must run before rebuild_call_graph"
        assert plan is not None, "merge step must produce a RefreshPlan"

        # ── Build merged component set ─────────────────────────────────────
        # Preserved: components from unchanged files (still in the prior KB)
        preserved_components = [
            c for c in prior_kb.components if c.id in plan.preserved_component_ids
        ]

        # Merged = preserved + freshly parsed
        merged_components = preserved_components + self._parsed_components

        # ── Rebase file paths to the refresh clone directory ───────────────
        # FlowAnalyzer.build_call_graph re-parses source files to extract call
        # edges. Components loaded from the prior KB may have absolute paths
        # pointing at the original clone directory (which no longer exists) or
        # relative paths. Freshly parsed components have relative paths. We
        # need all paths to resolve to actual files in the refresh clone.
        clone_dir = self._clone_dir
        assert clone_dir is not None, "clone step must set _clone_dir"

        for component in merged_components:
            fp = component.file_path
            # If the path is already absolute and inside the refresh clone, skip.
            if fp.is_absolute() and str(fp).startswith(str(clone_dir)):
                continue
            # Otherwise recover the repo-relative portion (shared with
            # _step_merge) and resolve it against the refresh clone directory.
            component.file_path = clone_dir / _repo_relative_path(fp)

        # ── Rebuild call graph ─────────────────────────────────────────────
        from ..catalog.component_catalog import ComponentCatalog, SimpleBusinessBehaviorAnalyzer
        from ..flow.flow_analyzer import FlowAnalyzer

        catalog = ComponentCatalog(
            SimpleBusinessBehaviorAnalyzer() if self._business_behavior_enabled else None
        )
        for component in merged_components:
            catalog.add_component(component)
        # One component per ID, as creation stores; parsed ones were added last, so they win.
        merged_components = catalog.get_all_components()
        self._merged_components = merged_components

        flow_analyzer = FlowAnalyzer(catalog)

        t0 = time.perf_counter()
        try:
            flow_analyzer.build_call_graph(merged_components)
        except Exception as exc:
            logger.error(
                "refresh.callgraph.failed",
                extra={
                    "space_id": self.space_id,
                    "merged_component_count": len(merged_components),
                    "error": str(exc),
                },
            )
            raise RefreshFailed(
                step="rebuild_call_graph",
                reason="call_graph_build_failed",
                detail=f"FlowAnalyzer.build_call_graph failed: {exc}",
            )
        duration_s = round(time.perf_counter() - t0, 2)

        # A call that only resolves now (a new or retargeted edge) has no prior edge to follow.
        self._widen_stale_callers(prior_kb.call_graph or {}, flow_analyzer._call_graph)

        # ── Persist rebuilt call graph into the KB ─────────────────────────
        # The call graph stored in the KB uses the format:
        #   { caller_id: [(callee_id, line_number), ...] }
        prior_kb.call_graph = flow_analyzer._call_graph

        # ── Backfill content hashes for the whole merged KB ────────────────
        # Freshly-parsed components already have hashes; this fills in the
        # preserved components from unchanged files — including legacy ones
        # (spaces analyzed before content_hash existed) — using their source in
        # the clone. After this refresh every persisted component has a hash, so
        # the next refresh gets full method-level change detection. At this point
        # merged-component paths are clone-absolute; clone_dir is passed as the
        # base only for any that are still relative.
        from .content_hash import backfill_component_hashes

        backfilled = backfill_component_hashes(clone_dir, merged_components, overwrite=False)
        if backfilled:
            logger.info(
                "refresh.rebuild.hash_backfill",
                extra={"space_id": self.space_id, "backfilled": backfilled},
            )

        # ── Restore repo-relative file paths ───────────────────────────────
        # Rebased to absolute for FlowAnalyzer; revert so no clone path is persisted.
        for component in merged_components:
            fp = component.file_path
            if fp.is_absolute() and str(fp).startswith(str(clone_dir)):
                try:
                    component.file_path = fp.relative_to(clone_dir)
                except ValueError:
                    pass  # leave as-is if relative_to fails

        # Update the KB's component list to the merged set
        prior_kb.components = merged_components

        # Before summarization, whose low-relevance skip reads these tiers.
        self._score_business_relevance()

        # ── Compute metrics and log ────────────────────────────────────────
        call_edges = sum(len(calls) for calls in flow_analyzer._call_graph.values())

        self.codekb_service._update_pipeline_status(
            self.space_id,
            "REFRESHING",
            "rebuild_call_graph",
            "completed",
            extra={
                "merged_component_count": len(merged_components),
                "call_edges": call_edges,
                "duration_s": duration_s,
            },
        )

        logger.info(
            "refresh.callgraph.done",
            extra={
                "space_id": self.space_id,
                "merged_component_count": len(merged_components),
                "call_edges": call_edges,
                "duration_s": duration_s,
            },
        )

    def _step_summarize_changed(self) -> None:
        """Re-summarize changed components and stale callers.

        AI descriptions stay in the vector index only: callee context is read
        from it, and nothing is written to the KB. Skipped when space creation
        ran no summarization.

        Raises:
            RefreshFailed: If summarization fails critically.
        """
        plan = self._refresh_plan
        prior_kb = self._prior_kb
        assert plan is not None, "merge step must produce a RefreshPlan"
        assert prior_kb is not None, "merge step must load prior KB"

        if not self._creation_summarized():
            logger.info(
                "refresh.summarize_changed.skipped",
                extra={
                    "space_id": self.space_id,
                    "reason": "no_prior_summaries",
                },
            )
            self._summarization_skipped = True
            self.codekb_service._update_pipeline_status(
                self.space_id,
                "REFRESHING",
                "summarize_changed",
                "skipped",
                extra={"reason": "prior analysis did not include summarization"},
            )
            return

        # If the plan is empty (identical SHA), skip summarization
        if plan.is_empty():
            self.codekb_service._update_pipeline_status(
                self.space_id,
                "REFRESHING",
                "summarize_changed",
                "completed",
                extra={
                    "summarize_total": 0,
                    "summarize_done": 0,
                    "summarize_percent": 0.0,
                },
            )
            return

        plan = self._add_undescribed_promoted(plan)
        target_ids = plan.components_to_summarize()
        if not target_ids:
            self.codekb_service._update_pipeline_status(
                self.space_id,
                "REFRESHING",
                "summarize_changed",
                "completed",
                extra={
                    "summarize_total": 0,
                    "summarize_done": 0,
                    "summarize_percent": 0.0,
                },
            )
            return

        self.codekb_service._update_pipeline_status(
            self.space_id,
            "REFRESHING",
            "summarize_changed",
            "in_progress",
            extra={
                "summarize_total": len(target_ids),
                "summarize_done": 0,
                "summarize_percent": 0.0,
            },
        )

        # Unchanged callees then render as CACHED DESCRIPTION, so they aren't re-described.
        try:
            prior_descriptions = self._load_callee_descriptions(prior_kb, target_ids)
        except Exception as exc:
            logger.error(
                "refresh.summarize_changed.prior_descriptions_failed",
                extra={"space_id": self.space_id, "error": str(exc)},
            )
            # Prompting with raw callee source instead would re-describe every callee.
            raise RefreshFailed(
                step="summarize_changed",
                reason="prior_descriptions_unavailable",
                detail=f"Could not read existing descriptions from the vector index: {exc}",
            )

        # Construct the summarizer
        import time as _time

        from ..vector.topdown_summarizer import TopDownSummarizer

        clone_dir = self._clone_dir
        assert clone_dir is not None

        # Throttle S3 progress writes: at most once every 15 seconds
        _last_progress_write = [0.0]

        def _on_progress(done: int, total: int) -> None:
            now = _time.monotonic()
            if done > 0 and done < total and (now - _last_progress_write[0]) < 15.0:
                return
            _last_progress_write[0] = now
            pct = round((done / total) * 100, 1) if total > 0 else 0.0
            self.codekb_service._update_pipeline_status(
                self.space_id,
                "REFRESHING",
                "summarize_changed",
                "in_progress",
                extra={
                    "summarize_total": total,
                    "summarize_done": done,
                    "summarize_percent": pct,
                },
            )

        summarizer = TopDownSummarizer(
            str(clone_dir),
            self.space_id,
            codekb_service=self.codekb_service,
            on_progress=_on_progress,
            omit_low_business_relevance=self._business_behavior_enabled,
        )

        # Point the service context at the MERGED knowledge base (rebuilt call
        # graph + freshly parsed components) for the duration of summarization.
        # The summarizer resolves every lookup — get_components_by_depth,
        # get_max_depth, get_call_graph_with_source, index_component_description —
        # through codekb_service._load_context(). During a refresh the canonical KB has
        # NOT been swapped yet, so without this injection those lookups would run
        # against the pre-refresh KB: newly added components would be invisible
        # (never summarized or indexed) and the prior call graph would be used
        # instead of the rebuilt one.
        self._inject_merged_context()
        try:
            summary_stats = asyncio.run(
                summarizer.summarize_all(
                    targets=set(target_ids), prior_descriptions=prior_descriptions
                )
            )
        except Exception as exc:
            logger.error(
                "refresh.summarize_changed.failed",
                extra={
                    "space_id": self.space_id,
                    "target_count": len(target_ids),
                    "error": str(exc),
                },
            )
            raise RefreshFailed(
                step="summarize_changed",
                reason="summarization_failed",
                detail=f"Targeted summarization failed: {exc}",
            )
        finally:
            # Restore repo-relative file paths and drop the merged
            # context so readers see the still-canonical prior KB until commit_kb.
            self._rebase_merged_paths(to_absolute=False)
            self.codekb_service.invalidate_context(self.space_id)
            # Recorded on every exit, so a failed run's tokens are still counted.
            self._refresh_token_usage = operation_usage(
                [
                    {
                        "token_usage": summarizer.token_usage.to_dict(),
                        "llm_workload": workload_record(
                            summarizer._llm, summarizer.token_usage, summarizer._bedrock_region
                        ),
                    }
                ]
            )
            self._refresh_region = summarizer._bedrock_region
            self._summarized_descriptions = {
                cid: summarizer._description_cache[cid]
                for cid in summarizer._indexed_cache
                if summarizer._description_cache.get(cid)
            }
            self._vectors_upserted = len(self._summarized_descriptions)

        # Stopped early: some targets still have their prior descriptions. Fail the
        # refresh before the KB is written, so the next one repeats the same diff.
        stop_reason = summary_stats.get("stopped_early")
        if stop_reason:
            raise RefreshFailed(
                step="summarize_changed",
                reason="stopped_early",
                detail=self._stopped_early_remedy(stop_reason),
            )

        # Matches the total the summarizer's progress reported.
        summarized = len(target_ids) - summary_stats.get("skipped_low_business_relevance", 0)
        self.codekb_service._update_pipeline_status(
            self.space_id,
            "REFRESHING",
            "summarize_changed",
            "completed",
            extra={
                "summarize_total": summarized,
                "summarize_done": summarized,
                "summarize_percent": 100.0,
                "total_analyzed": summary_stats.get("total_analyzed", 0),
            },
        )

        logger.info(
            "refresh.summarize_changed.done",
            extra={
                "space_id": self.space_id,
                "target_count": len(target_ids),
                "total_analyzed": summary_stats.get("total_analyzed", 0),
                "descriptions_indexed": len(self._summarized_descriptions),
            },
        )

    def _step_stage_kb(self) -> None:
        """Write knowledge_base.json.staging and source.tar.gz.staging to S3.

        Creates a tarball of the freshly cloned source tree and serializes
        the merged knowledge base, then writes both to staging keys via
        KnowledgeBaseManager._write_staging_kb.

        Raises:
            RefreshFailed: If staging writes fail.
        """
        self.codekb_service._update_pipeline_status(self.space_id, "REFRESHING", "stage_kb", "in_progress")

        prior_kb = self._prior_kb
        clone_dir = self._clone_dir
        assert prior_kb is not None, "merge step must load prior KB"
        assert clone_dir is not None, "clone step must set _clone_dir"

        # The staged KB is built from the prior one, so without this its analysis
        # metadata would describe the previous run. Rows for files this run
        # reparsed or removed are superseded; every other prior row still holds.
        from .code_analyzer import merge_parse_failure_metadata

        superseded = {
            fc.path
            for fc in self._file_changes
            if fc.change_type in (ChangeType.ADDED, ChangeType.MODIFIED, ChangeType.REMOVED)
        }
        if not isinstance(prior_kb.analysis_metadata, dict):
            prior_kb.analysis_metadata = {}
        prior_kb.analysis_metadata.update(
            merge_parse_failure_metadata(
                prior_kb.analysis_metadata,
                self._parse_failure_reports,
                superseded,
            )
        )

        # Ungrouped Dart part relationships travel the same way, as plain data.
        # Replaced outright rather than merged: the qualifier was built over the
        # whole clone, so its records describe the committed tree completely.
        if self._dart_qualifier is not None:
            from .code_analyzer import DART_UNRESOLVED_PARTS_KEY

            prior_kb.analysis_metadata[DART_UNRESOLVED_PARTS_KEY] = [
                record.to_dict() for record in self._dart_qualifier.unresolved_records
            ]

        # The prior totals describe the previous commit.
        try:
            prior_kb.analysis_metadata.update(
                self._whole_tree_totals(clone_dir, prior_kb.components)
            )
        except Exception as exc:
            logger.warning(
                "refresh.stage_kb.totals_failed",
                extra={"space_id": self.space_id, "error": str(exc)},
            )

        # Serialize the merged KB to dict
        kb_data = prior_kb.to_dict()

        # Store paths under the durable source root, as creation does; some readers open them as-is.
        source_root = self.codekb_service.source_provider.cache_path(self.space_id)
        for comp_data in kb_data["components"]:
            fp = Path(comp_data["file_path"])
            if fp.is_absolute():
                try:
                    fp = fp.relative_to(clone_dir)
                except ValueError:
                    continue
            comp_data["file_path"] = str(source_root / fp)

        # Add export metadata
        from datetime import datetime, timezone

        kb_data["export_metadata"] = {
            "export_timestamp": datetime.now(timezone.utc).isoformat(),
            "exporter_version": "1.0",
            "refresh": True,
        }

        from ..service.codekb_service import _is_git_dir_member

        # Create source tarball from the clone directory
        archive_path = clone_dir.parent / f"{self.space_id}-refresh-source.tar.gz"
        try:
            with tarfile.open(archive_path, "w:gz") as tar:
                # Exclude .git as creation does: its config can hold the clone token.
                tar.add(
                    clone_dir,
                    arcname=".",
                    filter=lambda ti: None if _is_git_dir_member(ti.name) else ti,
                )
            source_archive_bytes = archive_path.read_bytes()
        except Exception as exc:
            logger.error(
                "refresh.stage_kb.tarball_failed",
                extra={"space_id": self.space_id, "error": str(exc)},
            )
            raise RefreshFailed(
                step="stage_kb",
                reason="tarball_creation_failed",
                detail=f"Failed to create source tarball: {exc}",
            )
        finally:
            archive_path.unlink(missing_ok=True)

        # Write both staging artifacts to S3
        try:
            self.kb_manager._write_staging_kb(self.space_id, kb_data, source_archive_bytes)
        except Exception as exc:
            logger.error(
                "refresh.stage_kb.write_failed",
                extra={"space_id": self.space_id, "error": str(exc)},
            )
            raise RefreshFailed(
                step="stage_kb",
                reason="staging_write_failed",
                detail=f"Failed to write staging artifacts to S3: {exc}",
            )

        self.codekb_service._update_pipeline_status(self.space_id, "REFRESHING", "stage_kb", "completed")

        logger.info(
            "refresh.stage_kb.done",
            extra={
                "space_id": self.space_id,
                "kb_components": len(prior_kb.components),
                "source_bytes": len(source_archive_bytes),
            },
        )

    def _demoted_vectors(self) -> frozenset[str]:
        """The demoted components that have a vector to delete."""
        if not self._demoted_ids:
            return frozenset()
        try:
            return frozenset(self.vectors_client.get_descriptions(sorted(self._demoted_ids)))
        except Exception as exc:
            # Non-fatal like a failed delete: a leftover is an extra search hit, not a gap.
            logger.warning(
                "refresh.vector_deletes.demoted_lookup_failed",
                extra={"space_id": self.space_id, "error": str(exc)},
            )
            self._refresh_errors["demoted_vectors"] = (
                f"Could not check {len(self._demoted_ids)} low relevance function(s) "
                f"for descriptions to remove: {exc}"
            )
            return frozenset()

    def _step_vector_deletes(self) -> None:
        """Delete vectors for removed and demoted components. Partial failures are non-fatal.

        Per Req 7 AC 4, delete failures do not fail the refresh. Failed
        component IDs are recorded in refresh_errors and the step is marked
        completed_with_errors.
        """
        plan = self._refresh_plan
        assert plan is not None

        self._demoted_with_vectors = self._demoted_vectors()
        to_delete = plan.vectors_to_delete | self._demoted_with_vectors
        if not to_delete:
            self.codekb_service._update_pipeline_status(
                self.space_id,
                "REFRESHING",
                "vector_deletes",
                "completed",
                extra={"deletes": 0, "errors": 0},
            )
            return

        self.codekb_service._update_pipeline_status(
            self.space_id, "REFRESHING", "vector_deletes", "in_progress"
        )

        delete_errors: list[str] = []
        delete_count = 0

        for component_id in to_delete:
            try:
                self.vectors_client.delete_vector(component_id)
                delete_count += 1
            except Exception as exc:
                delete_errors.append(component_id)
                logger.warning(
                    "refresh.vector_deletes.partial_failure",
                    extra={
                        "space_id": self.space_id,
                        "component_id": component_id,
                        "error": str(exc),
                    },
                )

        self._vectors_deleted = delete_count
        self._vector_errors = delete_errors

        step_status = "completed_with_errors" if delete_errors else "completed"
        self.codekb_service._update_pipeline_status(
            self.space_id,
            "REFRESHING",
            "vector_deletes",
            step_status,
            extra={
                "deletes": delete_count,
                "errors": len(delete_errors),
                "failed_component_ids": delete_errors,
            },
        )

        if delete_errors:
            self._refresh_errors["vector_deletes"] = (
                f"{len(delete_errors)} vector deletes failed: {delete_errors[:10]}"
            )

        logger.info(
            "refresh.vectors.done",
            extra={
                "space_id": self.space_id,
                "upserts": self._vectors_upserted,
                "deletes": delete_count,
                "errors": len(delete_errors),
            },
        )

    def _step_commit_kb(self) -> None:
        """Swap staging artifacts into canonical locations.

        Calls _commit_staging_kb which performs:
          1. CopyObject source.tar.gz.staging -> source.tar.gz
          2. CopyObject knowledge_base.json.staging -> knowledge_base.json
          3. Delete staging keys (best-effort)

        Either swap failing raises; git_commit_hash is still the prior commit, so
        the next refresh repeats the same diff and swaps again.

        Clears the cached code list and local source right after the swap. Also
        computes and stores component_count, vector_count, languages and statistics
        in status.json before writing the terminal status.

        Raises:
            RefreshFailed: If either CopyObject fails.
        """
        self.codekb_service._update_pipeline_status(
            self.space_id, "REFRESHING", "commit_kb", "in_progress"
        )

        try:
            result = self.kb_manager._commit_staging_kb(self.space_id)
        except Exception as exc:
            logger.error(
                "refresh.commit_kb.source_swap_failed",
                extra={"space_id": self.space_id, "error": str(exc)},
            )
            raise RefreshFailed(
                step="commit_kb",
                reason="source_swap_failed",
                detail=f"CopyObject for source.tar.gz failed: {exc}",
            )

        if not result.success:
            logger.error(
                "refresh.commit_kb.kb_swap_failed",
                extra={
                    "space_id": self.space_id,
                    "refresh_errors": result.refresh_errors,
                },
            )
            raise RefreshFailed(
                step="commit_kb",
                reason="kb_swap_failed",
                detail="; ".join(result.refresh_errors.values()),
            )
        self._kb_committed = True
        # Right after the swap, so the code list and source switch together.
        self._caches_dropped = self._drop_read_caches() is None

        # Compute final status counters
        prior_kb = self._prior_kb
        plan = self._refresh_plan
        assert prior_kb is not None
        assert plan is not None

        component_count = len(prior_kb.components)
        languages = sorted(
            {c.language for c in prior_kb.components if hasattr(c, "language") and c.language}
        )

        vector_count = self._vector_count()

        extra: dict[str, Any] = {
            "component_count": component_count,
            "vector_count": vector_count,
            "languages": languages,
        }
        statistics = self._space_statistics(prior_kb)
        if statistics is not None:
            extra["statistics"] = statistics
        extra.update(self._language_set_record_fields(swap_succeeded=result.success))

        self.codekb_service._update_pipeline_status(
            self.space_id,
            "REFRESHING",
            "commit_kb",
            "completed",
            extra=extra,
        )

        logger.info(
            "refresh.commit_kb.done",
            extra={
                "space_id": self.space_id,
                "component_count": component_count,
                "vector_count": vector_count,
                "language_set_recorded": self.LANGUAGE_SET_FIELD in extra,
            },
        )

    def _space_statistics(self, kb: KnowledgeBase) -> dict[str, Any] | None:
        """The status statistics creation records, from the committed KB; None if unavailable."""
        try:
            # Without the heuristic analyzer, so the committed components aren't touched.
            ctx = self.codekb_service.orchestrator._initialize_context(
                kb, business_behavior_enabled=False
            )
            statistics = dict(ctx.statistics)
        except Exception as exc:
            # The KB is already committed; stale statistics must not fail the run.
            logger.warning(
                "refresh.commit_kb.statistics_failed",
                extra={"space_id": self.space_id, "error": str(exc)},
            )
            return None
        return {**(self.prior_status.get("statistics") or {}), **statistics}

    def _language_set_record_fields(self, *, swap_succeeded: bool) -> dict[str, Any]:
        """The record fields to write at this commit, which may be none.

        Written only where the KB swap itself succeeded, so a failed swap leaves the
        upgrade outstanding. A smaller set is never written.
        """
        if not swap_succeeded:
            logger.warning(
                "refresh.language_set.not_recorded",
                extra={"space_id": self.space_id, "reason": "kb_swap_failed"},
            )
            return {}

        if self._language_set_case in ("smaller", "mixed"):
            logger.info(
                "refresh.language_set.not_recorded",
                extra={
                    "space_id": self.space_id,
                    "reason": f"current_set_{self._language_set_case}",
                    "recorded_languages": self._recorded_language_set,
                    "current_languages": self._current_language_set,
                },
            )
            return {}

        from datetime import datetime, timezone

        return {
            self.LANGUAGE_SET_FIELD: self._current_language_set,
            self.LANGUAGE_SET_RECORDED_AT_FIELD: datetime.now(timezone.utc).isoformat(),
        }

    def _drop_read_caches(self) -> Exception | None:
        """Clear the cached code list and local source; the error, or None on success."""
        try:
            self.codekb_service.invalidate_context(self.space_id)
            # Drop the stale local source extraction so the agent re-downloads
            # the refreshed source.tar.gz on its next read.
            self.codekb_service.source_provider.invalidate(self.space_id)
        except Exception as exc:
            logger.warning(
                "refresh.invalidate_caches.failed",
                extra={"space_id": self.space_id, "error": str(exc)},
            )
            return exc
        return None

    def _step_invalidate_caches(self) -> None:
        """Invalidate the per-Space agent context cache and stale local source.

        Clears two caches so subsequent agent queries reflect the refreshed Space:
          1. codekb_service.invalidate_context(space_id) — the in-memory KB/catalog context.
          2. The on-disk source extraction at <cache_root>/<space_id> — the
             agent's read_source_file/list_source_files tools serve files from
             there via ensure_source_local(), which short-circuits when the
             directory already exists and would otherwise keep returning the
             pre-refresh source tree (e.g. a method deleted this refresh would
             still appear in source reads even though the KB and vectors are
             correct). Removing it forces a fresh re-download of the updated
             source.tar.gz on next access.

        commit_kb already cleared both right after the swap; this step retries
        only if that failed. Non-fatal on failure.
        """
        self.codekb_service._update_pipeline_status(
            self.space_id, "REFRESHING", "invalidate_caches", "in_progress"
        )

        # Clearing again would discard a source a reader already re-downloaded.
        error = None if self._caches_dropped else self._drop_read_caches()
        if error:
            self.codekb_service._update_pipeline_status(
                self.space_id,
                "REFRESHING",
                "invalidate_caches",
                "completed_with_errors",
                extra={"error": str(error)},
            )
            return

        self.codekb_service._update_pipeline_status(
            self.space_id, "REFRESHING", "invalidate_caches", "completed"
        )

        logger.info(
            "refresh.invalidate_caches.done",
            extra={"space_id": self.space_id},
        )

    def _step_mark_overviews_stale(self) -> None:
        """Mark all existing overview documents as stale.

        Sets stale=true on each overview doc entry and sets
        overview_status="STALE". Preserves all other fields.
        Non-fatal on failure.

        Also writes the final READY status with all result data.
        """
        self.codekb_service._update_pipeline_status(
            self.space_id, "REFRESHING", "mark_overviews_stale", "in_progress"
        )

        try:
            # Read current status to get overview docs
            current_status = self.codekb_service._read_status(self.space_id) or {}
            overview_docs = current_status.get("space_overview_docs", [])

            if overview_docs:
                # Mark each doc as stale, preserving all other fields
                for doc in overview_docs:
                    doc["stale"] = True

                # Write back with overview_status = "STALE"
                current_status["space_overview_docs"] = overview_docs
                current_status["overview_status"] = "STALE"
                self.codekb_service._write_status(self.space_id, current_status)

                logger.info(
                    "refresh.mark_overviews_stale.done",
                    extra={
                        "space_id": self.space_id,
                        "docs_marked_stale": len(overview_docs),
                    },
                )
            else:
                logger.info(
                    "refresh.mark_overviews_stale.no_docs",
                    extra={"space_id": self.space_id},
                )

        except Exception as exc:
            logger.warning(
                "refresh.mark_overviews_stale.failed",
                extra={"space_id": self.space_id, "error": str(exc)},
            )
            self.codekb_service._update_pipeline_status(
                self.space_id,
                "REFRESHING",
                "mark_overviews_stale",
                "completed_with_errors",
                extra={"error": str(exc)},
            )
            # Fall through to write final READY status anyway

        # Write the final READY status with all result data
        plan = self._refresh_plan
        prior_kb = self._prior_kb
        assert plan is not None
        assert prior_kb is not None

        from datetime import datetime, timezone

        component_count = len(prior_kb.components)
        languages = sorted(
            {c.language for c in prior_kb.components if hasattr(c, "language") and c.language}
        )
        vector_count = self._vector_count()

        # Surface ALL recorded refresh errors, not just vector-delete failures.
        # _refresh_errors carries step-keyed messages (e.g. "kb_swap",
        # "vector_deletes"); _vector_errors carries the raw failed-delete
        # component IDs. Combine both into a flat list of human-readable strings.
        refresh_errors_out: list[str] = [
            f"{step}: {detail}" for step, detail in self._refresh_errors.items()
        ]
        for failed_id in self._vector_errors:
            entry = f"vector_delete_failed: {failed_id}"
            if entry not in refresh_errors_out:
                refresh_errors_out.append(entry)

        final_extra: dict[str, Any] = {
            "component_count": component_count,
            "vector_count": vector_count,
            "languages": languages,
            "git_commit_hash": self._new_commit_hash,
            "refresh_completed_at": datetime.now(timezone.utc).isoformat(),
            "refresh_errors": refresh_errors_out,
            # Older refresh code set this on stopped-early runs.
            "refresh_warning": None,
            "refresh_result": self._result(time.perf_counter() - self._started_at).to_dict(),
        }

        # One token entry per refresh of a new commit, even at zero tokens; a same-commit
        # refresh ends in _finish_unchanged and shows only in the run history.
        run_usage = self._refresh_token_usage or {
            "by_model": {},
            "totals": {"input": 0, "output": 0, "total": 0},
        }
        current = self.codekb_service._read_status(self.space_id) or {}
        token_usage = current.get("token_usage") or {}
        prior_refresh = token_usage.get("space_refresh") or {}
        commit_short = (self._new_commit_hash or "")[:7]
        token_usage["space_refresh"] = self.codekb_service._record_recurring_usage(
            prior_refresh,
            run_usage,
            trigger=commit_short or "refresh",
            extra={
                "git_commit_hash": self._new_commit_hash,
                "components_reprocessed": len(plan.components_to_summarize()),
            },
        )
        final_extra["token_usage"] = token_usage
        final_extra["bedrock_model"] = self.codekb_service._model_metadata(self._refresh_region)

        self.codekb_service._update_pipeline_status(
            self.space_id,
            "READY",
            "mark_overviews_stale",
            "completed",
            extra=final_extra,
        )

    # ── Helpers ────────────────────────────────────────────────────────

    def _creation_summarized(self) -> bool:
        """True if space creation ran AI summarization. The KB can't say: it holds no AI text."""
        steps = self.prior_status.get("pipeline_steps") or []
        if any(s.get("step") == "summarize" and s.get("status") == "completed" for s in steps):
            return True
        # Older refresh code replaced pipeline_steps with its own steps, dropping "summarize".
        return int(self.prior_status.get("vector_count") or 0) > 0

    def _load_callee_descriptions(
        self, kb: KnowledgeBase, target_ids: frozenset[str]
    ) -> dict[str, str]:
        """Stored descriptions of the unchanged callees that appear in the targets' prompts."""
        # A cached target would have its own SOURCE masked, and the LLM would skip it.
        callees = (
            self._reachable_within(kb.call_graph, target_ids, self._prompt_hops()) - target_ids
        )
        if not callees:
            return {}
        descriptions = self.vectors_client.get_descriptions(sorted(callees))
        logger.info(
            "refresh.summarize_changed.prior_descriptions",
            extra={
                "space_id": self.space_id,
                "callees": len(callees),
                "with_description": len(descriptions),
            },
        )
        return descriptions

    def _vector_count(self) -> int:
        """Prior count, plus new descriptions, minus removed and demoted ones whose delete worked."""
        plan = self._refresh_plan
        assert plan is not None
        # Re-describing an existing function overwrites its vector, so only new IDs add one.
        prior_ids = (
            plan.preserved_component_ids | plan.evicted_component_ids
        ) - plan.undescribed_component_ids
        added = len(set(self._summarized_descriptions) - prior_ids)
        deleted = self._removed_callables | self._demoted_with_vectors
        removed = len(deleted - set(self._vector_errors))
        return max(0, int(self.prior_status.get("vector_count") or 0) + added - removed)

    def _stopped_early_remedy(self, stop_reason: str) -> str:
        """User-facing cause and fix; the budget figures stay in the summarizer's log."""
        if stop_reason == "token_budget_reached":
            # A retry repeats the same diff, so it stops at the same point until the budget rises.
            return (
                "AI summarization reached its token budget before finishing. Ask an "
                "administrator to raise the token budget, then use Refresh space."
            )
        if stop_reason == "circuit_breaker_open":
            return (
                "AI summarization stopped after repeated Bedrock failures. "
                "Use Refresh space to try again."
            )
        return "AI summarization stopped early. Use Refresh space to try again."

    def _prompt_hops(self) -> int:
        """Call levels a summarization prompt shows below its function."""
        from ..core.config import require_config

        # call_graph_depth counts the function itself as the first level.
        return int(require_config("analysis.call_graph_depth")) - 1

    def _score_business_relevance(self) -> None:
        """Re-score every callable over the rebuilt call graph, as creation does."""
        if not self._business_behavior_enabled:
            return
        prior_kb = self._prior_kb
        assert prior_kb is not None, "merge step must load prior KB"
        from ..service.source_provider import WORK_DIR_ANALYSIS
        from .business_relevance_analyzer import BusinessRelevanceAnalyzer

        # Path tokens count toward the score, so score the paths creation scored.
        root = self.codekb_service.source_provider.work_path(self.space_id, WORK_DIR_ANALYSIS)
        components = prior_kb.components
        relative = [c.file_path for c in components]
        for component in components:
            component.file_path = root / component.file_path
        metadata = prior_kb.analysis_metadata if isinstance(prior_kb.analysis_metadata, dict) else {}
        try:
            BusinessRelevanceAnalyzer(metadata.get("business_context")).score_components(
                components, prior_kb.call_graph
            )
        finally:
            for component, path in zip(components, relative):
                component.file_path = path
        tiers = {c.id: c.business_behavior.business_relevance_tier for c in components}
        prior_low = {cid for cid, tier in self._prior_tiers.items() if tier == "low"}
        now_low = {cid for cid, tier in tiers.items() if tier == "low"}
        # Creation skipped these as low; now a fresh index would describe them.
        self._promoted_ids = frozenset((prior_low - now_low) & self._stale_candidates)
        # A fresh index would not describe these; their prior vectors are deleted.
        self._demoted_ids = frozenset((now_low & set(self._prior_tiers)) - prior_low)

    def _add_undescribed_promoted(self, plan: RefreshPlan) -> RefreshPlan:
        """Add promoted components that have no description yet to the plan."""
        if not self._promoted_ids:
            return plan
        try:
            described = self.vectors_client.get_descriptions(sorted(self._promoted_ids))
        except Exception as exc:
            # Fatal, unlike the demoted lookup: the new tiers are persisted, so a skipped
            # promotion is never retried and the function stays undescribed for good.
            raise RefreshFailed(
                step="summarize_changed",
                reason="prior_descriptions_unavailable",
                detail=f"Could not read existing descriptions from the vector index: {exc}",
            )
        undescribed = self._promoted_ids - set(described)
        if not undescribed:
            return plan
        plan = dataclasses.replace(plan, undescribed_component_ids=frozenset(undescribed))
        self._refresh_plan = plan
        logger.info(
            "refresh.summarize_changed.promoted",
            extra={"space_id": self.space_id, "undescribed_promoted": len(undescribed)},
        )
        return plan

    def _widen_stale_callers(
        self, prior_call_graph: dict[str, Any], new_call_graph: dict[str, Any]
    ) -> None:
        """Re-plan stale callers over the prior and rebuilt call graphs together."""
        plan = self._refresh_plan
        assert plan is not None, "merge step must produce a RefreshPlan"
        reverse = self._build_reverse_call_graph(prior_call_graph)
        for callee, callers in self._build_reverse_call_graph(new_call_graph).items():
            reverse.setdefault(callee, []).extend(callers)
        seed = plan.changed_component_ids | self._removed_callables
        stale = frozenset(
            self._reachable_within(reverse, seed, self._prompt_hops()) & self._stale_candidates
        )
        added = stale - plan.stale_caller_ids
        if not added:
            return
        self._refresh_plan = dataclasses.replace(
            plan, stale_caller_ids=plan.stale_caller_ids | added
        )
        logger.info(
            "refresh.caller_closure.widened",
            extra={"space_id": self.space_id, "added_stale_callers": len(added)},
        )

    def _reachable_within(
        self, graph: dict[str, Any], roots: frozenset[str], max_hops: int
    ) -> set[str]:
        """Nodes reachable from roots in at most max_hops edges, roots excluded."""
        visited: set[str] = set(roots)
        frontier = list(roots)
        for _ in range(max_hops):
            next_frontier: list[str] = []
            for node in frontier:
                for edge in graph.get(node, ()):
                    neighbor = edge[0] if isinstance(edge, (tuple, list)) else edge
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_frontier.append(neighbor)
            if not next_frontier:
                break
            frontier = next_frontier
        return visited - roots

    def _rebase_merged_paths(self, *, to_absolute: bool) -> None:
        """Rebase merged-component file paths between repo-relative and clone-absolute.

        The refresh keeps paths repo-relative between steps, but reading source
        off disk (for summarization) and re-parsing require absolute paths under
        the refresh clone directory. This toggles the merged components between
        the two representations in place.

        Args:
            to_absolute: When True, resolve relative paths against the clone dir.
                When False, strip the clone dir back off to restore relative paths.
        """
        clone_dir = self._clone_dir
        prior_kb = self._prior_kb
        if clone_dir is None or prior_kb is None:
            return
        for component in prior_kb.components:
            fp = component.file_path
            if to_absolute:
                if not fp.is_absolute():
                    component.file_path = clone_dir / fp
            else:
                if fp.is_absolute() and str(fp).startswith(str(clone_dir)):
                    try:
                        component.file_path = fp.relative_to(clone_dir)
                    except ValueError:
                        pass

    def _inject_merged_context(self) -> None:
        """Inject an AnalysisContext built from the merged KB into the service cache.

        Makes the merged knowledge base (rebuilt call graph + freshly parsed
        components) the context that codekb_service._load_context() returns for this
        Space, so the summarizer operates on the new set rather than the
        not-yet-swapped canonical KB. Rebases component paths to clone-absolute
        first so source reads resolve on disk.
        """
        prior_kb = self._prior_kb
        assert prior_kb is not None, "merge step must load prior KB"
        self._rebase_merged_paths(to_absolute=True)
        # The heuristic analyzer fills descriptions in place, and stage_kb persists them.
        ctx = self.codekb_service.orchestrator._initialize_context(
            prior_kb, business_behavior_enabled=self._business_behavior_enabled
        )
        # Most recently used, so other spaces' loads don't evict it mid-refresh.
        with self.codekb_service._context_cache_lock:
            self.codekb_service._context_cache[self.space_id] = ctx
            self.codekb_service._context_cache.move_to_end(self.space_id)

    def _finish_unchanged(self) -> None:
        """End READY at the same commit, writing nothing but the run's completion."""
        from datetime import datetime, timezone

        logger.info(
            "refresh.unchanged",
            extra={"space_id": self.space_id, "git_commit_hash": self._new_commit_hash},
        )
        # Its own step, so the run history shows the refresh ran and found nothing to do.
        self.codekb_service._update_pipeline_status(
            self.space_id,
            "READY",
            "unchanged",
            "completed",
            extra={
                "refresh_completed_at": datetime.now(timezone.utc).isoformat(),
                "refresh_errors": [],
                # Older refresh code set this on stopped-early runs.
                "refresh_warning": None,
                # Zeros, so the last refresh's numbers don't stay on screen.
                "refresh_result": self._result(time.perf_counter() - self._started_at).to_dict(),
            },
            refresh_run=True,
        )

    def _result(self, duration_s: float) -> RefreshResult:
        """The run's file, component and vector counts; zeros before a plan exists."""
        plan = self._refresh_plan
        injected = self._injected_added_paths
        return RefreshResult(
            # The diff's own added files; files injected for a newly readable
            # language are reported in their own field rather than folded in.
            files_added=len(plan.added_files - injected) if plan else 0,
            files_added_for_new_languages=len(plan.added_files & injected) if plan else 0,
            # The diff's own modified files; part files reprocessed because a
            # split library's parent changed are reported in their own field
            # rather than folded in.
            files_modified=len(plan.modified_files - self._injected_part_paths) if plan else 0,
            files_modified_for_part_libraries=len(plan.modified_files & self._injected_part_paths)
            if plan
            else 0,
            files_removed=len(plan.removed_files) if plan else 0,
            files_unchanged=len(plan.unchanged_files) if plan else 0,
            # Not a skip and not a failure: the user's own generated-file
            # patterns kept these out of the run, the way they keep them out
            # of a full index.
            files_excluded_generated=sum(self._generated_files_excluded.values()),
            components_added=len(plan.changed_component_ids - plan.evicted_component_ids)
            if plan
            else 0,
            components_removed=len(plan.vectors_to_delete) if plan else 0,
            components_reprocessed=len(plan.components_to_summarize()) if plan else 0,
            vectors_upserted=self._vectors_upserted,
            vectors_deleted=self._vectors_deleted,
            vector_errors=self._vector_errors,
            duration_s=round(duration_s, 2),
        )

    def _end_failed(self, step: str, reason: str, error_msg: str) -> RefreshResult:
        """Log, finalize REFRESH_FAILED and report the failure."""
        duration_s = round(time.perf_counter() - self._started_at, 2)
        logger.error(
            "refresh.end",
            extra={
                "space_id": self.space_id,
                "status": "REFRESH_FAILED",
                "outcome": "refresh_failed",
                "duration_s": duration_s,
                "failed_step": step,
                "failed_reason": reason,
            },
        )
        self._finalize_failed_refresh(step, error_msg)
        return RefreshResult(
            duration_s=duration_s, final_error_step=step, final_error_reason=reason
        )

    def _finalize_failed_refresh(self, step: str, error_msg: str) -> None:
        """End a failed refresh REFRESH_FAILED, so it can be refreshed again.

        git_commit_hash is never written before the final step, so it still names
        the prior commit and the next refresh repeats the same diff.
        """
        self._delete_new_vectors()
        extra: dict[str, Any] = {"error": error_msg}
        # Count the tokens a failed run spent; a run that never reached the LLM adds no row.
        if self._refresh_token_usage is not None:
            current = self.codekb_service._read_status(self.space_id) or {}
            token_usage = current.get("token_usage") or {}
            token_usage["space_refresh"] = self.codekb_service._record_recurring_usage(
                token_usage.get("space_refresh") or {},
                self._refresh_token_usage,
                trigger=(self._new_commit_hash or "")[:7] or "refresh",
                extra={"git_commit_hash": self._new_commit_hash, "failed_step": step},
            )
            extra["token_usage"] = token_usage
            extra["bedrock_model"] = self.codekb_service._model_metadata(self._refresh_region)

        self.codekb_service._update_pipeline_status(
            self.space_id, "REFRESH_FAILED", step, "failed", extra=extra, refresh_run=True
        )

    def _delete_new_vectors(self) -> None:
        """Delete vectors this failed run wrote for functions the prior KB doesn't have.

        Otherwise a retry from a later commit that drops them never deletes them.
        Not once the KB is swapped: it holds them, and the retry won't re-describe them.
        """
        plan = self._refresh_plan
        if plan is None or self._kb_committed:
            return
        prior_ids = plan.preserved_component_ids | plan.evicted_component_ids
        for component_id in sorted(set(self._summarized_descriptions) - prior_ids):
            try:
                self.vectors_client.delete_vector(component_id)
            except Exception as exc:
                logger.warning(
                    "refresh.failed.new_vector_delete_failed",
                    extra={"space_id": self.space_id, "component_id": component_id, "error": str(exc)},
                )

    @staticmethod
    def _build_reverse_call_graph(
        call_graph: dict[str, Any],
    ) -> dict[str, list[str]]:
        """Build a reverse call graph from the prior KB's forward call graph.

        The forward call graph maps caller_id -> list of (callee_id, line_number)
        tuples (or plain callee_id strings in older formats). The reverse call
        graph maps callee_id -> list of caller_ids.

        Args:
            call_graph: The forward call graph from the prior KB.

        Returns:
            A reverse call graph mapping callee_id to list of caller_ids.
        """
        reverse: dict[str, list[str]] = {}
        for caller_id, calls in call_graph.items():
            for call in calls:
                # Handle both tuple/list format (callee_id, line_number)
                # and plain string format
                if isinstance(call, (tuple, list)):
                    callee_id = call[0]
                else:
                    callee_id = call
                reverse.setdefault(callee_id, []).append(caller_id)
        return reverse
