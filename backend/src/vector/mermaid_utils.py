"""Deterministic normalization of Mermaid code blocks in generated markdown.

Mermaid requires the diagram-type declaration (``flowchart``, ``graph``,
``sequenceDiagram``, …) to be the first meaningful line of a diagram. LLM output
intermittently emits ``accTitle``/``accDescr`` accessibility directives *before*
that line (ticket P525301586), which makes Mermaid read ``accTitle`` as the
diagram type and fail to render.

This module post-processes generated markdown and relocates any such leading
directives to immediately after the diagram-type line, inside each ```mermaid
fenced block. It is a targeted fixer for a known failure class, not a full
Mermaid grammar validator, and it mirrors the client-side repair in the frontend
``MermaidBlock.tsx`` so stored content and the renderer agree.

Pure and side-effect free: callers decide whether/how to log when a repair fires
(``NormalizationResult.blocks_repaired``), so the counts can carry per-space /
per-document context for telemetry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Set

# Diagram-type keywords that must be the first meaningful line of a diagram.
# Covers the types our generators emit (flowchart/graph/sequenceDiagram) plus the
# rest of Mermaid's set so the fixer never mistakes a real diagram for prose.
_DIAGRAM_TYPE_RE = re.compile(
    r"^\s*(?:flowchart|graph|sequenceDiagram|classDiagram|stateDiagram(?:-v2)?|"
    r"erDiagram|journey|gantt|pie|requirementDiagram|gitGraph|mindmap|timeline|"
    r"quadrantChart|C4Context|C4Container|C4Component|C4Dynamic|C4Deployment|"
    r"sankey(?:-beta)?|xychart(?:-beta)?|block(?:-beta)?)\b"
)

_ACC_LINE_RE = re.compile(r"^\s*(?:accTitle\s*:|accDescr\s*:|accDescr\s*\{)")
_ACC_BLOCK_RE = re.compile(r"^\s*accDescr\s*\{")

# Opening/closing markdown fence: optional indent, 3+ backticks, optional info.
_FENCE_RE = re.compile(r"^(\s*)(`{3,})(.*)$")


def _repair_diagram(src: str) -> str:
    """Relocate leading ``accTitle``/``accDescr`` directives after the type line.

    Returns ``src`` unchanged when the diagram type is already first (or absent),
    or when there are no misplaced directives. Only ``accTitle``/``accDescr`` are
    moved; YAML frontmatter and ``%%{init}%%`` directives, which legitimately
    precede the type keyword, are left in place.
    """
    lines = src.split("\n")

    type_idx = next((i for i, ln in enumerate(lines) if _DIAGRAM_TYPE_RE.match(ln)), -1)
    if type_idx <= 0:
        return src  # not found, or already first — nothing to reorder

    moved: List[str] = []
    removed: Set[int] = set()
    i = 0
    while i < type_idx:
        line = lines[i]
        if not _ACC_LINE_RE.match(line):
            i += 1
            continue
        if _ACC_BLOCK_RE.match(line):
            # Multiline accDescr block: consume through the closing brace.
            j = i
            while j < type_idx:
                moved.append(lines[j])
                removed.add(j)
                if "}" in lines[j]:
                    break
                j += 1
            i = j + 1
        else:
            moved.append(line)
            removed.add(i)
            i += 1

    if not moved:
        return src

    before = [ln for idx, ln in enumerate(lines) if idx < type_idx and idx not in removed]
    after = lines[type_idx + 1 :]
    return "\n".join(before + [lines[type_idx]] + moved + after)


@dataclass
class NormalizationResult:
    """Outcome of normalizing the Mermaid blocks in a markdown document."""

    markdown: str
    blocks_total: int
    blocks_repaired: int


def normalize_markdown_mermaid(markdown: str) -> NormalizationResult:
    """Normalize every ```mermaid block in ``markdown``.

    Returns the (possibly rewritten) markdown plus how many mermaid blocks were
    seen and how many were actually changed. Total and defensive: non-mermaid
    fenced blocks and any unexpected shape pass through unchanged.
    """
    if not markdown or "```" not in markdown:
        return NormalizationResult(markdown, 0, 0)

    lines = markdown.split("\n")
    out: List[str] = []
    total = 0
    repaired = 0
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        m = _FENCE_RE.match(line)
        if not m:
            out.append(line)
            i += 1
            continue

        ticks, info = m.group(2), m.group(3).strip()
        is_mermaid = bool(info) and info.split()[0].lower() == "mermaid"

        # Find the matching closing fence: same-or-more backticks, no info string.
        close_idx = -1
        for j in range(i + 1, n):
            cm = _FENCE_RE.match(lines[j])
            if cm and len(cm.group(2)) >= len(ticks) and cm.group(3).strip() == "":
                close_idx = j
                break

        if close_idx == -1:
            # Unterminated fence — leave the remainder untouched.
            out.append(line)
            i += 1
            continue

        body_lines = lines[i + 1 : close_idx]
        if is_mermaid:
            total += 1
            body = "\n".join(body_lines)
            fixed = _repair_diagram(body)
            if fixed != body:
                repaired += 1
                body_lines = fixed.split("\n")

        out.append(line)  # opening fence
        out.extend(body_lines)  # (possibly repaired) body
        out.append(lines[close_idx])  # closing fence
        i = close_idx + 1

    return NormalizationResult("\n".join(out), total, repaired)
