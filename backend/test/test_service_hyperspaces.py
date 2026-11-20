"""Tests for hyperspace CRUD on CodeKBService.

Hyperspaces are S3-only (hyperspaces/<id>/config.json). Creating one
validates every member space is READY. Note that hyperspace ids are
slugified with a random suffix, so tests capture the returned id from
create_hyperspace rather than predicting it.
"""

import json

import pytest

from src.service.codekb_service import (
    CodeKBService,
    HyperspaceNotFoundError,
    ServiceError,
    SpaceNotFoundError,
)


def _seed_status(codekb_service: CodeKBService, space_id: str, status_doc: dict) -> None:
    bucket = codekb_service.kb_manager._get_s3_bucket()
    codekb_service.kb_manager.s3_client.put_object(
        Bucket=bucket,
        Key=f"spaces/{space_id}/status.json",
        Body=json.dumps(status_doc).encode("utf-8"),
        ContentType="application/json",
    )


def _seed_ready(codekb_service: CodeKBService, space_id: str) -> None:
    _seed_status(
        codekb_service,
        space_id,
        {
            "status": "READY",
            "space_name": space_id,
            "component_count": 1,
        },
    )


class TestCreateHyperspace:
    def test_allows_a_single_space(self):
        # A hyperspace is a grouping, not a join — one member is legitimate
        # (e.g. a placeholder you grow later, or a single large monorepo).
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "lonely-space")

        cfg = codekb_service.create_hyperspace("Solo", ["lonely-space"])

        assert cfg["space_ids"] == ["lonely-space"]
        assert cfg["hyperspace_id"].startswith("solo-")

    def test_requires_at_least_one_space(self):
        codekb_service = CodeKBService.instance()
        with pytest.raises(ServiceError):
            codekb_service.create_hyperspace("Empty", [])

    def test_fails_when_member_space_missing(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "present-space")
        with pytest.raises(SpaceNotFoundError):
            codekb_service.create_hyperspace("Mixed", ["present-space", "absent-space"])

    def test_fails_when_member_not_ready(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "ready-one")
        _seed_status(codekb_service, "pending-one", {"status": "ANALYZING"})
        with pytest.raises(ServiceError):
            codekb_service.create_hyperspace("BadStatus", ["ready-one", "pending-one"])

    def test_accepts_a_member_whose_refresh_failed(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "steady-one")
        # A failed refresh keeps the last indexed snapshot, so the space stays usable.
        _seed_status(codekb_service, "refresh-failed-one", {"status": "REFRESH_FAILED"})

        cfg = codekb_service.create_hyperspace("Mixed", ["steady-one", "refresh-failed-one"])

        assert cfg["space_ids"] == ["steady-one", "refresh-failed-one"]

    def test_creates_and_persists(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "hs-space-a")
        _seed_ready(codekb_service, "hs-space-b")

        cfg = codekb_service.create_hyperspace("Team Platform", ["hs-space-a", "hs-space-b"])
        # id is slugified + random suffix, so check the prefix instead.
        assert cfg["hyperspace_id"].startswith("team-platform-")
        assert cfg["space_ids"] == ["hs-space-a", "hs-space-b"]
        assert cfg["name"] == "Team Platform"
        assert cfg.get("created_at")


class TestReadAndList:
    def test_get_returns_enriched_config(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "enrich-a")
        _seed_ready(codekb_service, "enrich-b")
        cfg = codekb_service.create_hyperspace("Enrichment", ["enrich-a", "enrich-b"])

        fetched = codekb_service.get_hyperspace(cfg["hyperspace_id"])
        assert fetched["hyperspace_id"] == cfg["hyperspace_id"]
        assert len(fetched["spaces"]) == 2
        assert all(s["status"] == "READY" for s in fetched["spaces"])
        assert fetched["invalid"] is False

    def test_invalid_flag_when_member_is_gone(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "present-mem")
        _seed_ready(codekb_service, "transient-mem")
        cfg = codekb_service.create_hyperspace("Transient", ["present-mem", "transient-mem"])

        # Delete the status.json for one member so enrichment flags it missing.
        bucket = codekb_service.kb_manager._get_s3_bucket()
        codekb_service.kb_manager.s3_client.delete_object(
            Bucket=bucket, Key="spaces/transient-mem/status.json"
        )

        fetched = codekb_service.get_hyperspace(cfg["hyperspace_id"])
        assert fetched["invalid"] is True
        assert "transient-mem" in fetched["missing_space_ids"]

    def test_list_includes_created_hyperspace(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "in-list-a")
        _seed_ready(codekb_service, "in-list-b")
        cfg = codekb_service.create_hyperspace("In List", ["in-list-a", "in-list-b"])

        ids = [h["hyperspace_id"] for h in codekb_service.list_hyperspaces()]
        assert cfg["hyperspace_id"] in ids

    def test_get_unknown_raises(self):
        codekb_service = CodeKBService.instance()
        with pytest.raises(HyperspaceNotFoundError):
            codekb_service.get_hyperspace("never-existed")


