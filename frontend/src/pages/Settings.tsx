import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
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
  const { t } = useTranslation();
  // "Not set" rather than "Model default": nothing is sent, and what the model
  // then does varies (no thinking on Opus 4.6, adaptive thinking on Claude 5.x).
  if (!value) {
    return <Box color="text-status-inactive">{t("settings.notSet")}</Box>;
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
    <span title={t("settings.inheritedTitle")}>
      <Box variant="code" color="text-status-inactive">
        {value}
      </Box>
      <ScreenReaderOnly>{t("settings.inheritedSrOnly")}</ScreenReaderOnly>
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
function startupCheckSummary(statuses: StartupCheckStatus[], t: TFunction) {
  const notVerified = statuses.filter((s) => s === "not_verified").length;
  const verified = statuses.filter((s) => s === "verified").length;
  // Same shape in every state: what happened, at startup. Provider-neutral, so
  // the copy holds when a provider other than Bedrock is added.
  // "At startup" matters: this is a snapshot from when the server started.
  if (notVerified > 0) {
    return (
      <StatusIndicator type="warning">
        {t("settings.startupNotVerifiedCount", { count: notVerified })}
      </StatusIndicator>
    );
  }
  if (verified === 0) {
    return (
      <StatusIndicator type="stopped">
        {t("settings.startupCheckOff")}
      </StatusIndicator>
    );
  }
  return <StatusIndicator type="success">{t("settings.startupVerified")}</StatusIndicator>;
}

/** Per-row mark, shown only for a workload the startup check didn't verify. */
function RowStartupMark({ status }: { status: StartupCheckStatus }) {
  const { t } = useTranslation();
  if (status === "not_verified") {
    return <StatusIndicator type="warning">{t("settings.rowNotVerified")}</StatusIndicator>;
  }
  return <Box color="text-status-inactive">{t("settings.rowNotChecked")}</Box>;
}

// Inlined at build time by bundle.sh (--release only). Undefined for local builds
// and `npm run dev`, in which case the About section is not rendered at all.
const RELEASE_VERSION = import.meta.env.VITE_CODEKB_VERSION as string | undefined;

export default function Settings() {
  const { t } = useTranslation();
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
          label: t("settings.defaultForAll"),
          envVars: "CODEKB_BEDROCK_MODEL_ID, CODEKB_LLM_*",
          isDefault: true,
          values: models.defaults,
          overrides: [],
          startupCheck: null,
        },
        ...models.workloads.map((m) => ({
          key: m.workload,
          label: WORKLOAD_LABELS[m.workload] ? t(WORKLOAD_LABELS[m.workload]) : m.workload,
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
        { label: t("settings.s3Bucket"), ok: infra.s3_bucket.exists },
        { label: t("settings.vectorsBucket"), ok: infra.vectors_bucket.exists },
        { label: t("settings.vectorIndex"), ok: infra.vector_index.exists },
        { label: t("settings.kmsKey"), ok: infra.kms_key.exists },
      ]
    : [];

  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup
        items={[{ text: t("common.breadcrumbRoot"), href: "/" }, { text: t("nav.settings"), href: "/settings" }]}
        onFollow={(e) => { e.preventDefault(); navigate(e.detail.href); }}
      />

      <Header variant="h1" description={t("settings.description")}>
        {t("nav.settings")}
      </Header>

      {/* Infrastructure status (read-only — provisioned via CDK or setup_aws.sh) */}
      <Container
        header={
          <Header
            variant="h2"
            description={t("settings.infrastructureDescription")}
          >
            {t("settings.infrastructure")}
          </Header>
        }
      >
        {!infra ? (
          <Alert type="error">{t("settings.infrastructureFailed")}</Alert>
        ) : (
          <SpaceBetween size="s">
            {infra.error && <Alert type="warning">{infra.error}</Alert>}
            <ColumnLayout columns={3} variant="text-grid">
              {items.map((it) => (
                <div key={it.label}>
                  <Box variant="awsui-key-label">{it.label}</Box>
                  <StatusIndicator type={it.ok ? "success" : "warning"}>
                    {it.ok ? t("settings.provisioned") : t("settings.notFound")}
                  </StatusIndicator>
                </div>
              ))}
              {/* Fixed, not configurable: the vector index's dimensions are tied to it. */}
              {models?.embedding_model_id && (
                <div>
                  <Box variant="awsui-key-label">{t("settings.embeddingsModel")}</Box>
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
            description={t("settings.modelsDescription")}
            actions={
              models ? startupCheckSummary(models.workloads.map((m) => m.startup_check), t) : null
            }
          >
            {t("settings.models")}
          </Header>
        }
      >
        {!models ? (
          <Alert type="error">{t("settings.modelsFailed")}</Alert>
        ) : (
          <SpaceBetween size="m">
            <Table
              variant="embedded"
              items={modelRows}
              trackBy="key"
              columnDefinitions={[
                {
                  id: "workload",
                  header: t("settings.colWorkload"),
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
                settingColumn("model_id", t("settings.colModel")),
                settingColumn("thinking", t("settings.colThinking")),
                settingColumn("effort", t("settings.colEffort")),
              ]}
            />
            <Box variant="small" color="text-body-secondary">
              {t("settings.modelsNote")}
            </Box>
          </SpaceBetween>
        )}
      </Container>

      {/* Allowed Git hosts (read-only — configured via CODEKB_ALLOWED_GIT_HOSTS) */}
      <Container
        header={
          <Header
            variant="h2"
            description={t("settings.gitHostsDescription")}
          >
            {t("settings.gitHosts")}
          </Header>
        }
      >
        {!config ? (
          <Alert type="error">{t("settings.gitHostsFailed")}</Alert>
        ) : config.allowed_git_hosts.length === 0 ? (
          <Alert type="warning">
            {t("settings.gitHostsEmpty")}
          </Alert>
        ) : (
          <SpaceBetween size="xs">
            <Box variant="awsui-key-label">{t("settings.permittedHosts")}</Box>
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
            description={t("settings.exclusionDescription")}
          >
            {t("settings.exclusion")}
          </Header>
        }
      >
        {!config ? (
          <Alert type="error">{t("settings.exclusionFailed")}</Alert>
        ) : (
          <Box variant="pre">{config.exclude_patterns.join("\n")}</Box>
        )}
      </Container>

      {/* Only rendered for release builds, where the version was injected. */}
      {RELEASE_VERSION && (
        <Container header={<Header variant="h2">{t("settings.about")}</Header>}>
          <Box variant="awsui-key-label">{t("settings.version")}</Box>
          <Box>{RELEASE_VERSION}</Box>
        </Container>
      )}

    </SpaceBetween>
  );
}
