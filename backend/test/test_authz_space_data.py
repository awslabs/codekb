"""Phase 2 data layer: authz fields survive create, finalize, failure, and upload."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.service.codekb_service import CodeKBService, ServiceError

GIT_URL = "https://github.com/octo/repo.git"
AUTHZ_SEED = {
    "git_url": GIT_URL,
    "visibility": "private",
    "provider": "github",
    "repo_key": "github.com/octo/repo",
    "created_by": "u@example.com",
    "created_by_sub": "sub-1",
    "admitted_by": "u@example.com",
}


def _seed_status(svc, space_id, status_doc):
    bucket = svc.kb_manager._get_s3_bucket()
    svc.kb_manager.s3_client.put_object(
        Bucket=bucket,
        Key=f"spaces/{space_id}/status.json",
        Body=json.dumps(status_doc).encode("utf-8"),
        ContentType="application/json",
    )


# ── create ───────────────────────────────────────────────────────────────


def test_create_persists_authz_fields():
    svc = CodeKBService.instance()
    with (
        patch("src.service.codekb_service.validate_git_url", side_effect=lambda u: u),
        patch("src.service.codekb_service.lookup_repo_visibility", return_value="private"),
        patch.object(svc, "_run_pipeline"),  # don't run the pipeline thread
    ):
        res = svc.start_analysis(GIT_URL, created_by="u@example.com", actor_sub="sub-1")

    status = svc._read_status(res["space_id"])
    assert status["visibility"] == "private"
    assert status["provider"] == "github"
    assert status["repo_key"] == "github.com/octo/repo"
    assert status["created_by"] == "u@example.com"
    assert status["created_by_sub"] == "sub-1"
    assert status["admitted_by"] == "u@example.com"


# ── finalize (the allowlist trap) ───────────────────────────────────────────


def test_finalize_preserves_authz_fields_on_ready():
    svc = CodeKBService.instance()
    sid = "finalize-space"
    _seed_status(svc, sid, {"space_id": sid, "status": "ANALYZING", **AUTHZ_SEED})

    kb = SimpleNamespace(
        source_directory="/tmp/x",
        version="1.0",
        created_at="2026-01-01T00:00:00Z",
        get_component_count=lambda: 1,
        components=[SimpleNamespace(language="python")],
        analysis_metadata={},
    )
    ctx = SimpleNamespace(knowledge_base=kb, statistics={"total_lines": 10})
    svc._finalize_pipeline(
        sid, ctx, summarize=False, summary_stats=None, source_metadata={"git_url": GIT_URL}
    )

    status = svc._read_status(sid)
    assert status["status"] == "READY"
    assert status["visibility"] == "private"
    assert status["provider"] == "github"
    assert status["repo_key"] == "github.com/octo/repo"
    assert status["created_by_sub"] == "sub-1"
    assert status["admitted_by"] == "u@example.com"


# ── create-failure handler (security-critical) ─────────────────────────────


def test_pipeline_failure_keeps_git_url_and_authz_fields():
    svc = CodeKBService.instance()
    sid = "failing-space"
    _seed_status(svc, sid, {"space_id": sid, "status": "ANALYZING", **AUTHZ_SEED})

    def _boom():
        raise RuntimeError("acquire blew up")

    source_dir = svc._analysis_work_path(sid)
    svc._run_pipeline(sid, source_dir, False, False, _boom)

    status = svc._read_status(sid)
    assert status["status"] == "FAILED"
    assert "acquire blew up" in status["error"]
    # git_url + authz survive — not silently reclassified as a public zip.
    assert status["git_url"] == GIT_URL
    assert status["visibility"] == "private"
    assert status["provider"] == "github"
    assert status["repo_key"] == "github.com/octo/repo"
    assert status["created_by_sub"] == "sub-1"


# ── zip upload ─────────────────────────────────────────────────────────────


def test_generate_upload_url_stores_created_by_sub():
    svc = CodeKBService.instance()
    res = svc.generate_upload_url("My Space", created_by="u@example.com", actor_sub="sub-1")
    status = svc._read_status(res["space_id"])
    assert status["source_type"] == "upload"
    assert status["created_by_sub"] == "sub-1"


def test_start_upload_analysis_by_owner_keeps_owner_key():
    svc = CodeKBService.instance()
    gen = svc.generate_upload_url("Up Space", created_by="u@example.com", actor_sub="sub-1")
    sid = gen["space_id"]
    # Put the "uploaded" zip so head_object succeeds.
    bucket = svc.kb_manager._get_s3_bucket()
    svc.kb_manager.s3_client.put_object(Bucket=bucket, Key=svc._upload_s3_key(sid), Body=b"zip")

    with patch.object(svc, "_run_pipeline"):
        svc.start_upload_analysis(space_id=sid, space_name="Up Space", actor_sub="sub-1")

    status = svc._read_status(sid)
    assert status["created_by_sub"] == "sub-1"  # owner key unchanged


def test_start_upload_analysis_rejects_non_owner_and_preserves_owner_key():
    # A second signed-in caller must not be able to upload into someone else's
    # space and take over its owner key. Rejected before S3 is
    # touched, with a 404 that does not confirm the space exists.
    svc = CodeKBService.instance()
    gen = svc.generate_upload_url("Victim Space", created_by="u@example.com", actor_sub="sub-1")
    sid = gen["space_id"]

    with patch.object(svc, "_run_pipeline") as run:
        with pytest.raises(ServiceError) as ei:
            svc.start_upload_analysis(space_id=sid, space_name="Victim Space", actor_sub="sub-2")
        run.assert_not_called()
    assert ei.value.status_code == 404

    status = svc._read_status(sid)
    assert status["created_by_sub"] == "sub-1"  # owner key not reassigned
    assert status["status"] == "PENDING_UPLOAD"  # status not clobbered


def test_start_upload_analysis_rejects_reupload_into_existing_space():
    # Re-upload into an already-created space is not supported. The owner reaches
    # the state guard and gets an honest 409, not a misleading "no access".
    svc = CodeKBService.instance()
    sid = "already-ready-space"
    _seed_status(
        svc, sid, {"space_id": sid, "status": "READY", "source_type": "upload", **{
            "created_by": "u@example.com", "created_by_sub": "sub-1",
        }}
    )
    bucket = svc.kb_manager._get_s3_bucket()
    svc.kb_manager.s3_client.put_object(Bucket=bucket, Key=svc._upload_s3_key(sid), Body=b"zip")

    with patch.object(svc, "_run_pipeline") as run:
        with pytest.raises(ServiceError) as ei:
            svc.start_upload_analysis(space_id=sid, space_name="X", actor_sub="sub-1")
        run.assert_not_called()
    assert ei.value.status_code == 409
    assert svc._read_status(sid)["status"] == "READY"  # untouched
