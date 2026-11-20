import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Input from "@cloudscape-design/components/input";
import Multiselect, { MultiselectProps } from "@cloudscape-design/components/multiselect";
import Box from "@cloudscape-design/components/box";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Badge from "@cloudscape-design/components/badge";
import Flashbar, { FlashbarProps } from "@cloudscape-design/components/flashbar";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Link from "@cloudscape-design/components/link";
import { api, isQueryable, SpaceSummary, Hyperspace } from "../api/client";
import { useDefaultTopK } from "../hooks/useSearchDefaults";

export default function GlobalSearch() {
  const navigate = useNavigate();
  const [spaces, setSpaces] = useState<SpaceSummary[]>([]);
  const [hyperspaces, setHyperspaces] = useState<Hyperspace[]>([]);
  const [selectedSpaces, setSelectedSpaces] = useState<MultiselectProps.Option[]>([]);
  const [query, setQuery] = useState("");
  const defaultTopK = useDefaultTopK();
  const [limit, setLimit] = useState<string>(String(defaultTopK));
  const limitTouched = useRef(false);
  const [results, setResults] = useState<Record<string, any>[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [flash, setFlash] = useState<FlashbarProps.MessageDefinition[]>([]);

  useEffect(() => {
    if (!limitTouched.current) setLimit(String(defaultTopK));
  }, [defaultTopK]);

  useEffect(() => {
    api.listSpaces().then(setSpaces).catch(() => {});
    api.listHyperspaces().then(setHyperspaces).catch(() => {});
  }, []);

  // Business relevance is only meaningful for spaces analyzed with the
  // "business relevance scoring optimization" turned on. Track which spaces
  // enabled it so we only surface scores for those results.
  const relevanceEnabledSpaceIds = new Set(
    spaces
      .filter((s) => s.omit_low_business_relevance === true)
      .map((s) => s.space_id)
  );
  const showRelevanceColumn = results.some(
    (item) => relevanceEnabledSpaceIds.has(item.metadata?.space_id)
  );

  const spaceOptions: MultiselectProps.Options = [
    ...(hyperspaces.length > 0
      ? [
          {
            label: "Hyperspaces",
            options: hyperspaces.map((hs) => ({
              label: `⚡ ${hs.name}`,
              value: `hs:${hs.hyperspace_id}`,
              description: `${hs.space_ids.length} spaces`,
            })),
          },
        ]
      : []),
    ...spaces
      .filter((s) => isQueryable(s.status))
      .map((s) => ({
        label: s.space_name || s.space_id,
        value: s.space_id,
        description: `${s.component_count ?? 0} components`,
      })),
  ];

  // Resolve hyperspace selections to their constituent space IDs
  const resolveSpaceIds = (options: MultiselectProps.Option[]): string[] => {
    const ids = new Set<string>();
    for (const opt of options) {
      const val = opt.value!;
      if (val.startsWith("hs:")) {
        const hsId = val.slice(3);
        const hs = hyperspaces.find((h) => h.hyperspace_id === hsId);
        if (hs) hs.space_ids.forEach((sid) => ids.add(sid));
      } else {
        ids.add(val);
      }
    }
    return Array.from(ids);
  };

  const handleSearch = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setSearched(true);
    try {
      const spaceIds = resolveSpaceIds(selectedSpaces);
      const data = await api.globalSemanticSearch(
        query.trim(),
        parseInt(limit) || defaultTopK,
        spaceIds.length > 0 ? spaceIds : undefined
      );
      setResults(data);
    } catch (e: any) {
      setFlash([{
        type: "error",
        content: `Search failed: ${e.message}`,
        dismissible: true,
        onDismiss: () => setFlash([]),
      }]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup
        items={[
          { text: "CodeKB", href: "/" },
          { text: "Global search", href: "/search" },
        ]}
        onFollow={(e) => { e.preventDefault(); navigate(e.detail.href); }}
      />
      <Flashbar items={flash} />

      <Header
        variant="h1"
        description="Search across component descriptions in all or selected spaces"
      >
        Global semantic search
      </Header>

      <Container>
        <SpaceBetween size="m">
          <ColumnLayout columns={2}>
            <FormField label="Search query" description="Describe what you're looking for in natural language">
              <Input
                value={query}
                onChange={({ detail }) => setQuery(detail.value)}
                placeholder="e.g. functions that handle authentication"
                type="search"
                onKeyDown={({ detail }) => { if (detail.key === "Enter") handleSearch(); }}
              />
            </FormField>
            <FormField label="Scope to spaces and hyperspaces" description="Leave empty to search all spaces">
              <Multiselect
                selectedOptions={selectedSpaces}
                onChange={({ detail }) => setSelectedSpaces([...detail.selectedOptions])}
                options={spaceOptions}
                placeholder="All spaces"
                filteringType="auto"
              />
            </FormField>
          </ColumnLayout>
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="primary" loading={loading} onClick={handleSearch} disabled={!query.trim()}>
              Search
            </Button>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <Box variant="awsui-key-label" fontSize="body-s">Limit</Box>
              <div style={{ width: 70 }}>
                <Input
                  value={limit}
                  onChange={({ detail }) => { limitTouched.current = true; setLimit(detail.value); }}
                  type="number"
                  inputMode="numeric"
                />
              </div>
            </div>
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      <Table
        loading={loading}
        loadingText="Searching vector store..."
        items={results}
        trackBy="key"
        wrapLines
        empty={
          <Box textAlign="center" padding="l" color="inherit">
            {searched ? "No results found. Try a different query." : "Enter a query above to search across spaces."}
          </Box>
        }
        header={
          <Header counter={searched ? `(${results.length})` : undefined}>
            Results
          </Header>
        }
        columnDefinitions={[
          {
            id: "key",
            header: "Component",
            cell: (item) => {
              const spaceId = item.metadata?.space_id;
              return spaceId ? (
                <Link onFollow={(e) => { e.preventDefault(); navigate(`/spaces/${spaceId}`); }}>
                  <Box variant="code">{item.key}</Box>
                </Link>
              ) : (
                <Box variant="code">{item.key}</Box>
              );
            },
            sortingField: "key",
          },
          {
            id: "space",
            header: "Space",
            cell: (item) => {
              const sid = item.metadata?.space_id;
              return sid ? <Badge color="blue">{sid}</Badge> : "—";
            },
          },
          {
            id: "type",
            header: "Type",
            cell: (item) => item.metadata?.type ?? "—",
          },
          {
            id: "distance",
            header: "Score",
            cell: (item) => {
              const d = item.distance;
              if (d == null) return "—";
              const score = (1 - d).toFixed(3);
              return <Box variant="code">{score}</Box>;
            },
          },
          ...(showRelevanceColumn
            ? [
                {
                  id: "relevance",
                  header: "Business relevance",
                  cell: (item: Record<string, any>) =>
                    relevanceEnabledSpaceIds.has(item.metadata?.space_id) ? (
                      <BusinessRelevanceSummary metadata={item.metadata} />
                    ) : (
                      <Box color="text-body-secondary">—</Box>
                    ),
                },
              ]
            : []),
          {
            id: "description",
            header: "Description",
            cell: (item) => {
              const desc = item.metadata?.description ?? "";
              return <Box variant="p" fontSize="body-s">{desc}</Box>;
            },
          },
        ]}
      />
    </SpaceBetween>
  );
}

function BusinessRelevanceSummary({ metadata }: { metadata?: Record<string, any> | null }) {
  const score = relevanceScore(metadata);
  const tier = String(metadata?.business_relevance_tier ?? "").trim();
  const reasons = relevanceReasons(metadata).slice(0, 2);
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

function relevanceScore(metadata?: Record<string, any> | null): number | null {
  const value = metadata?.business_relevance_score;
  if (value == null || value === "") return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function relevanceReasons(metadata?: Record<string, any> | null): string[] {
  const reasons = metadata?.business_relevance_reasons;
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
