import { useEffect, useState, useCallback, useRef } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Input from "@cloudscape-design/components/input";
import Select from "@cloudscape-design/components/select";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Tabs from "@cloudscape-design/components/tabs";
import Table from "@cloudscape-design/components/table";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Flashbar, { FlashbarProps } from "@cloudscape-design/components/flashbar";
import Modal from "@cloudscape-design/components/modal";
import Spinner from "@cloudscape-design/components/spinner";
import Badge from "@cloudscape-design/components/badge";
import Alert from "@cloudscape-design/components/alert";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Icon from "@cloudscape-design/components/icon";
import { api, ApiError, GIT_CREDS_PATH, SpaceDetail as SpaceDetailType, AnalysisStatus, SpaceOverviewDoc, SpaceTokenUsage } from "../api/client";
import {
  ModelCell,
  OperationCell,
  regionDisplay,
  OutputCell,
  SettingCell,
  UsagePart,
  totalsText,
  usageParts,
} from "../components/TokenUsageColumns";
import { useDefaultTopK } from "../hooks/useSearchDefaults";
import MermaidBlock from "../components/MermaidBlock";
import CodeBlock, { languageFromPath } from "../components/CodeBlock";
import CopyButton from "../components/CopyButton";
import "../styles/markdown.css";

const TYPE_OPTIONS = [
  { label: "All types", value: "" },
  { label: "Functions", value: "function" },
  { label: "Classes", value: "class" },
  { label: "Methods", value: "method" },
  { label: "Variables", value: "variable" },
];

