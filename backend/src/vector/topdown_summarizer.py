"""Top-down code summarization using LLM for business descriptions."""

# Set this BEFORE any imports that might load tokenizers
import os

os.environ["TOKENIZERS_PARALLELISM"] = "false"

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, Any, Set

from ..service.codekb_service import CodeKBService
from ..core.logging_utils import format_payload
from ..core.prompt_spotlighting import build_prompt
from ..core.bedrock_guardrail import GuardrailTarget, get_guardrail_kwargs
from ..core.circuit_breaker import CircuitBreaker
from ..core.language_directive import LanguageDirective, resolve_language_directive
from ..core.llm_settings import (
    Workload,
    anthropic_request_fields,
    max_tokens_error,
    settings_error_from_bedrock,
)
from ..core.llm_settings import resolve as resolve_llm_settings
from ..core.token_usage import (
    TokenUsage,
    from_anthropic_invoke,
    from_openai_invoke,
    workload_record,
)

logger = logging.getLogger(__name__)


class TopDownSummarizer:
    """Executes top-down summarization algorithm using LLM.

    Delegates all data access to CodeKBService — no shared globals needed.
    """

    # Type alias for the progress callback: (done, total) -> None
    ProgressCallback = Callable[[int, int], None]
    _MAX_BEDROCK_API_ATTEMPTS = 2

    def __init__(
        self,
        space_path: str,
        space_id: str,
        codekb_service: CodeKBService | None = None,
        on_progress: ProgressCallback | None = None,
        omit_low_business_relevance: bool = False,
        language_directive: LanguageDirective | None = None,
    ):
        self.space_path = space_path
        self.space_id = space_id
        self.codekb_service = codekb_service or CodeKBService.instance()
        self.on_progress = on_progress
        self.omit_low_business_relevance = omit_low_business_relevance
        self.language_directive = language_directive or resolve_language_directive(None)
        self.stats = {
            "total_analyzed": 0,
            "total_cached": 0,
            "depths_processed": 0,
            "skipped_low_business_relevance": 0,
        }
        self._indexed_cache: Set[str] = set()
        self._description_cache: Dict[str, str] = {}
        self._bedrock_client = None
        # Accumulates Bedrock LLM token consumption across every _call_bedrock
        # invocation in this summarization run (per-model). Surfaced via
        # summarize_all() -> stats["token_usage"] and persisted as the
        # space_creation / space_refresh cost record.
        self.token_usage = TokenUsage()

        from ..core.config import require_config

        self._llm = resolve_llm_settings(Workload.SUMMARIZATION)
        if not self._llm.model_id:
            raise ValueError("No model configured for summarization (CODEKB_BEDROCK_MODEL_ID)")
        self._bedrock_region = require_config("llm.providers.bedrock.region")
        self._bedrock_read_timeout = int(require_config("llm.providers.bedrock.read_timeout"))
        self._max_input_chars = int(require_config("summarization.max_input_chars"))
        self._call_graph_depth = int(require_config("analysis.call_graph_depth"))
        # Run-scoped cost/failure guardrails. token_budget is a plain ceiling
        # (0 disables); the breaker trips the run after repeated Bedrock failures.
        self._token_budget = int(require_config("summarization.token_budget"))
        self._circuit_breaker = CircuitBreaker(
            int(require_config("summarization.circuit_breaker_threshold"))
        )
        # First guardrail reason observed by _should_stop(); surfaced as
        # stats["stopped_early"]. None == ran to completion.
        self._stop_reason: str | None = None
        # Set when Bedrock rejects the configured LLM settings (e.g. a thinking
        # type the model doesn't accept). Every later call would fail the same
        # way, so _should_stop() ends the run instead of waiting for the breaker.
        self._settings_error: Exception | None = None

        # Batching: functions at depths <= this threshold are batched into
        # a single LLM call instead of one-call-per-function.
        self._batch_depth_threshold = int(require_config("summarization.batch_depth_threshold"))
        self._batch_size = int(require_config("summarization.batch_size"))
        # Max LLM attempts per batch: the first call plus re-requests for any
        # functions the model omitted. 1 == no reconciliation (old behavior).
        self._batch_reconcile_max_rounds = max(
            1, int(require_config("summarization.batch_reconcile_max_rounds"))
        )

        # Concurrency for LLM summarization. Default 1 == previous serial
        # behavior (safe rollback). Parallelism is applied ONLY within a depth
        # level; the depth-by-depth barrier is preserved so a function always
        # sees its (deeper) callees' descriptions before it runs — identical
        # inputs/outputs, just concurrent.
        self._concurrency = max(1, int(require_config("summarization.concurrency")))
        # Guards the shared description/indexed caches and stats counters when
        # running with concurrency > 1.
        self._lock = threading.Lock()
        # func_ids currently being indexed by some worker (prevents duplicate
        # indexing of the same component across concurrent tasks).
        self._claimed: Set[str] = set()

    def _eligible_components(
        self, component_ids: list[str], *, count_skipped: bool = True
    ) -> list[str]:
        """Apply optional business relevance filtering before LLM summarization."""
        if not self.omit_low_business_relevance:
            return component_ids

        eligible: list[str] = []
        skipped = 0
        for component_id in component_ids:
            tier = self.codekb_service.get_business_relevance_tier(self.space_id, component_id)
            if tier == "low":
                skipped += 1
                continue
            eligible.append(component_id)

        if skipped and count_skipped:
            self.stats["skipped_low_business_relevance"] += skipped
            logger.info(
                f"Skipped {skipped} low business relevance component(s) before summarization"
            )
        return eligible

    def _components_at_depth(self, depth: int, targets: set[str] | None) -> list[str]:
        """Component IDs at a call depth, narrowed to targets when given."""
        ids = self.codekb_service.get_components_by_depth(self.space_id, depth)
        return ids if targets is None else [c for c in ids if c in targets]

    def _get_bedrock_client(self):
        """Get or create the shared Bedrock client (thread-safe singleton).

        Concurrent ``boto3.client()`` creation on the default session is not
        thread-safe, so guard the one-time lazy creation with the shared lock —
        otherwise every worker races to build the client on the first depth.
        The lock wraps only the creation (a microsecond check, once); the
        invoke_model calls and reuse of an already-built client happen outside
        the lock, so parallelism is unaffected.
        """
        with self._lock:
            if self._bedrock_client is None:
                import boto3
                from botocore.config import Config

                config = Config(
                    read_timeout=self._bedrock_read_timeout,
                    retries={"max_attempts": self._MAX_BEDROCK_API_ATTEMPTS, "mode": "adaptive"},
                    # Size the connection pool to the worker count so every
                    # concurrent worker gets a connection. botocore defaults
                    # to 10, which would otherwise cap real parallelism at
                    # ~10 and churn connections ("Connection pool is full").
                    max_pool_connections=max(self._concurrency, 10),
                )
                self._bedrock_client = boto3.client(
                    "bedrock-runtime", region_name=self._bedrock_region, config=config
                )
        return self._bedrock_client

    def _bounded_input(self, input_text: str) -> str:
        """Cap untrusted LLM input length (cost/DoS guard).

        Truncates oversized input with a warning rather than failing, so a
        pathologically large call graph cannot drive unbounded token cost.
        A non-positive cap disables the check.
        """
        if 0 < self._max_input_chars < len(input_text):
            logger.warning(
                f"LLM input truncated from {len(input_text):,} to "
                f"{self._max_input_chars:,} chars (summarization.max_input_chars)"
            )
            return input_text[: self._max_input_chars]
        return input_text

    def _call_bedrock(self, prompt: str, input_text: str) -> str:
        """Call Bedrock LLM with prompt and input."""
        import time

        model_id = self._llm.model_id
        full_prompt = build_prompt(prompt, self._bounded_input(input_text))
        request = {
            "anthropic_version": "bedrock-2023-05-31",
            **anthropic_request_fields(self._llm),
            "messages": [{"role": "user", "content": full_prompt}],
        }
        body = json.dumps(request)
        logger.debug(
            f"bedrock.invoke_model: model={model_id}, "
            f"region={self._bedrock_region}, prompt_len={len(full_prompt):,}, "
            f"max_tokens={request['max_tokens']}, thinking={self._llm.thinking}, "
            f"effort={self._llm.effort}"
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "bedrock.invoke_model full request:\n%s",
                format_payload(
                    {"modelId": model_id, "region": self._bedrock_region, "body": request}
                ),
            )
        t0 = time.perf_counter()
        try:
            response = self._get_bedrock_client().invoke_model(
                modelId=model_id,
                body=body,
                **get_guardrail_kwargs(GuardrailTarget.BOTO3_INVOKE_MODEL),
            )
            response_body_str = response["body"].read()
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.debug(
                f"bedrock.invoke_model ok: model={model_id}, "
                f"response_bytes={len(response_body_str):,}, elapsed={elapsed_ms:.0f}ms"
            )
            response_body = json.loads(response_body_str, strict=False)
            # Record authoritative token usage from the response (best-effort —
            # never let accounting break summarization). Guarded by the lock
            # because summarize_all() may invoke this from multiple worker
            # threads when summarization.concurrency > 1, and TokenUsage.add()
            # does a non-atomic read-modify-write on the shared by_model dict.
            try:
                if "qwen" in model_id.lower():
                    usage_delta = from_openai_invoke(response_body, model_id)
                else:
                    usage_delta = from_anthropic_invoke(response_body, model_id)
                with self._lock:
                    self.token_usage.merge(usage_delta)
            except Exception:  # pragma: no cover - defensive
                logger.debug("token usage capture failed", exc_info=True)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "bedrock.invoke_model full response:\n%s",
                    format_payload(response_body),
                )

            # InvokeModel API has different response formats based on the model
            if "qwen" in model_id.lower():
                text = response_body["choices"][0]["message"]["content"]
            else:
                text = None
                for block in response_body["content"]:
                    if block.get("type") == "text":
                        text = block["text"]
                        break
                if text is None:
                    if response_body.get("stop_reason") == "max_tokens":
                        raise max_tokens_error(self._llm)
                    raise ValueError("No text content block in Bedrock response")
            self._circuit_breaker.record_success()
            return text
        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            settings_error = settings_error_from_bedrock(e, self._llm)
            if settings_error is not None:
                # Not a provider failure: every call fails the same way until the
                # setting is fixed. Stop the run instead of tripping the breaker.
                with self._lock:
                    first = self._settings_error is None
                    if first:
                        self._settings_error = settings_error
                if first:
                    logger.error(str(settings_error))
                raise RuntimeError(f"Bedrock call failed: {settings_error}") from e
            self._circuit_breaker.record_failure()
            logger.error(
                f"bedrock.invoke_model failed after {elapsed_ms:.0f}ms: "
                f"model={model_id}, region={self._bedrock_region}: {e}",
                exc_info=True,
            )
            raise RuntimeError(f"Bedrock call failed: {e}")

    def _replace_source_with_descriptions(self, call_graph_text: str) -> str:
        """Replace SOURCE blocks with CACHED DESCRIPTION for already-analyzed functions."""
        import re

        lines = call_graph_text.split("\n")
        result = []
        i = 0

        while i < len(lines):
            line = lines[i]
            result.append(line)

            match = re.match(r"^(.+?)\s+\((function|method)\)", line)
            if match:
                func_id = match.group(1).strip()

                if i + 1 < len(lines) and lines[i + 1].strip() == "SOURCE:":
                    if func_id in self._description_cache:
                        result.append(lines[i + 1].replace("SOURCE:", "CACHED DESCRIPTION:"))
                        i += 2
                        # Source lines are always indented, so only the "" separator ends the block.
                        while i < len(lines) and lines[i] != "":
                            i += 1
                        result.append(f"  {self._description_cache[func_id]}")
                        continue

            i += 1

        return "\n".join(result)

    @staticmethod
    def _recover_truncated_json(json_str: str) -> Dict[str, str] | None:
        """Recover every well-formed "id":"desc" pair, skipping any broken one.

        The response is a flat {"id": "description"} object. It can be invalid two
        ways: end-truncation (cut mid-value when the model hits max_tokens) or a
        malformed pair in the middle (an unescaped quote inside a description
        closes the string early). A single whole-object parse loses everything in
        both cases.

        Instead, match each "key": "value" pair and parse it on its own, so one
        bad description never discards the rest: clean pairs are recovered intact
        and a broken pair contributes its text up to the stray quote. Spurious
        keys (quoted text inside prose that happens to look like a pair) are
        harmless — index_component_description rejects any key not in the catalog.
        """
        if not json_str or "{" not in json_str:
            return None

        import re

        # "key": "value", allowing escaped sequences; a value stops at the first
        # unescaped quote, which is exactly where a malformed description breaks.
        recovered: Dict[str, str] = {}
        for match in re.finditer(
            r'"([^"\\]*(?:\\.[^"\\]*)*)"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
            json_str,
        ):
            try:
                recovered.update(json.loads("{" + match.group(0) + "}", strict=False))
            except json.JSONDecodeError:
                continue

        return recovered or None

    def _generate_descriptions(self, call_graph_data: str) -> Dict[str, str]:
        """Generate business descriptions for ALL functions in the call graph using LLM."""
        prompt = """Analyze the function call graph below and generate business descriptions.

Functions are marked as either:
- SOURCE: New function with code (ANALYZE AND DESCRIBE THIS)
- CACHED DESCRIPTION: Already analyzed (USE FOR CONTEXT ONLY, DO NOT INCLUDE IN OUTPUT)

For EACH function with SOURCE code, write a 3-5 sentence description covering purpose, rationale, flow, context, and impact.
Use business language from the caller's perspective.

IMPORTANT:
1. ONLY return descriptions for functions with SOURCE code
2. DO NOT return descriptions for functions with CACHED DESCRIPTION
3. Use the EXACT FULL function ID as shown in the call graph (e.g., "project::module::ClassName.method_name")
4. Return ONLY valid JSON. No explanations, no markdown, no extra text
5. Format: {"full_function_id": "description", "another_full_id": "description"}
6. Description text must be plain single-line text: do NOT use double-quote characters ("), newlines, tabs, or backslashes inside a description.

Start your response with { and end with }"""

        # Description text is human-readable; JSON keys (function IDs) are unaffected.
        language_instruction = self.language_directive.instruction_line()
        if language_instruction:
            prompt += "\n\n" + language_instruction

        response = self._call_bedrock(prompt, call_graph_data)
        try:
            json_str = response.strip()
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()
            if json_str.startswith(">"):
                lines = json_str.split("\n")
                json_str = "\n".join(lines[1:]).strip()
            start = json_str.find("{")
            end = json_str.rfind("}")
            if start != -1 and end != -1:
                json_str = json_str[start : end + 1]
            return json.loads(json_str, strict=False)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM response as JSON: {e}")
            logger.warning(f"Response preview: {response[:500]}...")
            recovered = self._recover_truncated_json(json_str)
            if recovered:
                logger.warning(
                    f"Recovered {len(recovered)} descriptions from truncated JSON "
                    f"(some may be missing)"
                )
                return recovered
            logger.error("Could not recover any descriptions from truncated response")
            return {}
        except Exception as e:
            logger.error(f"Unexpected error parsing LLM response: {e}")
            logger.error(f"Response preview: {response[:500]}...")
            return {}

    def _get_call_graph_text(self, component_id: str) -> str:
        """Get call graph with source as formatted text via the service."""
        text, _ids = self._call_graph_text_and_ids(component_id)
        return text

    def _call_graph_text_and_ids(self, component_id: str) -> tuple[str, list[str]]:
        """Formatted call-graph text plus the summarizable component IDs in it.

        The IDs are collected during the same traversal _format_call_graph uses
        to render the text, filtered to function/method nodes. Call graphs also
        contain non-callable callees (e.g. field references, type: variable);
        those have no source to describe and index_component_description rejects
        them, so they must not become reconcile targets — otherwise recovery
        would re-request IDs that can never be indexed.
        """
        call_graph = self.codekb_service.get_call_graph_with_source(
            self.space_id, component_id, self._call_graph_depth
        )
        if call_graph is None:
            return f"Component '{component_id}' not found or is not a function/method.", []

        output = [f"Call Graph for: {component_id}", "=" * 60, ""]
        seen: set[str] = set()
        node_types: dict[str, str] = {}
        self._format_call_graph(call_graph, output, seen=seen, node_types=node_types)
        callable_ids = [
            cid for cid, ctype in node_types.items() if ctype in ("function", "method")
        ]
        return "\n".join(output), callable_ids

    @staticmethod
    def _format_call_graph(
        node: Dict[str, Any],
        output: list,
        indent: int = 0,
        seen: set = None,
        node_types: Dict[str, str] | None = None,
    ):
        """Format call graph in raw/compact format for LLM consumption.

        When ``node_types`` is provided, each rendered node's ``id -> type`` is
        recorded into it, so callers can identify which nodes are summarizable
        (function/method) versus non-callable callees (e.g. field references).
        """
        if seen is None:
            seen = set()
        func_id = node["id"]
        if func_id in seen:
            return
        seen.add(func_id)
        if node_types is not None:
            node_types[func_id] = node.get("type")
        prefix = "  " * indent
        output.append(f"{prefix}{node['id']} ({node['type']})")
        output.append(f"{prefix}SOURCE:")
        for line in node["source"].split("\n"):
            output.append(f"{prefix}  {line}")
        output.append("")
        for child in node.get("calls", []):
            TopDownSummarizer._format_call_graph(child, output, indent + 1, seen, node_types)

    def _is_cached(self, component_id: str) -> bool:
        """Check if component is in local cache."""
        return component_id in self._indexed_cache

    def _index_descriptions(self, descriptions: Dict[str, str]) -> int:
        """Index returned descriptions into the vector store, thread-safely.

        Reserves each func_id under the lock so concurrent workers never index
        the same component twice, performs the network index OUTSIDE the lock
        (so Bedrock/S3 I/O stays parallel), then commits the cache update under
        the lock. Returns the count freshly indexed.
        """
        freshly_indexed = 0
        for func_id, description in descriptions.items():
            with self._lock:
                if func_id in self._indexed_cache or func_id in self._claimed:
                    continue
                self._claimed.add(func_id)
            try:
                result = self.codekb_service.index_component_description(
                    self.space_id, func_id, description
                )
            except Exception as e:
                logger.error(f"Indexing failed for {func_id}: {e}")
                with self._lock:
                    self._claimed.discard(func_id)
                continue
            with self._lock:
                if result.startswith("Stored"):
                    self._indexed_cache.add(func_id)
                    self._description_cache[func_id] = description
                    freshly_indexed += 1
                else:
                    self._claimed.discard(func_id)
                    logger.info(f"     Skipped indexing: {result}")
        return freshly_indexed

    def _process_component_individual(self, comp_id: str) -> int:
        """Summarize one high-depth function with its full call graph.

        The graph presents the target plus its callees as SOURCE functions, and
        the prompt asks for a description of each. The model can omit some
        (truncated input, output cut at max_tokens, or a misnamed ID); this
        reconciles the returned keys against the SOURCE functions the graph asked
        for and re-requests the missing ones in batch_size chunks. Disabled when
        batch_reconcile_max_rounds is 1.

        Safe to call concurrently (shared state is mutated only via the locked
        _index_descriptions). Catches per-component errors so one failure never
        aborts the whole run. Returns the count freshly indexed.
        """
        try:
            start_time = time.time()
            call_graph_output, graph_ids = self._call_graph_text_and_ids(comp_id)
            logger.info(f"{comp_id}: graph built in {time.time() - start_time:.1f}s")
            optimized_input = self._replace_source_with_descriptions(call_graph_output)
            # Functions the prompt asks for = rendered nodes not already described
            # (cached ones are rewritten to CACHED DESCRIPTION, not SOURCE).
            with self._lock:
                expected = [fid for fid in graph_ids if fid not in self._description_cache]

            freshly = self._generate_and_index(optimized_input, comp_id)

            if self._batch_reconcile_max_rounds > 1:
                with self._lock:
                    missing = [fid for fid in expected if fid not in self._indexed_cache]
                if missing:
                    logger.info(
                        f"{comp_id}: {len(missing)}/{len(expected)} SOURCE function(s) "
                        f"not returned; re-requesting them"
                    )
                    for i in range(0, len(missing), self._batch_size):
                        if self._should_stop():
                            break
                        freshly += self._reconcile(
                            missing[i : i + self._batch_size], f"recover {comp_id}"
                        )
            return freshly
        except Exception as e:
            logger.error(f"Summarization failed for {comp_id}: {e}", exc_info=True)
            return 0

    def _should_stop(self) -> str | None:
        """Return a value-free reason code to abort the run, or None to continue.

        Checked at the dispatch loops (not inside the LLM call) so a guardrail
        can't be swallowed by a per-item error handler. First reason wins:
        recorded in _stop_reason (surfaced as stats["stopped_early"]) and logged
        once with detail — the code stays backend-side, numbers never leave logs.
        """
        if self._stop_reason:
            return self._stop_reason
        reason: str | None = None
        detail: str | None = None
        if self._settings_error is not None:
            reason = "invalid_llm_settings"
            detail = "Bedrock rejected the configured LLM settings (see the error above)"
        elif self._circuit_breaker.is_open:
            reason = "circuit_breaker_open"
            detail = "circuit breaker open (repeated Bedrock failures)"
        elif self._token_budget > 0:
            used = self.token_usage.totals()["total"]
            if used >= self._token_budget:
                reason = "token_budget_reached"
                detail = f"token budget of {self._token_budget:,} reached ({used:,} used)"
        if reason:
            self._stop_reason = reason
            logger.warning(f"Stopping summarization early: {detail}")
        return reason

    def _build_batch_input(self, comp_ids: list[str]) -> str:
        """Build a combined input for a batch of functions (source only, no call graphs)."""
        sections = []
        for comp_id in comp_ids:
            call_graph_text = self._get_call_graph_text(comp_id)
            optimized = self._replace_source_with_descriptions(call_graph_text)
            sections.append(optimized)
        return "\n\n".join(sections)

    def _process_batch(self, comp_ids: list[str], depth: int) -> tuple[int, int]:
        """Summarize a batch, re-requesting any functions the model omits.

        Returns (freshly_indexed, already_cached). Safe to call concurrently.
        """
        return self._reconcile(comp_ids, f"batch depth {depth}"), 0

    def _generate_and_index(self, llm_input: str, label: str) -> int:
        """One summarization call: describe llm_input and index the result.

        Centralizes the generate -> log -> index step shared by every
        summarization path (batch, individual, and reconcile re-requests).
        Returns the count freshly indexed; a parse/empty response indexes
        nothing and returns 0.
        """
        llm_start = time.time()
        descriptions = self._generate_descriptions(llm_input)
        llm_time = time.time() - llm_start
        logger.info(
            f"{label}: llm={llm_time:.1f}s, input={len(llm_input)} chars, "
            f"returned={len(descriptions)} description(s)"
        )
        if not descriptions:
            logger.warning(f"No descriptions returned for {label}")
            return 0
        return self._index_descriptions(descriptions)

    def _reconcile(self, target_ids: list[str], label: str) -> int:
        """Describe target_ids, re-requesting any the model omits, bounded.

        The model is asked to describe every function, but it can return fewer
        than requested: input truncation (max_input_chars) drops the tail of a
        large call, the JSON output can be cut at max_tokens, or the model omits
        or misnames some IDs. After each call this reconciles the returned keys
        against target_ids: any target not yet in the indexed cache is
        re-requested. Re-requests shrink the input (callees indexed in earlier
        rounds come back as CACHED DESCRIPTION), so the loop converges. It is
        bounded by batch_reconcile_max_rounds so a function the model never
        returns cannot spin forever.

        Safe to call concurrently; per-round errors are caught so one bad round
        never aborts the run. Returns the count freshly indexed.
        """
        remaining = list(target_ids)
        total_freshly = 0

        for round_num in range(1, self._batch_reconcile_max_rounds + 1):
            if self._should_stop():
                break
            try:
                llm_input = self._build_batch_input(remaining)
                total_freshly += self._generate_and_index(
                    llm_input, f"{label} round {round_num}/{self._batch_reconcile_max_rounds}"
                )
            except Exception as e:
                logger.error(
                    f"Summarization failed ({label} round {round_num}): {e}", exc_info=True
                )

            # A requested function is done once it is in the indexed cache —
            # indexed this round, or by a concurrent worker as a callee.
            with self._lock:
                remaining = [c for c in remaining if c not in self._indexed_cache]
            if not remaining:
                break
            logger.info(
                f"{label}: re-requesting {len(remaining)} function(s) the model did not return"
            )

        if remaining:
            logger.warning(
                f"{label}: left {len(remaining)} function(s) unsummarized after "
                f"{self._batch_reconcile_max_rounds} round(s): "
                f"{remaining[:10]}{'...' if len(remaining) > 10 else ''}"
            )
        return total_freshly

    async def summarize_all(
        self,
        targets: set[str] | None = None,
        prior_descriptions: dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        """Execute complete top-down summarization.

        High-depth functions (depth > batch_depth_threshold) are processed
        individually with their full call graphs. Low-depth functions that
        aren't already cached are batched into groups for a single LLM call.

        Args:
            targets: Summarize only these component IDs; refresh passes its
                changed functions and their stale callers. None summarizes all.
            prior_descriptions: Stored descriptions of functions not summarized
                this run, so prompts show them in place of their source.

        Returns:
            Summary statistics
        """
        codekb_service = self.codekb_service
        sid = self.space_id
        if prior_descriptions:
            self._description_cache.update(prior_descriptions)

        # Step 1: Get plan from service
        max_depth = codekb_service.get_max_depth(sid)
        all_call_depths = codekb_service.get_call_depths(sid)
        candidates = list(all_call_depths) if targets is None else [
            c for c in all_call_depths if c in targets
        ]
        total_funcs = len(self._eligible_components(candidates, count_skipped=False))
        functions_done = 0
        logger.info(
            f"Processing {total_funcs} functions across {max_depth + 1} depths "
            f"(batch threshold={self._batch_depth_threshold}, batch size={self._batch_size}, "
            f"omit low business relevance={self.omit_low_business_relevance})"
        )

        # Report initial progress
        if self.on_progress:
            self.on_progress(0, total_funcs)

        # Step 2: Process depths above the batch threshold (individual call graphs)
        for depth in range(max_depth, self._batch_depth_threshold, -1):
            if self._should_stop():
                break
            components_at_depth = self._eligible_components(
                self._components_at_depth(depth, targets)
            )
            logger.info(f"Depth {depth}: {len(components_at_depth)} functions")

            # Split cached (already done) from uncached; cached just advance progress.
            depth_cached = sum(1 for c in components_at_depth if self._is_cached(c))
            uncached = [c for c in components_at_depth if not self._is_cached(c)]
            functions_done += depth_cached
            if self.on_progress:
                self.on_progress(functions_done, total_funcs)

            depth_analyzed = 0
            if self._concurrency > 1 and len(uncached) > 1:
                # Parallelize within this depth only (callees are deeper and
                # already summarized → identical inputs, just concurrent).
                with ThreadPoolExecutor(max_workers=self._concurrency) as ex:
                    futures = [
                        ex.submit(self._process_component_individual, comp_id)
                        for comp_id in uncached
                    ]
                    for fut in as_completed(futures):
                        freshly = fut.result()
                        with self._lock:
                            depth_analyzed += freshly
                            functions_done += 1
                            done_snapshot = functions_done
                        if self.on_progress:
                            self.on_progress(done_snapshot, total_funcs)
                        if self._should_stop():
                            for f in futures:
                                f.cancel()  # drop not-yet-started work in this depth
                            break
            else:
                for comp_id in uncached:
                    if self._should_stop():
                        break
                    depth_analyzed += self._process_component_individual(comp_id)
                    functions_done += 1
                    if self.on_progress:
                        self.on_progress(functions_done, total_funcs)

            self.stats["total_analyzed"] += depth_analyzed
            self.stats["total_cached"] += depth_cached
            self.stats["depths_processed"] += 1
            logger.info(
                f"Depth {depth} complete: {depth_analyzed} freshly indexed, {depth_cached} skipped"
            )

        # Step 3: Process low-depth functions in batches
        for depth in range(self._batch_depth_threshold, -1, -1):
            if self._should_stop():
                break
            components_at_depth = self._eligible_components(
                self._components_at_depth(depth, targets)
            )
            logger.info(f"Depth {depth} (batched): {len(components_at_depth)} functions")

            # Separate cached from uncached
            uncached = []
            depth_cached = 0
            for comp_id in components_at_depth:
                if self._is_cached(comp_id):
                    depth_cached += 1
                    functions_done += 1
                    if self.on_progress:
                        self.on_progress(functions_done, total_funcs)
                else:
                    uncached.append(comp_id)

            logger.info(
                f"Depth {depth}: {depth_cached} already cached, "
                f"{len(uncached)} to process in batches of {self._batch_size}"
            )

            depth_analyzed = 0

            # Process uncached in batches
            batches = [
                uncached[i : i + self._batch_size]
                for i in range(0, len(uncached), self._batch_size)
            ]
            total_batches = len(batches)

            if self._concurrency > 1 and total_batches > 1:
                with ThreadPoolExecutor(max_workers=self._concurrency) as ex:
                    futures = {ex.submit(self._process_batch, b, depth): b for b in batches}
                    for fut in as_completed(futures):
                        freshly_indexed, _ = fut.result()
                        b = futures[fut]
                        with self._lock:
                            depth_analyzed += freshly_indexed
                            functions_done += len(b)
                            done_snapshot = functions_done
                        if self.on_progress:
                            self.on_progress(done_snapshot, total_funcs)
                        if self._should_stop():
                            for f in futures:
                                f.cancel()  # drop not-yet-started batches in this depth
                            break
            else:
                for idx, batch in enumerate(batches, 1):
                    if self._should_stop():
                        break
                    logger.info(
                        f"Depth {depth} batch {idx}/{total_batches} ({len(batch)} functions)"
                    )
                    freshly_indexed, _ = self._process_batch(batch, depth)
                    depth_analyzed += freshly_indexed
                    functions_done += len(batch)
                    if self.on_progress:
                        self.on_progress(functions_done, total_funcs)

            self.stats["total_analyzed"] += depth_analyzed
            self.stats["total_cached"] += depth_cached
            self.stats["depths_processed"] += 1
            logger.info(
                f"Depth {depth} complete: {depth_analyzed} freshly indexed, {depth_cached} skipped"
            )

        logger.info(
            f"Summarization complete: {self.stats['total_analyzed']} freshly indexed, "
            f"{self.stats['total_cached']} skipped (already cached), "
            f"{self.stats['skipped_low_business_relevance']} skipped (low business relevance), "
            f"{len(self._indexed_cache)} total in cache"
        )
        self.stats["token_usage"] = self.token_usage.to_dict()
        self.stats["llm_workload"] = workload_record(self._llm, self.token_usage, self._bedrock_region)
        self.stats["region"] = self._bedrock_region
        # Distinguish a stopped-early run from a completed one for callers/UI.
        # None == ran to completion; otherwise the guardrail reason.
        self.stats["stopped_early"] = self._stop_reason
        logger.info(
            f"Summarization token usage: {self.token_usage.totals()} "
            f"across {len(self.token_usage.by_model)} model(s)"
        )
        return self.stats
