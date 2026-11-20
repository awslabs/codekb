"""A token embedded in a space's git URL never reaches refresh's logs or errors (M17)."""

import logging
import subprocess
from unittest.mock import MagicMock

import pytest

from src.analysis import git_ops
from src.analysis.errors import GitCommandError, RefreshFailed
from src.analysis.refresh_orchestrator import RefreshOrchestrator

TOKEN = "ghp_SECRET123"
URL = f"https://x-access-token:{TOKEN}@127.0.0.1:9/org/repo.git"


def _logged(caplog) -> str:
    return "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)


def test_clone_shallow_redacts_the_token_in_its_log_and_error(tmp_path, caplog):
    caplog.set_level(logging.INFO)

    # Port 9 refuses the connection at once, so git fails fast with the URL in hand.
    with pytest.raises(GitCommandError) as exc_info:
        git_ops.clone_shallow(URL, None, tmp_path / "clone", timeout_s=30)

    assert "https://127.0.0.1:9/org/repo.git" in exc_info.value.command
    assert TOKEN not in str(exc_info.value)
    assert TOKEN not in _logged(caplog)


@pytest.mark.parametrize(
    "call",
    [
        lambda d: git_ops.clone_shallow(URL, None, d / "clone"),
        lambda d: git_ops.fetch_specific_sha(d, "origin", "a" * 40),
        lambda d: git_ops.diff_name_status(d, "a" * 40, "b" * 40),
    ],
    ids=["clone", "fetch", "diff"],
)
def test_a_token_git_echoes_in_stderr_is_redacted_from_the_error(tmp_path, monkeypatch, call):
    # Faked: current git strips it from "unable to access", but older git and server messages don't.
    stderr = f"fatal: unable to access '{URL}/': The requested URL returned error: 403\n"
    monkeypatch.setattr(
        git_ops, "run_git", lambda argv, **kw: subprocess.CompletedProcess(argv, 128, "", stderr)
    )

    with pytest.raises(GitCommandError) as exc_info:
        call(tmp_path)

    assert TOKEN not in exc_info.value.stderr
    assert TOKEN not in str(exc_info.value)
    assert "'https://127.0.0.1:9/org/repo.git/'" in exc_info.value.stderr


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    monkeypatch.setattr("src.service.codekb_service.validate_git_url", lambda url: url)
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_url": URL, "git_commit_hash": "old"},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch.codekb_service.source_provider.work_path.return_value = tmp_path / "clone"
    return orch


@pytest.mark.parametrize(
    "outcome",
    [
        "new-sha",
        GitCommandError("git clone", 128, "fatal: unable to access"),
        subprocess.TimeoutExpired(["git", "clone", URL], 300),
    ],
    ids=["done", "failed", "timeout"],
)
def test_step_clone_never_logs_the_token(orchestrator, caplog, outcome):
    caplog.set_level(logging.INFO)
    if isinstance(outcome, str):
        orchestrator.git_ops.clone_shallow.return_value = outcome
    else:
        orchestrator.git_ops.clone_shallow.side_effect = outcome

    try:
        orchestrator._step_clone()
    except RefreshFailed as exc:
        assert TOKEN not in str(exc)

    assert "127.0.0.1:9/org/repo.git" in _logged(caplog)
    assert TOKEN not in _logged(caplog)
