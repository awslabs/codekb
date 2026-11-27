import { useState, useMemo, useEffect, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
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
  const { t } = useTranslation();
  if (status === "READY") return <StatusIndicator type="success">{t("common.statusReady")}</StatusIndicator>;
  if (status === "FAILED") return <StatusIndicator type="error">{t("common.statusFailed")}</StatusIndicator>;
  if (status === "REFRESH_FAILED") return <StatusIndicator type="error">{t("spaceList.statusRefreshFailed")}</StatusIndicator>;
  if (status === "ANALYZING" || status === "SUBMITTED" || status === "SUMMARIZING" || status === "REFRESHING") {
    const label = status === "SUMMARIZING" ? t("common.statusSummarizing") : status === "REFRESHING" ? t("common.statusRefreshing") : t("common.statusAnalyzing");
    return (
      <span className="pulse-status">
        <StatusIndicator type="in-progress">{label}</StatusIndicator>
      </span>
    );
  }
  return <StatusIndicator type="loading">{t("common.statusLoading")}</StatusIndicator>;
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
  const { t } = useTranslation();
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
      setFlash([{ type: "success", content: t("spaceList.flashDeleted", { count: succeeded }), dismissible: true, onDismiss: () => setFlash([]) }]);
    } else {
      const authz = failures
        .map((f) => f.reason)
        .find((r) => r instanceof ApiError && r.isAuthz) as ApiError | undefined;
      const detail = authz ? authz.displayMessage : failures[0].reason?.message ?? "";
      setFlash([{
        type: succeeded > 0 ? "warning" : "error",
        content: t("spaceList.flashDeletePartial", { succeeded, total: items.length, failed: failures.length, detail }),
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
      { key: "status", operators: ["=", "!="], propertyLabel: t("spaceList.propStatus"), groupValuesLabel: t("spaceList.propStatusValues") },
      { key: "language", operators: ["=", "!=", ":", "!:"], propertyLabel: t("spaceList.propLanguage"), groupValuesLabel: t("spaceList.propLanguageValues") },
      { key: "source_type", operators: ["=", "!="], propertyLabel: t("spaceList.propSourceType"), groupValuesLabel: t("spaceList.propSourceTypeValues") },
      { key: "created_by", operators: ["=", "!=", ":", "!:"], propertyLabel: t("spaceList.propCreatedBy"), groupValuesLabel: t("spaceList.propCreatedByValues") },
      { key: "space_name", operators: [":", "!:"], propertyLabel: t("spaceList.propName"), groupValuesLabel: t("spaceList.propNameValues") },
    ];

    const options: PropertyFilterProps.FilteringOption[] = [
      ...statuses.map((v) => ({ propertyKey: "status", value: v })),
      ...languages.map((v) => ({ propertyKey: "language", value: v })),
      ...sourceTypes.map((v) => ({ propertyKey: "source_type", value: v })),
      ...creators.map((v) => ({ propertyKey: "created_by", value: v })),
    ];

    return { filteringProperties: properties, filteringOptions: options };
  }, [spaces, t]);

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
      ? query.tokens.every((tok) => matchesToken(s, tok))
      : query.tokens.some((tok) => matchesToken(s, tok));
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
      <BreadcrumbGroup items={[{ text: t("common.breadcrumbRoot"), href: "/" }, { text: t("nav.spaces"), href: "/spaces" }]} />
      <Flashbar items={flash} />
      {expiredProviders.length > 0 && (
        <Alert
          type="warning"
          header={t("spaceList.tokenExpiredHeader")}
          action={<Button onClick={() => navigate(GIT_CREDS_PATH)}>{t("spaceList.gitCredentials")}</Button>}
        >
          {t("spaceList.tokenExpiredBody", { providers: expiredProviders.join(", ") })}
        </Alert>
      )}
      {listError && (
        <Alert type="error" header={t("spaceList.loadErrorHeader")}>
          {listError instanceof ApiError ? listError.displayMessage : (listError as Error).message}
        </Alert>
      )}

      <Table
        selectionType="multi"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
        loading={loading}
        loadingText={t("spaceList.loadingText")}
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
            <b>{t("spaceList.emptyTitle")}</b>
            <Box padding={{ bottom: "s" }} variant="p" color="inherit">
              {t("spaceList.emptyBody")}
            </Box>
            <Button onClick={() => navigate("/create")}>{t("spaceList.newSpace")}</Button>
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
                <Button iconName="refresh" ariaLabel={t("common.refresh")} onClick={refresh} />
                <Button disabled={selectedItems.length === 0 || selectedItems.some((s) => s.status === "REFRESHING")} onClick={() => setDeleteVisible(true)}>
                  {t("spaceList.delete")}
                </Button>
                <Button variant="primary" onClick={() => navigate("/create")}>
                  {t("spaceList.newSpace")}
                </Button>
              </SpaceBetween>
            }
          >
            {t("nav.spaces")}
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
              countText={t("spaceList.matchCount", { count: filtered.length })}
              expandToViewport
              i18nStrings={{
              filteringAriaLabel: t("spaceList.filterAriaLabel"),
              filteringPlaceholder: t("spaceList.filterPlaceholder"),
              dismissAriaLabel: t("spaceList.filterDismiss"),
              clearAriaLabel: t("spaceList.filterClear"),
              groupValuesText: t("spaceList.filterGroupValues"),
              groupPropertiesText: t("spaceList.filterGroupProperties"),
              operatorsText: t("spaceList.filterOperators"),
              operationAndText: t("spaceList.filterAnd"),
              operationOrText: t("spaceList.filterOr"),
              operatorLessText: t("spaceList.filterLess"),
              operatorLessOrEqualText: t("spaceList.filterLessOrEqual"),
              operatorGreaterText: t("spaceList.filterGreater"),
              operatorGreaterOrEqualText: t("spaceList.filterGreaterOrEqual"),
              operatorContainsText: t("spaceList.filterContains"),
              operatorDoesNotContainText: t("spaceList.filterDoesNotContain"),
              operatorEqualsText: t("spaceList.filterEquals"),
              operatorDoesNotEqualText: t("spaceList.filterDoesNotEqual"),
              editTokenHeader: t("spaceList.filterEditHeader"),
              propertyText: t("spaceList.filterProperty"),
              operatorText: t("spaceList.filterOperator"),
              valueText: t("spaceList.filterValue"),
              cancelActionText: t("common.cancel"),
              applyActionText: t("spaceList.filterApply"),
              allPropertiesLabel: t("spaceList.filterAllProperties"),
              tokenLimitShowMore: t("spaceList.filterShowMore"),
              tokenLimitShowFewer: t("spaceList.filterShowFewer"),
              clearFiltersText: t("spaceList.filterClearFilters"),
              removeTokenButtonAriaLabel: () => t("spaceList.filterRemove"),
              enteredTextLabel: (text) => t("spaceList.filterEnteredText", { text }),
            }}
          />
            <Toggle
              checked={showMySpaces}
              onChange={({ detail }) => { setShowMySpaces(detail.checked); setCurrentPage(1); }}
            >
              {t("spaceList.mySpaces")}
            </Toggle>
          </SpaceBetween>
        }
        pagination={<Pagination currentPageIndex={currentPage} pagesCount={pagesCount} ariaLabels={{ nextPageLabel: t("spaceList.pageNext"), previousPageLabel: t("spaceList.pagePrevious"), pageLabel: (n) => t("spaceList.pageLabel", { page: n }) }} onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)} />}
        preferences={
          <CollectionPreferences
            title={t("common.tableSettings")}
            confirmLabel={t("common.confirm")}
            cancelLabel={t("common.cancel")}
            preferences={{ pageSize }}
            onConfirm={({ detail }) => {
              setPageSize(detail.pageSize ?? 10);
              setCurrentPage(1);
            }}
            pageSizePreference={{
              title: t("common.pageSize"),
              options: [
                { value: 10, label: t("common.rows", { count: 10 }) },
                { value: 25, label: t("common.rows", { count: 25 }) },
                { value: 50, label: t("common.rows", { count: 50 }) },
                { value: 100, label: t("common.rows", { count: 100 }) },
              ],
            }}
          />
        }
        columnDefinitions={[
          {
            id: "space_id",
            header: t("spaceList.colSpace"),
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
            header: t("spaceList.colStatus"),
            cell: (item) => <AnimatedStatus status={item.status} />,
            sortingField: "status",
          },
          {
            id: "visibility",
            header: t("spaceList.colVisibility"),
            cell: (item) =>
              item.visibility ? (
                <Badge color={item.visibility === "public" ? "blue" : "grey"}>
                  {item.visibility === "public" ? t("spaceList.public") : t("spaceList.private")}
                </Badge>
              ) : (
                "—"
              ),
          },
          {
            id: "languages",
            header: t("spaceList.colLanguages"),
            cell: (item) => <LanguageBadges languages={item.languages} />,
          },
          {
            id: "lines",
            header: t("spaceList.colLines"),
            cell: (item) => {
              const lines = (item as any).total_lines;
              return lines ? Number(lines).toLocaleString() : "—";
            },
            sortingField: "total_lines",
          },
          {
            id: "created_by",
            header: t("spaceList.colCreatedBy"),
            cell: (item) => item.created_by || "—",
          },
          {
            id: "created_at",
            header: t("spaceList.colCreatedAt"),
            cell: (item) => formatDate(item.created_at),
            sortingField: "created_at",
          },
          {
            id: "git_url",
            header: t("spaceList.colSource"),
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
        header={t("spaceList.deleteHeader")}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setDeleteVisible(false)}>{t("common.cancel")}</Button>
              <Button variant="primary" loading={deleting} onClick={handleDelete}>
                {t("spaceList.delete")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {t("spaceList.deleteBody", { count: selectedItems.length })}
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
