import logging

import pytest

from src.vector.topdown_summarizer import (
    TopDownSummarizer,
    _OUTCOME_OK,
    _OUTCOME_TRUNCATED,
)
from src.vector.summarization_packer import PackedCall
from test.bedrock_fakes import (
    CapturingBedrockClient,
    FakeBedrockClient,
    anthropic_body,
)


class FakeRelevanceService:
    def __init__(self, tiers: dict[str, str]):
        self.tiers = tiers

    def get_business_relevance_tier(self, space_id: str, component_id: str) -> str | None:
        return self.tiers.get(component_id)

    def get_call_graph_with_source_diagnosed(self, space_id, component_id, depth):
        """Mirror the real service: delegate to get_call_graph_with_source and
        report a reason when it yields nothing, so the summarizer's diagnosed
        fetch path works against every fake (patched or subclassed)."""
        graph = self.get_call_graph_with_source(space_id, component_id, depth)
        return graph, (None if graph is not None else "component id not found in the catalog")


class TestTopDownSummarizerRelevanceFiltering:
    def test_eligible_components_omits_low_business_relevance(self):
        summarizer = TopDownSummarizer(
            "/tmp/project",
            "space",
            codekb_service=FakeRelevanceService(
                {
                    "space::low": "low",
                    "space::heuristic": "heuristic",
                    "space::medium": "medium",
                    "space::high": "high",
                }
            ),
            omit_low_business_relevance=True,
        )

        result = summarizer._eligible_components(
            ["space::low", "space::heuristic", "space::medium", "space::high"]
        )

        assert result == ["space::heuristic", "space::medium", "space::high"]
        assert summarizer.stats["skipped_low_business_relevance"] == 1

    def test_eligible_components_keeps_all_when_filter_disabled(self):
        summarizer = TopDownSummarizer(
            "/tmp/project",
            "space",
            codekb_service=FakeRelevanceService({"space::low": "low"}),
            omit_low_business_relevance=False,
        )

        result = summarizer._eligible_components(["space::low"])

        assert result == ["space::low"]
        assert summarizer.stats["skipped_low_business_relevance"] == 0


class TestTopDownSummarizerTokenCapture:
    def _summarizer(self):
        return TopDownSummarizer(
            "/tmp/project",
            "space",
            codekb_service=FakeRelevanceService({}),
        )

    def test_call_bedrock_accumulates_token_usage(self):
        s = self._summarizer()
        s._bedrock_client = FakeBedrockClient(
            anthropic_body(
                "a description",
                usage={"input_tokens": 1000, "output_tokens": 200},
            )
        )

        out, _ = s._call_bedrock("prompt", "input text")

        assert out == "a description"
        totals = s.token_usage.totals()
        assert totals["input"] == 1000
        assert totals["output"] == 200

    def test_token_usage_accumulates_across_calls(self):
        s = self._summarizer()
        s._bedrock_client = FakeBedrockClient(
            anthropic_body("x", usage={"input_tokens": 10, "output_tokens": 5})
        )

        s._call_bedrock("p", "i")
        s._call_bedrock("p", "i")

        assert s.token_usage.totals() == {"input": 20, "output": 10, "total": 30}

    def test_missing_usage_does_not_break_call(self):
        s = self._summarizer()
        s._bedrock_client = FakeBedrockClient(anthropic_body("ok"))

        out, _ = s._call_bedrock("p", "i")

        assert out == "ok"
        assert s.token_usage.is_empty()


class TestTopDownSummarizerResponseParsing:
    """_call_bedrock selects the answer by content-block *type*.

    The type filter is not incidental — with extended reasoning enabled the model
    emits a ``thinking`` block before its answer, so selecting "the first block"
    or "block[0]['text']" would return reasoning text (or KeyError). These tests
    pin that behaviour so the filter cannot be simplified away.
    """

    def _summarizer(self):
        return TopDownSummarizer(
            "/tmp/project",
            "space",
            codekb_service=FakeRelevanceService({}),
        )

    def test_skips_thinking_block_and_returns_text(self):
        s = self._summarizer()
        s._bedrock_client = FakeBedrockClient(
            anthropic_body("the answer", thinking="internal reasoning")
        )

        assert s._call_bedrock("p", "i")[0] == "the answer"

    def test_thinking_text_is_never_returned(self):
        s = self._summarizer()
        s._bedrock_client = FakeBedrockClient(
            anthropic_body("the answer", thinking="internal reasoning")
        )

        assert "internal reasoning" not in s._call_bedrock("p", "i")[0]

    def test_response_with_no_text_block_is_rejected(self):
        s = self._summarizer()
        # Reasoning emitted but the answer never arrived (truncation, stop
        # sequence). Must fail loudly rather than return the thinking text.
        s._bedrock_client = FakeBedrockClient(anthropic_body(thinking="only reasoning"))

        with pytest.raises(RuntimeError):
            s._call_bedrock("p", "i")

    def test_empty_content_list_is_rejected(self):
        s = self._summarizer()
        s._bedrock_client = FakeBedrockClient(anthropic_body())

        with pytest.raises(RuntimeError):
            s._call_bedrock("p", "i")


