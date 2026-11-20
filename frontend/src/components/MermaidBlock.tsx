import { useEffect, useRef, useState } from "react";
import mermaid from "mermaid";

// Diagram source is untrusted: it comes from analyzed repository content and from
// LLM output that can echo repository content back. "strict" is required here —
// it HTML-encodes tags in diagram labels, disables click/href interaction, and
// makes mermaid run the rendered SVG through DOMPurify before returning it
// (mermaid 11.x sanitizes whenever securityLevel !== "loose"). That sanitization
// is what makes the dangerouslySetInnerHTML sink below safe. Do not set "loose".
//
// htmlLabels MUST stay false: with securityLevel "strict", mermaid emits node
// labels as HTML inside <foreignObject> and then sanitizes the SVG with DOMPurify,
// which strips <foreignObject> (mermaid issue #5904). That left node boxes laid
// out correctly but with no visible label text. Rendering labels as native SVG
// <text> avoids the stripped-foreignObject problem while keeping strict sanitize.
mermaid.initialize({
  startOnLoad: false,
  theme: "base",
  securityLevel: "strict",
  htmlLabels: false,
  flowchart: { htmlLabels: false },
  themeVariables: {
    background: "#ffffff",
    primaryColor: "#e8eaf6",
    primaryTextColor: "#1a1a2e",
    primaryBorderColor: "#5c6bc0",
    lineColor: "#37474f",
    secondaryColor: "#f3e5f5",
    tertiaryColor: "#e0f2f1",
    noteBkgColor: "#fff9c4",
    noteTextColor: "#1a1a2e",
    noteBorderColor: "#f9a825",
    actorBorder: "#5c6bc0",
    actorBkg: "#e8eaf6",
    actorTextColor: "#1a1a2e",
    actorLineColor: "#37474f",
    signalColor: "#37474f",
    signalTextColor: "#1a1a2e",
    labelBoxBkgColor: "#e8eaf6",
    labelBoxBorderColor: "#5c6bc0",
    labelTextColor: "#1a1a2e",
    loopTextColor: "#1a1a2e",
    activationBorderColor: "#5c6bc0",
    activationBkgColor: "#e8eaf6",
    sequenceNumberColor: "#ffffff",
  },
});

let mermaidCounter = 0;

// Diagram-type declarations that must be the first meaningful line of a diagram.
// Covers the types our generators emit (flowchart/graph/sequenceDiagram) plus the
// rest of mermaid's set so the repair never mistakes a real diagram for prose.
const DIAGRAM_TYPE_RE =
  /^\s*(flowchart|graph|sequenceDiagram|classDiagram|stateDiagram(?:-v2)?|erDiagram|journey|gantt|pie|requirementDiagram|gitGraph|mindmap|timeline|quadrantChart|C4Context|C4Container|C4Component|C4Dynamic|C4Deployment|sankey(?:-beta)?|xychart(?:-beta)?|block(?:-beta)?)\b/;

const ACC_LINE_RE = /^\s*(?:accTitle\s*:|accDescr\s*:|accDescr\s*\{)/;

// Mermaid requires the diagram-type keyword to be the first meaningful line.
// LLM output intermittently emits accTitle/accDescr BEFORE it (ticket P525301586),
// which makes mermaid read "accTitle" as the diagram type and fail to parse. This
// relocates any such leading accessibility directives to immediately after the
// diagram-type line. It only touches accTitle/accDescr; YAML frontmatter and
// %%{init}%% directives (which legitimately precede the type keyword) are left in
// place. Returns the source unchanged when there is nothing to fix.
function repairMermaid(src: string): string {
  const lines = src.split("\n");

  const typeIdx = lines.findIndex((l) => DIAGRAM_TYPE_RE.test(l));
  if (typeIdx <= 0) return src; // not found, or already first — nothing to reorder

  const moved: string[] = [];
  const removed = new Set<number>();
  for (let i = 0; i < typeIdx; i++) {
    if (!ACC_LINE_RE.test(lines[i])) continue;
    if (/^\s*accDescr\s*\{/.test(lines[i])) {
      // Multiline accDescr block: consume through the closing brace.
      for (let j = i; j < typeIdx; j++) {
        moved.push(lines[j]);
        removed.add(j);
        if (/\}/.test(lines[j])) {
          i = j;
          break;
        }
        i = j;
      }
    } else {
      moved.push(lines[i]);
      removed.add(i);
    }
  }

  if (moved.length === 0) return src;

  const before = lines.filter((_, i) => i < typeIdx && !removed.has(i));
  const after = lines.slice(typeIdx + 1);
  return [...before, lines[typeIdx], ...moved, ...after].join("\n");
}

export default function MermaidBlock({ chart }: { chart: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [svg, setSvg] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const source = chart.trim();
    if (!source) return;
    let cancelled = false;

    const run = async () => {
      // First attempt: render as-authored so already-valid diagrams are untouched.
      try {
        const { svg: rendered } = await mermaid.render(`mermaid-${++mermaidCounter}`, source);
        if (!cancelled) { setSvg(rendered); setError(null); }
        return;
      } catch {
        // Fall through to the deterministic repair attempt.
      }

      // Second attempt: apply the directive-ordering repair and retry once.
      const repaired = repairMermaid(source);
      if (repaired !== source) {
        try {
          const { svg: rendered } = await mermaid.render(`mermaid-${++mermaidCounter}`, repaired);
          if (!cancelled) { setSvg(rendered); setError(null); }
          return;
        } catch {
          // Fall through to the source fallback.
        }
      }

      if (!cancelled) { setSvg(""); setError("render-failed"); }
    };

    run();
    return () => { cancelled = true; };
  }, [chart]);

  if (error) {
    return (
      <pre style={{ background: "#fdf0f0", padding: "12px", borderRadius: "8px", fontSize: "13px", overflowX: "auto" }}>
        <code>{chart}</code>
      </pre>
    );
  }

  return (
    <div
      ref={containerRef}
      style={{
        textAlign: "center",
        margin: "16px 0",
        overflowX: "auto",
        background: "#ffffff",
        borderRadius: "8px",
        padding: "16px",
        border: "1px solid #e0e0e0",
      }}
      // Safe only because securityLevel is "strict" above — mermaid returns
      // DOMPurify-sanitized SVG. Re-check this sink if that config changes.
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
