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
      {available && <Badge color="green">Available</Badge>}
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
  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup items={[{ text: "CodeKB", href: "/" }, { text: "User Guide", href: "/docs" }]} />

      <Header variant="h1" description="Learn how to use CodeKB to analyze, explore, and understand your codebases.">
        User Guide
      </Header>

      {/* 1. What is CodeKB */}
      <Container header={<Header variant="h2">What is CodeKB?</Header>}>
        <SpaceBetween size="m">
          <Box variant="p">
            CodeKB is an AI-powered code analysis system that extracts structure from your source code,
            builds call graphs, and enables semantic search across your entire codebase. It helps you
            understand how code is organized, trace execution flows, and find components by describing
            what they do in plain English.
          </Box>
          <Box variant="p">
            It's built for engineers ramping up on an unfamiliar service, teams doing brownfield work
            across many repositories, and AI coding assistants that need reliable, structured context
            about a codebase rather than raw file access.
          </Box>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
            <BenefitPill icon="search" text="Find code by intent" />
            <BenefitPill icon="share" text="Trace execution flows" />
            <BenefitPill icon="gen-ai" text="AI-generated overviews" />
            <BenefitPill icon="refresh" text="Always up to date" />
            <BenefitPill icon="settings" text="Works in your editor via MCP" />
          </div>
        </SpaceBetween>
      </Container>

      {/* 2. Who it's for and use cases */}
      <Container header={<Header variant="h2">Who it's for and use cases</Header>}>
        <Box variant="p">
          CodeKB is built for engineers ramping onto an unfamiliar codebase, teams doing brownfield work
          across many repositories, and AI assistants that need grounded, structured knowledge of a code
          base rather than raw file access. If you are joining a new service, auditing how a feature flows
          across repos, or wiring an IDE assistant to understand your code, CodeKB is for you.
        </Box>
      </Container>

      {/* 3. Core concepts */}
      <Container header={
        <Header variant="h2" description="Two ideas you'll see throughout the product">
          Core concepts
        </Header>
      }>
        <ColumnLayout columns={2}>
          <ConceptCard icon="folder" title="Spaces">
            A space is a self-contained knowledge base built from one codebase. It holds the parsed
            component catalog, call graph, statistics, and vector embeddings. Create a space from a
            Git repository or a zip upload.
          </ConceptCard>
          <ConceptCard icon="group-active" title="Hyperspaces">
            A hyperspace groups one or more spaces so you can search and query across related
            codebases as a single unit — handy for microservice fleets or multi-repo platforms.
          </ConceptCard>
        </ColumnLayout>
      </Container>

      {/* 4. Getting started */}
      <Container header={
        <Header variant="h2" description="Follow these steps to get up and running">
          Getting started
        </Header>
      }>
        <SpaceBetween size="l">
          <StepCard step={1} title="Create a space">
            Go to the Spaces page and click "New space". Provide a Git repository URL (HTTPS or SSH) and
            optionally pick a branch or tag, or upload a zip of your source. Under Analysis options you
            can omit low business relevance components from AI summarization to save time and cost.
          </StepCard>
          <StepCard step={2} title="Track the analysis">
            Watch the live pipeline steps and summarization progress bar on the space page. CodeKB
            reports any files skipped because their language isn't supported yet.
          </StepCard>
          <StepCard step={3} title="Explore your code">
            Open a space and use the Syntactic Search tab to find components by name, pattern, or type,
            or the Semantic Search tab to find them by describing what they do. From any component, view
            source, trace its execution flow, and inspect dependencies.
          </StepCard>
          <StepCard step={4} title="Generate a Space Overview">
            Open the Space Overview tab to generate AI documentation of the codebase's architecture,
            components, and data flows. Regenerate it after big changes.
          </StepCard>
          <StepCard step={5} title="Search across repositories">
            Use Global search to run one query across all spaces, a chosen subset, or a hyperspace.
            Group related spaces into a hyperspace from the Hyperspaces page.
          </StepCard>
          <StepCard step={6} title="Keep spaces fresh">
            For Git-sourced spaces, click "Refresh space" to run a space refresh against the latest
            commit.
          </StepCard>
          <StepCard step={7} title="Track usage and cost">
            Each space shows token usage per operation with cost estimates.
          </StepCard>
          <StepCard step={8} title="Ask Agent Hopper">
            Open Agent Hopper from the sidebar, select one or more spaces, and ask questions in plain
            English. Switch to Impact Analysis mode to assess the effect of a described change.
          </StepCard>
          <StepCard step={9} title="Connect via MCP">
            Add CodeKB as an MCP server in your AI coding assistant (Kiro, Amazon Q Developer CLI, or any
            MCP-compatible tool). See the MCP setup section below for the config snippet.
          </StepCard>
        </SpaceBetween>
      </Container>

      {/* 5. Key features */}
      <Header variant="h2">Key features</Header>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", columnGap: 24, rowGap: 24 }}>
        <FeatureCard themeKey="search" icon="search" title="Semantic search">
          Find components by what they do, not just literal text, using vector search over AI-written
          summaries.
        </FeatureCard>
        <FeatureCard themeKey="flow" icon="share" title="Call graph analysis">
          Trace callers and callees across the whole code base to see how functions and components call
          each other.
        </FeatureCard>
        <FeatureCard themeKey="overview" icon="file" title="AI Space Overview">
          Generates Business Overview, Application Architecture, Technology Stack, and Deployment docs
          for a single space.
        </FeatureCard>
        <FeatureCard themeKey="hyperspaceOverview" icon="group-active" title="Hyperspace Overview">
          A system-level view across a hyperspace: what the whole system does, plus a map of how its
          member spaces connect.
        </FeatureCard>
        <FeatureCard
          themeKey="relevance"
          icon="star"
          title="Business relevance scoring"
          badge={<Badge color="blue">Experimental</Badge>}
        >
          Ranks components by how likely they carry business logic, so AI summarization focuses where it
          matters.
        </FeatureCard>
        <FeatureCard themeKey="refresh" icon="refresh" title="Space refresh">
          Re-analyzes only the files changed since the last commit, then swaps in the new index
          atomically.
        </FeatureCard>
        <FeatureCard themeKey="agent" icon="gen-ai" title="Agent Hopper">
          Two modes. <strong>Chat</strong> answers questions about your code; <strong>Impact Analysis</strong>{" "}
          traces the components and flows a described change would touch.
        </FeatureCard>
        <FeatureCard themeKey="global" icon="search" title="Global search">
          Run one query across all spaces, a chosen subset, or a whole hyperspace, and rank matches
          across them.
        </FeatureCard>
        <FeatureCard themeKey="dashboard" icon="view-full" title="Visual overview dashboard">
          The home page aggregates metrics and renders an interactive Treemap of your
          spaces and hyperspaces.
        </FeatureCard>
        <FeatureCard themeKey="cost" icon="status-info" title="Usage & cost visibility">
          Per-operation Bedrock token counts and cost estimates, broken down per space and per
          hyperspace synthesis.
        </FeatureCard>
        <FeatureCard themeKey="auth" icon="lock-private" title="Authentication">
          OAuth 2.0 / OIDC sign-in via Amazon Cognito, federatable to your own IdP, plus an
          OAuth-protected MCP endpoint.
        </FeatureCard>
        <FeatureCard themeKey="repo" icon="key" title="Private repository access">
          Save a per-provider access token (GitHub, GitLab, or Bitbucket) to clone private repos,
          encrypted and scoped to you.
        </FeatureCard>
        <FeatureCard themeKey="settings" icon="settings" title="Settings and infrastructure status">
          Shows live readiness of the S3 and Vectors buckets, the vector index, the git-credentials KMS
          key, the active Bedrock model and region, and which providers are enabled.
        </FeatureCard>
        <FeatureCard themeKey="mcp" icon="settings" title="MCP integration">
          Expose search, overviews, and code tracing to IDE assistants over an OAuth-protected MCP
          endpoint.
        </FeatureCard>
      </div>

      {/* 6. Authentication and access */}
      <div style={{ marginTop: 24 }} />
      <Container header={
        <Header variant="h2" description="How people and AI assistants sign in, and how private repos are reached">
          Authentication and access
        </Header>
      }>
        <SpaceBetween size="m">
          <div>
            <Box variant="h4">Signing in</Box>
            <Box variant="p" color="text-body-secondary">
              CodeKB authenticates users with OAuth 2.0 / OIDC through Amazon Cognito. Cognito can
              federate to your own identity provider, so each deployment can plug in its own corporate
              sign-in. Sign-in is optional and off by default when you self-host: the shipped
              configuration runs with authentication disabled and bound to localhost, so you turn on
              Cognito (the reference CDK wires it for you) or put your own identity provider in front
              before exposing CodeKB beyond your machine.
            </Box>
          </div>
          <div>
            <Box variant="h4">Connecting an AI assistant (MCP)</Box>
            <Box variant="p" color="text-body-secondary">
              The MCP endpoint is OAuth-protected. Your IDE assistant runs a standard OAuth sign-in the
              first time it connects, handled by a built-in proxy, so you do not manage tokens by hand.
            </Box>
          </div>
          <div>
            <Box variant="h4">Accessing private repositories</Box>
            <Box variant="p" color="text-body-secondary">
              Private-repo ingestion is separate from sign-in and does not use an OAuth app flow. You save
              a personal access token for your Git provider (GitHub by default; GitLab and Bitbucket when
              your deployment enables them). The token is stored encrypted, is never shown back to you,
              and is scoped to your account.
            </Box>
          </div>
        </SpaceBetween>
      </Container>

      {/* 7. MCP and integrations */}
      <Container header={<Header variant="h2">MCP and integrations</Header>}>
        <SpaceBetween size="s">
          <Box variant="p">
            After deploying CodeKB, you can connect it as an MCP server to your AI coding assistant.
            The server exposes tools for loading spaces, searching components, tracing flows, and
            running semantic queries.
          </Box>
          <Box variant="h4">MCP configuration</Box>
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
            Add this to your MCP client's configuration file (e.g. Kiro, Amazon Q Developer CLI, or any MCP-compatible tool).
            Replace <code>&lt;your-domain&gt;</code> with your deployed CodeKB URL. On an auth-enforced
            instance, your client completes an OAuth sign-in on first connect.
          </Box>
          <Box variant="h4">What your assistant can do</Box>
          <Box variant="p" color="text-body-secondary">
            Once connected, the assistant can load a space, search components, trace flows, inspect
            dependencies, pull the call graph, and list spaces and hyperspaces. The MCP endpoint exposes
            these tools:
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
        <Header variant="h2" description="From raw source to an AI assistant that understands your codebase">
          How it works
        </Header>
      }>
        <SpaceBetween size="l">
          <Box variant="p">
            CodeKB runs as a service that sits between your source code and the people and tools that
            consume it. A web UI and REST API drive analysis and exploration, coding agents connect over
            MCP, and the knowledge base is persisted in S3 with embeddings and summaries produced by
            Amazon Bedrock models.
          </Box>
          <Figure
            src={`${import.meta.env.BASE_URL}diagrams/architecture.jpg`}
            alt="CodeKB architecture: the CodeKB service connects the web UI (REST API), identity providers (OAuth2/SAML), and coding agents (MCP) to source inputs (Git, zip, S3) and backing stores (S3 component catalog, S3 Vectors knowledge base, and Bedrock embedding and LLM models)."
            caption="CodeKB system architecture. Some integration points are forward-looking."
          />
          <ColumnLayout columns={2}>
            <AnnotationRow label="Web UI">Create spaces, browse components, and search — over the REST API.</AnnotationRow>
            <AnnotationRow label="Coding agents">Connect over MCP to query the knowledge base from your editor.</AnnotationRow>
            <AnnotationRow label="Source code">Ingested from a Git repository, a zip upload, or S3.</AnnotationRow>
            <AnnotationRow label="Automations">REST API and webhooks to keep spaces fresh from CI/CD.</AnnotationRow>
            <AnnotationRow label="Storage &amp; models">S3 component catalog, S3 Vectors knowledge base, and Bedrock embedding + LLM models.</AnnotationRow>
            <AnnotationRow label="Lifecycle">A space moves through ANALYZING, SUMMARIZING, READY, FAILED, and REFRESHING as it is built and kept up to date.</AnnotationRow>
          </ColumnLayout>
          <Figure
            src={`${import.meta.env.BASE_URL}diagrams/workflow.jpg`}
            alt="End-to-end CodeKB workflow: a developer analyzes a codebase in CodeKB, which ingests and parses it, summarizes functions with an LLM, vectorizes the summaries, and persists everything in S3; the developer then works in an AI coding assistant that queries LLMs and issues CodeKB MCP commands to navigate the codebase."
            caption="The end-to-end CodeKB workflow."
          />
          <SpaceBetween size="m">
            <FlowStep step={1}>A developer uses the CodeKB UI to analyze an application codebase.</FlowStep>
            <FlowStep step={2}>CodeKB ingests the codebase (Git clone, zip upload, or S3).</FlowStep>
            <FlowStep step={3}>
              CodeKB parses the code and builds the call graph, invokes an LLM to summarize functions,
              and invokes an embeddings model to vectorize those summaries.
            </FlowStep>
            <FlowStep step={4}>
              CodeKB persists the component catalog, a source clone, and the function-summary vectors in
              AWS S3.
            </FlowStep>
            <FlowStep step={5}>
              The developer initiates brownfield development tasks in an AI coding assistant.
            </FlowStep>
            <FlowStep step={6}>The AI coding assistant queries LLMs.</FlowStep>
            <FlowStep step={7}>
              The LLM issues CodeKB MCP commands to navigate the brownfield codebase structure in a loop.
            </FlowStep>
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      {/* 9. Business behaviour relevance */}
      <Container
        header={
          <Header
            variant="h2"
            description="How CodeKB decides which components are worth deeper AI summarization"
          >
            <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
              <span id="business-relevance">Business behaviour relevance</span>
              <Badge color="blue">Experimental</Badge>
            </span>
          </Header>
        }
      >
        <SpaceBetween size="m">
          <Box variant="p">
            Business behaviour relevance is a computational score from 0 to 100 that estimates
            whether a function or method represents meaningful product behaviour, workflow logic,
            or domain orchestration. Low-scoring components are usually framework glue, helpers,
            adapters, tests, generated code, or tiny technical utilities.
          </Box>
          <Box variant="p">
            The score is calculated after parsing and call graph creation, before AI summarization.
            It does not require an LLM per component. If AI summarization is enabled, CodeKB can
            optionally omit components in the low tier to reduce runtime and token usage.
          </Box>
          <ColumnLayout columns={2} variant="text-grid">
            <ScoringGroup
              title="Graph signals"
              items={[
                "Call depth: +8 per level, capped at +32",
                "Fan in: +4 per caller, capped at +20",
                "Fan out: +3 per dependency, capped at +18",
              ]}
            />
            <ScoringGroup
              title="Business/domain signals"
              items={[
                "Generic business terms in name: +6 each, capped at +18",
                "Repository-specific business terms in name: +10 each, capped at +30",
                "Business path terms: +5 each, capped at +15",
                "Repository business modules in path: +8 each, capped at +24",
                "Repository entrypoint patterns: +30 each, capped at +40",
                "Detected domain concepts: +5 each, capped at +15",
                "Business-facing patterns: +4 each, capped at +12",
              ]}
            />
            <ScoringGroup
              title="Component metadata"
              items={[
                "Documentation present: +10",
                "Public visibility: +8",
                "Complexity above 1: +2 per point, capped at +12",
                "Implementation length of 20 lines or more: +8",
                "Implementation length of 3 lines or less: -10",
              ]}
            />
            <ScoringGroup
              title="Technical penalties"
              items={[
                "Generic technical terms in name: -6 each, capped at -18",
                "Repository-specific technical terms in name: -8 each, capped at -24",
                "Technical path terms: -8 each, capped at -24",
              ]}
            />
          </ColumnLayout>
          <Box variant="p">
            Final scores are clamped to the 0-100 range. CodeKB labels scores below 25 as low,
            25-49.9 as heuristic, 50-79.9 as medium, and 80 or above as high. The “omit low
            business relevance” option skips only the low tier during AI summarization; the
            components still remain in the parsed catalog and call graph.
          </Box>
        </SpaceBetween>
      </Container>

      {/* 10. Limits, data, and cost */}
      <Container header={<Header variant="h2">Limits, data, and cost</Header>}>
        <SpaceBetween size="m">
          <div>
            <Box variant="h4">Repository size</Box>
            <Box variant="p" color="text-body-secondary">
              There is no hard cap. Code bases beyond 2M lines have been analyzed.
            </Box>
          </div>
          <div>
            <Box variant="h4">File exclusions</Box>
            <Box variant="p" color="text-body-secondary">
              CodeKB skips common noise such as <code>node_modules</code> and <code>__pycache__</code>{" "}
              automatically, and you can add your own <code>.gitignore</code>-style exclusion list via
              configuration (<code>CODEKB_EXCLUDE_PATTERNS_EXTRA</code>). A space keeps the patterns it
              was created with.
            </Box>
          </div>
          <div>
            <Box variant="h4">Where your data lives</Box>
            <Box variant="p" color="text-body-secondary">
              The source clone, the component catalog, and the summary vectors are stored in S3 and
              S3 Vectors in the AWS account where CodeKB runs. That is the account that operates your
              deployment, and your code stays within it.
            </Box>
          </div>
          <div>
            <Box variant="h4">Cost</Box>
            <Box variant="p" color="text-body-secondary">
              CodeKB runs on Amazon Bedrock, S3, and S3 Vectors. The main cost is Bedrock LLM tokens:
              a one-time cost when a space is summarized, plus recurring cost for chat and queries.
              Embedding cost is negligible by comparison. Every space shows the tokens used for creation
              and for each space refresh.
            </Box>
          </div>
        </SpaceBetween>
      </Container>

      {/* 11. FAQ / troubleshooting */}
      <Container header={
        <Header variant="h2" description="Quick answers to common questions">
          FAQ
        </Header>
      }>
        <SpaceBetween size="xs">
          <ExpandableSection headerText="Where does my code go, and where is my data stored?">
            CodeKB stores the source clone, the component catalog, and the summary vectors in S3 and
            S3 Vectors in the AWS account where it runs. That is the account that operates your
            deployment, so your code stays within it.
          </ExpandableSection>
          <ExpandableSection headerText="What does it cost?">
            CodeKB runs on Amazon Bedrock, S3, and S3 Vectors. The main cost is Bedrock LLM tokens:
            a one-time cost when a space is summarized, plus recurring cost for chat and queries.
            Embedding cost is negligible by comparison. Every space shows the tokens used for creation
            and for each space refresh, so you can price it against the Bedrock pricing page. Tip:
            enable "omit low business relevance components" to cut summarization cost.
          </ExpandableSection>
          <ExpandableSection headerText="How do I connect my AI assistant over MCP?">
            Add the CodeKB MCP URL (<code>https://&lt;your-domain&gt;/mcp/mcp</code>) to your MCP client
            config (Kiro, Amazon Q Developer CLI, Claude Code). On an auth-enforced instance your client
            completes an OAuth sign-in on first connect. Once connected, the assistant can load a space,
            search components, trace flows, inspect dependencies, and pull the call graph.
          </ExpandableSection>
          <ExpandableSection headerText="What is a Hyperspace Overview, and how is it different from a Space Overview?">
            A Space Overview documents one code base (business, architecture, tech stack, deployment).
            A Hyperspace Overview sits on top of a group of spaces and produces two system-level
            documents, a System Overview and a Space Interconnections map, synthesized from the member
            spaces' overviews. Open a hyperspace and use the Hyperspace Overview tab to generate it.
            CodeKB brings each member's Space Overview up to date first, so the synthesis can take a
            while and shows its own token cost.
          </ExpandableSection>
          <ExpandableSection headerText="Can Agent Hopper help me assess the impact of a change?">
            Yes. Switch Agent Hopper to Impact Analysis mode, describe the change in plain English (for
            example, "add input validation to all public API methods"), and the agent uses flow and
            dependency tracing to reason about what the change would touch.
          </ExpandableSection>
          <ExpandableSection headerText="Is there a way to see everything at once?">
            Yes. The Overview page aggregates metrics across all spaces and renders a Treemap
            visualization of your spaces and hyperspaces, with a space-health breakdown of ready,
            analyzing, and failed spaces.
          </ExpandableSection>
          <ExpandableSection headerText="How do I check my deployment is set up correctly?">
            Open Settings. It shows whether the S3 bucket, the S3 Vectors bucket, the vector index, and
            the git-credentials KMS key are reachable, the active Bedrock model and region, which Git
            providers and zip upload are enabled, and the version you are running.
          </ExpandableSection>
          <ExpandableSection headerText="Do I need to recreate a space when the code changes?">
            No. For Git-sourced spaces, use "Refresh space". CodeKB diffs the latest commit against the
            recorded commit, re-analyzes only what changed, and atomically swaps in the updated knowledge
            base. Recreate only to change the source or analysis options.
          </ExpandableSection>
          <ExpandableSection headerText="Is there a limit on repository size, and can I exclude files?">
            There is no hard size cap; repositories beyond 2M lines have been analyzed. CodeKB skips
            common noise like <code>node_modules</code> and <code>__pycache__</code> automatically, and
            you can add your own <code>.gitignore</code>-style exclusion list via configuration.
          </ExpandableSection>
          <ExpandableSection headerText="How do exclusion patterns work?">
            CodeKB skips files and folders matching its exclusion patterns: the built-in defaults plus
            any set in CODEKB_EXCLUDE_PATTERNS_EXTRA, listed on the Settings page. A space's patterns
            are fixed for its lifetime: they are recorded when the space is created, and every refresh
            applies the same patterns. Changing the system-wide patterns affects new spaces only. To
            apply the current patterns to an existing repository, delete the space and create it again.
          </ExpandableSection>
          <ExpandableSection headerText="Can I search across multiple repositories at once?">
            Yes. Use Global search across all or selected spaces, and group related spaces into a
            hyperspace to query them as one unit.
          </ExpandableSection>
        </SpaceBetween>
      </Container>

      {/* Troubleshooting */}
      <Container header={<Header variant="h2">Troubleshooting</Header>}>
        <SpaceBetween size="xs">
          <ExpandableSection headerText="Analysis finished but some files are missing.">
            Only supported languages are analyzed; the rest are skipped and listed on the Codebase tab.
            Files matching the space's exclusion patterns, shown on the same tab, are not analyzed either.
          </ExpandableSection>
          <ExpandableSection headerText="My Git branch was not found.">
            Branches and tags load from the repo once the URL is valid; pick from the list, or leave it
            blank for the default branch. Private repos need a saved credential first.
          </ExpandableSection>
        </SpaceBetween>
      </Container>

      {/* 12. Supported languages */}
      <Container header={
        <Header variant="h2" description="Tree-sitter powered parsing for accurate AST extraction">
          Supported languages
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