class TestFlooredCallIsRecorded:
    """A floored (truncated) call must surface reduced fidelity, not pass silently."""

    def _summarizer(self):
        return TopDownSummarizer("/tmp/project", "space", codekb_service=FakeRelevanceService({}))

    def test_floored_call_logs_reduced_fidelity(self, caplog, monkeypatch):
        s = self._summarizer()
        monkeypatch.setattr(s, "_generate_and_index", lambda text, label: 0)
        call = PackedCall(text="...", source_ids=["space::a.big"], floored=True)
        with caplog.at_level(logging.WARNING):
            s._run_packed_call(call, "space::a.big")
        assert any(
            "reduced fidelity" in r.message and "space::a.big" in r.message
            for r in caplog.records
        )

    def test_non_floored_call_does_not_warn(self, caplog, monkeypatch):
        s = self._summarizer()
        monkeypatch.setattr(s, "_generate_and_index", lambda text, label: 0)
        call = PackedCall(text="...", source_ids=["space::a.ok"], floored=False)
        with caplog.at_level(logging.WARNING):
            s._run_packed_call(call, "space::a.ok")
        assert not any("reduced fidelity" in r.message for r in caplog.records)


class TestTopDownSummarizerInputCap:
    def _summarizer(self):
        return TopDownSummarizer(
            "/tmp/project",
            "space",
            codekb_service=FakeRelevanceService({}),
        )

    def test_bounded_input_truncates_oversized(self):
        s = self._summarizer()
        s._packer_word_budget = 5
        out = s._bounded_input("x " * 500)   # 500 words -> truncated to 5
        assert len(out.split()) == 5

    def test_bounded_input_passes_through_within_cap(self):
        s = self._summarizer()
        s._packer_word_budget = 1000
        text = "y " * 500
        assert s._bounded_input(text) == text

    def test_zero_budget_disables_truncation(self):
        s = self._summarizer()
        s._packer_word_budget = 0
        text = "z " * 10000
        assert s._bounded_input(text) == text

    def test_call_bedrock_applies_cap(self):
        s = self._summarizer()
        s._packer_word_budget = 5
        client = CapturingBedrockClient(anthropic_body("ok"))
        s._bedrock_client = client

        s._call_bedrock("prompt", "q " * 500)

        content = client.sent_message_content()
        # Input truncated to 5 words; a 6th word would mean no truncation.
        assert "q q q q q" in content
        assert "q q q q q q" not in content


class TestTopDownSummarizerGuardrails:
    def _summarizer(self):
        return TopDownSummarizer(
            "/tmp/project",
            "space",
            codekb_service=FakeRelevanceService({}),
        )

    def test_should_stop_when_budget_exhausted(self):
        s = self._summarizer()
        s._token_budget = 100
        s.token_usage.add("m", input=100, output=0)  # at budget
        assert s._should_stop()  # returns a non-empty reason

    def test_should_not_stop_under_budget(self):
        s = self._summarizer()
        s._token_budget = 1000
        s.token_usage.add("m", input=100, output=0)
        assert s._should_stop() is None

    def test_zero_budget_never_stops_on_tokens(self):
        s = self._summarizer()
        s._token_budget = 0
        s.token_usage.add("m", input=10_000_000, output=0)
        assert s._should_stop() is None

    def test_should_stop_when_circuit_open(self):
        from src.core.circuit_breaker import CircuitBreaker

        s = self._summarizer()
        s._circuit_breaker = CircuitBreaker(1)
        s._circuit_breaker.record_failure()  # opens
        assert s._should_stop()

    def test_call_bedrock_failure_opens_circuit(self):
        from src.core.circuit_breaker import CircuitBreaker

        s = self._summarizer()
        s._circuit_breaker = CircuitBreaker(1)

        class _Boom:
            def invoke_model(self, **kwargs):
                raise RuntimeError("bedrock unavailable")

        s._bedrock_client = _Boom()

        with pytest.raises(RuntimeError):
            s._call_bedrock("p", "i")
        assert s._circuit_breaker.is_open
        assert s._should_stop()

    def test_call_bedrock_success_keeps_circuit_closed(self):
        from src.core.circuit_breaker import CircuitBreaker

        s = self._summarizer()
        s._circuit_breaker = CircuitBreaker(1)
        s._bedrock_client = FakeBedrockClient(anthropic_body("ok"))

        assert s._call_bedrock("p", "i")[0] == "ok"
        assert not s._circuit_breaker.is_open


