import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Spinner from "@cloudscape-design/components/spinner";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import { api } from "../api/client";
import TreemapChart from "../components/TreemapChart";
import SpaceScatter from "../components/SpaceScatter";
import LanguagePills from "../components/LanguagePills";

function MetricCard({ title, value, description }: { title: string; value: string | number; description?: React.ReactNode }) {
  return (
    <div>
      <Box variant="awsui-key-label">{title}</Box>
      <Box variant="awsui-value-large">{typeof value === "number" ? value.toLocaleString() : value}</Box>
      {description && <Box display="block" variant="small" color="text-body-secondary">{description}</Box>}
    </div>
  );
}

export default function Overview() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [view, setView] = useState<"treemap" | "scatter">("treemap");

  const { data: metrics, isLoading: metricsLoading, error: metricsError } = useQuery({
    queryKey: ["globalMetrics"],
    queryFn: () => api.getGlobalMetrics(),
  });
  const { data: spaces = [], isLoading: spacesLoading } = useQuery({
    queryKey: ["spaces"],
    queryFn: () => api.listSpaces(),
  });
  const { data: hyperspaces = [], isLoading: hsLoading } = useQuery({
    queryKey: ["hyperspaces"],
    queryFn: () => api.listHyperspaces(),
  });

  const loading = metricsLoading || spacesLoading || hsLoading;
  const error = metricsError ? (metricsError as Error).message : null;

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["globalMetrics"] });
    queryClient.invalidateQueries({ queryKey: ["spaces"] });
    queryClient.invalidateQueries({ queryKey: ["hyperspaces"] });
  };

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }

  if (error) {
    return (
      <SpaceBetween size="l">
        <BreadcrumbGroup items={[{ text: "CodeKB", href: "/" }, { text: "Overview", href: "/" }]} />
        <StatusIndicator type="error">Failed to load: {error}</StatusIndicator>
      </SpaceBetween>
    );
  }

  const m = metrics!;

  // Empty state
  if (m.total_spaces === 0) {
    return (
      <SpaceBetween size="l">
        <BreadcrumbGroup items={[{ text: "CodeKB", href: "/" }, { text: "Overview", href: "/" }]} />
        <Header variant="h1" description="Aggregate metrics and space visualization"
          actions={<Button variant="primary" onClick={() => navigate("/create")}>New space</Button>}>
          Overview
        </Header>
        <Box textAlign="center" padding={{ vertical: "xxxl" }}>
          <SpaceBetween size="s" alignItems="center">
            <Box variant="h2" color="text-body-secondary">No spaces yet</Box>
            <Box variant="p" color="text-body-secondary">Create your first space to start building your knowledge base.</Box>
            <Button variant="primary" onClick={() => navigate("/create")}>Create a space</Button>
          </SpaceBetween>
        </Box>
      </SpaceBetween>
    );
  }

  // Status summary
  const statusItems: { label: string; count: number; color: string }[] = [
    { label: "Ready", count: m.ready_spaces, color: "#1d8102" },
    { label: "Analyzing", count: m.in_progress_spaces, color: "#0972d3" },
    { label: "Failed", count: m.failed_spaces, color: "#d91515" },
  ].filter((s) => s.count > 0);

  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup items={[{ text: "CodeKB", href: "/" }, { text: "Overview", href: "/" }]} />

      <Header variant="h1" description="Only the spaces and hyperspaces you can access."
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button iconName="refresh" onClick={refresh} />
            <Button variant="primary" onClick={() => navigate("/create")}>New space</Button>
          </SpaceBetween>
        }>
        Overview
      </Header>

      <div>
        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
          <SegmentedControl
            selectedId={view}
            onChange={({ detail }) => setView(detail.selectedId as "treemap" | "scatter")}
            options={[
              { text: "Treemap", id: "treemap" },
              { text: "Scatter", id: "scatter" },
            ]}
          />
        </div>
        {view === "treemap" && <TreemapChart spaces={spaces} hyperspaces={hyperspaces} />}
        {view === "scatter" && <SpaceScatter spaces={spaces} />}
      </div>

      <Container
        header={
          <Header
            variant="h2"
            description="Totals across every space in the system, including ones you can't access."
          >
            Global metrics
          </Header>
        }
      >
        <SpaceBetween size="m">
          <ColumnLayout columns={5} variant="text-grid">
            <MetricCard title="Total spaces" value={m.total_spaces} />
            <MetricCard title="Total components" value={m.total_components} description="Functions, classes, methods" />
            <MetricCard title="Total vectors" value={m.total_vectors} description="Embeddings in S3 Vectors" />
            <MetricCard title="Lines of code" value={m.total_lines_of_code} />
            <MetricCard title="Languages" value={m.language_count} description={m.languages.length > 0 ? <LanguagePills languages={m.languages} marginTop={4} /> : "None detected"} />
          </ColumnLayout>
          {statusItems.length > 0 && (
            <div style={{ display: "flex", alignItems: "center", gap: 16, paddingTop: 4 }}>
              <Box variant="awsui-key-label">Space health</Box>
              {statusItems.map((s) => (
                <span key={s.label} style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 14, fontWeight: 500 }}>
                  <span style={{ width: 10, height: 10, borderRadius: "50%", backgroundColor: s.color, display: "inline-block" }} />
                  {s.count} {s.label}
                </span>
              ))}
            </div>
          )}
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}
