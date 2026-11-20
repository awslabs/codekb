import { useEffect, useMemo, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import StatusIndicator, { StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Flashbar, { FlashbarProps } from "@cloudscape-design/components/flashbar";
import Modal from "@cloudscape-design/components/modal";
import Spinner from "@cloudscape-design/components/spinner";
import Badge from "@cloudscape-design/components/badge";
import Alert from "@cloudscape-design/components/alert";
import TextFilter from "@cloudscape-design/components/text-filter";
import Pagination from "@cloudscape-design/components/pagination";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Icon from "@cloudscape-design/components/icon";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Tabs from "@cloudscape-design/components/tabs";
import { api, ApiError, isQueryable, SpaceSummary, RecurringTokenUsage } from "../api/client";
import {
  ModelCell,
  OutputCell,
  regionDisplay,
  SettingCell,
  totalsText,
  usageParts,
} from "../components/TokenUsageColumns";
import MermaidBlock from "../components/MermaidBlock";
import CopyButton from "../components/CopyButton";
import "../styles/markdown.css";

function stripLeadingTitleHeading(markdown: string): string {
  if (!markdown) return markdown;
  return markdown.replace(/^(?:[ \t]*\r?\n)*#[ \t]+[^\n]*\r?\n?/, "");
}

const HS_DOC_ICONS: Record<string, string> = {
  system_overview: "file",
  space_interconnections: "share",
};

function staleReasonText(r: {
  reason: string;
  from_commit?: string;
  to_commit?: string;
}): string {
  const short = (c?: string) => (c ? c.slice(0, 8) : "unknown");
  switch (r.reason) {
    case "code_changed":
      return `code changed (${short(r.from_commit)} → ${short(r.to_commit)})`;
    case "member_added":
      return "added to the hyperspace after this overview was generated";
    case "member_removed":
      return "removed from the hyperspace after this overview was generated";
    case "space_missing":
      return "space no longer exists";
    case "not_ready":
      return "currently being (re)analyzed";
    default:
      return r.reason;
  }
}

function fmtWhenUtc(iso?: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  return (
    d.toLocaleString([], {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      timeZone: "UTC",
    }) + " UTC"
  );
}

/** Token consumption for the hyperspace SYNTHESIS only.
 *
 * Deliberately excludes member Space Overview (re)generation, which is billed
 * on each member space and shown there — summing it here would double-count.
 * The info banner makes that scope explicit so the (small) synthesis number
 * isn't mistaken for the full cost of the action.
 */
function HyperspaceTokenConsumption({
  usage,
  pricingUrl,
  model,
  memberCount,
}: {
  usage?: RecurringTokenUsage;
  pricingUrl?: string;
  model?: { model_id?: string; region?: string };
  memberCount: number;
}) {
  const rows = (usage?.recent ?? [])
    .flatMap((entry, i) =>
      usageParts(entry).map((part, j) => ({
        ...part,
        id: `hov-${entry.at ?? i}-${j}`,
        when: entry.at,
        detail: entry.trigger,
      })),
    )
    .sort((a, b) => (b.when ?? "").localeCompare(a.when ?? ""));

  if (rows.length === 0) return null;
  const regions = regionDisplay(rows, model?.region);

  return (
    <ExpandableSection
      variant="container"
      headerText="Token consumption"
      headerCounter={`(${rows.length})`}
      headerDescription="Bedrock tokens for the hyperspace synthesis only (inference). Costs are not computed — use the AWS Bedrock pricing page to estimate."
    >
      <SpaceBetween size="s">
        <Alert type="info">
          This counts only the hyperspace “summary of summaries” synthesis. Generating or
          refreshing the {memberCount} member space{memberCount === 1 ? "" : "s"}’ Space
          Overviews is measured on each member space — see the individual spaces for those tokens.
        </Alert>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-end",
            flexWrap: "wrap",
            gap: 8,
          }}
        >
          {/* The model is per row: the configured model can change between runs. */}
          {regions.header ? (
            <Box variant="small" color="text-body-secondary">
              Region{" "}
              <Box variant="code" display="inline">
                {regions.header}
              </Box>
            </Box>
          ) : (
            <span />
          )}
          {pricingUrl && (
            <Button href={pricingUrl} target="_blank" iconAlign="right" iconName="external">
              AWS Bedrock pricing
            </Button>
          )}
        </div>
        <Table
          variant="embedded"
          items={rows}
          columnDefinitions={[
            {
              id: "operation",
              header: "Operation",
              cell: (r) => (r.detail ? `Hyperspace synthesis (${r.detail})` : "Hyperspace synthesis"),
            },
            { id: "when", header: "When", cell: (r) => fmtWhenUtc(r.when) },
            {
              id: "model",
              header: "Model",
              cell: (r) => (
                <ModelCell
                  models={r.models}
                  noCalls={r.noCalls}
                  region={regions.perRow ? r.region : undefined}
                />
              ),
            },
            { id: "thinking", header: "Thinking", cell: (r) => <SettingCell value={r.thinking} noCalls={r.noCalls} /> },
            { id: "effort", header: "Effort", cell: (r) => <SettingCell value={r.effort} noCalls={r.noCalls} /> },
            { id: "input", header: "Input", cell: (r) => r.input.toLocaleString() },
            {
              id: "output",
              header: "Output",
              cell: (r) => <OutputCell output={r.output} thinkingTokens={r.thinkingTokens} />,
            },
            { id: "total", header: "Total", cell: (r) => r.total.toLocaleString() },
          ]}
          footer={
            <Box textAlign="right" fontWeight="bold">
              {totalsText(rows)}
            </Box>
          }
        />
      </SpaceBetween>
    </ExpandableSection>
  );
}

/** A member row shown in the tables — merges hyperspace membership with the
 *  richer per-space summary (languages, live status) from the spaces list. */
interface MemberRow {
  space_id: string;
  space_name?: string;
  status: string;
  component_count?: number;
  languages?: string[];
}

const MIN_SPACES = 1;

function statusIndicator(status: string): JSX.Element {
  const map: Record<string, StatusIndicatorProps.Type> = {
    READY: "success",
    FAILED: "error",
    REFRESH_FAILED: "error",
    MISSING: "error",
    ANALYZING: "in-progress",
    SUBMITTED: "in-progress",
    SUMMARIZING: "in-progress",
    REFRESHING: "in-progress",
  };
  const type = map[status] ?? "pending";
  const label =
    status === "MISSING"
      ? "Deleted"
      : status === "REFRESH_FAILED"
      ? "Refresh failed"
      : status.charAt(0) + status.slice(1).toLowerCase();
  return <StatusIndicator type={type}>{label}</StatusIndicator>;
}

export default function HyperspaceDetail() {
  const { hyperspaceId } = useParams<{ hyperspaceId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const {
    data: hyperspace,
    isLoading: hsLoading,
    isError: hsError,
    error: hsErrorObj,
  } = useQuery({
    queryKey: ["hyperspace", hyperspaceId],
    queryFn: () => api.getHyperspace(hyperspaceId!),
    enabled: !!hyperspaceId,
  });

  const { data: spaces = [] } = useQuery({
    queryKey: ["spaces"],
    queryFn: () => api.listSpaces(),
  });

  // Working (staged) edit state — initialized from the fetched hyperspace and
  // applied only when the user clicks Save. This avoids the stale-selection
  // pitfalls of the old modal: the page always seeds from its own fresh fetch.
  const [memberIds, setMemberIds] = useState<string[]>([]);
  const [baseline, setBaseline] = useState<{ ids: string[] }>({ ids: [] });

  const [saving, setSaving] = useState(false);
  const [flash, setFlash] = useState<FlashbarProps.MessageDefinition[]>([]);
  const [deleteVisible, setDeleteVisible] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // Hyperspace Overview (system-level, synthesized from member Space Overviews)
  const [overviewDocs, setOverviewDocs] = useState<Record<string, string>>({});
  const [overviewLoading, setOverviewLoading] = useState<Record<string, boolean>>({});
  const [overviewLoadFailed, setOverviewLoadFailed] = useState<Record<string, boolean>>({});
  const [activeOverviewDoc, setActiveOverviewDoc] = useState<string | null>(null);
  const [overviewSubmitting, setOverviewSubmitting] = useState(false);
  const [overviewConfirmVisible, setOverviewConfirmVisible] = useState(false);
  const [overviewEnabled, setOverviewEnabled] = useState(true);
  const [activeTabId, setActiveTabId] = useState("overview");

  // Table UI state
  const [memberSelection, setMemberSelection] = useState<MemberRow[]>([]);
  const [memberFilter, setMemberFilter] = useState("");
  const [memberPage, setMemberPage] = useState(1);
  const [addSelection, setAddSelection] = useState<SpaceSummary[]>([]);
  const [addFilter, setAddFilter] = useState("");
  const [addPage, setAddPage] = useState(1);
  const pageSize = 10;

  // Seed working state whenever a fresh hyperspace arrives.
  useEffect(() => {
    api
      .getConfig()
      .then((c) => setOverviewEnabled(c.overview_generation_enabled))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!hyperspace) return;
    setMemberIds(hyperspace.space_ids);
    setBaseline({ ids: hyperspace.space_ids });
    setMemberSelection([]);
    setAddSelection([]);
  }, [hyperspace]);

  const showError = (msg: string) =>
    setFlash([{ type: "error", content: msg, dismissible: true, onDismiss: () => setFlash([]) }]);

  // Lookup of space_id -> rich summary from the spaces list.
  const spaceById = useMemo(() => {
    const m = new Map<string, SpaceSummary>();
    for (const s of spaces) m.set(s.space_id, s);
    return m;
  }, [spaces]);

  // Member rows for the current working set. Prefer the live spaces summary;
  // fall back to the hyperspace's enriched membership; finally mark unknown
  // members as deleted/missing.
  const memberRows = useMemo<MemberRow[]>(() => {
    const enrichedById = new Map((hyperspace?.spaces ?? []).map((s) => [s.space_id, s]));
    return memberIds.map((sid) => {
      const live = spaceById.get(sid);
      const enriched = enrichedById.get(sid);
      return {
        space_id: sid,
        space_name: live?.space_name ?? enriched?.space_name,
        status: live?.status ?? enriched?.status ?? "MISSING",
        component_count: live?.component_count ?? enriched?.component_count,
        languages: live?.languages,
      };
    });
  }, [memberIds, spaceById, hyperspace]);

  // Queryable spaces not already in the working member set, for the Add panel.
  const availableSpaces = useMemo(
    () =>
      spaces.filter((s) => isQueryable(s.status) && !memberIds.includes(s.space_id)),
    [spaces, memberIds],
  );

  const filteredMembers = useMemo(() => {
    const q = memberFilter.trim().toLowerCase();
    if (!q) return memberRows;
    return memberRows.filter(
      (m) =>
        m.space_id.toLowerCase().includes(q) ||
        (m.space_name ?? "").toLowerCase().includes(q),
    );
  }, [memberRows, memberFilter]);

  const filteredAvailable = useMemo(() => {
    const q = addFilter.trim().toLowerCase();
    if (!q) return availableSpaces;
    return availableSpaces.filter(
      (s) =>
        s.space_id.toLowerCase().includes(q) ||
        (s.space_name ?? "").toLowerCase().includes(q),
    );
  }, [availableSpaces, addFilter]);

  const memberPageItems = filteredMembers.slice((memberPage - 1) * pageSize, memberPage * pageSize);
  const addPageItems = filteredAvailable.slice((addPage - 1) * pageSize, addPage * pageSize);

  const removeMembers = (ids: string[]) => {
    const drop = new Set(ids);
    setMemberIds((prev) => prev.filter((id) => !drop.has(id)));
    setMemberSelection([]);
  };

  const addSpaces = (toAdd: SpaceSummary[]) => {
    setMemberIds((prev) => {
      const existing = new Set(prev);
      return [...prev, ...toAdd.map((s) => s.space_id).filter((id) => !existing.has(id))];
    });
    setAddSelection([]);
  };

  const membersChanged =
    memberIds.length !== baseline.ids.length ||
    memberIds.some((id) => !baseline.ids.includes(id));
  const dirty = membersChanged;
  const canSave = dirty && memberIds.length >= MIN_SPACES;

  const handleSave = async () => {
    if (!hyperspaceId || !canSave) return;
    setSaving(true);
    setFlash([]);
    try {
      await api.updateHyperspace(hyperspaceId, {
        space_ids: memberIds,
      });
      setFlash([
        {
          type: "success",
          content: "Hyperspace updated.",
          dismissible: true,
          onDismiss: () => setFlash([]),
        },
      ]);
      await queryClient.invalidateQueries({ queryKey: ["hyperspace", hyperspaceId] });
      queryClient.invalidateQueries({ queryKey: ["hyperspaces"] });
    } catch (e: any) {
      showError(e.message || "Failed to update hyperspace");
    } finally {
      setSaving(false);
    }
  };

  const handleReset = () => {
    setMemberIds(baseline.ids);
    setMemberSelection([]);
    setAddSelection([]);
  };

  const handleDelete = async () => {
    if (!hyperspaceId) return;
    setDeleting(true);
    try {
      await api.deleteHyperspace(hyperspaceId);
      queryClient.invalidateQueries({ queryKey: ["hyperspaces"] });
      navigate("/hyperspaces");
    } catch (e: any) {
      showError(`Delete failed: ${e.message}`);
      setDeleting(false);
    }
  };

  // ── Hyperspace Overview ─────────────────────────────────────────────
  const overviewDocDefs = hyperspace?.hyperspace_overview_docs ?? [];
  const overviewGenerating = overviewSubmitting || hyperspace?.overview_status === "in_progress";
  const overviewFailed = hyperspace?.overview_status === "failed";
  const staleReasons = hyperspace?.overview_staleness?.reasons ?? [];
  const isStale = !!hyperspace?.overview_staleness?.stale;
  const allOverviewDocsLoaded =
    overviewDocDefs.length > 0 && overviewDocDefs.every((d) => overviewDocs[d.key]);
  // One combined Markdown document: title + each section under its own heading.
  // The docs carry no top-level H1 (stripped defensively), so headings don't double up.
  const combinedOverviewMd = allOverviewDocsLoaded
    ? [
        `# ${hyperspace?.name ?? hyperspaceId} — Hyperspace Overview`,
        "",
        ...overviewDocDefs.flatMap((d) => [
          `## ${d.title}`,
          "",
          stripLeadingTitleHeading(overviewDocs[d.key]).trim(),
          "",
        ]),
      ].join("\n")
    : "";

  // A manual call always attempts a fetch (retries a previously failed doc).
  // The auto-retry loop is prevented in the eager-load effect, which skips
  // keys in overviewLoadFailed — not here — so this stays free of stale-closure
  // issues around the failed flag.
  const loadOverviewDoc = async (docKey: string) => {
    if (!hyperspaceId || overviewDocs[docKey] || overviewLoading[docKey]) return;
    setOverviewLoading((prev) => ({ ...prev, [docKey]: true }));
    try {
      const content = await api.getHyperspaceOverviewDocument(hyperspaceId, docKey);
      setOverviewDocs((prev) => ({ ...prev, [docKey]: content }));
      // Clear any prior failure now that it loaded.
      setOverviewLoadFailed((prev) => {
        if (!prev[docKey]) return prev;
        const { [docKey]: _removed, ...rest } = prev;
        return rest;
      });
    } catch (e: any) {
      setOverviewLoadFailed((prev) => ({ ...prev, [docKey]: true }));
      // An authorization denial (e.g. a member you can't read) shows its own
      // message, naming the inaccessible spaces; other failures get the prefix.
      if (e instanceof ApiError && e.isAuthz) {
        showError(e.displayMessage);
      } else {
        showError(`Failed to load overview document: ${e.message}`);
      }
    } finally {
      setOverviewLoading((prev) => ({ ...prev, [docKey]: false }));
    }
  };

  // Generate (or regenerate from scratch) the hyperspace overview. Clears any
  // locally-cached doc content so freshly generated docs are re-fetched.
  const generateOverview = async () => {
    if (!hyperspaceId || overviewGenerating) return;
    setOverviewConfirmVisible(false);
    setOverviewSubmitting(true);
    try {
      await api.generateHyperspaceOverview(hyperspaceId);
      setOverviewDocs({});
      setOverviewLoadFailed({});
      setActiveOverviewDoc(null);
      await queryClient.invalidateQueries({ queryKey: ["hyperspace", hyperspaceId] });
    } catch (e: any) {
      showError(`Failed to start overview generation: ${e.message}`);
    } finally {
      setOverviewSubmitting(false);
    }
  };

  // Auto-select the first overview doc once docs are available. Skipped while
  // (re)generating: the catalog may briefly still reference just-deleted docs.
  useEffect(() => {
    if (overviewGenerating) return;
    if (overviewDocDefs.length > 0 && !activeOverviewDoc) {
      setActiveOverviewDoc(overviewDocDefs[0].key);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overviewGenerating, overviewDocDefs, activeOverviewDoc]);

  // Eager-load every overview doc (2 small files) so switching cards is instant
  // and "Copy as Markdown" can assemble the full document without a fetch.
  useEffect(() => {
    if (overviewGenerating) return;
    for (const doc of overviewDocDefs) {
      if (!overviewDocs[doc.key] && !overviewLoading[doc.key] && !overviewLoadFailed[doc.key]) {
        loadOverviewDoc(doc.key);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overviewGenerating, overviewDocDefs, overviewDocs, overviewLoading, overviewLoadFailed]);

  // Poll the hyperspace while overview generation is in progress.
  useEffect(() => {
    if (!overviewGenerating) return;
    const interval = setInterval(() => {
      queryClient.invalidateQueries({ queryKey: ["hyperspace", hyperspaceId] });
    }, 5000);
    return () => clearInterval(interval);
  }, [overviewGenerating, hyperspaceId, queryClient]);

  const breadcrumbs = (
    <BreadcrumbGroup
      items={[
        { text: "CodeKB", href: "/" },
        { text: "Hyperspaces", href: "/hyperspaces" },
        { text: hyperspace?.name ?? hyperspaceId ?? "", href: `/hyperspaces/${hyperspaceId}` },
      ]}
      onFollow={(e) => {
        e.preventDefault();
        navigate(e.detail.href);
      }}
    />
  );

  if (hsLoading) {
    return (
      <SpaceBetween size="l">
        {breadcrumbs}
        <Box textAlign="center" padding="xxl">
          <Spinner size="large" />
        </Box>
      </SpaceBetween>
    );
  }

  if (hsError || !hyperspace) {
    return (
      <SpaceBetween size="l">
        {breadcrumbs}
        <Box textAlign="center" padding="xxl">
          <b>Hyperspace not found</b>
          <Box variant="p" color="text-body-secondary" padding={{ top: "xs" }}>
            {(hsErrorObj as Error)?.message || "It may have been deleted."}
          </Box>
          <Box padding={{ top: "s" }}>
            <Button onClick={() => navigate("/hyperspaces")}>Back to hyperspaces</Button>
          </Box>
        </Box>
      </SpaceBetween>
    );
  }

  const missing = new Set(hyperspace.missing_space_ids ?? []);
  const inaccessibleIds = hyperspace.inaccessible_space_ids ?? [];
  const inaccessible = new Set(inaccessibleIds);

  return (
    <SpaceBetween size="l">
      {breadcrumbs}
      <Flashbar items={flash} />

      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => navigate("/hyperspaces")}>Back to hyperspaces</Button>
            <Button onClick={() => setDeleteVisible(true)}>Delete</Button>
          </SpaceBetween>
        }
      >
        {hyperspace.name}
      </Header>

      {hyperspace.invalid && (
        <Alert type="warning" header="This hyperspace references deleted spaces">
          Remove the entries marked <b>Deleted</b> below and save to repair it.
          A hyperspace needs at least {MIN_SPACES} valid space.
        </Alert>
      )}

      {inaccessibleIds.length > 0 && (
        <Alert type="warning" header="You don't have access to every member space">
          You can't read {inaccessibleIds.length} member space
          {inaccessibleIds.length === 1 ? "" : "s"}: {inaccessibleIds.join(", ")}. Generating or
          reading this hyperspace's overview, and running agent queries over it, require read
          access to every member. Get read access to these git repositories, then make sure a
          token for them is connected on the Git credentials page.
        </Alert>
      )}

      <Container header={<Header variant="h2">Details</Header>}>
        <SpaceBetween size="l">
          <KeyValuePairs
            columns={3}
            items={[
              { label: "Name", value: hyperspace.name },
              { label: "Hyperspace ID", value: <Box variant="code">{hyperspace.hyperspace_id}</Box> },
              { label: "Created by", value: hyperspace.created_by || "—" },
              {
                label: "Last updated",
                value: hyperspace.updated_at
                  ? new Date(hyperspace.updated_at).toLocaleString(undefined, {
                      year: "numeric",
                      month: "short",
                      day: "2-digit",
                      hour: "2-digit",
                      minute: "2-digit",
                    })
                  : "—",
              },
            ]}
          />
        </SpaceBetween>
      </Container>

      <Tabs
        activeTabId={activeTabId}
        onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
        tabs={[
          {
            label: "Hyperspace Overview",
            id: "overview",
            content: (
              <SpaceBetween size="l">
      {/* System Overview (hyperspace-level, synthesized from member Space Overviews) */}
      {overviewDocDefs.length > 0 ? (
        <SpaceBetween size="l">
          <Header
            variant="h2"
            description="A system-level view synthesized from the member spaces' Space Overviews. Cross-space connections are inferred from documentation, not a verified call graph — verify against source before relying on them."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <CopyButton
                  variant="normal"
                  label="Copy as Markdown"
                  text={combinedOverviewMd}
                  disabled={!allOverviewDocsLoaded || overviewGenerating}
                />
                <Button
                  iconName="refresh"
                  loading={overviewGenerating}
                  disabled={!overviewEnabled || overviewGenerating}
                  disabledReason={
                    !overviewEnabled
                      ? "Overview generation is disabled on this instance."
                      : undefined
                  }
                  onClick={() => setOverviewConfirmVisible(true)}
                >
                  Regenerate
                </Button>
              </SpaceBetween>
            }
          >
            Hyperspace Overview
          </Header>

          {isStale && (
            <Alert type="warning" header="Hyperspace overview is out of date">
              <SpaceBetween size="xs">
                <span>
                  These member spaces changed since this overview was generated. Regenerate to
                  bring it up to date (member overviews are refreshed automatically as part of that).
                </span>
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {staleReasons.map((r) => {
                    const linkable = r.reason !== "member_removed" && r.reason !== "space_missing";
                    return (
                      <li key={r.space_id}>
                        {linkable ? (
                          <Link
                            href={`/spaces/${r.space_id}?tab=overview-docs`}
                            onFollow={(e) => {
                              e.preventDefault();
                              navigate(`/spaces/${r.space_id}?tab=overview-docs`);
                            }}
                          >
                            {r.space_name || r.space_id}
                          </Link>
                        ) : (
                          <b>{r.space_name || r.space_id}</b>
                        )}
                        : {staleReasonText(r)}
                      </li>
                    );
                  })}
                </ul>
              </SpaceBetween>
            </Alert>
          )}

          {overviewGenerating && (
            <StatusIndicator type="in-progress">
              Regenerating system overview… existing documents are being rebuilt from scratch.
            </StatusIndicator>
          )}

          {/* Nav card row */}
          <div style={{ display: "flex", gap: "8px" }}>
            {overviewDocDefs.map((doc) => {
              const isActive = activeOverviewDoc === doc.key;
              const isLoading = overviewLoading[doc.key] ?? false;
              return (
                <div
                  key={doc.key}
                  className="overview-card-clickable"
                  role="button"
                  aria-label={`View ${doc.title}`}
                  title={doc.title}
                  tabIndex={0}
                  onClick={() => {
                    if (!overviewDocs[doc.key]) loadOverviewDoc(doc.key);
                    setActiveOverviewDoc(doc.key);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      if (!overviewDocs[doc.key]) loadOverviewDoc(doc.key);
                      setActiveOverviewDoc(doc.key);
                    }
                  }}
                  style={{
                    flex: 1,
                    padding: "12px 8px",
                    borderRadius: "8px",
                    border: isActive
                      ? "2px solid var(--color-border-control-focus, #0972d3)"
                      : "1px solid var(--color-border-divider-default, #e9ebed)",
                    background: isActive
                      ? "var(--color-background-layout-toggle-selected-default, #e9f3ff)"
                      : "var(--color-background-container-content, #f4f4f4)",
                    textAlign: "center",
                    transition: "all 0.15s ease",
                    boxShadow: isActive ? "0 2px 8px rgba(9,114,211,0.15)" : "0 1px 4px rgba(0,0,0,0.06)",
                  }}
                >
                  <SpaceBetween size="xxs">
                    <Box textAlign="center">
                      {isLoading ? (
                        <Spinner size="normal" />
                      ) : (
                        <Icon name={(HS_DOC_ICONS[doc.key] as any) ?? "file"} size="medium" />
                      )}
                    </Box>
                    <Box variant="awsui-key-label" fontSize="body-s">
                      {doc.title}
                    </Box>
                  </SpaceBetween>
                </div>
              );
            })}
          </div>

          {/* Document content */}
          {activeOverviewDoc && (
            <Container
              header={
                <Header variant="h2">
                  {overviewDocDefs.find((d) => d.key === activeOverviewDoc)?.title ?? activeOverviewDoc}
                </Header>
              }
            >
              {overviewDocs[activeOverviewDoc] ? (
                <div className="agent-markdown">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                      code({ className, children }) {
                        if (/language-mermaid/.test(className || "")) {
                          return <MermaidBlock chart={String(children).replace(/\n$/, "")} />;
                        }
                        return <code className={className}>{children}</code>;
                      },
                    }}
                  >
                    {stripLeadingTitleHeading(overviewDocs[activeOverviewDoc])}
                  </ReactMarkdown>
                </div>
              ) : overviewLoading[activeOverviewDoc] ? (
                <Box textAlign="center" padding="xxl">
                  <Spinner size="large" />
                </Box>
              ) : (
                <Box textAlign="center" padding="l">
                  <SpaceBetween size="s">
                    <span>Failed to load this document.</span>
                    <Button iconName="refresh" onClick={() => loadOverviewDoc(activeOverviewDoc)}>
                      Try again
                    </Button>
                  </SpaceBetween>
                </Box>
              )}
            </Container>
          )}
        </SpaceBetween>
      ) : (
        <Container
          header={
            <Header
              variant="h2"
              description="Generates a system-level overview that synthesizes the member spaces' Space Overviews and infers how the spaces connect. To keep results accurate, this first ensures every member space has an up-to-date Space Overview — generating or regenerating as needed — which incurs Bedrock cost and can take significant time for large hyperspaces."
              actions={
                !overviewGenerating ? (
                  <Button
                    variant="primary"
                    iconName="gen-ai"
                    loading={overviewSubmitting}
                    disabled={!overviewEnabled || overviewSubmitting}
                    disabledReason={
                      !overviewEnabled
                        ? "Overview generation is disabled on this instance."
                        : undefined
                    }
                    onClick={() => setOverviewConfirmVisible(true)}
                  >
                    Generate overview
                  </Button>
                ) : undefined
              }
            >
              Hyperspace Overview
            </Header>
          }
        >
          {overviewGenerating ? (
            <SpaceBetween size="s">
              <StatusIndicator type="in-progress">
                {(() => {
                  const done = hyperspace.overview_done ?? 0;
                  const total = hyperspace.overview_total ?? 0;
                  if (total === 0 || done === 0)
                    return "Ensuring member space overviews are up to date…";
                  return `Generating… (step ${done} of ${total})`;
                })()}
              </StatusIndicator>
              <ProgressBar
                value={hyperspace.overview_percent ?? 0}
                label="Overview generation"
                variant="flash"
              />
              <Box color="text-body-secondary" fontSize="body-s">
                Member space overviews are generated or refreshed first (when missing or
                older than the latest code refresh), then the system overview is
                synthesized from them.
              </Box>
            </SpaceBetween>
          ) : overviewFailed ? (
            <Alert type="error" header="Overview generation failed">
              {hyperspace.overview_error ||
                "Generation failed. Ensure at least one member space has a generated Space Overview, then try again."}
            </Alert>
          ) : (
            <Box color="text-body-secondary">
              No system overview yet. Generate one for a cross-space narrative and an
              inferred map of how these spaces connect. CodeKB first ensures every member
              space has an up-to-date Space Overview, then synthesizes the system view.
            </Box>
          )}
        </Container>
      )}

      {/* Token consumption (hyperspace synthesis only) */}
      <HyperspaceTokenConsumption
        usage={hyperspace.token_usage?.hyperspace_overview}
        pricingUrl={hyperspace.pricing_url}
        model={hyperspace.bedrock_model}
        memberCount={hyperspace.space_ids.length}
      />
              </SpaceBetween>
            ),
          },
          {
            label: "Member spaces",
            id: "members",
            content: (
              <SpaceBetween size="l">
      {/* Current members */}
      <Table
        selectionType="multi"
        selectedItems={memberSelection}
        onSelectionChange={({ detail }) => setMemberSelection(detail.selectedItems)}
        items={memberPageItems}
        trackBy="space_id"
        variant="container"
        empty={
          <Box textAlign="center" padding="m" color="inherit">
            No spaces in this hyperspace yet. Add some below.
          </Box>
        }
        header={
          <Header
            counter={`(${memberRows.length})`}
            description="Spaces currently grouped in this hyperspace"
            actions={
              <Button
                disabled={memberSelection.length === 0}
                onClick={() => removeMembers(memberSelection.map((m) => m.space_id))}
              >
                Remove selected
              </Button>
            }
          >
            Members
          </Header>
        }
        filter={
          <TextFilter
            filteringText={memberFilter}
            filteringPlaceholder="Find members"
            onChange={({ detail }) => {
              setMemberFilter(detail.filteringText);
              setMemberPage(1);
            }}
          />
        }
        pagination={
          <Pagination
            currentPageIndex={memberPage}
            pagesCount={Math.max(1, Math.ceil(filteredMembers.length / pageSize))}
            onChange={({ detail }) => setMemberPage(detail.currentPageIndex)}
          />
        }
        columnDefinitions={[
          {
            id: "name",
            header: "Name",
            cell: (m) => {
              // Not a link when the space is gone or the caller can't read it —
              // following it would only land on a not-found / access-denied page.
              const unclickable =
                m.status === "MISSING" ||
                m.status === "NOT_FOUND" ||
                inaccessible.has(m.space_id);
              return (
                <div>
                  {unclickable ? (
                    <Box fontWeight="bold">{m.space_name || m.space_id}</Box>
                  ) : (
                    <Link
                      fontSize="body-m"
                      onFollow={(e) => { e.preventDefault(); navigate(`/spaces/${m.space_id}`); }}
                      href={`/spaces/${m.space_id}`}
                    >
                      <b>{m.space_name || m.space_id}</b>
                    </Link>
                  )}
                  <div style={{ fontSize: 11, color: "#687078", marginTop: 2 }}>{m.space_id}</div>
                </div>
              );
            },
          },
          { id: "status", header: "Status", cell: (m) => statusIndicator(m.status) },
          {
            id: "access",
            header: "Access",
            cell: (m) =>
              m.status === "MISSING" || m.status === "NOT_FOUND" ? (
                "—"
              ) : inaccessible.has(m.space_id) ? (
                <StatusIndicator type="warning">No access</StatusIndicator>
              ) : (
                <StatusIndicator type="success">Access</StatusIndicator>
              ),
          },
          { id: "components", header: "Components", cell: (m) => m.component_count ?? "—" },
          {
            id: "languages",
            header: "Languages",
            cell: (m) =>
              m.languages && m.languages.length > 0 ? (
                <SpaceBetween direction="horizontal" size="xxs">
                  {m.languages.map((l) => (
                    <Badge key={l} color="blue">
                      {l}
                    </Badge>
                  ))}
                </SpaceBetween>
              ) : (
                "—"
              ),
          },
          {
            id: "actions",
            header: "",
            cell: (m) => (
              <Button variant="inline-link" onClick={() => removeMembers([m.space_id])}>
                Remove
              </Button>
            ),
          },
        ]}
      />

      {/* Add spaces */}
      <Table
        selectionType="multi"
        selectedItems={addSelection}
        onSelectionChange={({ detail }) => setAddSelection(detail.selectedItems)}
        items={addPageItems}
        trackBy="space_id"
        variant="container"
        empty={
          <Box textAlign="center" padding="m" color="inherit">
            No other indexed spaces available to add.
          </Box>
        }
        header={
          <Header
            counter={`(${availableSpaces.length})`}
            description="Indexed spaces not yet in this hyperspace"
            actions={
              <Button
                disabled={addSelection.length === 0}
                onClick={() => addSpaces(addSelection)}
              >
                Add selected
              </Button>
            }
          >
            Add spaces
          </Header>
        }
        filter={
          <TextFilter
            filteringText={addFilter}
            filteringPlaceholder="Find spaces to add"
            onChange={({ detail }) => {
              setAddFilter(detail.filteringText);
              setAddPage(1);
            }}
          />
        }
        pagination={
          <Pagination
            currentPageIndex={addPage}
            pagesCount={Math.max(1, Math.ceil(filteredAvailable.length / pageSize))}
            onChange={({ detail }) => setAddPage(detail.currentPageIndex)}
          />
        }
        columnDefinitions={[
          {
            id: "name",
            header: "Name",
            cell: (s) => (
              <div>
                <Box fontWeight="bold">{s.space_name || s.space_id}</Box>
                <div style={{ fontSize: 11, color: "#687078", marginTop: 2 }}>{s.space_id}</div>
              </div>
            ),
          },
          { id: "components", header: "Components", cell: (s) => s.component_count ?? "—" },
          {
            id: "languages",
            header: "Languages",
            cell: (s) =>
              s.languages && s.languages.length > 0 ? (
                <SpaceBetween direction="horizontal" size="xxs">
                  {s.languages.map((l) => (
                    <Badge key={l} color="blue">
                      {l}
                    </Badge>
                  ))}
                </SpaceBetween>
              ) : (
                "—"
              ),
          },
          {
            id: "actions",
            header: "",
            cell: (s) => (
              <Button variant="inline-link" onClick={() => addSpaces([s])}>
                Add
              </Button>
            ),
          },
        ]}
      />

      {/* Sticky-ish save bar */}
      <Box>
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="primary" loading={saving} disabled={!canSave} onClick={handleSave}>
            Save changes
          </Button>
          <Button disabled={!dirty || saving} onClick={handleReset}>
            Discard changes
          </Button>
          {dirty && memberIds.length < MIN_SPACES && (
            <Box color="text-status-error" padding={{ top: "xxs" }}>
              A hyperspace needs at least {MIN_SPACES} space.
            </Box>
          )}
        </SpaceBetween>
      </Box>
              </SpaceBetween>
            ),
          },
        ]}
      />

      <Modal
        visible={deleteVisible}
        onDismiss={() => setDeleteVisible(false)}
        header="Delete hyperspace"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setDeleteVisible(false)}>Cancel</Button>
              <Button variant="primary" loading={deleting} onClick={handleDelete}>
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        Delete hyperspace <Box variant="code">{hyperspace.name}</Box>? This only removes
        the grouping — the individual spaces are not affected.
      </Modal>

      {/* Hyperspace overview generation confirmation (cost/time warning) */}
      <Modal
        visible={overviewConfirmVisible}
        onDismiss={() => setOverviewConfirmVisible(false)}
        header="Generate hyperspace overview"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setOverviewConfirmVisible(false)}>Cancel</Button>
              <Button
                variant="primary"
                iconName="gen-ai"
                loading={overviewSubmitting}
                onClick={generateOverview}
              >
                Generate
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {overviewDocDefs.length > 0 && !isStale ? (
          <SpaceBetween size="m">
            <Box variant="p">
              This hyperspace overview is already up to date — no member space has changed since
              it was generated. Regenerating will not refresh any member overview; it only re-runs
              the hyperspace synthesis (a small cost). Regenerate anyway?
            </Box>
          </SpaceBetween>
        ) : (
          <SpaceBetween size="m">
            <Box variant="p">
              To produce an accurate system-level view, CodeKB will first ensure every one of
              the <b>{hyperspace.space_ids.length}</b> member space
              {hyperspace.space_ids.length === 1 ? "" : "s"} has an up-to-date Space Overview.
              It generates one where it is missing and regenerates any that are older than the
              space's latest code refresh, then synthesizes the hyperspace overview from them.
            </Box>
            <Alert type="warning">
              This runs AI generation across the member spaces, so it will incur Bedrock cost
              and can take significant time — potentially many minutes, and longer for large
              hyperspaces. Member spaces that already have a current Space Overview are reused
              and not regenerated.
            </Alert>
          </SpaceBetween>
        )}
      </Modal>
    </SpaceBetween>
  );
}