class _FakeGraphService(FakeRelevanceService):
    """FakeRelevanceService plus just enough call-graph surface to drive
    summarize_all (with or without targets) end-to-end without any Bedrock calls."""

    def __init__(self, by_depth: dict[int, list[str]]):
        super().__init__({})
        self.by_depth = by_depth

    def get_max_depth(self, space_id: str) -> int:
        return max(self.by_depth, default=0)

    def get_call_depths(self, space_id: str) -> dict[str, int]:
        return {cid: d for d, ids in self.by_depth.items() for cid in ids}

    def get_components_by_depth(self, space_id: str, depth: int) -> list[str]:
        return list(self.by_depth.get(depth, []))


class TestStoppedEarlySurfacing:
    """A guardrail-truncated run must be distinguishable from a completed one."""

    def _tripped_breaker(self):
        from src.core.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(1)
        cb.record_failure()
        return cb

    def test_should_stop_records_first_reason_and_memoizes(self):
        s = TopDownSummarizer("/tmp/project", "space", codekb_service=FakeRelevanceService({}))
        s._token_budget = 10
        s.token_usage.add("m", input=10, output=0)

        first = s._should_stop()
        assert first == "token_budget_reached"
        assert s._stop_reason == first

        # A later, different guardrail firing does not overwrite the first reason.
        s._circuit_breaker = self._tripped_breaker()
        assert s._should_stop() == first

    def test_stop_reason_is_a_value_free_code(self):
        # The reason code lands in stats and service log lines; configured
        # budget and token counts belong in the detailed log message only.
        s = TopDownSummarizer("/tmp/project", "space", codekb_service=FakeRelevanceService({}))
        s._token_budget = 12345
        s.token_usage.add("m", input=99999, output=0)

        reason = s._should_stop()
        assert reason is not None
        assert "12345" not in reason
        assert "99999" not in reason

    def test_summarize_all_surfaces_stop_reason_in_stats(self):
        import asyncio

        s = TopDownSummarizer("/tmp/project", "space", codekb_service=_FakeGraphService({0: ["a", "b"]}))
        s._circuit_breaker = self._tripped_breaker()

        stats = asyncio.run(s.summarize_all())

        assert stats["stopped_early"] == "circuit_breaker_open"

    def test_summarize_all_completed_run_reports_none(self):
        import asyncio

        s = TopDownSummarizer("/tmp/project", "space", codekb_service=_FakeGraphService({0: ["a", "b"]}))
        # Everything already cached -> no LLM calls, run completes normally.
        s._indexed_cache = {"a", "b"}

        stats = asyncio.run(s.summarize_all())

        assert stats["stopped_early"] is None

    def test_targeted_run_surfaces_stop_reason_in_stats(self):
        import asyncio

        # Depth 2 > batch threshold -> exercises the individual (per-item)
        # loop, whose guardrail break previously left depth_cached unbound
        # (NameError) before stats could even be returned.
        s = TopDownSummarizer("/tmp/project", "space", codekb_service=_FakeGraphService({2: ["a"]}))
        s._circuit_breaker = self._tripped_breaker()

        stats = asyncio.run(s.summarize_all(targets={"a"}, prior_descriptions={}))

        assert stats["stopped_early"] == "circuit_breaker_open"

    def test_targeted_run_completed_reports_none(self):
        import asyncio

        s = TopDownSummarizer("/tmp/project", "space", codekb_service=_FakeGraphService({0: []}))

        stats = asyncio.run(s.summarize_all(targets=set(), prior_descriptions={}))

        assert stats["stopped_early"] is None

    def test_targeted_run_stops_outer_depth_loop(self):
        import asyncio

        # Targets across several depths, all above the batch threshold so they
        # hit the individual (per-item) loop. With the breaker already open the
        # outer depth loop must break immediately — it must NOT keep iterating
        # remaining depths and inflating depths_processed.
        s = TopDownSummarizer(
            "/tmp/project",
            "space",
            codekb_service=_FakeGraphService({4: ["a"], 3: ["b"], 2: ["c"]}),
        )
        s._circuit_breaker = self._tripped_breaker()

        stats = asyncio.run(s.summarize_all(targets={"a", "b", "c"}, prior_descriptions={}))

        assert stats["stopped_early"] == "circuit_breaker_open"
        assert stats["depths_processed"] == 0


