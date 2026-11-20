/**
 * Shared pieces of the Token consumption tables (space and hyperspace pages).
 *
 * Each operation record carries a per-workload breakdown (`workloads`): the
 * model, thinking and effort that run used, and its tokens. Records written
 * before that breakdown existed only have `by_model`, so they show the model
 * and "—" for the settings, which weren't recorded.
 */
import Box from "@cloudscape-design/components/box";
import { TokenUsage } from "../api/client";

/** Display names for workloads, shared with the Settings page. */
export const WORKLOAD_LABELS: Record<string, string> = {
  summarization: "Function descriptions",
  business_context: "Business vocabulary",
  overview: "Space and Hyperspace overviews",
  chat: "Agent Hopper chat",
};

/** One table row's worth of an operation: one workload, or the whole legacy record. */
export interface UsagePart {
  /** Set when the operation ran several workloads, so the row names its one. */
  workload?: string;
  models: string[];
  /** Undefined when not recorded (legacy); null when nothing was sent. */
  thinking?: string | null;
  effort?: string | null;
  /** Undefined when not recorded. */
  region?: string;
  input: number;
  output: number;
  total: number;
  thinkingTokens: number;
  /** The operation made no model calls (e.g. a space refresh with nothing to re-describe). */
  noCalls: boolean;
}

/** Split an operation record into rows: one per workload when recorded. */
export function usageParts(record?: TokenUsage): UsagePart[] {
  if (record?.workloads?.length) {
    // Name the workload only when the operation has several (space creation);
    // "Space refresh / Function descriptions" would just repeat itself.
    const several = record.workloads.length > 1;
    return record.workloads.map((w) => ({
      workload: several ? w.workload : undefined,
      models: [w.model_id],
      thinking: w.thinking,
      effort: w.effort,
      region: w.region,
      input: w.input,
      output: w.output,
      total: w.total,
      thinkingTokens: w.thinking_tokens ?? 0,
      noCalls: w.total === 0,
    }));
  }
  const t = record?.totals ?? { input: 0, output: 0, total: 0 };
  return [
    {
      models: Object.keys(record?.by_model ?? {}),
      input: t.input,
      output: t.output,
      total: t.total,
      thinkingTokens: t.thinking ?? 0,
      noCalls: t.total === 0,
    },
  ];
}

/**
 * The model(s) a row used. ``region`` is shown under the model only when the
 * table's rows ran in different regions; otherwise the header shows it once.
 */
export function ModelCell({
  models,
  noCalls,
  region,
}: {
  models: string[];
  noCalls: boolean;
  region?: string;
}) {
  // No calls: say so once, here, instead of three ambiguous dashes.
  if (noCalls) return <Box color="text-status-inactive">No model calls</Box>;
  if (models.length === 0) return <Box color="text-status-inactive">—</Box>;
  return (
    <div>
      {models.map((m) => (
        <Box key={m} variant="code">
          {m}
        </Box>
      ))}
      {region && (
        <Box variant="small" color="text-body-secondary">
          {region}
        </Box>
      )}
    </div>
  );
}

/**
 * How to show the region: once in the header when every row that made calls
 * agrees (or only one is known), or per row when they differ. ``fallback`` is
 * the space's last-written region, used when no row recorded one.
 */
export function regionDisplay(
  rows: UsagePart[],
  fallback?: string,
): { header?: string; perRow: boolean } {
  const known = new Set(rows.filter((r) => !r.noCalls && r.region).map((r) => r.region));
  if (known.size > 1) return { perRow: true };
  return { header: known.size === 1 ? [...known][0] : fallback, perRow: false };
}

/**
 * Thinking or effort as sent: the value, "Not set", or "—" when not recorded.
 * Empty when the operation made no calls; the Model cell says so.
 */
export function SettingCell({ value, noCalls }: { value?: string | null; noCalls: boolean }) {
  if (noCalls) return null;
  if (value === undefined) {
    return (
      <span title="Recorded before CodeKB tracked this setting">
        <Box color="text-status-inactive">—</Box>
      </span>
    );
  }
  if (value === null) return <Box color="text-status-inactive">Not set</Box>;
  return <Box variant="code">{value}</Box>;
}

/** Output tokens, with the share spent thinking when the model reported it. */
export function OutputCell({ output, thinkingTokens }: { output: number; thinkingTokens: number }) {
  if (!thinkingTokens) return <>{output.toLocaleString()}</>;
  return (
    <div>
      {output.toLocaleString()}
      <Box variant="small" color="text-body-secondary">
        {thinkingTokens.toLocaleString()} thinking
      </Box>
    </div>
  );
}

/** Operation label, plus the workload on a second line when the row is one workload. */
export function OperationCell({ label, workload }: { label: string; workload?: string }) {
  return (
    <div>
      {label}
      {workload && (
        <Box variant="small" color="text-body-secondary">
          {WORKLOAD_LABELS[workload] ?? workload}
        </Box>
      )}
    </div>
  );
}

/** Table footer text: grand totals, with thinking only when any row had it. */
export function totalsText(rows: Array<{ input: number; output: number; total: number; thinkingTokens: number }>): string {
  const g = rows.reduce(
    (acc, r) => ({
      input: acc.input + r.input,
      output: acc.output + r.output,
      total: acc.total + r.total,
      thinking: acc.thinking + r.thinkingTokens,
    }),
    { input: 0, output: 0, total: 0, thinking: 0 },
  );
  const thinking = g.thinking ? `, ${g.thinking.toLocaleString()} of it thinking` : "";
  return `Total: ${g.input.toLocaleString()} in / ${g.output.toLocaleString()} out${thinking} (${g.total.toLocaleString()} tokens)`;
}