class TestDeleteHyperspace:
    def test_delete_removes_from_list(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "del-a")
        _seed_ready(codekb_service, "del-b")
        cfg = codekb_service.create_hyperspace("Delete Me", ["del-a", "del-b"])
        hs_id = cfg["hyperspace_id"]

        assert any(h["hyperspace_id"] == hs_id for h in codekb_service.list_hyperspaces())

        result = codekb_service.delete_hyperspace(hs_id)
        assert result["deleted"] is True

        assert not any(h["hyperspace_id"] == hs_id for h in codekb_service.list_hyperspaces())

    def test_delete_unknown_raises(self):
        codekb_service = CodeKBService.instance()
        with pytest.raises(HyperspaceNotFoundError):
            codekb_service.delete_hyperspace("never-created")


class TestUpdateHyperspace:
    def test_name_is_immutable_across_updates(self):
        """The name cannot be edited; a membership update preserves it."""
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "upd-name-a")
        _seed_ready(codekb_service, "upd-name-b")
        _seed_ready(codekb_service, "upd-name-c")
        cfg = codekb_service.create_hyperspace("Old Name", ["upd-name-a", "upd-name-b"])

        updated = codekb_service.update_hyperspace(
            cfg["hyperspace_id"], space_ids=["upd-name-a", "upd-name-c"]
        )

        # Name is preserved from creation — there is no rename path.
        assert updated["name"] == "Old Name"
        assert updated["space_ids"] == ["upd-name-a", "upd-name-c"]
        assert updated.get("updated_at")
        # id and created_at are preserved
        assert updated["hyperspace_id"] == cfg["hyperspace_id"]
        assert updated["created_at"] == cfg["created_at"]

    def test_update_records_updated_by(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "upd-by-a")
        _seed_ready(codekb_service, "upd-by-b")
        _seed_ready(codekb_service, "upd-by-c")
        cfg = codekb_service.create_hyperspace("Audit", ["upd-by-a", "upd-by-b"])

        updated = codekb_service.update_hyperspace(
            cfg["hyperspace_id"],
            space_ids=["upd-by-a", "upd-by-c"],
            updated_by="alice@example.com",
        )

        assert updated["updated_by"] == "alice@example.com"

    def test_replace_spaces_swaps_membership(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "swap-a")
        _seed_ready(codekb_service, "swap-b")
        _seed_ready(codekb_service, "swap-c")
        cfg = codekb_service.create_hyperspace("Rotate", ["swap-a", "swap-b"])

        # Drop swap-b, add swap-c — the classic weekly rotation case.
        updated = codekb_service.update_hyperspace(cfg["hyperspace_id"], space_ids=["swap-a", "swap-c"])

        assert updated["space_ids"] == ["swap-a", "swap-c"]
        assert "swap-b" not in updated["space_ids"]

    def test_update_persists_to_s3(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "persist-a")
        _seed_ready(codekb_service, "persist-b")
        _seed_ready(codekb_service, "persist-c")
        cfg = codekb_service.create_hyperspace("Persisted", ["persist-a", "persist-b"])

        codekb_service.update_hyperspace(cfg["hyperspace_id"], space_ids=["persist-a", "persist-c"])

        # A fresh read should reflect the new membership; name is unchanged.
        fetched = codekb_service.get_hyperspace(cfg["hyperspace_id"])
        assert fetched["name"] == "Persisted"
        assert fetched["space_ids"] == ["persist-a", "persist-c"]

    def test_update_can_shrink_to_a_single_space(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "min-a")
        _seed_ready(codekb_service, "min-b")
        cfg = codekb_service.create_hyperspace("Minimum", ["min-a", "min-b"])

        updated = codekb_service.update_hyperspace(cfg["hyperspace_id"], space_ids=["min-a"])

        assert updated["space_ids"] == ["min-a"]

    def test_update_requires_minimum_one_space(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "empty-a")
        _seed_ready(codekb_service, "empty-b")
        cfg = codekb_service.create_hyperspace("Emptied", ["empty-a", "empty-b"])

        with pytest.raises(ServiceError):
            codekb_service.update_hyperspace(cfg["hyperspace_id"], space_ids=[])

    def test_update_fails_when_new_space_missing(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "miss-a")
        _seed_ready(codekb_service, "miss-b")
        cfg = codekb_service.create_hyperspace("Miss", ["miss-a", "miss-b"])

        with pytest.raises(SpaceNotFoundError):
            codekb_service.update_hyperspace(cfg["hyperspace_id"], space_ids=["miss-a", "not-there"])

    def test_update_fails_when_new_space_not_ready(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "ready-member")
        _seed_ready(codekb_service, "still-ready")
        cfg = codekb_service.create_hyperspace("NotReady", ["ready-member", "still-ready"])
        _seed_status(codekb_service, "busy-member", {"status": "ANALYZING"})

        with pytest.raises(ServiceError):
            codekb_service.update_hyperspace(cfg["hyperspace_id"], space_ids=["ready-member", "busy-member"])

    def test_update_requires_some_change(self):
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "noop-a")
        _seed_ready(codekb_service, "noop-b")
        cfg = codekb_service.create_hyperspace("NoOp", ["noop-a", "noop-b"])

        with pytest.raises(ServiceError):
            codekb_service.update_hyperspace(cfg["hyperspace_id"])

    def test_update_unknown_raises(self):
        codekb_service = CodeKBService.instance()
        with pytest.raises(HyperspaceNotFoundError):
            codekb_service.update_hyperspace("never-created", space_ids=["ghost-a", "ghost-b"])

    def test_update_repairs_invalid_hyperspace(self):
        """Rotating out a deleted member clears the invalid flag."""
        codekb_service = CodeKBService.instance()
        _seed_ready(codekb_service, "kept")
        _seed_ready(codekb_service, "going-away")
        _seed_ready(codekb_service, "replacement")
        cfg = codekb_service.create_hyperspace("Repair", ["kept", "going-away"])

        # Simulate the member being deleted — drop its status.json so
        # enrichment flags the hyperspace as invalid.
        bucket = codekb_service.kb_manager._get_s3_bucket()
        codekb_service.kb_manager.s3_client.delete_object(Bucket=bucket, Key="spaces/going-away/status.json")
        before = codekb_service.get_hyperspace(cfg["hyperspace_id"])
        assert before["invalid"] is True
        assert "going-away" in before["missing_space_ids"]

        updated = codekb_service.update_hyperspace(cfg["hyperspace_id"], space_ids=["kept", "replacement"])

        assert updated["invalid"] is False
        assert updated["missing_space_ids"] == []
        assert updated["space_ids"] == ["kept", "replacement"]