// Space Overview docs are generated with a top-level `# <Title>` heading that
// duplicates the section header rendered above the content (and the selector
// card). Strip a single leading level-1 heading so the title isn't repeated.
// Handles already-generated docs whose H1 is baked into the stored markdown.
function stripLeadingTitleHeading(markdown: string): string {
  if (!markdown) return markdown;
  return markdown.replace(/^(?:[ \t]*\r?\n)*#[ \t]+[^\n]*\r?\n?/, "");
}

export default function SpaceDetail() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const navigate = useNavigate();
  const [space, setSpace] = useState<SpaceDetailType | null>(null);
  const [status, setStatus] = useState<AnalysisStatus | null>(null);
  const [loading, setLoading] = useState(true);
  // A sticky read denial (read_denied / expired): stop polling and show an access
  // state. authorization_unavailable is excluded — it's transient (503), so we keep
  // polling and let it recover instead of stranding the page.
  const [accessError, setAccessError] = useState<ApiError | null>(null);
  // A transient 503 while loading a READY space's detail. The status poller is
  // stopped (READY is terminal), so a dedicated effect retries getSpace until it
  // recovers, and we show a retryable banner meanwhile instead of a dead spinner.
  const [spaceUnavailable, setSpaceUnavailable] = useState(false);
  // A transient 503 on GET /status (now behind the git read gate). status stays
  // null, so the main poller — guarded on status — never starts; a dedicated
  // effect retries loadStatus and we show a retryable banner meanwhile.
  const [statusUnavailable, setStatusUnavailable] = useState(false);
  const [flash, setFlash] = useState<FlashbarProps.MessageDefinition[]>([]);

  // Delete confirmation
  const [deleteVisible, setDeleteVisible] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // Refresh
  const [refreshing, setRefreshing] = useState(false);
  // System-wide exclusion patterns, to warn before refreshing a space whose
  // own (immutable) patterns differ from them.
  const [systemPatterns, setSystemPatterns] = useState<string[] | null>(null);
  const [refreshConfirmVisible, setRefreshConfirmVisible] = useState(false);

  // Explorer: component search
  const [searchPattern, setSearchPattern] = useState("");
  const [typeFilter, setTypeFilter] = useState(TYPE_OPTIONS[0]);
  const [components, setComponents] = useState<Record<string, any>[]>([]);
  const [searchedPattern, setSearchedPattern] = useState<string | null>(null);
  const [compLoading, setCompLoading] = useState(false);

  // Explorer: semantic search
  const defaultTopK = useDefaultTopK();
  const [semanticQuery, setSemanticQuery] = useState("");
  const [semanticTopK, setSemanticTopK] = useState<string>(String(defaultTopK));
  const semanticTopKTouched = useRef(false);
  const [semanticResults, setSemanticResults] = useState<Record<string, any>[]>([]);
  const [searchedSemanticQuery, setSearchedSemanticQuery] = useState<string | null>(null);
  const [semanticLoading, setSemanticLoading] = useState(false);

  // Seed the Top K input from config once it arrives, unless the user has
  // already edited it.
  useEffect(() => {
    if (!semanticTopKTouched.current) setSemanticTopK(String(defaultTopK));
  }, [defaultTopK]);

  // Explorer: detail panel (per-tab to avoid cross-tab bleed)
  const [syntacticDetail, setSyntacticDetail] = useState<Record<string, any> | null>(null);
  const [syntacticFlow, setSyntacticFlow] = useState<Record<string, any> | null>(null);
  const [syntacticDeps, setSyntacticDeps] = useState<Record<string, any> | null>(null);
  const [syntacticDetailLoading, setSyntacticDetailLoading] = useState(false);
  const [syntacticFlowLoading, setSyntacticFlowLoading] = useState(false);
  const [syntacticDepsLoading, setSyntacticDepsLoading] = useState(false);

  const [semanticDetail, setSemanticDetail] = useState<Record<string, any> | null>(null);
  const [semanticFlow, setSemanticFlow] = useState<Record<string, any> | null>(null);
  const [semanticDeps, setSemanticDeps] = useState<Record<string, any> | null>(null);
  const [semanticDetailLoading, setSemanticDetailLoading] = useState(false);
  const [semanticFlowLoading, setSemanticFlowLoading] = useState(false);
  const [semanticDepsLoading, setSemanticDepsLoading] = useState(false);

  // Space Overview
  const [overviewDocs, setOverviewDocs] = useState<Record<string, string>>({});
  const [overviewLoading, setOverviewLoading] = useState<Record<string, boolean>>({});
  const [overviewLoadFailed, setOverviewLoadFailed] = useState<Record<string, boolean>>({});
  const [activeOverviewDoc, setActiveOverviewDoc] = useState<string | null>(null);
  const [overviewSubmitting, setOverviewSubmitting] = useState(false);
  const [overviewEnabled, setOverviewEnabled] = useState(true);
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeTabId, setActiveTabId] = useState(() => searchParams.get("tab") || "codebase");

  const syntacticDetailRef = useRef<HTMLDivElement>(null);
  const semanticDetailRef = useRef<HTMLDivElement>(null);

  const isTerminal = (s: string) => ["READY", "FAILED", "REFRESH_FAILED", "NOT_FOUND"].includes(s);
  // A refresh keeps the last completed snapshot viewable; initial analysis has nothing yet.
  const hasReadableSnapshot = (s: string) =>
    s === "READY" || s === "REFRESHING" || s === "REFRESH_FAILED";
  const isRefreshFailed = status?.status === "REFRESH_FAILED";

  // Writes 409 server-side during refresh; disable rather than surface errors.
  const isRefreshing = status?.status === "REFRESHING";

  const showError = (msg: string) => {
    setFlash([{ type: "error", content: msg, dismissible: true, onDismiss: () => setFlash([]) }]);
  };

  const handleConfirmDelete = async () => {
    const id = space?.space_id ?? spaceId;
    if (!id) return;
    setDeleting(true);
    try {
      await api.deleteSpace(id);
      setDeleteVisible(false);
      navigate("/");
    } catch (e: any) {
      // Close the dialog either way — the flash explains the failure, so the
      // confirm popup shouldn't linger (e.g. a write_denied authz refusal).
      setDeleteVisible(false);
      const msg =
        e instanceof ApiError && e.isAuthz ? e.displayMessage : `Delete failed: ${e.message}`;
      showError(msg);
    } finally {
      setDeleting(false);
    }
  };

  const handleRefresh = async () => {
    if (!spaceId) return;
    setRefreshing(true);
    try {
      await api.refreshSpace(spaceId);
      await loadStatus();
    } catch (e: any) {
      const msg = e.message?.includes("is busy")
        ? "Space is currently being processed. Try again once it finishes."
        : e.message?.includes("upload-sourced")
        ? "Space refresh is only supported for git-sourced spaces."
        : `Refresh failed: ${e.message}`;
      showError(msg);
    } finally {
      setRefreshing(false);
    }
  };

  const requestRefresh = () => {
    if (isRefreshing) return;
    const spacePatterns = space?.exclude_patterns ?? [];
    const differs =
      systemPatterns !== null &&
      (systemPatterns.length !== spacePatterns.length ||
        systemPatterns.some((p) => !spacePatterns.includes(p)));
    if (differs) setRefreshConfirmVisible(true);
    else handleRefresh();
  };

  useEffect(() => {
    api
      .getConfig()
      .then((c) => {
        setSystemPatterns(c.exclude_patterns);
        setOverviewEnabled(c.overview_generation_enabled);
      })
      .catch(() => {});
  }, []);

  // A real denial halts polling; a transient 503 does not (the poller retries it).
  const isStickyAuthz = (e: unknown): e is ApiError =>
    e instanceof ApiError && e.isAuthz && !e.isAuthorizationUnavailable;

  const loadStatus = useCallback(async () => {
    if (!spaceId) return;
    try { const s = await api.getStatus(spaceId); setStatus(s); setStatusUnavailable(false); return s; }
    catch (e) {
      if (isStickyAuthz(e)) setAccessError(e);
      // Transient 503: flag for the retry effect below (the main poller needs a status to run).
      else if (e instanceof ApiError && e.isAuthorizationUnavailable) setStatusUnavailable(true);
      return null;
    }
  }, [spaceId]);

  const loadSpace = useCallback(async () => {
    if (!spaceId) return;
    try { const p = await api.getSpace(spaceId); setSpace(p); setSpaceUnavailable(false); }
    catch (e: any) {
      if (isStickyAuthz(e)) { setAccessError(e); return; }
      // Transient 503: flag for the retry effect below (and show a retryable banner).
      if (e instanceof ApiError && e.isAuthorizationUnavailable) { setSpaceUnavailable(true); return; }
      showError(`Failed to load space details: ${e.message}`);
    }
  }, [spaceId]);

  useEffect(() => {
    if (!spaceId) return;
    // React Router reuses this component across /spaces/:spaceId navigations, so
    // clear any prior access denial — otherwise a readable space would keep
    // showing the stale "no access" state and polling would stay disabled.
    setAccessError(null);
    setSpaceUnavailable(false);
    setStatusUnavailable(false);
    setLoading(true);
    (async () => {
      const s = await loadStatus();
      if (s && hasReadableSnapshot(s.status)) await loadSpace();
      setLoading(false);
    })();
  }, [spaceId, loadStatus, loadSpace]);

  useEffect(() => {
    if (!status || isTerminal(status.status) || accessError) return;
    const interval = setInterval(async () => {
      const s = await loadStatus();
      if (s && hasReadableSnapshot(s.status)) await loadSpace();
    }, 5000);
    return () => clearInterval(interval);
  }, [status, loadStatus, loadSpace, accessError]);

  // The status poller stops at READY, so a detail fetch that hit a transient 503
  // would otherwise never retry. Poll getSpace here until it loads or recovers.
  useEffect(() => {
    if (!spaceUnavailable || space || accessError) return;
    const interval = setInterval(() => { loadSpace(); }, 5000);
    return () => clearInterval(interval);
  }, [spaceUnavailable, space, accessError, loadSpace]);

  // Retry loadStatus after a transient 503 until it recovers (see statusUnavailable).
  useEffect(() => {
    if (!statusUnavailable || status || accessError) return;
    const interval = setInterval(() => { loadStatus(); }, 5000);
    return () => clearInterval(interval);
  }, [statusUnavailable, status, accessError, loadStatus]);

  // Return to the space list once an in-progress deletion finishes (the
  // DELETING view promises this). The 5s poller above keeps running while the
  // space is DELETING and picks up the terminal NOT_FOUND the backend writes
  // when deletion completes. Guard on having actually observed DELETING so a
  // direct load of a missing space — where status resolves to NOT_FOUND — does
  // not bounce to "/".
  const sawDeletingRef = useRef(false);
  // Scope the DELETING→NOT_FOUND detection to the current space. React Router
  // reuses this component instance across /spaces/:spaceId params, so reset the
  // latch when spaceId changes — otherwise a space that was DELETING would make
  // a later, different space that resolves to NOT_FOUND bounce to "/".
  useEffect(() => {
    sawDeletingRef.current = false;
  }, [spaceId]);
  useEffect(() => {
    if (status?.status === "DELETING") {
      sawDeletingRef.current = true;
    } else if (sawDeletingRef.current && status?.status === "NOT_FOUND") {
      navigate("/");
    }
  }, [status, navigate]);

  // Explorer handlers
  const handleComponentSearch = async () => {
    if (!spaceId) return;
    setCompLoading(true);
    setSyntacticDetail(null); setSyntacticFlow(null); setSyntacticDeps(null);
    try {
      const params: { pattern?: string; type?: string } = {};
      if (searchPattern.trim()) params.pattern = searchPattern.trim();
      if (typeFilter.value) params.type = typeFilter.value;
      setComponents(await api.listComponents(spaceId, params));
      setSearchedPattern(searchPattern.trim());
    } catch (e: any) { showError(`Component search failed: ${e.message}`); }
    finally { setCompLoading(false); }
  };

  const handleSemanticSearch = async () => {
    if (!spaceId || !semanticQuery.trim()) return;
    setSemanticLoading(true);
    setSemanticDetail(null); setSemanticFlow(null); setSemanticDeps(null);
    try {
      setSemanticResults(await api.semanticSearch(spaceId, semanticQuery.trim(), parseInt(semanticTopK) || defaultTopK));
      setSearchedSemanticQuery(semanticQuery.trim());
    }
    catch (e: any) { showError(`Semantic search failed: ${e.message}`); }
    finally { setSemanticLoading(false); }
  };

  // Per-tab component selection handlers
  const handleSelectSyntactic = async (name: string) => {
    if (!spaceId) return;
    setSyntacticDetailLoading(true); setSyntacticFlow(null); setSyntacticDeps(null);
    try {
      setSyntacticDetail(await api.findComponent(spaceId, name));
      setTimeout(() => syntacticDetailRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 100);
    }
    catch (e: any) { showError(`Failed to load component: ${e.message}`); }
    finally { setSyntacticDetailLoading(false); }
  };

  const handleSelectSemantic = async (name: string) => {
    if (!spaceId) return;
    setSemanticDetailLoading(true); setSemanticFlow(null); setSemanticDeps(null);
    try {
      setSemanticDetail(await api.findComponent(spaceId, name));
      setTimeout(() => semanticDetailRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 100);
    }
    catch (e: any) { showError(`Failed to load component: ${e.message}`); }
    finally { setSemanticDetailLoading(false); }
  };

  const handleTraceSyntacticFlow = async () => {
    if (!spaceId || !syntacticDetail) return;
    setSyntacticFlowLoading(true);
    try { setSyntacticFlow(await api.traceFlow(spaceId, syntacticDetail.name)); }
    catch (e: any) { showError(`Flow trace failed: ${e.message}`); }
    finally { setSyntacticFlowLoading(false); }
  };

  const handleTraceSemanticFlow = async () => {
    if (!spaceId || !semanticDetail) return;
    setSemanticFlowLoading(true);
    try { setSemanticFlow(await api.traceFlow(spaceId, semanticDetail.name)); }
    catch (e: any) { showError(`Flow trace failed: ${e.message}`); }
    finally { setSemanticFlowLoading(false); }
  };

  const handleShowSyntacticDeps = async () => {
    if (!spaceId || !syntacticDetail) return;
    setSyntacticDepsLoading(true);
    try { setSyntacticDeps(await api.showDependencies(spaceId, syntacticDetail.name)); }
    catch (e: any) { showError(`Dependency analysis failed: ${e.message}`); }
    finally { setSyntacticDepsLoading(false); }
  };

  const handleShowSemanticDeps = async () => {
    if (!spaceId || !semanticDetail) return;
    setSemanticDepsLoading(true);
    try { setSemanticDeps(await api.showDependencies(spaceId, semanticDetail.name)); }
    catch (e: any) { showError(`Dependency analysis failed: ${e.message}`); }
    finally { setSemanticDepsLoading(false); }
  };

  // A manual call always attempts a fetch (retries a previously failed doc).
  // The auto-retry loop is prevented in the eager-load effect, which skips
  // keys in overviewLoadFailed — not here.
  const loadOverviewDoc = async (docKey: string) => {
    if (!spaceId || overviewDocs[docKey] || overviewLoading[docKey]) return;
    setOverviewLoading((prev) => ({ ...prev, [docKey]: true }));
    try {
      const content = await api.getOverviewDocument(spaceId, docKey);
      setOverviewDocs((prev) => ({ ...prev, [docKey]: content }));
      setOverviewLoadFailed((prev) => {
        if (!prev[docKey]) return prev;
        const { [docKey]: _removed, ...rest } = prev;
        return rest;
      });
    } catch (e: any) {
      setOverviewLoadFailed((prev) => ({ ...prev, [docKey]: true }));
      showError(`Failed to load overview document: ${e.message}`);
    } finally {
      setOverviewLoading((prev) => ({ ...prev, [docKey]: false }));
    }
  };

  const overviewDocDefs = status?.space_overview_docs ?? [];
  const overviewGenerating = overviewSubmitting || status?.overview_status === "in_progress";
  const allOverviewDocsLoaded =
    overviewDocDefs.length > 0 && overviewDocDefs.every((d) => overviewDocs[d.key]);
  // One combined Markdown document: title + each section under its own heading.
  // Docs carry no top-level H1 (stripped defensively), so headings don't double up.
  const combinedOverviewMd = allOverviewDocsLoaded
    ? [
        `# ${space?.space_name || spaceId} — Space Overview`,
        "",
        ...overviewDocDefs.flatMap((d) => [
          `## ${d.title}`,
          "",
          stripLeadingTitleHeading(overviewDocs[d.key]).trim(),
          "",
        ]),
      ].join("\n")
    : "";

  // Generate (or regenerate from scratch) the Space Overview. Clears any
  // locally-cached doc content so freshly generated docs are re-fetched.
  const generateOverview = async () => {
    if (!spaceId || overviewGenerating || isRefreshing) return;
    setOverviewSubmitting(true);
    try {
      await api.generateOverview(spaceId);
      setOverviewDocs({});
      setOverviewLoadFailed({});
      setActiveOverviewDoc(null);
      await loadStatus();
    } catch (e: any) {
      showError(`Failed to start overview generation: ${e.message}`);
    } finally {
      setOverviewSubmitting(false);
    }
  };

  // Auto-select first overview doc when docs are available and none is selected.
  // Skip entirely while (re)generating: the catalog may briefly still reference
  // documents that have just been deleted server-side, and fetching them would
  // 404. Once generation completes and the catalog repopulates, this re-runs.
  useEffect(() => {
    if (overviewGenerating) return;
    if (overviewDocDefs.length > 0 && !activeOverviewDoc) {
      setActiveOverviewDoc(overviewDocDefs[0].key);
    }
  }, [overviewGenerating, overviewDocDefs, activeOverviewDoc]);

  // Eager-load every overview doc so switching cards is instant and "Copy as
  // Markdown" can assemble the full document without a fetch.
  useEffect(() => {
    if (overviewGenerating) return;
    for (const doc of overviewDocDefs) {
      if (!overviewDocs[doc.key] && !overviewLoading[doc.key] && !overviewLoadFailed[doc.key]) {
        loadOverviewDoc(doc.key);
      }
    }
  }, [overviewGenerating, overviewDocDefs, overviewDocs, overviewLoading, overviewLoadFailed]);

  // Poll status while overview is generating
  useEffect(() => {
    if (!overviewGenerating || accessError) return;
    const interval = setInterval(async () => {
      await loadStatus();
    }, 5000);
    return () => clearInterval(interval);
  }, [overviewGenerating, loadStatus, accessError]);

  if (loading) {
    return <Box textAlign="center" padding="xxl"><Spinner size="large" /></Box>;
  }

  // A sticky read denial: show the reason (plus a reconnect link if the token
  // expired) instead of the space. Polling has stopped; transient 503s don't reach here.
  if (accessError) {
    const expired = accessError.isExpiredCredentials;
    return (
      <SpaceBetween size="l">
        <Breadcrumbs spaceId={spaceId!} navigate={navigate} />
        <Alert
          type="error"
          header={expired ? "Your git token has expired" : "You don't have access to this space"}
          action={
            expired ? (
              <Button onClick={() => navigate(GIT_CREDS_PATH)}>Git credentials</Button>
            ) : undefined
          }
        >
          {accessError.message}
        </Alert>
      </SpaceBetween>
    );
  }

  // Transient 503 on the status endpoint: show a retryable banner (the effect
  // above auto-retries) instead of falling through to the "not found" view.
  if (statusUnavailable && !status) {
    return (
      <SpaceBetween size="l">
        <Breadcrumbs spaceId={spaceId!} navigate={navigate} />
        <Alert
          type="warning"
          header="Couldn't check your git permissions"
          action={<Button onClick={() => loadStatus()}>Retry</Button>}
        >
          We couldn't reach your git provider to load this space. Retrying automatically…
        </Alert>
      </SpaceBetween>
    );
  }

  const currentStatus = status?.status ?? "NOT_FOUND";

  const deleteModal = (
    <Modal
      visible={deleteVisible}
      onDismiss={() => setDeleteVisible(false)}
      header="Delete space"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => setDeleteVisible(false)}>Cancel</Button>
            <Button variant="primary" loading={deleting} onClick={handleConfirmDelete}>
              Delete
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      Delete space <Box variant="code">{space?.space_id ?? spaceId}</Box>?

      This will delete all associated data including component catalog,
      local copy of source code and vectorized function summaries.

      This action cannot be undone.
    </Modal>
  );

  const refreshConfirmModal = (
    <Modal
      visible={refreshConfirmVisible}
      onDismiss={() => setRefreshConfirmVisible(false)}
      header="Exclusion patterns have changed"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => setRefreshConfirmVisible(false)}>Cancel</Button>
            <Button
              variant="primary"
              onClick={() => {
                setRefreshConfirmVisible(false);
                handleRefresh();
              }}
            >
              Refresh with original patterns
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      The system-wide exclusion patterns have changed since this space was created. A space's
      exclusion patterns are fixed for its lifetime, so refresh keeps applying the patterns it was
      created with. To use the current system-wide patterns, delete this space and create a new one.
    </Modal>
  );

  // ── Deleting ─────────────────────────────────────────────────────────
  if (currentStatus === "DELETING") {
    return (
      <SpaceBetween size="l">
        <Breadcrumbs spaceId={spaceId!} navigate={navigate} />
        <Flashbar items={flash} />
        <Header variant="h1" description={status?.space_name ? spaceId : undefined}>
          {status?.space_name || spaceId}
        </Header>
        <Container header={<Header variant="h2">Deleting space</Header>}>
          <SpaceBetween size="xs">
            <StatusIndicator type="in-progress">
              Deleting space and all associated data...
            </StatusIndicator>
            <Box color="text-body-secondary" fontSize="body-s">
              This page refreshes automatically every 5 seconds. You'll be
              returned to the space list once deletion completes.
            </Box>
          </SpaceBetween>
        </Container>
      </SpaceBetween>
    );
  }

  // ── Analyzing ────────────────────────────────────────────────────────
  // Refresh uses the Ready view; it lands here only with no snapshot to show.
  const refreshWithoutSnapshot = isRefreshing && !space;
  if (
    currentStatus === "ANALYZING" ||
    currentStatus === "SUBMITTED" ||
    currentStatus === "SUMMARIZING" ||
    refreshWithoutSnapshot
  ) {
    // Refresh steps live in the latest refresh run, analysis in pipeline_steps.
    const stepOrder = refreshWithoutSnapshot ? REFRESH_STEP_ORDER : PIPELINE_STEP_ORDER;
    const steps = refreshWithoutSnapshot
      ? (status?.refresh_pipeline_runs ?? []).slice(-1)[0]?.steps ?? []
      : status?.pipeline_steps ?? [];
    return (
      <SpaceBetween size="l">
        <Breadcrumbs spaceId={spaceId!} navigate={navigate} />
        <Flashbar items={flash} />
        <Header variant="h1" description={status?.space_name ? spaceId : undefined}>
          {status?.space_name || spaceId}
        </Header>
        <Container header={<Header variant="h2">{refreshWithoutSnapshot ? "Refresh in progress" : "Analysis in progress"}</Header>}>
          <SpaceBetween size="l">
            <SpaceBetween size="xs">
              <StatusIndicator type="in-progress">
                {status?.current_step
                  ? STEP_LABELS[status.current_step] ?? status.current_step
                  : refreshWithoutSnapshot
                  ? "Starting refresh..."
                  : "Starting analysis..."}
              </StatusIndicator>
              {(status?.current_step === "summarize" || status?.current_step === "summarize_changed") && status.summarize_total != null && status.summarize_total > 0 && (
                <ProgressBar
                  value={status.summarize_percent ?? 0}
                  description={`Summarized ${status.summarize_done ?? 0} of ${status.summarize_total} functions`}
                  additionalInfo="Early progress is slower by design — the deepest functions are summarized individually first, then shallower ones in fast batches."
                  label="AI Summarization"
                  resultText={`Summarized ${status.summarize_total} functions`}
                />
              )}
              {status?.business_context && (
                <BusinessContextPanel
                  context={status.business_context}
                  statistics={status.statistics}
                  omitLowBusinessRelevance={status.omit_low_business_relevance}
                />
              )}
              <Box color="text-body-secondary" fontSize="body-s">
                This page refreshes automatically every 5 seconds.
              </Box>
            </SpaceBetween>
            {status?.git_url && (
              <KeyValuePairs columns={2} items={[
                { label: "Repository", value: <Box variant="code">{status.git_url}</Box> },
                { label: "Branch", value: <Box variant="code">{status.branch ?? "main"}</Box> },
                ...(status.git_commit_hash
                  ? [{ label: "Commit", value: <Box variant="code">{status.git_commit_hash.slice(0, 12)}</Box> }]
                  : []),
                ...(status.created_by ? [{ label: "Created by", value: status.created_by }] : []),
                // started_at is set at creation; created_at only lands at READY, so fall back.
                // Rendered in UTC to match the Pipeline steps table below.
                ...(status.started_at || status.created_at
                  ? [{
                      label: "Created",
                      value: new Date((status.started_at ?? status.created_at)!).toLocaleString([], {
                        year: "numeric",
                        month: "short",
                        day: "2-digit",
                        hour: "2-digit",
                        minute: "2-digit",
                        timeZone: "UTC",
                      }) + " UTC",
                    }]
                  : []),
              ]} />
            )}
            {status?.statistics &&
              (status.statistics.total_lines != null || status.statistics.files_analyzed != null) && (
                <SpaceBetween size="xs">
                  <Box color="text-body-secondary" fontSize="body-s">
                    Repository scale — larger codebases take longer to summarize.
                  </Box>
                  <KeyValuePairs columns={3} items={[
                    {
                      label: "Lines of code",
                      value: status.statistics.total_lines != null
                        ? Number(status.statistics.total_lines).toLocaleString()
                        : "—",
                    },
                    { label: "Files analyzed", value: status.statistics.files_analyzed ?? "—" },
                    {
                      label: "Languages",
                      value: Object.keys(status.statistics.components_by_language ?? {}).join(", ") || "—",
                    },
                  ]} />
                </SpaceBetween>
              )}
            {steps.length > 0 && (
              <Table
                items={buildPipelineItems(steps, stepOrder, status?.current_step)}
                trackBy="step"
                header={<Header variant="h3">Pipeline steps</Header>}
                columnDefinitions={pipelineColumnDefs}
              />
            )}
          </SpaceBetween>
        </Container>
      </SpaceBetween>
    );
  }

  // ── Failed ───────────────────────────────────────────────────────────
  if (currentStatus === "FAILED") {
    return (
      <SpaceBetween size="l">
        <Breadcrumbs spaceId={spaceId!} navigate={navigate} />
        <Flashbar items={flash} />
        <Header variant="h1" info={<StatusIndicator type="error">Failed</StatusIndicator>}
          description={status?.space_name ? spaceId : undefined}
          actions={<SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => navigate("/")}>Back to spaces</Button>
            <Button onClick={() => setDeleteVisible(true)}>Delete</Button>
          </SpaceBetween>}>
          {status?.space_name || spaceId}
        </Header>
        <Container header={<Header variant="h2">Analysis failed</Header>}>
          <SpaceBetween size="m">
            {status?.error ? (
              <Box variant="p">
                <span style={{ whiteSpace: "pre-wrap" }}>{status.error}</span>
              </Box>
            ) : (
              <Box variant="p">
                The analysis pipeline encountered an error. Check the service
                logs for details, or try creating the space again.
              </Box>
            )}
          </SpaceBetween>
        </Container>
        {deleteModal}
      </SpaceBetween>
    );
  }

  // ── Not found ────────────────────────────────────────────────────────
  if (currentStatus === "NOT_FOUND" && !space) {
    return (
      <SpaceBetween size="l">
        <Breadcrumbs spaceId={spaceId!} navigate={navigate} />
        <Box textAlign="center" padding="xxl">
          <b>Space not found</b>
          <Box padding={{ top: "s" }}><Button onClick={() => navigate("/")}>Back to spaces</Button></Box>
        </Box>
      </SpaceBetween>
    );
  }

  // ── Waiting for space data to load ───────────────────────────────
  if (!space) {
    // Transient 503 loading the detail: show a retryable banner (the effect above
    // also auto-retries) rather than a spinner that never resolves.
    if (spaceUnavailable) {
      return (
        <SpaceBetween size="l">
          <Breadcrumbs spaceId={spaceId!} navigate={navigate} />
          <Alert
            type="warning"
            header="Couldn't check your git permissions"
            action={<Button onClick={() => loadSpace()}>Retry</Button>}
          >
            We couldn't reach your git provider to load this space. Retrying automatically…
          </Alert>
        </SpaceBetween>
      );
    }
    return <Box textAlign="center" padding="xxl"><Spinner size="large" /></Box>;
  }

  // ── Ready ────────────────────────────────────────────────────────────
  const stats = space?.statistics || {};
  const byType = stats.components_by_type || {};
  const byLang = stats.components_by_language || {};
  const languages = Object.keys(byLang);
  // Files CodeKB skipped because their language isn't supported yet (e.g.
  // Swift). Stored as { language: count } in the space statistics.
  const skippedByLang: Record<string, number> = stats.skipped_files || {};
  const skippedLangs = Object.keys(skippedByLang).sort(
    (a, b) => skippedByLang[b] - skippedByLang[a]
  );
  const skippedCount =
    stats.skipped_file_count ??
    skippedLangs.reduce((sum, l) => sum + (skippedByLang[l] || 0), 0);
  const skippedSummary = skippedLangs
    .map((l) => `${l} (${skippedByLang[l]})`)
    .join(", ");
  // Business relevance is only meaningful for spaces analyzed with the
  // "business relevance scoring optimization" turned on (persisted as
  // omit_low_business_relevance in the space status). Hide it everywhere
  // otherwise, since the stored scores are not trustworthy/intended.
  const showBusinessRelevance = status?.omit_low_business_relevance === true;

  // Staleness and refresh error banners
  const hasStaleOverviews = (status?.space_overview_docs ?? []).some((doc) => doc.stale === true);
  const refreshErrors = status?.refresh_errors ?? [];
  const refreshWarning = status?.refresh_warning;
  const refreshResult = status?.refresh_result;

  // An Alert, not a Flashbar item: the in-progress Flashbar is a solid blue
  // fill, which makes the nested progress bar and step table unreadable.
  const liveRefreshRun = (status?.refresh_pipeline_runs ?? []).slice(-1)[0];
  const refreshBanner = isRefreshing ? (
    <Alert type="info" header="Refresh in progress">
      <SpaceBetween size="s">
        <StatusIndicator type="in-progress">
          {status?.current_step
            ? STEP_LABELS[status.current_step] ?? status.current_step
            : "Starting refresh..."}
        </StatusIndicator>
        <Box color="text-body-secondary" fontSize="body-s">
          Showing the last indexed snapshot
          {space!.git_commit_hash ? ` (${space!.git_commit_hash.slice(0, 7)})` : ""}. Semantic
          search may return incomplete results until the refresh commits.
        </Box>
        {status?.current_step === "summarize_changed" &&
          status.summarize_total != null &&
          status.summarize_total > 0 && (
            <ProgressBar
              value={status.summarize_percent ?? 0}
              description={`Summarized ${status.summarize_done ?? 0} of ${status.summarize_total} changed functions`}
              label="AI Summarization"
            />
          )}
        {(liveRefreshRun?.steps ?? []).length > 0 && (
          <ExpandableSection headerText="Refresh steps" variant="footer">
            <Table
              items={buildPipelineItems(
                liveRefreshRun?.steps ?? [],
                REFRESH_STEP_ORDER,
                status?.current_step,
              )}
              trackBy="step"
              columnDefinitions={pipelineColumnDefs}
              variant="embedded"
            />
          </ExpandableSection>
        )}
      </SpaceBetween>
    </Alert>
  ) : null;

  const bannerItems: FlashbarProps.MessageDefinition[] = [];
  if (refreshWarning) {
    bannerItems.push({
      type: "warning",
      content: refreshWarning,
      dismissible: true,
      onDismiss: () => {/* allow dismiss */},
      id: "refresh-warning",
    });
  }
  if (hasStaleOverviews) {
    bannerItems.push({
      type: "warning",
      content: "One or more Space Overview documents are out of date following a recent refresh.",
      action: (
        <Button
          disabled={!overviewEnabled || isRefreshing}
          disabledReason={
            !overviewEnabled
              ? "Overview generation is disabled on this instance."
              : undefined
          }
          onClick={async () => {
            if (!spaceId) return;
            try {
              await api.generateOverview(spaceId);
              await loadStatus();
            } catch (e: any) {
              showError(`Failed to start overview generation: ${e.message}`);
            }
          }}
        >
          Regenerate overview
        </Button>
      ),
      dismissible: true,
      onDismiss: () => {/* allow dismiss */},
      id: "stale-overviews",
    });
  }
  if (refreshErrors.length > 0) {
    bannerItems.push({
      type: "warning",
      content: (
        <SpaceBetween size="xs">
          <span>Some vector operations failed during the last refresh ({refreshErrors.length} component{refreshErrors.length > 1 ? "s" : ""} affected).</span>
          <ExpandableSection headerText="Show affected component IDs" variant="footer">
            <SpaceBetween size="xxs">
              {refreshErrors.map((id) => (
                <Box key={id} variant="code" fontSize="body-s">{id}</Box>
              ))}
            </SpaceBetween>
          </ExpandableSection>
        </SpaceBetween>
      ),
      dismissible: true,
      onDismiss: () => {/* allow dismiss */},
      id: "refresh-errors",
    });
  }

  const refreshFailedBanner = isRefreshFailed ? (
    <Alert type="error" header="Refresh failed">
      <SpaceBetween size="s">
        <Box variant="p">
          Search may return partly updated results until a refresh completes. Check the
          error below, then use Refresh space to finish.
        </Box>
        {status?.error && (
          <Box variant="p">
            <span style={{ whiteSpace: "pre-wrap" }}>{status.error}</span>
          </Box>
        )}
        {(liveRefreshRun?.steps ?? []).length > 0 && (
          <ExpandableSection headerText="Refresh steps" variant="footer">
            <Table
              items={buildPipelineItems(liveRefreshRun?.steps ?? [], REFRESH_STEP_ORDER)}
              trackBy="step"
              columnDefinitions={pipelineColumnDefs}
              variant="embedded"
            />
          </ExpandableSection>
        )}
      </SpaceBetween>
    </Alert>
  ) : null;

  return (
    <SpaceBetween size="l">
      <Breadcrumbs spaceId={spaceId!} navigate={navigate} />
      <Flashbar items={[...flash, ...bannerItems]} />

      <Header variant="h1"
        info={
          <SpaceBetween direction="horizontal" size="xs">
            {isRefreshing
              ? <StatusIndicator type="in-progress">Refreshing</StatusIndicator>
              : isRefreshFailed
              ? <StatusIndicator type="error">Refresh failed</StatusIndicator>
              : <StatusIndicator type="success">Ready</StatusIndicator>}
            {space!.visibility && (
              <Badge color={space!.visibility === "public" ? "blue" : "grey"}>
                {space!.visibility === "public" ? "Public" : "Private"}
              </Badge>
            )}
          </SpaceBetween>
        }
        description={(space!.space_name || status?.space_name) ? space!.space_id : undefined}
        actions={<SpaceBetween direction="horizontal" size="xs">
          <Button onClick={() => navigate("/")}>Back to spaces</Button>
          {status?.git_url != null && (
            <Button
              loading={refreshing || isRefreshing}
              disabled={refreshing || isRefreshing}
              onClick={requestRefresh}
            >
              Refresh space
            </Button>
          )}
          <Button onClick={() => setDeleteVisible(true)} disabled={isRefreshing}>Delete</Button>
        </SpaceBetween>}>
        {space!.space_name || status?.space_name || space!.space_id}
      </Header>

      {refreshBanner}
      {refreshFailedBanner}

      {/* Last refresh summary */}
      {refreshResult && (
        <Container header={<Header variant="h3">Last refresh</Header>}>
          <ColumnLayout columns={4} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Files</Box>
              <Box variant="p">
                {refreshResult.files_added} added, {refreshResult.files_modified} modified, {refreshResult.files_removed} removed
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Components</Box>
              <Box variant="p">
                {refreshResult.components_added} added, {refreshResult.components_removed} removed, {refreshResult.components_reprocessed} reprocessed
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Vectors</Box>
              <Box variant="p">
                {refreshResult.vectors_upserted} upserted, {refreshResult.vectors_deleted} deleted
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Duration</Box>
              <Box variant="p">
                {refreshResult.duration_s < 60
                  ? `${Math.round(refreshResult.duration_s)}s`
                  : `${Math.floor(refreshResult.duration_s / 60)}m ${Math.round(refreshResult.duration_s % 60)}s`}
              </Box>
            </div>
          </ColumnLayout>
        </Container>
      )}

      {/* Primary features — tabs first */}
      <Tabs
        activeTabId={activeTabId}
        onChange={({ detail }) => {
          setActiveTabId(detail.activeTabId);
          setSearchParams(
            (prev) => {
              prev.set("tab", detail.activeTabId);
              return prev;
            },
            { replace: true },
          );
        }}
        tabs={[
        {
          label: "Codebase",
          id: "codebase",
          content: (
            <SpaceBetween size="l">
              {skippedCount > 0 && (
                <Alert type="info" header="Some files weren't analyzed">
                  CodeKB skipped {skippedCount} file{skippedCount === 1 ? "" : "s"} written
                  in languages it doesn't support yet: {skippedSummary}. Everything in a
                  supported language was analyzed normally.
                </Alert>
              )}
              <Container header={<Header variant="h2">Codebase statistics</Header>}>
                <SpaceBetween size="l">
                  <ColumnLayout columns={3} variant="text-grid">
                    <StatBox label="Components" value={space!.component_count} detail="Functions, classes, methods" />
                    <StatBox label="Lines of code" value={stats.total_lines ? Number(stats.total_lines).toLocaleString() : "—"} detail="Total lines across all files" />
                    <StatBox label="Files analyzed" value={stats.files_analyzed ?? "—"} detail={`Across ${languages.length} language(s)`} />
                  </ColumnLayout>
                  <ColumnLayout columns={3} variant="text-grid">
                    <StatBox label="Vectors" value={space!.vector_count} detail="Summarized components" />
                    <StatBox label="Languages" value={languages.length} detail={languages.join(", ") || "—"} />
                    <StatBox label="Skipped files" value={skippedCount || "—"} detail={skippedCount > 0 ? `Unsupported: ${skippedLangs.join(", ")}` : "All supported files analyzed"} />
                  </ColumnLayout>
                </SpaceBetween>
              </Container>

              {/* Space details */}
              <ExpandableSection variant="container" headerText="Space details"
                headerDescription="Space ID, git URL, version, and other metadata">
                <KeyValuePairs columns={2} items={[
                  { label: "Space ID", value: <Box variant="code">{space!.space_id}</Box> },
                  { label: "Git URL", value: space!.git_url ? <Box variant="code">{space!.git_url}</Box> : "—" },
                  { label: "Commit hash", value: space!.git_commit_hash ? <Box variant="code">{space!.git_commit_hash}</Box> : "—" },
                  { label: "Source directory", value: <Box variant="code">{space!.source_directory}</Box> },
                  { label: "Version", value: space!.version },
                  {
                    label: "Visibility",
                    value: space!.visibility ? (
                      <Badge color={space!.visibility === "public" ? "blue" : "grey"}>
                        {space!.visibility === "public" ? "Public" : "Private"}
                      </Badge>
                    ) : (
                      "—"
                    ),
                  },
                  { label: "Created by", value: space!.created_by || "—" },
                  {
                    label: "Bedrock model",
                    value: status?.bedrock_model?.model_id
                      ? <Box variant="code">{status.bedrock_model.model_id}</Box>
                      : "—",
                  },
                  {
                    label: "Bedrock region",
                    value: status?.bedrock_model?.region
                      ? <Box variant="code">{status.bedrock_model.region}</Box>
                      : "—",
                  },
                  {
                    label: "Created at",
                    value: space!.created_at
                      ? new Date(space!.created_at).toLocaleString(undefined, {
                          year: "numeric",
                          month: "short",
                          day: "2-digit",
                          hour: "2-digit",
                          minute: "2-digit",
                        })
                      : "—",
                  },
                  { label: "Languages detected", value: <SpaceBetween direction="horizontal" size="xs">{languages.map((l) => <Badge key={l} color="blue">{l}</Badge>)}</SpaceBetween> },
                  { label: "Components by type", value: <SpaceBetween size="xxs">{Object.entries(byType).map(([t, c]) => <div key={t}>{t}: {c as number}</div>)}</SpaceBetween> },
                  ...(skippedCount > 0 ? [{
                    label: "Skipped (unsupported languages)",
                    value: <SpaceBetween direction="horizontal" size="xs">{skippedLangs.map((l) => <Badge key={l} color="grey">{l}: {skippedByLang[l]}</Badge>)}</SpaceBetween>,
                  }] : []),
                ]} />
              </ExpandableSection>

              <TokenConsumptionTable
                usage={status?.token_usage}
                pricingUrl={status?.pricing_url}
                model={status?.bedrock_model}
              />

              {/* Pipeline history: initial space creation + each space refresh, each a closable window */}
              {((status?.pipeline_steps ?? []).length > 0 ||
                (status?.refresh_pipeline_runs ?? []).length > 0) && (
                <ExpandableSection
                  variant="container"
                  headerText="Analysis pipeline"
                  headerDescription="Timing and status of each pipeline run: initial space creation and every space refresh"
                >
                  <SpaceBetween size="xs">
                    {/* Newest refresh first; chronological #N numbering preserved */}
                    {(status?.refresh_pipeline_runs ?? [])
                      .map((run, i) => ({ run, num: i + 1 }))
                      .reverse()
                      .map(({ run, num }, idx) => {
                        const when = fmtWhenShort(run.started_at) ?? fmtRunStart(run.steps ?? []);
                        return (
                        <ExpandableSection
                          key={run.started_at ?? num}
                          headerText={when ? `Space refresh - ${num} (${when})` : `Space refresh - ${num}`}
                          defaultExpanded={idx === 0}
                        >
                          <Table
                            items={buildPipelineItems(run.steps ?? [], REFRESH_STEP_ORDER)}
                            trackBy="step"
                            columnDefinitions={pipelineColumnDefs}
                          />
                        </ExpandableSection>
                        );
                      })}
                    {/* Oldest event (initial creation) at the bottom */}
                    {(status?.pipeline_steps ?? []).length > 0 && (
                      <ExpandableSection
                        headerText={(() => {
                          const when = fmtRunStart(status?.pipeline_steps ?? []);
                          return when
                            ? `Initial space creation (${when})`
                            : "Initial space creation";
                        })()}
                        defaultExpanded={(status?.refresh_pipeline_runs ?? []).length === 0}
                      >
                        <Table
                          items={buildPipelineItems(status?.pipeline_steps ?? [], PIPELINE_STEP_ORDER)}
                          trackBy="step"
                          columnDefinitions={pipelineColumnDefs}
                        />
                      </ExpandableSection>
                    )}
                  </SpaceBetween>
                </ExpandableSection>
              )}
              {status?.business_context && (
                <BusinessContextPanel
                  context={status.business_context}
                  statistics={status.statistics ?? stats}
                  omitLowBusinessRelevance={status.omit_low_business_relevance}
                />
              )}

              {/* Exclusion patterns (fixed when the space was created) */}
              <ExpandableSection variant="container" headerText="Exclusion patterns"
                headerCounter={`(${(space!.exclude_patterns ?? []).length})`}
                headerDescription="Files and folders matching these patterns are not indexed in this space">
                <Box variant="pre">{(space!.exclude_patterns ?? []).join("\n")}</Box>
              </ExpandableSection>
            </SpaceBetween>
          ),
        },
        {
          label: "Space Overview",
          id: "overview-docs",
          content: overviewDocDefs.length > 0 ? (
            <SpaceBetween size="l">
              <Header
                variant="h2"
                description="AI-generated documentation summarizing this codebase. Regenerating deletes the current documents and rebuilds them from scratch."
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
                      disabled={!overviewEnabled || overviewGenerating || isRefreshing}
                      disabledReason={
                        !overviewEnabled
                          ? "Overview generation is disabled on this instance."
                          : undefined
                      }
                      onClick={generateOverview}
                    >
                      Regenerate
                    </Button>
                  </SpaceBetween>
                }
              >
                Space Overview
              </Header>
              {overviewGenerating && (
                <StatusIndicator type="in-progress">
                  Regenerating Space Overview… existing documents are being rebuilt from scratch.
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
                      onMouseEnter={(e) => {
                        if (!isActive) {
                          e.currentTarget.style.boxShadow = "0 3px 10px rgba(0,0,0,0.12)";
                          e.currentTarget.style.borderColor = "var(--color-border-control-focus, #0972d3)";
                        }
                      }}
                      onMouseLeave={(e) => {
                        if (!isActive) {
                          e.currentTarget.style.boxShadow = "0 1px 4px rgba(0,0,0,0.06)";
                          e.currentTarget.style.borderColor = "var(--color-border-divider-default, #e9ebed)";
                        }
                      }}
                    >
                      <SpaceBetween size="xxs">
                        <Box textAlign="center">
                          {isLoading ? <Spinner size="normal" /> : <Icon name={DOC_ICONS[doc.key] as any ?? "file"} size="medium" />}
                        </Box>
                        <Box variant="awsui-key-label" fontSize="body-s">{doc.title}</Box>
                      </SpaceBetween>
                    </div>
                  );
                })}
              </div>

              {/* Document content */}
              {activeOverviewDoc && (
                <Container header={<Header variant="h2">{overviewDocDefs.find((d) => d.key === activeOverviewDoc)?.title ?? activeOverviewDoc}</Header>}>
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
                    <Box textAlign="center" padding="xxl"><Spinner size="large" /></Box>
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
                  description="Generate AI-powered documentation that summarizes your codebase architecture, components, and data flows."
                  actions={
                    !overviewGenerating ? (
                      <Button
                        variant="primary"
                        iconName="gen-ai"
                        loading={overviewSubmitting}
                        disabled={!overviewEnabled || overviewSubmitting || isRefreshing}
                        disabledReason={
                          !overviewEnabled
                            ? "Overview generation is disabled on this instance."
                            : undefined
                        }
                        onClick={generateOverview}
                      >
                        Generate overview
                      </Button>
                    ) : undefined
                  }
                >
                  Space Overview
                </Header>
              }
            >
              {overviewGenerating ? (
                <SpaceBetween size="s">
                  <StatusIndicator type="in-progress">
                    {(() => {
                      const done = status?.overview_done ?? 0;
                      const total = status?.overview_total ?? 0;
                      if (total === 0 || done === 0) return "Exploring codebase structure...";
                      const stepNames = ["Exploration", "Business Overview", "Application Architecture", "Technology Stack", "Deployment Architecture"];
                      const currentStep = stepNames[Math.min(done, stepNames.length - 1)] ?? `Step ${done}`;
                      return `Generating ${currentStep} (step ${done} of ${total})`;
                    })()}
                  </StatusIndicator>
                  <ProgressBar
                    value={status?.overview_percent ?? 0}
                    label="Overview generation"
                    variant="flash"
                  />
                  <Box color="text-body-secondary" fontSize="body-s">
                    This page refreshes automatically. The exploration phase may take several minutes before document generation begins.
                  </Box>
                </SpaceBetween>
              ) : (
                <Box color="text-body-secondary" fontSize="body-s">
                  The overview uses AI to analyze your knowledge base and produce documents such as an architecture summary, component catalog, and data flow diagrams. Generation time and cost scale with codebase size.
                </Box>
              )}
            </Container>
          ),
        },
        {
          label: "Syntactic Search",
          id: "components",
          content: (
            <SpaceBetween size="l">
              <Table loading={compLoading} loadingText="Searching..." items={components} trackBy="id"
                empty={<Box textAlign="center" padding="l" color="inherit">
                  {searchedPattern === null ? "Run a search to see components"
                    : searchedPattern ? `No components match "${searchedPattern}"` : "No components found"}
                </Box>}
                header={<Header counter={`(${components.length})`}>Syntactic Results</Header>}
                filter={
                  <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
                    <div style={{ flex: 1 }}>
                      <Input value={searchPattern} onChange={({ detail }) => setSearchPattern(detail.value)}
                        placeholder="Pattern (e.g. User*, *Service)" type="search"
                        onKeyDown={({ detail }) => { if (detail.key === "Enter") handleComponentSearch(); }} />
                    </div>
                    <div style={{ minWidth: "160px" }}>
                      <Select selectedOption={typeFilter} onChange={({ detail }) => setTypeFilter(detail.selectedOption as any)} options={TYPE_OPTIONS} />
                    </div>
                    <Button loading={compLoading} onClick={handleComponentSearch}>Search</Button>
                  </div>
                }
                columnDefinitions={[
                  { id: "name", header: "Name", cell: (item) => <Button variant="inline-link" onClick={() => handleSelectSyntactic(item.name)}>{item.display_name ?? item.name}</Button>, sortingField: "name" },
                  { id: "type", header: "Type", cell: (item) => item.type },
                  { id: "language", header: "Language", cell: (item) => item.language },
                  ...(showBusinessRelevance ? [{ id: "relevance", header: "Business relevance", cell: (item: Record<string, any>) => <BusinessRelevanceSummary behavior={item.business_behavior} /> }] : []),
                  { id: "file", header: "File", cell: (item) => <Box variant="code" fontSize="body-s">{item.file_path}</Box> },
                  { id: "lines", header: "Lines", cell: (item) => `${item.line_number}–${item.end_line_number}` },
                ]} />
              {syntacticDetail && (
                <div ref={syntacticDetailRef}>
                <ComponentDetail component={syntacticDetail} flowData={syntacticFlow} depsData={syntacticDeps}
                  loading={syntacticDetailLoading} flowLoading={syntacticFlowLoading} depsLoading={syntacticDepsLoading}
                  onTraceFlow={handleTraceSyntacticFlow} onShowDeps={handleShowSyntacticDeps}
                  showBusinessRelevance={showBusinessRelevance} />
                </div>
              )}
            </SpaceBetween>
          ),
        },
        {
          label: "Semantic Search",
          id: "semantic",
          content: (
            <SpaceBetween size="l">
            <Table loading={semanticLoading} loadingText="Searching vector store..." items={semanticResults} trackBy="key" wrapLines
              empty={<Box textAlign="center" padding="l" color="inherit">
                {searchedSemanticQuery === null ? "Enter a query to search the vector store"
                  : `No results for "${searchedSemanticQuery}"`}
              </Box>}
              header={<Header counter={`(${semanticResults.length})`}>Semantic results</Header>}
              filter={
                <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
                  <div style={{ flex: 1 }}>
                    <Input value={semanticQuery} onChange={({ detail }) => setSemanticQuery(detail.value)}
                      placeholder="Describe what you're looking for in natural language..." type="search"
                      onKeyDown={({ detail }) => { if (detail.key === "Enter") handleSemanticSearch(); }} />
                  </div>
                  <div style={{ minWidth: "80px", display: "flex", alignItems: "center", gap: "4px" }}>
                    <Box variant="awsui-key-label" fontSize="body-s">Top K</Box>
                    <Input value={semanticTopK} onChange={({ detail }) => { semanticTopKTouched.current = true; setSemanticTopK(detail.value); }}
                      type="number" inputMode="numeric" ariaLabel="Top K" />
                  </div>
                  <Button loading={semanticLoading} onClick={handleSemanticSearch}>Search vectors</Button>
                </div>
              }
              columnDefinitions={[
                { id: "key", header: "Component", cell: (item) => {
                  const name = item.key ?? "";
                  return <Button variant="inline-link" onClick={() => handleSelectSemantic(name)}>{item.display_name ?? name}</Button>;
                }},
                { id: "type", header: "Type", cell: (item) => item.metadata?.type ?? "—" },
                { id: "space", header: "Space", cell: (item) => item.metadata?.space_id ?? "—" },
                ...(showBusinessRelevance ? [{ id: "relevance", header: "Business relevance", cell: (item: Record<string, any>) => <BusinessRelevanceSummary behavior={item.metadata} /> }] : []),
                { id: "description", header: "Description", cell: (item) => { const d = item.metadata?.description ?? ""; return <Box variant="p" fontSize="body-s">{d}</Box>; } },
              ]} />
              {semanticDetail && (
                <div ref={semanticDetailRef}>
                <ComponentDetail component={semanticDetail} flowData={semanticFlow} depsData={semanticDeps}
                  loading={semanticDetailLoading} flowLoading={semanticFlowLoading} depsLoading={semanticDepsLoading}
                  onTraceFlow={handleTraceSemanticFlow} onShowDeps={handleShowSemanticDeps}
                  showBusinessRelevance={showBusinessRelevance} />
                </div>
              )}
            </SpaceBetween>
          ),
        },
      ]} />
      {deleteModal}
      {refreshConfirmModal}
    </SpaceBetween>
  );
}

