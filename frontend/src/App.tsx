import { useEffect, useState, useRef } from "react";
import { useTranslation } from "react-i18next";
import { BrowserRouter, Routes, Route, useNavigate, useLocation } from "react-router-dom";
import TopNavigation from "@cloudscape-design/components/top-navigation";
import AppLayout from "@cloudscape-design/components/app-layout";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Icon from "@cloudscape-design/components/icon";
import SpaceList from "./pages/SpaceList";
import CreateSpace from "./pages/CreateSpace";
import SpaceDetail from "./pages/SpaceDetail";
import Overview from "./pages/Overview";
import Settings from "./pages/Settings";
import GitCredentials from "./pages/GitCredentials";
import GlobalSearch from "./pages/GlobalSearch";
import AgentChat from "./pages/AgentChat";
import Hyperspaces from "./pages/Hyperspaces";
import HyperspaceDetail from "./pages/HyperspaceDetail";
import CreateHyperspace from "./pages/CreateHyperspace";
import Documentation from "./pages/Documentation";
import { buildLanguageSwitcherUtility } from "./components/LanguageSwitcher";
import {
  isAuthEnabled,
  isAuthenticated,
  getUser,
  login,
  logout,
  handleCallback,
} from "./auth";

/** Handles the /auth/callback route — exchanges code for tokens then redirects to /. */
function AuthCallback() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const handled = useRef(false);

  useEffect(() => {
    if (handled.current) return;
    handled.current = true;

    const params = new URLSearchParams(window.location.search);
    const errorDesc = params.get("error_description") || params.get("error");
    if (errorDesc) {
      console.error("Auth callback error:", errorDesc);
      setError(errorDesc);
      sessionStorage.setItem("auth_error", "true");
      return;
    }

    handleCallback()
      .then((success) => {
        if (success) {
          sessionStorage.removeItem("auth_error");
          navigate("/", { replace: true });
        } else {
          setError(t("auth.exchangeFailed"));
          sessionStorage.setItem("auth_error", "true");
        }
      })
      .catch((e) => {
        console.error("Auth callback exchange threw:", e);
        setError(`${t("auth.signInFailed")}: ${e instanceof Error ? e.message : String(e)}`);
        sessionStorage.setItem("auth_error", "true");
      });
  }, [navigate, t]);

  if (error) {
    return (
      <div style={{ padding: 40 }}>
        <h2>{t("auth.signInFailed")}</h2>
        <p style={{ color: "#d13212" }}>{error}</p>
        <p style={{ marginTop: 16 }}>
          <button onClick={() => { sessionStorage.removeItem("auth_error"); login(); }}>
            {t("auth.tryAgain")}
          </button>
        </p>
      </div>
    );
  }

  return <div style={{ padding: 40 }}>{t("auth.signingIn")}</div>;
}

/**
 * Auth gate — renders children only if authenticated (or auth is disabled).
 * If not authenticated and no prior error, redirects to login ONCE.
 * If there was an auth error, shows the error instead of looping.
 */
function AuthGate({ children }: { children: React.ReactNode }) {
  const { t } = useTranslation();
  const loginAttempted = useRef(false);

  // Auth not configured — render app normally
  if (!isAuthEnabled()) return <>{children}</>;

  // Already authenticated — render app
  if (isAuthenticated()) return <>{children}</>;

  // On the callback route — let AuthCallback handle it.
  if (window.location.pathname.replace(/\/+$/, "").endsWith("/auth/callback")) return <>{children}</>;

  // There was a previous auth error — show it, don't loop
  const authError = sessionStorage.getItem("auth_error");
  if (authError) {
    return (
      <div style={{ padding: 40 }}>
        <h2>{t("auth.authRequired")}</h2>
        <p>{t("auth.previousAttemptFailed")}</p>
        <p style={{ marginTop: 16 }}>
          <button onClick={() => { sessionStorage.removeItem("auth_error"); login(); }}>
            {t("auth.signIn")}
          </button>
        </p>
      </div>
    );
  }

  // First attempt — redirect to login (only once)
  if (!loginAttempted.current) {
    loginAttempted.current = true;
    login();
  }

  return <div style={{ padding: 40 }}>{t("auth.redirecting")}</div>;
}

