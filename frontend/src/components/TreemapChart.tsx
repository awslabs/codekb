import { useMemo, useState, useRef, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { SpaceSummary, Hyperspace } from "../api/client";

// ── Types ──────────────────────────────────────────────────────────────

interface TreeNode {
  id: string;
  label: string;
  value: number;
  status?: string;
  languages?: string[];
  group?: string;        // hyperspace name
  groupId?: string;      // hyperspace id
}

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface LayoutNode extends TreeNode, Rect {}

interface GroupRect extends Rect {
  groupId: string;
  name: string;
  color: string;
  stroke: string;
}

// ── Squarified treemap algorithm ───────────────────────────────────────

function squarify(items: TreeNode[], bounds: Rect): LayoutNode[] {
  if (items.length === 0) return [];
  const total = items.reduce((s, i) => s + i.value, 0);
  if (total === 0) return items.map((it, i) => ({ ...it, x: bounds.x, y: bounds.y + i, w: bounds.w, h: 1 }));

  const sorted = [...items].sort((a, b) => b.value - a.value);
  const result: LayoutNode[] = [];
  layoutStrip(sorted, bounds, total, result);
  return result;
}

function layoutStrip(items: TreeNode[], bounds: Rect, totalValue: number, out: LayoutNode[]) {
  if (items.length === 0) return;
  if (items.length === 1) {
    out.push({ ...items[0], ...bounds });
    return;
  }

  const { w, h } = bounds;
  const horizontal = w >= h;
  const side = horizontal ? h : w;

  let row: TreeNode[] = [];
  let rowSum = 0;
  let bestAspect = Infinity;

  for (let i = 0; i < items.length; i++) {
    const candidate = [...row, items[i]];
    const candidateSum = rowSum + items[i].value;
    const worst = worstAspect(candidate, candidateSum, side, totalValue, bounds);

    if (worst <= bestAspect) {
      row = candidate;
      rowSum = candidateSum;
      bestAspect = worst;
    } else {
      // lay out current row, recurse on remainder
      const usedFraction = rowSum / totalValue;
      const usedSize = horizontal ? bounds.w * usedFraction : bounds.h * usedFraction;

      let offset = 0;
      for (const item of row) {
        const frac = item.value / rowSum;
        const itemSize = side * frac;
        if (horizontal) {
          out.push({ ...item, x: bounds.x, y: bounds.y + offset, w: usedSize, h: itemSize });
        } else {
          out.push({ ...item, x: bounds.x + offset, y: bounds.y, w: itemSize, h: usedSize });
        }
        offset += itemSize;
      }

      const remaining = items.slice(i);
      const remainingTotal = totalValue - rowSum;
      const newBounds: Rect = horizontal
        ? { x: bounds.x + usedSize, y: bounds.y, w: bounds.w - usedSize, h: bounds.h }
        : { x: bounds.x, y: bounds.y + usedSize, w: bounds.w, h: bounds.h - usedSize };
      layoutStrip(remaining, newBounds, remainingTotal, out);
      return;
    }
  }

  // lay out final row
  let offset = 0;
  const usedFraction = rowSum / totalValue;
  const usedSize = horizontal ? bounds.w * usedFraction : bounds.h * usedFraction;
  for (const item of row) {
    const frac = item.value / rowSum;
    const itemSize = side * frac;
    if (horizontal) {
      out.push({ ...item, x: bounds.x, y: bounds.y + offset, w: usedSize, h: itemSize });
    } else {
      out.push({ ...item, x: bounds.x + offset, y: bounds.y, w: itemSize, h: usedSize });
    }
    offset += itemSize;
  }
}

function worstAspect(row: TreeNode[], rowSum: number, side: number, totalValue: number, bounds: Rect): number {
  if (row.length === 0) return Infinity;
  const horizontal = bounds.w >= bounds.h;
  const stripSize = (horizontal ? bounds.w : bounds.h) * (rowSum / totalValue);
  if (stripSize === 0) return Infinity;

  let worst = 0;
  for (const item of row) {
    const itemSize = side * (item.value / rowSum);
    if (itemSize === 0) continue;
    const aspect = Math.max(stripSize / itemSize, itemSize / stripSize);
    worst = Math.max(worst, aspect);
  }
  return worst;
}

// ── Color palettes ─────────────────────────────────────────────────────

const CELL_COLORS = [
  { bg: "#dbeafe", border: "#93c5fd", text: "#1e40af" },
  { bg: "#dcfce7", border: "#86efac", text: "#166534" },
  { bg: "#f3e8ff", border: "#c4b5fd", text: "#5b21b6" },
  { bg: "#fef3c7", border: "#fcd34d", text: "#92400e" },
  { bg: "#ffe4e6", border: "#fda4af", text: "#9f1239" },
  { bg: "#ccfbf1", border: "#5eead4", text: "#115e59" },
  { bg: "#fef9c3", border: "#fde047", text: "#854d0e" },
  { bg: "#e0e7ff", border: "#a5b4fc", text: "#3730a3" },
  { bg: "#fce7f3", border: "#f9a8d4", text: "#9d174d" },
  { bg: "#d1fae5", border: "#6ee7b7", text: "#065f46" },
  { bg: "#e8e0d4", border: "#d0c4ac", text: "#5c4a2a" },
  { bg: "#dbeafe", border: "#7dd3fc", text: "#0c4a6e" },
];

const GROUP_COLORS = [
  { bg: "rgba(9,114,211,0.06)", border: "#539fe5" },
  { bg: "rgba(29,129,2,0.06)", border: "#4ea83b" },
  { bg: "rgba(136,47,160,0.06)", border: "#b06cc4" },
  { bg: "rgba(217,21,21,0.05)", border: "#e86161" },
  { bg: "rgba(232,124,0,0.06)", border: "#f0a030" },
];

// ── Build grouped layout ───────────────────────────────────────────────

function buildTreemapLayout(
  spaces: SpaceSummary[],
  hyperspaces: Hyperspace[],
  width: number,
  height: number,
): { cells: LayoutNode[]; groups: GroupRect[] } {
  const ready = spaces.filter((s) => s.status === "READY" || s.component_count);
  if (ready.length === 0) return { cells: [], groups: [] };

  const assigned = new Set<string>();
  interface GroupData { hs: Hyperspace; items: TreeNode[]; totalValue: number }
  const groupDatas: GroupData[] = [];

  for (const hs of hyperspaces) {
    const members = ready.filter((s) => hs.space_ids.includes(s.space_id) && !assigned.has(s.space_id));
    if (members.length === 0) continue;
    members.forEach((s) => assigned.add(s.space_id));
    const items: TreeNode[] = members.map((s) => ({
      id: s.space_id, label: s.space_name || s.space_id,
      value: Math.max(s.component_count ?? 0, 1),
      status: s.status, languages: s.languages,
      group: hs.name, groupId: hs.hyperspace_id,
    }));
    groupDatas.push({ hs, items, totalValue: items.reduce((s, i) => s + i.value, 0) });
  }

  const standalone = ready.filter((s) => !assigned.has(s.space_id)).map((s) => ({
    id: s.space_id, label: s.space_name || s.space_id,
    value: Math.max(s.component_count ?? 0, 1),
    status: s.status, languages: s.languages,
  }));

  // Treat each hyperspace group + standalone as top-level items for outer layout
  interface OuterItem { kind: "group" | "standalone"; value: number; idx: number }
  const outerItems: OuterItem[] = [
    ...groupDatas.map((g, i) => ({ kind: "group" as const, value: g.totalValue, idx: i })),
    ...standalone.map((_, i) => ({ kind: "standalone" as const, value: _.value, idx: i })),
  ];

  // First pass: lay out outer regions.
  // squarify sorts its input descending, so the returned nodes are NOT in the
  // same order as outerItems. Match them back by id instead of by index.
  const outerNodes = squarify(
    outerItems.map((o, i) => ({ id: `outer-${i}`, label: "", value: o.value })),
    { x: 0, y: 0, w: width, h: height },
  );
  const regionById = new Map(outerNodes.map((n) => [n.id, n]));

  const cells: LayoutNode[] = [];
  const groups: GroupRect[] = [];
  const PAD = 2;
  const GROUP_PAD = 22; // extra top padding for group label

  for (let i = 0; i < outerItems.length; i++) {
    const outer = outerItems[i];
    const region = regionById.get(`outer-${i}`);
    if (!region) continue;

    if (outer.kind === "group") {
      const gd = groupDatas[outer.idx];
      const gc = GROUP_COLORS[outer.idx % GROUP_COLORS.length];
      groups.push({
        x: region.x + PAD, y: region.y + PAD,
        w: region.w - PAD * 2, h: region.h - PAD * 2,
        groupId: gd.hs.hyperspace_id, name: gd.hs.name,
        color: gc.bg, stroke: gc.border,
      });
      // Layout children inside the group region with padding
      const inner: Rect = {
        x: region.x + PAD + 2, y: region.y + PAD + GROUP_PAD,
        w: Math.max(region.w - PAD * 2 - 4, 1),
        h: Math.max(region.h - PAD * 2 - GROUP_PAD - 2, 1),
      };
      const innerCells = squarify(gd.items, inner);
      cells.push(...innerCells);
    } else {
      const s = standalone[outer.idx];
      cells.push({
        ...s,
        x: region.x + PAD, y: region.y + PAD,
        w: region.w - PAD * 2, h: region.h - PAD * 2,
      });
    }
  }

  return { cells, groups };
}

// ── TreemapChart component ─────────────────────────────────────────────

interface TreemapChartProps {
  spaces: SpaceSummary[];
  hyperspaces: Hyperspace[];
  hint?: string;
}

const CHART_W = 1200;
const CHART_H = 700;

export default function TreemapChart({ spaces, hyperspaces, hint }: TreemapChartProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [tooltip, setTooltip] = useState<{ x: number; y: number; text: string } | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const { cells, groups } = useMemo(
    () => buildTreemapLayout(spaces, hyperspaces, CHART_W, CHART_H),
    [spaces, hyperspaces],
  );

  const handleMouseEnter = useCallback((e: React.MouseEvent, cell: LayoutNode) => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const text = `${cell.label} — ${cell.value.toLocaleString()} ${t("common.components")}${cell.languages?.length ? ` · ${cell.languages.join(", ")}` : ""}${cell.group ? ` · ${cell.group}` : ""}`;
    setTooltip({ x: e.clientX - rect.left, y: e.clientY - rect.top - 14, text });
  }, [t]);

  if (cells.length === 0) return null;

  return (
    <div>
      <div
        ref={containerRef}
        style={{
          position: "relative",
          background: "linear-gradient(135deg, #f8f9fb 0%, #eef1f6 100%)",
          borderRadius: 16,
          border: "1px solid #d5dbdb",
          overflow: "hidden",
        }}
      >
        {hint && (
          <div style={{
            position: "absolute", top: 10, right: 14, fontSize: 12, fontStyle: "italic",
            color: "#5f6b7a", fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
            pointerEvents: "none", zIndex: 5,
          }}>
            {hint}
          </div>
        )}
        <svg
          viewBox={`0 0 ${CHART_W} ${CHART_H}`}
          style={{ width: "100%", height: "60vh", display: "block" }}
          role="img"
          aria-label={t("components.treemapAriaLabel")}
        >
          {/* Hyperspace group backgrounds */}
          {groups.map((g) => {
            // Uppercase bold text at ~11px with letter-spacing ≈ 7px per glyph.
            // Reserve 16px for left/right padding inside the header.
            const maxGroupChars = Math.max(3, Math.floor((g.w - 16) / 7));
            const groupLabel = g.name.length > maxGroupChars
              ? g.name.slice(0, maxGroupChars - 1) + "…"
              : g.name;
            return (
              <g key={g.groupId}>
                <rect
                  x={g.x} y={g.y} width={g.w} height={g.h}
                  rx={6} ry={6}
                  fill={g.color} stroke={g.stroke}
                  strokeWidth={1.5} strokeDasharray="4 2"
                />
                <text
                  x={g.x + 8} y={g.y + 15}
                  fontSize={11} fontWeight={700}
                  fontFamily="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
                  fill={g.stroke} letterSpacing="0.5"
                  style={{ pointerEvents: "none", textTransform: "uppercase" } as React.CSSProperties}
                >
                  {groupLabel}
                  {groupLabel !== g.name && <title>{g.name}</title>}
                </text>
              </g>
            );
          })}

          {/* Space cells */}
          {cells.map((cell, idx) => {
            const palette = CELL_COLORS[idx % CELL_COLORS.length];
            const minDim = Math.min(cell.w, cell.h);
            // Below these sizes a label is just noise; the hover tooltip
            // (wired on every cell's <g>) carries the full name + count instead.
            const showLabel = cell.w > 46 && cell.h > 24;
            const showCount = cell.w > 46 && cell.h > 38;
            const fontSize = Math.max(8, Math.min(13, minDim / 6));
            const countSize = Math.max(7, Math.min(11, minDim / 7));
            const maxChars = Math.max(3, Math.floor(cell.w / (fontSize * 0.55)));
            const displayLabel = cell.label.length > maxChars
              ? cell.label.slice(0, maxChars - 1) + "…"
              : cell.label;

            return (
              <g
                key={cell.id}
                style={{ cursor: "pointer" }}
                onClick={() => navigate(`/spaces/${cell.id}`)}
                onMouseEnter={(e) => handleMouseEnter(e, cell)}
                onMouseMove={(e) => handleMouseEnter(e, cell)}
                onMouseLeave={() => setTooltip(null)}
              >
                <rect
                  x={cell.x + 1} y={cell.y + 1}
                  width={Math.max(cell.w - 2, 0)} height={Math.max(cell.h - 2, 0)}
                  rx={4} ry={4}
                  fill={palette.bg}
                  stroke={palette.border}
                  strokeWidth={0.75}
                />
                {showLabel && (
                  <text
                    x={cell.x + cell.w / 2} y={cell.y + cell.h / 2 + (showCount ? -3 : 3)}
                    textAnchor="middle" fontSize={fontSize} fontWeight={500}
                    fontFamily="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
                    fill={palette.text}
                    style={{ pointerEvents: "none" }}
                  >
                    {displayLabel}
                  </text>
                )}
                {showCount && (
                  <text
                    x={cell.x + cell.w / 2} y={cell.y + cell.h / 2 + countSize + 1}
                    textAnchor="middle" fontSize={countSize} fontWeight={400}
                    fontFamily="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
                    fill={palette.text} opacity={0.7}
                    style={{ pointerEvents: "none" }}
                  >
                    {cell.value.toLocaleString()}
                  </text>
                )}
              </g>
            );
          })}
        </svg>

        {/* Tooltip */}
        {tooltip && (
          <div style={{
            position: "absolute", left: tooltip.x, top: tooltip.y,
            transform: "translate(-50%, -100%)",
            background: "rgba(22,25,31,0.92)", backdropFilter: "blur(6px)",
            color: "#fff", padding: "7px 12px", borderRadius: 8, fontSize: 12,
            fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
            whiteSpace: "nowrap", pointerEvents: "none", zIndex: 10,
            boxShadow: "0 4px 12px rgba(0,0,0,0.2)",
          }}>
            {tooltip.text}
          </div>
        )}
      </div>
    </div>
  );
}
