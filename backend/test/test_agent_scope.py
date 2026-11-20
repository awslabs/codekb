"""The agent's all-spaces scope covers every space that can be queried."""

import contextvars
from unittest.mock import MagicMock

from src.agent import codekb_agent


def test_all_spaces_scope_includes_spaces_whose_refresh_failed(monkeypatch):
    svc = MagicMock()
    svc.list_spaces.return_value = [
        {"space_id": "ready", "status": "READY"},
        {"space_id": "refresh-failed", "status": "REFRESH_FAILED"},
        {"space_id": "analyzing", "status": "ANALYZING"},
    ]
    monkeypatch.setattr(codekb_agent, "_codekb_service", svc)

    # A fresh context, so the resolved scope doesn't leak into other tests.
    assert contextvars.copy_context().run(codekb_agent._sids) == ["ready", "refresh-failed"]
