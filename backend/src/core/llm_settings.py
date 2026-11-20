"""Per-workload LLM settings: model, output cap, thinking and effort.

Each text-generation call site resolves its settings once with
:func:`resolve` for its :class:`Workload`, then builds its request fields with
:func:`anthropic_request_fields`. The mapping from settings to the provider API
lives here only, so an API change (a new thinking type, the Converse API)
touches one module.

Resolution, per field, first non-empty value wins:

1. ``llm.workloads.<workload>.<field>``, env ``CODEKB_<WORKLOAD>_<FIELD>``
   (e.g. ``CODEKB_SUMMARIZATION_THINKING``).
2. ``llm.defaults.<field>``, env ``CODEKB_BEDROCK_MODEL_ID``,
   ``CODEKB_BEDROCK_MAX_TOKENS``, ``CODEKB_LLM_THINKING``, ``CODEKB_LLM_EFFORT``.

The model-bound fields (thinking, effort, budget_tokens) only
make sense for one model: the same value is accepted by one model and rejected
by another. A workload that sets its own ``model_id`` therefore does not
inherit them from ``llm.defaults``; it uses its own values or the model's
default.

Thinking and effort values are sent to the API unchanged and are not checked
against a list here, so a value introduced by a new model works without a
CodeKB release. Bedrock rejects a bad value with a ``ValidationException``;
:func:`settings_error_from_bedrock` turns that into a ``ConfigurationError``
that names the setting to fix.

This module must not import ``config`` at module level: ``config`` imports it
to build the env-var map.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional

# Send no thinking field: the model's own default applies (off on Claude Opus
# 4.8 and earlier, adaptive thinking on Claude 5.x).
MODEL_DEFAULT = "model_default"

# Legacy extended thinking needs room for the answer above the thinking budget.
_ENABLED_ANSWER_HEADROOM = 1024


class Workload(Enum):
    """A kind of LLM call with its own settings. The value is its YAML key."""

    SUMMARIZATION = "summarization"
    BUSINESS_CONTEXT = "business_context"
    OVERVIEW = "overview"
    CHAT = "chat"

    def env_var(self, field_name: str) -> str:
        """Env var for one of this workload's fields, e.g. CODEKB_SUMMARIZATION_THINKING."""
        return f"CODEKB_{self.name}_{field_name.upper()}"


# Fields a workload can override through env vars. budget_tokens is YAML-only:
# it only applies to the legacy "enabled" thinking type.
_WORKLOAD_ENV_FIELDS = ("model_id", "max_tokens", "thinking", "effort")
_MODEL_BOUND_FIELDS = ("thinking", "effort", "budget_tokens")

# Env vars that feed llm.defaults, by field. The model and max_tokens keep
# their original CODEKB_BEDROCK_* names for backward compatibility.
_DEFAULTS_ENV_VARS: Dict[str, str] = {
    "model_id": "CODEKB_BEDROCK_MODEL_ID",
    "max_tokens": "CODEKB_BEDROCK_MAX_TOKENS",
    "thinking": "CODEKB_LLM_THINKING",
    "effort": "CODEKB_LLM_EFFORT",
}


# Deprecated chat-only switch from before per-workload settings: reasoning=true
# means thinking "enabled" with reasoning_budget_tokens. Kept so existing
# deployments behave the same; CODEKB_CHAT_THINKING takes precedence.
_LEGACY_REASONING = "llm.workloads.chat.reasoning"
_LEGACY_REASONING_BUDGET = "llm.workloads.chat.reasoning_budget_tokens"
_LEGACY_CHAT_ENV_VARS: Dict[str, str] = {
    _LEGACY_REASONING: "CODEKB_BEDROCK_REASONING",
    _LEGACY_REASONING_BUDGET: "CODEKB_BEDROCK_REASONING_BUDGET_TOKENS",
}


def workload_env_overrides() -> Dict[str, str]:
    """Env var -> config path for every workload field, for config's env map."""
    return {
        workload.env_var(name): f"llm.workloads.{workload.value}.{name}"
        for workload in Workload
        for name in _WORKLOAD_ENV_FIELDS
    }


def workload_int_env_vars() -> set[str]:
    """The generated workload env vars whose values are ints."""
    return {workload.env_var("max_tokens") for workload in Workload}


def defaults_env_overrides() -> Dict[str, str]:
    """Env var -> config path for the new provider-neutral shared defaults."""
    return {
        _DEFAULTS_ENV_VARS["thinking"]: "llm.defaults.thinking",
        _DEFAULTS_ENV_VARS["effort"]: "llm.defaults.effort",
    }


