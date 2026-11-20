"""
Pytest configuration and fixtures for the Code Knowledge Base test suite.

The fixtures here mock out all AWS calls so tests run offline with no
credentials:
  - S3 is mocked via moto (real behavior against an in-memory S3).
  - S3 Vectors is a custom AWS service not covered by moto, so it's hand-rolled
    as an in-memory dict.
  - Bedrock (Titan embeddings) is mocked so S3VectorsClient._generate_embedding
    returns a deterministic vector without calling AWS.
"""

import json
import os
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import boto3
import botocore.session
import pytest
from moto import mock_aws

# OpenTelemetry log export is mandatory at startup: setup_logging() (run at
# import time by codekb.api.app) refuses to start when OTEL_LOGS_EXPORTER is
# unset. Set it here — at conftest import, before any test module is collected —
# so importing the app during collection doesn't fail. 'none' keeps test
# logs on stdout with no OTLP endpoint/IAM, mirroring a local run.
os.environ.setdefault("OTEL_LOGS_EXPORTER", "none")

# The guardrail fails fast at startup when enabled without an id (default.yaml
# ships it enabled but blank). Disable it here, before collection imports the
# app, mirroring a local run with no provisioned guardrail. The guardrail's own
# unit tests stub require_config directly, so they're unaffected.
os.environ.setdefault("CODEKB_BEDROCK_GUARDRAIL_ENABLED", "false")

# The LLM startup probe calls Bedrock from the app's lifespan. Turn it off so
# tests that enter the lifespan make no model calls; its own unit tests call
# probe_llm_settings directly with a fake client.
os.environ.setdefault("CODEKB_LLM_STARTUP_CHECK", "false")


@pytest.fixture(autouse=True, scope="session")
def _isolate_config():
    """Ensure tests use default.yaml values, ignoring .env and local overrides.

    Clears CODEKB_S3* env vars that the .env file sets, so moto buckets
    (named after default.yaml values) are the ones the code actually uses.
    """
    import os
    import src.core.config as config_module
    from src.core.config import ConfigurationManager, get_config_manager

    # Prevent .env from overriding test bucket names
    env_vars_to_clear = [
        "CODEKB_S3_BUCKET",
        "CODEKB_S3_REGION",
        "CODEKB_S3_VECTORS_BUCKET",
        "CODEKB_S3_VECTORS_REGION",
        "CODEKB_S3_VECTORS_INDEX",
    ]
    saved = {k: os.environ.pop(k, None) for k in env_vars_to_clear}

    # default.yaml ships blank bucket names on purpose (anti bucket-squatting),
    # so the suite must provide them the way a real deployment does: via env.
    # These match the moto buckets created in mock_s3.
    os.environ["CODEKB_S3_BUCKET"] = "codekb-knowledgebase"
    os.environ["CODEKB_S3_VECTORS_BUCKET"] = "codekb-vectors"

    # Reset global singleton so tests get a fresh config
    config_module._config_manager = None
    mgr = get_config_manager()

    # Patch out local overrides (now a no-op, but kept for safety)
    with patch.object(ConfigurationManager, "_apply_local_overrides"):
        mgr.load_configuration()
        yield

    config_module._config_manager = None
    # Restore env vars
    os.environ.pop("CODEKB_S3_BUCKET", None)
    os.environ.pop("CODEKB_S3_VECTORS_BUCKET", None)
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


@pytest.fixture(scope="session")
def mock_s3():
    """Mock S3 service using moto."""
    with mock_aws():
        s3_client = boto3.client("s3", region_name="us-east-1")
        # Create the buckets referenced by codekb/core/default.yaml
        s3_client.create_bucket(Bucket="codekb-knowledgebase")
        s3_client.create_bucket(Bucket="codekb-vectors")
        yield s3_client


