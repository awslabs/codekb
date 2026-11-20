import { useState, useMemo, useEffect, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import PropertyFilter, { PropertyFilterProps } from "@cloudscape-design/components/property-filter";
import Pagination from "@cloudscape-design/components/pagination";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Flashbar, { FlashbarProps } from "@cloudscape-design/components/flashbar";
import Modal from "@cloudscape-design/components/modal";
import Box from "@cloudscape-design/components/box";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Link from "@cloudscape-design/components/link";
import Badge from "@cloudscape-design/components/badge";
import CollectionPreferences from "@cloudscape-design/components/collection-preferences";
import Toggle from "@cloudscape-design/components/toggle";
import Alert from "@cloudscape-design/components/alert";
import { api, ApiError, GIT_CREDS_PATH, SpaceSummary } from "../api/client";
import { getUser } from "../auth";
import { languageColor } from "../components/LanguagePills";

// Only allow http(s) URLs as link hrefs; block javascript:/data: (XSS).
function safeHttpUrl(raw?: string): string | undefined {
  if (!raw) return undefined;
  try {
    const parsed = new URL(raw);
    return parsed.protocol === "http:" || parsed.protocol === "https:"
      ? raw
      : undefined;
  } catch {
    return undefined;
  }
}

function LanguageBadges({ languages }: { languages?: string[] }) {
  if (!languages?.length) return <span>—</span>;
  return (
    <SpaceBetween direction="horizontal" size="xxs">
      {languages.map((lang) => {
        const color = languageColor(lang);
        return (
          <span
            key={lang}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              padding: "2px 8px",
              borderRadius: 12,
              fontSize: 12,
              fontWeight: 600,
              backgroundColor: `${color}18`,
              color,
              border: `1px solid ${color}40`,
            }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                backgroundColor: color,
              }}
            />
            {lang}
          </span>
        );
      })}
    </SpaceBetween>
  );
}

// ── Pulsing status for in-progress ─────────────────────────────────────
function AnimatedStatus({ status }: { status?: string }) {
  if (status === "READY") return <StatusIndicator type="success">Ready</StatusIndicator>;
  if (status === "FAILED") return <StatusIndicator type="error">Failed</StatusIndicator>;
  if (status === "REFRESH_FAILED") return <StatusIndicator type="error">Refresh failed</StatusIndicator>;
  if (status === "ANALYZING" || status === "SUBMITTED" || status === "SUMMARIZING" || status === "REFRESHING") {
    const label = status === "SUMMARIZING" ? "Summarizing" : status === "REFRESHING" ? "Refreshing" : "Analyzing";
    return (
      <span className="pulse-status">
        <StatusIndicator type="in-progress">{label}</StatusIndicator>
      </span>
    );
  }
  return <StatusIndicator type="loading">Loading</StatusIndicator>;
}

