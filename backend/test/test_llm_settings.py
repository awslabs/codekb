"""Tests for per-workload LLM settings: resolution, request fields, errors.

Each test loads a fresh ConfigurationManager with the env vars it sets and
installs it as the global manager, so ``resolve`` reads exactly that config.
"""

import pytest
from botocore.exceptions import ClientError

import src.core.config as config_module
from src.core.config import ConfigurationManager
from src.core.llm_settings import (
    MODEL_DEFAULT,
    LlmSettings,
    Workload,
    anthropic_request_fields,
    max_tokens_error,
    resolve,
    settings_error_from_bedrock,
)
from src.core.logging_utils import ConfigurationError

OPUS_46 = "us.anthropic.claude-opus-4-6-v1"
SONNET_55 = "eu.anthropic.claude-sonnet-5-5"
HAIKU_55 = "eu.anthropic.claude-haiku-5-5"

_LLM_ENV_PREFIXES = (
    "CODEKB_LLM_",
    "CODEKB_SUMMARIZATION_",
    "CODEKB_BUSINESS_CONTEXT_",
    "CODEKB_OVERVIEW_",
    "CODEKB_CHAT_",
)


@pytest.fixture
def load(monkeypatch):
    """Return a function that loads config from the given env vars."""
    import os

    for name in list(os.environ):
        if name.startswith(_LLM_ENV_PREFIXES) or name in (
            "CODEKB_BEDROCK_MODEL_ID",
            "CODEKB_BEDROCK_MAX_TOKENS",
            "CODEKB_BEDROCK_REASONING",
            "CODEKB_BEDROCK_REASONING_BUDGET_TOKENS",
        ):
            monkeypatch.delenv(name, raising=False)

    def _load(**env: str) -> None:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        mgr = ConfigurationManager()
        mgr.load_configuration()
        monkeypatch.setattr(config_module, "_config_manager", mgr)

    return _load


def _validation_error(message: str, code: str = "ValidationException") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "InvokeModel")


class TestDefaults:
    def test_nothing_set_keeps_todays_request(self, load):
        load()
        s = resolve(Workload.SUMMARIZATION)

        assert s.model_id == OPUS_46
        assert s.max_tokens == 65536
        assert s.thinking == MODEL_DEFAULT
        assert s.effort is None
        # Byte-identical to the body sent before these settings existed.
        assert anthropic_request_fields(s) == {"max_tokens": 65536}

    def test_business_context_keeps_its_own_max_tokens(self, load):
        # Today's 2048 is the workload's own YAML value, so the shared
        # CODEKB_BEDROCK_MAX_TOKENS does not raise it.
        load(CODEKB_BEDROCK_MAX_TOKENS="4096")

        assert resolve(Workload.BUSINESS_CONTEXT).max_tokens == 2048
        assert resolve(Workload.SUMMARIZATION).max_tokens == 4096


