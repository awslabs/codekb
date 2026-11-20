"""Hyperspace Overview documentation generator (summary-of-summaries).

A hyperspace is a named group of member spaces. Each member space may already
have a Space Overview (see ``space_overview_generator.py``): reverse engineered
markdown documents describing that space in isolation. This generator sits ON
TOP of that feature. It does NOT re-explore any codebase. Instead it reads the
already-generated per-space overview documents and asks an LLM to synthesize a
*system-level* view across all member spaces, with a specific focus on how the
spaces are connected to one another.

Design notes / deliberate limitations:
  * This is intentionally a summary-of-summaries. The connections it describes
    are INFERRED from each space's documentation (shared integration points,
    external services, request/message names, producer/consumer language), not
    resolved from a call graph. CodeKB's call graph is strictly per-space, so
    there is no verified cross-space edge to draw from. The prompts make this
    limitation explicit in the output and forbid fabrication.
  * The generator is pure: it receives the member data already assembled by the
    service (which owns S3 access) and returns markdown. No S3, no threads here.

Produces 2 markdown documents:
  1. System Overview       — what the whole system does, end to end.
  2. Space Interconnections — how the member spaces connect (the core deliverable).
"""

from __future__ import annotations

from datetime import datetime, timezone
from string import Template
from typing import Any, Dict, List, Optional

from ..core.logging_utils import get_logger
from .overview_common import (
    MERMAID_RULE,
    NO_TOP_HEADING,
    OUTPUT_ONLY,
    OverviewGeneratorBase,
    ProgressCallback,
)

logger = get_logger(__name__)

# ── Document definitions ─────────────────────────────────────────────────

HYPERSPACE_OVERVIEW_DOCUMENTS: List[Dict[str, str]] = [
    {
        "key": "system_overview",
        "title": "System Overview",
        "filename": "system_overview.md",
    },
    {
        "key": "space_interconnections",
        "title": "Space Interconnections",
        "filename": "space_interconnections.md",
    },
]

# Which per-space overview documents feed the synthesis. Business Overview
# carries external integrations and domain language; Application Architecture
# carries the Integration Points section (external APIs, data stores, third
# party services) — together these are the richest signal for inferring how
# spaces connect. Keeping the set small also bounds the prompt size for large
# hyperspaces.
MEMBER_DOC_KEYS_FOR_SYNTHESIS: List[str] = [
    "business_overview",
    "application_architecture",
]

_MEMBER_DOC_TITLES: Dict[str, str] = {
    "business_overview": "Business Overview",
    "application_architecture": "Application Architecture Overview",
    "technology_stack": "Technology Stack",
    "deployment_architecture": "Deployment Architecture",
}


# ── Document generation prompts (no tools, context-injected) ─────────────
# Rule/heading language shared with the Space overview prompts comes from
# overview_common via $-placeholders, substituted into _DOC_GENERATION_PROMPTS
# at module load.