class TestHyperspaceAuthzData:
    """Phase 4: create persists created_by_sub, and membership is capped."""

    def test_persists_created_by_sub(self):
        svc = CodeKBService.instance()
        _seed_ready(svc, "owned-member")

        cfg = svc.create_hyperspace(
            "Owned", ["owned-member"], created_by="e@x.com", created_by_sub="sub-123"
        )

        assert cfg["created_by_sub"] == "sub-123"
        reread = svc.get_hyperspace(cfg["hyperspace_id"])
        assert reread["created_by_sub"] == "sub-123"

    def test_create_rejects_more_than_member_cap(self):
        # The cap is checked before member existence, so no spaces need seeding.
        from src.authz.hyperspace import MEMBER_CAP

        svc = CodeKBService.instance()
        with pytest.raises(ServiceError):
            svc.create_hyperspace("TooBig", [f"s{i}" for i in range(MEMBER_CAP + 1)])

    def test_update_rejects_more_than_member_cap(self):
        from src.authz.hyperspace import MEMBER_CAP

        svc = CodeKBService.instance()
        _seed_ready(svc, "grow-member")
        cfg = svc.create_hyperspace("Grow", ["grow-member"])
        with pytest.raises(ServiceError):
            svc.update_hyperspace(
                cfg["hyperspace_id"], space_ids=[f"s{i}" for i in range(MEMBER_CAP + 1)]
            )
