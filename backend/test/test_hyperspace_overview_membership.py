"""Overview reads must use the members that produced the stored document."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.api.app import authz_exception_handler
from src.api.routes import hyperspaces
from src.authz.context import AuthContext
from src.authz.errors import AuthzError
from src.service.codekb_service import CodeKBService, ServiceError


@pytest.fixture
def overview(monkeypatch):
    svc = CodeKBService.instance()
    svc.write_status(
        "overview-public",
        {
            "space_id": "overview-public",
            "status": "READY",
            "visibility": "public",
            "git_url": "https://github.com/example/public",
        },
    )
    svc.write_status(
        "overview-private",
        {
            "space_id": "overview-private",
            "status": "READY",
            "source_type": "upload",
            "created_by_sub": "alice",
        },
    )
    cfg = svc.create_hyperspace(
        "Overview membership", ["overview-public", "overview-private"], created_by_sub="alice"
    )
    hid = cfg["hyperspace_id"]
    bucket = svc.kb_manager._get_s3_bucket()
    key = f"hyperspaces/{hid}/docs/system_overview.md"

    def save_document(members, content):
        current = svc.read_hyperspace_config(hid)
        response = svc.kb_manager.s3_client.put_object(
            Bucket=bucket, Key=key, Body=content.encode()
        )
        current.update(
            {
                "space_ids": members,
                "overview_status": "completed",
                "overview_member_snapshot": [{"space_id": sid} for sid in members],
                "hyperspace_overview_docs": [
                    {"key": "system_overview", "s3_path": key, "etag": response["ETag"]}
                ],
            }
        )
        svc._write_hyperspace(current)

    save_document(cfg["space_ids"], "Alice's private project details")
    for module in (
        "src.api.routes.hyperspaces",
        "src.authz.hyperspace",
        "src.authz.repo_access",
    ):
        monkeypatch.setattr(f"{module}.require_authorization", lambda: True)

    def identity(request):
        sub = request.headers.get("X-Test-Subject", "bob")
        return SimpleNamespace(sub=sub, email=f"{sub}@example.com")

    monkeypatch.setattr(hyperspaces, "require_current_user", identity)
    monkeypatch.setattr(
        hyperspaces,
        "current_auth_context",
        lambda request: AuthContext.from_identity(identity(request), service=svc),
    )
    app = FastAPI()
    app.include_router(hyperspaces.router, prefix="/api/hyperspaces")
    app.add_exception_handler(AuthzError, authz_exception_handler)
    with TestClient(app) as client:
        yield SimpleNamespace(
            svc=svc,
            hid=hid,
            bucket=bucket,
            key=key,
            client=client,
            path=f"/api/hyperspaces/{hid}",
            read_path=f"/api/hyperspaces/{hid}/overview/system_overview",
            owner={"X-Test-Subject": "alice"},
            save_document=save_document,
        )


def test_overview_requires_access_to_every_source(overview):
    assert overview.client.get(overview.read_path).status_code == 403
    response = overview.client.get(overview.read_path, headers=overview.owner)
    assert response.status_code == 200
    assert response.text == "Alice's private project details"


def test_removing_private_member_invalidates_overview_until_regenerated(overview):
    assert overview.client.get(overview.read_path).status_code == 403
    response = overview.client.put(
        overview.path, json={"space_ids": ["overview-public"]}, headers=overview.owner
    )
    assert response.status_code == 200
    assert response.json()["hyperspace_overview_docs"] == []
    assert "overview_status" not in response.json()
    assert overview.client.get(overview.read_path).status_code == 404
    assert overview.client.get(overview.read_path, headers=overview.owner).status_code == 404

    overview.save_document(["overview-public"], "Public project only")
    response = overview.client.get(overview.read_path)
    assert response.status_code == 200
    assert response.text == "Public project only"


@pytest.mark.parametrize("members", [["overview-public"], ["overview-private"]])
def test_preexisting_membership_mismatch_requires_regeneration(overview, members):
    # Simulate a membership edit made before the fix, with the old catalog intact.
    cfg = overview.svc.read_hyperspace_config(overview.hid)
    cfg["space_ids"] = members
    overview.svc._write_hyperspace(cfg)
    response = overview.client.get(overview.read_path, headers=overview.owner)
    assert response.status_code == 409
    assert "regenerate" in response.json()["detail"].lower()
    assert "private project details" not in response.text


@pytest.mark.parametrize("through_service", [False, True])
def test_deleted_private_member_does_not_make_overview_readable(overview, through_service):
    if through_service:
        asyncio.run(overview.svc.delete_space("overview-private"))
    else:
        overview.svc.kb_manager.s3_client.delete_object(
            Bucket=overview.bucket, Key="spaces/overview-private/status.json"
        )
    response = overview.client.get(overview.read_path)
    assert response.status_code == 403
    assert "private project details" not in response.text
    with pytest.raises(ServiceError) as exc:
        overview.svc.get_hyperspace_overview_document(overview.hid, "system_overview")
    assert exc.value.status_code == 409


@pytest.mark.parametrize("snapshot", [None, [], [{"space_id": "overview-public"}]])
def test_overview_without_complete_provenance_requires_regeneration(overview, snapshot):
    cfg = overview.svc.read_hyperspace_config(overview.hid)
    cfg["overview_member_snapshot"] = snapshot
    overview.svc._write_hyperspace(cfg)
    response = overview.client.get(overview.read_path, headers=overview.owner)
    assert response.status_code == 409


def test_legacy_document_without_etag_requires_regeneration(overview):
    cfg = overview.svc.read_hyperspace_config(overview.hid)
    cfg["hyperspace_overview_docs"][0].pop("etag")
    overview.svc._write_hyperspace(cfg)
    response = overview.client.get(overview.read_path, headers=overview.owner)
    assert response.status_code == 409


def test_orphaned_document_is_not_served(overview):
    cfg = overview.svc.read_hyperspace_config(overview.hid)
    cfg["hyperspace_overview_docs"] = []
    overview.svc._write_hyperspace(cfg)
    assert overview.client.get(overview.read_path, headers=overview.owner).status_code == 404


def test_reordering_members_preserves_overview(overview):
    response = overview.client.put(
        overview.path,
        json={"space_ids": ["overview-private", "overview-public"]},
        headers=overview.owner,
    )
    assert response.status_code == 200
    assert response.json()["overview_status"] == "completed"
    assert overview.client.get(overview.read_path, headers=overview.owner).status_code == 200
    assert overview.client.get(overview.read_path).status_code == 403


def test_member_permission_change_is_checked_on_each_read(overview):
    assert overview.client.get(overview.read_path, headers=overview.owner).status_code == 200
    status = overview.svc.read_status("overview-private")
    status["created_by_sub"] = "someone-else"
    overview.svc.write_status("overview-private", status)
    assert overview.client.get(overview.read_path, headers=overview.owner).status_code == 403


def test_document_replaced_after_authorization_is_not_served(overview, monkeypatch):
    overview.save_document(["overview-public"], "Public project only")
    original_read = overview.svc.get_hyperspace_overview_document

    def replace_then_read(*args, **kwargs):
        # Bob's check saw only the public member. Before S3 is read, another
        # request adds a private member and publishes a new document.
        overview.save_document(
            ["overview-public", "overview-private"], "Alice's private project details"
        )
        return original_read(*args, **kwargs)

    monkeypatch.setattr(overview.svc, "get_hyperspace_overview_document", replace_then_read)
    response = overview.client.get(overview.read_path)
    assert response.status_code == 409
    assert "private project details" not in response.text


@pytest.mark.parametrize("change_members", [False, True])
def test_generated_document_is_bound_to_its_original_members(overview, monkeypatch, change_members):
    def generate():
        if change_members:
            overview.svc.update_hyperspace(overview.hid, ["overview-public"])
        return {
            "documents": [
                {
                    "key": "system_overview",
                    "filename": "system_overview.md",
                    "title": "System Overview",
                    "content": "Alice's private project details",
                    "generated_at": "2026-01-01T00:00:00+00:00",
                }
            ],
            "stats": {"generated": 1, "failed": 0},
        }

    monkeypatch.setattr(overview.svc, "_member_overview_needs_generation", lambda _: False)
    monkeypatch.setattr(overview.svc, "_load_member_overview", lambda sid: {"space_id": sid})
    generator = MagicMock()
    generator.generate_all.side_effect = generate
    monkeypatch.setattr(
        "src.vector.hyperspace_overview_generator.HyperspaceOverviewGenerator",
        lambda **kwargs: generator,
    )
    result = overview.svc._run_hyperspace_overview(overview.hid)
    assert result["generated"] == 1
    cfg = overview.svc.read_hyperspace_config(overview.hid)
    assert cfg["hyperspace_overview_docs"][0]["etag"]
    response = overview.client.get(overview.read_path, headers=overview.owner)
    assert response.status_code == (409 if change_members else 200)
    if change_members:
        assert overview.client.get(overview.read_path).status_code == 409