// ── Sub-components ─────────────────────────────────────────────────────

function Breadcrumbs({ spaceId, navigate }: { spaceId: string; navigate: ReturnType<typeof useNavigate> }) {
  return (
    <BreadcrumbGroup items={[
      { text: "CodeKB", href: "/" },
      { text: "Spaces", href: "/" },
      { text: spaceId, href: `/spaces/${spaceId}` },
    ]} onFollow={(e) => { e.preventDefault(); navigate(e.detail.href); }} />
  );
}

function StatBox({ label, value, detail }: { label: string; value: any; detail: string }) {
  return <div><Box variant="awsui-key-label">{label}</Box><Box variant="h1">{value}</Box><Box color="text-body-secondary">{detail}</Box></div>;
}

interface TokenUsageRow extends UsagePart {
  id: string;
  operation: string;
  when?: string;
  detail?: string;
}

type TokenOp = "space_creation" | "space_overview" | "space_refresh";

const ALL_TOKEN_OPS: TokenOp[] = ["space_creation", "space_overview", "space_refresh"];

const TOKEN_OP_LABELS: Record<TokenOp, string> = {
  space_creation: "Space creation",
  space_overview: "Space Overview",
  space_refresh: "Space refresh",
};

/** Flatten persisted per-operation token usage into one row per event,
 * optionally limited to a subset of operations.
 *
 * Scales as the space accrues more operations: space creation is a single
 * row; Space Overview regenerations and (future) space refreshes contribute
 * one row each from their `recent` history. */
