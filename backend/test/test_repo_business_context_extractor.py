"""Tests for compact repository business context extraction helpers."""

from pathlib import Path

import pytest

from src.analysis.repo_business_context_extractor import RepoBusinessContextExtractor
from src.core.llm_settings import LlmSettings, Workload
from src.core.models import Component, ComponentAttributes, ComponentType, Visibility
from test.bedrock_fakes import FakeBedrockClient, anthropic_body


def _component(
    component_id: str,
    component_type: ComponentType,
    *,
    path: str = "src/domain.py",
    parent_component: str | None = None,
    docs: str | None = None,
) -> Component:
    return Component(
        id=component_id,
        type=component_type,
        file_path=Path(path),
        line_number=1,
        end_line_number=5,
        language="python",
        parent_component=parent_component,
        attributes=ComponentAttributes(
            documentation=docs,
            visibility=Visibility.PUBLIC,
            line_count=5,
        ),
    )


class TestRepoBusinessContextExtractor:
    def test_parse_response_accepts_fenced_json_and_cleans_terms(self):
        response = """```json
        {
          "business_terms": ["Order", "order", " Payment "],
          "technical_terms": ["Logger", 123, "config"],
          "business_modules": ["services/orders"],
          "entrypoint_patterns": ["*Controller"],
          "confidence": 0.8
        }
        ```"""

        parsed = RepoBusinessContextExtractor._parse_response(response)

        assert parsed["business_terms"] == ["order", "payment"]
        assert parsed["technical_terms"] == ["logger", "config"]
        assert parsed["business_modules"] == ["services/orders"]
        assert parsed["entrypoint_patterns"] == ["*controller"]
        assert parsed["confidence"] == 0.8

    def test_empty_context_is_fallback_safe(self):
        context = RepoBusinessContextExtractor.empty_context("disabled")

        assert context["business_terms"] == []
        assert context["technical_terms"] == []
        assert context["business_modules"] == []
        assert context["entrypoint_patterns"] == []
        assert context["source"] == "fallback"
        assert context["error"] == "disabled"

    def test_build_input_includes_class_and_named_value_sections(self, tmp_path):
        extractor = RepoBusinessContextExtractor()
        extractor._max_components = 1
        extractor._max_classes = 2
        extractor._max_named_values = 2
        components = [
            _component("repo::OrderWorkflow.run", ComponentType.FUNCTION),
            _component("repo::OrderWorkflow", ComponentType.CLASS, docs="Coordinates orders."),
            _component("repo::PaymentPolicy", ComponentType.CLASS),
            _component("repo::SHIPPING_REGION", ComponentType.CONSTANT),
            _component("repo::order_status", ComponentType.VARIABLE),
            _component("repo::tmp", ComponentType.VARIABLE),
            _component(
                "repo::OrderWorkflow.local_value",
                ComponentType.VARIABLE,
                parent_component="repo::OrderWorkflow",
            ),
        ]

        payload = extractor._build_input(tmp_path, components, {"repo::OrderWorkflow.run": []})

        assert "# Component Inventory" in payload
        assert "# Class Inventory" in payload
        assert "id=repo::OrderWorkflow" in payload
        assert "id=repo::PaymentPolicy" in payload
        assert "# Named Values Inventory" in payload
        assert "id=repo::SHIPPING_REGION" in payload
        assert "id=repo::order_status" in payload
        assert "id=repo::tmp" not in payload
        assert "id=repo::OrderWorkflow.local_value" not in payload

    def test_inventory_caps_classes_and_named_values(self):
        extractor = RepoBusinessContextExtractor()
        extractor._max_classes = 1
        extractor._max_named_values = 1
        components = [
            _component("repo::AClass", ComponentType.CLASS),
            _component("repo::BClass", ComponentType.CLASS),
            _component("repo::A_CONSTANT", ComponentType.CONSTANT),
            _component("repo::B_CONSTANT", ComponentType.CONSTANT),
        ]

        class_lines = extractor._class_inventory(components)
        named_value_lines = extractor._named_value_inventory(components)

        assert len(class_lines) == 1
        assert len(named_value_lines) == 1