// ── Date formatting ─────────────────────────────────────────────────────
function formatDate(iso?: string) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ── Main component ─────────────────────────────────────────────────────
export default function SpaceList() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data: spaces = [], isLoading: loading, isFetching, refetch, error: listError, dataUpdatedAt } = useQuery({
    queryKey: ["spaces"],
    queryFn: () => api.listSpaces(),
    refetchInterval: (query: { state: { data?: SpaceSummary[] } }) => {
      const data = query.state.data;
      if (data?.some((s: SpaceSummary) => ["ANALYZING", "SUBMITTED", "SUMMARIZING", "REFRESHING"].includes(s.status ?? ""))) {
        return 5000;
      }
      return false;
    },
  });

  // An expired token hides private spaces from the list. Check once after the first
  // load settles (that load is what flags a rejected token) and on manual refresh,
  // not on every 5s poll.
  const [expiredProviders, setExpiredProviders] = useState<string[]>([]);
  const checkExpiredTokens = useCallback(() => {
    api.listGitCredentials()
      .then((creds) => setExpiredProviders(creds.filter((c) => c.status === "expired").map((c) => c.provider)))
      .catch(() => setExpiredProviders([]));
  }, []);
  const tokensChecked = useRef(false);
  useEffect(() => {
    if (tokensChecked.current || isFetching || !dataUpdatedAt) return;
    tokensChecked.current = true;
    checkExpiredTokens();
  }, [isFetching, dataUpdatedAt, checkExpiredTokens]);
  const refresh = async () => {
    await refetch();
    checkExpiredTokens();
  };
  const [query, setQuery] = useState<PropertyFilterProps.Query>({ tokens: [], operation: "and" });
  const [currentPage, setCurrentPage] = useState(1);
  const [selectedItems, setSelectedItems] = useState<SpaceSummary[]>([]);
  const [flash, setFlash] = useState<FlashbarProps.MessageDefinition[]>([]);
  const [deleteVisible, setDeleteVisible] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [pageSize, setPageSize] = useState(10);
  const [sortingColumn, setSortingColumn] = useState<{ sortingField?: string }>({});
  const [sortingDescending, setSortingDescending] = useState(false);

  // "My spaces" filter — opt-in, defaults OFF so everyone (including teammates
  // working on spaces a colleague created) sees the full list on first load.
  const currentUser = getUser();
  const [showMySpaces, setShowMySpaces] = useState(false);

  const handleDelete = async () => {
    setDeleting(true);
    // Delete all selected independently so one denial (e.g. write_denied) doesn't
    // abort the rest or hide the deletes that already succeeded.
    const items = selectedItems;
    const results = await Promise.allSettled(items.map((item) => api.deleteSpace(item.space_id)));
    const failures = results.filter((r): r is PromiseRejectedResult => r.status === "rejected");
    const succeeded = items.length - failures.length;
    setDeleteVisible(false);
    setSelectedItems([]);
    queryClient.invalidateQueries({ queryKey: ["spaces"] });
    if (failures.length === 0) {
      setFlash([{ type: "success", content: `Deleted ${succeeded} space(s).`, dismissible: true, onDismiss: () => setFlash([]) }]);
    } else {
      const authz = failures
        .map((f) => f.reason)
        .find((r) => r instanceof ApiError && r.isAuthz) as ApiError | undefined;
      const detail = authz ? authz.displayMessage : failures[0].reason?.message ?? "";
      setFlash([{
        type: succeeded > 0 ? "warning" : "error",
        content: `Deleted ${succeeded} of ${items.length}; ${failures.length} failed. ${detail}`,
        dismissible: true,
        onDismiss: () => setFlash([]),
      }]);
    }
    setDeleting(false);
  };

  // ── Property filter wiring ──────────────────────────────────────────
  // Build dropdown options from the loaded data so they reflect what's
  // actually present (statuses, languages, source types, creators).
  const { filteringProperties, filteringOptions } = useMemo(() => {
    const statuses = Array.from(new Set(spaces.map((s) => s.status).filter(Boolean) as string[])).sort();
    const languages = Array.from(new Set(spaces.flatMap((s) => s.languages ?? []))).sort();
    const sourceTypes = Array.from(new Set(spaces.map((s) => s.source_type).filter(Boolean) as string[])).sort();
    const creators = Array.from(new Set(spaces.map((s) => s.created_by).filter(Boolean) as string[])).sort();

    const properties: PropertyFilterProps.FilteringProperty[] = [
      { key: "status", operators: ["=", "!="], propertyLabel: "Status", groupValuesLabel: "Status values" },
      { key: "language", operators: ["=", "!=", ":", "!:"], propertyLabel: "Language", groupValuesLabel: "Language values" },
      { key: "source_type", operators: ["=", "!="], propertyLabel: "Source type", groupValuesLabel: "Source type values" },
      { key: "created_by", operators: ["=", "!=", ":", "!:"], propertyLabel: "Created by", groupValuesLabel: "Creator values" },
      { key: "space_name", operators: [":", "!:"], propertyLabel: "Name", groupValuesLabel: "Name values" },
    ];

    const options: PropertyFilterProps.FilteringOption[] = [
      ...statuses.map((v) => ({ propertyKey: "status", value: v })),
      ...languages.map((v) => ({ propertyKey: "language", value: v })),
      ...sourceTypes.map((v) => ({ propertyKey: "source_type", value: v })),
      ...creators.map((v) => ({ propertyKey: "created_by", value: v })),
    ];

    return { filteringProperties: properties, filteringOptions: options };
  }, [spaces]);

  const matchesToken = (s: SpaceSummary, token: PropertyFilterProps.Token): boolean => {
    const op = token.operator;
    const val = String(token.value ?? "").toLowerCase();
    const eq = (a?: string) => (a ?? "").toLowerCase() === val;
    const contains = (a?: string) => (a ?? "").toLowerCase().includes(val);

    switch (token.propertyKey) {
      case "status":
        return op === "!=" ? !eq(s.status) : eq(s.status);
      case "source_type":
        return op === "!=" ? !eq(s.source_type) : eq(s.source_type);
      case "created_by":
        if (op === ":") return contains(s.created_by);
        if (op === "!:") return !contains(s.created_by);
        if (op === "!=") return !eq(s.created_by);
        return eq(s.created_by);
      case "space_name":
        return op === "!:" ? !contains(s.space_name) : contains(s.space_name);
      case "language": {
        const langs = (s.languages ?? []).map((l) => l.toLowerCase());
        const has = langs.includes(val);
        const sub = langs.some((l) => l.includes(val));
        if (op === "=") return has;
        if (op === "!=") return !has;
        if (op === ":") return sub;
        if (op === "!:") return !sub;
        return true;
      }
      default:
        // Free-text token (no propertyKey): match space_id or space_name.
        return contains(s.space_id) || contains(s.space_name);
    }
  };

  const filtered = spaces.filter((s) => {
    // "My spaces" filter: restrict to the current user's spaces.
    if (showMySpaces) {
      const myIdentity = (currentUser?.email || "anonymous").toLowerCase();
      if ((s.created_by ?? "").toLowerCase() !== myIdentity) return false;
    }
    if (query.tokens.length === 0) return true;
    return query.operation === "and"
      ? query.tokens.every((t) => matchesToken(s, t))
      : query.tokens.some((t) => matchesToken(s, t));
  });

  const sorted = [...filtered].sort((a, b) => {
    const field = sortingColumn.sortingField;
    if (!field) return 0;
    let cmp = 0;
    if (field === "status") {
      cmp = (a.status ?? "").localeCompare(b.status ?? "");
    } else if (field === "component_count") {
      cmp = (a.component_count ?? 0) - (b.component_count ?? 0);
    } else if (field === "total_lines") {
      cmp = ((a as any).total_lines ?? 0) - ((b as any).total_lines ?? 0);
    } else if (field === "created_at") {
      const ta = a.created_at ? Date.parse(a.created_at) : 0;
      const tb = b.created_at ? Date.parse(b.created_at) : 0;
      cmp = ta - tb;
    } else if (field === "space_id") {
      cmp = a.space_id.localeCompare(b.space_id);
    }
    return sortingDescending ? -cmp : cmp;
  });

  const pagesCount = Math.max(1, Math.ceil(sorted.length / pageSize));
  const paginated = sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup items={[{ text: "CodeKB", href: "/" }, { text: "Spaces", href: "/spaces" }]} />
      <Flashbar items={flash} />
      {expiredProviders.length > 0 && (
        <Alert
          type="warning"
          header="Git token expired"
          action={<Button onClick={() => navigate(GIT_CREDS_PATH)}>Git credentials</Button>}
        >
          Your {expiredProviders.join(", ")} token has expired, so only public spaces are shown.
          Reconnect it to see your private spaces.
        </Alert>
      )}
      {listError && (
        <Alert type="error" header="Could not load spaces">
          {listError instanceof ApiError ? listError.displayMessage : (listError as Error).message}
        </Alert>
      )}

      <Table
        selectionType="multi"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
        loading={loading}
        loadingText="Loading spaces..."
        items={paginated}
        trackBy="space_id"
        sortingColumn={sortingColumn as any}
        sortingDescending={sortingDescending}
        onSortingChange={({ detail }) => {
          setSortingColumn(detail.sortingColumn as any);
          setSortingDescending(detail.isDescending ?? false);
          setCurrentPage(1);
        }}
        empty={
          <Box textAlign="center" color="inherit" padding="l">
            <b>No spaces</b>
            <Box padding={{ bottom: "s" }} variant="p" color="inherit">
              No spaces yet. Click "New space" to get started.
            </Box>
            <Button onClick={() => navigate("/create")}>New space</Button>
          </Box>
        }
        header={
          <Header
            counter={
              query.tokens.length > 0
                ? `(${filtered.length}/${spaces.length})`
                : `(${spaces.length})`
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" onClick={refresh} />
                <Button disabled={selectedItems.length === 0 || selectedItems.some((s) => s.status === "REFRESHING")} onClick={() => setDeleteVisible(true)}>
                  Delete
                </Button>
                <Button variant="primary" onClick={() => navigate("/create")}>
                  New space
                </Button>
              </SpaceBetween>
            }
          >
            Spaces
          </Header>
        }
        filter={
          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
            <PropertyFilter
              query={query}
              onChange={({ detail }) => {
                setQuery(detail);
                setCurrentPage(1);
              }}
              filteringProperties={filteringProperties}
              filteringOptions={filteringOptions}
              countText={`${filtered.length} match${filtered.length === 1 ? "" : "es"}`}
              expandToViewport
              i18nStrings={{
              filteringAriaLabel: "Filter spaces",
              filteringPlaceholder: "Filter by status, language, source, creator, or name",
              dismissAriaLabel: "Dismiss",
              clearAriaLabel: "Clear",
              groupValuesText: "Values",
              groupPropertiesText: "Properties",
              operatorsText: "Operators",
              operationAndText: "and",
              operationOrText: "or",
              operatorLessText: "Less than",
              operatorLessOrEqualText: "Less than or equal",
              operatorGreaterText: "Greater than",
              operatorGreaterOrEqualText: "Greater than or equal",
              operatorContainsText: "Contains",
              operatorDoesNotContainText: "Does not contain",
              operatorEqualsText: "Equals",
              operatorDoesNotEqualText: "Does not equal",
              editTokenHeader: "Edit filter",
              propertyText: "Property",
              operatorText: "Operator",
              valueText: "Value",
              cancelActionText: "Cancel",
              applyActionText: "Apply",
              allPropertiesLabel: "All properties",
              tokenLimitShowMore: "Show more",
              tokenLimitShowFewer: "Show fewer",
              clearFiltersText: "Clear filters",
              removeTokenButtonAriaLabel: () => "Remove filter",
              enteredTextLabel: (text) => `Use: "${text}"`,
            }}
          />
            <Toggle
              checked={showMySpaces}
              onChange={({ detail }) => { setShowMySpaces(detail.checked); setCurrentPage(1); }}
            >
              My spaces
            </Toggle>
          </SpaceBetween>
        }
        pagination={<Pagination currentPageIndex={currentPage} pagesCount={pagesCount} onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)} />}
        preferences={
          <CollectionPreferences
            title="Table settings"
            confirmLabel="Confirm"
            cancelLabel="Cancel"
            preferences={{ pageSize }}
            onConfirm={({ detail }) => {
              setPageSize(detail.pageSize ?? 10);
              setCurrentPage(1);
            }}
            pageSizePreference={{
              title: "Page size",
              options: [
                { value: 10, label: "10 rows" },
                { value: 25, label: "25 rows" },
                { value: 50, label: "50 rows" },
                { value: 100, label: "100 rows" },
              ],
            }}
          />
        }
        columnDefinitions={[
          {
            id: "space_id",
            header: "Space",
            cell: (item) => {
              const displayName = item.space_name || item.space_id.replace(/-[a-f0-9]{4,}$/, "");
              return (
                <div>
                  <Link onFollow={(e) => { e.preventDefault(); navigate(`/spaces/${item.space_id}`); }}>
                    {displayName}
                  </Link>
                  <div style={{ fontSize: 11, color: "#687078", marginTop: 2 }}>{item.space_id}</div>
                </div>
              );
            },
            sortingField: "space_id",
          },
          {
            id: "status",
            header: "Status",
            cell: (item) => <AnimatedStatus status={item.status} />,
            sortingField: "status",
          },
          {
            id: "visibility",
            header: "Visibility",
            cell: (item) =>
              item.visibility ? (
                <Badge color={item.visibility === "public" ? "blue" : "grey"}>
                  {item.visibility === "public" ? "Public" : "Private"}
                </Badge>
              ) : (
                "—"
              ),
          },
          {
            id: "languages",
            header: "Languages",
            cell: (item) => <LanguageBadges languages={item.languages} />,
          },
          {
            id: "lines",
            header: "Lines of code",
            cell: (item) => {
              const lines = (item as any).total_lines;
              return lines ? Number(lines).toLocaleString() : "—";
            },
            sortingField: "total_lines",
          },
          {
            id: "created_by",
            header: "Created by",
            cell: (item) => item.created_by || "—",
          },
          {
            id: "created_at",
            header: "Created at",
            cell: (item) => formatDate(item.created_at),
            sortingField: "created_at",
          },
          {
            id: "git_url",
            header: "Source",
            cell: (item) => {
              const href = safeHttpUrl(item.git_url);
              if (href) {
                return (
                  <Link href={href} external>
                    Git
                  </Link>
                );
              }
              if (item.git_url) return "Git";
              if (item.source_type === "upload") return <Badge color="blue">Zip</Badge>;
              return "—";
            },
          },
        ]}
      />

      <Modal
        visible={deleteVisible}
        onDismiss={() => setDeleteVisible(false)}
        header="Delete spaces"
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
        Delete {selectedItems.length} space(s)?

        This will delete all associated data including component catalog,
        local copy of source code and vectorized function summaries.

        This action cannot be undone.
        <Box variant="p" padding={{ top: "s" }}>
          {selectedItems.map((p) => (
            <Box key={p.space_id} variant="code">{p.space_id} </Box>
          ))}
        </Box>
      </Modal>

      {/* Pulse animation for in-progress statuses */}
      <style>{`
        @keyframes pulse-glow {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.6; }
        }
        .pulse-status {
          display: inline-block;
          animation: pulse-glow 2s ease-in-out infinite;
        }
      `}</style>
    </SpaceBetween>
  );
}