function buildTokenRows(
  usage?: SpaceTokenUsage,
  operations: TokenOp[] = ALL_TOKEN_OPS,
): TokenUsageRow[] {
  const rows: TokenUsageRow[] = [];
  const include = (op: TokenOp) => operations.includes(op);

  const creation = usage?.space_creation;
  if (include("space_creation") && creation?.totals && creation.totals.total > 0) {
    // One row per workload (function descriptions, business vocabulary).
    usageParts(creation).forEach((part, i) => {
      rows.push({
        ...part,
        id: `creation-${i}`,
        operation: TOKEN_OP_LABELS.space_creation,
        when: creation.generated_at,
      });
    });
  }

  const pushRecent = (
    rec: SpaceTokenUsage["space_overview"],
    op: TokenOp,
    prefix: string,
  ) => {
    if (!include(op)) return;
    (rec?.recent ?? []).forEach((entry, i) => {
      // Show every run in the history, including zero-token ones (e.g. a
      // no-op space refresh that re-summarized nothing).
      usageParts(entry).forEach((part, j) => {
        rows.push({
          ...part,
          id: `${prefix}-${entry.at ?? i}-${j}`,
          operation: TOKEN_OP_LABELS[op],
          when: entry.at,
          detail: entry.trigger,
        });
      });
    });
  };

  pushRecent(usage?.space_overview, "space_overview", "ov");
  pushRecent(usage?.space_refresh, "space_refresh", "rf");

  // Most recent first; undated rows sort last.
  return rows.sort((a, b) => (b.when ?? "").localeCompare(a.when ?? ""));
}

