"""Regression tests for the two review issues on parallel summarization:

  Issue 1 — the Bedrock / S3 Vectors botocore clients must size their HTTP
            connection pool to the summarization concurrency (botocore
            defaults ``max_pool_connections`` to 10, which caps real
            parallelism at ~10 and churns connections at concurrency > 10).

  Issue 2 — lazy creation of the shared client must be thread-safe: when all
            workers hit the first-use creation simultaneously, exactly one
            client is built (double-checked locking), with no race.

These tests are deterministic (no AWS, no network): they spy on
``boto3.client`` to (a) capture the ``config`` passed so we can assert the
pool size, and (b) count how many times each client is actually created under
40 concurrent threads. The client factory sleeps briefly to widen the race
window so the thread-safety assertion is reliable.

The same file runs against the pre-fix code as a before/after check:
  - pre-fix topdown  -> pool is botocore default 10  (assert 40 fails)
  - pre-fix s3vectors -> no config passed at all      (assert 40 fails)
  - pre-fix creation  -> >1 client built under 40 threads (assert ==1 fails)
"""

import json
import threading
import time
from unittest.mock import MagicMock

import boto3
import pytest

from src.vector.topdown_summarizer import TopDownSummarizer
from src.vector.s3_vectors_client import S3VectorsClient

CONCURRENCY = 40


class _DummyService:
    """Service stand-in — these tests never exercise data access."""

    def get_business_relevance_tier(self, *args, **kwargs):
        return None


class _Body:
    """Minimal streaming-body stand-in for a Bedrock invoke_model response."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload


class ClientSpy:
    """Drop-in replacement for ``boto3.client`` that records every creation.

    Records (service_name, config) per call and returns a MagicMock. For
    ``bedrock-runtime`` the mock's ``invoke_model`` returns a valid Titan
    embedding so ``S3VectorsClient._generate_embedding`` completes cleanly.
    An optional delay widens the creation window to expose races.
    """

    def __init__(self, delay: float = 0.0):
        self.calls = []  # list[tuple[str, botocore.config.Config | None]]
        self.delay = delay

    def __call__(self, service_name, *args, **kwargs):
        self.calls.append((service_name, kwargs.get("config")))
        if self.delay:
            time.sleep(self.delay)
        client = MagicMock(name=f"client:{service_name}")
        if service_name == "bedrock-runtime":
            payload = json.dumps({"embedding": [0.0] * 1024}).encode("utf-8")
            client.invoke_model.return_value = {"body": _Body(payload)}
        return client

    def configs_for(self, service_name):
        return [cfg for (svc, cfg) in self.calls if svc == service_name]


def _override_concurrency(monkeypatch, n):
    """Force summarization.concurrency=n for S3VectorsClient._pool_size()."""
    import src.vector.s3_vectors_client as s3mod

    real_require_config = s3mod.require_config

    def fake_require_config(key):
        if key == "summarization.concurrency":
            return n
        return real_require_config(key)

    monkeypatch.setattr(s3mod, "require_config", fake_require_config)


@pytest.fixture(autouse=True)
def _reset_shared_bedrock_client():
    """Isolate the process-wide S3VectorsClient._bedrock_client between tests."""
    S3VectorsClient._bedrock_client = None
    yield
    S3VectorsClient._bedrock_client = None


# --------------------------------------------------------------------------- #
# Issue 1 — connection pool sized to concurrency
# --------------------------------------------------------------------------- #


def test_topdown_bedrock_pool_sized_to_concurrency(monkeypatch):
    spy = ClientSpy()
    monkeypatch.setattr(boto3, "client", spy)

    s = TopDownSummarizer("/tmp/proj", "space", codekb_service=_DummyService())
    s._concurrency = CONCURRENCY
    s._bedrock_client = None

    s._get_bedrock_client()

    configs = spy.configs_for("bedrock-runtime")
    assert len(configs) == 1
    cfg = configs[0]
    assert cfg is not None and cfg.max_pool_connections == CONCURRENCY, (
        f"expected pool sized to {CONCURRENCY}, got {getattr(cfg, 'max_pool_connections', None)}"
    )


def test_s3vectors_client_pool_sized_to_concurrency(monkeypatch):
    _override_concurrency(monkeypatch, CONCURRENCY)
    spy = ClientSpy()
    monkeypatch.setattr(boto3, "client", spy)

    S3VectorsClient(region="us-east-1")

    configs = spy.configs_for("s3vectors")
    assert len(configs) == 1
    cfg = configs[0]
    assert cfg is not None and cfg.max_pool_connections == CONCURRENCY, (
        f"expected pool sized to {CONCURRENCY}, got {getattr(cfg, 'max_pool_connections', None)}"
    )


def test_s3vectors_embedding_pool_sized_to_concurrency(monkeypatch):
    _override_concurrency(monkeypatch, CONCURRENCY)
    spy = ClientSpy()
    monkeypatch.setattr(boto3, "client", spy)

    c = S3VectorsClient(region="us-east-1")
    c._generate_embedding("hello world")

    configs = spy.configs_for("bedrock-runtime")
    assert len(configs) == 1
    cfg = configs[0]
    assert cfg is not None and cfg.max_pool_connections == CONCURRENCY, (
        f"expected pool sized to {CONCURRENCY}, got {getattr(cfg, 'max_pool_connections', None)}"
    )


# --------------------------------------------------------------------------- #
# Issue 2 — thread-safe single client creation under concurrency
# --------------------------------------------------------------------------- #


def test_topdown_bedrock_created_once_under_concurrency(monkeypatch):
    spy = ClientSpy(delay=0.05)
    monkeypatch.setattr(boto3, "client", spy)

    s = TopDownSummarizer("/tmp/proj", "space", codekb_service=_DummyService())
    s._concurrency = CONCURRENCY
    s._bedrock_client = None

    errors = []

    def worker():
        try:
            s._get_bedrock_client()
        except Exception as e:  # pragma: no cover - only on a real race failure
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(CONCURRENCY)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"client creation raised under concurrency: {errors[:1]}"
    created = spy.configs_for("bedrock-runtime")
    assert len(created) == 1, (
        f"Bedrock client created {len(created)}x under {CONCURRENCY}-way "
        f"concurrency (expected exactly 1 — creation is not thread-safe)"
    )


def test_s3vectors_embedding_created_once_under_concurrency(monkeypatch):
    _override_concurrency(monkeypatch, CONCURRENCY)
    spy = ClientSpy(delay=0.05)
    monkeypatch.setattr(boto3, "client", spy)

    c = S3VectorsClient(region="us-east-1")

    errors = []

    def worker():
        try:
            c._generate_embedding("hello world")
        except Exception as e:  # pragma: no cover - only on a real race failure
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(CONCURRENCY)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"embedding client creation raised under concurrency: {errors[:1]}"
    created = spy.configs_for("bedrock-runtime")
    assert len(created) == 1, (
        f"embedding Bedrock client created {len(created)}x under {CONCURRENCY}-way "
        f"concurrency (expected exactly 1 — creation is not thread-safe)"
    )
