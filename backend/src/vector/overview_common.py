"""Shared scaffolding for the Space and Hyperspace overview generators.

Both generators follow the same shape: assemble a context corpus, then run a
series of tool-less Bedrock LLM calls that each turn that corpus into one
markdown document. This module holds everything they share — Bedrock wiring,
token-usage capture, the single-document generation step (including the
deterministic Mermaid repair for ticket P525301586), and the prompt language
that is identical across every document — so the per-generator modules carry
only what differs: which documents they produce and how their corpus is built.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict

from strands import Agent
from strands.models.bedrock import BedrockModel

from ..core.bedrock_guardrail import GuardrailTarget, get_guardrail_kwargs
from ..core.config import require_config
from ..core.llm_settings import Workload, strands_model_kwargs
from ..core.llm_settings import resolve as resolve_llm_settings
from ..core.logging_utils import get_logger
from ..core.token_usage import TokenUsage, from_strands_metrics, workload_record
from .mermaid_utils import normalize_markdown_mermaid

logger = get_logger(__name__)

# Progress callback shared by both generators: on_progress(done, total).
ProgressCallback = Callable[[int, int], None]


# ── Shared prompt language ───────────────────────────────────────────────
# Centralized so every overview document — space or hyperspace — speaks with one
# voice and a single edit updates them all. Prompts reference these as
# ``string.Template`` placeholders ($NO_TOP_HEADING, $MERMAID_RULE, $OUTPUT_ONLY)
# and substitute them at module load.

# Tail of the "## Output Format —" line in every document prompt.
NO_TOP_HEADING = (
    "produce EXACTLY this Markdown structure (do NOT emit a top-level "
    "`# <title>` heading; the UI renders the document title):"
)

# The fix for ticket P525301586: Mermaid needs the diagram-type keyword first.
# One canonical rule, referenced by every prompt that emits a diagram.
MERMAID_RULE = (
    "- Mermaid syntax: the FIRST line inside every ```mermaid block MUST be the "
    "diagram-type declaration (e.g. `flowchart TB`, `graph LR`, `sequenceDiagram`). "
    "Do NOT put `accTitle`, `accDescr`, comments, YAML frontmatter, or blank lines "
    "before it — doing so is invalid Mermaid and fails to render. Omit "
    "`accTitle`/`accDescr` entirely.\n"
    "- Mermaid node labels: when a label contains anything other than letters, "
    "digits, and spaces — especially parentheses, brackets, or punctuation — wrap "
    "the label text in double quotes, e.g. write `KONG[\"API Gateway (Kong)\"]`, "
    "NOT `KONG[API Gateway (Kong)]`. Unquoted special characters in a label are "
    "invalid Mermaid and fail to render."
)

# Last rule line in every document prompt.
OUTPUT_ONLY = "- Output ONLY the markdown document, nothing else."


class OverviewGeneratorBase:
    """Common Bedrock wiring and single-document generation for overviews.

    Subclasses provide the documents to generate and the corpus/context, and
    call :meth:`_generate_document` once per document. Bedrock model config,
    token-usage accounting, Mermaid normalization, and per-document logging all
    live here.
    """

    def __init__(self, *, log_prefix: str) -> None:
        """
        Args:
            log_prefix: Bracketed tag prepended to this generator's log lines,
                e.g. ``"[space_overview]"``.
        """
        self._log_prefix = log_prefix

        self._llm = resolve_llm_settings(Workload.OVERVIEW)
        self._bedrock_model_id = self._llm.model_id
        if not self._bedrock_model_id:
            raise ValueError("No model configured for overview (CODEKB_BEDROCK_MODEL_ID)")
        self._bedrock_region = require_config("llm.providers.bedrock.region")
        # Accumulates Bedrock token consumption across every LLM call this
        # generator makes (exploration pass and all document generations).
        self.token_usage = TokenUsage()

    def _create_model(self) -> BedrockModel:
        """Create a Bedrock model instance."""
        model_kwargs: dict[str, Any] = strands_model_kwargs(self._llm)
        # Always-on secret/PII guardrail (see core/bedrock_guardrail). Empty unless
        # a guardrail id is configured, so local runs are unaffected.
        model_kwargs.update(get_guardrail_kwargs(GuardrailTarget.STRANDS_BEDROCK_MODEL))
        return BedrockModel(**model_kwargs)

    def _workload_record(self) -> Dict[str, Any]:
        """This generator's settings and tokens, for the operation's usage record."""
        return workload_record(self._llm, self.token_usage, self._bedrock_region)

    def _capture_usage(self, result: Any) -> None:
        """Merge Bedrock token usage from a Strands agent result (best-effort).

        ``accumulated_usage`` already aggregates every model invocation in the
        agent's event loop, so this is called once per ``agent(...)`` call.
        Accounting must never break generation.
        """
        try:
            self.token_usage.merge(from_strands_metrics(result, self._bedrock_model_id))
        except Exception:  # pragma: no cover - defensive
            logger.debug(f"{self._log_prefix} token usage capture failed", exc_info=True)

    def _generate_document(
        self,
        *,
        system_prompt: str,
        user_message: str,
        doc_key: str,
        doc_title: str,
        repair_context: Dict[str, Any],
    ) -> str:
        """Run one tool-less LLM call and return normalized markdown.

        Captures token usage, then deterministically repairs malformed Mermaid
        (accTitle/accDescr emitted before the diagram-type keyword — ticket
        P525301586) before returning, so every consumer gets valid diagrams. The
        repair warning doubles as telemetry: a non-zero count means the prompt
        rule did not hold and the model still produced bad ordering.

        Args:
            system_prompt: The document-specific generation prompt.
            user_message: The context-injected user turn (exploration report or
                synthesis corpus).
            doc_key: Document key, for logs/telemetry.
            doc_title: Human-readable document title, for logs.
            repair_context: Extra structured fields merged into the repair
                warning (e.g. ``{"space_id": ...}`` or ``{"hyperspace_id": ...}``).

        Returns:
            The generated markdown, with Mermaid blocks normalized.
        """
        logger.info(f"{self._log_prefix} Generating '{doc_title}'")
        start = time.time()

        model = self._create_model()
        agent = Agent(model=model, tools=[], system_prompt=system_prompt)
        result = agent(user_message)
        self._capture_usage(result)

        markdown = str(result)
        norm = normalize_markdown_mermaid(markdown)
        if norm.blocks_repaired:
            logger.warning(
                f"{self._log_prefix} Repaired {norm.blocks_repaired}/{norm.blocks_total} "
                f"malformed Mermaid block(s) in '{doc_key}'",
                extra={
                    "codekb_event": "mermaid_repair",
                    "doc_key": doc_key,
                    "mermaid_blocks_total": norm.blocks_total,
                    "mermaid_blocks_repaired": norm.blocks_repaired,
                    **repair_context,
                },
            )
        markdown = norm.markdown

        logger.info(
            f"{self._log_prefix} Generated '{doc_title}' in {time.time() - start:.1f}s "
            f"({len(markdown)} chars)"
        )
        return markdown