class TestInheritance:
    def test_shared_thinking_applies_to_every_workload(self, load):
        load(CODEKB_BEDROCK_MODEL_ID=SONNET_55, CODEKB_LLM_THINKING="between_tools")

        for workload in Workload:
            s = resolve(workload)
            assert s.model_id == SONNET_55
            assert s.thinking == "between_tools"
            assert s.sources["thinking"] == "llm.defaults.thinking"

    def test_workload_value_wins_over_shared(self, load):
        load(CODEKB_LLM_THINKING="between_tools", CODEKB_SUMMARIZATION_THINKING="adaptive")

        assert resolve(Workload.SUMMARIZATION).thinking == "adaptive"
        assert resolve(Workload.BUSINESS_CONTEXT).thinking == "between_tools"

    def test_model_default_overrides_a_shared_value(self, load):
        load(CODEKB_LLM_THINKING="between_tools", CODEKB_SUMMARIZATION_THINKING=MODEL_DEFAULT)

        s = resolve(Workload.SUMMARIZATION)
        assert s.thinking == MODEL_DEFAULT
        assert "thinking" not in anthropic_request_fields(s)

    def test_own_model_does_not_inherit_model_bound_fields(self, load):
        # between_tools is Sonnet 5.5 only; Haiku 5.5 rejects it. A workload
        # moved to Haiku must not pick it up from the shared default.
        load(
            CODEKB_BEDROCK_MODEL_ID=SONNET_55,
            CODEKB_LLM_THINKING="between_tools",
            CODEKB_LLM_EFFORT="medium",
            CODEKB_BEDROCK_MAX_TOKENS="32000",
            CODEKB_SUMMARIZATION_MODEL_ID=HAIKU_55,
        )

        s = resolve(Workload.SUMMARIZATION)
        assert s.model_id == HAIKU_55
        assert s.thinking == MODEL_DEFAULT
        assert s.effort is None
        # max_tokens isn't tied to a model, so it still inherits.
        assert s.max_tokens == 32000
        # The other workload is unaffected.
        assert resolve(Workload.BUSINESS_CONTEXT).thinking == "between_tools"

    def test_own_model_keeps_its_own_model_bound_values(self, load):
        load(
            CODEKB_LLM_THINKING="between_tools",
            CODEKB_SUMMARIZATION_MODEL_ID=HAIKU_55,
            CODEKB_SUMMARIZATION_THINKING="disabled",
        )

        assert resolve(Workload.SUMMARIZATION).thinking == "disabled"

    def test_empty_env_var_inherits(self, load):
        load(CODEKB_SUMMARIZATION_MODEL_ID="", CODEKB_SUMMARIZATION_THINKING="  ")

        s = resolve(Workload.SUMMARIZATION)
        assert s.model_id == OPUS_46
        assert s.thinking == MODEL_DEFAULT

    def test_values_are_passed_through_unchanged(self, load):
        # No allow-list: a value a future model introduces needs no release.
        load(CODEKB_LLM_THINKING="some_future_type", CODEKB_LLM_EFFORT="ultra")

        fields = anthropic_request_fields(resolve(Workload.SUMMARIZATION))
        assert fields["thinking"] == {"type": "some_future_type"}
        assert fields["output_config"] == {"effort": "ultra"}


class TestValidation:
    def test_non_positive_max_tokens_is_rejected(self, load):
        load(CODEKB_SUMMARIZATION_MAX_TOKENS="0")

        with pytest.raises(ConfigurationError, match="CODEKB_SUMMARIZATION_MAX_TOKENS"):
            resolve(Workload.SUMMARIZATION)

    def test_enabled_without_budget_is_rejected(self, load):
        load(CODEKB_SUMMARIZATION_THINKING="enabled")

        with pytest.raises(ConfigurationError, match="budget_tokens"):
            resolve(Workload.SUMMARIZATION)


class TestRequestFields:
    def _settings(self, **kwargs) -> LlmSettings:
        return LlmSettings(Workload.SUMMARIZATION, model_id=SONNET_55, max_tokens=8000, **kwargs)

    def test_between_tools(self):
        fields = anthropic_request_fields(self._settings(thinking="between_tools"))
        assert fields == {"max_tokens": 8000, "thinking": {"type": "between_tools"}}

    def test_adaptive_with_effort(self):
        fields = anthropic_request_fields(self._settings(thinking="adaptive", effort="low"))
        # Only the chat shows reasoning, so only the chat asks for its text.
        assert fields == {
            "max_tokens": 8000,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "low"},
        }

    def test_chat_adaptive_asks_for_summarized_reasoning(self):
        s = LlmSettings(Workload.CHAT, model_id=SONNET_55, max_tokens=8000, thinking="adaptive")
        assert anthropic_request_fields(s)["thinking"] == {
            "type": "adaptive",
            "display": "summarized",
        }

    @pytest.mark.parametrize("thinking", ["between_tools", "disabled", "enabled"])
    def test_chat_sends_no_display_for_other_types(self, thinking):
        s = LlmSettings(
            Workload.CHAT,
            model_id=SONNET_55,
            max_tokens=8000,
            thinking=thinking,
            budget_tokens=4096 if thinking == "enabled" else None,
        )
        assert "display" not in anthropic_request_fields(s)["thinking"]

    def test_effort_without_thinking(self):
        fields = anthropic_request_fields(self._settings(effort="medium"))
        assert fields == {"max_tokens": 8000, "output_config": {"effort": "medium"}}

    def test_enabled_raises_max_tokens_above_budget(self):
        fields = anthropic_request_fields(self._settings(thinking="enabled", budget_tokens=10000))
        assert fields["thinking"] == {"type": "enabled", "budget_tokens": 10000}
        assert fields["max_tokens"] == 10000 + 1024