_RAW_PROMPTS: Dict[str, str] = {
    "system_overview": """\
You are a principal engineer writing orientation documentation for a large
software system that is composed of several member "spaces" (each space is an
independently indexed group of packages/services). You are given the per-space
overview documents for every member space. Using ONLY that data, produce a
**System Overview** that explains what the WHOLE system does, end to end.

Focus on the system level, not on any single space in isolation.

## Output Format — $NO_TOP_HEADING

## System Purpose
[2-4 sentences: what this overall system does from a business/product standpoint,
synthesized across all member spaces.]

## Member Spaces at a Glance
| Space | Role in the System | Primary Domain |
|-------|--------------------|----------------|
| [space name] | [one-line role] | [domain area] |

(Include EVERY member space. For a space with no overview available, write its
role as "Unknown — no Space Overview generated" rather than guessing.)

## End-to-End Capabilities
[Describe the major end-to-end capabilities the system delivers and, for each,
which spaces collaborate to deliver it. Base collaboration claims on the
integration points and domain language in the space documents.]

## Domain Glossary
[Key cross-cutting domain terms that appear across multiple spaces, with brief
definitions.]

## RULES
- Base ALL claims strictly on the provided per-space documents. Do NOT invent
  capabilities, spaces, or relationships that the data does not support.
- Prefer plain business/system language over implementation detail.
$OUTPUT_ONLY
""",
    "space_interconnections": """\
You are a principal engineer mapping how the member spaces of a large system
connect to one another. You are given the per-space overview documents for every
member space. Your job is to infer and explain the connections BETWEEN spaces
and give the reader a high-level mental model of how the whole system is wired.

You infer connections from evidence in the per-space documents, such as:
  - Integration Points sections (external APIs, data stores, third-party services)
    where one space's stated dependency matches another space's stated capability.
  - Shared external services, databases, queues, or topics named by two spaces.
  - Matching request/response, message, event, or RPC names across spaces
    (one space describes calling/sending it; another describes handling/receiving it).
  - Producer/consumer or client/server language that lines up across two spaces.

## Output Format — $NO_TOP_HEADING

## How to Read This
[One short paragraph stating plainly that the connections below are INFERRED from
per-space documentation, NOT resolved from a verified call graph, and should be
verified against source before being relied on.]

## System Connection Diagram
```mermaid
graph LR
[A graph with one node per member space and one edge per inferred connection.
Label each edge with what flows across it (e.g. "checkout requests", "product
catalog reads"). Only draw edges you can support with evidence from the documents.]
```

## Inferred Connections
For each inferred connection, one subsection:
### [Source Space] → [Target Space]
- **What flows**: [what data/requests/events cross this boundary]
- **Evidence**: [which space(s) and which part of their docs this is inferred from —
  e.g. "backend space lists an outbound 'BeginCheckout' integration; EGS space
  describes a begin-checkout handler"]
- **Confidence**: [High | Medium | Low]

## Shared Infrastructure
[Data stores, external services, queues, or platforms referenced by more than one
space, and which spaces share each.]

## Isolated Spaces & Gaps
[Spaces with no clear inferred connection to others, and spaces for which no
Space Overview was available (so their connections could not be assessed).]

## RULES
- These connections are INFERRED from documentation, NOT a verified call graph.
  State this in "How to Read This" and never present an inference as a proven call.
- Base EVERY connection on evidence in the provided documents. Do NOT invent
  connections the data does not support. When in doubt, mark Confidence: Low or
  omit the edge.
- EXCLUDE test, mock, stub, and emulator components/clients from connection
  claims (e.g. names containing Test, Mock, Stub, Emulator, or .spec). A test
  double is not a real integration.
- Always cite which space(s) each inference is based on in the Evidence field.
$MERMAID_RULE
$OUTPUT_ONLY
""",
}

# Resolve the shared prompt fragments once at module load. Templates that do not
# use $MERMAID_RULE (e.g. system_overview) simply ignore the extra mapping.
_DOC_GENERATION_PROMPTS: Dict[str, str] = {
    key: Template(raw).substitute(
        NO_TOP_HEADING=NO_TOP_HEADING,
        MERMAID_RULE=MERMAID_RULE,
        OUTPUT_ONLY=OUTPUT_ONLY,
    )
    for key, raw in _RAW_PROMPTS.items()
}