class TestTopDownSummarizerLlmSettings:
    """The request body follows the resolved summarization settings."""

    def _summarizer(self, **settings):
        from src.core.circuit_breaker import CircuitBreaker
        from src.core.llm_settings import LlmSettings, Workload

        s = TopDownSummarizer("/tmp/project", "space", codekb_service=FakeRelevanceService({}))
        s._llm = LlmSettings(
            Workload.SUMMARIZATION,
            model_id="eu.anthropic.claude-sonnet-5-5",
            max_tokens=8000,
            **settings,
        )
        s._circuit_breaker = CircuitBreaker(1)
        return s

    def test_default_body_has_no_thinking_fields(self):
        s = TopDownSummarizer("/tmp/project", "space", codekb_service=FakeRelevanceService({}))
        client = CapturingBedrockClient(anthropic_body("ok"))
        s._bedrock_client = client

        s._call_bedrock("p", "i")

        assert set(client.sent_body()) == {"anthropic_version", "max_tokens", "messages"}

    def test_thinking_and_effort_are_sent(self):
        s = self._summarizer(thinking="between_tools", effort="medium")
        client = CapturingBedrockClient(anthropic_body("ok"))
        s._bedrock_client = client

        s._call_bedrock("p", "i")

        body = client.sent_body()
        assert body["thinking"] == {"type": "between_tools"}
        assert body["output_config"] == {"effort": "medium"}
        assert body["max_tokens"] == 8000

    def test_rejected_settings_stop_the_run_without_tripping_the_breaker(self):
        from test.bedrock_fakes import RejectingBedrockClient

        s = self._summarizer(thinking="between_tools")
        s._bedrock_client = RejectingBedrockClient(
            '"thinking.type.between_tools" is not supported for this model'
        )

        with pytest.raises(RuntimeError, match="CODEKB_SUMMARIZATION_THINKING"):
            s._call_bedrock("p", "i")

        assert not s._circuit_breaker.is_open
        assert s._should_stop() == "invalid_llm_settings"

    def test_other_client_errors_still_trip_the_breaker(self):
        from test.bedrock_fakes import RejectingBedrockClient

        s = self._summarizer()
        s._bedrock_client = RejectingBedrockClient("Rate exceeded", code="ThrottlingException")

        with pytest.raises(RuntimeError):
            s._call_bedrock("p", "i")

        assert s._circuit_breaker.is_open
        assert s._should_stop() == "circuit_breaker_open"

    def test_max_tokens_stop_without_text_names_the_setting(self):
        s = self._summarizer(thinking="adaptive")
        body = anthropic_body(thinking="")
        body["stop_reason"] = "max_tokens"
        s._bedrock_client = FakeBedrockClient(body)

        with pytest.raises(RuntimeError, match="CODEKB_SUMMARIZATION_MAX_TOKENS"):
            s._call_bedrock("p", "i")

    def test_thinking_tokens_are_recorded(self):
        s = self._summarizer(thinking="adaptive")
        s._bedrock_client = FakeBedrockClient(
            anthropic_body(
                "ok",
                usage={
                    "input_tokens": 100,
                    "output_tokens": 500,
                    "output_tokens_details": {"thinking_tokens": 300},
                },
            )
        )

        s._call_bedrock("p", "i")

        assert s.token_usage.totals() == {
            "input": 100,
            "output": 500,
            "total": 600,
            "thinking": 300,
        }

    def test_stopped_early_reports_invalid_settings(self):
        import asyncio

        from src.core.logging_utils import ConfigurationError

        s = TopDownSummarizer(
            "/tmp/project", "space", codekb_service=_FakeGraphService({0: ["a", "b"]})
        )
        s._settings_error = ConfigurationError("rejected")

        stats = asyncio.run(s.summarize_all())

        assert stats["stopped_early"] == "invalid_llm_settings"