class TestBedrockErrors:
    def _settings(self, load, **env) -> LlmSettings:
        load(**env)
        return resolve(Workload.SUMMARIZATION)

    def test_rejected_thinking_names_the_shared_env_var(self, load):
        s = self._settings(load, CODEKB_LLM_THINKING="between_tools")
        err = settings_error_from_bedrock(
            _validation_error('"thinking.type.between_tools" is not supported for this model'), s
        )

        assert isinstance(err, ConfigurationError)
        assert "CODEKB_LLM_THINKING" in str(err)
        assert "between_tools" in str(err)

    def test_rejected_thinking_names_the_workload_env_var(self, load):
        s = self._settings(load, CODEKB_SUMMARIZATION_THINKING="disabled")
        err = settings_error_from_bedrock(
            _validation_error("thinking.type: disabled is not supported"), s
        )

        assert "CODEKB_SUMMARIZATION_THINKING" in str(err)

    def test_rejected_effort_is_a_settings_error(self, load):
        s = self._settings(load, CODEKB_LLM_EFFORT="xhigh")
        err = settings_error_from_bedrock(
            _validation_error("output_config.effort 'xhigh' is not supported"), s
        )

        assert isinstance(err, ConfigurationError)

    @pytest.mark.parametrize(
        "exc",
        [
            _validation_error("Input is too long for requested model."),
            _validation_error("Rate exceeded", code="ThrottlingException"),
            RuntimeError("thinking connection reset"),
        ],
    )
    def test_other_failures_are_not_settings_errors(self, load, exc):
        s = self._settings(load)
        assert settings_error_from_bedrock(exc, s) is None

    def test_max_tokens_error_names_the_setting_in_effect(self, load):
        load()
        s = resolve(Workload.BUSINESS_CONTEXT)

        assert "CODEKB_BUSINESS_CONTEXT_MAX_TOKENS" in str(max_tokens_error(s))


class TestEnvMap:
    def test_no_thinking_display_setting(self):
        # Display is decided by CodeKB (summarized for an adaptive chat).
        assert not any("THINKING_DISPLAY" in name for name in ConfigurationManager._ENV_OVERRIDES)

    def test_every_workload_field_has_an_env_var(self):
        overrides = ConfigurationManager._ENV_OVERRIDES
        for workload in Workload:
            for name in ("model_id", "max_tokens", "thinking", "effort"):
                env_var = workload.env_var(name)
                assert overrides[env_var] == f"llm.workloads.{workload.value}.{name}"
            assert workload.env_var("max_tokens") in ConfigurationManager._INT_ENV_VARS

    def test_shared_defaults_keep_their_names(self):
        overrides = ConfigurationManager._ENV_OVERRIDES
        assert overrides["CODEKB_BEDROCK_MODEL_ID"] == "llm.defaults.model_id"
        assert overrides["CODEKB_BEDROCK_MAX_TOKENS"] == "llm.defaults.max_tokens"
        assert overrides["CODEKB_LLM_THINKING"] == "llm.defaults.thinking"
        assert overrides["CODEKB_LLM_EFFORT"] == "llm.defaults.effort"

    def test_describe_shows_value_and_source(self, load):
        load(CODEKB_LLM_THINKING="between_tools")
        line = resolve(Workload.SUMMARIZATION).describe()

        assert "thinking=between_tools [llm.defaults.thinking]" in line
        assert f"model_id={OPUS_46} [llm.defaults.model_id]" in line