class HyperspaceOverviewGenerator(OverviewGeneratorBase):
    """Synthesizes a system-level overview from per-space overview documents.

    Pure and side-effect free: the caller assembles ``members`` (each member's
    metadata plus its already-generated per-space overview markdown) and this
    class returns generated markdown. Persistence and threading are the
    service's responsibility.
    """

    def __init__(
        self,
        hyperspace_id: str,
        hyperspace_name: str,
        members: List[Dict[str, Any]],
        on_progress: Optional[ProgressCallback] = None,
    ) -> None:
        """
        Args:
            hyperspace_id: The hyperspace identifier.
            hyperspace_name: Human-readable hyperspace name.
            members: One dict per member space, each with keys:
                space_id (str), space_name (str | None),
                component_count (int | None), languages (list[str] | None),
                business_context (str | None),
                docs (dict[str, str]): map of member overview doc_key -> markdown.
                    Empty when the member has no Space Overview.
            on_progress: Optional callback(done, total) for progress reporting.
        """
        super().__init__(log_prefix="[hyperspace_overview]")
        self.hyperspace_id = hyperspace_id
        self.hyperspace_name = hyperspace_name
        self.members = members
        self.on_progress = on_progress

    # ── internal helpers ─────────────────────────────────────────────────

    def _members_with_overview(self) -> int:
        return sum(1 for m in self.members if m.get("docs"))

    def _build_corpus(self) -> str:
        """Assemble the per-space documents into one synthesis corpus.

        Purely mechanical (no LLM). Members without overview documents are
        included as metadata-only stubs so the model can report the gap rather
        than silently omit the space.
        """
        lines: List[str] = [
            f"SYSTEM (hyperspace): {self.hyperspace_name} (id: {self.hyperspace_id})",
            f"Member spaces: {len(self.members)} "
            f"({self._members_with_overview()} with a Space Overview available)",
            "",
            "Below are the per-space overview documents for each member space. "
            "Use them to synthesize a system-level view and to infer how the "
            "spaces connect to one another.",
            "",
        ]

        for m in self.members:
            space_name = m.get("space_name") or m.get("space_id")
            lines.append("=" * 70)
            lines.append(f"## SPACE: {space_name} (space_id: {m.get('space_id')})")
            if m.get("component_count") is not None:
                lines.append(f"- Components: {m.get('component_count')}")
            langs = m.get("languages")
            if langs:
                lines.append(f"- Languages: {', '.join(langs)}")
            if m.get("business_context"):
                lines.append(f"- Business context: {m.get('business_context')}")

            docs: Dict[str, str] = m.get("docs") or {}
            if not docs:
                lines.append(
                    "- NOTE: No Space Overview has been generated for this space; "
                    "only metadata is available. Its connections cannot be assessed "
                    "from documentation."
                )
                lines.append("")
                continue

            for doc_key in MEMBER_DOC_KEYS_FOR_SYNTHESIS:
                content = docs.get(doc_key)
                if not content:
                    continue
                title = _MEMBER_DOC_TITLES.get(doc_key, doc_key)
                lines.append("")
                lines.append(f"### {title}")
                lines.append(content.strip())
            lines.append("")

        return "\n".join(lines)

    def _generate_doc(self, doc_key: str, corpus: str) -> str:
        gen_prompt = _DOC_GENERATION_PROMPTS.get(doc_key)
        if not gen_prompt:
            raise ValueError(f"Unknown hyperspace document key: {doc_key}")

        doc_title = next(
            (d["title"] for d in HYPERSPACE_OVERVIEW_DOCUMENTS if d["key"] == doc_key), doc_key
        )

        return self._generate_document(
            system_prompt=gen_prompt,
            user_message=(
                f"Here are the per-space overview documents for the member spaces:\n\n"
                f"---\n{corpus}\n---\n\n"
                f"Generate the {doc_title} document based on this data. "
                f"Output ONLY the markdown document."
            ),
            doc_key=doc_key,
            doc_title=doc_title,
            repair_context={"hyperspace_id": self.hyperspace_id},
        )

    # ── public API ────────────────────────────────────────────────────────

    def generate_all(self) -> Dict[str, Any]:
        """Generate all hyperspace overview documents.

        Returns a dict with 'documents', 'failed', and 'stats'. Raises
        ValueError if no member space has a Space Overview to synthesize from
        (the caller enforces the "generate space overviews first" dependency).
        """
        members_with_overview = self._members_with_overview()
        if members_with_overview == 0:
            raise ValueError(
                "No member space has a generated Space Overview. Generate Space "
                "Overviews for the member spaces before generating a hyperspace overview."
            )

        total = len(HYPERSPACE_OVERVIEW_DOCUMENTS)
        generated: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []

        if self.on_progress:
            self.on_progress(0, total)

        corpus = self._build_corpus()

        for i, doc_def in enumerate(HYPERSPACE_OVERVIEW_DOCUMENTS):
            doc_key = doc_def["key"]
            try:
                markdown = self._generate_doc(doc_key, corpus)
                generated.append(
                    {
                        "key": doc_key,
                        "title": doc_def["title"],
                        "filename": doc_def["filename"],
                        "content": markdown,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            except Exception as e:
                logger.error(
                    f"[hyperspace_overview] Failed to generate '{doc_key}': {e}",
                    exc_info=True,
                )
                failed.append({"key": doc_key, "error": str(e)})

            if self.on_progress:
                self.on_progress(i + 1, total)

        return {
            "documents": generated,
            "failed": failed,
            "stats": {
                "total": total,
                "generated": len(generated),
                "failed": len(failed),
                "members_total": len(self.members),
                "members_with_overview": members_with_overview,
                "token_usage": self.token_usage.to_dict(),
                "llm_workload": self._workload_record(),
                "region": self._bedrock_region,
            },
        }
