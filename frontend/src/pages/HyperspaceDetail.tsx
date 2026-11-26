import { useEffect, useMemo, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
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

function staleReasonText(
  r: {
    reason: string;
    from_commit?: string;
    to_commit?: string;
  },
  t: TFunction,
): string {
  const short = (c?: string) => (c ? c.slice(0, 8) : t("hyperspaceDetail.staleUnknownCommit"));
  switch (r.reason) {
    case "code_changed":
      return t("hyperspaceDetail.staleCodeChanged", { from: short(r.from_commit), to: short(r.to_commit) });
    case "member_added":
      return t("hyperspaceDetail.staleMemberAdded");
    case "member_removed":
      return t("hyperspaceDetail.staleMemberRemoved");
    case "space_missing":
      return t("hyperspaceDetail.staleSpaceMissing");
    case "not_ready":
      return t("hyperspaceDetail.staleNotReady");
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
  const { t } = useTranslation();
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
      headerText={t("hyperspaceDetail.tokenHeader")}
      headerCounter={`(${rows.length})`}
      headerDescription={t("hyperspaceDetail.tokenDescription")}
    >
      <SpaceBetween size="s">
        <Alert type="info">
          {t("hyperspaceDetail.tokenInfo", { count: memberCount })}
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
              {t("hyperspaceDetail.region")}{" "}
              <Box variant="code" display="inline">
                {regions.header}
              </Box>
            </Box>
          ) : (
            <span />
          )}
          {pricingUrl && (
            <Button href={pricingUrl} target="_blank" iconAlign="right" iconName="external">
              {t("hyperspaceDetail.bedrockPricing")}
            </Button>
          )}
        </div>
        <Table
          variant="embedded"
          items={rows}
          columnDefinitions={[
            {
              id: "operation",
              header: t("hyperspaceDetail.colOperation"),
              cell: (r) => (r.detail ? t("hyperspaceDetail.operationWithDetail", { detail: r.detail }) : t("hyperspaceDetail.operation")),
            },
            { id: "when", header: t("hyperspaceDetail.colWhen"), cell: (r) => fmtWhenUtc(r.when) },
            {
              id: "model",
              header: t("hyperspaceDetail.colModel"),
              cell: (r) => (
                <ModelCell
                  models={r.models}
                  noCalls={r.noCalls}
                  region={regions.perRow ? r.region : undefined}
                />
              ),
            },
            { id: "thinking", header: t("hyperspaceDetail.colThinking"), cell: (r) => <SettingCell value={r.thinking} noCalls={r.noCalls} /> },
            { id: "effort", header: t("hyperspaceDetail.colEffort"), cell: (r) => <SettingCell value={r.effort} noCalls={r.noCalls} /> },
            { id: "input", header: t("hyperspaceDetail.colInput"), cell: (r) => r.input.toLocaleString() },
            {
              id: "output",
              header: t("hyperspaceDetail.colOutput"),
              cell: (r) => <OutputCell output={r.output} thinkingTokens={r.thinkingTokens} />,
            },
            { id: "total", header: t("hyperspaceDetail.colTotal"), cell: (r) => r.total.toLocaleString() },
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

function statusIndicator(status: string, t: TFunction): JSX.Element {
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
  const labelKeys: Record<string, string> = {
    MISSING: "common.statusDeleted",
    READY: "common.statusReady",
    FAILED: "common.statusFailed",
    REFRESH_FAILED: "hyperspaceDetail.statusRefreshFailed",
    ANALYZING: "common.statusAnalyzing",
    SUBMITTED: "hyperspaceDetail.statusSubmitted",
    SUMMARIZING: "common.statusSummarizing",
    REFRESHING: "common.statusRefreshing",
  };
  const label = labelKeys[status]
    ? t(labelKeys[status])
    : status.charAt(0) + status.slice(1).toLowerCase();
  return <StatusIndicator type={type}>{label}</StatusIndicator>;
}

export default function HyperspaceDetail() {
  const { t } = useTranslation();
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
          content: t("hyperspaceDetail.flashUpdated"),
          dismissible: true,
          onDismiss: () => setFlash([]),
        },
      ]);
      await queryClient.invalidateQueries({ queryKey: ["hyperspace", hyperspaceId] });
      queryClient.invalidateQueries({ queryKey: ["hyperspaces"] });
    } catch (e: any) {
      showError(e.message || t("hyperspaceDetail.updateFailed"));
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
      showError(t("hyperspaceDetail.deleteFailed", { message: e.message }));
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
        t("hyperspaceDetail.overviewTitle", { name: hyperspace?.name ?? hyperspaceId }),
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
        showError(t("hyperspaceDetail.loadDocFailed", { message: e.message }));
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
      showError(t("hyperspaceDetail.startFailed", { message: e.message }));
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
        { text: t("common.breadcrumbRoot"), href: "/" },
        { text: t("nav.hyperspaces"), href: "/hyperspaces" },
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
          <b>{t("hyperspaceDetail.notFoundTitle")}</b>
          <Box variant="p" color="text-body-secondary" padding={{ top: "xs" }}>
            {(hsErrorObj as Error)?.message || t("hyperspaceDetail.notFoundDefault")}
          </Box>
          <Box padding={{ top: "s" }}>
            <Button onClick={() => navigate("/hyperspaces")}>{t("hyperspaceDetail.backToHyperspaces")}</Button>
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
            <Button onClick={() => navigate("/hyperspaces")}>{t("hyperspaceDetail.backToHyperspaces")}</Button>
            <Button onClick={() => setDeleteVisible(true)}>{t("hyperspaceDetail.delete")}</Button>
          </SpaceBetween>
        }
      >
        {hyperspace.name}
      </Header>

      {hyperspace.invalid && (
        <Alert type="warning" header={t("hyperspaceDetail.invalidHeader")}>
          {t("hyperspaceDetail.invalidBodyPrefix")}<b>{t("common.statusDeleted")}</b>{t("hyperspaceDetail.invalidBodySuffix", { min: MIN_SPACES })}
        </Alert>
      )}

      {inaccessibleIds.length > 0 && (
        <Alert type="warning" header={t("hyperspaceDetail.noAccessHeader")}>
          {t("hyperspaceDetail.noAccessBody", { count: inaccessibleIds.length, ids: inaccessibleIds.join(", ") })}
        </Alert>
      )}

      <Container header={<Header variant="h2">{t("hyperspaceDetail.detailsHeader")}</Header>}>
        <SpaceBetween size="l">
          <KeyValuePairs
            columns={3}
            items={[
              { label: t("hyperspaceDetail.detailName"), value: hyperspace.name },
              { label: t("hyperspaceDetail.detailId"), value: <Box variant="code">{hyperspace.hyperspace_id}</Box> },
              { label: t("hyperspaceDetail.detailCreatedBy"), value: hyperspace.created_by || "—" },
              {
                label: t("hyperspaceDetail.detailUpdated"),
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
            label: t("hyperspaceDetail.tabOverview"),
            id: "overview",
            content: (
              <SpaceBetween size="l">
      {/* System Overview (hyperspace-level, synthesized from member Space Overviews) */}
      {overviewDocDefs.length > 0 ? (
        <SpaceBetween size="l">
          <Header
            variant="h2"
            description={t("hyperspaceDetail.overviewDescription")}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <CopyButton
                  variant="normal"
                  label={t("hyperspaceDetail.copyMarkdown")}
                  text={combinedOverviewMd}
                  disabled={!allOverviewDocsLoaded || overviewGenerating}
                />
                <Button
                  iconName="refresh"
                  loading={overviewGenerating}
                  disabled={!overviewEnabled || overviewGenerating}
                  disabledReason={
                    !overviewEnabled
                      ? t("hyperspaceDetail.overviewDisabled")
                      : undefined
                  }
                  onClick={() => setOverviewConfirmVisible(true)}
                >
                  {t("hyperspaceDetail.regenerate")}
                </Button>
              </SpaceBetween>
            }
          >
            {t("hyperspaceDetail.tabOverview")}
          </Header>

          {isStale && (
            <Alert type="warning" header={t("hyperspaceDetail.staleHeader")}>
              <SpaceBetween size="xs">
                <span>
                  {t("hyperspaceDetail.staleBody")}
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
                        : {staleReasonText(r, t)}
                      </li>
                    );
                  })}
                </ul>
              </SpaceBetween>
            </Alert>
          )}

          {overviewGenerating && (
            <StatusIndicator type="in-progress">
              {t("hyperspaceDetail.regenerating")}
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
                  aria-label={t("hyperspaceDetail.viewDoc", { title: doc.title })}
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
                    <span>{t("hyperspaceDetail.docLoadFailed")}</span>
                    <Button iconName="refresh" onClick={() => loadOverviewDoc(activeOverviewDoc)}>
                      {t("hyperspaceDetail.tryAgain")}
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
              description={t("hyperspaceDetail.generateDescription")}
              actions={
                !overviewGenerating ? (
                  <Button
                    variant="primary"
                    iconName="gen-ai"
                    loading={overviewSubmitting}
                    disabled={!overviewEnabled || overviewSubmitting}
                    disabledReason={
                      !overviewEnabled
                        ? t("hyperspaceDetail.overviewDisabled")
                        : undefined
                    }
                    onClick={() => setOverviewConfirmVisible(true)}
                  >
                    {t("hyperspaceDetail.generateOverview")}
                  </Button>
                ) : undefined
              }
            >
              {t("hyperspaceDetail.tabOverview")}
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
                    return t("hyperspaceDetail.ensuring");
                  return t("hyperspaceDetail.generatingStep", { done, total });
                })()}
              </StatusIndicator>
              <ProgressBar
                value={hyperspace.overview_percent ?? 0}
                label={t("hyperspaceDetail.progressLabel")}
                variant="flash"
              />
              <Box color="text-body-secondary" fontSize="body-s">
                {t("hyperspaceDetail.progressNote")}
              </Box>
            </SpaceBetween>
          ) : overviewFailed ? (
            <Alert type="error" header={t("hyperspaceDetail.failedHeader")}>
              {hyperspace.overview_error ||
                t("hyperspaceDetail.failedDefault")}
            </Alert>
          ) : (
            <Box color="text-body-secondary">
              {t("hyperspaceDetail.noOverviewYet")}
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
            label: t("hyperspaceDetail.tabMembers"),
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
            {t("hyperspaceDetail.membersEmpty")}
          </Box>
        }
        header={
          <Header
            counter={`(${memberRows.length})`}
            description={t("hyperspaceDetail.membersDescription")}
            actions={
              <Button
                disabled={memberSelection.length === 0}
                onClick={() => removeMembers(memberSelection.map((m) => m.space_id))}
              >
                {t("hyperspaceDetail.removeSelected")}
              </Button>
            }
          >
            {t("hyperspaceDetail.membersHeader")}
          </Header>
        }
        filter={
          <TextFilter
            filteringText={memberFilter}
            filteringPlaceholder={t("hyperspaceDetail.findMembersPlaceholder")}
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
            ariaLabels={{ nextPageLabel: t("hyperspaceDetail.pageNext"), previousPageLabel: t("hyperspaceDetail.pagePrevious"), pageLabel: (n) => t("hyperspaceDetail.pageLabel", { page: n }) }}
            onChange={({ detail }) => setMemberPage(detail.currentPageIndex)}
          />
        }
        columnDefinitions={[
          {
            id: "name",
            header: t("hyperspaceDetail.colName"),
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
          { id: "status", header: t("hyperspaceDetail.colStatus"), cell: (m) => statusIndicator(m.status, t) },
          {
            id: "access",
            header: t("hyperspaceDetail.colAccess"),
            cell: (m) =>
              m.status === "MISSING" || m.status === "NOT_FOUND" ? (
                "—"
              ) : inaccessible.has(m.space_id) ? (
                <StatusIndicator type="warning">{t("hyperspaceDetail.accessNo")}</StatusIndicator>
              ) : (
                <StatusIndicator type="success">{t("hyperspaceDetail.accessYes")}</StatusIndicator>
              ),
          },
          { id: "components", header: t("hyperspaceDetail.colComponents"), cell: (m) => m.component_count ?? "—" },
          {
            id: "languages",
            header: t("hyperspaceDetail.colLanguages"),
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
                {t("hyperspaceDetail.remove")}
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
            {t("hyperspaceDetail.availableEmpty")}
          </Box>
        }
        header={
          <Header
            counter={`(${availableSpaces.length})`}
            description={t("hyperspaceDetail.availableDescription")}
            actions={
              <Button
                disabled={addSelection.length === 0}
                onClick={() => addSpaces(addSelection)}
              >
                {t("hyperspaceDetail.addSelected")}
              </Button>
            }
          >
            {t("hyperspaceDetail.addSpaces")}
          </Header>
        }
        filter={
          <TextFilter
            filteringText={addFilter}
            filteringPlaceholder={t("hyperspaceDetail.findToAddPlaceholder")}
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
            ariaLabels={{ nextPageLabel: t("hyperspaceDetail.pageNext"), previousPageLabel: t("hyperspaceDetail.pagePrevious"), pageLabel: (n) => t("hyperspaceDetail.pageLabel", { page: n }) }}
            onChange={({ detail }) => setAddPage(detail.currentPageIndex)}
          />
        }
        columnDefinitions={[
          {
            id: "name",
            header: t("hyperspaceDetail.colName"),
            cell: (s) => (
              <div>
                <Box fontWeight="bold">{s.space_name || s.space_id}</Box>
                <div style={{ fontSize: 11, color: "#687078", marginTop: 2 }}>{s.space_id}</div>
              </div>
            ),
          },
          { id: "components", header: t("hyperspaceDetail.colComponents"), cell: (s) => s.component_count ?? "—" },
          {
            id: "languages",
            header: t("hyperspaceDetail.colLanguages"),
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
                {t("hyperspaceDetail.add")}
              </Button>
            ),
          },
        ]}
      />

      {/* Sticky-ish save bar */}
      <Box>
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="primary" loading={saving} disabled={!canSave} onClick={handleSave}>
            {t("hyperspaceDetail.saveChanges")}
          </Button>
          <Button disabled={!dirty || saving} onClick={handleReset}>
            {t("hyperspaceDetail.discardChanges")}
          </Button>
          {dirty && memberIds.length < MIN_SPACES && (
            <Box color="text-status-error" padding={{ top: "xxs" }}>
              {t("hyperspaceDetail.minSpaces", { min: MIN_SPACES })}
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
        header={t("hyperspaceDetail.deleteHeader")}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setDeleteVisible(false)}>{t("common.cancel")}</Button>
              <Button variant="primary" loading={deleting} onClick={handleDelete}>
                {t("hyperspaceDetail.delete")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {t("hyperspaceDetail.deleteBodyPrefix")}<Box variant="code">{hyperspace.name}</Box>{t("hyperspaceDetail.deleteBodySuffix")}
      </Modal>

      {/* Hyperspace overview generation confirmation (cost/time warning) */}
      <Modal
        visible={overviewConfirmVisible}
        onDismiss={() => setOverviewConfirmVisible(false)}
        header={t("hyperspaceDetail.confirmHeader")}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setOverviewConfirmVisible(false)}>{t("common.cancel")}</Button>
              <Button
                variant="primary"
                iconName="gen-ai"
                loading={overviewSubmitting}
                onClick={generateOverview}
              >
                {t("hyperspaceDetail.generate")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {overviewDocDefs.length > 0 && !isStale ? (
          <SpaceBetween size="m">
            <Box variant="p">
              {t("hyperspaceDetail.confirmUpToDate")}
            </Box>
          </SpaceBetween>
        ) : (
          <SpaceBetween size="m">
            <Box variant="p">
              {t("hyperspaceDetail.confirmBodyPrefix")}<b>{hyperspace.space_ids.length}</b>{t("hyperspaceDetail.confirmBodySuffix", { count: hyperspace.space_ids.length })}
            </Box>
            <Alert type="warning">
              {t("hyperspaceDetail.confirmWarning")}
            </Alert>
          </SpaceBetween>
        )}
      </Modal>
    </SpaceBetween>
  );
}