function fmtWhen(iso?: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  // Render in UTC for consistency with the pipeline step rows and backend logs.
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

/** Minute-precision UTC timestamp for run headers, e.g. "Jul 3, 2026, 01:42 PM UTC". */
function fmtWhenShort(iso?: string): string | undefined {
  if (!iso) return undefined;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return undefined;
  return (
    d.toLocaleString([], {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "UTC",
    }) + " UTC"
  );
}

const RECURRING_TOKEN_OPS = ["space_overview", "space_refresh"] as const;

/** Tokens of every run. The rows list only each operation's newest runs. */
function lifetimeTokenTotals(
  usage?: SpaceTokenUsage,
  operations: TokenOp[] = ALL_TOKEN_OPS,
): { input: number; output: number; total: number; thinkingTokens: number } {
  const parts = [
    operations.includes("space_creation") ? usage?.space_creation?.totals : undefined,
    ...RECURRING_TOKEN_OPS.map((op) =>
      operations.includes(op) ? usage?.[op]?.cumulative?.totals : undefined,
    ),
  ];
  const sum = { input: 0, output: 0, total: 0, thinkingTokens: 0 };
  for (const t of parts) {
    sum.input += t?.input ?? 0;
    sum.output += t?.output ?? 0;
    sum.total += t?.total ?? 0;
    sum.thinkingTokens += t?.thinking ?? 0;
  }
  return sum;
}

/** True when an operation has run more times than its listed history. */
function hasUnlistedRuns(
  usage?: SpaceTokenUsage,
  operations: TokenOp[] = ALL_TOKEN_OPS,
): boolean {
  return RECURRING_TOKEN_OPS.some((op) => {
    const rec = usage?.[op];
    return operations.includes(op) && (rec?.generation_count ?? 0) > (rec?.recent?.length ?? 0);
  });
}

/** Dedicated, scalable view of Bedrock token consumption per operation. */
function TokenConsumptionTable({
  usage,
  pricingUrl,
  model,
  operations,
  title = "Token consumption",
  description = "Bedrock LLM token consumption per operation (inference only). Costs are not computed — use the AWS Bedrock pricing page to estimate.",
  variant = "embedded",
}: {
  usage?: SpaceTokenUsage;
  pricingUrl?: string;
  model?: { model_id?: string; region?: string };
  operations?: TokenOp[];
  title?: string;
  description?: string;
  variant?: "container" | "embedded";
}) {
  const rows = buildTokenRows(usage, operations);
  if (rows.length === 0) return null;
  const regions = regionDisplay(rows, model?.region);
  const grand = lifetimeTokenTotals(usage, operations);
  const olderRunsHidden = hasUnlistedRuns(usage, operations);

  return (
    <ExpandableSection
      variant="container"
      headerText={title}
      headerCounter={`(${rows.length})`}
      headerDescription={description}
    >
      <SpaceBetween size="s">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", flexWrap: "wrap", gap: 8 }}>
          {/* The model is per row: each operation ran with its own, and the
              configured model can change between operations. */}
          {regions.header ? (
            <Box variant="small" color="text-body-secondary">
              Region <Box variant="code" display="inline">{regions.header}</Box>
            </Box>
          ) : <span />}
          {pricingUrl && (
            <Button href={pricingUrl} target="_blank" iconAlign="right" iconName="external">
              AWS Bedrock pricing
            </Button>
          )}
        </div>
        <Table
          variant={variant}
          items={rows}
          columnDefinitions={[
            {
              id: "operation",
              header: "Operation",
              cell: (r) => (
                <OperationCell
                  label={r.detail ? `${r.operation} (${r.detail})` : r.operation}
                  workload={r.workload}
                />
              ),
            },
            { id: "when", header: "When", cell: (r) => fmtWhen(r.when) },
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
            <SpaceBetween size="xxs">
              <Box textAlign="right" fontWeight="bold">
                {totalsText([grand])}
              </Box>
              {olderRunsHidden && (
                <Box textAlign="right" variant="small" color="text-body-secondary">
                  Rows show the most recent runs of each operation; the total includes all runs.
                </Box>
              )}
            </SpaceBetween>
          }
        />
      </SpaceBetween>
    </ExpandableSection>
  );
}

function BusinessContextPanel({
  context,
  statistics,
  omitLowBusinessRelevance,
}: {
  context: NonNullable<AnalysisStatus["business_context"]>;
  statistics?: Record<string, any>;
  omitLowBusinessRelevance?: boolean;
}) {
  const businessTerms = context.business_terms ?? [];
  const technicalTerms = context.technical_terms ?? [];
  const businessModules = context.business_modules ?? [];
  const entrypointPatterns = context.entrypoint_patterns ?? [];
  const totalTerms = businessTerms.length + technicalTerms.length + businessModules.length + entrypointPatterns.length;
  const tierCounts = relevanceTierCounts(statistics);

  return (
    <ExpandableSection
      variant="container"
      defaultExpanded={false}
      headerText="Repository business context"
      headerDescription={`${totalTerms} extracted item${totalTerms === 1 ? "" : "s"} · source: ${context.source ?? "unknown"}${context.confidence != null ? ` · confidence ${Math.round(context.confidence * 100)}%` : ""}`}
    >
      <SpaceBetween size="m">
        {context.error && (
          <StatusIndicator type="warning">Fallback used: {context.error}</StatusIndicator>
        )}
        {tierCounts.total > 0 && (
          <BusinessRelevanceTierCounts
            counts={tierCounts}
            omitLowBusinessRelevance={omitLowBusinessRelevance}
          />
        )}
        <ColumnLayout columns={2} variant="text-grid">
          <TermGroup title="Business terms" terms={businessTerms} />
          <TermGroup title="Technical terms" terms={technicalTerms} />
          <TermGroup title="Business modules" terms={businessModules} />
          <TermGroup title="Entrypoint patterns" terms={entrypointPatterns} />
        </ColumnLayout>
      </SpaceBetween>
    </ExpandableSection>
  );
}

function BusinessRelevanceTierCounts({
  counts,
  omitLowBusinessRelevance,
}: {
  counts: RelevanceTierCounts;
  omitLowBusinessRelevance?: boolean;
}) {
  const considered = omitLowBusinessRelevance
    ? counts.high + counts.medium + counts.heuristic
    : counts.total;

  return (
    <SpaceBetween size="s">
      <Box color="text-body-secondary">
        {considered} of {counts.total} functions/methods considered for AI indexing
        {omitLowBusinessRelevance ? " · low relevance omitted" : ""}
      </Box>
      <ColumnLayout columns={4} variant="text-grid">
        <StatBox label="High relevance" value={counts.high} detail="Score >= 80" />
        <StatBox label="Medium relevance" value={counts.medium} detail="Score 50-79" />
        <StatBox label="Heuristic" value={counts.heuristic} detail="Score 25-49" />
        <StatBox label="Low relevance" value={counts.low} detail="Score < 25" />
      </ColumnLayout>
    </SpaceBetween>
  );
}

type RelevanceTierCounts = {
  high: number;
  medium: number;
  heuristic: number;
  low: number;
  total: number;
};

function relevanceTierCounts(statistics?: Record<string, any>): RelevanceTierCounts {
  const raw =
    statistics?.business_relevance_function_method_tiers ??
    statistics?.business_relevance_tiers;
  const counts = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const high = numericCount(counts.high);
  const medium = numericCount(counts.medium);
  const heuristic = numericCount(counts.heuristic);
  const low = numericCount(counts.low);
  return { high, medium, heuristic, low, total: high + medium + heuristic + low };
}

function numericCount(value: unknown): number {
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) ? numeric : 0;
}

function TermGroup({ title, terms }: { title: string; terms: string[] }) {
  return (
    <div>
      <Box variant="awsui-key-label">{title}</Box>
      {terms.length > 0 ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "8px" }}>
          {terms.map((term) => <Badge key={term}>{term}</Badge>)}
        </div>
      ) : (
        <Box color="text-body-secondary">No terms extracted</Box>
      )}
    </div>
  );
}

