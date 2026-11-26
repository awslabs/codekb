"""Per-space output language settings: API models, status.json storage, carry-over."""

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.core.language_directive import resolve_language_directive
from src.service.codekb_service import (
    CodeKBService,
    SpaceLanguageSettingsView,
    SpaceNotFoundError,
)

GIT_URL = "https://github.com/octo/lang-repo.git"


def _seed_status(svc, space_id, status_doc):
    bucket = svc.kb_manager._get_s3_bucket()
    svc.kb_manager.s3_client.put_object(
        Bucket=bucket,
        Key=f"spaces/{space_id}/status.json",
        Body=json.dumps(status_doc).encode("utf-8"),
        ContentType="application/json",
    )


def _start(svc, **kwargs):
    with (
        patch("src.service.codekb_service.validate_git_url", side_effect=lambda u: u),
        patch("src.service.codekb_service.lookup_repo_visibility", return_value="public"),
        patch.object(svc, "_run_pipeline"),  # no worker runs
    ):
        return svc.start_analysis(GIT_URL, **kwargs)


def _ctx():
    kb = SimpleNamespace(
        source_directory="/tmp/x",
        version="1.0",
        created_at="2026-01-01T00:00:00Z",
        get_component_count=lambda: 1,
        components=[SimpleNamespace(language="python")],
        analysis_metadata={},
    )
    return SimpleNamespace(knowledge_base=kb, statistics={"total_lines": 10})


# ── Pydantic models ──────────────────────────────────────────────────────


def test_request_models_default_none_and_accept_values():
    from src.api.routes.spaces import CreateSpaceRequest, StartUploadAnalysisRequest

    c = CreateSpaceRequest(git_url=GIT_URL)
    assert c.overview_language is None and c.analysis_language is None
    c = CreateSpaceRequest(git_url=GIT_URL, overview_language="ja", analysis_language="en")
    assert (c.overview_language, c.analysis_language) == ("ja", "en")

    u = StartUploadAnalysisRequest(space_id="s", space_name="n")
    assert u.overview_language is None and u.analysis_language is None
    u = StartUploadAnalysisRequest(
        space_id="s", space_name="n", overview_language="ja", analysis_language="ja"
    )
    assert (u.overview_language, u.analysis_language) == ("ja", "ja")


def test_space_detail_exposes_fields():
    from src.api.routes.spaces import SpaceDetail

    base = dict(space_id="s", source_directory="/x", component_count=0, version="1", statistics={})
    d = SpaceDetail(**base)
    assert d.overview_language is None and d.analysis_language is None
    d = SpaceDetail(**base, overview_language="ja", analysis_language="en")
    assert d.model_dump()["overview_language"] == "ja"
    assert d.model_dump()["analysis_language"] == "en"


# ── read_space_language_settings ─────────────────────────────────────────


def test_read_settings_values_none_and_partial():
    svc = CodeKBService.instance()
    _seed_status(svc, "lang-both", {"status": "READY", "overview_language": "ja",
                                    "analysis_language": "en"})
    _seed_status(svc, "lang-none", {"status": "READY"})
    _seed_status(svc, "lang-partial", {"status": "READY", "analysis_language": "ja"})

    assert svc.read_space_language_settings("lang-both") == SpaceLanguageSettingsView("ja", "en")
    assert svc.read_space_language_settings("lang-none") == SpaceLanguageSettingsView(None, None)
    assert svc.read_space_language_settings("lang-partial") == SpaceLanguageSettingsView(None, "ja")


def test_read_settings_does_not_default_unsupported_values():
    svc = CodeKBService.instance()
    _seed_status(svc, "lang-raw", {"status": "READY", "overview_language": "xx"})
    assert svc.read_space_language_settings("lang-raw").overview_language == "xx"


def test_read_settings_missing_space_raises():
    svc = CodeKBService.instance()
    with pytest.raises(SpaceNotFoundError):
        svc.read_space_language_settings("no-such-lang-space")


# ── start_analysis ───────────────────────────────────────────────────────


def test_start_analysis_writes_keys_when_given():
    svc = CodeKBService.instance()
    res = _start(svc, overview_language="ja", analysis_language="xx")
    status = svc._read_status(res["space_id"])
    assert status["overview_language"] == "ja"
    assert status["analysis_language"] == "xx"  # stored verbatim, not validated


def test_start_analysis_omits_keys_when_not_given():
    svc = CodeKBService.instance()
    res = _start(svc)
    status = svc._read_status(res["space_id"])
    assert "overview_language" not in status
    assert "analysis_language" not in status


def test_start_analysis_writes_only_the_given_key():
    svc = CodeKBService.instance()
    res = _start(svc, analysis_language="ja")
    status = svc._read_status(res["space_id"])
    assert status["analysis_language"] == "ja"
    assert "overview_language" not in status


# ── carry-over ───────────────────────────────────────────────────────────