@dataclass(frozen=True)
class LlmSettings:
    """Resolved settings for one workload.

    ``sources`` maps each field to where its value came from: a config path,
    ``"model_default"``, or ``"unset"``. It is used to name the setting to fix
    in error messages and in the startup log.
    """

    workload: Workload
    model_id: str
    max_tokens: int
    thinking: str = MODEL_DEFAULT
    effort: Optional[str] = None
    budget_tokens: Optional[int] = None
    sources: Mapping[str, str] = field(default_factory=dict)

    def setting_name(self, field_name: str) -> str:
        """The env var a user would set to change ``field_name`` for this workload.

        Points at the layer the value came from, so the message names the
        variable that is actually in effect.
        """
        return _setting_name(self.workload, field_name, self.sources.get(field_name, ""))

    def describe(self) -> str:
        """One line for the startup log: each field's value and where it came from."""
        parts = []
        for name in ("model_id", "max_tokens", "thinking", "effort"):
            value = getattr(self, name)
            shown = "(unset)" if value is None else value
            parts.append(f"{name}={shown} [{self.sources.get(name, 'unset')}]")
        return ", ".join(parts)


def _setting_name(workload: Workload, field_name: str, source: str) -> str:
    """The env var (or YAML key) that feeds ``field_name`` from ``source``."""
    if source in _LEGACY_CHAT_ENV_VARS:
        return _LEGACY_CHAT_ENV_VARS[source]
    if source.startswith("llm.defaults."):
        return _DEFAULTS_ENV_VARS.get(field_name, f"llm.defaults.{field_name}")
    if field_name in _WORKLOAD_ENV_FIELDS:
        return workload.env_var(field_name)
    return f"llm.workloads.{workload.value}.{field_name}"


