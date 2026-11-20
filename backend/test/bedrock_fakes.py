"""Shared fakes for Bedrock ``InvokeModel`` responses.

The Anthropic Messages API returns content as a list of *typed* blocks::

    {"content": [{"type": "text", "text": "..."}], "usage": {...}}

Both production call sites — ``TopDownSummarizer._call_bedrock`` and
``RepoBusinessContextExtractor._call_bedrock`` — select the answer by filtering
on ``block.get("type") == "text"``. That filter is deliberate: it is what lets
them skip the leading ``thinking`` block emitted when extended reasoning is
enabled (``llm.workloads.<workload>.thinking``).

Building the shape here, once, keeps fixtures from drifting away from the real
wire format. Fixtures that omitted ``"type"`` silently broke seven tests once
already; if you find yourself hand-writing a ``{"content": [...]}`` literal in a
test, use :func:`anthropic_body` instead.
"""

from __future__ import annotations

import json
from typing import Any


def anthropic_body(
    text: str | None = None,
    *,
    usage: dict[str, int] | None = None,
    thinking: str | None = None,
) -> dict[str, Any]:
    """Build an Anthropic ``InvokeModel`` response body.

    Args:
        text: Content for the ``text`` block. Pass ``None`` to build a response
            with no text block at all, which production code must reject.
        usage: Optional token usage, e.g.
            ``{"input_tokens": 1000, "output_tokens": 200}``. Omitted entirely
            when ``None``, matching responses that carry no usage.
        thinking: When set, prepends a ``thinking`` block ahead of the text
            block, mirroring a reasoning-enabled response.

    Returns:
        The response body as a dict, ready to be JSON-encoded.
    """
    content: list[dict[str, Any]] = []
    if thinking is not None:
        content.append({"type": "thinking", "thinking": thinking})
    if text is not None:
        content.append({"type": "text", "text": text})

    body: dict[str, Any] = {"content": content}
    if usage is not None:
        body["usage"] = usage
    return body


def anthropic_bytes(
    text: str | None = None,
    *,
    usage: dict[str, int] | None = None,
    thinking: str | None = None,
) -> bytes:
    """:func:`anthropic_body`, encoded as ``response["body"].read()`` returns it."""
    return json.dumps(anthropic_body(text, usage=usage, thinking=thinking)).encode("utf-8")


class FakeBody:
    """Stand-in for the streaming body on a boto3 ``invoke_model`` response."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class FakeBedrockClient:
    """Bedrock client returning one fixed response, counting invocations.

    Takes a raw payload *dict* rather than a text string so tests can also send
    deliberately malformed shapes (no text block, qwen format). Build well-formed
    payloads with :func:`anthropic_body`.
    """

    def __init__(self, payload: dict[str, Any]):
        self._payload = json.dumps(payload).encode("utf-8")
        self.calls = 0

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"body": FakeBody(self._payload)}


class CapturingBedrockClient(FakeBedrockClient):
    """:class:`FakeBedrockClient` that also records the last request body.

    Use when the assertion is about what we *sent* (prompt construction, input
    truncation, spotlighting) rather than what we parsed back.
    """

    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload)
        self.last_body: str | None = None

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self.last_body = kwargs["body"]
        return super().invoke_model(**kwargs)

    def sent_body(self) -> dict[str, Any]:
        """Return the whole captured request body, parsed."""
        if self.last_body is None:
            raise AssertionError("invoke_model was never called")
        return json.loads(self.last_body)

    def sent_message_content(self) -> str:
        """Return the user message content from the captured request body."""
        return self.sent_body()["messages"][0]["content"]


class RejectingBedrockClient:
    """Bedrock client whose every call fails with a ``ClientError``.

    Use to drive the error paths, e.g. a ``ValidationException`` for a thinking
    type the model doesn't accept.
    """

    def __init__(self, message: str, code: str = "ValidationException"):
        self.message = message
        self.code = code
        self.calls = 0

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        self.calls += 1
        raise ClientError({"Error": {"Code": self.code, "Message": self.message}}, "InvokeModel")
