"""The overview generator must attach the always-on guardrail to its BedrockModel.

Regression guard for the call site the guardrail change could easily miss —
under the deploy's IAM deny, an overview BedrockModel without the guardrail
would fail closed.
"""

import src.vector.overview_common as oc
from src.vector.overview_common import OverviewGeneratorBase


def test_create_model_attaches_guardrail(monkeypatch):
    captured: dict = {}

    class _FakeModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(oc, "BedrockModel", _FakeModel)
    monkeypatch.setattr(
        oc,
        "get_guardrail_kwargs",
        lambda target: {"guardrail_id": "gr-x", "guardrail_version": "DRAFT"},
    )

    OverviewGeneratorBase(log_prefix="[test]")._create_model()

    assert captured["guardrail_id"] == "gr-x"
    assert captured["guardrail_version"] == "DRAFT"
