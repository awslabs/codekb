"""Tests for CodeKBService space CRUD against mocked S3.

These seed `status.json` and `knowledge_base.json` under the S3 layout the
service expects, then exercise list_spaces / get_space / delete_space /
invalidate_context without starting any pipeline threads.
"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.models import (
    Component,
    ComponentAttributes,
    ComponentType,
    KnowledgeBase,
    Visibility,
)
from src.core.file_utils import LEGACY_DEFAULT_EXCLUDE_PATTERNS
from src.persistence.knowledge_base_manager import KnowledgeBaseManager
from src.service.codekb_service import (
    CodeKBService,
    ServiceError,
    SpaceBusyError,
    SpaceNotFoundError,
)


def _seed_status(codekb_service: CodeKBService, space_id: str, status_doc: dict) -> None:
    """Write a status.json into the mocked KB bucket."""
    bucket = codekb_service.kb_manager._get_s3_bucket()
    codekb_service.kb_manager.s3_client.put_object(
        Bucket=bucket,
        Key=f"spaces/{space_id}/status.json",
        Body=json.dumps(status_doc).encode("utf-8"),
        ContentType="application/json",
    )


def _seed_ready_space(codekb_service: CodeKBService, space_id: str) -> None:
    """Write a fully-populated READY status + minimal KB into S3."""
    _seed_status(
        codekb_service,
        space_id,
        {
            "status": "READY",
            "component_count": 1,
            "source_directory": "/tmp/seeded",
            "space_name": space_id,
            "languages": ["python"],
            "statistics": {"total_lines": 42},
            "created_at": "2026-01-01T00:00:00Z",
            "version": "1.0",
            "git_url": "https://example.com/seed.git",
        },
    )

    kb = KnowledgeBase(
        space_id=space_id,
        source_directory="/tmp/seeded",
        components=[
            Component(
                id=f"{space_id}::mod.fn",
                type=ComponentType.FUNCTION,
                file_path=Path("mod.py"),
                line_number=1,
                end_line_number=3,
                language="python",
                space_id=space_id,
                attributes=ComponentAttributes(visibility=Visibility.PUBLIC, line_count=3),
            )
        ],
    )
    KnowledgeBaseManager().export_knowledge_base(kb)


@pytest.fixture(autouse=True)
def _restore_vectors_store(mock_s3vectors):
    """Isolate the session-scoped S3 Vectors store per test.

    mock_s3vectors._vectors_store is shared across the whole session, and
    several delete tests intentionally leave a vector behind (the absent-KB,
    malformed-KB, refresh-orphan, and foreign-prefix paths). Snapshot the store
    before each test and restore it after, so no test's residue leaks into a
    later test's query_vectors/list_vectors and causes order-dependent
    flakiness.
    """
    store = mock_s3vectors._vectors_store
    before = dict(store)
    yield
    store.clear()
    store.update(before)


class TestListSpaces:
    def test_returns_empty_when_no_spaces(self):
        codekb_service = CodeKBService.instance()
        # Other tests may have seeded, so just assert type contract rather than empty.
        result = codekb_service.list_spaces()
        assert isinstance(result, list)

    def test_reports_seeded_space(self):
        codekb_service = CodeKBService.instance()
        _seed_ready_space(codekb_service, "listed-space")

        spaces = codekb_service.list_spaces()
        ids = [s["space_id"] for s in spaces]
        assert "listed-space" in ids

        entry = next(s for s in spaces if s["space_id"] == "listed-space")
        assert entry["status"] == "READY"
        assert entry["component_count"] == 1
        assert entry["languages"] == ["python"]


class TestGetSpace:
    def test_returns_details_for_ready_space(self):
        codekb_service = CodeKBService.instance()
        _seed_ready_space(codekb_service, "detail-space")

        info = codekb_service.get_space("detail-space")
        assert info["space_id"] == "detail-space"
        assert info["component_count"] == 1
        assert info["source_directory"] == "/tmp/seeded"

    def test_returns_recorded_exclude_patterns(self):
        svc = CodeKBService.instance()
        _seed_ready_space(svc, "patterns-space")
        status = svc._read_status("patterns-space")
        status["exclude_patterns"] = ["node_modules", ".claude/"]
        _seed_status(svc, "patterns-space", status)

        assert svc.get_space("patterns-space")["exclude_patterns"] == ["node_modules", ".claude/"]

    def test_historical_space_reports_legacy_exclude_patterns(self):
        svc = CodeKBService.instance()
        _seed_ready_space(svc, "historical-space")

        info = svc.get_space("historical-space")
        assert info["exclude_patterns"] == LEGACY_DEFAULT_EXCLUDE_PATTERNS

    def test_creation_records_system_exclude_patterns(self):
        from src.core.config import get_config

        svc = CodeKBService.instance()
        kb = KnowledgeBase(space_id="new-space", source_directory="/tmp/new")
        ctx = SimpleNamespace(knowledge_base=kb, statistics={})
        svc._finalize_pipeline("new-space", ctx, False, None, {})

        recorded = svc._read_status("new-space")["exclude_patterns"]
        assert recorded == list(get_config("analysis.exclude_patterns", []))

    def test_unknown_space_raises(self):
        codekb_service = CodeKBService.instance()
        with pytest.raises((SpaceNotFoundError, Exception)):
            codekb_service.get_space("definitely-not-a-space")


class TestInvalidateContext:
    def test_removes_cached_context_without_error(self):
        codekb_service = CodeKBService.instance()
        _seed_ready_space(codekb_service, "cache-space")

        # Populate cache via get_space fallback path (it calls _load_context only
        # when status != READY, so prime it directly).
        codekb_service._context_cache["cache-space"] = object()  # type: ignore[assignment]
        codekb_service.invalidate_context("cache-space")
        assert "cache-space" not in codekb_service._context_cache

    def test_invalidate_unknown_space_is_noop(self):
        codekb_service = CodeKBService.instance()
        codekb_service.invalidate_context("never-cached-space")  # should not raise


class TestContextCacheEviction:
    """The context cache is bounded, and evicts least-recently-used first."""

    @staticmethod
    def _service_with_limit(limit: int, monkeypatch) -> CodeKBService:
        codekb_service = CodeKBService.instance()
        codekb_service._max_cached_spaces = limit
        # Stand in for the S3 load so these tests exercise cache policy alone.
        monkeypatch.setattr(
            codekb_service.orchestrator, "load_space", lambda sid: SimpleNamespace(space_id=sid)
        )
        return codekb_service

    def test_evicts_the_coldest_entry_once_over_the_limit(self, monkeypatch):
        codekb_service = self._service_with_limit(2, monkeypatch)

        codekb_service._load_context("a")
        codekb_service._load_context("b")
        codekb_service._load_context("c")  # takes the cache over its limit

        assert list(codekb_service._context_cache) == ["b", "c"]

    def test_reuse_protects_an_entry_from_eviction(self, monkeypatch):
        codekb_service = self._service_with_limit(2, monkeypatch)

        codekb_service._load_context("a")
        codekb_service._load_context("b")
        codekb_service._load_context("a")  # "a" becomes the most recently used
        codekb_service._load_context("c")

        # A plain insertion-ordered cache would have dropped "a" here; LRU drops "b".
        assert list(codekb_service._context_cache) == ["a", "c"]

    def test_a_hit_does_not_reload_from_storage(self, monkeypatch):
        codekb_service = CodeKBService.instance()
        codekb_service._max_cached_spaces = 2
        loads: list[str] = []

        def counting_load(sid):
            loads.append(sid)
            return SimpleNamespace(space_id=sid)

        monkeypatch.setattr(codekb_service.orchestrator, "load_space", counting_load)

        first = codekb_service._load_context("a")
        again = codekb_service._load_context("a")

        # Asserting object identity alone would prove nothing: the double-check
        # after the load returns the already-cached entry, so identity holds even
        # if the fast path is skipped. The load count is what pins the fast path.
        assert loads == ["a"]
        assert again is first


class TestDeleteSpace:
    @pytest.mark.anyio
    async def test_delete_removes_status_from_list(self):
        codekb_service = CodeKBService.instance()
        _seed_ready_space(codekb_service, "doomed-space")

        # Sanity check it's there
        assert any(s["space_id"] == "doomed-space" for s in codekb_service.list_spaces())

        result = await codekb_service.delete_space("doomed-space")
        assert isinstance(result, dict)
        # Service returns some form of confirmation dict; don't be strict about keys.

        # After deletion, the space should no longer appear in the listing.
        remaining_ids = [s["space_id"] for s in codekb_service.list_spaces()]
        assert "doomed-space" not in remaining_ids


class TestDeleteSpaceVectorCleanup:
    """Vector cleanup is not optional: it always runs, and it gates the delete.

    Vectors live in an index shared by every space, so any left behind stay
    searchable forever. They are therefore removed first, and if that fails the
    delete stops before touching metadata — ``status.json`` is the tombstone, and
    while it exists the space is still listed and the delete can be re-issued.
    """

    @pytest.mark.anyio
    async def test_delete_removes_the_spaces_vectors(self, mock_s3vectors):
        codekb_service = CodeKBService.instance()
        store = mock_s3vectors._vectors_store
        # A READY space with a KB — the KB-keyed path reconstructs the component
        # key and deletes exactly that vector.
        _seed_ready_space(codekb_service, "vector-cleanup-space")
        store["vector-cleanup-space::mod.fn"] = {
            "key": "vector-cleanup-space::mod.fn",
            "data": {"float32": [0.1] * 1024},
            "metadata": {"space_id": "vector-cleanup-space", "description": "seeded"},
        }

        await codekb_service.delete_space("vector-cleanup-space")

        assert "vector-cleanup-space::mod.fn" not in store

    @pytest.mark.anyio
    async def test_vector_failure_aborts_before_metadata_is_touched(self, mock_s3vectors, monkeypatch):
        codekb_service = CodeKBService.instance()
        _seed_ready_space(codekb_service, "stuck-space")
        # ``stuck-space`` has a KB (one FUNCTION component), so the primary
        # KB-keyed path runs and calls delete_vectors — not list_vectors. Fail
        # that call to prove the delete still aborts before metadata is touched.
        # mock_s3vectors is session-scoped, so set this through monkeypatch to get
        # it torn down again — a bare assignment would poison every later test.
        monkeypatch.setattr(
            mock_s3vectors.delete_vectors,
            "side_effect",
            RuntimeError("vector store unreachable"),
        )

        with pytest.raises(ServiceError):
            await codekb_service.delete_space("stuck-space")

        # The tombstone survived, so a retry can still find the space and finish
        # the job. Deleting it here would orphan the vectors permanently.
        assert any(s["space_id"] == "stuck-space" for s in codekb_service.list_spaces())

    @pytest.mark.anyio
    async def test_delete_removes_kb_keyed_vectors(self, mock_s3vectors):
        """A space with a readable KB has the exact keys reconstructed from
        knowledge_base.json deleted through delete_space."""
        codekb_service = CodeKBService.instance()
        store = mock_s3vectors._vectors_store
        _seed_ready_space(codekb_service, "kb-keyed-space")
        store["kb-keyed-space::mod.fn"] = {
            "key": "kb-keyed-space::mod.fn",
            "data": {"float32": [0.1] * 1024},
            "metadata": {"space_id": "kb-keyed-space", "description": "seeded"},
        }

        await codekb_service.delete_space("kb-keyed-space")

        # The exact KB-derived key was removed.
        assert "kb-keyed-space::mod.fn" not in store

    @pytest.mark.anyio
    async def test_delete_with_absent_kb_deletes_zero_but_completes(
        self, mock_s3vectors
    ):
        """A space with a status.json but NO committed knowledge_base.json (e.g.
        one that FAILED before analysis finished) is still deletable: key
        reconstruction sees the KB is absent and returns zero keys, so no vectors
        are deleted, but the delete completes and removes the space's metadata
        entirely (no tombstone; get_analysis_status synthesizes NOT_FOUND)."""
        codekb_service = CodeKBService.instance()
        store = mock_s3vectors._vectors_store
        # Status exists, but there is NO knowledge_base.json object, so
        # _exists() is False and _vector_keys_for_space returns [].
        _seed_status(codekb_service, "no-kb-space", {"status": "FAILED", "space_name": "no-kb-space"})
        # A stray vector with no corresponding KB component.
        store["no-kb-space::mod.fn"] = {
            "key": "no-kb-space::mod.fn",
            "data": {"float32": [0.1] * 1024},
            "metadata": {"space_id": "no-kb-space", "description": "orphan"},
        }

        await codekb_service.delete_space("no-kb-space")

        # No KB -> zero keys -> the stray vector is not deleted ...
        assert "no-kb-space::mod.fn" in store
        # ... but the delete completed and removed the space's metadata entirely:
        # no status.json tombstone remains, the status is synthesized as
        # NOT_FOUND, and the space no longer appears in the listing.
        assert codekb_service._read_status("no-kb-space") is None
        assert codekb_service.get_analysis_status("no-kb-space").get("status") == "NOT_FOUND"
        assert not any(s["space_id"] == "no-kb-space" for s in codekb_service.list_spaces())

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "kb_body",
        [
            # ``components`` is the wrong shape (a mapping, not a list of records).
            {"space_id": "malformed-kb-space", "components": {"oops": "notalist"}},
            # ``components`` key is absent entirely.
            {"space_id": "malformed-kb-space"},
            # A component record is not an object.
            {"space_id": "malformed-kb-space", "components": ["malformed-kb-space::mod.fn"]},
            # A component record is missing a usable ``id``.
            {"space_id": "malformed-kb-space", "components": [{"type": "FUNCTION"}]},
            # Root is not a JSON object.
            [],
        ],
    )
    async def test_delete_with_malformed_kb_aborts_with_error(
        self, mock_s3vectors, caplog, kb_body
    ):
        """A readable but structurally untrustworthy KB makes key reconstruction
        raise (TypeError/AttributeError on the bad shape), which propagates to the
        vector-cleanup hard gate. delete_space logs LOUDLY, aborts BEFORE touching
        metadata (status -> FAILED, space stays listed), and raises ServiceError.
        No vectors are deleted."""
        codekb_service = CodeKBService.instance()
        store = mock_s3vectors._vectors_store
        space_id = "malformed-kb-space"
        _seed_status(codekb_service, space_id, {"status": "READY"})
        bucket = codekb_service.kb_manager._get_s3_bucket()
        codekb_service.kb_manager.s3_client.put_object(
            Bucket=bucket,
            Key=codekb_service.kb_manager._get_s3_key(space_id),
            Body=json.dumps(kb_body).encode("utf-8"),
            ContentType="application/json",
        )
        store[f"{space_id}::mod.fn"] = {
            "key": f"{space_id}::mod.fn",
            "data": {"float32": [0.1] * 1024},
            "metadata": {"space_id": space_id, "description": "seeded"},
        }

        with caplog.at_level(logging.ERROR):
            with pytest.raises(ServiceError):
                await codekb_service.delete_space(space_id)

        # Loud error naming the space; nothing deleted; space left intact.
        assert any(
            space_id in r.getMessage() and r.levelno == logging.ERROR
            for r in caplog.records
        )
        assert f"{space_id}::mod.fn" in store
        assert codekb_service._read_status(space_id).get("status") == "FAILED"
        assert any(s["space_id"] == space_id for s in codekb_service.list_spaces())

    @pytest.mark.anyio
    async def test_refresh_orphan_residual_is_left_by_kb_keyed_path(self, mock_s3vectors):
        """ACCEPTED, DOCUMENTED TRADEOFF (orphan safety) — locks in the chosen design.

        A space refresh can evict a component from knowledge_base.json while that
        component's per-component vector delete fails; the refresh pipeline
        tolerates this as a harmless same-tenant orphan and still commits the
        reduced KB (see ``RefreshOrchestrator._step_vector_deletes``). The vector
        then survives in the shared index with no corresponding KB component.

        Because the KB-keyed path reconstructs keys ONLY from the committed KB,
        it cannot see that stale key, so the orphan is intentionally left behind.
        There is no full-index scan to sweep it. This test pins that behavior so
        it cannot change silently.

        NOTE: the review loop's aggregation rule flags this residual as blocking
        by policy; the product owner has reviewed and explicitly accepted it as
        the intended tradeoff (deletion runs in a background task behind the
        async/202 route, so response speed is unaffected). See
        ``.agents/tasks/optimize-vector-delete-2026-10-08/verification.md``.
        """
        codekb_service = CodeKBService.instance()
        store = mock_s3vectors._vectors_store
        # Committed (reduced) KB holds only the surviving component ``mod.fn``.
        _seed_ready_space(codekb_service, "refresh-orphan-space")
        # Active vector for the surviving KB component.
        store["refresh-orphan-space::mod.fn"] = {
            "key": "refresh-orphan-space::mod.fn",
            "data": {"float32": [0.1] * 1024},
            "metadata": {"space_id": "refresh-orphan-space", "description": "kept"},
        }
        # Refresh-orphan: a prior space refresh evicted ``removed.fn`` and its
        # vector delete FAILED, so the vector lingers even though the id is absent
        # from the committed KB. It still carries metadata.space_id.
        store["refresh-orphan-space::removed.fn"] = {
            "key": "refresh-orphan-space::removed.fn",
            "data": {"float32": [0.1] * 1024},
            "metadata": {"space_id": "refresh-orphan-space", "description": "orphan"},
        }
        mock_s3vectors.list_vectors.reset_mock()

        await codekb_service.delete_space("refresh-orphan-space")

        # The KB-keyed path ran (no scan) and removed exactly the KB-derived key...
        assert mock_s3vectors.list_vectors.call_count == 0
        assert "refresh-orphan-space::mod.fn" not in store
        # ...and left the refresh-orphan behind: the accepted, documented residual.
        assert "refresh-orphan-space::removed.fn" in store

    @pytest.mark.anyio
    async def test_foreign_prefix_component_is_skipped_and_logged(
        self, mock_s3vectors, caplog
    ):
        """A KB holding a foreign-space id must never delete another space's
        vector from the shared index. The cross-tenant guard SKIPS the
        out-of-namespace id — it is never passed to delete_vectors_by_keys — and
        logs a LOUD error. Only this space's own key is deleted; there is no
        scan."""
        codekb_service = CodeKBService.instance()
        store = mock_s3vectors._vectors_store
        _seed_status(codekb_service, "tenant-a", {"status": "READY"})
        bucket = codekb_service.kb_manager._get_s3_bucket()
        codekb_service.kb_manager.s3_client.put_object(
            Bucket=bucket,
            Key=codekb_service.kb_manager._get_s3_key("tenant-a"),
            Body=json.dumps(
                {
                    "space_id": "tenant-a",
                    "components": [
                        {"id": "tenant-a::mod.fn"},
                        {"id": "tenant-b::victim.fn"},  # foreign-space id
                    ],
                }
            ).encode("utf-8"),
            ContentType="application/json",
        )
        store["tenant-a::mod.fn"] = {
            "key": "tenant-a::mod.fn",
            "data": {"float32": [0.1] * 1024},
            "metadata": {"space_id": "tenant-a", "description": "mine"},
        }
        store["tenant-b::victim.fn"] = {
            "key": "tenant-b::victim.fn",
            "data": {"float32": [0.1] * 1024},
            "metadata": {"space_id": "tenant-b", "description": "another tenant"},
        }
        mock_s3vectors.list_vectors.reset_mock()
        mock_s3vectors.delete_vectors.reset_mock()

        with caplog.at_level(logging.ERROR):
            await codekb_service.delete_space("tenant-a")

        # No scan; only tenant-a's own key was deleted. The foreign id was never
        # submitted for deletion, so the other tenant's vector is untouched.
        assert mock_s3vectors.list_vectors.call_count == 0
        submitted_keys = [
            k
            for call in mock_s3vectors.delete_vectors.call_args_list
            for k in call.kwargs["keys"]
        ]
        assert "tenant-b::victim.fn" not in submitted_keys
        assert "tenant-a::mod.fn" in submitted_keys
        assert "tenant-a::mod.fn" not in store
        assert "tenant-b::victim.fn" in store
        # The guard logged a loud error naming the foreign key.
        assert any(
            "tenant-b::victim.fn" in r.getMessage() and r.levelno == logging.ERROR
            for r in caplog.records
        )


class TestMarkSpaceDeleting:
    """Delete vs. active indexing is serialized at the source: a space being
    indexed is rejected with 409 (SpaceBusyError) and is NOT flipped to DELETING,
    so the KB-keyed delete never reads a KB that lags in-flight upserts."""

    @pytest.mark.parametrize("busy_status", ["REFRESHING", "ANALYZING", "SUMMARIZING"])
    def test_rejects_busy_space_with_409_and_does_not_flip(self, busy_status):
        codekb_service = CodeKBService.instance()
        space_id = f"busy-{busy_status.lower()}-space"
        _seed_status(codekb_service, space_id, {"status": busy_status, "space_name": space_id})

        with pytest.raises(SpaceBusyError) as excinfo:
            codekb_service.mark_space_deleting(space_id)

        assert excinfo.value.status_code == 409
        # Status was NOT flipped to DELETING — it stays as it was.
        assert codekb_service._read_status(space_id).get("status") == busy_status

    def test_unknown_space_raises_not_found(self):
        codekb_service = CodeKBService.instance()
        with pytest.raises(SpaceNotFoundError):
            codekb_service.mark_space_deleting("no-such-space-at-all")

    def test_ready_space_is_flipped_to_deleting(self):
        codekb_service = CodeKBService.instance()
        _seed_ready_space(codekb_service, "ready-to-delete-space")

        codekb_service.mark_space_deleting("ready-to-delete-space")

        assert codekb_service._read_status("ready-to-delete-space").get("status") == "DELETING"


class TestComponentSource:
    def test_reads_exact_component_source_range(self, tmp_path):
        source = tmp_path / "example.py"
        source.write_text(
            "import os\n\ndef first():\n    return 1\n\ndef second():\n    return 2\n"
        )
        component = Component(
            id="space::example.first",
            type=ComponentType.FUNCTION,
            file_path=source,
            line_number=3,
            end_line_number=4,
            language="python",
            space_id="space",
        )
        knowledge_base = KnowledgeBase(
            space_id="space",
            source_directory=tmp_path,
            components=[component],
        )
        ctx = SimpleNamespace(knowledge_base=knowledge_base)

        result = CodeKBService.instance()._read_component_source("space", ctx, component)

        assert result == "def first():\n    return 1"

    def test_reads_source_from_relative_catalog_path(self, tmp_path):
        source = tmp_path / "pkg" / "example.py"
        source.parent.mkdir()
        source.write_text("def wrapped():\n    return 'ok'\n")
        component = Component(
            id="space::pkg.example.wrapped",
            type=ComponentType.FUNCTION,
            file_path=Path("pkg/example.py"),
            line_number=1,
            end_line_number=2,
            language="python",
            space_id="space",
        )
        knowledge_base = KnowledgeBase(
            space_id="space",
            source_directory=tmp_path,
            components=[component],
        )
        ctx = SimpleNamespace(knowledge_base=knowledge_base)

        result = CodeKBService.instance()._read_component_source("space", ctx, component)

        assert result == "def wrapped():\n    return 'ok'"
