"""Tests for the Bedrock token-usage accounting utility.

Covers accumulation, merging, serialization round-trips, and the two
extractors that pull authoritative counts from the LLM-inference response
shapes the codebase produces (Anthropic invoke_model, Strands agent metrics).
Embedding tokens are intentionally not tracked.
"""

from types import SimpleNamespace

from src.core.token_usage import (
    OP_AGENT_CHAT,
    OP_SPACE_CREATION,
    TokenUsage,
    from_anthropic_invoke,
    from_openai_invoke,
    from_strands_metrics,
    pricing_url,
)


CLAUDE = "us.anthropic.claude-opus-4-6-v1"
QWEN = "qwen2.5-72b-instruct-v1:0"


class TestTokenUsageAccumulation:
    def test_add_accumulates_per_model(self):
        u = TokenUsage()
        u.add(CLAUDE, input=100, output=50)
        u.add(CLAUDE, input=10, output=5)
        assert u.by_model[CLAUDE].input == 110
        assert u.by_model[CLAUDE].output == 55

    def test_add_keeps_models_separate(self):
        u = TokenUsage()
        u.add(CLAUDE, input=100, output=50)
        u.add("anthropic.claude-haiku", input=200, output=20)
        assert u.by_model[CLAUDE].input == 100
        assert u.by_model["anthropic.claude-haiku"].input == 200

    def test_negative_and_none_coerced_to_zero(self):
        u = TokenUsage()
        u.add(CLAUDE, input=-5, output=None)
        assert u.by_model[CLAUDE].input == 0
        assert u.by_model[CLAUDE].output == 0

    def test_empty_model_id_recorded_as_unknown(self):
        u = TokenUsage()
        u.add("", input=10)
        assert u.by_model["unknown"].input == 10

    def test_is_empty(self):
        u = TokenUsage()
        assert u.is_empty()
        u.add(CLAUDE, input=1)
        assert not u.is_empty()

    def test_is_empty_with_zero_counts(self):
        u = TokenUsage()
        u.add(CLAUDE, input=0, output=0)
        assert u.is_empty()


class TestTotals:
    def test_totals_sum_across_models(self):
        u = TokenUsage()
        u.add(CLAUDE, input=100, output=50)
        u.add("anthropic.claude-haiku", input=200, output=20)
        totals = u.totals()
        assert totals == {"input": 300, "output": 70, "total": 370}


class TestMerge:
    def test_merge_combines_usages(self):
        a = TokenUsage().add(CLAUDE, input=100, output=50)
        b = TokenUsage().add(CLAUDE, input=20, output=10).add("haiku", input=5, output=1)
        a.merge(b)
        assert a.by_model[CLAUDE].input == 120
        assert a.by_model[CLAUDE].output == 60
        assert a.by_model["haiku"].input == 5

    def test_merge_none_is_noop(self):
        a = TokenUsage().add(CLAUDE, input=100)
        a.merge(None)
        assert a.by_model[CLAUDE].input == 100


class TestSerialization:
    def test_round_trip(self):
        u = TokenUsage().add(CLAUDE, input=100, output=50).add("haiku", input=5, output=1)
        restored = TokenUsage.from_dict(u.to_dict())
        assert restored.to_dict() == u.to_dict()

    def test_to_dict_includes_derived_totals(self):
        u = TokenUsage().add(CLAUDE, input=100, output=50)
        d = u.to_dict()
        assert d["totals"]["total"] == 150
        assert d["by_model"][CLAUDE] == {"input": 100, "output": 50}

    def test_from_dict_ignores_stale_totals(self):
        # Totals are always recomputed, never trusted from input.
        restored = TokenUsage.from_dict(
            {"by_model": {CLAUDE: {"input": 10, "output": 5}}, "totals": {"total": 9999}}
        )
        assert restored.totals()["total"] == 15

    def test_from_dict_empty(self):
        assert TokenUsage.from_dict(None).is_empty()
        assert TokenUsage.from_dict({}).is_empty()

    def test_thinking_is_serialized_only_when_present(self):
        u = TokenUsage().add(CLAUDE, input=100, output=50, thinking=30).add("haiku", output=5)
        d = u.to_dict()
        assert d["by_model"][CLAUDE] == {"input": 100, "output": 50, "thinking": 30}
        assert d["by_model"]["haiku"] == {"input": 0, "output": 5}
        # Thinking is part of output, so it doesn't add to the total.
        assert d["totals"] == {"input": 100, "output": 55, "total": 155, "thinking": 30}
        assert TokenUsage.from_dict(d).to_dict() == d

    def test_merge_carries_thinking(self):
        a = TokenUsage().add(CLAUDE, output=10, thinking=4)
        a.merge(TokenUsage().add(CLAUDE, output=10, thinking=6))
        assert a.by_model[CLAUDE].thinking == 10


class TestExtractors:
    def test_from_anthropic_invoke(self):
        body = {"usage": {"input_tokens": 1200, "output_tokens": 340}}
        u = from_anthropic_invoke(body, CLAUDE)
        assert u.by_model[CLAUDE].input == 1200
        assert u.by_model[CLAUDE].output == 340

    def test_from_anthropic_invoke_missing_usage(self):
        u = from_anthropic_invoke({}, CLAUDE)
        assert u.is_empty()

    def test_from_anthropic_invoke_reads_thinking_tokens(self):
        body = {
            "usage": {
                "input_tokens": 52,
                "output_tokens": 498,
                "output_tokens_details": {"thinking_tokens": 312},
            }
        }
        u = from_anthropic_invoke(body, CLAUDE)
        assert u.by_model[CLAUDE].output == 498
        assert u.by_model[CLAUDE].thinking == 312

    def test_from_openai_invoke(self):
        body = {"usage": {"prompt_tokens": 800, "completion_tokens": 250, "total_tokens": 1050}}
        u = from_openai_invoke(body, QWEN)
        assert u.by_model[QWEN].input == 800
        assert u.by_model[QWEN].output == 250

    def test_from_openai_invoke_missing_usage(self):
        u = from_openai_invoke({}, QWEN)
        assert u.is_empty()

    def test_from_openai_invoke_partial_usage(self):
        body = {"usage": {"prompt_tokens": 400}}
        u = from_openai_invoke(body, QWEN)
        assert u.by_model[QWEN].input == 400
        assert u.by_model[QWEN].output == 0

    def test_from_strands_metrics(self):
        result = SimpleNamespace(
            metrics=SimpleNamespace(
                accumulated_usage={"inputTokens": 500, "outputTokens": 120, "totalTokens": 620}
            )
        )
        u = from_strands_metrics(result, CLAUDE)
        assert u.by_model[CLAUDE].input == 500
        assert u.by_model[CLAUDE].output == 120

    def test_from_strands_metrics_no_metrics(self):
        assert from_strands_metrics(SimpleNamespace(), CLAUDE).is_empty()
        assert from_strands_metrics(object(), CLAUDE).is_empty()


class TestPricingUrl:
    def test_pricing_url_default(self):
        # Falls back to the canonical Bedrock pricing page.
        assert "aws.amazon.com" in pricing_url()
        assert "bedrock" in pricing_url()


class TestOperationConstants:
    def test_constants_are_stable_strings(self):
        assert OP_SPACE_CREATION == "space_creation"
        assert OP_AGENT_CHAT == "agent_chat"
