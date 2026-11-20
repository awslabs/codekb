"""Bedrock token-usage accounting.

CodeKB reports *token consumption* per Bedrock model — never a dollar amount.
Per-model rates vary by region and change over time, so users estimate cost
themselves via the official AWS Bedrock pricing page (see ``pricing_url``).

This module provides:

- :class:`TokenUsage` — a serializable, mergeable per-model token accumulator.
- Extractors that pull authoritative token counts out of the LLM-inference
  shapes the codebase produces:
    * raw Anthropic ``invoke_model`` responses (top-down summarizer),
    * OpenAI-compatible ``invoke_model`` responses — Qwen and similar models
      (top-down summarizer),
    * Strands ``AgentResult.metrics.accumulated_usage`` (Hopper chat, Space
      Overview generation).
- :func:`pricing_url` — the configured AWS Bedrock pricing link to surface in
  API responses and the UI next to any token display.

Embedding tokens (Amazon Titan via the S3 Vectors client) are intentionally
NOT tracked — their cost is negligible next to LLM inference.

Operation tags (use the OP_* constants) classify where usage was incurred so it
can be aggregated per space and per operation:

    space_creation  — initial analysis + AI summarization of a new space
    space_refresh   — delta re-summarization after a git-diff refresh
    space_overview  — Space Overview document generation (initial or manual regen)
    agent_chat      — Hopper agent chat turn
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .config import get_config

# ── operation tags ─────────────────────────────────────────────────────────

OP_SPACE_CREATION = "space_creation"
OP_SPACE_REFRESH = "space_refresh"
OP_SPACE_OVERVIEW = "space_overview"
OP_AGENT_CHAT = "agent_chat"


def pricing_url() -> str:
    """Return the configured AWS Bedrock pricing page URL.

    Surface this next to every token display so users can estimate cost
    themselves — CodeKB never computes dollar amounts.
    """
    return get_config(
        "pricing.bedrock_pricing_url",
        "https://aws.amazon.com/bedrock/pricing/",
    )


@dataclass
class _ModelTokens:
    """Generation token counts for a single model.

    ``thinking`` is the part of ``output`` the model spent thinking. It is
    already counted in ``output`` (thinking is billed as output), so it is
    never added to totals. Serialized only when non-zero, so records from
    models and settings without thinking keep their original shape.
    """

    input: int = 0
    output: int = 0
    thinking: int = 0

    def to_dict(self) -> Dict[str, int]:
        data = {"input": self.input, "output": self.output}
        if self.thinking:
            data["thinking"] = self.thinking
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "_ModelTokens":
        return cls(
            input=int(data.get("input", 0) or 0),
            output=int(data.get("output", 0) or 0),
            thinking=int(data.get("thinking", 0) or 0),
        )


@dataclass
class TokenUsage:
    """Mergeable, serializable per-model LLM token accumulator.

    Tracks generation token consumption broken down by Bedrock model id.
    Designed to be:

    - accumulated incrementally at each LLM call site (``add``),
    - merged across call sites or across operations (``merge``),
    - serialized into ``status.json`` (``to_dict``),
    - rehydrated from persisted state (``from_dict``).

    The serialized shape is::

        {
          "by_model": {
            "<model_id>": {"input": N, "output": N, "thinking": N},
            ...
          },
          "totals": {"input": N, "output": N, "total": N, "thinking": N}
        }

    ``thinking`` appears only when non-zero and is a subset of ``output``.
    ``totals`` is derived and included for convenience of UI/consumers; it is
    ignored on :meth:`from_dict` and always recomputed.
    """

    by_model: Dict[str, _ModelTokens] = field(default_factory=dict)

    # ── mutation ────────────────────────────────────────────────────────────

    def add(
        self,
        model_id: str,
        *,
        input: int = 0,
        output: int = 0,
        thinking: int = 0,
    ) -> "TokenUsage":
        """Add token counts for a model. Returns self for chaining.

        Negative or missing values are coerced to 0. Unknown ``model_id`` is
        recorded as-is so usage is never silently dropped. ``thinking`` is the
        share of ``output`` spent thinking, not an addition to it.
        """
        if not model_id:
            model_id = "unknown"
        bucket = self.by_model.setdefault(model_id, _ModelTokens())
        bucket.input += max(0, int(input or 0))
        bucket.output += max(0, int(output or 0))
        bucket.thinking += max(0, int(thinking or 0))
        return self

    def merge(self, other: Optional["TokenUsage"]) -> "TokenUsage":
        """Merge another TokenUsage into this one. Returns self for chaining."""
        if not other:
            return self
        for model_id, tokens in other.by_model.items():
            self.add(model_id, input=tokens.input, output=tokens.output, thinking=tokens.thinking)
        return self

    # ── queries ───────────────────────────────────────────────────────────

    def is_empty(self) -> bool:
        """True if no tokens have been recorded."""
        return not any(t.input or t.output for t in self.by_model.values())

    def totals(self) -> Dict[str, int]:
        """Return summed token counts across all models."""
        inp = sum(t.input for t in self.by_model.values())
        out = sum(t.output for t in self.by_model.values())
        totals = {"input": inp, "output": out, "total": inp + out}
        thinking = sum(t.thinking for t in self.by_model.values())
        if thinking:
            totals["thinking"] = thinking
        return totals

    # ── serialization ───────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-friendly dict (for status.json)."""
        return {
            "by_model": {mid: t.to_dict() for mid, t in self.by_model.items()},
            "totals": self.totals(),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "TokenUsage":
        """Rehydrate from a previously serialized dict. ``totals`` is ignored."""
        usage = cls()
        if not data:
            return usage
        by_model = data.get("by_model", {}) or {}
        for model_id, tokens in by_model.items():
            usage.by_model[model_id] = _ModelTokens.from_dict(tokens or {})
        return usage


# ── extractors ──────────────────────────────────────────────────────────────
#
# Each extractor is defensive: token reporting must never break a real
# operation, so malformed/absent usage data yields zero rather than raising.


def from_anthropic_invoke(response_body: Dict[str, Any], model_id: str) -> TokenUsage:
    """Extract usage from a raw Anthropic ``invoke_model`` response body.

    Anthropic Claude on Bedrock returns ``{"usage": {"input_tokens": N,
    "output_tokens": N}}`` in the parsed response body. Models that think also
    report ``usage.output_tokens_details.thinking_tokens``, the share of
    ``output_tokens`` spent thinking.
    """
    usage = TokenUsage()
    u = (response_body or {}).get("usage", {}) or {}
    details = u.get("output_tokens_details", {}) or {}
    usage.add(
        model_id,
        input=u.get("input_tokens", 0),
        output=u.get("output_tokens", 0),
        thinking=details.get("thinking_tokens", 0) if isinstance(details, dict) else 0,
    )
    return usage


def from_openai_invoke(response_body: Dict[str, Any], model_id: str) -> TokenUsage:
    """Extract usage from an OpenAI-compatible ``invoke_model`` response body.

    Qwen (and other OpenAI-compatible models) on Bedrock return::

        {"usage": {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}}

    This extractor reads those keys and falls back to zero when absent.
    """
    usage = TokenUsage()
    u = (response_body or {}).get("usage", {}) or {}
    usage.add(
        model_id,
        input=u.get("prompt_tokens", 0),
        output=u.get("completion_tokens", 0),
    )
    return usage


def workload_record(
    settings: Any, usage: TokenUsage, region: Optional[str] = None
) -> Dict[str, Any]:
    """One workload's share of an operation: the settings it ran with and its tokens.

    ``settings`` is an ``llm_settings.LlmSettings``. The model, thinking,
    effort and region are snapshotted here because the configuration can change
    later; the Token consumption panel must show what this run actually used.
    ``region`` is the Bedrock region the calls were sent to (the source region
    for a cross-Region inference profile, which is what pricing follows).
    ``thinking`` and ``effort`` are None when nothing was sent.
    """
    from .llm_settings import MODEL_DEFAULT

    totals = usage.totals()
    record: Dict[str, Any] = {
        "workload": settings.workload.value,
        "model_id": settings.model_id,
        "thinking": None if settings.thinking == MODEL_DEFAULT else settings.thinking,
        "effort": settings.effort,
        **({"region": region} if region else {}),
        "input": totals["input"],
        "output": totals["output"],
        "total": totals["total"],
    }
    if totals.get("thinking"):
        record["thinking_tokens"] = totals["thinking"]
    return record


def operation_usage(parts: Any) -> Optional[Dict[str, Any]]:
    """Combine the workloads of one operation into its persisted usage record.

    Each part is a dict carrying ``token_usage`` (a serialized TokenUsage) and
    ``llm_workload`` (a :func:`workload_record`), e.g. a summarizer's stats.
    The result keeps the existing ``by_model`` / ``totals`` shape, so older
    readers are unaffected, plus a ``workloads`` list with each workload's
    settings and tokens. Returns None when there's nothing to record.
    """
    usage = TokenUsage()
    workloads = []
    for part in parts:
        if not part:
            continue
        usage.merge(TokenUsage.from_dict(part.get("token_usage")))
        if part.get("llm_workload"):
            workloads.append(part["llm_workload"])
    if usage.is_empty() and not workloads:
        return None
    record = usage.to_dict()
    if workloads:
        record["workloads"] = workloads
    return record


def from_strands_metrics(result: Any, model_id: str) -> TokenUsage:
    """Extract usage from a Strands agent result.

    Reads ``result.metrics.accumulated_usage`` — a ``{"inputTokens",
    "outputTokens", "totalTokens"}`` mapping that already aggregates every model
    invocation across the agent's event loop (including all tool cycles), so it
    must not be combined with per-call counting for the same run.
    """
    usage = TokenUsage()
    metrics = getattr(result, "metrics", None)
    accumulated = getattr(metrics, "accumulated_usage", None)
    if not accumulated:
        return usage
    usage.add(
        model_id,
        input=accumulated.get("inputTokens", 0),
        output=accumulated.get("outputTokens", 0),
    )
    return usage