class TestStartupValidation:
    def test_defaults_are_valid(self, load):
        from src.core.llm_startup_check import validate_llm_settings

        load()
        resolved = validate_llm_settings()

        assert [s.workload for s in resolved] == list(Workload)

    def test_non_integer_workload_max_tokens_fails_instead_of_inheriting(self, load):
        from src.core.llm_startup_check import validate_llm_settings

        load(CODEKB_SUMMARIZATION_MAX_TOKENS="abc")

        with pytest.raises(ConfigurationError, match="CODEKB_SUMMARIZATION_MAX_TOKENS"):
            validate_llm_settings()

    def test_missing_model_fails(self, load):
        from src.core.llm_startup_check import validate_llm_settings

        load(CODEKB_BEDROCK_MODEL_ID="")

        with pytest.raises(ConfigurationError, match="No model configured"):
            validate_llm_settings()


class _RecordingClient:
    """Bedrock client that answers OK and records every request body."""

    def __init__(self):
        self.bodies: list[dict] = []
        self.model_ids: list[str] = []

    def invoke_model(self, **kwargs):
        import json

        from test.bedrock_fakes import FakeBody, anthropic_bytes

        self.bodies.append(json.loads(kwargs["body"]))
        self.model_ids.append(kwargs["modelId"])
        return {"body": FakeBody(anthropic_bytes("OK"))}


class TestStartupProbe:
    def _probe(self, client):
        from src.core.llm_startup_check import probe_llm_settings, validate_llm_settings

        probe_llm_settings(validate_llm_settings(), client_factory=lambda: client)

    def test_disabled_makes_no_call(self, load):
        load(CODEKB_LLM_STARTUP_CHECK="false")
        client = _RecordingClient()

        self._probe(client)

        assert client.bodies == []

    def test_one_call_per_distinct_combination(self, load):
        # Summarization and overview share 65536; business vocabulary (2048) and
        # chat (16000) have their own max_tokens.
        load(CODEKB_LLM_STARTUP_CHECK="true")
        client = _RecordingClient()

        self._probe(client)

        assert sorted(b["max_tokens"] for b in client.bodies) == [2048, 16000, 65536]

    def test_identical_settings_share_one_call(self, load):
        load(
            CODEKB_LLM_STARTUP_CHECK="true",
            CODEKB_BUSINESS_CONTEXT_MAX_TOKENS="65536",
            CODEKB_CHAT_MAX_TOKENS="65536",
        )
        client = _RecordingClient()

        self._probe(client)

        assert len(client.bodies) == 1

    def test_probe_sends_the_configured_settings(self, load):
        load(
            CODEKB_LLM_STARTUP_CHECK="true",
            CODEKB_BEDROCK_MODEL_ID=SONNET_55,
            CODEKB_LLM_THINKING="between_tools",
        )
        client = _RecordingClient()

        self._probe(client)

        assert all(b["thinking"] == {"type": "between_tools"} for b in client.bodies)

    def test_rejected_settings_fail_startup(self, load):
        from test.bedrock_fakes import RejectingBedrockClient

        load(
            CODEKB_LLM_STARTUP_CHECK="true",
            CODEKB_LLM_THINKING="between_tools",
            CODEKB_BEDROCK_MAX_TOKENS="2048",
        )
        client = RejectingBedrockClient(
            '"thinking.type.between_tools" is not supported for this model'
        )

        with pytest.raises(ConfigurationError) as excinfo:
            self._probe(client)

        message = str(excinfo.value)
        assert "CODEKB_LLM_THINKING" in message
        # Every workload sharing the combination is named.
        assert "summarization, business_context, overview" in message

    @pytest.mark.parametrize(
        "code,message",
        [
            ("ThrottlingException", "Rate exceeded"),
            ("AccessDeniedException", "not authorized to perform bedrock:InvokeModel"),
            ("ValidationException", "Input is too long for requested model."),
        ],
    )
    def test_other_failures_only_warn(self, load, code, message):
        from test.bedrock_fakes import RejectingBedrockClient

        load(CODEKB_LLM_STARTUP_CHECK="true")
        client = RejectingBedrockClient(message, code=code)

        self._probe(client)  # does not raise

        assert client.calls == 3

    def test_client_creation_failure_only_warns(self, load):
        from src.core.llm_startup_check import probe_llm_settings, validate_llm_settings

        load(CODEKB_LLM_STARTUP_CHECK="true")

        def _no_credentials():
            raise RuntimeError("Unable to locate credentials")

        probe_llm_settings(validate_llm_settings(), client_factory=_no_credentials)


