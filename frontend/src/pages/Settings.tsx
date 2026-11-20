import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import Box from "@cloudscape-design/components/box";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Spinner from "@cloudscape-design/components/spinner";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Table from "@cloudscape-design/components/table";
import { api, AppConfig, StartupCheckStatus } from "../api/client";
import { WORKLOAD_LABELS } from "../components/TokenUsageColumns";


type ModelField = "model_id" | "thinking" | "effort";

/** One table row: the default for all workloads, or a workload. */
interface ModelRow {
  key: string;
  label: string;
  /** The env vars an operator sets to change this row. */
  envVars: string;
  isDefault: boolean;
  values: Record<ModelField, string | null>;
  overrides: ModelField[];
  startupCheck: StartupCheckStatus | null;
}

/** Text for screen readers only, for meaning the page shows visually. */
function ScreenReaderOnly({ children }: { children: string }) {
  return (
    <span
      style={{
        position: "absolute",
        width: 1,
        height: 1,
        overflow: "hidden",
        clip: "rect(0 0 0 0)",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}

/**
 * One setting cell. One rule: a value this row sets is shown at full contrast;
 * everything else is grey (inherited from the default row, or not sent).
 * Inherited values also carry a tooltip and screen-reader text, so the rule
 * doesn't rely on contrast alone.
 */
function SettingValue({ value, setHere }: { value: string | null; setHere: boolean }) {
  // "Not set" rather than "Model default": nothing is sent, and what the model
  // then does varies (no thinking on Opus 4.6, adaptive thinking on Claude 5.x).
  if (!value) {
    return <Box color="text-status-inactive">Not set</Box>;
  }
  if (setHere) {
    // Bold, not a lighter grey: the inactive grey is already Cloudscape's
    // lightest text color, and inherited model ids still need to be readable.
    return (
      <Box variant="code" fontWeight="bold">
        {value}
      </Box>
    );
  }
  return (
    <span title="Inherited from Default for all workloads">
      <Box variant="code" color="text-status-inactive">
        {value}
      </Box>
      <ScreenReaderOnly>, inherited from the default for all workloads</ScreenReaderOnly>
    </span>
  );
}

function settingColumn(id: ModelField, header: string) {
  return {
    id,
    header,
    cell: (r: ModelRow) => (
      <SettingValue value={r.values[id]} setHere={r.isDefault || r.overrides.includes(id)} />
    ),
  };
}

/**
 * The startup check, summarized once. Verified is the normal case, so only
 * the rows that weren't verified are marked in the table.
 */
function startupCheckSummary(statuses: StartupCheckStatus[]) {
  const notVerified = statuses.filter((s) => s === "not_verified").length;
  const verified = statuses.filter((s) => s === "verified").length;
  // Same shape in every state: what happened, at startup. Provider-neutral, so
  // the copy holds when a provider other than Bedrock is added.
  // "At startup" matters: this is a snapshot from when the server started.
  if (notVerified > 0) {
    return (
      <StatusIndicator type="warning">
        Couldn't verify {notVerified} workload{notVerified === 1 ? "" : "s"} at startup. See
        the service log.
      </StatusIndicator>
    );
  }
  if (verified === 0) {
    return (
      <StatusIndicator type="stopped">
        Not verified at startup: CODEKB_LLM_STARTUP_CHECK is off.
      </StatusIndicator>
    );
  }
  return <StatusIndicator type="success">Verified at startup</StatusIndicator>;
}

/** Per-row mark, shown only for a workload the startup check didn't verify. */
function RowStartupMark({ status }: { status: StartupCheckStatus }) {
  if (status === "not_verified") {
    return <StatusIndicator type="warning">Not verified at startup</StatusIndicator>;
  }
  return <Box color="text-status-inactive">Not checked at startup</Box>;
}

// Inlined at build time by bundle.sh (--release only). Undefined for local builds
// and `npm run dev`, in which case the About section is not rendered at all.
const RELEASE_VERSION = import.meta.env.VITE_CODEKB_VERSION as string | undefined;

export default function Settings() {
  const navigate = useNavigate();
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    api.getConfig()
      .then(setConfig)
      .catch(() => setConfig(null))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  if (loading) {
    return <Box textAlign="center" padding="xxl"><Spinner size="large" /></Box>;
  }

  const models = config?.models;
  const modelRows: ModelRow[] = models
    ? [
        {
          key: "__defaults__",
          label: "Default for all workloads",
          envVars: "CODEKB_BEDROCK_MODEL_ID, CODEKB_LLM_*",
          isDefault: true,
          values: models.defaults,
          overrides: [],
          startupCheck: null,
        },
        ...models.workloads.map((m) => ({
          key: m.workload,
          label: WORKLOAD_LABELS[m.workload] ?? m.workload,
          // Mirrors Workload.env_var() in backend/src/core/llm_settings.py.
          envVars: `CODEKB_${m.workload.toUpperCase()}_*`,
          isDefault: false,
          values: { model_id: m.model_id, thinking: m.thinking, effort: m.effort },
          overrides: m.overrides ?? [],
          startupCheck: m.startup_check,
        })),
      ]
    : [];

  // Mark individual rows only when the check ran for at least one workload;
  // when it's off entirely, the summary line already says so.
  const showRowMarks =
    !!models && models.workloads.some((m) => m.startup_check !== "not_checked");

  const infra = config?.infrastructure ?? null;
  const items = infra
    ? [
        { label: "S3 bucket", ok: infra.s3_bucket.exists },
        { label: "Vectors bucket", ok: infra.vectors_bucket.exists },
        { label: "Vector index", ok: infra.vector_index.exists },
        { label: "Git credentials KMS key", ok: infra.kms_key.exists },
      ]
    : [];

  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup
        items={[{ text: "CodeKB", href: "/" }, { text: "Settings", href: "/settings" }]}
        onFollow={(e) => { e.preventDefault(); navigate(e.detail.href); }}
      />

      <Header variant="h1" description="Manage CodeKB infrastructure and configuration">
        Settings
      </Header>

      {/* Infrastructure status (read-only — provisioned via CDK or setup_aws.sh) */}
      <Container
        header={
          <Header
            variant="h2"
            description="The S3 buckets, vector index and embeddings model CodeKB uses in your AWS account"
          >
            Infrastructure
          </Header>
        }
      >
        {!infra ? (
          <Alert type="error">Failed to check infrastructure status. Verify your AWS credentials and config.</Alert>
        ) : (
          <SpaceBetween size="s">
            {infra.error && <Alert type="warning">{infra.error}</Alert>}
            <ColumnLayout columns={3} variant="text-grid">
              {items.map((it) => (
                <div key={it.label}>
                  <Box variant="awsui-key-label">{it.label}</Box>
                  <StatusIndicator type={it.ok ? "success" : "warning"}>
                    {it.ok ? "Provisioned" : "Not found"}
                  </StatusIndicator>
                </div>
              ))}
              {/* Fixed, not configurable: the vector index's dimensions are tied to it. */}
              {models?.embedding_model_id && (
                <div>
                  <Box variant="awsui-key-label">Embeddings model</Box>
                  <Box variant="code">{models.embedding_model_id}</Box>
                </div>
              )}
            </ColumnLayout>
          </SpaceBetween>
        )}
      </Container>

      {/* Models (read-only — CODEKB_BEDROCK_MODEL_ID, CODEKB_LLM_* and CODEKB_<WORKLOAD>_*) */}
      <Container
        header={
          <Header
            variant="h2"
            description="The model and settings each workload uses."
            actions={
              models ? startupCheckSummary(models.workloads.map((m) => m.startup_check)) : null
            }
          >
            Models
          </Header>
        }
      >
        {!models ? (
          <Alert type="error">Failed to load the model configuration.</Alert>
        ) : (
          <SpaceBetween size="m">
            <Table
              variant="embedded"
              items={modelRows}
              trackBy="key"
              columnDefinitions={[
                {
                  id: "workload",
                  header: "Workload",
                  cell: (r) => (
                    <div>
                      <Box fontWeight={r.isDefault ? "bold" : "normal"}>{r.label}</Box>
                      <Box variant="small" color="text-body-secondary">
                        {r.envVars}
                      </Box>
                      {r.startupCheck && r.startupCheck !== "verified" && showRowMarks && (
                        <RowStartupMark status={r.startupCheck} />
                      )}
                    </div>
                  ),
                },
                settingColumn("model_id", "Model"),
                settingColumn("thinking", "Thinking"),
                settingColumn("effort", "Effort"),
              ]}
            />
            <Box variant="small" color="text-body-secondary">
              When thinking isn't set, Claude 5.x models think by default. Grey values
              come from Default for all workloads, or aren't sent. Change any value with
              the environment variables under its row; changes take effect when CodeKB
              restarts.
            </Box>
          </SpaceBetween>
        )}
      </Container>

      {/* Allowed Git hosts (read-only — configured via CODEKB_ALLOWED_GIT_HOSTS) */}
      <Container
        header={
          <Header
            variant="h2"
            description="Repositories can be analyzed by URL only from these hosts."
          >
            Allowed Git hosts
          </Header>
        }
      >
        {!config ? (
          <Alert type="error">Failed to load the Git host configuration.</Alert>
        ) : config.allowed_git_hosts.length === 0 ? (
          <Alert type="warning">
            No Git hosts are configured, so analyzing a repository by URL is disabled.
            Set CODEKB_ALLOWED_GIT_HOSTS to enable it. Uploading a zip archive still works.
          </Alert>
        ) : (
          <SpaceBetween size="xs">
            <Box variant="awsui-key-label">Permitted hosts</Box>
            <SpaceBetween size="xs" direction="horizontal">
              {config.allowed_git_hosts.map((h) => (
                <Badge key={h} color="blue">{h}</Badge>
              ))}
            </SpaceBetween>
          </SpaceBetween>
        )}
      </Container>

      {/* Exclusion patterns (read-only — default.yaml plus CODEKB_EXCLUDE_PATTERNS_EXTRA) */}
      <Container
        header={
          <Header
            variant="h2"
            description="New spaces are created with these exclusion patterns."
          >
            Exclusion patterns
          </Header>
        }
      >
        {!config ? (
          <Alert type="error">Failed to load the exclusion patterns.</Alert>
        ) : (
          <Box variant="pre">{config.exclude_patterns.join("\n")}</Box>
        )}
      </Container>

      {/* Only rendered for release builds, where the version was injected. */}
      {RELEASE_VERSION && (
        <Container header={<Header variant="h2">About</Header>}>
          <Box variant="awsui-key-label">Version</Box>
          <Box>{RELEASE_VERSION}</Box>
        </Container>
      )}

    </SpaceBetween>
  );
}
