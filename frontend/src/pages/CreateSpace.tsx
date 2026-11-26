import { useState, useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Autosuggest from "@cloudscape-design/components/autosuggest";
import Select from "@cloudscape-design/components/select";
import Toggle from "@cloudscape-design/components/toggle";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Flashbar, { FlashbarProps } from "@cloudscape-design/components/flashbar";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Tiles from "@cloudscape-design/components/tiles";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Popover from "@cloudscape-design/components/popover";
import Icon from "@cloudscape-design/components/icon";
import Badge from "@cloudscape-design/components/badge";
import LanguagePills, { SUPPORTED_LANGUAGES } from "../components/LanguagePills";
import { api, SpaceSummary } from "../api/client";
import {
  SUPPORTED_LANGUAGES as UI_SUPPORTED_LANGUAGES,
  DEFAULT_LANGUAGE,
  type LanguageCode,
} from "../i18n/languages";

const LANGUAGE_OPTIONS = [
  { label: "Auto-detect (all supported languages)", value: "auto" },
  { label: "Python only", value: "python" },
  { label: "JavaScript only", value: "javascript" },
  { label: "Java only", value: "java" },
  { label: "TypeScript only", value: "typescript" },
  { label: "Kotlin only", value: "kotlin" },
  { label: "Go only", value: "go" },
  { label: "C# only", value: "csharp" },
  { label: "Rust only", value: "rust" },
  { label: "PHP only", value: "php" },
  { label: "Ruby only", value: "ruby" },
  { label: "Terraform only", value: "terraform" },
];

// Languages CodeKB can currently analyze are shown as colored pills so the
// names stand out, helping users avoid pointing CodeKB at an unsupported
// codebase. The list itself lives in LanguagePills alongside the colors.

// Renders the supported languages as colored pills, consistent with the
// Overview screen.
function SupportedLanguageBadges() {
  return <LanguagePills languages={SUPPORTED_LANGUAGES} />;
}

// Small info icon that reveals help text on hover/click. Use next to a field
// label to give guidance without cluttering the form.
function InfoTooltip({ content }: { content: React.ReactNode }) {
  const { t } = useTranslation();
  return (
    <Popover
      dismissButton={false}
      position="top"
      size="medium"
      triggerType="custom"
      content={content}
    >
      <span style={{ cursor: "help", color: "#5f6b7a" }} aria-label={t("common.moreInformation")}>
        <Icon name="status-info" size="small" />
      </span>
    </Popover>
  );
}

// Render a field label with an inline info tooltip.
function LabelWithInfo({
  label,
  optional,
  info,
}: {
  label: string;
  optional?: boolean;
  info: React.ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <span>
      {label} {optional && <i>{t("common.optional")}</i>} <InfoTooltip content={info} />
    </span>
  );
}

type SourceType = "git" | "upload";

// Parse the host from a Git URL for a client-side allowlist hint. Returns null
// for values we can't parse (e.g. scp-style git@host:path) — those are left to
// the server, which is the authoritative check.
function extractGitHost(url: string): string | null {
  try {
    return new URL(url.trim()).hostname.toLowerCase().replace(/^\[|\]$/g, "");
  } catch {
    return null;
  }
}

// Mirror of the backend match: exact host or dotted-subdomain, case-insensitive.
function hostInAllowlist(host: string, hosts: string[]): boolean {
  return hosts.some((a) => {
    const allowed = a.toLowerCase();
    return host === allowed || host.endsWith("." + allowed);
  });
}

// Turn the server's raw allowlist/SSRF rejection into a user-facing message.
function friendlyGitError(msg: string, hosts: string[] | null, t: TFunction): string {
  const allowed = hosts && hosts.length ? ` ${t("createSpace.allowedHosts", { hosts: hosts.join(", ") })}` : "";
  if (/No Git hosts are permitted/i.test(msg)) {
    return t("createSpace.errUrlDisabled");
  }
  if (/allowed Git hosts list/i.test(msg)) {
    return `${t("createSpace.errHostNotPermitted")}${allowed}`;
  }
  if (/cloud metadata service|loopback, link-local/i.test(msg)) {
    return t("createSpace.errInternalAddress");
  }
  return msg;
}

// True if an http(s) URL embeds credentials (user:pass@ or token@host). The
// normal SSH form git@host:path is not flagged — that "git" is a user name, not
// a secret. Credentials belong in a saved connection, never in the URL.
function gitUrlHasEmbeddedCredentials(url: string): boolean {
  try {
    const u = new URL(url.trim());
    if (u.protocol === "http:" || u.protocol === "https:") {
      return u.username !== "" || u.password !== "";
    }
  } catch {
    // Not a parseable absolute URL (scp-style etc.) — leave it to the server.
  }
  return false;
}

// Mirrors the backend's `_slugify_to_space_id` (without the random suffix).
function slugifySpaceName(raw: string): string {
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 54);
}