class TestChatSettings:
    def test_chat_has_its_own_max_tokens(self, load):
        load(CODEKB_BEDROCK_MAX_TOKENS="32000")

        assert resolve(Workload.CHAT).max_tokens == 16000
        assert resolve(Workload.OVERVIEW).max_tokens == 32000

    def test_legacy_reasoning_maps_to_enabled_thinking(self, load):
        load(CODEKB_BEDROCK_REASONING="true", CODEKB_BEDROCK_REASONING_BUDGET_TOKENS="8000")

        s = resolve(Workload.CHAT)
        assert s.thinking == "enabled"
        assert s.budget_tokens == 8000
        assert s.setting_name("thinking") == "CODEKB_BEDROCK_REASONING"
        assert anthropic_request_fields(s)["thinking"] == {
            "type": "enabled",
            "budget_tokens": 8000,
        }
        # Only the chat ever read the legacy switch.
        assert resolve(Workload.SUMMARIZATION).thinking == MODEL_DEFAULT

    def test_legacy_reasoning_beats_the_shared_default(self, load):
        load(CODEKB_BEDROCK_REASONING="true", CODEKB_LLM_THINKING="between_tools")

        assert resolve(Workload.CHAT).thinking == "enabled"

    def test_chat_thinking_beats_legacy_reasoning(self, load):
        from src.core.llm_settings import legacy_setting_warnings

        load(CODEKB_BEDROCK_REASONING="true", CODEKB_CHAT_THINKING="adaptive")

        s = resolve(Workload.CHAT)
        assert s.thinking == "adaptive"
        assert s.budget_tokens is None
        assert "ignored" in legacy_setting_warnings()[0]

    def test_legacy_reasoning_warns_as_deprecated(self, load):
        from src.core.llm_settings import legacy_setting_warnings

        load(CODEKB_BEDROCK_REASONING="true")

        assert "deprecated" in legacy_setting_warnings()[0]

    def test_no_warning_when_legacy_reasoning_is_off(self, load):
        from src.core.llm_settings import legacy_setting_warnings

        load()

        assert legacy_setting_warnings() == []


class TestStrandsKwargs:
    def test_default_sends_no_additional_fields(self, load):
        from src.core.llm_settings import strands_model_kwargs

        load()
        kwargs = strands_model_kwargs(resolve(Workload.CHAT))

        assert kwargs["model_id"] == OPUS_46
        assert kwargs["region_name"] == "us-east-1"
        assert kwargs["max_tokens"] == 16000
        assert kwargs["boto_client_config"].read_timeout == 1800
        assert "additional_request_fields" not in kwargs

    def test_thinking_goes_into_additional_request_fields(self, load):
        from src.core.llm_settings import strands_model_kwargs

        load(CODEKB_CHAT_THINKING="adaptive")
        kwargs = strands_model_kwargs(resolve(Workload.CHAT))

        assert kwargs["additional_request_fields"] == {
            "thinking": {"type": "adaptive", "display": "summarized"}
        }

    def test_chat_agent_uses_chat_settings(self, load, monkeypatch):
        import src.agent.codekb_agent as agent_module

        load(CODEKB_CHAT_MODEL_ID=SONNET_55, CODEKB_CHAT_THINKING="between_tools")
        captured: dict = {}

        class _FakeModel:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(agent_module, "BedrockModel", _FakeModel)
        monkeypatch.setattr(agent_module, "Agent", lambda **kwargs: kwargs)

        agent_module.create_agent(["space"])

        assert captured["model_id"] == SONNET_55
        assert captured["max_tokens"] == 16000
        assert captured["additional_request_fields"] == {"thinking": {"type": "between_tools"}}

    def test_overview_uses_overview_settings(self, load, monkeypatch):
        import src.vector.overview_common as oc

        load(CODEKB_OVERVIEW_MODEL_ID=SONNET_55, CODEKB_OVERVIEW_EFFORT="low")
        captured: dict = {}

        class _FakeModel:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(oc, "BedrockModel", _FakeModel)

        oc.OverviewGeneratorBase(log_prefix="[test]")._create_model()

        assert captured["model_id"] == SONNET_55
        assert captured["additional_request_fields"] == {"output_config": {"effort": "low"}}