def test_finalize_preserves_keys():
    svc = CodeKBService.instance()
    sid = "lang-finalize"
    _seed_status(svc, sid, {"space_id": sid, "status": "ANALYZING",
                            "overview_language": "ja", "analysis_language": "ja"})
    svc._finalize_pipeline(sid, _ctx(), summarize=False, summary_stats=None,
                           source_metadata={"git_url": GIT_URL})
    status = svc._read_status(sid)
    assert status["status"] == "READY"
    assert status["overview_language"] == "ja"
    assert status["analysis_language"] == "ja"


def test_finalize_leaves_keys_absent_when_unset():
    svc = CodeKBService.instance()
    sid = "lang-finalize-unset"
    _seed_status(svc, sid, {"space_id": sid, "status": "ANALYZING"})
    svc._finalize_pipeline(sid, _ctx(), summarize=False, summary_stats=None,
                           source_metadata={"git_url": GIT_URL})
    status = svc._read_status(sid)
    assert "overview_language" not in status and "analysis_language" not in status


def test_pipeline_failure_writeback_preserves_keys():
    svc = CodeKBService.instance()
    sid = "lang-failing"
    _seed_status(svc, sid, {"space_id": sid, "status": "ANALYZING", "git_url": GIT_URL,
                            "overview_language": "ja", "analysis_language": "en"})

    def _boom():
        raise RuntimeError("acquire blew up")

    svc._run_pipeline(sid, svc._analysis_work_path(sid), False, False, _boom)
    status = svc._read_status(sid)
    assert status["status"] == "FAILED"
    assert status["overview_language"] == "ja"
    assert status["analysis_language"] == "en"


def _upload_ready(svc, sid):
    bucket = svc.kb_manager._get_s3_bucket()
    svc.kb_manager.s3_client.put_object(Bucket=bucket, Key=svc._upload_s3_key(sid), Body=b"zip")


def test_upload_flow_preserves_existing_keys():
    svc = CodeKBService.instance()
    sid = "lang-upload-keep"
    _seed_status(svc, sid, {"space_id": sid, "status": "PENDING_UPLOAD", "source_type": "upload",
                            "created_by_sub": "sub-1",
                            "overview_language": "ja", "analysis_language": "ja"})
    _upload_ready(svc, sid)
    with patch.object(svc, "_run_pipeline"):
        svc.start_upload_analysis(space_id=sid, space_name="n", actor_sub="sub-1")
    status = svc._read_status(sid)
    assert status["status"] == "ANALYZING"
    assert status["overview_language"] == "ja"
    assert status["analysis_language"] == "ja"


def test_upload_flow_request_value_wins_over_existing():
    svc = CodeKBService.instance()
    sid = "lang-upload-win"
    _seed_status(svc, sid, {"space_id": sid, "status": "PENDING_UPLOAD", "source_type": "upload",
                            "overview_language": "en", "analysis_language": "en"})
    _upload_ready(svc, sid)
    with patch.object(svc, "_run_pipeline"):
        svc.start_upload_analysis(space_id=sid, space_name="n", overview_language="ja")
    status = svc._read_status(sid)
    assert status["overview_language"] == "ja"
    assert status["analysis_language"] == "en"


def test_upload_flow_without_any_value_leaves_keys_absent():
    svc = CodeKBService.instance()
    gen = svc.generate_upload_url("Lang Plain Space")
    sid = gen["space_id"]
    _upload_ready(svc, sid)
    with patch.object(svc, "_run_pipeline"):
        svc.start_upload_analysis(space_id=sid, space_name="Lang Plain Space")
    status = svc._read_status(sid)
    assert "overview_language" not in status and "analysis_language" not in status


def test_upload_flow_request_values_stored_when_pending_record_has_none():
    svc = CodeKBService.instance()
    gen = svc.generate_upload_url("Lang Req Space")
    sid = gen["space_id"]
    _upload_ready(svc, sid)
    with patch.object(svc, "_run_pipeline"):
        svc.start_upload_analysis(space_id=sid, space_name="Lang Req Space",
                                  overview_language="ja", analysis_language="ja")
    status = svc._read_status(sid)
    assert status["overview_language"] == "ja" and status["analysis_language"] == "ja"


# ── refresh ──────────────────────────────────────────────────────────────


def test_refresh_status_writes_preserve_keys():
    svc = CodeKBService.instance()
    sid = "lang-refresh"
    _seed_status(svc, sid, {
        "space_id": sid, "status": "READY", "git_url": GIT_URL,
        "overview_language": "ja", "analysis_language": "ja",
        "refresh_pipeline_runs": [{"started_at": "t", "steps": []}],
        "space_overview_docs": [{"key": "business_overview"}],
    })
    svc._update_pipeline_status(sid, "REFRESHING", "clone", "in_progress")
    svc._update_pipeline_status(sid, "REFRESHING", "summarize_changed", "completed",
                                extra={"summarize_total": 0})
    # Overview-stale write-back (read-modify-write inside the orchestrator)
    from src.analysis.refresh_orchestrator import RefreshOrchestrator

    orch = RefreshOrchestrator(
        space_id=sid, prior_status={}, kb_manager=MagicMock(), vectors_client=MagicMock(),
        codekb_service=svc, git_ops=MagicMock(),
    )
    # The stale write-back happens first; the trailing final-READY write needs a
    # full refresh plan this test does not build, so its assert is expected.
    with contextlib.suppress(AssertionError):
        orch._step_mark_overviews_stale()
    svc._update_pipeline_status(sid, "READY", "unchanged", "completed", refresh_run=True)
    status = svc._read_status(sid)
    assert status["status"] == "READY"
    assert status["overview_language"] == "ja"
    assert status["analysis_language"] == "ja"

    svc._update_pipeline_status(sid, "REFRESH_FAILED", "clone", "failed", refresh_run=True)
    status = svc._read_status(sid)
    assert status["status"] == "REFRESH_FAILED"
    assert status["overview_language"] == "ja"