// Mirrors Python's `Path(...).stem`, which the backend uses to drop the
// extension from the repo segment: the final ".suffix" is removed whatever it
// is (".git", ".io", ".js"), while dotfiles (".gitignore") and names ending in
// a dot are left untouched.
function stripLastExtension(segment: string): string {
  const dot = segment.lastIndexOf(".");
  return dot > 0 && dot < segment.length - 1 ? segment.slice(0, dot) : segment;
}

// Derive a space name from a Git clone URL's repository segment, mirroring the
// backend's `generate_space_id`. Returns "" when there's no repo segment yet.
export function deriveSpaceNameFromGitUrl(url: string): string {
  let path = url.trim().replace(/\/+$/, "");
  if (!path) return "";
  try {
    path = new URL(path).pathname;
  } catch {
    // scp-style (git@host:org/repo.git): keep the part after the last colon.
    const colon = path.lastIndexOf(":");
    if (colon !== -1) path = path.slice(colon + 1);
  }
  const lastSegment = path.split("/").filter(Boolean).pop() ?? "";
  return slugifySpaceName(stripLastExtension(lastSegment));
}

// Reduce a Git URL to a canonical `host/path` identity so equivalent forms —
// SSH vs HTTPS, embedded credentials, a trailing ".git" or slash, host casing —
// compare equal. Used only for the advisory "already indexed?" hint, so it is
// intentionally broad: branch is not part of the identity. Returns "" for
// values we can't resolve to a host + path (the hint simply won't fire).
export function canonicalizeGitUrl(url: string): string {
  const raw = (url ?? "").trim();
  if (!raw) return "";
  let host = "";
  let path = "";
  try {
    if (!raw.includes("://")) {
      // scp-style (git@host:org/repo.git): host before the colon, path after.
      const m = raw.match(/^(?:[^@/]+@)?([^/:]+):(.+)$/);
      if (!m) return "";
      host = m[1];
      path = m[2];
    } else {
      const u = new URL(raw);
      host = u.hostname;
      path = u.pathname;
    }
  } catch {
    return "";
  }
  path = path.replace(/^\/+|\/+$/g, "");
  if (path.toLowerCase().endsWith(".git")) path = path.slice(0, -4);
  path = path.replace(/^\/+|\/+$/g, "");
  if (!host || !path) return "";
  return `${host}/${path}`.toLowerCase();
}

// Spaces whose repository matches `gitUrl` (canonical, URL-only match). Deleted
// (NOT_FOUND) and upload-sourced spaces are excluded. Sorted so a reusable
// (READY) space comes first, then newest, so the best candidate leads.
function findDuplicateSpaces(spaces: SpaceSummary[], gitUrl: string): SpaceSummary[] {
  const target = canonicalizeGitUrl(gitUrl);
  if (!target) return [];
  return spaces
    .filter(
      (s) =>
        s.source_type !== "upload" &&
        s.status !== "NOT_FOUND" &&
        !!s.git_url &&
        canonicalizeGitUrl(s.git_url) === target
    )
    .sort((a, b) => {
      const ra = a.status === "READY" ? 0 : 1;
      const rb = b.status === "READY" ? 0 : 1;
      if (ra !== rb) return ra - rb;
      return (b.created_at ?? "").localeCompare(a.created_at ?? ""); // newest first
    });
}