class TestStrandsErrors:
    def test_error_wrapped_by_strands_is_recognized(self, load):
        from strands.types.exceptions import EventLoopException

        load(CODEKB_CHAT_THINKING="between_tools")
        original = _validation_error('"thinking.type.between_tools" is not supported')
        try:
            raise EventLoopException(original) from original
        except EventLoopException as wrapped:
            err = settings_error_from_bedrock(wrapped, resolve(Workload.CHAT))

        assert "CODEKB_CHAT_THINKING" in str(err)

    def test_stream_reports_a_clear_error(self, load, monkeypatch):
        import asyncio

        from strands.types.exceptions import EventLoopException

        import src.agent.codekb_agent as agent_module

        load(CODEKB_CHAT_THINKING="between_tools")
        original = _validation_error('"thinking.type.between_tools" is not supported')

        class _FailingAgent:
            async def stream_async(self, message):
                raise EventLoopException(original) from original
                yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr(agent_module, "create_agent", lambda *a, **k: _FailingAgent())

        async def _collect():
            return [e async for e in agent_module.stream_agent_query(["space"], "hi")]

        events = asyncio.run(_collect())

        assert len(events) == 1
        assert "event: error" in events[0]
        assert "Bedrock rejected the chat model settings" in events[0]
        # Operator details stay in the log.
        assert "CODEKB_CHAT_THINKING" not in events[0]


class TestProbeSkipsDisabledWorkloads:
    def test_overview_is_not_probed_when_disabled(self, load):
        from src.core.llm_startup_check import probe_llm_settings, validate_llm_settings

        load(
            CODEKB_LLM_STARTUP_CHECK="true",
            CODEKB_OVERVIEW_GENERATION_ENABLED="false",
            CODEKB_OVERVIEW_MODEL_ID=HAIKU_55,
        )
        client = _RecordingClient()

        probe_llm_settings(validate_llm_settings(), client_factory=lambda: client)

        # Summarization, business_context and chat; the Haiku overview would be a fourth.
        assert len(client.bodies) == 3
        assert HAIKU_55 not in client.model_ids