def _is_unset(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _clean(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def resolve(workload: Workload) -> LlmSettings:
    """Resolve the settings for ``workload`` from the loaded configuration.

    Raises:
        ConfigurationError: max_tokens or budget_tokens is not a positive int,
            or thinking is "enabled" without a budget.
    """
    from .config import get_config  # lazy: config imports this module
    from .logging_utils import ConfigurationError

    workload_cfg = get_config(f"llm.workloads.{workload.value}", None) or {}
    defaults_cfg = get_config("llm.defaults", None) or {}
    if not isinstance(workload_cfg, dict) or not isinstance(defaults_cfg, dict):
        raise ConfigurationError(
            f"llm.workloads.{workload.value} and llm.defaults must be mappings"
        )

    own_model = not _is_unset(workload_cfg.get("model_id"))
    values: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    for name in ("model_id", "max_tokens", *_MODEL_BOUND_FIELDS):
        layers = [(f"llm.workloads.{workload.value}.{name}", workload_cfg.get(name))]
        if not (own_model and name in _MODEL_BOUND_FIELDS):
            layers.append((f"llm.defaults.{name}", defaults_cfg.get(name)))
        for source, raw in layers:
            if not _is_unset(raw):
                values[name] = _clean(raw)
                sources[name] = source
                break
        else:
            values[name] = None
            sources[name] = "unset"

    # Legacy chat switch: an explicit chat setting, so it beats the shared
    # default but not CODEKB_CHAT_THINKING.
    if (
        workload is Workload.CHAT
        and _is_unset(workload_cfg.get("thinking"))
        and bool(workload_cfg.get("reasoning"))
    ):
        values["thinking"] = "enabled"
        sources["thinking"] = _LEGACY_REASONING
        if _is_unset(workload_cfg.get("budget_tokens")):
            values["budget_tokens"] = workload_cfg.get("reasoning_budget_tokens")
            sources["budget_tokens"] = _LEGACY_REASONING_BUDGET

    if values["thinking"] is None:
        values["thinking"] = MODEL_DEFAULT
        sources["thinking"] = MODEL_DEFAULT

    def _positive_int(name: str) -> Optional[int]:
        raw = values[name]
        if raw is None:
            return None
        try:
            number = int(raw)
        except (TypeError, ValueError):
            number = 0
        if number < 1:
            raise ConfigurationError(
                f"{_setting_name(workload, name, sources[name])} must be a positive "
                f"integer, got {raw!r}"
            )
        return number

    max_tokens = _positive_int("max_tokens")
    if max_tokens is None:
        raise ConfigurationError(
            f"No max_tokens for the {workload.value} workload: set llm.defaults.max_tokens "
            f"(CODEKB_BEDROCK_MAX_TOKENS) or {workload.env_var('max_tokens')}"
        )
    budget_tokens = _positive_int("budget_tokens")
    if values["thinking"] == "enabled" and budget_tokens is None:
        raise ConfigurationError(
            f"thinking 'enabled' for the {workload.value} workload needs "
            f"llm.workloads.{workload.value}.budget_tokens"
        )

    return LlmSettings(
        workload=workload,
        model_id=str(values["model_id"] or ""),
        max_tokens=max_tokens,
        thinking=str(values["thinking"]),
        effort=None if values["effort"] is None else str(values["effort"]),
        budget_tokens=budget_tokens,
        sources=sources,
    )


def anthropic_request_fields(settings: LlmSettings) -> Dict[str, Any]:
    """``max_tokens``, ``thinking`` and ``output_config`` for an Anthropic Messages body.

    With thinking left to the model and no effort, this is just ``max_tokens``,
    so request bodies are unchanged from before these settings existed.
    """
    fields: Dict[str, Any] = {"max_tokens": settings.max_tokens}
    if settings.thinking != MODEL_DEFAULT:
        thinking: Dict[str, Any] = {"type": settings.thinking}
        if settings.thinking == "enabled" and settings.budget_tokens is not None:
            thinking["budget_tokens"] = settings.budget_tokens
            fields["max_tokens"] = max(
                settings.max_tokens, settings.budget_tokens + _ENABLED_ANSWER_HEADROOM
            )
        if settings.workload is Workload.CHAT and settings.thinking == "adaptive":
            # Agent Hopper shows the reasoning as it streams. Claude 5.x returns
            # empty thinking text unless display is "summarized" (earlier models
            # default to it). Billing is the same either way. Only adaptive takes
            # it: Bedrock rejects display with between_tools and disabled.
            thinking["display"] = "summarized"
        fields["thinking"] = thinking
    if settings.effort is not None:
        fields["output_config"] = {"effort": settings.effort}
    return fields


def strands_model_kwargs(settings: LlmSettings) -> Dict[str, Any]:
    """Keyword arguments for a Strands ``BedrockModel`` (Converse API).

    Converse takes ``maxTokens`` in its inference config and the model-specific
    ``thinking`` and ``output_config`` in ``additionalModelRequestFields``, so
    the same settings reach the model as on the InvokeModel path. Guardrail
    kwargs are added by the caller.
    """
    from botocore.config import Config

    from .config import require_config  # lazy: config imports this module

    fields = anthropic_request_fields(settings)
    kwargs: Dict[str, Any] = {
        "model_id": settings.model_id,
        "region_name": require_config("llm.providers.bedrock.region"),
        "max_tokens": fields.pop("max_tokens"),
        "boto_client_config": Config(
            read_timeout=int(require_config("llm.providers.bedrock.read_timeout"))
        ),
    }
    if fields:
        kwargs["additional_request_fields"] = fields
    return kwargs


def legacy_setting_warnings() -> list[str]:
    """Warnings for deprecated settings that are in effect or being ignored.

    Logged once at startup by ``validate_llm_settings``; ``resolve`` runs per
    chat request, so it stays silent.
    """
    from .config import get_config  # lazy: config imports this module

    chat_cfg = get_config(f"llm.workloads.{Workload.CHAT.value}", None) or {}
    if not isinstance(chat_cfg, dict) or not bool(chat_cfg.get("reasoning")):
        return []
    if not _is_unset(chat_cfg.get("thinking")):
        return [
            "CODEKB_BEDROCK_REASONING is ignored because CODEKB_CHAT_THINKING is set. "
            "Remove CODEKB_BEDROCK_REASONING."
        ]
    return [
        "CODEKB_BEDROCK_REASONING is deprecated and only works on models that accept "
        "thinking type 'enabled' (Claude 4.6 models and earlier). Set CODEKB_CHAT_THINKING "
        "instead, for example 'adaptive'."
    ]


# Request fields whose rejection means the configured settings are wrong for the
# model, as opposed to a bad prompt or a transient failure.
_SETTINGS_FIELDS_IN_ERRORS = ("thinking", "output_config", "effort", "max_tokens")


def _find_client_error(exc: BaseException, client_error_type: type) -> Optional[Any]:
    """The botocore ``ClientError`` behind ``exc``, if any.

    Strands wraps model errors in its own ``EventLoopException``, which keeps
    the original as ``original_exception`` and as ``__cause__``.
    """
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, client_error_type):
            return current
        seen.add(id(current))
        original = getattr(current, "original_exception", None)
        current = original if isinstance(original, BaseException) else current.__cause__
    return None


def settings_error_from_bedrock(exc: BaseException, settings: LlmSettings) -> Optional[Exception]:
    """A ``ConfigurationError`` naming the setting to fix, if Bedrock rejected the settings.

    Returns ``None`` for any other failure, which the caller handles as before.
    Bedrock answers a value the model doesn't accept with a ValidationException
    whose message names the request field, e.g. ``thinking.type.between_tools
    is not supported for this model``. Retrying cannot succeed.
    """
    from botocore.exceptions import ClientError

    from .logging_utils import ConfigurationError

    client_error = _find_client_error(exc, ClientError)
    if client_error is None:
        return None
    error = client_error.response.get("Error", {}) or {}
    if error.get("Code") != "ValidationException":
        return None
    message = str(error.get("Message", ""))
    if not any(name in message for name in _SETTINGS_FIELDS_IN_ERRORS):
        return None

    current = [
        f"{name}={getattr(settings, name) if getattr(settings, name) is not None else '(unset)'}"
        f" ({settings.setting_name(name)})"
        for name in ("max_tokens", "thinking", "effort")
    ]
    return ConfigurationError(
        f"Bedrock rejected the {settings.workload.value} settings for model "
        f"{settings.model_id}: {message}. Settings in effect: {', '.join(current)}. "
        "Change the setting that Bedrock names for this model."
    )


def max_tokens_error(settings: LlmSettings) -> ValueError:
    """The error for a response that hit max_tokens before writing any text."""
    return ValueError(
        f"Bedrock stopped at max_tokens ({settings.max_tokens}) before writing any text, "
        f"likely because thinking used the whole budget. Raise "
        f"{settings.setting_name('max_tokens')} or reduce thinking for the "
        f"{settings.workload.value} workload."
    )