@pytest.fixture(scope="session")
def mock_s3vectors():
    """Mock S3 Vectors service (not supported by moto, so hand-rolled)."""
    mock_client = MagicMock()
    vectors_store: Dict[str, Dict[str, Any]] = {}

    def put_vectors(
        vectorBucketName: str, indexName: str, vectors: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        for vector in vectors:
            key = vector["key"]
            vectors_store[key] = {
                "key": key,
                "data": vector.get("data", {}),
                "metadata": vector.get("metadata", {}),
            }
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def query_vectors(
        vectorBucketName: str,
        indexName: str,
        queryVector: Dict[str, List[float]],
        topK: int = 5,
        filter: Dict[str, Any] = None,
        returnDistance: bool = True,
        returnMetadata: bool = True,
    ) -> Dict[str, Any]:
        results = []
        for key, vec_data in list(vectors_store.items())[:topK]:
            result = {"key": key}
            if returnDistance:
                result["distance"] = 0.5  # mock
            if returnMetadata:
                result["metadata"] = vec_data.get("metadata", {})
            results.append(result)
        return {"vectors": results}

    def get_vectors(
        vectorBucketName: str,
        indexName: str,
        keys: List[str],
        returnMetadata: bool = True,
    ) -> Dict[str, Any]:
        results = []
        for key in keys:
            if key in vectors_store:
                vec_data = vectors_store[key]
                result = {"key": key, "data": vec_data.get("data", {})}
                if returnMetadata:
                    result["metadata"] = vec_data.get("metadata", {})
                results.append(result)
        return {"vectors": results}

    def list_vectors(
        vectorBucketName: str,
        indexName: str,
        maxResults: int = 100,
        returnMetadata: bool = True,
    ) -> Dict[str, Any]:
        results = []
        for key, vec_data in list(vectors_store.items())[:maxResults]:
            result = {"key": key, "data": vec_data.get("data", {})}
            if returnMetadata:
                result["metadata"] = vec_data.get("metadata", {})
            results.append(result)
        return {"vectors": results}

    def delete_vectors(vectorBucketName: str, indexName: str, keys: List[str]) -> Dict[str, Any]:
        for key in keys:
            vectors_store.pop(key, None)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    mock_client.put_vectors = MagicMock(side_effect=put_vectors)
    mock_client.query_vectors = MagicMock(side_effect=query_vectors)
    mock_client.get_vectors = MagicMock(side_effect=get_vectors)
    mock_client.list_vectors = MagicMock(side_effect=list_vectors)
    mock_client.delete_vectors = MagicMock(side_effect=delete_vectors)
    mock_client._vectors_store = vectors_store
    return mock_client


@pytest.fixture(scope="session")
def mock_bedrock_runtime():
    """Mock Bedrock runtime client used by S3VectorsClient for Titan embeddings.

    Returns a deterministic 1024-dim zero-ish vector for any invoke_model call,
    so we never hit AWS during tests.
    """
    mock_client = MagicMock()

    def invoke_model(modelId: str, contentType: str, accept: str, body: str) -> Dict[str, Any]:
        # Real Titan V2 returns {"embedding": [...], "inputTextTokenCount": N}
        payload = {"embedding": [0.1] * 1024, "inputTextTokenCount": 1}
        response_body = MagicMock()
        response_body.read = MagicMock(return_value=json.dumps(payload).encode("utf-8"))
        return {"body": response_body}

    mock_client.invoke_model = MagicMock(side_effect=invoke_model)
    return mock_client


@pytest.fixture(scope="session")
def mock_kms():
    """Mock KMS client (not supported by moto for all calls, so hand-rolled).

    ``describe_key`` returns a minimal response by default; tests that need the
    probe to fail set ``describe_key.side_effect``.
    """
    mock_client = MagicMock()
    mock_client.describe_key = MagicMock(return_value={"KeyMetadata": {"KeyId": "test"}})
    return mock_client


@pytest.fixture(autouse=True, scope="session")
def mock_aws_services(mock_s3, mock_s3vectors, mock_bedrock_runtime, mock_kms):
    """Combined fixture that intercepts every AWS client call made by codekb.

    Autouse + session-scoped so that a simple `uv run pytest` works without
    every test needing to request it explicitly.

    Covers both entry points. ``boto3.client`` is the common one, and
    ``botocore.session.Session.create_client`` is used directly by
    ``codekb.core.observability`` for its CloudWatch Logs client, so patching only
    the former would let a test reach the real service.
    """
    supported = {"s3", "s3vectors", "bedrock-runtime", "kms"}
    original_create_client = botocore.session.Session.create_client

    def guarded_create_client(self, service_name: str, *args: Any, **kwargs: Any):
        if service_name not in supported:
            raise ValueError(f"Unsupported AWS service in tests: {service_name}")
        return original_create_client(self, service_name, *args, **kwargs)

    with (
        patch("boto3.client") as mock_boto_client,
        patch.object(botocore.session.Session, "create_client", guarded_create_client),
    ):

        def client_factory(service_name: str, **kwargs):
            if service_name == "s3":
                return mock_s3
            if service_name == "s3vectors":
                return mock_s3vectors
            if service_name == "bedrock-runtime":
                return mock_bedrock_runtime
            if service_name == "kms":
                return mock_kms
            raise ValueError(f"Unsupported boto3 service in tests: {service_name}")

        mock_boto_client.side_effect = client_factory

        # Reset cached Bedrock client inside S3VectorsClient so tests that
        # import it pick up the mock.
        from src.vector.s3_vectors_client import S3VectorsClient

        S3VectorsClient._bedrock_client = None

        yield {
            "s3": mock_s3,
            "s3vectors": mock_s3vectors,
            "bedrock-runtime": mock_bedrock_runtime,
            "kms": mock_kms,
        }

        S3VectorsClient._bedrock_client = None


@pytest.fixture(autouse=True)
def _reset_service_singleton():
    """Give each test a fresh CodeKBService.

    CodeKBService is a process-wide singleton reached via
    ``CodeKBService.instance()``, so without this reset one test's cached
    context, config tweaks (e.g. ``_max_cached_spaces``), or seeded state would
    leak into the next. Clearing ``_instance`` before and after each test makes
    the next ``instance()`` build a fresh object, restoring per-test isolation.
    """
    from src.service.codekb_service import CodeKBService

    CodeKBService._instance = None
    yield
    CodeKBService._instance = None