class TestSettingsPageModels:
    """GET /api/config's ``models`` section, which feeds the Settings page."""

    def test_lists_every_workload_and_the_embeddings_model(self, load):
        from src.api.routes.config import _models

        load(CODEKB_LLM_THINKING="between_tools", CODEKB_CHAT_MODEL_ID=SONNET_55)
        models = _models()

        by_workload = {m["workload"]: m for m in models["workloads"]}
        assert list(by_workload) == [w.value for w in Workload]
        assert by_workload["summarization"]["model_id"] == OPUS_46
        assert by_workload["summarization"]["thinking"] == "between_tools"
        # The chat has its own model, so it doesn't inherit the shared thinking.
        assert by_workload["chat"]["model_id"] == SONNET_55
        assert by_workload["chat"]["thinking"] is None
        assert by_workload["chat"]["effort"] is None
        assert models["embedding_model_id"] == "amazon.titan-embed-text-v2:0"

    def test_only_the_agreed_fields_are_returned(self, load):
        from src.api.routes.config import _models

        load()
        models = _models()

        # No region, guardrail, token caps or budgets.
        assert set(models["workloads"][0]) == {
            "workload",
            "model_id",
            "thinking",
            "effort",
            "overrides",
            "startup_check",
        }
        assert set(models["defaults"]) == {"model_id", "thinking", "effort"}

    def test_defaults_and_overrides(self, load):
        from src.api.routes.config import _models

        load(
            CODEKB_LLM_THINKING="between_tools",
            CODEKB_SUMMARIZATION_EFFORT="low",
            CODEKB_CHAT_MODEL_ID=SONNET_55,
        )
        models = _models()
        by_workload = {m["workload"]: m for m in models["workloads"]}

        assert models["defaults"] == {
            "model_id": OPUS_46,
            "thinking": "between_tools",
            "effort": None,
        }
        assert by_workload["summarization"]["overrides"] == ["effort"]
        assert by_workload["business_context"]["overrides"] == []
        # The chat's own model is an override; its "Model default" thinking is not.
        assert by_workload["chat"]["overrides"] == ["model_id"]

    def test_model_default_shows_as_none_in_defaults(self, load):
        from src.api.routes.config import _models

        load()  # default.yaml ships thinking: model_default

        assert _models()["defaults"]["thinking"] is None

    def test_legacy_chat_reasoning_counts_as_an_override(self, load):
        from src.api.routes.config import _models

        load(CODEKB_BEDROCK_REASONING="true")
        chat = {m["workload"]: m for m in _models()["workloads"]}["chat"]

        assert chat["thinking"] == "enabled"
        assert chat["overrides"] == ["thinking"]

    def test_startup_check_status_follows_the_probe(self, load):
        from src.api.routes.config import _models
        from src.core.llm_startup_check import probe_llm_settings, validate_llm_settings
        from test.bedrock_fakes import RejectingBedrockClient

        load(CODEKB_LLM_STARTUP_CHECK="true")
        probe_llm_settings(validate_llm_settings(), client_factory=lambda: _RecordingClient())
        assert {m["startup_check"] for m in _models()["workloads"]} == {"verified"}

        throttled = RejectingBedrockClient("Rate exceeded", code="ThrottlingException")
        probe_llm_settings(validate_llm_settings(), client_factory=lambda: throttled)
        assert {m["startup_check"] for m in _models()["workloads"]} == {"not_verified"}

        load(CODEKB_LLM_STARTUP_CHECK="false")
        probe_llm_settings(validate_llm_settings())
        assert {m["startup_check"] for m in _models()["workloads"]} == {"not_checked"}

    def test_disabled_overview_is_not_checked(self, load):
        from src.api.routes.config import _models
        from src.core.llm_startup_check import probe_llm_settings, validate_llm_settings

        load(CODEKB_LLM_STARTUP_CHECK="true", CODEKB_OVERVIEW_GENERATION_ENABLED="false")
        probe_llm_settings(validate_llm_settings(), client_factory=lambda: _RecordingClient())

        status = {m["workload"]: m["startup_check"] for m in _models()["workloads"]}
        assert status["overview"] == "not_checked"
        assert status["summarization"] == "verified"


