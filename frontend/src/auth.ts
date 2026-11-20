/**
 * Lightweight Cognito OAuth 2.0 auth module.
 *
 * Uses authorization code grant with PKCE — no Amplify dependency needed.
 * Config comes from Vite env vars injected at build time:
 *   VITE_AUTH_ENABLED      – 'true' to turn on the hosted-UI login flow
 *   VITE_COGNITO_DOMAIN    – Cognito hosted-UI domain, e.g. https://<prefix>.auth.<region>.amazoncognito.com
 *   VITE_COGNITO_CLIENT_ID – Cognito app client ID
 *   VITE_COGNITO_REDIRECT  – callback URL (defaults to window.location.origin + /auth/callback)
 *   VITE_COGNITO_IDP_NAME  – optional federated IdP hint; unset shows the Cognito IdP picker
 *
 * These are per-deployment values baked into the bundle. Set them in .env.production.local 
 * (gitignored) rather than in .env.production.
 */

const COGNITO_DOMAIN = import.meta.env.VITE_COGNITO_DOMAIN as string | undefined;
const CLIENT_ID = import.meta.env.VITE_COGNITO_CLIENT_ID as string | undefined;

// The app's base path, without a trailing slash. Vite's BASE_URL is the build's
// --base value — "" for the CloudFront deployment (served at the origin root),
// or "/<prefix>" if the container is fronted under a path prefix. Cognito matches
// redirect/logout URLs exactly, so this must equal what the Auth stack registers
// (see callbackBasePath in auth-stack.ts).
const BASE_PATH = import.meta.env.BASE_URL.replace(/\/+$/, ''); // "" at the root
const REDIRECT_URI =
  (import.meta.env.VITE_COGNITO_REDIRECT as string) ||
  `${window.location.origin}${BASE_PATH}/auth/callback`;
// Optional federated IdP name for the Cognito hosted-UI `identity_provider`
// hint (build-time). When unset, no hint is sent and Cognito presents its IdP
// picker. A deployment that federates to a specific IdP sets VITE_COGNITO_IDP_NAME.
const IDP_NAME = import.meta.env.VITE_COGNITO_IDP_NAME as string | undefined;

/** Whether auth is configured and enabled.
 *  Requires the Cognito env vars AND VITE_AUTH_ENABLED=true, so a build that
 *  merely inherited a pool URL never starts redirecting people to a login. */
export const isAuthEnabled = (): boolean =>
  !!COGNITO_DOMAIN && !!CLIENT_ID && import.meta.env.VITE_AUTH_ENABLED === 'true';

// ── PKCE helpers ──────────────────────────────────────────────────────

function generateRandomString(length: number): string {
  const array = new Uint8Array(length);
  crypto.getRandomValues(array);
  return Array.from(array, (b) => b.toString(16).padStart(2, '0')).join('');
}

async function sha256(plain: string): Promise<ArrayBuffer> {
  return crypto.subtle.digest('SHA-256', new TextEncoder().encode(plain));
}

function base64UrlEncode(buffer: ArrayBuffer): string {
  return btoa(String.fromCharCode(...new Uint8Array(buffer)))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
}

// ── Token storage ─────────────────────────────────────────────────────

interface TokenSet {
  id_token: string;
  access_token: string;
  expires_at: number; // epoch ms
}

const TOKEN_KEY = 'codekb_tokens';

function getStoredTokens(): TokenSet | null {
  try {
    const raw = sessionStorage.getItem(TOKEN_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function storeTokens(tokens: TokenSet): void {
  sessionStorage.setItem(TOKEN_KEY, JSON.stringify(tokens));
}

export function clearTokens(): void {
  sessionStorage.removeItem(TOKEN_KEY);
}

// ── Public API ────────────────────────────────────────────────────────

/** Redirect the browser to the Cognito hosted UI to begin the OAuth flow. */
export async function login(): Promise<void> {
  if (!COGNITO_DOMAIN || !CLIENT_ID) return;

  const verifier = generateRandomString(64);
  const challenge = base64UrlEncode(await sha256(verifier));
  sessionStorage.setItem('pkce_verifier', verifier);

  const params = new URLSearchParams({
    response_type: 'code',
    client_id: CLIENT_ID,
    redirect_uri: REDIRECT_URI,
    scope: 'email openid profile',
    code_challenge: challenge,
    code_challenge_method: 'S256',
  });
  if (IDP_NAME) params.set('identity_provider', IDP_NAME);

  window.location.href = `${COGNITO_DOMAIN}/oauth2/authorize?${params}`;
}

/** Exchange the authorization code (from callback URL) for tokens. */
export async function handleCallback(): Promise<boolean> {
  if (!COGNITO_DOMAIN || !CLIENT_ID) return false;

  const params = new URLSearchParams(window.location.search);
  const code = params.get('code');
  const verifier = sessionStorage.getItem('pkce_verifier');
  if (!code || !verifier) return false;

  const res = await fetch(`${COGNITO_DOMAIN}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      client_id: CLIENT_ID,
      redirect_uri: REDIRECT_URI,
      code,
      code_verifier: verifier,
    }),
  });

  sessionStorage.removeItem('pkce_verifier');

  if (!res.ok) return false;

  const data = await res.json();
  storeTokens({
    id_token: data.id_token,
    access_token: data.access_token,
    expires_at: Date.now() + data.expires_in * 1000,
  });

  return true;
}

/** Get a valid token for API calls, or null if not authenticated.
 *
 * Returns the ID token (which contains user claims like email, name)
 * rather than the access token, so the backend can extract user identity.
 * Falls back to access_token if id_token is missing for any reason.
 */
export function getAccessToken(): string | null {
  const tokens = getStoredTokens();
  if (!tokens) return null;
  if (Date.now() > tokens.expires_at) {
    clearTokens();
    return null;
  }
  return tokens.id_token || tokens.access_token;
}

/** Get the ID token (contains user claims like email). */
export function getIdToken(): string | null {
  return getStoredTokens()?.id_token ?? null;
}

/** Parse the ID token to get user info (email, name, etc.). */
export function getUser(): { email?: string; given_name?: string; family_name?: string } | null {
  const idToken = getIdToken();
  if (!idToken) return null;
  try {
    const payload = JSON.parse(atob(idToken.split('.')[1]));
    return {
      email: payload.email,
      given_name: payload.given_name,
      family_name: payload.family_name,
    };
  } catch {
    return null;
  }
}

/** Check if user is currently authenticated. */
export function isAuthenticated(): boolean {
  return !!getAccessToken();
}

/** Redirect to Cognito logout endpoint. */
export function logout(): void {
  clearTokens();
  if (!COGNITO_DOMAIN || !CLIENT_ID) {
    window.location.href = '/';
    return;
  }
  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    logout_uri: `${window.location.origin}${BASE_PATH}`,
  });
  window.location.href = `${COGNITO_DOMAIN}/logout?${params}`;
}