class TestRepoBusinessContextExtractorResponseParsing:
    """_call_bedrock selects the answer by content-block *type*.

    Mirrors TestTopDownSummarizerResponseParsing — the two _call_bedrock
    implementations carry the same block-selection logic, so they need the same
    guarantees. With extended reasoning enabled the model emits a ``thinking``
    block ahead of its answer; selecting by position rather than by type would
    feed reasoning text into _parse_response.
    """

    def _extractor(self, client):
        e = RepoBusinessContextExtractor.__new__(RepoBusinessContextExtractor)
        e._bedrock_client = client
        e._llm = LlmSettings(Workload.BUSINESS_CONTEXT, model_id="anthropic.claude", max_tokens=512)
        e._bedrock_region = "us-east-1"
        return e

    def test_skips_thinking_block_and_returns_text(self):
        client = FakeBedrockClient(anthropic_body("{}", thinking="internal reasoning"))
        e = self._extractor(client)

        assert e._call_bedrock("INSTRUCTION", "repo metadata") == "{}"

    def test_thinking_text_is_never_returned(self):
        client = FakeBedrockClient(anthropic_body("{}", thinking="internal reasoning"))
        e = self._extractor(client)

        assert "internal reasoning" not in e._call_bedrock("INSTRUCTION", "repo metadata")

    def test_response_with_no_text_block_is_rejected(self):
        client = FakeBedrockClient(anthropic_body(thinking="only reasoning"))
        e = self._extractor(client)

        with pytest.raises(ValueError):
            e._call_bedrock("INSTRUCTION", "repo metadata")

    def test_empty_content_list_is_rejected(self):
        client = FakeBedrockClient(anthropic_body())
        e = self._extractor(client)

        with pytest.raises(ValueError):
            e._call_bedrock("INSTRUCTION", "repo metadata")


class TestRepoBusinessContextExtractorLlmSettings:
    """The request follows the resolved business_context settings."""

    def _extractor(self, client, **settings):
        e = RepoBusinessContextExtractor.__new__(RepoBusinessContextExtractor)
        e._bedrock_client = client
        e._llm = LlmSettings(
            Workload.BUSINESS_CONTEXT,
            model_id="eu.anthropic.claude-sonnet-5-5",
            max_tokens=2048,
            **settings,
        )
        e._bedrock_region = "us-east-1"
        return e

    def test_default_resolution_keeps_2048_max_tokens(self):
        e = RepoBusinessContextExtractor()

        assert e._llm.max_tokens == 2048

    def test_thinking_is_sent(self):
        from test.bedrock_fakes import CapturingBedrockClient

        client = CapturingBedrockClient(anthropic_body("{}"))
        e = self._extractor(client, thinking="between_tools")

        e._call_bedrock("INSTRUCTION", "repo metadata")

        body = client.sent_body()
        assert body["thinking"] == {"type": "between_tools"}
        assert body["max_tokens"] == 2048

    def test_rejected_settings_raise_a_configuration_error(self):
        from src.core.logging_utils import ConfigurationError
        from test.bedrock_fakes import RejectingBedrockClient

        e = self._extractor(
            RejectingBedrockClient("thinking.type: disabled is not supported"),
            thinking="disabled",
        )

        with pytest.raises(ConfigurationError, match="CODEKB_BUSINESS_CONTEXT_THINKING"):
            e._call_bedrock("INSTRUCTION", "repo metadata")

    def test_max_tokens_stop_without_text_names_the_setting(self):
        body = anthropic_body(thinking="")
        body["stop_reason"] = "max_tokens"
        e = self._extractor(FakeBedrockClient(body), thinking="adaptive")

        with pytest.raises(ValueError, match="CODEKB_BUSINESS_CONTEXT_MAX_TOKENS"):
            e._call_bedrock("INSTRUCTION", "repo metadata")