function BusinessRelevanceSummary({ behavior }: { behavior?: Record<string, any> | null }) {
  const score = relevanceScore(behavior);
  const tier = relevanceTier(behavior);
  const reasons = relevanceReasons(behavior).slice(0, 2);
  if (score == null && !tier) return <Box color="text-body-secondary">—</Box>;

  return (
    <SpaceBetween size="xxs">
      {score != null && <Box variant="code">{score.toFixed(1)}</Box>}
      {tier && <Badge color={tierBadgeColor(tier)}>{tier}</Badge>}
      {reasons.map((reason) => (
        <Box key={reason} color="text-body-secondary" fontSize="body-s">{reason}</Box>
      ))}
    </SpaceBetween>
  );
}

function BusinessRelevanceDetails({ behavior }: { behavior?: Record<string, any> | null }) {
  const score = relevanceScore(behavior);
  const tier = relevanceTier(behavior);
  const reasons = relevanceReasons(behavior);
  if (score == null && !tier && reasons.length === 0) return null;

  return (
    <ExpandableSection
      headerText="Business relevance"
      headerDescription={`${score != null ? `Score ${score.toFixed(1)}` : "No score"}${tier ? ` · ${tier}` : ""}`}
      defaultExpanded
    >
      <SpaceBetween size="m">
        <ColumnLayout columns={2}>
          <div>
            <Box variant="awsui-key-label">Score</Box>
            <Box variant="h2">{score != null ? score.toFixed(1) : "—"}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Tier</Box>
            {tier ? <Badge color={tierBadgeColor(tier)}>{tier}</Badge> : <Box color="text-body-secondary">—</Box>}
          </div>
        </ColumnLayout>
        <div>
          <Box variant="awsui-key-label">Reasons</Box>
          {reasons.length > 0 ? (
            <SpaceBetween size="xxs">
              {reasons.map((reason) => <Box key={reason} fontSize="body-s">{reason}</Box>)}
            </SpaceBetween>
          ) : (
            <Box color="text-body-secondary">No reasons recorded</Box>
          )}
        </div>

      </SpaceBetween>
    </ExpandableSection>
  );
}

