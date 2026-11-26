import { useTranslation } from "react-i18next";
import Box from "@cloudscape-design/components/box";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Icon from "@cloudscape-design/components/icon";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Badge from "@cloudscape-design/components/badge";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import { SUPPORTED_LANGUAGES, languageColor } from "../components/LanguagePills";

const FEATURE_THEMES: Record<string, { bg: string; accent: string; iconBg: string }> = {
  search:       { bg: "#fafbff", accent: "#0972d3", iconBg: "#0972d30d" },
  flow:         { bg: "#f8fcf9", accent: "#1d8102", iconBg: "#1d81020d" },
  overview:     { bg: "#f7faff", accent: "#2f6fb0", iconBg: "#2f6fb00d" },
  relevance:    { bg: "#fffaf4", accent: "#946200", iconBg: "#9462000d" },
  refresh:      { bg: "#f5fbfb", accent: "#0d7d70", iconBg: "#0d7d700d" },
  agent:        { bg: "#faf8ff", accent: "#7b61c4", iconBg: "#7b61c40d" },
  spaces:       { bg: "#fffbf7", accent: "#b8860b", iconBg: "#b8860b0d" },
  hyperspaces:  { bg: "#fef8f9", accent: "#c44a5b", iconBg: "#c44a5b0d" },
  global:       { bg: "#f6f9ff", accent: "#3b5bdb", iconBg: "#3b5bdb0d" },
  cost:         { bg: "#f9fbf7", accent: "#5c7a1e", iconBg: "#5c7a1e0d" },
  mcp:          { bg: "#f7fcfb", accent: "#0d7d70", iconBg: "#0d7d700d" },
  hyperspaceOverview: { bg: "#fef8f9", accent: "#c44a5b", iconBg: "#c44a5b0d" },
  dashboard:    { bg: "#f6fafb", accent: "#2a7f8e", iconBg: "#2a7f8e0d" },
  auth:         { bg: "#f7f9fc", accent: "#51607a", iconBg: "#51607a0d" },
  repo:         { bg: "#fffaf3", accent: "#b7791f", iconBg: "#b7791f0d" },
  settings:     { bg: "#f7f9fb", accent: "#5a7184", iconBg: "#5a71840d" },
};

function FeatureCard({ themeKey, icon, title, badge, children }: {
  themeKey: string; icon: string; title: string; badge?: React.ReactNode; children: React.ReactNode;
}) {
  const theme = FEATURE_THEMES[themeKey] ?? FEATURE_THEMES.search;
  return (
    <div style={{
      background: theme.bg, borderRadius: 12, padding: 20,
      border: `1px solid ${theme.accent}30`,
      transition: "box-shadow .15s ease",
    }}
      onMouseEnter={e => { e.currentTarget.style.boxShadow = `0 2px 8px ${theme.accent}10`; }}
      onMouseLeave={e => { e.currentTarget.style.boxShadow = "none"; }}
    >
      <SpaceBetween size="s">
        <div style={{
          width: 40, height: 40, borderRadius: 10, backgroundColor: theme.iconBg,
          display: "flex", alignItems: "center", justifyContent: "center", color: theme.accent,
        }}>
          <Icon name={icon as any} size="medium" />
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <Box variant="h3" color="text-body-secondary">
            <span style={{ color: theme.accent }}>{title}</span>
          </Box>
          {badge}
        </div>
        <Box variant="p" color="text-body-secondary" fontSize="body-s">{children}</Box>
      </SpaceBetween>
    </div>
  );
}

function StepCard({ step, title, children }: { step: number; title: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 20, alignItems: "flex-start" }}>
      <div style={{
        width: 32, height: 32, borderRadius: "50%",
        backgroundColor: "#0972d3",
        color: "#fff", display: "flex", alignItems: "center", justifyContent: "center",
        fontWeight: 600, fontSize: 14, flexShrink: 0,
      }}>
        {step}
      </div>
      <div style={{ flex: 1 }}>
        <Box variant="h4">{title}</Box>
        <Box variant="p" color="text-body-secondary">{children}</Box>
      </div>
    </div>
  );
}

