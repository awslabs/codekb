import { useState } from "react";
import { useTranslation } from "react-i18next";
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
  const { t } = useTranslation();
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
        <BreadcrumbGroup items={[{ text: t("common.breadcrumbRoot"), href: "/" }, { text: t("nav.overview"), href: "/" }]} />
        <StatusIndicator type="error">{t("overview.failedToLoad")}: {error}</StatusIndicator>
      </SpaceBetween>
    );
  }

  const m = metrics!;

  // Empty state
  if (m.total_spaces === 0) {
    return (
      <SpaceBetween size="l">
        <BreadcrumbGroup items={[{ text: t("common.breadcrumbRoot"), href: "/" }, { text: t("nav.overview"), href: "/" }]} />
        <Header variant="h1" description={t("overview.descriptionEmpty")}
          actions={<Button variant="primary" onClick={() => navigate("/create")}>{t("overview.newSpace")}</Button>}>
          {t("nav.overview")}
        </Header>
        <Box textAlign="center" padding={{ vertical: "xxxl" }}>
          <SpaceBetween size="s" alignItems="center">
            <Box variant="h2" color="text-body-secondary">{t("overview.noSpacesTitle")}</Box>
            <Box variant="p" color="text-body-secondary">{t("overview.noSpacesBody")}</Box>
            <Button variant="primary" onClick={() => navigate("/create")}>{t("overview.createSpace")}</Button>
          </SpaceBetween>
        </Box>
      </SpaceBetween>
    );
  }

  // Status summary
  const statusItems: { label: string; count: number; color: string }[] = [
    { label: t("common.statusReady"), count: m.ready_spaces, color: "#1d8102" },
    { label: t("common.statusAnalyzing"), count: m.in_progress_spaces, color: "#0972d3" },
    { label: t("common.statusFailed"), count: m.failed_spaces, color: "#d91515" },
  ].filter((s) => s.count > 0);

  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup items={[{ text: t("common.breadcrumbRoot"), href: "/" }, { text: t("nav.overview"), href: "/" }]} />

      <Header variant="h1" description={t("overview.description")}
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button iconName="refresh" onClick={refresh} ariaLabel={t("common.refresh")} />
            <Button variant="primary" onClick={() => navigate("/create")}>{t("overview.newSpace")}</Button>
          </SpaceBetween>
        }>
        {t("nav.overview")}
      </Header>

      <div>
        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
          <SegmentedControl
            selectedId={view}
            onChange={({ detail }) => setView(detail.selectedId as "treemap" | "scatter")}
            options={[
              { text: t("overview.viewTreemap"), id: "treemap" },
              { text: t("overview.viewScatter"), id: "scatter" },
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
            description={t("overview.globalMetricsDescription")}
          >
            {t("overview.globalMetrics")}
          </Header>
        }
      >
        <SpaceBetween size="m">
          <ColumnLayout columns={5} variant="text-grid">
            <MetricCard title={t("overview.totalSpaces")} value={m.total_spaces} />
            <MetricCard title={t("overview.totalComponents")} value={m.total_components} description={t("overview.totalComponentsDesc")} />
            <MetricCard title={t("overview.totalVectors")} value={m.total_vectors} description={t("overview.totalVectorsDesc")} />
            <MetricCard title={t("overview.linesOfCode")} value={m.total_lines_of_code} />
            <MetricCard title={t("overview.languages")} value={m.language_count} description={m.languages.length > 0 ? <LanguagePills languages={m.languages} marginTop={4} /> : t("overview.noneDetected")} />
          </ColumnLayout>
          {statusItems.length > 0 && (
            <div style={{ display: "flex", alignItems: "center", gap: 16, paddingTop: 4 }}>
              <Box variant="awsui-key-label">{t("overview.spaceHealth")}</Box>
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
