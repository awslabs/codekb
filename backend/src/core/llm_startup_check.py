"""Startup checks for the per-workload LLM settings.

Two checks, so a bad setting stops the service before it accepts work instead
of failing every analysis later:

- :func:`validate_llm_settings` checks what CodeKB can check by itself (a
  model is set, max_tokens is a positive integer, a legacy "enabled" thinking
  has a budget). Always on; raises on the first invalid workload.
- :func:`probe_llm_settings` checks what only Bedrock knows: whether the model
  accepts the thinking, effort and max_tokens values. It sends one short
  request per distinct model and settings combination. It raises only when
  Bedrock rejects the settings; any other failure (no credentials, throttling,
  an outage, an IAM deny) is logged as a warning, so a Bedrock problem can't
  keep the service from starting. Controlled by ``llm.startup_check``
  (``CODEKB_LLM_STARTUP_CHECK``), on by default.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from .bedrock_guardrail import GuardrailTarget, get_guardrail_kwargs
from .config import get_config, require_config
from .llm_settings import (
    LlmSettings,
    Workload,
    anthropic_request_fields,
    legacy_setting_warnings,
    resolve,
    settings_error_from_bedrock,
)
from .logging_utils import ConfigurationError

logger = logging.getLogger(__name__)

_PROBE_PROMPT = "Reply with the single word OK."
# Keep a slow or unreachable Bedrock from holding up startup for long.
_PROBE_CONNECT_TIMEOUT_S = 5
_PROBE_READ_TIMEOUT_S = 60

# Outcome of this process's startup probe, per workload, for the Settings page.
STATUS_VERIFIED = "verified"  # Bedrock accepted the settings
STATUS_NOT_VERIFIED = "not_verified"  # the probe ran but couldn't reach Bedrock
STATUS_NOT_CHECKED = "not_checked"  # check off, workload disabled, or never run
_probe_results: Dict[Workload, str] = {}


def startup_check_status(workload: Workload) -> str:
    """This process's startup-probe outcome for ``workload`` (a ``STATUS_*`` value)."""
    return _probe_results.get(workload, STATUS_NOT_CHECKED)


def validate_llm_settings() -> List[LlmSettings]:
    """Resolve every workload's settings, raising on the first invalid one.

    Returns the resolved settings, in workload order, for the probe.

    Raises:
        ConfigurationError: a workload has no model, or a setting is invalid.
    """
    resolved: List[LlmSettings] = []
    for workload in Workload:
        try:
            settings = resolve(workload)
        except ConfigurationError as e:
            raise ConfigurationError(f"Invalid LLM settings for {workload.value}: {e}") from e
        if not settings.model_id:
            raise ConfigurationError(
                f"No model configured for {workload.value}: set CODEKB_BEDROCK_MODEL_ID "
                f"or {workload.env_var('model_id')}"
            )
        resolved.append(settings)
    for warning in legacy_setting_warnings():
        logger.warning(warning)
    return resolved


def _workload_enabled(workload: Workload) -> bool:
    """False for a workload the deployment has switched off, which isn't probed."""
    if workload is Workload.OVERVIEW:
        return bool(get_config("features.overview_generation_enabled", True))
    return True


def _probe_request(settings: LlmSettings) -> Dict[str, Any]:
    return {
        "anthropic_version": "bedrock-2023-05-31",
        **anthropic_request_fields(settings),
        "messages": [{"role": "user", "content": _PROBE_PROMPT}],
    }


def _default_client() -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-runtime",
        region_name=require_config("llm.providers.bedrock.region"),
        config=Config(
            connect_timeout=_PROBE_CONNECT_TIMEOUT_S,
            read_timeout=_PROBE_READ_TIMEOUT_S,
            retries={"max_attempts": 1},
        ),
    )


def probe_llm_settings(
    settings_list: List[LlmSettings],
    client_factory: Optional[Callable[[], Any]] = None,
) -> None:
    """Send one short request per distinct model and settings combination.

    Workloads that send the same model and request fields share one request.

    Raises:
        ConfigurationError: Bedrock rejected a combination's settings.
    """
    _probe_results.clear()
    if not require_config("llm.startup_check"):
        logger.info("LLM startup check skipped (CODEKB_LLM_STARTUP_CHECK=false)")
        return

    groups: Dict[Tuple[str, str], List[LlmSettings]] = {}
    for settings in settings_list:
        if not _workload_enabled(settings.workload):
            continue
        key = (settings.model_id, json.dumps(anthropic_request_fields(settings), sort_keys=True))
        groups.setdefault(key, []).append(settings)
    if not groups:
        return

    try:
        client = (client_factory or _default_client)()
    except Exception as e:
        logger.warning(f"LLM startup check skipped: could not create a Bedrock client: {e}")
        for members in groups.values():
            _record(members, STATUS_NOT_VERIFIED)
        return

    guardrail_kwargs = get_guardrail_kwargs(GuardrailTarget.BOTO3_INVOKE_MODEL)
    for (model_id, _), members in groups.items():
        first = members[0]
        names = ", ".join(s.workload.value for s in members)
        try:
            client.invoke_model(
                modelId=model_id,
                body=json.dumps(_probe_request(first)),
                **guardrail_kwargs,
            )
        except Exception as e:
            settings_error = settings_error_from_bedrock(e, first)
            if settings_error is not None:
                if len(members) > 1:
                    raise ConfigurationError(
                        f"{settings_error} The same model and settings are used by: {names}."
                    ) from e
                raise settings_error from e
            logger.warning(
                f"LLM startup check could not verify the settings for {names} "
                f"(model {model_id}); continuing: {e}"
            )
            _record(members, STATUS_NOT_VERIFIED)
            continue
        _record(members, STATUS_VERIFIED)
        logger.info(f"LLM startup check passed for {names} (model {model_id})")


def _record(members: List[LlmSettings], status: str) -> None:
    for settings in members:
        _probe_results[settings.workload] = status
