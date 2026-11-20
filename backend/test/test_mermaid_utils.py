"""Tests for deterministic Mermaid normalization (ticket P525301586).

The generators run ``normalize_markdown_mermaid`` on LLM output before persisting
so that ``accTitle``/``accDescr`` accessibility directives emitted *before* the
diagram-type keyword — which make Mermaid fail to render — are relocated to just
after it. This mirrors the client-side repair in ``MermaidBlock.tsx``.
"""

from src.vector.mermaid_utils import (
    NormalizationResult,
    normalize_markdown_mermaid,
)


def _mermaid_doc(body: str) -> str:
    return f"## Diagram\n\n```mermaid\n{body}\n```\n\nTrailing prose.\n"


def test_reorders_acc_lines_before_flowchart():
    """The ticket's exact failure: accTitle/accDescr ahead of `flowchart`."""
    body = (
        "accTitle: Business context for Xero Workbench CLI\n"
        "accDescr: Shows the Xero engineer using the Workbench CLI\n"
        "flowchart TB\n"
        "    engineer([Xero Engineer])\n"
        "    cli --> GIT & CLAUDE"
    )
    result = normalize_markdown_mermaid(_mermaid_doc(body))

    assert result.blocks_total == 1
    assert result.blocks_repaired == 1

    # First meaningful line inside the fence must now be the diagram type.
    fence_body = result.markdown.split("```mermaid\n", 1)[1].split("\n```", 1)[0]
    lines = fence_body.split("\n")
    assert lines[0] == "flowchart TB"
    assert lines[1] == "accTitle: Business context for Xero Workbench CLI"
    assert lines[2] == "accDescr: Shows the Xero engineer using the Workbench CLI"
    # Body content and the ampersand edge survive untouched.
    assert "cli --> GIT & CLAUDE" in fence_body


def test_valid_diagram_is_unchanged():
    """A diagram whose type keyword is already first must be byte-identical."""
    body = (
        "flowchart TB\n"
        "accTitle: Deployment topology\n"
        "accDescr: Shows the CI/CD path\n"
        "    R1 --> DT"
    )
    doc = _mermaid_doc(body)
    result = normalize_markdown_mermaid(doc)

    assert result.markdown == doc
    assert result.blocks_total == 1
    assert result.blocks_repaired == 0


def test_multiline_accdescr_block_moves_as_a_unit():
    """A braced multi-line accDescr block is relocated whole, in order."""
    body = (
        "accTitle: x\n"
        "accDescr {\n"
        "  line one\n"
        "  line two\n"
        "}\n"
        "flowchart LR\n"
        "  A --> B"
    )
    result = normalize_markdown_mermaid(_mermaid_doc(body))

    assert result.blocks_repaired == 1
    fence_body = result.markdown.split("```mermaid\n", 1)[1].split("\n```", 1)[0]
    lines = fence_body.split("\n")
    assert lines[0] == "flowchart LR"
    assert lines[1] == "accTitle: x"
    assert lines[2] == "accDescr {"
    assert lines[3] == "  line one"
    assert lines[4] == "  line two"
    assert lines[5] == "}"
    assert lines[6] == "  A --> B"


def test_graph_lr_variant_is_handled():
    """`graph LR` (used by the hyperspace generator) is a recognized type."""
    body = "accTitle: System connections\ngraph LR\n  S1 --> S2"
    result = normalize_markdown_mermaid(_mermaid_doc(body))

    assert result.blocks_repaired == 1
    fence_body = result.markdown.split("```mermaid\n", 1)[1].split("\n```", 1)[0]
    assert fence_body.split("\n")[0] == "graph LR"


def test_non_mermaid_fences_are_untouched():
    """A python code block that happens to contain `accTitle:` is left alone."""
    doc = (
        "## Example\n\n"
        "```python\n"
        "accTitle = 'not a mermaid directive'\n"
        "flowchart = compute()\n"
        "```\n"
    )
    result = normalize_markdown_mermaid(doc)

    assert result.markdown == doc
    assert result.blocks_total == 0
    assert result.blocks_repaired == 0


def test_multiple_blocks_counted_independently():
    """A doc with one broken and one valid mermaid block reports total=2, repaired=1."""
    broken = _mermaid_doc("accTitle: a\nflowchart TB\n  A --> B")
    valid = _mermaid_doc("sequenceDiagram\n  A->>B: hi")
    result = normalize_markdown_mermaid(broken + "\n" + valid)

    assert result.blocks_total == 2
    assert result.blocks_repaired == 1


def test_no_diagram_keyword_is_left_as_is():
    """If no recognized diagram type is present, nothing is moved."""
    body = "accTitle: orphaned\naccDescr: no diagram type here\n  A --> B"
    doc = _mermaid_doc(body)
    result = normalize_markdown_mermaid(doc)

    assert result.markdown == doc
    assert result.blocks_total == 1
    assert result.blocks_repaired == 0


def test_markdown_without_fences_is_passthrough():
    doc = "# Title\n\nJust prose, no code blocks.\n"
    result = normalize_markdown_mermaid(doc)
    assert result == NormalizationResult(doc, 0, 0)


def test_empty_input():
    assert normalize_markdown_mermaid("") == NormalizationResult("", 0, 0)