// Short date for the duplicate list (mirrors SpaceList's formatter).
function formatSpaceDate(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

// Status as a Cloudscape StatusIndicator, consistent with the Spaces list.
function DuplicateStatus({ status }: { status?: string }) {
  const { t } = useTranslation();
  if (status === "READY") return <StatusIndicator type="success">{t("common.statusReady")}</StatusIndicator>;
  if (status === "FAILED") return <StatusIndicator type="error">{t("common.statusFailed")}</StatusIndicator>;
  if (status === "REFRESH_FAILED") return <StatusIndicator type="error">{t("createSpace.statusRefreshFailed")}</StatusIndicator>;
  if (["ANALYZING", "SUBMITTED", "SUMMARIZING", "REFRESHING"].includes(status ?? "")) {
    const label =
      status === "SUMMARIZING"
        ? t("common.statusSummarizing")
        : status === "REFRESHING"
          ? t("common.statusRefreshing")
          : t("common.statusAnalyzing");
    return <StatusIndicator type="in-progress">{label}</StatusIndicator>;
  }
  return <StatusIndicator type="loading">{status ?? t("createSpace.statusUnknown")}</StatusIndicator>;
}

export default function CreateSpace() {
  const navigate = useNavigate();
  const { t } = useTranslation();

  // Source type
  const [sourceType, setSourceType] = useState<SourceType>("git");

  // Git fields
  const [gitUrl, setGitUrl] = useState("");
  const [branch, setBranch] = useState("");
  const [gitSpaceName, setGitSpaceName] = useState("");
  // True once the user overrides the auto-derived name; clearing the field resets it.
  const [gitSpaceNameEdited, setGitSpaceNameEdited] = useState(false);

  // Branch dropdown (populated from the remote once a git URL is provided)
  const [branchOptions, setBranchOptions] = useState<{ value: string }[]>([]);
  const [branchesLoading, setBranchesLoading] = useState(false);
  const [branchesError, setBranchesError] = useState<string | null>(null);
  const [loadedBranchesFor, setLoadedBranchesFor] = useState<string | null>(null);

  // Upload fields
  const [spaceName, setSpaceName] = useState("");
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);

  // Shared fields
  const [language, setLanguage] = useState(LANGUAGE_OPTIONS[0]);
  const [summarize] = useState(true);
  // Output language for the generated overview/analysis. Always starts at the
  // default language, independent of the current UI language.
  const outputLanguageOptions = UI_SUPPORTED_LANGUAGES.map((l) => ({
    label: l.displayName,
    value: l.languageCode as LanguageCode,
  }));
  const [outputLanguage, setOutputLanguage] = useState(
    () => outputLanguageOptions.find((o) => o.value === DEFAULT_LANGUAGE) ?? outputLanguageOptions[0]
  );
  const [omitLowBusinessRelevance, setOmitLowBusinessRelevance] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  // Pre-submit duplicate re-check (drives the submit button spinner so the
  // fresh /spaces fetch doesn't look like a dead pause).
  const [checkingDuplicates, setCheckingDuplicates] = useState(false);
  const [flash, setFlash] = useState<FlashbarProps.MessageDefinition[]>([]);
  const [infraReady, setInfraReady] = useState<boolean | null>(null);
  const [allowedHosts, setAllowedHosts] = useState<string[] | null>(null);
  const [zipUploadEnabled, setZipUploadEnabled] = useState(true);
  // Whether the server accepts credentials embedded in a git URL.
  const [allowCredsInUrl, setAllowCredsInUrl] = useState(false);
  // Whether any git provider is configured — the connections feature is off otherwise.
  const [connectionsEnabled, setConnectionsEnabled] = useState(false);
  // null = still loading; false = no git-provider connection saved yet.
  const [hasConnections, setHasConnections] = useState<boolean | null>(null);

  const queryClient = useQueryClient();
  // Existing spaces, reused from the shared ["spaces"] cache, for a live hint as
  // the user types. Always refetch on mount so returning to this form right
  // after creating a space sees the newest list rather than a stale cache. The
  // authoritative duplicate check happens at submit with a fresh fetch (below).
  const { data: existingSpaces = [] } = useQuery({
    queryKey: ["spaces"],
    queryFn: () => api.listSpaces(),
    refetchOnMount: "always",
    staleTime: 0,
  });
  // Advisory duplicate dialog: holds the matching spaces while it's open.
  const [duplicateMatches, setDuplicateMatches] = useState<SpaceSummary[]>([]);
  const [duplicateModalVisible, setDuplicateModalVisible] = useState(false);

  useEffect(() => {
    api.getConfig()
      .then((c) => {
        setInfraReady(c.infrastructure.ready);
        setAllowedHosts(c.allowed_git_hosts);
        setZipUploadEnabled(c.zip_upload_enabled);
        setAllowCredsInUrl(c.allow_credentials_in_git_url ?? false);
        setConnectionsEnabled((c.git_credential_providers ?? []).length > 0);
        if (!c.zip_upload_enabled) {
          setSourceType((prev) => (prev === "upload" ? "git" : prev));
        }
      })
      .catch(() => {
        setInfraReady(false);
        setAllowedHosts(null);
      });
    api.listGitCredentials()
      .then((list) => setHasConnections(list.length > 0))
      .catch(() => setHasConnections(null));
  }, []);

  const handleGitUrlChange = (value: string) => {
    setGitUrl(value);
    // The error described the old URL; the lookup only re-runs on blur.
    setBranchesError(null);
    if (!gitSpaceNameEdited) {
      setGitSpaceName(deriveSpaceNameFromGitUrl(value));
    }
  };

  const handleGitSpaceNameChange = (value: string) => {
    setGitSpaceName(value);
    setGitSpaceNameEdited(value.trim().length > 0);
  };

  // Live hint from the (mount-refreshed) cache as the user types. Advisory only;
  // the authoritative check runs against a fresh fetch at submit.
  const gitDuplicates = useMemo(
    () => (sourceType === "git" ? findDuplicateSpaces(existingSpaces, gitUrl) : []),
    [sourceType, gitUrl, existingSpaces]
  );

  // The actual create call, shared by the normal submit and the modal's
  // "Create anyway" action.
  const createGitSpace = async () => {
    setSubmitting(true);
    try {
      const result = await api.createSpace({
        git_url: gitUrl,
        branch: branch.trim() || undefined,
        space_name: gitSpaceName.trim() || undefined,
        summarize,
        omit_low_business_relevance: omitLowBusinessRelevance,
        overview_language: outputLanguage.value,
        analysis_language: outputLanguage.value,
      });
      // Keep the shared list current so a back-to-back second create (and other
      // pages) immediately see this new space.
      queryClient.invalidateQueries({ queryKey: ["spaces"] });
      const newSpaceId = typeof result?.space_id === "string" ? result.space_id : null;
      navigate(newSpaceId ? `/spaces/${newSpaceId}` : "/spaces");
    } catch (e: any) {
      setFlash([
        {
          type: "error",
          content: `${t("createSpace.failedToStart")} ${friendlyGitError(e.message, allowedHosts, t)}`,
          dismissible: true,
          onDismiss: () => setFlash([]),
        },
      ]);
    } finally {
      setSubmitting(false);
    }
  };

  const handleGitSubmit = async () => {
    // Re-check against the latest server state at click time (another user may
    // have indexed this repo since the form loaded). The button shows a spinner
    // meanwhile, so the fetch isn't a dead pause. On any failure we fall back to
    // the cached hint rather than blocking creation.
    setCheckingDuplicates(true);
    let matches = gitDuplicates;
    try {
      const latest = await api.listSpaces();
      queryClient.setQueryData(["spaces"], latest);
      matches = findDuplicateSpaces(latest, gitUrl);
    } catch {
      // keep the cached-memo fallback
    } finally {
      setCheckingDuplicates(false);
    }
    if (matches.length > 0) {
      setDuplicateMatches(matches);
      setDuplicateModalVisible(true);
      return;
    }
    await createGitSpace();
  };

  const fetchBranches = async () => {
    const url = gitUrl.trim();
    if (!url || url === loadedBranchesFor) return;
    // Don't send a URL that embeds credentials unless the deployment allows it.
    if (gitUrlHasEmbeddedCredentials(url) && !allowCredsInUrl) return;
    // Skip a doomed lookup when URL analysis is off or the host isn't allowed —
    // the field-level hint/warning already explains why.
    if (allowedHosts !== null && allowedHosts.length === 0) return;
    const host = extractGitHost(url);
    if (allowedHosts && allowedHosts.length > 0 && host && !hostInAllowlist(host, allowedHosts)) return;
    setBranchesLoading(true);
    setBranchesError(null);
    try {
      const { branches, tags } = await api.getGitBranches(url);
      const options = [...branches, ...tags].map((b) => ({ value: b }));
      setBranchOptions(options);
      setLoadedBranchesFor(url);
    } catch (e: any) {
      setBranchOptions([]);
      setLoadedBranchesFor(null);
      setBranchesError(
        `${t("createSpace.branchLoadFailed")} ${friendlyGitError(e.message, allowedHosts, t)} ${t("createSpace.branchTypeManually")}`
      );
    } finally {
      setBranchesLoading(false);
    }
  };

  const handleUploadSubmit = async () => {
    if (!zipFile || !spaceName.trim()) return;
    setSubmitting(true);
    setUploadProgress(0);
    try {
      // 1. Get presigned URL
      const { space_id, upload_url } = await api.getUploadUrl({
        space_name: spaceName,
        summarize,
        omit_low_business_relevance: omitLowBusinessRelevance,
      });

      // 2. Upload zip to S3
      setUploadProgress(30);
      await api.uploadZipToS3(upload_url, zipFile);
      setUploadProgress(70);

      // 3. Trigger analysis
      await api.startUploadAnalysis({
        space_id,
        space_name: spaceName,
        summarize,
        omit_low_business_relevance: omitLowBusinessRelevance,
        overview_language: outputLanguage.value,
        analysis_language: outputLanguage.value,
      });
      setUploadProgress(100);
      navigate(`/spaces/${space_id}`);
    } catch (e: any) {
      setFlash([
        {
          type: "error",
          content: `${t("createSpace.failedToUpload")} ${e.message}`,
          dismissible: true,
          onDismiss: () => setFlash([]),
        },
      ]);
    } finally {
      setSubmitting(false);
      setUploadProgress(null);
    }
  };

  const handleSubmit = sourceType === "git" ? handleGitSubmit : handleUploadSubmit;

  // Warn (without blocking) when the typed branch doesn't match any loaded
  // branch/tag — the most common cause of a failed clone. Only meaningful once
  // branches have actually loaded for the current URL.
  const trimmedBranch = branch.trim();
  const branchMatchesLoaded =
    trimmedBranch === "" ||
    branchOptions.some((o) => o.value === trimmedBranch);
  const showBranchMismatchWarning =
    !!loadedBranchesFor &&
    loadedBranchesFor === gitUrl.trim() &&
    !branchMatchesLoaded;

  // Git host allowlist (fail-closed): empty list means URL analysis is off.
  const gitAnalysisDisabled = allowedHosts !== null && allowedHosts.length === 0;
  const typedHost = extractGitHost(gitUrl);
  const hostNotAllowed =
    sourceType === "git" &&
    !!typedHost &&
    allowedHosts !== null &&
    allowedHosts.length > 0 &&
    !hostInAllowlist(typedHost, allowedHosts);

  const gitUrlHasCredentials =
    sourceType === "git" && gitUrlHasEmbeddedCredentials(gitUrl) && !allowCredsInUrl;
  // Each condition is one the server would reject. Branch loading is NOT a
  // validity condition: blocking on it lets a blur-triggered fetchBranches()
  // (fired when the button steals focus from the URL field) disable the button
  // mid-click, which swallows the user's first click. The server is
  // authoritative on whether the branch exists, so an in-flight lookup must not
  // gate submission.
  const isGitValid =
    gitUrl.trim().length > 0 &&
    !gitAnalysisDisabled &&
    !gitUrlHasCredentials &&
    !hostNotAllowed &&
    !branchesError &&
    !showBranchMismatchWarning;
  const isUploadValid = spaceName.trim().length > 0 && zipFile !== null;
  const isValid = sourceType === "git" ? isGitValid : isUploadValid;

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      if (!file.name.endsWith(".zip")) {
        setFlash([
          {
            type: "error",
            content: t("createSpace.selectZip"),
            dismissible: true,
            onDismiss: () => setFlash([]),
          },
        ]);
        return;
      }
      if (file.size > 500 * 1024 * 1024) {
        setFlash([
          {
            type: "error",
            content: t("createSpace.fileTooLarge"),
            dismissible: true,
            onDismiss: () => setFlash([]),
          },
        ]);
        return;
      }
      setZipFile(file);
    }
  };

  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup
        items={[
          { text: t("common.breadcrumbRoot"), href: "/" },
          { text: t("nav.spaces"), href: "/spaces" },
          { text: t("createSpace.newSpace"), href: "/create" },
        ]}
        onFollow={(e) => {
          e.preventDefault();
          navigate(e.detail.href);
        }}
      />
      <Flashbar items={flash} />

      <Modal
        visible={duplicateModalVisible}
        onDismiss={() => setDuplicateModalVisible(false)}
        header={t("createSpace.dupHeader")}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              {/* Create-anyway is the understated outlined button; Cancel is
                  the emphasized primary, making the safe action the default. */}
              <Button
                variant="normal"
                loading={submitting}
                onClick={() => {
                  setDuplicateModalVisible(false);
                  createGitSpace();
                }}
              >
                {t("createSpace.dupCreateAnyway")}
              </Button>
              <Button variant="primary" onClick={() => setDuplicateModalVisible(false)}>
                {t("common.cancel")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box variant="p">
            {duplicateMatches.length === 1
              ? t("createSpace.dupOne")
              : t("createSpace.dupMany", { count: duplicateMatches.length })}{" "}
            {t("createSpace.dupHint")}
          </Box>
          <Box variant="small">{duplicateMatches[0]?.git_url}</Box>
          <SpaceBetween size="xs">
            {duplicateMatches.map((s) => {
              const meta = [s.created_by, formatSpaceDate(s.created_at)]
                .filter(Boolean)
                .join(" · ");
              return (
                <Box key={s.space_id}>
                  <SpaceBetween direction="horizontal" size="s">
                    <DuplicateStatus status={s.status} />
                    <Link
                      onFollow={(e) => {
                        e.preventDefault();
                        navigate(`/spaces/${s.space_id}`);
                      }}
                    >
                      {s.space_id}
                    </Link>
                  </SpaceBetween>
                  {meta && <Box variant="small">{meta}</Box>}
                </Box>
              );
            })}
          </SpaceBetween>
        </SpaceBetween>
      </Modal>

      {infraReady === false && (
        <Alert type="warning" header={t("createSpace.infraHeader")}>
          {t("createSpace.infraPrefix")}{" "}
          <Link onFollow={(e) => { e.preventDefault(); navigate("/settings"); }}>{t("nav.settings")}</Link>
          {" "}{t("createSpace.infraSuffix")}
        </Alert>
      )}

      <Form
        header={
          <Header
            variant="h1"
            description={t("createSpace.pageDescription")}
          >
            {t("createSpace.newSpace")}
          </Header>
        }
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={() => navigate("/spaces")}>
              {t("common.cancel")}
            </Button>
            <Button variant="primary" loading={submitting || checkingDuplicates} disabled={!isValid || infraReady === false} onClick={handleSubmit}>
              {sourceType === "upload" ? t("createSpace.uploadAndAnalyze") : t("createSpace.startAnalysis")}
            </Button>
          </SpaceBetween>
        }
      >
        <SpaceBetween size="l">
          {/* Source type selector */}
          <Container
            header={
              <Header variant="h2" description={t("createSpace.sourceDescription")}>
                {t("createSpace.sourceHeader")}
              </Header>
            }
          >
            <SpaceBetween size="l">
              <Tiles
                value={sourceType}
                onChange={({ detail }) => {
                  setSourceType(detail.value as SourceType);
                }}
                items={[
                  {
                    value: "git",
                    label: t("createSpace.tileGitLabel"),
                    description: t("createSpace.tileGitDescription"),
                  },
                  {
                    value: "upload",
                    label: t("createSpace.tileUploadLabel"),
                    description: zipUploadEnabled
                      ? t("createSpace.tileUploadDescription")
                      : t("createSpace.tileUploadDescriptionDisabled"),
                    disabled: !zipUploadEnabled,
                  },
                ]}
              />

              {sourceType === "git" && (
                <SpaceBetween size="l">
                  {gitAnalysisDisabled && (
                    <Alert type="warning" header={t("createSpace.urlDisabledHeader")}>
                      {t("createSpace.urlDisabledBody")}
                    </Alert>
                  )}

                  {connectionsEnabled && hasConnections === false && (
                    <Alert type="warning" header={t("createSpace.publicOnlyHeader")}>
                      {t("createSpace.publicOnlyPrefix")}{" "}
                      <Link onFollow={(e) => { e.preventDefault(); navigate("/git-creds"); }}>
                        {t("createSpace.gitCredentialsLink")}
                      </Link>{" "}
                      {t("createSpace.publicOnlySuffix")}
                    </Alert>
                  )}

                  <Alert type="info">
                    <SpaceBetween size="xs">
                      <span>{t("createSpace.analyzeLanguages")}</span>
                      <SupportedLanguageBadges />
                      <span>{t("createSpace.otherLanguagesSkipped")}</span>
                    </SpaceBetween>
                  </Alert>

                  <FormField
                    label={
                      <LabelWithInfo
                        label={t("createSpace.spaceNameLabel")}
                        info={t("createSpace.gitSpaceNameInfo")}
                      />
                    }
                    description={t("createSpace.gitSpaceNameDescription")}
                    constraintText={
                      gitSpaceNameEdited
                        ? t("createSpace.gitSpaceNameEdited")
                        : t("createSpace.gitSpaceNameAuto")
                    }
                  >
                    <Input
                      value={gitSpaceName}
                      onChange={({ detail }) => handleGitSpaceNameChange(detail.value)}
                      placeholder={t("createSpace.gitSpaceNamePlaceholder")}
                    />
                  </FormField>

                  <FormField
                    label={
                      <LabelWithInfo
                        label={t("createSpace.gitUrlLabel")}
                        info={
                          <>
                            {t("createSpace.gitUrlInfoPrefix")}
                            <Box variant="code" display="inline"> .git</Box>
                            {t("createSpace.gitUrlInfoSuffix")}
                          </>
                        }
                      />
                    }
                    description={t("createSpace.gitUrlDescription")}
                    constraintText={
                      allowedHosts && allowedHosts.length > 0
                        ? t("createSpace.gitUrlConstraintHosts", { hosts: allowedHosts.join(", ") })
                        : t("createSpace.gitUrlConstraintDefault")
                    }
                    errorText={
                      gitUrlHasCredentials
                        ? t("createSpace.gitUrlErrCredentials")
                        : hostNotAllowed
                          ? t("createSpace.gitUrlErrHost", { host: typedHost, hosts: allowedHosts!.join(", ") })
                          : undefined
                    }
                  >
                    <Input
                      value={gitUrl}
                      onChange={({ detail }) => handleGitUrlChange(detail.value)}
                      onBlur={() => fetchBranches()}
                      placeholder="https://github.com/org/repo.git"
                      type="url"
                    />
                  </FormField>

                  <FormField
                    label={
                      <LabelWithInfo
                        label={t("createSpace.branchLabel")}
                        optional
                        info={t("createSpace.branchInfo")}
                      />
                    }
                    description={t("createSpace.branchDescription")}
                    constraintText={t("createSpace.branchConstraint")}
                    errorText={
                      branchesError ??
                      (showBranchMismatchWarning
                        ? t("createSpace.branchMismatch", { branch: trimmedBranch })
                        : undefined)
                    }
                  >
                    <Autosuggest
                      value={branch}
                      onChange={({ detail }) => setBranch(detail.value)}
                      onFocus={() => fetchBranches()}
                      options={branchOptions}
                      statusType={branchesLoading ? "loading" : "finished"}
                      loadingText={t("createSpace.branchesLoading")}
                      empty={
                        loadedBranchesFor
                          ? t("createSpace.branchesNone")
                          : t("createSpace.branchesEnterUrl")
                      }
                      enteredTextLabel={(v) => t("createSpace.branchUse", { value: v })}
                      placeholder={t("createSpace.branchPlaceholder")}
                      ariaLabel={t("createSpace.branchAriaLabel")}
                    />
                  </FormField>
                </SpaceBetween>
              )}

              {sourceType === "upload" && (
                <SpaceBetween size="l">
                  <Alert type="info">
                    <SpaceBetween size="xs">
                      <span>{t("createSpace.analyzeLanguages")}</span>
                      <SupportedLanguageBadges />
                      <span>{t("createSpace.otherLanguagesSkipped")}</span>
                    </SpaceBetween>
                  </Alert>

                  <FormField
                    label={
                      <LabelWithInfo
                        label={t("createSpace.spaceNameLabel")}
                        info={t("createSpace.uploadSpaceNameInfo")}
                      />
                    }
                    description={t("createSpace.uploadSpaceNameDescription")}
                    constraintText={t("createSpace.uploadSpaceNameConstraint")}
                  >
                    <Input
                      value={spaceName}
                      onChange={({ detail }) => setSpaceName(detail.value)}
                      placeholder="my-service"
                    />
                  </FormField>

                  <FormField
                    label={
                      <LabelWithInfo
                        label={t("createSpace.zipLabel")}
                        info={t("createSpace.zipInfo")}
                      />
                    }
                    description={t("createSpace.zipDescription")}
                    constraintText={t("createSpace.zipConstraint")}
                  >
                    <input
                      type="file"
                      accept=".zip"
                      onChange={handleFileChange}
                      style={{ padding: "8px 0" }}
                    />
                    {zipFile && (
                      <Box
                        variant="span"
                        color="text-body-secondary"
                        margin={{ left: "xs" }}
                      >
                        {zipFile.name} ({(zipFile.size / 1024 / 1024).toFixed(1)} MB)
                      </Box>
                    )}
                  </FormField>

                  {uploadProgress !== null && (
                    <ProgressBar
                      value={uploadProgress}
                      label={t("createSpace.uploadingLabel")}
                      description={
                        uploadProgress < 30
                          ? t("createSpace.progressPreparing")
                          : uploadProgress < 70
                          ? t("createSpace.progressUploading")
                          : uploadProgress < 100
                          ? t("createSpace.progressStarting")
                          : t("createSpace.progressDone")
                      }
                    />
                  )}
                </SpaceBetween>
              )}
            </SpaceBetween>
          </Container>

          <Container
            header={
              <Header variant="h2" description={t("createSpace.optionsDescription")}>
                <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                  <span>{t("createSpace.optionsHeader")}</span>
                  <Badge color="blue">{t("createSpace.experimental")}</Badge>
                </span>
              </Header>
            }
          >
            <SpaceBetween size="m">
              <FormField
                description={
                  <>
                    {t("createSpace.relevancePrefix")}{" "}
                    <Link onFollow={(e) => { e.preventDefault(); navigate("/docs#business-relevance"); }}>
                      {t("createSpace.documentationLink")}
                    </Link>{t("createSpace.relevanceSuffix")}
                  </>
                }
              >
                <Toggle
                  checked={omitLowBusinessRelevance}
                  onChange={({ detail }) => setOmitLowBusinessRelevance(detail.checked)}
                >
                  {t("createSpace.omitToggle")}
                </Toggle>
              </FormField>
              <FormField
                label={t("spaceForm.outputLanguageLabel")}
                description={t("spaceForm.outputLanguageDescription")}
              >
                <Select
                  selectedOption={outputLanguage}
                  onChange={({ detail }) =>
                    setOutputLanguage(
                      outputLanguageOptions.find((o) => o.value === detail.selectedOption.value) ??
                        outputLanguageOptions[0]
                    )
                  }
                  options={outputLanguageOptions}
                  ariaLabel={t("spaceForm.outputLanguageLabel")}
                />
              </FormField>
            </SpaceBetween>
          </Container>

        </SpaceBetween>
      </Form>
    </SpaceBetween>
  );
}