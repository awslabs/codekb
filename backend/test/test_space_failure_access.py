"""Failed uploads and deletions must remain accessible for cleanup or retry."""

import asyncio
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from src.authz import repo_access
from src.authz.context import AuthContext
from src.authz.errors import ReadDenied
from src.service.codekb_service import CodeKBService, ServiceError


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(repo_access, "require_authorization", lambda: True)
    return AuthContext(sub="space-owner", email="owner@example.com")


def assert_owner_can_recover(svc, sid, owner):
    status = svc.read_status(sid)
    assert status["status"] == "FAILED"
    assert status["created_by_sub"] == owner.sub
    assert status["created_by"] == owner.email
    repo_access.can_read(owner, status)
    repo_access.can_write(owner, status)
    assert svc.get_analysis_status(sid)["error"]
    assert any(s["space_id"] == sid for s in owner.visible_spaces())

    other = AuthContext(sub="other-user", email="other@example.com")
    with pytest.raises(ReadDenied):
        repo_access.can_read(other, status)
    with pytest.raises(ReadDenied):
        repo_access.can_write(other, status)


def assert_delete_succeeds(svc, sid, bucket):
    svc.mark_space_deleting(sid)
    assert asyncio.run(svc.delete_space(sid))["deleted"] is True
    assert svc.read_status(sid) is None
    assert not svc.kb_manager.s3_client.list_objects_v2(Bucket=bucket, Prefix=f"spaces/{sid}/").get(
        "Contents"
    )


def test_oversized_upload_remains_accessible_to_owner(owner, monkeypatch):
    svc = CodeKBService.instance()
    sid = svc.generate_upload_url("Too large", created_by=owner.email, actor_sub=owner.sub)[
        "space_id"
    ]
    bucket = svc.kb_manager._get_s3_bucket()
    upload_key = svc._upload_s3_key(sid)
    svc.kb_manager.s3_client.put_object(Bucket=bucket, Key=upload_key, Body=b"zip")
    monkeypatch.setattr(svc, "_max_upload_bytes", lambda: 2)
    run_pipeline = MagicMock()
    monkeypatch.setattr(svc, "_run_pipeline", run_pipeline)

    with pytest.raises(ServiceError, match="too large"):
        svc.start_upload_analysis(sid, "Too large", created_by=owner.email, actor_sub=owner.sub)

    run_pipeline.assert_not_called()
    with pytest.raises(ClientError) as exc:
        svc.kb_manager.s3_client.head_object(Bucket=bucket, Key=upload_key)
    assert exc.value.response["Error"]["Code"] == "404"
    assert_owner_can_recover(svc, sid, owner)
    assert svc.read_status(sid)["source_type"] == "upload"
    assert_delete_succeeds(svc, sid, bucket)


@pytest.mark.parametrize("source", ["upload", "private-repo"])
@pytest.mark.parametrize("failure", ["vectors", "s3", "unexpected"])
def test_failed_delete_preserves_access_and_allows_retry(owner, monkeypatch, source, failure):
    svc = CodeKBService.instance()
    sid = f"failed-delete-{source}-{failure}"
    provenance = (
        {"source_type": "upload"}
        if source == "upload"
        else {
            "git_url": "https://github.com/example/private.git",
            "visibility": "private",
            "provider": "github",
            "repo_key": "github.com/example/private",
        }
    )
    if source == "private-repo":
        # Model a maintainer and an unrelated user without contacting GitHub.
        monkeypatch.setattr(
            repo_access,
            "repo_level",
            lambda ctx, *args: "maintain" if ctx.sub == owner.sub else "none",
        )
    svc.write_status(
        sid,
        {
            "space_id": sid,
            "space_name": "Retry deletion",
            "status": "READY",
            "created_by_sub": owner.sub,
            "created_by": owner.email,
            **provenance,
        },
    )
    bucket = svc.kb_manager._get_s3_bucket()
    s3 = svc.kb_manager.s3_client
    source_key = f"spaces/{sid}/source.tar.gz"
    s3.put_object(Bucket=bucket, Key=source_key, Body=b"source")
    repo_access.can_write(owner, svc.read_status(sid))
    svc.mark_space_deleting(sid)

    with monkeypatch.context() as fault:
        if failure == "vectors":
            fault.setattr(
                svc,
                "_vector_keys_for_space",
                MagicMock(side_effect=RuntimeError("vector store unavailable")),
            )
            message = "Could not delete the vectors"
        elif failure == "s3":

            def partial_delete(**kwargs):
                # status.json may already be gone when a later S3 operation fails.
                s3.delete_object(Bucket=bucket, Key=f"spaces/{sid}/status.json")
                raise RuntimeError("S3 deletion interrupted")

            fault.setattr(s3, "delete_objects", partial_delete)
            message = "Could not fully delete space"
        else:
            fault.setattr(
                svc,
                "_source_local_path",
                MagicMock(side_effect=RuntimeError("source cache unavailable")),
            )
            message = "Unexpected error during deletion"

        with pytest.raises(ServiceError, match=message):
            asyncio.run(svc.delete_space(sid))

    if failure != "unexpected":
        assert s3.get_object(Bucket=bucket, Key=source_key)["Body"].read() == b"source"
    assert_owner_can_recover(svc, sid, owner)
    status = svc.read_status(sid)
    assert status["space_name"] == "Retry deletion"
    for key, value in provenance.items():
        assert status[key] == value
    assert_delete_succeeds(svc, sid, bucket)
