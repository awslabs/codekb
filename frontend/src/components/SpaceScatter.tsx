import { useMemo, useState, useRef, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { scaleLinear, type ScaleLinear } from "d3-scale";
import { select } from "d3-selection";
import { zoom, zoomIdentity, type ZoomBehavior, type ZoomTransform, type D3ZoomEvent } from "d3-zoom";
import Button from "@cloudscape-design/components/button";
import Select from "@cloudscape-design/components/select";
import FormField from "@cloudscape-design/components/form-field";
import { SpaceSummary } from "../api/client";

// ── Colors ─────────────────────────────────────────────────────────────
// Bright, high-saturation qualitative palette with wide hue spacing. Assigned by
// frequency, so the most common languages get the most separated hues.
const LANG_COLORS = [
  "#2f6bff", // blue    - java
  "#ff8a00", // orange  - typescript
  "#17c650", // green   - python
  "#ff3b30", // red     - javascript
  "#a042ff", // purple  - kotlin
  "#00c2d1", // cyan    - cpp
  "#ff2d9e", // magenta - go
  "#00a884", // teal    - unknown
  "#9acd00", // lime    - c
  "#b5651d", // brown   - csharp
];
const OTHER_COLOR = "#aab4c0";

// Small deterministic jitter (stable per space) to de-overlap the dense diagonal
// band where points pile up. Returns offsets in [-1, 1].
function jitter(id: string): [number, number] {
  let h = 2166136261;
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  const a = ((h >>> 0) % 1000) / 1000;
  const b = (((h >>> 11) >>> 0) % 1000) / 1000;
  return [a * 2 - 1, b * 2 - 1];
}
const JITTER = 6; // px

function primaryLang(s: SpaceSummary): string {
  return (s.languages && s.languages[0]) || "unknown";
}

function fmtNum(n: number): string {
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(n % 1e9 ? 1 : 0) + "B";
  if (a >= 1e6) return (n / 1e6).toFixed(n % 1e6 ? 1 : 0) + "M";
  if (a >= 1e3) return (n / 1e3).toFixed(n % 1e3 ? 1 : 0) + "k";
  if (a > 0 && a < 1) return n.toFixed(2);
  return Math.round(n).toString();
}

// Integer powers of ten within a log10-valued domain (half-steps when zoomed in).
function logTicks(domain: [number, number]): number[] {
  const lo = Math.min(domain[0], domain[1]);
  const hi = Math.max(domain[0], domain[1]);
  let step = hi - lo > 9 ? 2 : 1;
  const collect = (st: number) => {
    const out: number[] = [];
    for (let p = Math.ceil(lo / st) * st; p <= hi + 1e-9; p += st) out.push(p);
    return out;
  };
  let ticks = collect(step);
  if (ticks.length < 3) ticks = collect((step = 0.5));
  return ticks;
}

// ── Metrics (axis + size options) ──────────────────────────────────────
type ScaleKind = "log" | "linear";

interface Row {
  id: string;
  label: string;
  loc: number;
  comps: number;
  vectors: number;
  langCount: number;
  granularity: number; // components per 1k lines
  lang: string;
  status: string;
  languages?: string[];
}

interface Metric {
  id: string;
  label: string;
  get: (r: Row) => number;
  scale: ScaleKind;
}

const METRICS: Metric[] = [
  { id: "loc", label: "Lines of code", get: (r) => r.loc, scale: "log" },
  { id: "components", label: "Components", get: (r) => r.comps, scale: "log" },
  { id: "granularity", label: "Components / 1k lines", get: (r) => r.granularity, scale: "log" },
  { id: "vectors", label: "Vectors", get: (r) => r.vectors, scale: "log" },
  { id: "languages", label: "Language count", get: (r) => r.langCount, scale: "linear" },
];
const metricById = (id: string) => METRICS.find((m) => m.id === id) ?? METRICS[0];

const DOT_R = 3.4;
const toPlot = (v: number, scale: ScaleKind) => (scale === "log" ? Math.log10(Math.max(1, v)) : v);

const H = 620;
const MARGIN = { top: 18, right: 26, bottom: 54, left: 72 };

interface SpaceScatterProps {
  spaces: SpaceSummary[];
}

export default function SpaceScatter({ spaces }: SpaceScatterProps) {
  const navigate = useNavigate();
  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const zoomRef = useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const [width, setWidth] = useState(900);
  const [transform, setTransform] = useState<ZoomTransform>(zoomIdentity);
  const [tooltip, setTooltip] = useState<{ x: number; y: number; text: string } | null>(null);
  const [hoverId, setHoverId] = useState<string | null>(null);

  const [xId, setXId] = useState("loc");
  const [yId, setYId] = useState("vectors");

  const xMetric = metricById(xId);
  const yMetric = metricById(yId);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const update = () => setWidth(el.clientWidth);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const rows = useMemo<Row[]>(
    () =>
      spaces
        .filter((s) => (s.status === "READY" || s.component_count) && (s.total_lines ?? 0) > 0)
        .map((s) => {
          const loc = s.total_lines ?? 0;
          const comps = s.component_count ?? 0;
          return {
            id: s.space_id,
            label: s.space_name || s.space_id,
            loc,
            comps,
            vectors: s.vector_count ?? 0,
            langCount: s.language_count ?? (s.languages?.length ?? 0),
            granularity: loc > 0 ? (comps / loc) * 1000 : 0,
            lang: primaryLang(s),
            status: s.status ?? "UNKNOWN",
            languages: s.languages,
          };
        }),
    [spaces],
  );

  // Color always encodes primary language; legend sorted by frequency.
  const { colorOf, legend } = useMemo(() => {
    const freq = new Map<string, number>();
    rows.forEach((r) => freq.set(r.lang, (freq.get(r.lang) ?? 0) + 1));
    const sorted = [...freq.entries()].sort((a, b) => b[1] - a[1]);
    const top = sorted.slice(0, LANG_COLORS.length);
    const map = new Map<string, string>();
    top.forEach(([l], i) => map.set(l, LANG_COLORS[i]));
    const colorOf = (r: Row) => map.get(r.lang) ?? OTHER_COLOR;
    const legend = top.map(([l, c]) => ({ key: l, color: map.get(l)!, count: c }));
    const otherCount = sorted.slice(LANG_COLORS.length).reduce((s, [, c]) => s + c, 0);
    if (otherCount > 0) legend.push({ key: "Other", color: OTHER_COLOR, count: otherCount });
    return { colorOf, legend };
  }, [rows]);

  const { xScale, yScale } = useMemo(() => {
    const xs = rows.map((r) => toPlot(xMetric.get(r), xMetric.scale));
    const ys = rows.map((r) => toPlot(yMetric.get(r), yMetric.scale));
    const dom = (vals: number[]): [number, number] => {
      const lo = Math.min(...vals);
      const hi = Math.max(...vals);
      const pad = (hi - lo) * 0.05 || 0.5;
      return [lo - pad, hi + pad];
    };
    const xd = vals_or_default(xs);
    const yd = vals_or_default(ys);
    const xScale = scaleLinear().domain(dom(xd)).range([MARGIN.left, width - MARGIN.right]);
    const yScale = scaleLinear().domain(dom(yd)).range([H - MARGIN.bottom, MARGIN.top]);
    return { xScale, yScale };
  }, [rows, width, xMetric, yMetric]);

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const sel = select<SVGSVGElement, unknown>(svg);
    const zb = zoom<SVGSVGElement, unknown>()
      .scaleExtent([1, 60])
      .extent([[0, 0], [width, H]])
      .translateExtent([[0, 0], [width, H]])
      .on("zoom", (e: D3ZoomEvent<SVGSVGElement, unknown>) => setTransform(e.transform));
    zoomRef.current = zb;
    sel.call(zb);
    return () => {
      sel.on(".zoom", null);
    };
  }, [width]);

  const resetZoom = useCallback(() => {
    const svg = svgRef.current;
    if (svg && zoomRef.current) {
      select<SVGSVGElement, unknown>(svg).transition().duration(400).call(zoomRef.current.transform, zoomIdentity);
    }
  }, []);

  const zx = transform.rescaleX(xScale);
  const zy = transform.rescaleY(yScale);
  const xticks = axisTicks(zx, xMetric.scale);
  const yticks = axisTicks(zy, yMetric.scale);

  const plotX = MARGIN.left;
  const plotY = MARGIN.top;
  const plotW = Math.max(0, width - MARGIN.left - MARGIN.right);
  const plotH = Math.max(0, H - MARGIN.top - MARGIN.bottom);

  const handleHover = useCallback(
    (e: React.MouseEvent, r: Row) => {
      const rect = containerRef.current?.getBoundingClientRect();
      setHoverId(r.id);
      if (!rect) return;
      const parts = [
        `${fmtNum(r.loc)} lines`,
        `${fmtNum(r.comps)} components`,
        r.vectors > 0 ? `${fmtNum(r.vectors)} vectors` : null,
        r.languages?.length ? r.languages.join(", ") : null,
      ].filter(Boolean);
      setTooltip({ x: e.clientX - rect.left, y: e.clientY - rect.top - 14, text: `${r.label} — ${parts.join(" · ")}` });
    },
    [],
  );

  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-end", marginBottom: 10 }}>
        <AxisSelect label="X axis" value={xId} onChange={setXId} options={METRICS} />
        <AxisSelect label="Y axis" value={yId} onChange={setYId} options={METRICS} />
        <div style={{ flex: 1 }} />
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 12, color: "#5f6b7a" }}>{rows.length.toLocaleString()} spaces · scroll to zoom</span>
          <Button iconName="zoom-to-fit" onClick={resetZoom}>Reset</Button>
        </div>
      </div>

      <div
        ref={containerRef}
        style={{ position: "relative", background: "#ffffff", borderRadius: 16, border: "1px solid #d5dbdb", overflow: "hidden" }}
      >
        <svg
          ref={svgRef}
          width={width}
          height={H}
          style={{ display: "block", cursor: "grab" }}
          role="img"
          aria-label={`Scatterplot of spaces: ${xMetric.label} versus ${yMetric.label}`}
        >
          <defs>
            <clipPath id="scatter-plot">
              <rect x={plotX} y={plotY} width={plotW} height={plotH} />
            </clipPath>
          </defs>

          {xticks.map((t) => (
            <g key={`x${t.tv}`}>
              <line x1={zx(t.tv)} y1={plotY} x2={zx(t.tv)} y2={plotY + plotH} stroke="#eef1f6" strokeWidth={1} />
              <text x={zx(t.tv)} y={plotY + plotH + 16} textAnchor="middle" fontSize={10} fill="#8b97a7" fontFamily="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">
                {t.label}
              </text>
            </g>
          ))}
          {yticks.map((t) => (
            <g key={`y${t.tv}`}>
              <line x1={plotX} y1={zy(t.tv)} x2={plotX + plotW} y2={zy(t.tv)} stroke="#eef1f6" strokeWidth={1} />
              <text x={plotX - 8} y={zy(t.tv) + 3} textAnchor="end" fontSize={10} fill="#8b97a7" fontFamily="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">
                {t.label}
              </text>
            </g>
          ))}

          <text x={plotX + plotW / 2} y={H - 6} textAnchor="middle" fontSize={16} fontWeight={700} fill="#16202b" fontFamily="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">
            {xMetric.label}
            {xMetric.scale === "log" && <tspan fontSize={12} fontWeight={400} fill="#8b97a7"> (log scale)</tspan>}
          </text>
          <text transform={`translate(18, ${plotY + plotH / 2}) rotate(-90)`} textAnchor="middle" fontSize={16} fontWeight={700} fill="#16202b" fontFamily="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">
            {yMetric.label}
            {yMetric.scale === "log" && <tspan fontSize={12} fontWeight={400} fill="#8b97a7"> (log scale)</tspan>}
          </text>

          <g clipPath="url(#scatter-plot)">
            <g transform={`translate(${transform.x},${transform.y}) scale(${transform.k})`}>
              {rows.map((r) => {
                const active = !hoverId || hoverId === r.id;
                const base = DOT_R / transform.k;
                const [jx, jy] = jitter(r.id);
                return (
                  <circle
                    key={r.id}
                    cx={xScale(toPlot(xMetric.get(r), xMetric.scale)) + jx * JITTER}
                    cy={yScale(toPlot(yMetric.get(r), yMetric.scale)) + jy * JITTER}
                    r={hoverId === r.id ? base * 1.8 : base}
                    fill={colorOf(r)}
                    stroke="#ffffff"
                    strokeWidth={0.6 / transform.k}
                    opacity={active ? 0.9 : 0.22}
                    style={{ cursor: "pointer" }}
                    onClick={() => navigate(`/spaces/${r.id}`)}
                    onMouseEnter={(e) => handleHover(e, r)}
                    onMouseMove={(e) => handleHover(e, r)}
                    onMouseLeave={() => {
                      setHoverId(null);
                      setTooltip(null);
                    }}
                  />
                );
              })}
            </g>
          </g>
        </svg>

        <div
          style={{
            position: "absolute", left: 72, top: 14,
            background: "rgba(255,255,255,0.85)", backdropFilter: "blur(4px)",
            border: "1px solid #e4e8ee", borderRadius: 8, padding: "8px 10px",
            fontSize: 11, fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
            color: "#334155", maxWidth: 180,
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 4, color: "#5f6b7a" }}>Primary language</div>
          {legend.map((d) => (
            <div key={d.key} style={{ display: "flex", alignItems: "center", gap: 6, lineHeight: "16px" }}>
              <span style={{ width: 9, height: 9, borderRadius: 9, background: d.color, flexShrink: 0 }} />
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{d.key}</span>
              <span style={{ color: "#8b97a7" }}>{d.count}</span>
            </div>
          ))}
        </div>

        {tooltip && (
          <div
            style={{
              position: "absolute", left: tooltip.x, top: tooltip.y, transform: "translate(-50%, -100%)",
              background: "rgba(22,25,31,0.92)", backdropFilter: "blur(6px)", color: "#fff",
              padding: "7px 12px", borderRadius: 8, fontSize: 12,
              fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
              whiteSpace: "nowrap", pointerEvents: "none", zIndex: 10, boxShadow: "0 4px 12px rgba(0,0,0,0.2)",
            }}
          >
            {tooltip.text}
          </div>
        )}
      </div>

      <div style={{ marginTop: 8, fontSize: 12, color: "#5f6b7a", fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" }}>
        Each dot is a space. Pick any pair of metrics for the axes. Click a space to open it.
      </div>
    </div>
  );
}

// ── helpers ────────────────────────────────────────────────────────────

function vals_or_default(vals: number[]): number[] {
  return vals.length > 0 ? vals : [0, 1];
}

function axisTicks(scale: ScaleLinear<number, number>, kind: ScaleKind): { tv: number; label: string }[] {
  if (kind === "log") {
    return logTicks(scale.domain() as [number, number]).map((tv) => ({ tv, label: fmtNum(Math.pow(10, tv)) }));
  }
  return scale.ticks(6).map((tv) => ({ tv, label: fmtNum(tv) }));
}

function AxisSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { id: string; label: string }[];
}) {
  const selected = options.find((o) => o.id === value) ?? options[0];
  return (
    <div style={{ minWidth: 150 }}>
      <FormField label={label}>
        <Select
          selectedOption={{ label: selected.label, value: selected.id }}
          onChange={({ detail }) => onChange(detail.selectedOption.value as string)}
          options={options.map((o) => ({ label: o.label, value: o.id }))}
        />
      </FormField>
    </div>
  );
}
