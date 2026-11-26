import { useState, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import FormField from "@cloudscape-design/components/form-field";
import Button from "@cloudscape-design/components/button";
import Multiselect, { MultiselectProps } from "@cloudscape-design/components/multiselect";
import Box from "@cloudscape-design/components/box";
import Badge from "@cloudscape-design/components/badge";
import Alert from "@cloudscape-design/components/alert";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Tiles from "@cloudscape-design/components/tiles";
import PromptInput from "@cloudscape-design/components/prompt-input";
import LiveRegion from "@cloudscape-design/components/live-region";
import {
  Avatar,
  ChatBubble,
  LoadingBar,
  SupportPromptGroup,
} from "@cloudscape-design/chat-components";
import { api, ApiError, errorFromResponse, isQueryable, SpaceSummary, Hyperspace } from "../api/client";
import { getAccessToken, getUser } from "../auth";
import CopyButton from "../components/CopyButton";
import CodeBlock, { languageFromPath } from "../components/CodeBlock";
import "../styles/markdown.css";

const API_BASE =
  import.meta.env.VITE_API_URL || `${import.meta.env.BASE_URL.replace(/\/+$/, "")}/api`;

/** Ensure markdown headings start on their own line (fixes streaming artifacts). */
function fixMarkdown(text: string): string {
  return text.replace(/([^\n])(#{1,4}\s)/g, "$1\n\n$2");
}

/** Strip markdown syntax for clean preview text in collapsed headers. */
function stripMarkdown(text: string): string {
  return text
    .replace(/#{1,4}\s/g, "")           // headings
    .replace(/\*\*(.+?)\*\*/g, "$1")    // bold
    .replace(/\*(.+?)\*/g, "$1")        // italic
    .replace(/`(.+?)`/g, "$1")          // inline code
    .replace(/```[\s\S]*?```/g, "")     // code blocks
    .replace(/\[(.+?)\]\(.+?\)/g, "$1") // links
    .replace(/\n+/g, " ")               // newlines to spaces
    .replace(/\s+/g, " ")               // collapse whitespace
    .trim();
}

/** Recursively extract plain text from React children (for code block copy). */
function extractTextFromChildren(children: React.ReactNode): string {
  if (typeof children === "string") return children;
  if (Array.isArray(children)) return children.map(extractTextFromChildren).join("");
  if (children && typeof children === "object" && "props" in children) {
    return extractTextFromChildren((children as React.ReactElement).props.children ?? "");
  }
  return "";
}

type AgentMode = "chat" | "impact_analysis";

interface TokenUsageInfo {
  by_model?: Record<string, { input: number; output: number }>;
  totals?: { input: number; output: number; total: number };
}

/** Format a token count compactly (e.g. 12,480). */
function fmtTokens(n: number): string {
  return n.toLocaleString();
}

/** Small footer line showing Bedrock token consumption for a chat turn. */
function TokenUsageBadge({
  usage,
  pricingUrl,
}: {
  usage?: TokenUsageInfo;
  pricingUrl?: string;
}) {
  const { t } = useTranslation();
  const totals = usage?.totals;
  if (!totals || totals.total <= 0) return null;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
      <Badge color="blue">{t("agentChat.tokensBadge", { total: fmtTokens(totals.total) })}</Badge>
      <Box variant="small" color="text-body-secondary">
        {t("agentChat.tokensInOut", { input: fmtTokens(totals.input), output: fmtTokens(totals.output) })}
        {pricingUrl && (
          <>
            {" · "}
            <a href={pricingUrl} target="_blank" rel="noopener noreferrer">
              {t("agentChat.estimateCost")}
            </a>
          </>
        )}
      </Box>
    </div>
  );
}

interface ToolStep {
  tool: string;
  input: Record<string, any>;
  result?: string;
  status: "running" | "done" | "error";
  toolUseId?: string;
}

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  steps?: ToolStep[];
  reasoning?: string;
  timestamp: Date;
  tokenUsage?: TokenUsageInfo;
  pricingUrl?: string;
}

/**
 * Assemble the whole conversation into a single Markdown document: a title,
 * then each turn under its own heading. Mirrors the "Copy as Markdown" export
 * on the Space Overview and Hyperspace Overview pages.
 */
function conversationToMarkdown(
  messages: ChatMessage[],
  scopeLabel: string,
  agentMode: AgentMode,
  t: TFunction,
): string {
  const title =
    `# ${t("agentChat.mdTitle")}${agentMode === "impact_analysis" ? ` — ${t("agentChat.modeImpactAnalysis")}` : ""}` +
    (scopeLabel ? ` — ${scopeLabel}` : "");
  return [
    title,
    "",
    ...messages.flatMap((m) => [
      `## ${m.role === "user" ? t("agentChat.you") : "Agent Hopper"}`,
      "",
      m.content.trim(),
      "",
    ]),
  ].join("\n");
}

/** Copy button styled for dark code blocks — light text on a translucent background. */
function CodeCopyButton({ text }: { text: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  const handleClick = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <button
      type="button"
      onClick={handleClick}
      aria-label={copied ? t("common.copied") : t("agentChat.copyCode")}
      title={copied ? t("agentChat.copiedBang") : t("agentChat.copyCode")}
      className={`agent-code-copy-btn${copied ? " agent-code-copy-btn--copied" : ""}`}
    >
      {copied ? `✓ ${t("common.copied")}` : t("common.copy")}
    </button>
  );
}

/** Renders a list of tool steps with consistent formatting. */
/** Pretty-print a value as JSON if possible; otherwise return it as text. */
function prettyMaybeJson(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }
  const str = String(value);
  const trimmed = str.trim();
  if (
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"))
  ) {
    try {
      return JSON.stringify(JSON.parse(trimmed), null, 2);
    } catch {
      return str;
    }
  }
  return str;
}

function ToolStepList({ steps }: { steps: ToolStep[] }) {
  return (
    <SpaceBetween size="xs">
      {steps.map((step, j) => (
        <ToolStepDisplay key={step.toolUseId || j} step={step} index={j + 1} />
      ))}
    </SpaceBetween>
  );
}

/**
 * If a tool response is JSON carrying source code (get_component_source,
 * read_source_file), return the code plus an inferred language. Otherwise null.
 */
function extractSourceResult(
  result: string,
): { code: string; language?: string; filePath?: string } | null {
  const trimmed = result.trim();
  if (!trimmed.startsWith("{")) return null;
  try {
    const obj = JSON.parse(trimmed);
    const code = obj.source_code ?? obj.content;
    if (typeof code !== "string" || !code) return null;
    const filePath = typeof obj.file_path === "string" ? obj.file_path : undefined;
    return { code, language: languageFromPath(filePath), filePath };
  } catch {
    return null;
  }
}

/** Render a tool Response: highlighted source if detected, else pretty JSON/text. */
function ToolResponse({ result, isError }: { result: string; isError: boolean }) {
  const source = !isError ? extractSourceResult(result) : null;
  if (source) {
    return (
      <div>
        {source.filePath && (
          <Box variant="small" color="text-body-secondary">
            {source.filePath}
          </Box>
        )}
        <CodeBlock
          code={source.code}
          language={source.language}
          className="tool-step__source"
        />
      </div>
    );
  }
  return (
    <pre className={`tool-step__code${isError ? " tool-step__code--error" : ""}`}>
      {prettyMaybeJson(result)}
    </pre>
  );
}

function ToolStepDisplay({ step, index }: { step: ToolStep; index: number }) {
  const { t } = useTranslation();
  const hasInput =
    step.input &&
    Object.values(step.input).some((v) => v !== undefined && v !== null && v !== "");

  const statusType =
    step.status === "running"
      ? "in-progress"
      : step.status === "error"
        ? "error"
        : "success";

  return (
    <div className="tool-step">
      <ExpandableSection
        variant="footer"
        headerText={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span className="tool-step__index">{index}.</span>
            <StatusIndicator type={statusType}>
              <span style={{ fontWeight: 600, fontSize: 13 }}>{step.tool}</span>
            </StatusIndicator>
          </span>
        }
      >
        <SpaceBetween size="xs">
          {hasInput && (
            <div>
              <Box variant="awsui-key-label">{t("agentChat.toolRequest")}</Box>
              <pre className="tool-step__code">{prettyMaybeJson(step.input)}</pre>
            </div>
          )}
          {step.result && step.status !== "running" && (
            <div>
              <Box variant="awsui-key-label">{t("agentChat.toolResponse")}</Box>
              <ToolResponse result={step.result} isError={step.status === "error"} />
            </div>
          )}
          {step.status === "running" && (
            <Box color="text-body-secondary" fontSize="body-s">
              {t("agentChat.toolRunning")}
            </Box>
          )}
        </SpaceBetween>
      </ExpandableSection>
    </div>
  );
}

/** Collapsible "Thinking" trace showing the model's reasoning. */
function ReasoningSection({
  text,
  defaultExpanded = false,
}: {
  text: string;
  defaultExpanded?: boolean;
}) {
  const { t } = useTranslation();
  if (!text.trim()) return null;
  return (
    <div style={{ marginBottom: 8 }}>
      <ExpandableSection
        headerText={t("agentChat.thinking")}
        variant="footer"
        defaultExpanded={defaultExpanded}
      >
        <div className="agent-reasoning">{text}</div>
      </ExpandableSection>
    </div>
  );
}

/** Markdown renderer with code-block copy buttons. */
function AgentMarkdown({ text }: { text: string }) {
  return (
    <div className="agent-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
        components={{
          pre({ children, ...props }) {
            const codeText = extractTextFromChildren(children);
            return (
              <div style={{ position: "relative" }}>
                <pre {...props}>{children}</pre>
                {codeText && <CodeCopyButton text={codeText} />}
              </div>
            );
          },
        }}
      >
        {fixMarkdown(text)}
      </ReactMarkdown>
    </div>
  );
}

const suggestedPromptsByMode = (t: TFunction): Record<AgentMode, { id: string; text: string }[]> => ({
  chat: [
    { id: "entry-points",  text: t("agentChat.promptEntryPoints") },
    { id: "exec-flow",     text: t("agentChat.promptExecFlow") },
    { id: "auth",          text: t("agentChat.promptAuth") },
    { id: "coupling",      text: t("agentChat.promptCoupling") },
  ],
  impact_analysis: [
    { id: "div-by-zero",   text: t("agentChat.promptDivByZero") },
    { id: "validation",    text: t("agentChat.promptValidation") },
    { id: "gh-issue",      text: "https://github.com/org/repo/issues/42" },
    { id: "async-parser",  text: t("agentChat.promptAsyncParser") },
  ],
});

/** Sentinel option value meaning "every space" — never sent to the backend. */
const ALL_SPACES_VALUE = "__all__";

/** Max space ids shown inline before collapsing to a count. */
const MAX_SPACES_IN_LABEL = 5;

export default function AgentChat() {
  const { t } = useTranslation();
  const [projects, setProjects] = useState<SpaceSummary[]>([]);
  const [hyperspaces, setHyperspaces] = useState<Hyperspace[]>([]);
  const [selectedProject, setSelectedProject] =
    useState<MultiselectProps.Option[]>([]);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [currentSteps, setCurrentSteps] = useState<ToolStep[]>([]);
  const [streamingText, setStreamingText] = useState("");
  const [streamingReasoning, setStreamingReasoning] = useState("");
  const [agentMode, setAgentMode] = useState<AgentMode>("chat");
  const [collapsedMessages, setCollapsedMessages] = useState<Set<number>>(new Set());
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const userIdentity = getUser();
  const userInitials = userIdentity?.email
    ? userIdentity.email.charAt(0).toUpperCase()
    : undefined;

  useEffect(() => {
    api.listSpaces().then(setProjects).catch(() => {});
    api.listHyperspaces().then(setHyperspaces).catch(() => {});
  }, []);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, currentSteps, streamingText, streamingReasoning]);

  const readySpaces = projects.filter((p) => isQueryable(p.status));

  const ALL_SPACES_OPTION: MultiselectProps.Option = {
    label: t("agentChat.allSpaces"),
    value: ALL_SPACES_VALUE,
    description: t("agentChat.allSpacesDescription", { count: readySpaces.length }),
  };

  const projectOptions: MultiselectProps.Options = [
    ALL_SPACES_OPTION,
    ...(hyperspaces.length > 0
      ? [
          {
            label: t("nav.hyperspaces"),
            options: hyperspaces.map((hs) => ({
              label: `⚡ ${hs.name}`,
              value: `hs:${hs.hyperspace_id}`,
              description: t("agentChat.spacesCount", { count: hs.space_ids.length }),
            })),
          },
        ]
      : []),
    ...readySpaces.map((p) => ({
      label: p.space_name || p.space_id,
      value: p.space_id,
      description: t("agentChat.componentsLanguages", { count: p.component_count ?? 0, languages: (p.languages ?? []).join(", ") }),
    })),
  ];

  // Resolve hyperspace selections to their constituent space IDs.
  // The "All spaces" sentinel is deliberately not expanded — the backend
  // treats a null space_ids as "search everything" and runs the vector
  // search unfiltered, which is the only thing that scales to 1000s of spaces.
  const resolveSpaceIds = (options: MultiselectProps.Option[]): string[] => {
    const ids = new Set<string>();
    for (const opt of options) {
      const val = opt.value!;
      if (val === ALL_SPACES_VALUE) {
        continue;
      } else if (val.startsWith("hs:")) {
        const hsId = val.slice(3);
        const hs = hyperspaces.find((h) => h.hyperspace_id === hsId);
        if (hs) hs.space_ids.forEach((sid) => ids.add(sid));
      } else {
        ids.add(val);
      }
    }
    return Array.from(ids);
  };

  // "All spaces" wins over any other selection: the scope is unfiltered.
  const allSpacesSelected = selectedProject.some(
    (opt) => opt.value === ALL_SPACES_VALUE,
  );
  const activeSpaceIds = allSpacesSelected ? [] : resolveSpaceIds(selectedProject);
  const hasScope = allSpacesSelected || activeSpaceIds.length > 0;

  const scopeLabel = allSpacesSelected
    ? t("agentChat.allSpaces")
    : activeSpaceIds.length > MAX_SPACES_IN_LABEL
      ? t("agentChat.spacesCount", { count: activeSpaceIds.length })
      : activeSpaceIds.join(", ");

  // A hyperspace query needs READ on every member (all-or-nothing, like the
  // hyperspace gate). If a selected hyperspace has members the caller can't read,
  // block the request. Individually-pickable spaces are already readable-only, so
  // only hyperspace selections can introduce an inaccessible member.
  const blockedHyperspaces = selectedProject
    .filter((o) => typeof o.value === "string" && (o.value as string).startsWith("hs:"))
    .map((o) => hyperspaces.find((h) => h.hyperspace_id === (o.value as string).slice(3)))
    .filter((h): h is Hyperspace => !!h && (h.inaccessible_space_ids?.length ?? 0) > 0);
  const scopeBlocked = blockedHyperspaces.length > 0;
  const canSubmit = hasScope && !scopeBlocked;

  const clearConversation = () => {
    setMessages([]);
    setError(null);
    setCurrentSteps([]);
    setStreamingText("");
    setStreamingReasoning("");
    setCollapsedMessages(new Set());
  };

  const submit = async (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || !canSubmit || loading) return;

    const userMsg: ChatMessage = {
      role: "user",
      content: trimmed,
      timestamp: new Date(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setLoading(true);
    setError(null);
    setCurrentSteps([]);
    setStreamingText("");
    setStreamingReasoning("");

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const body: Record<string, any> = {
        message: trimmed,
        // null = every space; the backend then skips the metadata filter
        // instead of receiving thousands of enumerated space ids.
        space_ids: allSpacesSelected ? null : activeSpaceIds,
        mode: agentMode,
      };
      // Build history from existing UI messages (user + assistant text only)
      const history = messages.map((m) => ({ role: m.role, content: m.content }));
      if (history.length > 0) {
        body.history = history;
      }

      const fetchHeaders: Record<string, string> = { "Content-Type": "application/json" };
      const token = getAccessToken();
      if (token) fetchHeaders["Authorization"] = `Bearer ${token}`;
      const chatUser = getUser();
      if (chatUser?.email) fetchHeaders["X-User-Identity"] = chatUser.email;

      const res = await fetch(`${API_BASE}/agent/chat/stream`, {
        method: "POST",
        headers: fetchHeaders,
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (!res.ok) throw await errorFromResponse(res);

      const reader = res.body?.getReader();
      if (!reader) throw new Error(t("agentChat.noResponseBody"));

      const decoder = new TextDecoder();
      let buffer = "";
      let steps: ToolStep[] = [];
      let fullText = "";
      let fullReasoning = "";
      let doneReceived = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        let eventType = "";
        for (const line of lines) {
          if (line.startsWith("event: ")) {
            eventType = line.slice(7).trim();
          } else if (line.startsWith("data: ")) {
            const data = JSON.parse(line.slice(6));

            if (eventType === "tool_start") {
              const tid = data.toolUseId;
              const existingIdx = tid
                ? steps.findIndex((s) => s.toolUseId === tid)
                : -1;
              if (existingIdx >= 0) {
                steps[existingIdx].input = data.input || {};
                steps = [...steps];
              } else {
                steps = [
                  ...steps,
                  {
                    tool: data.tool,
                    input: data.input || {},
                    status: "running",
                    toolUseId: tid,
                  },
                ];
              }
              setCurrentSteps([...steps]);
            } else if (eventType === "tool_done") {
              const tid = data.toolUseId;
              const idx = tid
                ? steps.findIndex((s) => s.toolUseId === tid)
                : -1;
              if (idx >= 0) {
                steps[idx].status = "done";
                setCurrentSteps([...steps]);
              }
            } else if (eventType === "tool_result") {
              const tid = data.toolUseId;
              const idx = tid
                ? steps.findIndex((s) => s.toolUseId === tid)
                : -1;
              if (idx >= 0) {
                steps[idx].result = data.result;
                steps[idx].status = data.status === "error" ? "error" : "done";
                steps = [...steps];
                setCurrentSteps([...steps]);
              }
            } else if (eventType === "reasoning") {
              fullReasoning += data.text;
              setStreamingReasoning(fullReasoning);
            } else if (eventType === "text") {
              fullText += data.text;
              setStreamingText(fullText);
            } else if (eventType === "done") {
              doneReceived = true;
              const finalText = fullText || data.response;
              const tokenUsage = data.token_usage as TokenUsageInfo | undefined;
              const pricingUrl = data.pricing_url as string | undefined;
              // Auto-collapse all previous assistant messages
              setMessages((prev) => {
                const prevAssistantIndices = new Set<number>();
                prev.forEach((m, idx) => {
                  if (m.role === "assistant") prevAssistantIndices.add(idx);
                });
                setCollapsedMessages(prevAssistantIndices);
                return [
                  ...prev,
                  {
                    role: "assistant",
                    content: finalText,
                    steps: steps.length > 0 ? [...steps] : undefined,
                    reasoning: fullReasoning || undefined,
                    timestamp: new Date(),
                    tokenUsage,
                    pricingUrl,
                  },
                ];
              });
              setCurrentSteps([]);
              setStreamingText("");
              setStreamingReasoning("");
            }
            eventType = "";
          }
        }
      }

      // If stream ended without an explicit done event
      if (fullText && !doneReceived) {
        setMessages((prev) => {
          const prevAssistantIndices = new Set<number>();
          prev.forEach((m, idx) => {
            if (m.role === "assistant") prevAssistantIndices.add(idx);
          });
          setCollapsedMessages(prevAssistantIndices);
          return [
            ...prev,
            {
              role: "assistant",
              content: fullText || t("agentChat.noResponse"),
              steps: steps.length > 0 ? [...steps] : undefined,
              reasoning: fullReasoning || undefined,
              timestamp: new Date(),
            },
          ];
        });
        setCurrentSteps([]);
        setStreamingText("");
        setStreamingReasoning("");
      }
    } catch (e: any) {
      if (e.name !== "AbortError") {
        if (e instanceof ApiError && e.isExpiredCredentials) {
          setError(t("agentChat.reconnectToken", { message: e.message }));
        } else if (e instanceof ApiError && e.isAuthz) {
          // Other authz denials (hyperspace_access_denied, read_denied): the
          // authz-aware message names the blocked member spaces.
          setError(e.displayMessage);
        } else {
          setError(e.message || t("agentChat.requestFailed"));
        }
      }
    } finally {
      setLoading(false);
      abortRef.current = null;
    }
  };

  const handleSend = () => submit(input);

  const formatTimestamp = (d: Date) =>
    d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

  // ── Status text for the live region (announces streaming progress) ──
  const liveStatus = loading
    ? currentSteps.length > 0
      ? t("agentChat.liveUsingTool", { tool: currentSteps[currentSteps.length - 1].tool })
      : streamingText
        ? t("agentChat.liveGenerating")
        : streamingReasoning
          ? t("agentChat.liveThinkingThrough")
          : t("agentChat.liveThinking")
    : "";

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={t("agentChat.description")}
      >
        Agent Hopper
      </Header>

      <Container>
        <FormField label={t("agentChat.scopeLabel")} description={t("agentChat.scopeDescription")}>
          <Multiselect
            selectedOptions={selectedProject}
            onChange={({ detail }) => {
              const picked = [...detail.selectedOptions];
              const all = picked.find((o) => o.value === ALL_SPACES_VALUE);
              // "All spaces" is exclusive — collapse the chips to a single
              // token instead of listing every space id.
              setSelectedProject(all ? [all] : picked);
              // Changing spaces starts a fresh conversation
              clearConversation();
            }}
            options={projectOptions}
            placeholder={t("agentChat.scopePlaceholder")}
            filteringType="auto"
          />
        </FormField>
      </Container>

      {scopeBlocked && (
        <Alert type="warning" header={t("agentChat.blockedHeader")}>
          <SpaceBetween size="xs">
            {blockedHyperspaces.map((h) => (
              <span key={h.hyperspace_id}>
                <b>{h.name}</b>{t("agentChat.blockedCantRead", { ids: h.inaccessible_space_ids!.join(", ") })}
              </span>
            ))}
            <span>
              {t("agentChat.blockedHelp")}
            </span>
          </SpaceBetween>
        </Alert>
      )}

      {hasScope && (
        <Container>
          <FormField label={t("agentChat.modeLabel")} description={t("agentChat.modeDescription")}>
            <Tiles
              value={agentMode}
              onChange={({ detail }) => {
                setAgentMode(detail.value as AgentMode);
                clearConversation();
              }}
              items={[
                {
                  value: "chat",
                  label: t("agentChat.modeChat"),
                  description: t("agentChat.modeChatDescription"),
                },
                {
                  value: "impact_analysis",
                  label: t("agentChat.modeImpactAnalysis"),
                  description: t("agentChat.modeImpactAnalysisDescription"),
                },
              ]}
            />
          </FormField>
        </Container>
      )}

      {hasScope && (
        <Container
          header={
            <Header
              variant="h2"
              actions={
                messages.length > 0 ? (
                  <SpaceBetween direction="horizontal" size="xs">
                    <CopyButton
                      variant="normal"
                      label={t("agentChat.copyAsMarkdown")}
                      text={conversationToMarkdown(messages, scopeLabel, agentMode, t)}
                      disabled={loading}
                    />
                    <Button
                      onClick={clearConversation}
                      disabled={loading}
                      iconName="refresh"
                    >
                      {t("agentChat.newConversation")}
                    </Button>
                  </SpaceBetween>
                ) : undefined
              }
            >
              {agentMode === "impact_analysis"
                ? t("agentChat.chatHeaderImpact", { scope: scopeLabel })
                : t("agentChat.chatHeader", { scope: scopeLabel })}
            </Header>
          }
        >
          <SpaceBetween size="m">
            {/* Live region announces streaming activity to assistive tech */}
            <LiveRegion>{liveStatus}</LiveRegion>

            {/* Messages area */}
            <div style={{ minHeight: 300, overflowY: "auto", padding: "8px 0" }}>
              {messages.length === 0 && !loading && (
                <Box padding="l">
                  <SpaceBetween size="m">
                    <Box variant="h3">
                      {agentMode === "impact_analysis"
                        ? t("agentChat.emptyImpact")
                        : t("agentChat.emptyChat")}
                    </Box>
                    <Box variant="p" color="text-body-secondary">
                      {t("agentChat.startingPoint")}
                    </Box>
                    <SupportPromptGroup
                      ariaLabel={t("agentChat.suggestedPrompts")}
                      alignment="vertical"
                      items={suggestedPromptsByMode(t)[agentMode]}
                      onItemClick={({ detail }) => {
                        const item = suggestedPromptsByMode(t)[agentMode].find(
                          (p) => p.id === detail.id,
                        );
                        if (item) submit(typeof item.text === "string" ? item.text : "");
                      }}
                    />
                  </SpaceBetween>
                </Box>
              )}

              {messages.map((msg, i) => {
                const isUser = msg.role === "user";
                const isCollapsed = collapsedMessages.has(i);
                const ts = formatTimestamp(msg.timestamp);
                return (
                  <div key={i} style={{ marginBottom: 16 }}>
                    {/* Thinking trace (above the tool calls) */}
                    {!isUser && msg.reasoning && (
                      <ReasoningSection text={msg.reasoning} />
                    )}

                    {/* Tool call trace (above the assistant bubble) */}
                    {!isUser && msg.steps && msg.steps.length > 0 && (
                      <div style={{ marginBottom: 8 }}>
                        <ExpandableSection
                          headerText={t("agentChat.toolCalls", { count: msg.steps.length })}
                          variant="footer"
                        >
                          <ToolStepList steps={msg.steps} />
                        </ExpandableSection>
                      </div>
                    )}

                    <ChatBubble
                      type={isUser ? "outgoing" : "incoming"}
                      ariaLabel={t("agentChat.bubbleAria", { who: isUser ? t("agentChat.you") : "Agent Hopper", time: ts })}
                      avatar={
                        isUser ? (
                          <Avatar
                            ariaLabel={t("agentChat.you")}
                            tooltipText={userIdentity?.email || t("agentChat.you")}
                            initials={userInitials}
                          />
                        ) : (
                          <Avatar
                            color="gen-ai"
                            iconName="gen-ai"
                            ariaLabel="Agent Hopper"
                            tooltipText="Agent Hopper"
                          />
                        )
                      }
                      actions={
                        !isUser ? (
                          <SpaceBetween direction="horizontal" size="xs">
                            <CopyButton text={msg.content} />
                            <Button
                              variant="inline-icon"
                              iconName={isCollapsed ? "angle-down" : "angle-up"}
                              ariaLabel={isCollapsed ? t("agentChat.expandResponse") : t("agentChat.collapseResponse")}
                              onClick={() => {
                                setCollapsedMessages((prev) => {
                                  const next = new Set(prev);
                                  isCollapsed ? next.delete(i) : next.add(i);
                                  return next;
                                });
                              }}
                            />
                          </SpaceBetween>
                        ) : undefined
                      }
                    >
                      {isUser ? (
                        <div style={{ whiteSpace: "pre-wrap" }}>{msg.content}</div>
                      ) : isCollapsed ? (
                        <Box color="text-body-secondary">
                          {stripMarkdown(msg.content).slice(0, 160) +
                            (msg.content.length > 160 ? "…" : "")}
                        </Box>
                      ) : (
                        <AgentMarkdown text={msg.content} />
                      )}
                    </ChatBubble>
                    {!isUser && !isCollapsed && msg.tokenUsage && (
                      <div style={{ marginTop: 4, marginLeft: 4 }}>
                        <TokenUsageBadge
                          usage={msg.tokenUsage}
                          pricingUrl={msg.pricingUrl}
                        />
                      </div>
                    )}
                  </div>
                );
              })}

              {/* Live streaming bubble */}
              {loading && (
                <div style={{ marginBottom: 16 }}>
                  {streamingReasoning && (
                    <ReasoningSection text={streamingReasoning} defaultExpanded />
                  )}

                  {currentSteps.length > 0 && (
                    <div style={{ marginBottom: 8 }}>
                      <ExpandableSection
                        headerText={t("agentChat.toolCallsInProgress", { count: currentSteps.length })}
                        variant="footer"
                        defaultExpanded
                      >
                        <ToolStepList steps={currentSteps} />
                      </ExpandableSection>
                    </div>
                  )}

                  <ChatBubble
                    type="incoming"
                    ariaLabel={t("agentChat.responding")}
                    showLoadingBar={!streamingText}
                    avatar={
                      <Avatar
                        color="gen-ai"
                        iconName="gen-ai"
                        ariaLabel="Agent Hopper"
                        tooltipText="Agent Hopper"
                        loading={!streamingText}
                      />
                    }
                  >
                    {streamingText ? (
                      <SpaceBetween size="xs">
                        <AgentMarkdown text={streamingText} />
                        <LoadingBar variant="gen-ai" />
                      </SpaceBetween>
                    ) : (
                      <Box color="text-body-secondary">
                        {currentSteps.length > 0
                          ? t("agentChat.runningTool", { tool: currentSteps[currentSteps.length - 1].tool })
                          : t("agentChat.thinkingEllipsis")}
                      </Box>
                    )}
                  </ChatBubble>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>

            {error && (
              <Alert type="error" dismissible onDismiss={() => setError(null)}>
                {error}
              </Alert>
            )}

            {/* Prompt input — Cloudscape gen-AI primitive */}
            <PromptInput
              value={input}
              onChange={({ detail }) => setInput(detail.value)}
              onAction={handleSend}
              actionButtonAriaLabel={t("agentChat.send")}
              actionButtonIconName="send"
              disableActionButton={!input.trim() || loading || !canSubmit}
              disabled={loading}
              placeholder={
                agentMode === "impact_analysis"
                  ? t("agentChat.placeholderImpact")
                  : t("agentChat.placeholderChat")
              }
              minRows={2}
              maxRows={6}
            />
          </SpaceBetween>
        </Container>
      )}
    </SpaceBetween>
  );
}
