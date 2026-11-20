import { useState, useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
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
  return (
    <Popover
      dismissButton={false}
      position="top"
      size="medium"
      triggerType="custom"
      content={content}
    >
      <span style={{ cursor: "help", color: "#5f6b7a" }} aria-label="More information">
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
  return (
    <span>
      {label} {optional && <i>- optional</i>} <InfoTooltip content={info} />
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
function friendlyGitError(msg: string, hosts: string[] | null): string {
  const allowed = hosts && hosts.length ? ` Allowed hosts: ${hosts.join(", ")}.` : "";
  if (/No Git hosts are permitted/i.test(msg)) {
    return "Repository analysis by URL is disabled on this instance. Ask your administrator to configure the allowed Git hosts, or upload a zip instead.";
  }
  if (/allowed Git hosts list/i.test(msg)) {
    return `This Git host isn't permitted on this instance.${allowed}`;
  }
  if (/cloud metadata service|loopback, link-local/i.test(msg)) {
    return "That URL points at an internal address and can't be analyzed.";
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
  if (status === "READY") return <StatusIndicator type="success">Ready</StatusIndicator>;
  if (status === "FAILED") return <StatusIndicator type="error">Failed</StatusIndicator>;
  if (status === "REFRESH_FAILED") return <StatusIndicator type="error">Refresh failed</StatusIndicator>;
  if (["ANALYZING", "SUBMITTED", "SUMMARIZING", "REFRESHING"].includes(status ?? "")) {
    const label =
      status === "SUMMARIZING" ? "Summarizing" : status === "REFRESHING" ? "Refreshing" : "Analyzing";
    return <StatusIndicator type="in-progress">{label}</StatusIndicator>;
  }
  return <StatusIndicator type="loading">{status ?? "Unknown"}</StatusIndicator>;
}

export default function CreateSpace() {
  const navigate = useNavigate();

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
          content: `Failed to start analysis: ${friendlyGitError(e.message, allowedHosts)}`,
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
        `Couldn't load branches: ${friendlyGitError(e.message, allowedHosts)} You can still type a branch name.`
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
      });
      setUploadProgress(100);
      navigate(`/spaces/${space_id}`);
    } catch (e: any) {
      setFlash([
        {
          type: "error",
          content: `Failed to upload and analyze: ${e.message}`,
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
            content: "Please select a .zip file",
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
            content: "File size must be under 500 MB",
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
          { text: "CodeKB", href: "/" },
          { text: "Spaces", href: "/spaces" },
          { text: "New space", href: "/create" },
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
        header="This repository looks already indexed"
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
                Create a new space anyway
              </Button>
              <Button variant="primary" onClick={() => setDuplicateModalVisible(false)}>
                Cancel
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box variant="p">
            {duplicateMatches.length === 1
              ? "A space for this repository already exists."
              : `${duplicateMatches.length} spaces for this repository already exist.`}{" "}
            Open one to reuse it (refresh it for the latest code), or create a
            separate new space.
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
        <Alert type="warning" header="Infrastructure not ready">
          S3 buckets and vector index must be provisioned before creating a space.
          Go to the{" "}
          <Link onFollow={(e) => { e.preventDefault(); navigate("/settings"); }}>Settings</Link>
          {" "}page to set up infrastructure.
        </Alert>
      )}

      <Form
        header={
          <Header
            variant="h1"
            description="A CodeKB space is a self-contained knowledge base built from source code. Provide a Git repository URL or upload a zip of your source directory."
          >
            New space
          </Header>
        }
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={() => navigate("/spaces")}>
              Cancel
            </Button>
            <Button variant="primary" loading={submitting || checkingDuplicates} disabled={!isValid || infraReady === false} onClick={handleSubmit}>
              {sourceType === "upload" ? "Upload & analyze" : "Start analysis"}
            </Button>
          </SpaceBetween>
        }
      >
        <SpaceBetween size="l">
          {/* Source type selector */}
          <Container
            header={
              <Header variant="h2" description="Choose where CodeKB should read your source code from.">
                Source
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
                    label: "Git repository",
                    description: "Clone from a Git URL accessible by CodeKB",
                  },
                  {
                    value: "upload",
                    label: "Upload zip",
                    description: zipUploadEnabled
                      ? "Upload a zipped source directory from your machine"
                      : "Upload a zipped source directory from your machine (disabled on this instance)",
                    disabled: !zipUploadEnabled,
                  },
                ]}
              />

              {sourceType === "git" && (
                <SpaceBetween size="l">
                  {gitAnalysisDisabled && (
                    <Alert type="warning" header="Repository analysis by URL is disabled">
                      No Git hosts are configured on this instance, so CodeKB can't
                      clone from a URL. You can still upload a zip of your source
                      (choose "Upload zip" above). To enable URL analysis, an
                      administrator must set the allowed Git hosts.
                    </Alert>
                  )}

                  {connectionsEnabled && hasConnections === false && (
                    <Alert type="warning" header="Only public repositories can be indexed">
                      You haven't connected any git credentials, so CodeKB can only clone
                      public repositories. To index a private repository, add a token on
                      the{" "}
                      <Link onFollow={(e) => { e.preventDefault(); navigate("/git-creds"); }}>
                        Git credentials
                      </Link>{" "}
                      page, then come back here. If a private repo fails with an
                      expired-token error, reconnect your token there.
                    </Alert>
                  )}

                  <Alert type="info">
                    <SpaceBetween size="xs">
                      <span>CodeKB can analyze these languages:</span>
                      <SupportedLanguageBadges />
                      <span>
                        Files in other languages are skipped. If none of the
                        files are in a supported language, there's nothing to
                        analyze.
                      </span>
                    </SpaceBetween>
                  </Alert>

                  <FormField
                    label={
                      <LabelWithInfo
                        label="Space name"
                        info="Filled in from the repository name as soon as you enter a URL, so the same repository always gets the same name. Type over it to use your own, or clear the field to go back to the default."
                      />
                    }
                    description="Defaults to the repository name. Edit it if you need a different name."
                    constraintText={
                      gitSpaceNameEdited
                        ? "Using your name instead of the repository default. Clear the field to restore the default."
                        : "Set automatically from the repository URL."
                    }
                  >
                    <Input
                      value={gitSpaceName}
                      onChange={({ detail }) => handleGitSpaceNameChange(detail.value)}
                      placeholder="Enter a repository URL below"
                    />
                  </FormField>

                  <FormField
                    label={
                      <LabelWithInfo
                        label="Git repository URL"
                        info={
                          <>
                            The full clone URL of the repository, ending in
                            <Box variant="code" display="inline"> .git</Box>.
                            CodeKB must be able to reach it — public repos work
                            out of the box; private repos need credentials
                            configured on the service. Branches load
                            automatically once you enter a valid URL.
                          </>
                        }
                      />
                    }
                    description="HTTPS or SSH URL of the repository to analyze"
                    constraintText={
                      allowedHosts && allowedHosts.length > 0
                        ? `This instance can analyze repositories from: ${allowedHosts.join(", ")}. Must be reachable from the CodeKB service.`
                        : "Example: https://github.com/org/repo.git — must be reachable from the CodeKB service"
                    }
                    errorText={
                      gitUrlHasCredentials
                        ? "Remove the username or token from the URL. Enter the plain clone URL and save a git credential on the Git credentials page instead."
                        : hostNotAllowed
                          ? `"${typedHost}" isn't permitted on this instance. Allowed hosts: ${allowedHosts!.join(", ")}.`
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
                        label="Branch"
                        optional
                        info={
                          <>
                            Pick a branch or tag from the list — it's loaded
                            directly from the repository, so you don't have to
                            type it. Leave it blank to analyze the repository's
                            default branch. It must be a branch or tag name; a
                            commit SHA won't work.
                          </>
                        }
                      />
                    }
                    description="Branch or tag to analyze. Leave blank to use the repository's default branch."
                    constraintText="Tip: pick from the list to avoid typos."
                    errorText={
                      branchesError ??
                      (showBranchMismatchWarning
                        ? `"${trimmedBranch}" isn't a branch or tag in this repository — check the name, or pick one from the list. A commit SHA won't work.`
                        : undefined)
                    }
                  >
                    <Autosuggest
                      value={branch}
                      onChange={({ detail }) => setBranch(detail.value)}
                      onFocus={() => fetchBranches()}
                      options={branchOptions}
                      statusType={branchesLoading ? "loading" : "finished"}
                      loadingText="Loading branches…"
                      empty={
                        loadedBranchesFor
                          ? "No branches found"
                          : "Enter a repository URL to load branches"
                      }
                      enteredTextLabel={(v) => `Use: ${v}`}
                      placeholder="Default branch"
                      ariaLabel="Branch or tag"
                    />
                  </FormField>
                </SpaceBetween>
              )}

              {sourceType === "upload" && (
                <SpaceBetween size="l">
                  <Alert type="info">
                    <SpaceBetween size="xs">
                      <span>CodeKB can analyze these languages:</span>
                      <SupportedLanguageBadges />
                      <span>
                        Files in other languages are skipped. If none of the
                        files are in a supported language, there's nothing to
                        analyze.
                      </span>
                    </SpaceBetween>
                  </Alert>

                  <FormField
                    label={
                      <LabelWithInfo
                        label="Space name"
                        info="A friendly name shown in the spaces list. This is also used to generate the space identifier, so pick something recognizable."
                      />
                    }
                    description="A human-readable name for this codebase"
                    constraintText="Used to generate the space identifier"
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
                        label="Source zip file"
                        info="Zip the source directory itself (not a folder containing unrelated files). CodeKB scans the extracted contents for supported source files. Build artifacts, dependencies, and binaries are ignored."
                      />
                    }
                    description="Zip your source code directory and upload it here"
                    constraintText="Max 500 MB. Must be a .zip file."
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
                      label="Uploading source code"
                      description={
                        uploadProgress < 30
                          ? "Preparing upload..."
                          : uploadProgress < 70
                          ? "Uploading to S3..."
                          : uploadProgress < 100
                          ? "Starting analysis..."
                          : "Done"
                      }
                    />
                  )}
                </SpaceBetween>
              )}
            </SpaceBetween>
          </Container>

          <Container
            header={
              <Header variant="h2" description="Choose how much AI enrichment CodeKB should do after parsing.">
                <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                  <span>Analysis options</span>
                  <Badge color="blue">Experimental</Badge>
                </span>
              </Header>
            }
          >
            <SpaceBetween size="m">
              <FormField
                description={
                  <>
                    Business behaviour relevance estimates how likely a component is to represent
                    product or workflow behaviour rather than plumbing, helpers, or framework glue.
                    For the full scoring model, see the{" "}
                    <Link onFollow={(e) => { e.preventDefault(); navigate("/docs#business-relevance"); }}>
                      documentation
                    </Link>.
                  </>
                }
              >
                <Toggle
                  checked={omitLowBusinessRelevance}
                  onChange={({ detail }) => setOmitLowBusinessRelevance(detail.checked)}
                >
                  Omit low business relevance components from AI summarization
                </Toggle>
              </FormField>
            </SpaceBetween>
          </Container>

        </SpaceBetween>
      </Form>
    </SpaceBetween>
  );
}