def test_refresh_summarizer_gets_analysis_directive(tmp_path):
    from test.test_refresh_summarize_vectors import (
        SUMMARIZED_STATUS,
        _FakeSummarizer,
        _orchestrator,
    )

    _FakeSummarizer.instances = []
    _FakeSummarizer.stop_reason = None
    directive = resolve_language_directive("ja")
    orch = _orchestrator(SUMMARIZED_STATUS, tmp_path)
    orch.codekb_service._resolve_analysis_language.return_value = directive
    with patch("src.vector.topdown_summarizer.TopDownSummarizer", _FakeSummarizer):
        orch._step_summarize_changed()
    orch.codekb_service._resolve_analysis_language.assert_called_once_with("space")
    (summarizer,) = _FakeSummarizer.instances
    assert summarizer.kwargs["language_directive"] is directive


# ── get_space + HTTP ─────────────────────────────────────────────────────


def _seed_ready(svc, sid, **extra):
    _seed_status(svc, sid, {
        "status": "READY", "component_count": 1, "source_directory": "/tmp/seeded",
        "space_name": sid, "statistics": {}, "created_at": "2026-01-01T00:00:00Z",
        "version": "1.0", "git_url": "https://example.com/seed.git", **extra,
    })


def test_get_space_returns_language_keys():
    svc = CodeKBService.instance()
    _seed_ready(svc, "lang-get-set", overview_language="ja", analysis_language="en")
    _seed_ready(svc, "lang-get-unset")
    info = svc.get_space("lang-get-set")
    assert info["overview_language"] == "ja" and info["analysis_language"] == "en"
    info = svc.get_space("lang-get-unset")
    assert info["overview_language"] is None and info["analysis_language"] is None


def test_get_space_legacy_fallback_returns_language_keys():
    svc = CodeKBService.instance()
    kb = SimpleNamespace(source_directory=Path("/x"), get_component_count=lambda: 0,
                         created_at=None, version="1.0", id_scheme="")
    ctx = SimpleNamespace(knowledge_base=kb, statistics={})
    with (
        patch.object(svc, "_read_status", return_value=None),
        patch.object(svc, "_load_context", return_value=ctx),
    ):
        info = svc.get_space("legacy-lang")
    assert info["overview_language"] is None and info["analysis_language"] is None


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from src.api.routes import spaces

    app = FastAPI()
    app.include_router(spaces.router, prefix="/api/spaces")
    return TestClient(app, raise_server_exceptions=False)


def _http_env():
    import src.api.auth as auth_mod
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch.object(auth_mod, "_REQUIRE_AUTH", False))
    stack.enter_context(patch("src.api.gates.require_authorization", return_value=False))
    stack.enter_context(patch("src.api.gates.lookup_repo_visibility", return_value="public"))
    stack.enter_context(patch("src.api.gates.validate_git_url", side_effect=lambda u: u))
    stack.enter_context(patch("src.service.codekb_service.validate_git_url",
                              side_effect=lambda u: u))
    stack.enter_context(patch("src.service.codekb_service.lookup_repo_visibility",
                              return_value="public"))
    return stack


def test_get_space_endpoint_returns_both_fields():
    svc = CodeKBService.instance()
    _seed_ready(svc, "lang-http-set", overview_language="ja", analysis_language="ja")
    _seed_ready(svc, "lang-http-unset")
    with _http_env():
        c = _client()
        r = c.get("/api/spaces/lang-http-set")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["overview_language"] == "ja" and body["analysis_language"] == "ja"
        body = c.get("/api/spaces/lang-http-unset").json()
        assert body["overview_language"] is None and body["analysis_language"] is None


def test_create_space_with_and_without_language():
    svc = CodeKBService.instance()
    with _http_env(), patch.object(svc, "_run_pipeline"):
        c = _client()
        r = c.post("/api/spaces", json={"git_url": GIT_URL})
        assert r.status_code == 201, r.text
        status = svc._read_status(r.json()["space_id"])
        assert "overview_language" not in status and "analysis_language" not in status

        r = c.post("/api/spaces", json={"git_url": GIT_URL, "overview_language": "ja",
                                        "analysis_language": "en"})
        assert r.status_code == 201, r.text
        status = svc._read_status(r.json()["space_id"])
        assert status["overview_language"] == "ja"
        assert status["analysis_language"] == "en"