class _ReconcileGraphService(FakeRelevanceService):
    """Call-graph + indexing surface to drive _process_batch end-to-end.

    Records every description that reaches index_component_description so a test
    can assert exactly which requested functions ended up stored.
    """

    def __init__(self):
        super().__init__({})
        self.indexed: dict[str, str] = {}

    def get_call_graph_with_source(self, space_id, component_id, depth):
        return {"id": component_id, "type": "function", "source": "def f(): pass", "calls": []}

    def index_component_description(self, space_id, component_key, description):
        self.indexed[component_key] = description
        return f"Stored description for '{component_key}'."


class _SequencedBedrockClient:
    """Returns a queued payload per invoke_model call; repeats the last once drained."""

    def __init__(self, payloads):
        import json

        self._payloads = [json.dumps(p).encode("utf-8") for p in payloads]
        self.calls = 0

    def invoke_model(self, **kwargs):
        from test.bedrock_fakes import FakeBody

        idx = min(self.calls, len(self._payloads) - 1)
        self.calls += 1
        return {"body": FakeBody(self._payloads[idx])}


class TestProcessBatchReconciliation:
    """_process_batch re-requests functions the model omits, bounded by
    summarization.batch_reconcile_max_rounds."""

    def _summarizer(self, svc):
        return TopDownSummarizer("/tmp/project", "space", codekb_service=svc)

    def test_rerequests_omitted_functions_until_complete(self):
        import json

        svc = _ReconcileGraphService()
        s = self._summarizer(svc)
        # Round 1 returns a and b but omits c; round 2 returns c.
        s._bedrock_client = _SequencedBedrockClient(
            [
                anthropic_body(json.dumps({"space::a": "A", "space::b": "B"})),
                anthropic_body(json.dumps({"space::c": "C"})),
            ]
        )

        fresh, _ = s._process_batch(["space::a", "space::b", "space::c"], depth=0)

        assert fresh == 3
        assert set(svc.indexed) == {"space::a", "space::b", "space::c"}
        assert s._bedrock_client.calls == 2

    def test_single_call_when_model_returns_everything(self):
        import json

        svc = _ReconcileGraphService()
        s = self._summarizer(svc)
        s._bedrock_client = _SequencedBedrockClient(
            [anthropic_body(json.dumps({"space::a": "A", "space::b": "B"}))]
        )

        fresh, _ = s._process_batch(["space::a", "space::b"], depth=0)

        assert fresh == 2
        # No re-request when nothing is missing after the first round.
        assert s._bedrock_client.calls == 1

    def test_bounded_by_max_rounds_and_skips_unreturned(self):
        import json

        svc = _ReconcileGraphService()
        s = self._summarizer(svc)
        s._batch_reconcile_max_rounds = 2
        # The model never returns b, no matter how many times it is re-requested.
        s._bedrock_client = _SequencedBedrockClient(
            [anthropic_body(json.dumps({"space::a": "A"}))]
        )

        fresh, _ = s._process_batch(["space::a", "space::b"], depth=0)

        assert fresh == 1
        assert "space::a" in svc.indexed
        assert "space::b" not in svc.indexed
        # Tried the first call plus one bounded re-request, then gave up.
        assert s._bedrock_client.calls == 2


class TestProcessIndividualReconciliation:
    """_process_component_individual re-requests SOURCE functions the model
    omits from a call-graph response, bounded by batch_reconcile_max_rounds."""

    def _summarizer(self, svc):
        return TopDownSummarizer("/tmp/project", "space", codekb_service=svc)

    def test_rerequests_when_target_omitted(self):
        import json

        svc = _ReconcileGraphService()
        s = self._summarizer(svc)
        # First (full-graph) call returns nothing; the reconcile re-request returns it.
        s._bedrock_client = _SequencedBedrockClient(
            [
                anthropic_body(json.dumps({})),
                anthropic_body(json.dumps({"space::a": "A"})),
            ]
        )

        fresh = s._process_component_individual("space::a")

        assert fresh == 1
        assert "space::a" in svc.indexed
        assert s._bedrock_client.calls == 2

    def test_single_call_when_target_returned(self):
        import json

        svc = _ReconcileGraphService()
        s = self._summarizer(svc)
        s._bedrock_client = _SequencedBedrockClient(
            [anthropic_body(json.dumps({"space::a": "A"}))]
        )

        fresh = s._process_component_individual("space::a")

        assert fresh == 1
        # Nothing missing after the first call => no re-request.
        assert s._bedrock_client.calls == 1

    def test_no_reconcile_when_disabled(self):
        import json

        svc = _ReconcileGraphService()
        s = self._summarizer(svc)
        s._batch_reconcile_max_rounds = 1  # disables re-requesting
        s._bedrock_client = _SequencedBedrockClient([anthropic_body(json.dumps({}))])

        fresh = s._process_component_individual("space::a")

        assert fresh == 0
        assert "space::a" not in svc.indexed
        assert s._bedrock_client.calls == 1


