import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Alert from "@cloudscape-design/components/alert";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Spinner from "@cloudscape-design/components/spinner";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import { api, ApiError, GitCredential } from "../api/client";

// Display metadata per provider id. Which providers appear is driven entirely
// by the server (config.git_credential_providers); this only supplies the label
// and the minimum token scopes. Unknown ids fall back sensibly.
const PROVIDER_META: Record<string, { label: string; scopes: string }> = {
  github: {
    label: "GitHub",
    scopes: "gitCredentials.scopesGithub",
  },
  gitlab: {
    label: "GitLab",
    scopes: "gitCredentials.scopesGitlab",
  },
  bitbucket: {
    label: "Bitbucket",
    scopes: "gitCredentials.scopesBitbucket",
  },
};

const metaFor = (id: string) =>
  PROVIDER_META[id] ?? { label: id, scopes: "" };

interface Draft {
  username: string;
  token: string;
}
const emptyDraft: Draft = { username: "", token: "" };

export default function GitCredentials() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [loading, setLoading] = useState(true);
  const [providers, setProviders] = useState<string[]>([]);
  const [connected, setConnected] = useState<Record<string, GitCredential>>({});
  const [editing, setEditing] = useState<Record<string, boolean>>({});
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [messages, setMessages] = useState<Record<string, { type: "success" | "error"; text: string }>>({});
  // Live verification result per provider from "Check status". Not populated on
  // load — landing only reads what's stored, no provider calls.
  const [verify, setVerify] = useState<Record<string, "ok" | "bad" | "expired">>({});
  const [checking, setChecking] = useState(false);

  const load = () => {
    setLoading(true);
    setVerify({});
    Promise.all([api.getConfig(), api.listGitCredentials()])
      .then(([cfg, list]) => {
        setProviders(cfg.git_credential_providers ?? []);
        const byProvider: Record<string, GitCredential> = {};
        for (const c of list) byProvider[c.provider] = c;
        setConnected(byProvider);
      })
      .catch(() => {
        setProviders([]);
        setConnected({});
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  // Verify every saved connection against its provider in parallel. On-demand
  // only — this is the sole path that spends provider API calls.
  const checkAll = async () => {
    const ids = Object.keys(connected);
    if (ids.length === 0) return;
    setChecking(true);
    const results = await Promise.allSettled(ids.map((id) => api.testGitCredential(id)));
    const nextVerify: Record<string, "ok" | "bad" | "expired"> = {};
    const nextMessages = { ...messages };
    ids.forEach((id, i) => {
      const r = results[i];
      if (r.status === "fulfilled") {
        nextVerify[id] = "ok";
        nextMessages[id] = { type: "success", text: t("gitCredentials.verifiedAs", { username: r.value.username }) };
      } else {
        // A rejected token (git_credentials_expired / 401) is Expired, not just
        // unverified, so the card can prompt a reconnect.
        const reason = r.reason;
        const expired = reason instanceof ApiError && reason.isExpiredCredentials;
        nextVerify[id] = expired ? "expired" : "bad";
        nextMessages[id] = {
          type: "error",
          text: reason instanceof Error ? reason.message : t("gitCredentials.verificationFailed"),
        };
      }
    });
    setVerify(nextVerify);
    setMessages(nextMessages);
    // Open the update form for any expired provider so the fix is one step away.
    setEditing((e) => {
      const next = { ...e };
      for (const id of ids) if (nextVerify[id] === "expired") next[id] = true;
      return next;
    });
    setChecking(false);
  };

  const draftFor = (id: string): Draft => drafts[id] ?? emptyDraft;
  const setDraft = (id: string, patch: Partial<Draft>) =>
    setDrafts((d) => ({ ...d, [id]: { ...draftFor(id), ...patch } }));
  const setMessage = (id: string, type: "success" | "error", text: string) =>
    setMessages((m) => ({ ...m, [id]: { type, text } }));
  const clearMessage = (id: string) =>
    setMessages((m) => { const next = { ...m }; delete next[id]; return next; });

  const startEdit = (id: string) => {
    setDraft(id, { ...emptyDraft, username: connected[id]?.username ?? "" });
    setEditing((e) => ({ ...e, [id]: true }));
    clearMessage(id);
  };
  const cancelEdit = (id: string) => {
    setEditing((e) => ({ ...e, [id]: false }));
    setDrafts((d) => ({ ...d, [id]: emptyDraft }));
    clearMessage(id);
  };

  const save = async (id: string) => {
    const draft = draftFor(id);
    if (!draft.username.trim() || !draft.token.trim()) {
      setMessage(id, "error", t("gitCredentials.enterBoth"));
      return;
    }
    setBusy(id);
    clearMessage(id);
    try {
      const res = await api.saveGitCredential(id, {
        username: draft.username.trim(),
        token: draft.token,
      });
      setMessage(id, "success", t("gitCredentials.savedAs", { username: res.username }));
      setEditing((e) => ({ ...e, [id]: false }));
      setDrafts((d) => ({ ...d, [id]: emptyDraft }));
      // Which private spaces are listed depends on this token.
      queryClient.invalidateQueries({ queryKey: ["spaces"] });
      load();
    } catch (e) {
      setMessage(id, "error", e instanceof Error ? e.message : t("gitCredentials.saveFailed"));
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async (id: string) => {
    setBusy(id);
    clearMessage(id);
    try {
      await api.deleteGitCredential(id);
      setEditing((e) => ({ ...e, [id]: false }));
      queryClient.invalidateQueries({ queryKey: ["spaces"] });
      load();
    } catch (e) {
      setMessage(id, "error", e instanceof Error ? e.message : t("gitCredentials.deleteFailed"));
    } finally {
      setBusy(null);
    }
  };

  // The connect/update form body, shared by the "new connection" container and
  // the expanded body of a connected provider. `isExisting` adds a Cancel action
  // and changes the token placeholder.
  const connectForm = (id: string, isExisting: boolean) => {
    const meta = metaFor(id);
    const isBusy = busy === id;
    const msg = messages[id];
    return (
      <SpaceBetween size="m">
        <FormField label={t("gitCredentials.username")}>
          <Input
            value={draftFor(id).username}
            onChange={(e) => setDraft(id, { username: e.detail.value })}
            placeholder={t("gitCredentials.usernamePlaceholder", { provider: meta.label })}
            disabled={isBusy}
          />
        </FormField>
        <FormField
          label={t("gitCredentials.token")}
          description={t("gitCredentials.tokenDescription")}
          constraintText={meta.scopes ? t("gitCredentials.minimumScopes", { scopes: t(meta.scopes) }) : undefined}
        >
          <Input
            type="password"
            value={draftFor(id).token}
            onChange={(e) => setDraft(id, { token: e.detail.value })}
            placeholder={isExisting ? t("gitCredentials.tokenPlaceholderExisting") : t("gitCredentials.tokenPlaceholderNew")}
            disabled={isBusy}
          />
        </FormField>
        <Box float="right">
          <SpaceBetween size="xs" direction="horizontal">
            {isExisting && (
              <Button variant="link" onClick={() => cancelEdit(id)} disabled={isBusy}>
                {t("common.cancel")}
              </Button>
            )}
            <Button variant="primary" onClick={() => save(id)} loading={isBusy}>
              {t("gitCredentials.save")}
            </Button>
          </SpaceBetween>
        </Box>
        {msg && (
          <StatusIndicator type={msg.type === "success" ? "success" : "error"}>
            {msg.text}
          </StatusIndicator>
        )}
      </SpaceBetween>
    );
  };

  const renderProvider = (id: string) => {
    const meta = metaFor(id);
    const conn = connected[id];
    const isEditing = !!editing[id];
    const isBusy = busy === id;

    // Connected → a box whose header row is a flex with the title/status block
    // on the left and the actions vertically centered against the full block on
    // the right. Update reveals the edit form; the body only renders when editing.
    if (conn) {
      return (
        <Container
          key={id}
          disableContentPaddings={!isEditing}
          header={
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "12px", padding: "8px 0" }}>
              <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                <div>
                  <Box fontSize="heading-m" fontWeight="bold" display="inline">
                    {meta.label}
                  </Box>{" "}
                  <Box
                    color={verify[id] && verify[id] !== "ok" ? "text-status-error" : "text-status-success"}
                    fontWeight="bold"
                    display="inline"
                  >
                    {verify[id] === "expired"
                      ? t("gitCredentials.expired")
                      : verify[id] === "bad"
                        ? t("gitCredentials.unverified")
                        : t("gitCredentials.verified")}
                  </Box>
                </div>
                {conn.username && (
                  <Box color="text-body-secondary">{conn.username}</Box>
                )}
              </div>
              <SpaceBetween size="xs" direction="horizontal">
                {!isEditing && (
                  <Button onClick={() => startEdit(id)} disabled={isBusy}>{t("gitCredentials.update")}</Button>
                )}
                <Button onClick={() => disconnect(id)} loading={isBusy}>{t("gitCredentials.delete")}</Button>
              </SpaceBetween>
            </div>
          }
        >
          {isEditing ? connectForm(id, true) : null}
        </Container>
      );
    }

    // Not connected → a plain box with the connect form.
    return (
      <Container key={id} header={<Header variant="h3">{meta.label}</Header>}>
        {connectForm(id, false)}
      </Container>
    );
  };

  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup
        items={[{ text: t("common.breadcrumbRoot"), href: "/" }, { text: t("components.navGitCredentials"), href: "/git-creds" }]}
        onFollow={(e) => { e.preventDefault(); navigate(e.detail.href); }}
      />

      <Header
        variant="h1"
        actions={
          Object.keys(connected).length > 0 ? (
            <Button iconName="refresh" onClick={checkAll} loading={checking}>
              {t("gitCredentials.checkStatus")}
            </Button>
          ) : undefined
        }
        description={t("gitCredentials.description")}
      >
        {t("components.navGitCredentials")}
      </Header>

      {loading ? (
        <Box textAlign="center" padding="xxl"><Spinner size="large" /></Box>
      ) : providers.length === 0 ? (
        <Alert type="info" header={t("gitCredentials.notEnabledHeader")}>
          {t("gitCredentials.notEnabledBody")}
        </Alert>
      ) : (
        <SpaceBetween size="l">{providers.map(renderProvider)}</SpaceBetween>
      )}
    </SpaceBetween>
  );
}