// Numbered workflow step. Uses an amber badge to echo the yellow markers in the
// "How it works" diagram, visually tying the legend to the figure above it.
function FlowStep({ step, children }: { step: number; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
      <div style={{
        width: 28, height: 28, borderRadius: "50%",
        backgroundColor: "#f2cd00", color: "#16191f",
        display: "flex", alignItems: "center", justifyContent: "center",
        fontWeight: 700, fontSize: 13, flexShrink: 0,
        boxShadow: "0 0 0 3px #f2cd0022",
      }}>
        {step}
      </div>
      <Box variant="p" color="text-body-secondary">{children}</Box>
    </div>
  );
}

function ConceptCard({ icon, title, children }: { icon: string; title: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
      <div style={{ color: "#0972d3", marginTop: 2 }}>
        <Icon name={icon as any} size="medium" />
      </div>
      <div>
        <Box variant="h4">{title}</Box>
        <Box variant="p" color="text-body-secondary">{children}</Box>
      </div>
    </div>
  );
}

// Framed figure for a diagram image. White matte keeps white-background diagrams
// looking intentional in both light and dark themes. If the image is missing,
// the alt text is shown so the page never looks broken.
function Figure({ src, alt, caption }: { src: string; alt: string; caption?: React.ReactNode }) {
  return (
    <figure style={{ margin: 0 }}>
      <div style={{
        background: "#ffffff", border: "1px solid #e9ebed", borderRadius: 12,
        padding: 24, display: "flex", justifyContent: "center",
      }}>
        <img
          src={src}
          alt={alt}
          loading="lazy"
          style={{ width: "100%", maxWidth: 720, height: "auto" }}
        />
      </div>
      {caption && (
        <figcaption style={{ marginTop: 8, textAlign: "center" }}>
          <Box variant="small" color="text-body-secondary">{caption}</Box>
        </figcaption>
      )}
    </figure>
  );
}

function AnnotationRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "baseline" }}>
      <div style={{ minWidth: 150 }}>
        <Box variant="awsui-key-label">{label}</Box>
      </div>
      <Box variant="p" color="text-body-secondary" fontSize="body-s">{children}</Box>
    </div>
  );
}

function BenefitPill({ icon, text }: { icon: string; text: string }) {
  return (
    <div style={{
      display: "inline-flex", alignItems: "center", gap: 8,
      padding: "6px 14px", borderRadius: 999,
      background: "#0972d30d", border: "1px solid #0972d322", color: "#0972d3",
    }}>
      <Icon name={icon as any} size="small" />
      <span style={{ fontSize: 13, fontWeight: 600 }}>{text}</span>
    </div>
  );
}

function LanguageBadge({ name, color, available }: { name: string; color: string; available: boolean }) {
  const { t } = useTranslation();
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 12, padding: "12px 20px",
      borderRadius: 10, backgroundColor: `${color}08`, border: `1px solid ${color}25`,
    }}>
      <div style={{
        width: 8, height: 8, borderRadius: "50%", backgroundColor: color,
      }} />
      <div style={{ flex: 1 }}>
        <Box variant="p"><span style={{ fontWeight: 600 }}>{name}</span></Box>
      </div>
      {available && <Badge color="green">{t("documentation.available")}</Badge>}
    </div>
  );
}