class _GraphWithNonCallableService(FakeRelevanceService):
    """Call graph whose root (function) calls non-callable nodes.

    Call graphs include callees that are not functions/methods — field
    references (variable/constant) and referenced types (class/constructor).
    They have no source and index_component_description rejects them, so they
    must never become reconcile targets.
    """

    def __init__(self):
        super().__init__({})
        self.indexed: dict[str, str] = {}

    def get_call_graph_with_source(self, space_id, component_id, depth):
        return {
            "id": component_id,
            "type": "function",
            "source": "def f(): return self.field",
            "calls": [
                {"id": "space::Obj.field", "type": "variable", "source": "", "calls": []},
                {"id": "space::Obj.LIMIT", "type": "constant", "source": "", "calls": []},
                {"id": "space::Other.Other", "type": "class", "source": "", "calls": []},
            ],
        }

    def index_component_description(self, space_id, component_key, description):
        self.indexed[component_key] = description
        return f"Stored description for '{component_key}'."


class TestIndividualExcludesNonCallableNodes:
    def test_non_callable_callees_are_not_reconcile_targets(self):
        import json

        svc = _GraphWithNonCallableService()
        s = TopDownSummarizer("/tmp/project", "space", codekb_service=svc)
        # First call describes only the function; the variable/constant/class
        # callees are never expected.
        s._bedrock_client = _SequencedBedrockClient(
            [anthropic_body(json.dumps({"space::a": "A"}))]
        )

        fresh = s._process_component_individual("space::a")

        assert fresh == 1
        assert svc.indexed == {"space::a": "A"}
        # Non-callable callees excluded from expected => nothing missing => no
        # re-request (the bug that spammed warnings re-requesting fields).
        assert s._bedrock_client.calls == 1


class TestRecoverTruncatedJson:
    """_recover_truncated_json salvages well-formed pairs even when one pair in
    the middle is broken by an unescaped quote (not just end-truncation)."""

    def test_mid_response_unescaped_quote_keeps_other_pairs(self):
        raw = (
            '{"space::a": "clean one", '
            '"space::b": "has a " quote inside", '
            '"space::c": "also clean"}'
        )
        out = TopDownSummarizer._recover_truncated_json(raw)
        assert out["space::a"] == "clean one"
        assert out["space::c"] == "also clean"
        # The broken pair contributes its text up to the stray quote.
        assert out["space::b"] == "has a "

    def test_end_truncation_recovers_complete_pairs(self):
        # Second value cut off (no closing quote) — only the first pair is whole.
        raw = '{"space::a": "one", "space::b": "two and then cut'
        out = TopDownSummarizer._recover_truncated_json(raw)
        assert out == {"space::a": "one"}

    def test_clean_json_recovers_all(self):
        raw = '{"space::a": "x", "space::b": "y"}'
        out = TopDownSummarizer._recover_truncated_json(raw)
        assert out == {"space::a": "x", "space::b": "y"}

    def test_no_pairs_returns_none(self):
        assert TopDownSummarizer._recover_truncated_json("not json at all") is None
        assert TopDownSummarizer._recover_truncated_json("") is None


class _IndexingGraphService(_FakeGraphService):
    """_FakeGraphService that also renders call graphs and accepts index writes."""

    def __init__(self, by_depth: dict[int, list[str]]):
        super().__init__(by_depth)
        self.indexed: list[str] = []

    def get_call_graph_with_source(self, space_id: str, component_id: str, depth: int):
        return {"id": component_id, "type": "function", "source": "def f(): pass", "calls": []}

    def index_component_description(self, space_id: str, component_id: str, description: str):
        self.indexed.append(component_id)
        return f"Stored description for '{component_id}'."


