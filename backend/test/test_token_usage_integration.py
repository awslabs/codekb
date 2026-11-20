"""Integration-ish tests for token-usage capture across the three operations.

Covers:
- the recurring per-operation usage record helper (overview regen / refresh),
- Space Overview generator usage accumulation,
- Hopper chat (run_agent_query) returning per-turn token usage.

These use lightweight fakes — no AWS, no real Bedrock calls.
"""

from types import SimpleNamespace

from src.service.codekb_service import CodeKBService
from src.vector.space_overview_generator import SpaceOverviewGenerator


CLAUDE = "us.anthropic.claude-opus-4-6-v1"


def _strands_result(inp: int, out: int):
    return SimpleNamespace(
        metrics=SimpleNamespace(
            accumulated_usage={"inputTokens": inp, "outputTokens": out, "totalTokens": inp + out}
        )
    )


class TestRecurringUsageRecord:
    def test_first_run_is_initial(self):
        run = {
            "by_model": {CLAUDE: {"input": 100, "output": 50}},
            "totals": {"input": 100, "output": 50, "total": 150},
        }
        rec = CodeKBService._record_recurring_usage(None, run, trigger="initial")

        assert rec["generation_count"] == 1
        assert rec["last_generation"]["trigger"] == "initial"
        assert rec["cumulative"]["totals"]["total"] == 150
        assert len(rec["recent"]) == 1

    def test_second_run_accumulates_cumulative(self):
        run1 = {"by_model": {CLAUDE: {"input": 100, "output": 50}}}
        rec1 = CodeKBService._record_recurring_usage(None, run1, trigger="initial")

        run2 = {"by_model": {CLAUDE: {"input": 20, "output": 10}}}
        rec2 = CodeKBService._record_recurring_usage(rec1, run2, trigger="manual")

        assert rec2["generation_count"] == 2
        assert rec2["last_generation"]["trigger"] == "manual"
        # cumulative = 150 + 30
        assert rec2["cumulative"]["totals"]["total"] == 180
        assert len(rec2["recent"]) == 2

    def test_extra_fields_recorded_on_last(self):
        run = {"by_model": {CLAUDE: {"input": 1, "output": 1}}}
        rec = CodeKBService._record_recurring_usage(
            None, run, trigger="manual", extra={"documents": 4}
        )
        assert rec["last_generation"]["documents"] == 4

    def test_custom_count_and_last_keys_for_refresh(self):
        run = {"by_model": {CLAUDE: {"input": 5, "output": 5}}}
        rec = CodeKBService._record_recurring_usage(
            None,
            run,
            trigger="space_refresh",
            count_key="update_count",
            last_key="last_update",
        )
        assert rec["update_count"] == 1
        assert "last_update" in rec
        assert "last_generation" not in rec


class TestOverviewGeneratorCapture:
    def test_capture_usage_accumulates_across_calls(self):
        gen = SpaceOverviewGenerator(space_id="space")
        gen._capture_usage(_strands_result(500, 120))
        gen._capture_usage(_strands_result(50, 20))

        totals = gen.token_usage.totals()
        assert totals["input"] == 550
        assert totals["output"] == 140

    def test_capture_usage_tolerates_missing_metrics(self):
        gen = SpaceOverviewGenerator(space_id="space")
        gen._capture_usage(object())
        assert gen.token_usage.is_empty()


class TestRunAgentQueryUsage:
    def test_returns_token_usage(self, monkeypatch):
        from src.agent import codekb_agent

        class _FakeAgent:
            def __call__(self, message):
                return _strands_result(800, 300)

        monkeypatch.setattr(codekb_agent, "create_agent", lambda *a, **k: _FakeAgent())

        response, history, token_usage = codekb_agent.run_agent_query(["space"], "what does X do?")

        assert token_usage["totals"]["input"] == 800
        assert token_usage["totals"]["output"] == 300
        assert history[-1]["role"] == "assistant"
