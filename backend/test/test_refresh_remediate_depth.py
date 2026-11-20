"""Refresh fetches the last indexed commit once and fails fast with git's reason (M15)."""

import os
import subprocess
from unittest.mock import MagicMock

import pytest

from src.analysis import git_ops
from src.analysis.errors import GitCommandError, RefreshFailed
from src.analysis.refresh_orchestrator import RefreshOrchestrator


def _orchestrator(tmp_path, prior_sha="abc123def4567890"):
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_commit_hash": prior_sha},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch._clone_dir = tmp_path
    return orch


def test_a_fetched_prior_commit_completes_the_step(tmp_path):
    orch = _orchestrator(tmp_path)

    orch._step_remediate_depth()

    orch.git_ops.fetch_specific_sha.assert_called_once()
    orch.codekb_service._update_pipeline_status.assert_called_with(
        "space", "REFRESHING", "remediate_depth", "completed"
    )


def test_a_failed_fetch_fails_with_gits_reason_and_no_fallback(tmp_path):
    orch = _orchestrator(tmp_path)
    orch.git_ops.fetch_specific_sha.side_effect = GitCommandError(
        "git fetch", 128, "fatal: remote error: upload-pack: not our ref abc123"
    )

    with pytest.raises(RefreshFailed) as exc_info:
        orch._step_remediate_depth()

    assert exc_info.value.reason == "prior_commit_unavailable"
    assert "not our ref" in exc_info.value.detail
    assert "delete this Space and create it again" in exc_info.value.detail
    assert [c[0] for c in orch.git_ops.method_calls] == ["fetch_specific_sha"]


def test_a_server_that_refuses_sha_fetches_gets_its_own_remedy(tmp_path):
    orch = _orchestrator(tmp_path)
    orch.git_ops.fetch_specific_sha.side_effect = GitCommandError(
        "git fetch", 128, "error: Server does not allow request for unadvertised object abc123"
    )

    with pytest.raises(RefreshFailed) as exc_info:
        orch._step_remediate_depth()

    assert "uploadpack.allowReachableSHA1InWant" in exc_info.value.detail
    assert "delete this Space" not in exc_info.value.detail


def test_a_fetch_timeout_fails_the_step(tmp_path):
    orch = _orchestrator(tmp_path)
    orch.git_ops.fetch_specific_sha.side_effect = subprocess.TimeoutExpired("git fetch", 120)

    with pytest.raises(RefreshFailed) as exc_info:
        orch._step_remediate_depth()

    assert exc_info.value.reason == "fetch_timeout"


def test_fetch_specific_sha_fetches_an_older_commit_and_raises_for_a_missing_one(tmp_path):
    def git(cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    env = {**os.environ, "GIT_ALLOW_PROTOCOL": "file"}
    work, bare, clone = tmp_path / "work", tmp_path / "remote.git", tmp_path / "clone"
    work.mkdir()
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(work, "init", "-q", "-b", "main")
    for n in (1, 2):
        (work / "a.py").write_text(f"x = {n}\n")
        git(work, "add", "-A")
        git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"c{n}")
    older = git(work, "rev-parse", "HEAD~1")
    git(work, "push", "-q", f"file://{bare}", "main")
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{bare}", str(clone))

    git_ops.fetch_specific_sha(clone, "origin", older, env=env)
    assert git(clone, "rev-parse", "--verify", f"{older}^{{commit}}") == older

    with pytest.raises(GitCommandError) as exc_info:
        git_ops.fetch_specific_sha(clone, "origin", "0" * 40, env=env)
    assert exc_info.value.stderr