function SourceCodeViewer({ component }: { component: Record<string, any> }) {
  const source = String(component.source_code ?? "");
  const startLine = Number(component.line_number ?? 1);
  const lineCount = source ? source.split("\n").length : 0;
  const lineNumbers = Array.from({ length: lineCount }, (_, index) => startLine + index);
  const language = languageFromPath(component.file_path);

  return (
    <ExpandableSection
      headerText="View code"
      headerDescription={`${component.file_path ?? "source"}:${component.line_number ?? "?"}-${component.end_line_number ?? "?"}`}
      defaultExpanded={false}
    >
      <div className="source-viewer">
        <div className="source-viewer__toolbar">
          <Box variant="code" fontSize="body-s">{language ?? component.language ?? "text"}</Box>
          <CopyButton text={source} variant="inline-icon" label="Copy code" />
        </div>
        <div className="source-viewer__body">
          <pre className="source-viewer__gutter" aria-hidden="true">
            {lineNumbers.join("\n")}
          </pre>
          <CodeBlock code={source} language={language} className="source-viewer__code" />
        </div>
      </div>
    </ExpandableSection>
  );
}

function uniqueStrings(values?: unknown): string[] {
  if (!Array.isArray(values)) return [];
  const seen = new Set<string>();
  const unique: string[] = [];
  for (const value of values) {
    if (typeof value !== "string" || seen.has(value)) continue;
    seen.add(value);
    unique.push(value);
  }
  return unique;
}