class TestSummarizeTargetedSkipsDescribedTargets:
    """A target described inside a higher target's prompt is not prompted again (as at creation)."""

    def _run(self, by_depth, targets, llm_replies):
        import asyncio

        service = _IndexingGraphService(by_depth)
        s = TopDownSummarizer("/tmp/project", "space", codekb_service=service)
        prompts: list[str] = []
        replies = iter(llm_replies)

        def fake_generate(text):
            prompts.append(text)
            return next(replies), _OUTCOME_OK

        s._generate_descriptions = fake_generate
        progress: list[tuple[int, int]] = []
        s.on_progress = lambda done, total: progress.append((done, total))
        stats = asyncio.run(s.summarize_all(targets=targets, prior_descriptions={}))
        return s, service, prompts, progress, stats

    def test_individual_path_skips_target_described_by_a_higher_target(self):
        # a (depth 3) calls b (depth 2); a's prompt describes both.
        s, service, prompts, progress, stats = self._run(
            {3: ["a"], 2: ["b"]}, {"a", "b"}, [{"a": "A desc", "b": "B desc"}]
        )

        assert len(prompts) == 1
        assert service.indexed == ["a", "b"]
        assert progress[-1] == (2, 2)
        assert stats["total_cached"] == 1

    def test_batched_path_skips_target_described_by_a_higher_target(self):
        # a (depth 2) calls c (depth 0, batched); a's prompt describes both.
        s, service, prompts, progress, stats = self._run(
            {2: ["a"], 0: ["c"]}, {"a", "c"}, [{"a": "A desc", "c": "C desc"}]
        )

        assert len(prompts) == 1
        assert service.indexed == ["a", "c"]
        assert progress[-1] == (2, 2)


class TestSummarizeTargetedRelevanceFilter:
    """Refresh skips low business relevance targets when the space opted in (as at creation)."""

    def _run(self, omit: bool):
        import asyncio

        service = _IndexingGraphService({3: ["low"], 2: ["high"]})
        service.tiers = {"low": "low", "high": "high"}
        s = TopDownSummarizer(
            "/tmp/project", "space", codekb_service=service, omit_low_business_relevance=omit
        )
        prompts: list[str] = []

        def fake_generate(text):
            prompts.append(text)
            d = {"high": "H desc"} if "high" in text else {"low": "L desc"}
            return d, _OUTCOME_OK

        s._generate_descriptions = fake_generate
        progress: list[tuple[int, int]] = []
        s.on_progress = lambda done, total: progress.append((done, total))
        stats = asyncio.run(s.summarize_all(targets={"low", "high"}, prior_descriptions={}))
        return service, prompts, progress, stats

    def test_low_relevance_target_is_skipped_when_omitted(self):
        service, prompts, progress, stats = self._run(omit=True)

        assert service.indexed == ["high"]
        assert len(prompts) == 1
        assert progress[-1] == (1, 1)
        assert stats["skipped_low_business_relevance"] == 1

    def test_every_target_is_summarized_when_not_omitted(self):
        service, prompts, progress, stats = self._run(omit=False)

        assert sorted(service.indexed) == ["high", "low"]
        assert progress[-1] == (2, 2)
        assert stats["skipped_low_business_relevance"] == 0


def _describe_defs(text: str) -> dict[str, str]:
    import re

    return {cid: f"{cid} desc" for cid in re.findall(r"def (\w+)\(", text)}