function ScoringGroup({ title, items }: { title: string; items: string[] }) {
  return (
    <div>
      <Box variant="h4">{title}</Box>
      <ul style={{ marginTop: 8, paddingLeft: 20, color: "#5f6b7a", lineHeight: 1.6 }}>
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

export default function Documentation() {
  const { t } = useTranslation();
  const mcpUrl = <code>https://&lt;your-domain&gt;/mcp/mcp</code>;
  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup items={[{ text: "CodeKB", href: "/" }, { text: t("documentation.breadcrumbUserGuide"), href: "/docs" }]} />

      <Header variant="h1" description={t("documentation.description")}>
        {t("documentation.title")}
      </Header>

      {/* 1. What is CodeKB */}
      <Container header={<Header variant="h2">{t("documentation.what.title")}</Header>}>
        <SpaceBetween size="m">
          <Box variant="p">
            {t("documentation.what.p1")}
          </Box>
          <Box variant="p">
            {t("documentation.what.p2")}
          </Box>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
            <BenefitPill icon="search" text={t("documentation.what.pillFind")} />
            <BenefitPill icon="share" text={t("documentation.what.pillTrace")} />
            <BenefitPill icon="gen-ai" text={t("documentation.what.pillOverviews")} />
            <BenefitPill icon="refresh" text={t("documentation.what.pillUpToDate")} />
            <BenefitPill icon="settings" text={t("documentation.what.pillMcp")} />
          </div>
        </SpaceBetween>
      </Container>

      {/* 2. Who it's for and use cases */}
      <Container header={<Header variant="h2">{t("documentation.who.title")}</Header>}>
        <Box variant="p">
          {t("documentation.who.body")}
        </Box>
      </Container>

      {/* 3. Core concepts */}
      <Container header={
        <Header variant="h2" description={t("documentation.concepts.description")}>
          {t("documentation.concepts.title")}
        </Header>
      }>
        <ColumnLayout columns={2}>
          <ConceptCard icon="folder" title={t("documentation.concepts.spacesTitle")}>
            {t("documentation.concepts.spacesBody")}
          </ConceptCard>
          <ConceptCard icon="group-active" title={t("documentation.concepts.hyperspacesTitle")}>
            {t("documentation.concepts.hyperspacesBody")}
          </ConceptCard>
        </ColumnLayout>
      </Container>

      {/* 4. Getting started */}
      <Container header={
        <Header variant="h2" description={t("documentation.start.description")}>
          {t("documentation.start.title")}
        </Header>
      }>
        <SpaceBetween size="l">
          {[1, 2, 3, 4, 5, 6, 7, 8, 9].map((n) => (
            <StepCard key={n} step={n} title={t(`documentation.start.step${n}Title`)}>
              {t(`documentation.start.step${n}Body`)}
            </StepCard>
          ))}
        </SpaceBetween>
      </Container>

      {/* 5. Key features */}
      <Header variant="h2">{t("documentation.features.title")}</Header>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", columnGap: 24, rowGap: 24 }}>
        <FeatureCard themeKey="search" icon="search" title={t("documentation.features.semanticTitle")}>
          {t("documentation.features.semanticBody")}
        </FeatureCard>
        <FeatureCard themeKey="flow" icon="share" title={t("documentation.features.callGraphTitle")}>
          {t("documentation.features.callGraphBody")}
        </FeatureCard>
        <FeatureCard themeKey="overview" icon="file" title={t("documentation.features.overviewTitle")}>
          {t("documentation.features.overviewBody")}
        </FeatureCard>
        <FeatureCard themeKey="hyperspaceOverview" icon="group-active" title={t("documentation.features.hyperspaceOverviewTitle")}>
          {t("documentation.features.hyperspaceOverviewBody")}
        </FeatureCard>
        <FeatureCard
          themeKey="relevance"
          icon="star"
          title={t("documentation.features.relevanceTitle")}
          badge={<Badge color="blue">{t("documentation.experimental")}</Badge>}
        >
          {t("documentation.features.relevanceBody")}
        </FeatureCard>
        <FeatureCard themeKey="refresh" icon="refresh" title={t("documentation.features.refreshTitle")}>
          {t("documentation.features.refreshBody")}
        </FeatureCard>
        <FeatureCard themeKey="agent" icon="gen-ai" title={t("documentation.features.agentTitle")}>
          {t("documentation.features.agentBodyPrefix")}<strong>{t("documentation.features.agentChat")}</strong>{t("documentation.features.agentBodyMiddle")}<strong>{t("documentation.features.agentImpact")}</strong>{t("documentation.features.agentBodySuffix")}
        </FeatureCard>
        <FeatureCard themeKey="global" icon="search" title={t("documentation.features.globalTitle")}>
          {t("documentation.features.globalBody")}
        </FeatureCard>
        <FeatureCard themeKey="dashboard" icon="view-full" title={t("documentation.features.dashboardTitle")}>
          {t("documentation.features.dashboardBody")}
        </FeatureCard>
        <FeatureCard themeKey="cost" icon="status-info" title={t("documentation.features.costTitle")}>
          {t("documentation.features.costBody")}
        </FeatureCard>
        <FeatureCard themeKey="auth" icon="lock-private" title={t("documentation.features.authTitle")}>
          {t("documentation.features.authBody")}
        </FeatureCard>
        <FeatureCard themeKey="repo" icon="key" title={t("documentation.features.repoTitle")}>
          {t("documentation.features.repoBody")}
        </FeatureCard>
        <FeatureCard themeKey="settings" icon="settings" title={t("documentation.features.settingsTitle")}>
          {t("documentation.features.settingsBody")}
        </FeatureCard>
        <FeatureCard themeKey="mcp" icon="settings" title={t("documentation.features.mcpTitle")}>
          {t("documentation.features.mcpBody")}
        </FeatureCard>
      </div>

      {/* 6. Authentication and access */}
      <div style={{ marginTop: 24 }} />
      <Container header={
        <Header variant="h2" description={t("documentation.access.description")}>
          {t("documentation.access.title")}
        </Header>
      }>
        <SpaceBetween size="m">
          <div>
            <Box variant="h4">{t("documentation.access.signInTitle")}</Box>
            <Box variant="p" color="text-body-secondary">
              {t("documentation.access.signInBody")}
            </Box>
          </div>
          <div>
            <Box variant="h4">{t("documentation.access.assistantTitle")}</Box>
            <Box variant="p" color="text-body-secondary">
              {t("documentation.access.assistantBody")}
            </Box>
          </div>
          <div>
            <Box variant="h4">{t("documentation.access.privateTitle")}</Box>
            <Box variant="p" color="text-body-secondary">
              {t("documentation.access.privateBody")}
            </Box>
          </div>
        </SpaceBetween>
      </Container>

      {/* 7. MCP and integrations */}
      <Container header={<Header variant="h2">{t("documentation.mcp.title")}</Header>}>
        <SpaceBetween size="s">
          <Box variant="p">
            {t("documentation.mcp.intro")}
          </Box>
          <Box variant="h4">{t("documentation.mcp.configTitle")}</Box>
          <div style={{
            backgroundColor: "#f4f4f4", borderRadius: 8, padding: 16,
            fontFamily: "monospace", fontSize: 13, color: "#414d5c",
            lineHeight: 1.7, border: "1px solid #e9ebed",
            whiteSpace: "pre", overflowX: "auto",
          }}>
            <span style={{ color: "#687078" }}>{"{"}</span>{"\n"}
            {"  "}<span style={{ color: "#0972d3" }}>"mcpServers"</span>: <span style={{ color: "#687078" }}>{"{"}</span>{"\n"}
            {"    "}<span style={{ color: "#0972d3" }}>"aws-codekb"</span>: <span style={{ color: "#687078" }}>{"{"}</span>{"\n"}
            {"      "}<span style={{ color: "#0972d3" }}>"type"</span>: <span style={{ color: "#1d8102" }}>"http"</span>,{"\n"}
            {"      "}<span style={{ color: "#0972d3" }}>"url"</span>: <span style={{ color: "#1d8102" }}>"https://&lt;your-domain&gt;/mcp/mcp"</span>{"\n"}
            {"    "}<span style={{ color: "#687078" }}>{"}"}</span>{"\n"}
            {"  "}<span style={{ color: "#687078" }}>{"}"}</span>{"\n"}
            <span style={{ color: "#687078" }}>{"}"}</span>
          </div>
          <Box variant="p" color="text-body-secondary">
            {t("documentation.mcp.configNotePrefix")}<code>&lt;your-domain&gt;</code>{t("documentation.mcp.configNoteSuffix")}
          </Box>
          <Box variant="h4">{t("documentation.mcp.canDoTitle")}</Box>
          <Box variant="p" color="text-body-secondary">
            {t("documentation.mcp.canDoBody")}
          </Box>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {[
              "load_knowledge_base", "find_component", "search_components", "list_components",
              "show_component", "trace_flow", "show_dependencies", "get_call_graph_with_source",
              "get_stats", "list_spaces", "list_hyperspaces", "get_space_details",
              "get_hyperspace_details", "get_component_from_description",
            ].map((tool) => (
              <code key={tool} style={{
                background: "#f4f4f4", border: "1px solid #e9ebed", borderRadius: 6,
                padding: "2px 8px", fontSize: 12, color: "#414d5c",
              }}>{tool}</code>
            ))}
          </div>
        </SpaceBetween>
      </Container>

      {/* 8. How it works (architecture + pipeline) */}
      <Container header={
        <Header variant="h2" description={t("documentation.how.description")}>
          {t("documentation.how.title")}
        </Header>
      }>
        <SpaceBetween size="l">
          <Box variant="p">
            {t("documentation.how.intro")}
          </Box>
          <Figure
            src={`${import.meta.env.BASE_URL}diagrams/architecture.jpg`}
            alt={t("documentation.how.architectureAlt")}
            caption={t("documentation.how.architectureCaption")}
          />
          <ColumnLayout columns={2}>
            <AnnotationRow label={t("documentation.how.webUiLabel")}>{t("documentation.how.webUiBody")}</AnnotationRow>
            <AnnotationRow label={t("documentation.how.agentsLabel")}>{t("documentation.how.agentsBody")}</AnnotationRow>
            <AnnotationRow label={t("documentation.how.sourceLabel")}>{t("documentation.how.sourceBody")}</AnnotationRow>
            <AnnotationRow label={t("documentation.how.automationsLabel")}>{t("documentation.how.automationsBody")}</AnnotationRow>
            <AnnotationRow label={t("documentation.how.storageLabel")}>{t("documentation.how.storageBody")}</AnnotationRow>
            <AnnotationRow label={t("documentation.how.lifecycleLabel")}>{t("documentation.how.lifecycleBody")}</AnnotationRow>
          </ColumnLayout>
          <Figure
            src={`${import.meta.env.BASE_URL}diagrams/workflow.jpg`}
            alt={t("documentation.how.workflowAlt")}
            caption={t("documentation.how.workflowCaption")}
          />
          <SpaceBetween size="m">
            {[1, 2, 3, 4, 5, 6, 7].map((n) => (
              <FlowStep key={n} step={n}>{t(`documentation.how.flow${n}`)}</FlowStep>
            ))}
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      {/* 9. Business behaviour relevance */}
      <Container
        header={
          <Header
            variant="h2"
            description={t("documentation.relevance.description")}
          >
            <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
              <span id="business-relevance">{t("documentation.relevance.title")}</span>
              <Badge color="blue">{t("documentation.experimental")}</Badge>
            </span>
          </Header>
        }
      >
        <SpaceBetween size="m">
          <Box variant="p">
            {t("documentation.relevance.p1")}
          </Box>
          <Box variant="p">
            {t("documentation.relevance.p2")}
          </Box>
          <ColumnLayout columns={2} variant="text-grid">
            <ScoringGroup
              title={t("documentation.relevance.graphTitle")}
              items={[1, 2, 3].map((n) => t(`documentation.relevance.graph${n}`))}
            />
            <ScoringGroup
              title={t("documentation.relevance.businessTitle")}
              items={[1, 2, 3, 4, 5, 6, 7].map((n) => t(`documentation.relevance.business${n}`))}
            />
            <ScoringGroup
              title={t("documentation.relevance.metadataTitle")}
              items={[1, 2, 3, 4, 5].map((n) => t(`documentation.relevance.metadata${n}`))}
            />
            <ScoringGroup
              title={t("documentation.relevance.penaltiesTitle")}
              items={[1, 2, 3].map((n) => t(`documentation.relevance.penalties${n}`))}
            />
          </ColumnLayout>
          <Box variant="p">
            {t("documentation.relevance.p3")}
          </Box>
        </SpaceBetween>
      </Container>

      {/* 10. Limits, data, and cost */}
      <Container header={<Header variant="h2">{t("documentation.limits.title")}</Header>}>
        <SpaceBetween size="m">
          <div>
            <Box variant="h4">{t("documentation.limits.sizeTitle")}</Box>
            <Box variant="p" color="text-body-secondary">
              {t("documentation.limits.sizeBody")}
            </Box>
          </div>
          <div>
            <Box variant="h4">{t("documentation.limits.exclusionsTitle")}</Box>
            <Box variant="p" color="text-body-secondary">
              {t("documentation.limits.exclusionsPrefix")}<code>node_modules</code>{t("documentation.limits.exclusionsAnd")}<code>__pycache__</code>{t("documentation.limits.exclusionsAuto")}<code>.gitignore</code>{t("documentation.limits.exclusionsGitignoreSuffix")}<code>CODEKB_EXCLUDE_PATTERNS_EXTRA</code>{t("documentation.limits.exclusionsSuffix")}
            </Box>
          </div>
          <div>
            <Box variant="h4">{t("documentation.limits.dataTitle")}</Box>
            <Box variant="p" color="text-body-secondary">
              {t("documentation.limits.dataBody")}
            </Box>
          </div>
          <div>
            <Box variant="h4">{t("documentation.limits.costTitle")}</Box>
            <Box variant="p" color="text-body-secondary">
              {t("documentation.limits.costBody")}
            </Box>
          </div>
        </SpaceBetween>
      </Container>

      {/* 11. FAQ / troubleshooting */}
      <Container header={
        <Header variant="h2" description={t("documentation.faq.description")}>
          {t("documentation.faq.title")}
        </Header>
      }>
        <SpaceBetween size="xs">
          <ExpandableSection headerText={t("documentation.faq.dataQ")}>
            {t("documentation.faq.dataA")}
          </ExpandableSection>
          <ExpandableSection headerText={t("documentation.faq.costQ")}>
            {t("documentation.faq.costA")}
          </ExpandableSection>
          <ExpandableSection headerText={t("documentation.faq.mcpQ")}>
            {t("documentation.faq.mcpAPrefix")}{mcpUrl}{t("documentation.faq.mcpASuffix")}
          </ExpandableSection>
          <ExpandableSection headerText={t("documentation.faq.hyperspaceQ")}>
            {t("documentation.faq.hyperspaceA")}
          </ExpandableSection>
          <ExpandableSection headerText={t("documentation.faq.impactQ")}>
            {t("documentation.faq.impactA")}
          </ExpandableSection>
          <ExpandableSection headerText={t("documentation.faq.everythingQ")}>
            {t("documentation.faq.everythingA")}
          </ExpandableSection>
          <ExpandableSection headerText={t("documentation.faq.setupQ")}>
            {t("documentation.faq.setupA")}
          </ExpandableSection>
          <ExpandableSection headerText={t("documentation.faq.recreateQ")}>
            {t("documentation.faq.recreateA")}
          </ExpandableSection>
          <ExpandableSection headerText={t("documentation.faq.sizeQ")}>
            {t("documentation.faq.sizeAPrefix")}<code>node_modules</code>{t("documentation.faq.sizeAAnd")}<code>__pycache__</code>{t("documentation.faq.sizeAAuto")}<code>.gitignore</code>{t("documentation.faq.sizeASuffix")}
          </ExpandableSection>
          <ExpandableSection headerText={t("documentation.faq.patternsQ")}>
            {t("documentation.faq.patternsA")}
          </ExpandableSection>
          <ExpandableSection headerText={t("documentation.faq.multiQ")}>
            {t("documentation.faq.multiA")}
          </ExpandableSection>
        </SpaceBetween>
      </Container>

      {/* Troubleshooting */}
      <Container header={<Header variant="h2">{t("documentation.troubleshooting.title")}</Header>}>
        <SpaceBetween size="xs">
          <ExpandableSection headerText={t("documentation.troubleshooting.missingQ")}>
            {t("documentation.troubleshooting.missingA")}
          </ExpandableSection>
          <ExpandableSection headerText={t("documentation.troubleshooting.branchQ")}>
            {t("documentation.troubleshooting.branchA")}
          </ExpandableSection>
        </SpaceBetween>
      </Container>

      {/* 12. Supported languages */}
      <Container header={
        <Header variant="h2" description={t("documentation.languages.description")}>
          {t("documentation.languages.title")}
        </Header>
      }>
        {/* Rendered from the shared list and color map in LanguagePills, so this
            grid stays in step with the Create space form instead of carrying its
            own copy of both. */}
        <ColumnLayout columns={4}>
          {SUPPORTED_LANGUAGES.map((name) => (
            <LanguageBadge key={name} name={name} color={languageColor(name)} available />
          ))}
        </ColumnLayout>
      </Container>
    </SpaceBetween>
  );
}