function relevanceScore(behavior?: Record<string, any> | null): number | null {
  const value = behavior?.business_relevance_score;
  if (value == null || value === "") return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function relevanceTier(behavior?: Record<string, any> | null): string {
  return String(behavior?.business_relevance_tier ?? "").trim();
}

function relevanceReasons(behavior?: Record<string, any> | null): string[] {
  const reasons = behavior?.business_relevance_reasons;
  return Array.isArray(reasons) ? reasons.filter((reason) => typeof reason === "string") : [];
}

function tierBadgeColor(tier: string): "blue" | "green" | "grey" | "red" {
  switch (tier.toLowerCase()) {
    case "high":
      return "green";
    case "medium":
      return "blue";
    case "heuristic":
      return "grey";
    default:
      return "red";
  }
}

// ── Space Overview card helpers ──────────────────────────────────────

const DOC_ICONS: Record<string, string> = {
  business_overview: "contact",
  application_architecture: "share",
  technology_stack: "settings",
  deployment_architecture: "upload-download",
};

// ── Pipeline step helpers ───────────────────────────────────────────

const STEP_LABELS: Record<string, string> = {
  clone: "Clone repository",
  extract: "Extract upload",
  analyze: "Parse & build call graphs",
  business_context: "Extract business context",
  summarize: "Deep Semantic Analysis",
  save: "Save knowledge base",
  // Refresh pipeline steps
  unchanged: "No new commits, nothing to update",
  remediate_depth: "Prepare git history",
  refresh_diff: "Compute file diff",
  parse_changed: "Parse changed files",
  merge: "Merge components",
  rebuild_call_graph: "Rebuild call graph",
  summarize_changed: "Summarize changed components",
  stage_kb: "Stage knowledge base",
  vector_upserts: "Update vectors (upsert)",
  vector_deletes: "Update vectors (delete)",
  commit_kb: "Commit knowledge base",
  invalidate_caches: "Invalidate caches",
  mark_overviews_stale: "Mark overviews stale",
};

const PIPELINE_STEP_ORDER = ["clone", "extract", "analyze", "business_context", "summarize", "save"];
// vector_upserts only appears in older runs' history.
const REFRESH_STEP_ORDER = [
  "clone", "unchanged", "remediate_depth", "refresh_diff", "parse_changed", "merge",
  "rebuild_call_graph", "summarize_changed", "vector_upserts", "vector_deletes",
  "stage_kb", "commit_kb", "invalidate_caches", "mark_overviews_stale",
];

const pipelineColumnDefs = [
  {
    id: "step",
    header: "Step",
    cell: (item: { step: string; status: string }) => {
      const label = STEP_LABELS[item.step] ?? item.step;
      if (item.status === "completed") return <StatusIndicator type="success">{label}</StatusIndicator>;
      if (item.status === "failed") return <StatusIndicator type="error">{label}</StatusIndicator>;
      if (item.status === "in_progress") return <SpaceBetween direction="horizontal" size="xs"><Spinner size="normal" /><span>{label}</span></SpaceBetween>;
      return <StatusIndicator type="pending">{label}</StatusIndicator>;
    },
  },
  {
    id: "started",
    header: "Started",
    cell: (item: { started_at?: string }) => item.started_at ? formatTimestamp(item.started_at) : "—",
  },
  {
    id: "duration",
    header: "Duration",
    cell: (item: { started_at?: string; status: string; updated_at?: string }) =>
      formatDuration(item.started_at, item.status === "in_progress" ? undefined : item.updated_at),
  },
];

/** Formatted UTC start time of a pipeline run, derived from its earliest step.
 * ISO-8601 UTC strings sort chronologically, so the first is the start. */
function fmtRunStart(
  steps: NonNullable<AnalysisStatus["pipeline_steps"]>,
): string | undefined {
  const earliest = steps
    .map((s) => s.started_at)
    .filter((t): t is string => !!t)
    .sort()[0];
  return fmtWhenShort(earliest);
}

/** Project a pipeline step list into ordered rows for the pipeline table,
 * dropping steps that never ran. `currentStep` (in-flight runs only) marks
 * later steps "pending" instead. */
function buildPipelineItems(
  steps: NonNullable<AnalysisStatus["pipeline_steps"]>,
  order: string[],
  currentStep?: string,
) {
  const currentIdx = currentStep ? order.indexOf(currentStep) : -1;
  return order
    .map((stepKey, idx) => {
      const step = steps.find((s) => s.step === stepKey);
      const isPending = !step && currentIdx >= 0 && idx > currentIdx;
      return {
        step: stepKey,
        status: step?.status ?? (isPending ? "pending" : "unknown"),
        started_at: step?.started_at,
        updated_at: step?.updated_at,
      };
    })
    .filter((s) => s.status !== "unknown");
}

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "UTC" }) + " UTC";
  } catch {
    return iso;
  }
}

function formatDuration(startedAt?: string, endedAt?: string): string {
  if (!startedAt) return "—";
  const start = new Date(startedAt).getTime();
  const end = endedAt ? new Date(endedAt).getTime() : Date.now();
  const seconds = Math.round((end - start) / 1000);
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}m ${secs}s`;
}

function ComponentDetail({ component, flowData, depsData, loading, flowLoading, depsLoading, onTraceFlow, onShowDeps, showBusinessRelevance }: {
  component: Record<string, any>; flowData: Record<string, any> | null; depsData: Record<string, any> | null;
  loading: boolean; flowLoading: boolean; depsLoading: boolean; onTraceFlow: () => void; onShowDeps: () => void;
  showBusinessRelevance?: boolean;
}) {
  // Trace flow / dependency analysis only make sense for callable components.
  // For variables, constants, enums, etc. the backend returns an error, so
  // disable the buttons and explain why on hover.
  const CALLABLE_TYPES = ["function", "method", "constructor"];
  const isCallable = CALLABLE_TYPES.includes(String(component.type ?? "").toLowerCase());
  const nonCallableReason = `Not available for ${component.type ?? "this"} components — only functions, methods, and constructors can be traced.`;
  const directDependencies = uniqueStrings(depsData?.direct_dependencies);
  const dependents = uniqueStrings(depsData?.dependents);
  // UI-only labels keyed by component id. The backend attaches display_name
  // (single component) or a display_names id→label map (flow / deps). Ids stay
  // the lookup key everywhere; only the rendered label changes.
  const flowDisplayNames: Record<string, string> = flowData?.display_names ?? {};
  const depsDisplayNames: Record<string, string> = depsData?.display_names ?? {};

  return (
    <Container header={<Header variant="h2" actions={
      <SpaceBetween direction="horizontal" size="xs">
        <Button
          loading={flowLoading}
          disabled={!isCallable}
          disabledReason={!isCallable ? nonCallableReason : undefined}
          onClick={onTraceFlow}
        >
          Trace flow
        </Button>
        <Button
          loading={depsLoading}
          disabled={!isCallable}
          disabledReason={!isCallable ? nonCallableReason : undefined}
          onClick={onShowDeps}
        >
          Show dependencies
        </Button>
      </SpaceBetween>}>{component.display_name ?? component.name}</Header>}>
      <SpaceBetween size="l">
        <ColumnLayout columns={3}>
          <div><Box variant="awsui-key-label">Type</Box><div>{component.type}</div></div>
          <div><Box variant="awsui-key-label">Language</Box><div>{component.language}</div></div>
          <div><Box variant="awsui-key-label">Location</Box><Box variant="code">{component.file_path}:{component.line_number}</Box></div>
        </ColumnLayout>
        <BusinessRelevanceDetails behavior={showBusinessRelevance ? component.business_behavior : undefined} />
        {component.attributes?.documentation && <div><Box variant="awsui-key-label">Documentation</Box><Box variant="p">{component.attributes.documentation}</Box></div>}
        {component.business_behavior?.description && <div><Box variant="awsui-key-label">Business description</Box><Box variant="p">{component.business_behavior.description}</Box></div>}
        {component.source_code && (
          <SourceCodeViewer component={component} />
        )}
        {flowData && (
          <ExpandableSection headerText={`Execution flow (${flowData.flow_steps?.length ?? 0} steps)`} defaultExpanded>
            <div style={{ maxHeight: "400px", overflow: "auto", background: "#0f1b2a", borderRadius: "8px", padding: "16px" }}>
              <pre style={{ margin: 0, color: "#d1d5db", fontSize: "13px", fontFamily: "'Amazon Ember Mono', monospace" }}>
                {flowData.flow_steps?.map((s: any, i: number) => `${"  ".repeat(s.depth ?? 0)}${i + 1}. ${flowDisplayNames[s.caller] ?? s.caller} → ${flowDisplayNames[s.component] ?? s.component}`).join("\n") || "No flow steps"}
              </pre>
            </div>
          </ExpandableSection>
        )}
        {depsData && (
          <ExpandableSection headerText="Dependencies" defaultExpanded>
            <ColumnLayout columns={2}>
              <div>
                <Box variant="awsui-key-label">Direct dependencies ({directDependencies.length})</Box>
                <SpaceBetween size="xxs">
                  {directDependencies.map((d: string) => <Box key={d} variant="code" fontSize="body-s">{depsDisplayNames[d] ?? d}</Box>)}
                  {directDependencies.length === 0 && <Box color="text-body-secondary">None</Box>}
                </SpaceBetween>
              </div>
              <div>
                <Box variant="awsui-key-label">Dependents ({dependents.length})</Box>
                <SpaceBetween size="xxs">
                  {dependents.map((d: string) => <Box key={d} variant="code" fontSize="body-s">{depsDisplayNames[d] ?? d}</Box>)}
                  {dependents.length === 0 && <Box color="text-body-secondary">None</Box>}
                </SpaceBetween>
              </div>
            </ColumnLayout>
            <Box padding={{ top: "s" }}>
              <StatusIndicator type={depsData.coupling_strength === "high" ? "error" : depsData.coupling_strength === "medium" ? "warning" : "success"}>
                Coupling: {depsData.coupling_strength ?? "unknown"}
              </StatusIndicator>
            </Box>
          </ExpandableSection>
        )}
      </SpaceBetween>
    </Container>
  );
}