class TestSummarizeTargetedConcurrency:
    """Refresh summarizes targets at one depth in parallel and survives one failure (as at creation)."""

    def _summarizer(self, by_depth, fake_generate):
        service = _IndexingGraphService(by_depth)
        service.get_call_graph_with_source = lambda sid, cid, depth: {
            "id": cid, "type": "function", "source": f"def {cid}(): pass", "calls": []
        }
        s = TopDownSummarizer("/tmp/project", "space", codekb_service=service)
        s._concurrency = 2
        s._batch_depth_threshold, s._batch_size = 1, 1
        s._generate_descriptions = fake_generate
        return s, service

    @pytest.mark.parametrize("depth", [3, 0], ids=["individual", "batched"])
    def test_targets_at_one_depth_run_in_parallel(self, depth):
        import asyncio
        import threading

        # Both calls must be in flight at once to pass the barrier.
        barrier = threading.Barrier(2, timeout=5)

        def fake_generate(text):
            barrier.wait()
            return _describe_defs(text), _OUTCOME_OK

        s, service = self._summarizer({depth: ["a", "b"]}, fake_generate)
        asyncio.run(s.summarize_all(targets={"a", "b"}, prior_descriptions={}))

        assert sorted(service.indexed) == ["a", "b"]

    def test_one_failing_target_does_not_stop_the_others(self):
        import asyncio

        def fake_generate(text):
            if "def a(" in text:
                raise RuntimeError("Bedrock throttled")
            return _describe_defs(text), _OUTCOME_OK

        s, service = self._summarizer({3: ["a", "b"]}, fake_generate)
        stats = asyncio.run(s.summarize_all(targets={"a", "b"}, prior_descriptions={}))

        assert service.indexed == ["b"]
        assert stats["total_analyzed"] == 1

    def test_open_breaker_stops_before_the_next_depth(self):
        import asyncio

        from src.core.circuit_breaker import CircuitBreaker

        def fake_generate(text):
            if "def a(" in text:
                s._circuit_breaker.record_failure()
                raise RuntimeError("Bedrock down")
            return _describe_defs(text), _OUTCOME_OK

        s, service = self._summarizer({3: ["a", "b"], 2: ["c"]}, fake_generate)
        s._circuit_breaker = CircuitBreaker(1)
        stats = asyncio.run(s.summarize_all(targets={"a", "b", "c"}, prior_descriptions={}))

        assert "c" not in service.indexed
        assert stats["stopped_early"] == "circuit_breaker_open"


class TestCoverageGateAndAdaptiveReconcile:
    """Strict coverage gate + adaptive-shrink reconcile (customer-reported fix)."""

    def _run(self, by_depth, targets, fake_generate, **attrs):
        import asyncio

        service = _IndexingGraphService(by_depth)
        s = TopDownSummarizer("/tmp/project", "space", codekb_service=service)
        s._batch_depth_threshold, s._batch_size = 5, 100  # force the batched path
        for k, v in attrs.items():
            setattr(s, k, v)
        s._generate_descriptions = fake_generate
        stats = asyncio.run(s.summarize_all(targets=set(targets), prior_descriptions={}))
        return service, stats

    def _present(self, text, targets):
        return [t for t in targets if f'"id": "{t}"' in text]

    def test_incomplete_coverage_fails_the_run_and_reports_coverage(self):
        # The model never returns a description for "b" → strict gate fails.
        def fake(text):
            described = {t: f"{t} desc" for t in self._present(text, ["a", "b"]) if t != "b"}
            return described, _OUTCOME_OK

        service, stats = self._run({0: ["a", "b"]}, ["a", "b"], fake)

        assert service.indexed == ["a"]
        assert stats["coverage"] == "1/2"
        assert stats["stopped_early"] == "incomplete_coverage"

    def test_full_coverage_completes_and_reports_n_over_n(self):
        def fake(text):
            return {t: f"{t} desc" for t in self._present(text, ["a", "b"])}, _OUTCOME_OK

        service, stats = self._run({0: ["a", "b"]}, ["a", "b"], fake)

        assert sorted(service.indexed) == ["a", "b"]
        assert stats["coverage"] == "2/2"
        assert stats["stopped_early"] is None

    def test_truncated_batch_splits_until_each_fits_one_at_a_time(self):
        # A call with >1 function "truncates" (empty + TRUNCATED); a single
        # function succeeds. Adaptive split must still cover all four.
        ids = ["a", "b", "c", "d"]

        def fake(text):
            present = self._present(text, ids)
            if len(present) > 1:
                return {}, _OUTCOME_TRUNCATED          # output too big → cut
            return {present[0]: f"{present[0]} desc"}, _OUTCOME_OK

        service, stats = self._run({0: ids}, ids, fake)

        assert sorted(service.indexed) == ids
        assert stats["coverage"] == "4/4"
        assert stats["stopped_early"] is None

    def test_no_progress_round_does_not_resend_forever(self):
        # The model returns valid JSON but never any of the requested ids. The
        # loop must stop (no infinite identical re-send) and fail coverage.
        calls = {"n": 0}

        def fake(text):
            calls["n"] += 1
            return {}, _OUTCOME_OK

        service, stats = self._run({0: ["a", "b"]}, ["a", "b"], fake)

        assert service.indexed == []
        assert stats["stopped_early"] == "incomplete_coverage"
        assert calls["n"] <= 6  # bounded, not spinning