function Shell() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const [user, setUser] = useState(getUser());

  useEffect(() => {
    setUser(getUser());
  }, [location]);

  const userUtility = isAuthEnabled() && user
    ? {
        type: "button" as const,
        text: user.email || t("nav.user"),
        iconName: "user-profile" as const,
      }
    : { type: "button" as const, text: "", iconName: "user-profile" as const };

  const utilities = [buildLanguageSwitcherUtility(t, i18n), userUtility];

  // Tiny branded logo as inline SVG data URI (sparkle icon matching gen-ai)
  const logoSvg = `data:image/svg+xml,${encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">' +
    '<rect width="24" height="24" rx="5" fill="%230972d3"/>' +
    '<path d="M12 4l1.2 3.6L17 9l-3.8 1.4L12 14l-1.2-3.6L7 9l3.8-1.4z" fill="white"/>' +
    '<path d="M17 14l.7 2.1 2.3.9-2.3.9L17 20l-.7-2.1L14 17l2.3-.9z" fill="white" opacity=".7"/>' +
    '</svg>'
  )}`;

  return (
    <>
      <TopNavigation
        identity={{
          href: "/",
          title: t("nav.appTitle"),
          logo: { src: logoSvg, alt: t("nav.logoAlt") },
        }}
        i18nStrings={{ overflowMenuTriggerText: t("nav.overflowMenuTrigger"), overflowMenuTitleText: t("nav.overflowMenuTitle") }}
        utilities={utilities}
      />
      <AppLayout
        toolsHide
        navigation={
          <SideNavigation
            header={{ text: t("nav.header"), href: "/" }}
            activeHref={location.pathname}
            onFollow={(e) => {
              e.preventDefault();
              navigate(e.detail.href);
            }}
            items={[
              { type: "link", text: t("nav.overview"), href: "/" },
              { type: "link", text: t("nav.spaces"), href: "/spaces" },
              { type: "link", text: t("nav.hyperspaces"), href: "/hyperspaces" },
              { type: "divider" },
              { type: "link", text: t("nav.globalSearch"), href: "/search" },
              { type: "divider" },
              { type: "link", text: t("nav.agentHopper"), href: "/agent", info: <Icon name="gen-ai" /> },
              { type: "divider" },
              { type: "link", text: t("components.navGitCredentials"), href: "/git-creds" },
              { type: "link", text: t("nav.settings"), href: "/settings" },
              { type: "divider" },
              { type: "link", text: t("nav.userGuide"), href: "/docs", info: <Icon name="status-info" /> },
            ]}
          />
        }
        content={
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/spaces" element={<SpaceList />} />
            <Route path="/create" element={<CreateSpace />} />
            <Route path="/search" element={<GlobalSearch />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/git-creds" element={<GitCredentials />} />
            <Route path="/spaces/:spaceId" element={<SpaceDetail />} />
            <Route path="/agent" element={<AgentChat />} />
            <Route path="/hyperspaces" element={<Hyperspaces />} />
            <Route path="/hyperspaces/new" element={<CreateHyperspace />} />
            <Route path="/hyperspaces/:hyperspaceId" element={<HyperspaceDetail />} />
            <Route path="/docs" element={<Documentation />} />
            <Route path="/auth/callback" element={<AuthCallback />} />
          </Routes>
        }
      />
    </>
  );
}

export default function App() {
  const basename = import.meta.env.BASE_URL.replace(/\/+$/, '') || '/';
  return (
    <BrowserRouter basename={basename}>
      <AuthGate>
        <Shell />
      </AuthGate>
    </BrowserRouter>
  );
}
