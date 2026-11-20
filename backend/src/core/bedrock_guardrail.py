"""Attach the always-on secret/PII guardrail to Bedrock text-generation calls.

Without it, a credential committed to repo source reaches Bedrock during
summarization and persists into the stored descriptions and the shared vector
index, resurfacing to other users. The guardrail (built-in PII + custom regex,
all ANONYMIZE) masks it in the model input.

Provisioned by the deploy (CDK/CLI), injected via CODEKB_BEDROCK_GUARDRAIL_*.
Titan embeddings aren't guarded here — they embed the already-redacted
description, so the guardrail covers them transitively.
"""

from __future__ import annotations

import logging
from enum import Enum

from .config import require_config
from .logging_utils import ConfigurationError

logger = logging.getLogger(__name__)


class GuardrailTarget(Enum):
    """Which SDK to build kwargs for — the two differ only in key spelling.

    Each value is the ``(id_key, version_key)`` that SDK expects.
    """

    BOTO3_INVOKE_MODEL = ("guardrailIdentifier", "guardrailVersion")
    STRANDS_BEDROCK_MODEL = ("guardrail_id", "guardrail_version")


def validate_guardrail_config() -> None:
    """Validate the guardrail config at startup.

    Enabled but missing an id/version is a deploy mistake — fail fast instead of
    letting it surface as denied calls mid-run. Disabled is a deliberate choice
    that leaves secrets unredacted, so warn loudly rather than abort.
    """
    if not require_config("llm.providers.bedrock.guardrail.enabled"):
        logger.warning(
            "Bedrock secret/PII guardrail disabled — committed secrets reach "
            "Bedrock and the vector index unredacted."
        )
        return

    missing = [
        env_var
        for env_var, key in (
            ("CODEKB_BEDROCK_GUARDRAIL_ID", "llm.providers.bedrock.guardrail.id"),
            ("CODEKB_BEDROCK_GUARDRAIL_VERSION", "llm.providers.bedrock.guardrail.version"),
        )
        if not require_config(key)
    ]
    if missing:
        raise ConfigurationError(
            "Bedrock guardrail enabled but "
            + " and ".join(missing)
            + " not set. Provision it via the CDK/CLI deploy, or set "
            "CODEKB_BEDROCK_GUARDRAIL_ENABLED=false to run without redaction."
        )
    logger.info("Bedrock secret/PII guardrail enabled.")


def get_guardrail_kwargs(target: GuardrailTarget) -> dict[str, str]:
    """Guardrail kwargs spelled for ``target``'s SDK, or ``{}`` when disabled.

    When disabled, ``{}`` leaves the call unguarded; under the deploy's IAM policy
    that call is then denied, failing closed rather than running unredacted.
    """
    if not require_config("llm.providers.bedrock.guardrail.enabled"):
        return {}

    # Startup (validate_guardrail_config) refused to boot if enabled without an
    # id/version, so both are present here.
    guardrail_id = require_config("llm.providers.bedrock.guardrail.id")
    version = str(require_config("llm.providers.bedrock.guardrail.version"))
    id_key, version_key = target.value
    return {id_key: guardrail_id, version_key: version}
