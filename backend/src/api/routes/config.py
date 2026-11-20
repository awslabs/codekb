"""Read-only configuration values exposed to the frontend.

GET /api/config — effective, non-sensitive config for the UI in one payload
"""

from fastapi import APIRouter, Request

from ...gitcreds.providers import enabled_provider_ids
from ...core.config import get_config, require_config
from ...core.llm_settings import MODEL_DEFAULT, Workload
from ...core.llm_settings import resolve as resolve_llm_settings
from ...core.llm_startup_check import startup_check_status
from ...core.logging_utils import get_logger
from ...service.codekb_service import (
    allowed_git_hosts,
    git_url_credentials_allowed,
    overview_generation_enabled,
    zip_upload_enabled,
)
from ..auth import require_current_user

logger = get_logger(__name__)

router = APIRouter()


def _infrastructure_status() -> dict:
    """Live readiness of the CDK-provisioned S3 infrastructure.

    Acutual resource ARNs etc are intentionally not included as a security measure.
    """
    import boto3

    storage = get_config("storage", {})
    s3_cfg = storage.get("s3", {})
    vec_cfg = storage.get("s3_vectors", {})
    s3_region = s3_cfg.get("region", "")
    s3_bucket = s3_cfg.get("bucket_name", "")
    vectors_bucket = vec_cfg.get("bucket_name", "")
    index_name = vec_cfg.get("index_name", "")
    vectors_region = vec_cfg.get("region", s3_region)
    gitcreds_kms_arn = (get_config("gitcreds.kms_key_arn", "") or "").strip()

    if not s3_bucket or not vectors_bucket:
        return {
            "ready": False,
            "s3_bucket": {"exists": False},
            "vectors_bucket": {"exists": False},
            "vector_index": {"exists": False},
            "kms_key": {"exists": False},
            "error": "Infrastructure buckets are not configured.",
        }

    s3 = boto3.client("s3", region_name=s3_region)
    s3vectors = boto3.client("s3vectors", region_name=vectors_region)

    s3_ok = vec_ok = idx_ok = kms_ok = False

    try:
        s3.head_bucket(Bucket=s3_bucket)
        s3_ok = True
    except Exception as e:
        logger.debug(f"S3 bucket check failed: {e}")

    try:
        s3vectors.get_vector_bucket(vectorBucketName=vectors_bucket)
        vec_ok = True
    except Exception as e:
        logger.debug(f"S3 Vectors bucket check failed: {e}")

    if vec_ok:
        try:
            s3vectors.get_index(vectorBucketName=vectors_bucket, indexName=index_name)
            idx_ok = True
        except Exception as e:
            logger.debug(f"Vector index check failed: {e}")

    if gitcreds_kms_arn:
        kms = boto3.client("kms", region_name=s3_region)
        try:
            kms.describe_key(KeyId=gitcreds_kms_arn)
            kms_ok = True
        except Exception as e:
            logger.debug(f"Git credentials KMS key check failed: {e}")

    return {
        "ready": s3_ok and vec_ok and idx_ok,
        "s3_bucket": {"exists": s3_ok},
        "vectors_bucket": {"exists": vec_ok},
        "vector_index": {"exists": idx_ok},
        "kms_key": {"exists": kms_ok},
    }


@router.get("")
async def get_settings(request: Request) -> dict:
    """Return read-only configuration for the frontend.

    - ``default_top_k``: default top-K for semantic search when ``limit`` is omitted.
    - ``allowed_git_hosts``: the effective Git host allowlist as the validator
      enforces it (fail-closed — an empty list means repository-by-URL analysis
      is disabled).
    - ``infrastructure``: live readiness of the CDK-provisioned S3 infra. Returns
      readiness booleans only (FIX-25); no bucket names or region.
    - ``models``: each workload's model id, thinking type and effort, whether
      this process's startup check verified them, and the embeddings model id.
    - ``zip_upload_enabled``: whether the zip-upload source is enabled. When
      false, the UI hides the upload tile and the upload endpoints reject
      requests (enterprises can restrict analysis to vetted repositories).
    - ``overview_generation_enabled``: whether on-demand overview generation
      (Space + Hyperspace) is enabled.
    - ``git_credential_providers``: git providers enabled for per-user
      credentials, so the UI shows only those (empty when none are configured).
    - ``allow_credentials_in_git_url``: whether the API accepts credentials
      embedded in a git URL (the UI mirrors this).
    - ``exclude_patterns``: the system-wide exclusion patterns new spaces are
      created with (``default.yaml`` plus ``CODEKB_EXCLUDE_PATTERNS_EXTRA``).
    """
    require_current_user(request)
    return {
        "default_top_k": int(require_config("storage.s3_vectors.default_top_k")),
        "allowed_git_hosts": allowed_git_hosts(),
        "infrastructure": _infrastructure_status(),
        "zip_upload_enabled": zip_upload_enabled(),
        "overview_generation_enabled": overview_generation_enabled(),
        "git_credential_providers": enabled_provider_ids(),
        "allow_credentials_in_git_url": git_url_credentials_allowed(),
        "exclude_patterns": list(require_config("analysis.exclude_patterns")),
        "models": _models(),
    }


_DISPLAYED_FIELDS = ("model_id", "thinking", "effort")


def _shown(value: object) -> str | None:
    """A displayed setting value; None when nothing is sent and the model decides."""
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text == MODEL_DEFAULT else text


def _models() -> dict:
    """The shared defaults, each workload's settings, and the embeddings model.

    Each workload lists the displayed fields it sets itself in ``overrides``,
    so the UI can tell an override from an inherited value.

    Model ids are not sensitive: the space pages already show the model that
    wrote each space. Region, guardrail, token caps and budgets stay out.
    """
    defaults_cfg = get_config("llm.defaults", None) or {}
    if not isinstance(defaults_cfg, dict):
        defaults_cfg = {}
    defaults = {name: _shown(defaults_cfg.get(name)) for name in _DISPLAYED_FIELDS}
    workloads = []
    for workload in Workload:
        try:
            settings = resolve_llm_settings(workload)
        except Exception:  # startup refuses invalid settings; never fail the page
            logger.warning(f"Could not resolve LLM settings for {workload.value}", exc_info=True)
            continue
        workloads.append(
            {
                "workload": workload.value,
                "model_id": settings.model_id,
                # None when no thinking field is sent and the model's default applies.
                "thinking": None if settings.thinking == MODEL_DEFAULT else settings.thinking,
                "effort": settings.effort,
                # Fields this workload sets itself instead of inheriting.
                "overrides": [
                    name
                    for name in _DISPLAYED_FIELDS
                    if settings.sources.get(name, "").startswith("llm.workloads.")
                ],
                "startup_check": startup_check_status(workload),
            }
        )
    return {
        "defaults": defaults,
        "workloads": workloads,
        "embedding_model_id": str(require_config("storage.s3_vectors.embedding_model")),
    }
