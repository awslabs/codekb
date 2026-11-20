"""Unit tests for the Bedrock guardrail attach helper.

Pure config -> invoke_model kwargs mapping; no AWS, no network.
"""

import logging

import pytest

import src.core.bedrock_guardrail as gr
from src.core.bedrock_guardrail import (
    GuardrailTarget,
    get_guardrail_kwargs,
    validate_guardrail_config,
)
from src.core.logging_utils import ConfigurationError


def _set(monkeypatch, *, enabled=True, guardrail_id="", version="DRAFT"):
    """Stub require_config with the given guardrail settings."""
    values = {
        "llm.providers.bedrock.guardrail.enabled": enabled,
        "llm.providers.bedrock.guardrail.id": guardrail_id,
        "llm.providers.bedrock.guardrail.version": version,
    }
    monkeypatch.setattr(gr, "require_config", lambda key: values[key])


def test_boto3_target_uses_boto3_keys(monkeypatch):
    _set(monkeypatch, enabled=True, guardrail_id="gr-123", version="7")
    assert get_guardrail_kwargs(GuardrailTarget.BOTO3_INVOKE_MODEL) == {
        "guardrailIdentifier": "gr-123",
        "guardrailVersion": "7",
    }


def test_strands_target_uses_strands_keys(monkeypatch):
    _set(monkeypatch, enabled=True, guardrail_id="gr-123", version="7")
    assert get_guardrail_kwargs(GuardrailTarget.STRANDS_BEDROCK_MODEL) == {
        "guardrail_id": "gr-123",
        "guardrail_version": "7",
    }


def test_version_coerced_to_str(monkeypatch):
    _set(monkeypatch, enabled=True, guardrail_id="gr-123", version=7)
    kwargs = get_guardrail_kwargs(GuardrailTarget.BOTO3_INVOKE_MODEL)
    assert kwargs["guardrailVersion"] == "7"


def test_disabled_attaches_nothing(monkeypatch):
    _set(monkeypatch, enabled=False, guardrail_id="gr-123")
    assert get_guardrail_kwargs(GuardrailTarget.BOTO3_INVOKE_MODEL) == {}
    assert get_guardrail_kwargs(GuardrailTarget.STRANDS_BEDROCK_MODEL) == {}


def test_attach_path_does_not_warn(monkeypatch, caplog):
    """The attach path is quiet; the missing-id warning is a startup concern."""
    _set(monkeypatch, enabled=True, guardrail_id="gr-123")
    with caplog.at_level(logging.WARNING):
        get_guardrail_kwargs(GuardrailTarget.BOTO3_INVOKE_MODEL)
        get_guardrail_kwargs(GuardrailTarget.BOTO3_INVOKE_MODEL)
    assert not caplog.records


def test_validate_fails_when_enabled_without_id(monkeypatch):
    _set(monkeypatch, enabled=True, guardrail_id="", version="DRAFT")
    with pytest.raises(ConfigurationError, match="CODEKB_BEDROCK_GUARDRAIL_ID"):
        validate_guardrail_config()


def test_validate_fails_when_enabled_without_version(monkeypatch):
    _set(monkeypatch, enabled=True, guardrail_id="gr-123", version="")
    with pytest.raises(ConfigurationError, match="CODEKB_BEDROCK_GUARDRAIL_VERSION"):
        validate_guardrail_config()


def test_validate_passes_when_configured(monkeypatch, caplog):
    _set(monkeypatch, enabled=True, guardrail_id="gr-123", version="DRAFT")
    with caplog.at_level(logging.WARNING):
        validate_guardrail_config()  # does not raise
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_validate_warns_when_disabled(monkeypatch, caplog):
    _set(monkeypatch, enabled=False, guardrail_id="")
    with caplog.at_level(logging.WARNING):
        validate_guardrail_config()  # does not raise
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "disabled" in warnings[0].message.lower()