class TestOperationTokenRecords:
    """Each operation records the settings and tokens of every workload it ran."""

    def _settings(self, workload=Workload.SUMMARIZATION, **kwargs):
        return LlmSettings(workload, model_id=SONNET_55, max_tokens=8000, **kwargs)

    def test_workload_record_snapshots_settings_and_tokens(self):
        from src.core.token_usage import TokenUsage, workload_record

        usage = TokenUsage().add(SONNET_55, input=100, output=50, thinking=30)
        record = workload_record(self._settings(thinking="between_tools", effort="low"), usage)

        assert record == {
            "workload": "summarization",
            "model_id": SONNET_55,
            "thinking": "between_tools",
            "effort": "low",
            "input": 100,
            "output": 50,
            "total": 150,
            "thinking_tokens": 30,
        }

    def test_region_is_snapshotted_when_given(self):
        from src.core.token_usage import TokenUsage, workload_record

        assert workload_record(self._settings(), TokenUsage(), "eu-central-1")["region"] == (
            "eu-central-1"
        )
        assert "region" not in workload_record(self._settings(), TokenUsage())

    def test_summarizer_records_its_region(self):
        import asyncio

        from test.test_topdown_summarizer import _FakeGraphService
        from src.vector.topdown_summarizer import TopDownSummarizer

        s = TopDownSummarizer("/tmp/project", "space", codekb_service=_FakeGraphService({0: []}))
        s._bedrock_region = "eu-central-1"

        stats = asyncio.run(s.summarize_all())

        assert stats["llm_workload"]["region"] == "eu-central-1"

    def test_model_default_is_recorded_as_not_sent(self):
        from src.core.token_usage import TokenUsage, workload_record

        record = workload_record(self._settings(), TokenUsage())

        assert record["thinking"] is None
        assert record["effort"] is None
        assert "thinking_tokens" not in record

    def test_operation_usage_merges_parts_and_keeps_the_old_shape(self):
        from src.core.token_usage import TokenUsage, operation_usage, workload_record

        summary = TokenUsage().add(SONNET_55, input=1000, output=200)
        vocab = TokenUsage().add(OPUS_46, input=300, output=40)
        record = operation_usage(
            [
                {
                    "token_usage": summary.to_dict(),
                    "llm_workload": workload_record(self._settings(), summary),
                },
                {
                    "token_usage": vocab.to_dict(),
                    "llm_workload": workload_record(
                        LlmSettings(Workload.BUSINESS_CONTEXT, model_id=OPUS_46, max_tokens=2048),
                        vocab,
                    ),
                },
            ]
        )

        # by_model and totals are unchanged in shape, now including vocabulary.
        assert record["totals"] == {"input": 1300, "output": 240, "total": 1540}
        assert set(record["by_model"]) == {SONNET_55, OPUS_46}
        assert [w["workload"] for w in record["workloads"]] == ["summarization", "business_context"]

    def test_operation_usage_with_nothing_is_none(self):
        from src.core.token_usage import operation_usage

        assert operation_usage([None, {}]) is None

    def test_summarizer_stats_carry_the_workload_record(self):
        import asyncio

        from test.test_topdown_summarizer import _FakeGraphService
        from src.vector.topdown_summarizer import TopDownSummarizer

        s = TopDownSummarizer("/tmp/project", "space", codekb_service=_FakeGraphService({0: []}))
        s._llm = self._settings(thinking="between_tools")

        stats = asyncio.run(s.summarize_all())

        assert stats["llm_workload"]["workload"] == "summarization"
        assert stats["llm_workload"]["thinking"] == "between_tools"

    def test_business_vocabulary_tokens_are_counted(self):
        from src.analysis.repo_business_context_extractor import RepoBusinessContextExtractor
        from test.bedrock_fakes import FakeBedrockClient, anthropic_body

        e = RepoBusinessContextExtractor.__new__(RepoBusinessContextExtractor)
        e._bedrock_client = FakeBedrockClient(
            anthropic_body("{}", usage={"input_tokens": 300, "output_tokens": 40})
        )
        e._llm = LlmSettings(Workload.BUSINESS_CONTEXT, model_id=OPUS_46, max_tokens=2048)
        e._bedrock_region = "us-east-1"
        from src.core.token_usage import TokenUsage

        e.token_usage = TokenUsage()

        e._call_bedrock("INSTRUCTION", "repo metadata")
        part = e.usage_part()

        assert part["token_usage"]["totals"]["total"] == 340
        assert part["llm_workload"]["workload"] == "business_context"
        assert part["llm_workload"]["input"] == 300

    def test_creation_record_includes_business_vocabulary(self):
        from types import SimpleNamespace

        from src.core.token_usage import TokenUsage, workload_record
        from src.service.codekb_service import CodeKBService

        summary = TokenUsage().add(SONNET_55, input=1000, output=200)
        vocab = TokenUsage().add(OPUS_46, input=300, output=40)
        bc_settings = LlmSettings(Workload.BUSINESS_CONTEXT, model_id=OPUS_46, max_tokens=2048)
        kb = SimpleNamespace(
            analysis_metadata={
                "business_context_usage": {
                    "token_usage": vocab.to_dict(),
                    "llm_workload": workload_record(bc_settings, vocab),
                }
            }
        )
        summary_stats = {
            "token_usage": summary.to_dict(),
            "llm_workload": workload_record(self._settings(), summary),
        }

        record = CodeKBService._creation_usage(summary_stats, kb)

        assert record["totals"]["total"] == 1540
        assert {w["workload"] for w in record["workloads"]} == {"summarization", "business_context"}

    def test_creation_record_without_business_vocabulary(self):
        from types import SimpleNamespace

        from src.core.token_usage import TokenUsage, workload_record
        from src.service.codekb_service import CodeKBService

        summary = TokenUsage().add(SONNET_55, input=10, output=5)
        stats = {
            "token_usage": summary.to_dict(),
            "llm_workload": workload_record(self._settings(), summary),
        }

        record = CodeKBService._creation_usage(stats, SimpleNamespace(analysis_metadata={}))

        assert [w["workload"] for w in record["workloads"]] == ["summarization"]
