import { getAccessToken, getUser } from '../auth';

const API_BASE =
  (import.meta.env.VITE_API_URL as string) || `${import.meta.env.BASE_URL.replace(/\/+$/, '')}/api`;

/** Path to the Git credentials page (used by error UIs linking to token setup). */
export const GIT_CREDS_PATH = "/git-creds";

/**
 * A failed API response, carrying the HTTP status and — for authorization
 * failures — the body `error` code and its extras. The space-authorization
 * backend returns `{error, message, ...}` (not FastAPI's `detail`), so UIs can
 * branch on `code`: `read_denied` → no-access, `git_credentials_expired` →
 * reconnect prompt, `write_denied`/`repo_read_denied` → not-allowed,
 * `hyperspace_access_denied` → names `inaccessibleSpaceIds`,
 * `authorization_unavailable` → transient "try again" (503).
 *
 * A 401 carrying a `code` is an authorization denial, not an auth failure — the
 * caller must NOT refresh the token or log out; it should stop polling.
 */
export class ApiError extends Error {
  status: number;
  code?: string;
  provider?: string;
  updateUrl?: string;
  inaccessibleSpaceIds?: string[];
  retryAfter?: number;

  constructor(status: number, body: Record<string, any>, retryAfter?: number) {
    super(body?.message || body?.detail || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = body?.error;
    this.provider = body?.provider;
    this.updateUrl = body?.update_url;
    this.inaccessibleSpaceIds = body?.inaccessible_space_ids;
    this.retryAfter = retryAfter;
  }

  /** True when this is an authorization decision (has a body `error` code). */
  get isAuthz(): boolean {
    return typeof this.code === "string" && this.code.length > 0;
  }

  /** True when the caller's git token expired/was revoked (needs reconnect). */
  get isExpiredCredentials(): boolean {
    return this.code === "git_credentials_expired";
  }

  /** True when git permissions couldn't be checked (provider down, 503) — transient. */
  get isAuthorizationUnavailable(): boolean {
    return this.code === "authorization_unavailable";
  }

  /** User-facing message, naming the inaccessible spaces when the body carries them. */
  get displayMessage(): string {
    if (this.inaccessibleSpaceIds && this.inaccessibleSpaceIds.length > 0) {
      return `${this.message} Inaccessible spaces: ${this.inaccessibleSpaceIds.join(", ")}.`;
    }
    return this.message;
  }
}

/** Build an ApiError from a non-OK response, reading its JSON body once.
 *  Exported for call sites that bypass `request()` (e.g. the agent SSE stream). */
export async function errorFromResponse(res: Response): Promise<ApiError> {
  const body = await res.json().catch(() => ({}));
  const retryAfterHeader = res.headers.get("Retry-After");
  const retryAfter = retryAfterHeader ? Number(retryAfterHeader) : undefined;
  return new ApiError(res.status, body, Number.isNaN(retryAfter) ? undefined : retryAfter);
}

export interface SpaceSummary {
  space_id: string;
  status?: string;
  component_count?: number;
  vector_count?: number;
  source_directory?: string;
  git_url?: string;
  source_type?: string;
  space_name?: string;
  language_count?: number;
  languages?: string[];
  total_lines?: number;
  created_by?: string;
  created_at?: string;
  omit_low_business_relevance?: boolean;
  visibility?: string | null; // "public" | "private" for git spaces; null for zip/legacy
  error?: string;
}

/** Chat, search and hyperspaces use these; a failed refresh keeps the last indexed snapshot. */
export const isQueryable = (status?: string) => status === "READY" || status === "REFRESH_FAILED";

export interface SpaceDetail {
  space_id: string;
  space_name?: string;
  source_directory: string;
  component_count: number;
  created_at?: string;
  created_by?: string;
  version: string;
  statistics: Record<string, any>;
  vector_count: number;
  git_url: string;
  git_commit_hash?: string;
  overview_language?: string;
  analysis_language?: string;
  /** Exclusion patterns the space was created with (fixed for its lifetime). */
  exclude_patterns: string[];
  visibility?: string | null; // "public" | "private" for git spaces; null for zip/legacy
}

export interface PipelineStep {
  step: string;
  status: string; // in_progress | completed | failed
  started_at?: string;
  updated_at?: string;
}

/** One space-refresh run: its start time and the steps it executed. */
export interface RefreshPipelineRun {
  started_at?: string;
  steps: PipelineStep[];
}

export interface SpaceOverviewDoc {
  key: string;
  title: string;
  s3_path: string;
  generated_at?: string;
  stale?: boolean;
}

export interface RefreshResultPayload {
  files_added: number;
  files_modified: number;
  files_removed: number;
  files_unchanged: number;
  components_added: number;
  components_removed: number;
  components_reprocessed: number;
  vectors_upserted: number;
  vectors_deleted: number;
  duration_s: number;
}

export interface BusinessContext {
  business_terms?: string[];
  technical_terms?: string[];
  business_modules?: string[];
  entrypoint_patterns?: string[];
  confidence?: number;
  source?: string;
  error?: string;
}

/** One workload's share of an operation: the settings it ran with and its tokens. */
export interface WorkloadUsage {
  /** summarization | business_context | overview | chat */
  workload: string;
  model_id: string;
  /** thinking.type sent; null when nothing was sent. */
  thinking: string | null;
  /** output_config.effort sent; null when nothing was sent. */
  effort: string | null;
  /** Bedrock region the calls went to. Absent on records written before it was tracked. */
  region?: string;
  input: number;
  output: number;
  total: number;
  /** Share of output spent thinking, when the model reported it. */
  thinking_tokens?: number;
}

/** Per-model Bedrock token consumption. Cost is not computed — see pricing_url. */
export interface TokenUsage {
  /** ``thinking`` is the share of output spent thinking, present only when non-zero. */
  by_model?: Record<string, { input: number; output: number; thinking?: number }>;
  totals?: { input: number; output: number; total: number; thinking?: number };
  /** Per-workload breakdown with settings. Absent on records written before it existed. */
  workloads?: WorkloadUsage[];
  generated_at?: string;
  region?: string;
}

/** Recurring per-operation usage record (overview regen, space refresh). */
export interface RecurringTokenUsage {
  cumulative?: TokenUsage;
  generation_count?: number;
  update_count?: number;
  last_generation?: TokenUsage & { trigger?: string; documents?: number; at?: string };
  last_update?: TokenUsage & { trigger?: string; at?: string };
  recent?: Array<TokenUsage & { trigger?: string; at?: string }>;
}

export interface SpaceTokenUsage {
  space_creation?: TokenUsage;
  space_overview?: RecurringTokenUsage;
  space_refresh?: RecurringTokenUsage;
}

export interface AnalysisStatus {
  space_id: string;
  space_name?: string;
  status: string;
  component_count?: number;
  error?: string;
  current_step?: string;
  pipeline_steps?: PipelineStep[];
  refresh_pipeline_runs?: RefreshPipelineRun[];
  git_url?: string;
  branch?: string;
  git_commit_hash?: string;
  created_by?: string;
  started_at?: string;
  created_at?: string;
  updated_at?: string;
  summarize_total?: number;
  summarize_done?: number;
  summarize_percent?: number;
  /** Written only by older refresh code; the next completed refresh clears it. */
  refresh_warning?: string | null;
  space_overview_docs?: SpaceOverviewDoc[];
  overview_status?: "in_progress" | "completed" | "failed" | "STALE";
  overview_total?: number;
  overview_done?: number;
  overview_percent?: number;
  business_context?: BusinessContext;
  omit_low_business_relevance?: boolean;
  statistics?: Record<string, any>;
  token_usage?: SpaceTokenUsage;
  pricing_url?: string;
  bedrock_model?: { model_id?: string; region?: string };
  refresh_result?: RefreshResultPayload;
  refresh_errors?: string[];
}

export interface CreateSpaceRequest {
  git_url: string;
  space_id?: string;
  space_name?: string;
  branch?: string;
  summarize?: boolean;
  omit_low_business_relevance?: boolean;
  overview_language?: string;
  analysis_language?: string;
}

export interface GitBranches {
  git_url: string;
  branches: string[];
  tags: string[];
}

export interface UploadUrlRequest {
  space_name: string;
  summarize?: boolean;
  omit_low_business_relevance?: boolean;
}

export interface UploadUrlResponse {
  space_id: string;
  upload_url: string;
  s3_key: string;
  expires_in: number;
}

export interface StartUploadAnalysisRequest {
  space_id: string;
  space_name: string;
  summarize?: boolean;
  omit_low_business_relevance?: boolean;
  overview_language?: string;
  analysis_language?: string;
}

export interface Hyperspace {
  hyperspace_id: string;
  name: string;
  space_ids: string[];
  created_at?: string;
  created_by?: string;
  updated_at?: string;
  updated_by?: string;
  spaces?: { space_id: string; status: string; space_name?: string; component_count?: number }[];
  invalid?: boolean;
  missing_space_ids?: string[];
  /** Members the current caller can't READ (authz on). Overview/query is refused
   *  unless they can read every member. Empty/absent when authz is off. */
  inaccessible_space_ids?: string[];
  invalidated_at?: string;
  hyperspace_overview_docs?: SpaceOverviewDoc[];
  overview_status?: "in_progress" | "completed" | "failed";
  overview_total?: number;
  overview_done?: number;
  overview_percent?: number;
  overview_staleness?: {
    stale: boolean;
    reasons: {
      space_id: string;
      space_name?: string;
      reason: "code_changed" | "member_added" | "member_removed" | "space_missing" | "not_ready";
      from_commit?: string;
      to_commit?: string;
    }[];
  };
  overview_error?: string;
  token_usage?: { hyperspace_overview?: RecurringTokenUsage };
  pricing_url?: string;
  bedrock_model?: { model_id?: string; region?: string };
}

export interface GlobalMetrics {
  total_spaces: number;
  ready_spaces: number;
  failed_spaces: number;
  in_progress_spaces: number;
  total_components: number;
  total_lines_of_code: number;
  total_vectors: number;
  languages: string[];
  language_count: number;
}

export interface InfraStatus {
  ready: boolean;
  s3_bucket: { exists: boolean };
  vectors_bucket: { exists: boolean };
  vector_index: { exists: boolean };
  kms_key: { exists: boolean };
  error?: string;
}

export interface AppConfig {
  default_top_k: number;
  allowed_git_hosts: string[];
  infrastructure: InfraStatus;
  zip_upload_enabled: boolean;
  overview_generation_enabled: boolean;
  /** Git providers enabled for per-user git credentials (UI shows only these). */
  git_credential_providers: string[];
  /** Whether the server accepts credentials embedded in a git URL (user:token@host). */
  allow_credentials_in_git_url: boolean;
  /** System-wide exclusion patterns: default.yaml + CODEKB_EXCLUDE_PATTERNS_EXTRA. */
  exclude_patterns: string[];
  /** Models in effect. Optional so the page still renders against an older backend. */
  models?: ModelsConfig;
}

/** Outcome of the serving container's startup check of a workload's settings. */
export type StartupCheckStatus = "verified" | "not_verified" | "not_checked";

export interface WorkloadModel {
  /** summarization | business_context | overview | chat */
  workload: string;
  model_id: string;
  /** thinking.type sent to the model; null when the model's default applies. */
  thinking: string | null;
  /** output_config.effort; null when the model's default applies. */
  effort: string | null;
  /** Fields this workload sets itself instead of inheriting the shared default. */
  overrides: Array<"model_id" | "thinking" | "effort">;
  startup_check: StartupCheckStatus;
}

/** Shared defaults every workload inherits unless it overrides them. */
export interface ModelDefaults {
  model_id: string | null;
  thinking: string | null;
  effort: string | null;
}

export interface ModelsConfig {
  defaults: ModelDefaults;
  workloads: WorkloadModel[];
  embedding_model_id: string;
}

/** A saved git credential (never carries a token). */
export interface GitCredential {
  provider: string;
  username: string;
  /** "connected" | "expired" (the provider rejected the token). */
  status?: string;
}

/** Result of saving or testing a git credential. */
export interface GitCredentialStatus {
  provider: string;
  username: string;
  status: string;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = getAccessToken();
  if (token) {
    // Standard RFC 6750 Bearer scheme. The hosted deployment has no edge
    // authorizer — the container verifies the JWT (see api/auth.py verify_token,
    // which accepts "Bearer <jwt>" or a bare JWT).
    headers["Authorization"] = `Bearer ${token}`;
  }
  const user = getUser();
  if (user?.email) {
    headers["X-User-Identity"] = user.email;
  }
  const res = await fetch(`${API_BASE}${path}`, {
    headers,
    ...options,
  });
  if (!res.ok) {
    throw await errorFromResponse(res);
  }
  return res.json();
}

export const api = {
  listSpaces: () => request<SpaceSummary[]>("/spaces"),

  getGitBranches: (gitUrl: string) =>
    request<GitBranches>(`/spaces/git-branches?git_url=${encodeURIComponent(gitUrl)}`),

  createSpace: (data: CreateSpaceRequest) =>
    request<Record<string, any>>("/spaces", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  getUploadUrl: (data: UploadUrlRequest) =>
    request<UploadUrlResponse>("/spaces/upload-url", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  startUploadAnalysis: (data: StartUploadAnalysisRequest) =>
    request<Record<string, any>>("/spaces/upload", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  uploadZipToS3: async (url: string, file: File): Promise<void> => {
    const res = await fetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/zip" },
      body: file,
    });
    if (!res.ok) {
      throw new Error(`Upload failed: HTTP ${res.status}`);
    }
  },

  getSpace: (id: string) => request<SpaceDetail>(`/spaces/${id}`),

  deleteSpace: (id: string) =>
    request<Record<string, any>>(`/spaces/${id}`, { method: "DELETE" }),

  refreshSpace: (id: string) =>
    request<Record<string, any>>(`/spaces/${id}/refresh`, { method: "POST" }),

  getStatus: (id: string) => request<AnalysisStatus>(`/spaces/${id}/status`),

  getKnowledgeBase: (id: string) => request<Record<string, any>>(`/spaces/${id}/knowledge-base`),

  getOverviewDocument: async (id: string, docKey: string): Promise<string> => {
    const headers: Record<string, string> = {};
    const token = getAccessToken();
    // Standard Bearer scheme (see request() above and api/auth.py verify_token).
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const user = getUser();
    if (user?.email) headers["X-User-Identity"] = user.email;
    const res = await fetch(`${API_BASE}/spaces/${id}/overview/${docKey}`, { headers });
    if (!res.ok) {
      throw await errorFromResponse(res);
    }
    return res.text();
  },

  // Query / Explorer
  listComponents: (id: string, params?: { pattern?: string; type?: string }) => {
    const qs = new URLSearchParams();
    if (params?.pattern) qs.set("pattern", params.pattern);
    if (params?.type) qs.set("type", params.type);
    const suffix = qs.toString() ? `?${qs}` : "";
    return request<Record<string, any>[]>(`/spaces/${id}/components${suffix}`);
  },

  findComponent: (id: string, name: string) =>
    request<Record<string, any>>(`/spaces/${id}/components/${encodeURIComponent(name)}`),

  traceFlow: (id: string, name: string, depth = 10) =>
    request<Record<string, any>>(`/spaces/${id}/components/${encodeURIComponent(name)}/flow?depth=${depth}`),

  showDependencies: (id: string, name: string) =>
    request<Record<string, any>>(`/spaces/${id}/components/${encodeURIComponent(name)}/deps`),

  semanticSearch: (id: string, query: string, limit?: number) => {
    const qs = new URLSearchParams({ q: query });
    if (limit !== undefined) qs.set("limit", String(limit));
    return request<Record<string, any>[]>(`/spaces/${id}/search?${qs}`);
  },

  globalSemanticSearch: (query: string, limit?: number, spaceIds?: string[]) => {
    const qs = new URLSearchParams({ q: query });
    if (limit !== undefined) qs.set("limit", String(limit));
    if (spaceIds?.length) qs.set("spaces", spaceIds.join(","));
    return request<Record<string, any>[]>(`/search?${qs}`);
  },

  getConfig: () => request<AppConfig>("/config"),

  getGlobalMetrics: () => request<GlobalMetrics>("/metrics"),

  // Hyperspaces
  listHyperspaces: () => request<Hyperspace[]>("/hyperspaces"),

  createHyperspace: (name: string, spaceIds: string[]) =>
    request<Hyperspace>("/hyperspaces", {
      method: "POST",
      body: JSON.stringify({ name, space_ids: spaceIds }),
    }),

  getHyperspace: (id: string) => request<Hyperspace>(`/hyperspaces/${id}`),

  updateHyperspace: (id: string, payload: { space_ids?: string[] }) =>
    request<Hyperspace>(`/hyperspaces/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  deleteHyperspace: (id: string) =>
    request<Record<string, any>>(`/hyperspaces/${id}`, { method: "DELETE" }),

  generateHyperspaceOverview: (id: string) =>
    request<Record<string, any>>(`/hyperspaces/${id}/generate-overview`, { method: "POST" }),

  getHyperspaceOverviewDocument: async (id: string, docKey: string): Promise<string> => {
    const headers: Record<string, string> = {};
    const token = getAccessToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const user = getUser();
    if (user?.email) headers["X-User-Identity"] = user.email;
    const res = await fetch(`${API_BASE}/hyperspaces/${id}/overview/${docKey}`, { headers });
    if (!res.ok) {
      throw await errorFromResponse(res);
    }
    return res.text();
  },


  agentChat: (spaceIds: string[], message: string) =>
    request<{ space_ids: string[]; message: string; response: string }>(
      `/agent/chat`,
      { method: "POST", body: JSON.stringify({ message, space_ids: spaceIds }) },
    ),

  generateOverview: (id: string) =>
    request<Record<string, any>>(`/spaces/${id}/generate-overview`, { method: "POST" }),

  agentChatStream: (spaceIds: string[], message: string): EventSource | null => {
    // SSE streaming is handled directly in the component via fetch
    return null;
  },

  // Per-user git credentials. Tokens are write-only: sent on save,
  // never returned by the API.
  listGitCredentials: () => request<GitCredential[]>("/user-profile/gitcreds"),

  saveGitCredential: (provider: string, payload: { username: string; token: string }) =>
    request<GitCredentialStatus>(`/user-profile/gitcreds/${provider}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  testGitCredential: (provider: string) =>
    request<GitCredentialStatus>(`/user-profile/gitcreds/${provider}/test`, { method: "POST" }),

  deleteGitCredential: (provider: string) =>
    request<Record<string, any>>(`/user-profile/gitcreds/${provider}`, { method: "DELETE" }),
};
