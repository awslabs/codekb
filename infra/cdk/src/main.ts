import { App } from "aws-cdk-lib";
import "source-map-support/register";
import { StorageStack } from "./stacks/storage-stack";
import { ServiceStack } from "./stacks/service-stack";
import { GuardrailStack } from "./stacks/guardrail-stack";
import { AuthStack } from "./stacks/auth-stack";
import { OidcSecretStack } from "./stacks/oidc-secret-stack";
import { WafStack } from "./stacks/waf-stack";
import { EdgeStack } from "./stacks/edge-stack";
import { CertificateStack } from "./stacks/certificate-stack";
import { getEnvConfig, CLOUDFRONT_REGION } from "./config/env";

const SOLUTION_TRACKING_ID = "uksb-fkl6zxftt3";
const stackDescription = (summary: string, tag: string): string =>
  `${summary} (${SOLUTION_TRACKING_ID})(tag:${tag}).`;

const app = new App();

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION,
};

// A deployment is identified by projectName + stage.
const projectName: string = (
  process.env.CODEKB_PROJECT || "codekb"
).toLowerCase();
const stage: string = (process.env.CODEKB_STAGE || "alpha").toLowerCase();

// A leading "aws" is stripped because it is a reserved word for some resources.
const resourcePrefix = `${projectName.replace(/^aws/, "")}-${stage}`;

// ── Auth ────────────────────────────────────────────────────────────────────
// CodeKB has a single hosted deployment path: CloudFront in front of the
// container (which serves the SPA and the API), gated by Cognito. There are two
// ways to wire in Cognito:
//
//   • Create a new pool: CODEKB_ENABLE_AUTH=true. Sign-in uses users created in
//     the pool (AdminCreateUser) and/or an optional federated identity provider.
//   • Adopt an existing pool: set CODEKB_COGNITO_USER_POOL_ID and
//     CODEKB_COGNITO_DOMAIN_URL (e.g. a production pool that already has users).
//     The existing pool, its users, its hosted-UI domain, and its federation
//     are imported and left untouched; this deployment's OWN web + mcp app
//     clients are created inside that pool (see AuthStack), so their callback
//     URLs match this deployment's host and no pre-existing client is modified.
//
// The service always runs with CODEKB_REQUIRE_AUTH=true and fails closed: with
// no Cognito wired in, every /api/* and /mcp/* request is rejected with 503.
const enableAuth = process.env.CODEKB_ENABLE_AUTH === "true";

// Space-level authorization is ON by default; turn it off per deployment with
// CODEKB_REQUIRE_AUTHORIZATION=false. It acts on a signed-in identity, so it
// only has effect once auth is active.
const requireAuthorization =
  process.env.CODEKB_REQUIRE_AUTHORIZATION !== "false";

// Adopt an existing Cognito user pool instead of creating one. Only the pool id
// and its hosted-UI domain are needed (both required); region and IdP name are
// optional. The app clients are created by AuthStack, not supplied here.
const adoptUserPoolId = process.env.CODEKB_COGNITO_USER_POOL_ID;
const adoptDomainUrl = process.env.CODEKB_COGNITO_DOMAIN_URL;
const adoptCognitoRegion = process.env.CODEKB_COGNITO_REGION;
const adoptIdpName = process.env.CODEKB_COGNITO_IDP_NAME;

const adoptCognitoParts = [adoptUserPoolId, adoptDomainUrl];
const adoptCognito = adoptCognitoParts.every((v) => !!v);
// All-or-nothing: a partial set would point the service at a half-real pool.
if (!adoptCognito && adoptCognitoParts.some((v) => !!v)) {
  throw new Error(
    "Adopting an existing Cognito pool requires BOTH CODEKB_COGNITO_USER_POOL_ID and " +
      "CODEKB_COGNITO_DOMAIN_URL (or neither, to create a new pool with CODEKB_ENABLE_AUTH=true).",
  );
}
// Creating a new pool and adopting one are mutually exclusive.
if (adoptCognito && enableAuth) {
  throw new Error(
    "CODEKB_ENABLE_AUTH=true creates a new Cognito pool, but the CODEKB_COGNITO_* adopt " +
      "variables point at an existing one. Set one or the other, not both.",
  );
}

// Create a brand-new pool only when asked and not adopting one.
const createAuth = enableAuth && !adoptCognito;
// Whether this deployment has auth wired in at all (new pool or adopted).
const authActive = createAuth || adoptCognito;

// Fail loud only when an operator EXPLICITLY asks for authorization but wires no
// Cognito pool for it to act on — a clear misconfiguration. Leaving it at the
// on-by-default with no pool is safe: auth is always required, so the service
// 503-walls every request and authorization never gets to run.
if (process.env.CODEKB_REQUIRE_AUTHORIZATION === "true" && !authActive) {
  throw new Error(
    "CODEKB_REQUIRE_AUTHORIZATION=true needs auth active (no Cognito pool is wired in).",
  );
}

// Host the SPA is served from — the hosted UI only redirects back to this. On
// the CloudFront default domain it is the distribution domain,
// {distributionId}.cloudfront.net (from the Edge stack's
// DistributionDomainName output). See docs/deployment.md
// for the deploy-then-set ordering.
const callbackDomain: string | undefined = process.env.CODEKB_CALLBACK_DOMAIN;

if (authActive && !callbackDomain) {
  throw new Error(
    "Enabling auth requires CODEKB_CALLBACK_DOMAIN=<host>, the domain the SPA is served from " +
      "(the CloudFront distribution domain {distributionId}.cloudfront.net, from the Edge " +
      "stack DistributionDomainName output, or your custom domain). " +
      "Cognito rejects any redirect_uri that is not registered against the app client.",
  );
}

// Hosted-UI domain prefixes are unique across all accounts in a region, so a
// second deployment cannot reuse another's. Scope it by account when not given.
const cognitoDomainPrefix: string =
  process.env.CODEKB_COGNITO_PREFIX ?? `${resourcePrefix}-${env.account}`;

// Optional external OIDC identity provider wired into a newly created pool (e.g.
// an enterprise OIDC/SSO provider). Supply the OIDC client id + secret via env;
// CODEKB_OIDC_ONLY=true refuses the pool's own users. Omit all for a pool whose
// own users sign in through the hosted UI. (Only applies when creating a pool;
// an adopted pool brings its own IdP config.)
const oidcClientId = process.env.CODEKB_OIDC_CLIENT_ID;
const oidcOnly = process.env.CODEKB_OIDC_ONLY === "true";
const oidcIssuer = process.env.CODEKB_OIDC_ISSUER;
const oidcProviderName = process.env.CODEKB_OIDC_PROVIDER_NAME;
let oidcAttributeMapping: Record<string, string> | undefined;
if (process.env.CODEKB_OIDC_ATTRIBUTE_MAPPING) {
  try {
    oidcAttributeMapping = JSON.parse(
      process.env.CODEKB_OIDC_ATTRIBUTE_MAPPING,
    );
  } catch {
    throw new Error(
      'CODEKB_OIDC_ATTRIBUTE_MAPPING must be valid JSON, e.g. \'{"email":"EMAIL"}\'.',
    );
  }
}

// A partial OIDC config is almost always a mistake. Fail loud here instead.
if (
  !oidcClientId &&
  (oidcOnly || oidcIssuer || oidcProviderName || oidcAttributeMapping)
) {
  throw new Error(
    "CODEKB_OIDC_* variables require CODEKB_OIDC_CLIENT_ID to be set; otherwise no federated " +
      "identity provider is configured. Set CODEKB_OIDC_CLIENT_ID (with CODEKB_OIDC_ISSUER), " +
      "or unset the other CODEKB_OIDC_* variables.",
  );
}

// Optional custom domain for the CloudFront distribution. Omit to use the
// distribution's own {distributionId}.cloudfront.net domain (HTTPS included, no
// domain to own). When set, both are required and an ACM cert is issued in
// us-east-1 with a Route 53 alias record.
const customDomainName = process.env.CODEKB_DOMAIN_NAME;
const hostedZoneName = process.env.CODEKB_HOSTED_ZONE_NAME;

// Storage bucket/index names. Storage derives them from the project name +
// account and creates the buckets. A deployment that manages storage elsewhere
// skips the Storage stack and overrides these to point at existing buckets;
// Service references them by name, so it has no CloudFormation link to Storage.
const kbBucketNameOverride = process.env.CODEKB_S3_BUCKET;
const vectorBucketNameOverride = process.env.CODEKB_S3_VECTORS_BUCKET;
const vectorIndexNameOverride = process.env.CODEKB_S3_VECTORS_INDEX;

if (createAuth && cognitoDomainPrefix.includes("undefined")) {
  throw new Error(
    `Refusing to deploy with Cognito domain prefix "${cognitoDomainPrefix}". ` +
      "CDK_DEFAULT_ACCOUNT is unset — export it, or set CODEKB_COGNITO_PREFIX=<prefix>.",
  );
}

// Logging knobs — toggle verbose backend logs without editing code.
//   CODEKB_LOG_LEVEL=DEBUG CODEKB_LOG_PAYLOAD_MAX=50000 ./scripts/deploy.sh
const logLevel = process.env.CODEKB_LOG_LEVEL;
const logPayloadMaxCtx = process.env.CODEKB_LOG_PAYLOAD_MAX;
const logPayloadMax =
  logPayloadMaxCtx !== undefined ? Number(logPayloadMaxCtx) : undefined;

// Per-workload LLM settings, passed to the container when set in the deploy
// shell, e.g. CODEKB_LLM_THINKING=between_tools ./scripts/deploy.sh. Values go
// through unchanged; the backend validates them at startup. Names mirror
// backend/src/core/llm_settings.py (Workload and its env fields).
const LLM_WORKLOADS = ["SUMMARIZATION", "BUSINESS_CONTEXT", "OVERVIEW", "CHAT"];
const LLM_WORKLOAD_FIELDS = ["MODEL_ID", "MAX_TOKENS", "THINKING", "EFFORT"];
const llmEnvVarNames = [
  "CODEKB_LLM_THINKING",
  "CODEKB_LLM_EFFORT",
  "CODEKB_LLM_STARTUP_CHECK",
  ...LLM_WORKLOADS.flatMap((workload) =>
    LLM_WORKLOAD_FIELDS.map((field) => `CODEKB_${workload}_${field}`),
  ),
];
const llmEnvironment: Record<string, string> = Object.fromEntries(
  llmEnvVarNames.flatMap((name) => {
    const value = process.env[name];
    return value !== undefined && value !== "" ? [[name, value]] : [];
  }),
);

const stageConfig = getEnvConfig(stage);

// Git host allowlist
const allowedGitHosts = stageConfig.allowedGitHosts;

// Storage: S3 buckets for KB JSON + vectors
const storage = new StorageStack(app, `${resourcePrefix}-storage`, {
  env,
  resourcePrefix,
  kbBucketVersionRetentionDays: stageConfig.kbBucketVersionRetentionDays,
  description: stackDescription(
    "CodeKB storage stack: S3 buckets for knowledge base JSON and vectors",
    "storage",
  ),
});

// Dev-only opt-in: register http://localhost:5173 on the Cognito app clients so
// the SPA can run via `vite` against this pool. Explicit, never implied by the
// stage name, so a production deploy can't accidentally allow localhost redirects.
const allowLocalhostRedirects =
  process.env.CODEKB_ALLOW_LOCALHOST_REDIRECTS === "true";

// External OIDC client secret: CodeKB owns the Secrets Manager secret so its
// lifecycle is managed with the deployment, rather than depending on an
// operator-provisioned ARN. Only applies when creating a pool with OIDC
// federation (an adopted pool brings its own IdP). The operator populates the
// value out of band — scripts/deploy.sh deploys this stack first and pauses for it
// — and the AuthStack's IdP reads it at deploy time by this same deterministic
// name via a dynamic reference. The name is the only coupling between the two
// stacks; there is no cross-stack CloudFormation link (same by-name decoupling
// Storage/Service use).
const oidcEnabled = createAuth && !!oidcClientId;
const oidcSecretName = `${resourcePrefix}-oidc-client-secret`;
if (oidcEnabled) {
  new OidcSecretStack(app, `${resourcePrefix}-auth-secret`, {
    env,
    resourcePrefix,
    secretName: oidcSecretName,
    description: stackDescription(
      "CodeKB auth secret stack: Secrets Manager secret for the external OIDC client secret",
      "auth-secret",
    ),
  });
}

// Auth: deploy whenever auth is active — creating a new pool, or adopting an
// existing one. Either way this stack OWNS the web + mcp app clients (created in
// a new pool, or added to the adopted pool), so the Service always reads the
// client ids from here. The SPA is served at the CloudFront root, so callbacks
// register at the root (no base path).
const auth = authActive
  ? new AuthStack(app, `${resourcePrefix}-auth`, {
      env,
      resourcePrefix,
      allowLocalhostRedirects,
      callbackDomain: callbackDomain!,
      description: stackDescription(
        "CodeKB auth stack: Cognito pool (created or adopted) and app clients",
        "auth",
      ),
      ...(adoptCognito
        ? {
            existingUserPoolId: adoptUserPoolId!,
            existingDomainUrl: adoptDomainUrl!,
            ...(adoptIdpName && { existingIdpName: adoptIdpName }),
          }
        : {
            domainPrefix: cognitoDomainPrefix,
            ...(oidcClientId && {
              oidcClientId,
              oidcClientSecretName: oidcSecretName,
              oidcOnly,
              oidcIssuer,
              oidcProviderName,
              oidcAttributeMapping,
            }),
          }),
    })
  : undefined;

// Audience validation: accept tokens from both the web UI and MCP app clients.
// Both client ids come from the Auth stack, in create and adopt modes alike.
const cognitoAppClientIds = auth
  ? [auth.userPoolClientId, auth.mcpClientId].filter(Boolean).join(",")
  : undefined;

// SPA build args — inlined into the frontend bundle by the image's Node build
// stage (see Dockerfile). The web app client id is only known after the Auth
// stack deploys, so scripts/deploy.sh reads it (and the domain / IdP) from the Auth
// outputs and re-exports them as CODEKB_SPA_* for the Service deploy. These are
// script-populated plumbing vars, NOT user inputs. On the first (Storage/Auth)
// synth they are unset, so the Service's throwaway image hash is just auth-less;
// the Service phase re-synths with them present and builds the real UI.
const spaBuildArgs: Record<string, string> = {
  VITE_BASE: "/",
  VITE_AUTH_ENABLED: authActive ? "true" : "false",
};
if (process.env.CODEKB_SPA_COGNITO_DOMAIN) {
  spaBuildArgs.VITE_COGNITO_DOMAIN = process.env.CODEKB_SPA_COGNITO_DOMAIN;
}
if (process.env.CODEKB_SPA_WEB_CLIENT_ID) {
  spaBuildArgs.VITE_COGNITO_CLIENT_ID = process.env.CODEKB_SPA_WEB_CLIENT_ID;
}
if (process.env.CODEKB_SPA_IDP_NAME) {
  spaBuildArgs.VITE_COGNITO_IDP_NAME = process.env.CODEKB_SPA_IDP_NAME;
}

// Region for Bedrock calls and the guardrail; may differ from the app region.
// Override with CODEKB_BEDROCK_REGION — scripts/deploy.sh exports the same value so
// shell and app agree.
const bedrockRegion =
  process.env.CODEKB_BEDROCK_REGION || stageConfig.bedrock.region;

// Secret/PII guardrail on by default; disable only with an explicit "false"
// (same variable the backend reads). Off skips the guardrail stack, the task's
// guardrail env, and the IAM grant + Deny — generation then runs unredacted.
const guardrailEnabled =
  process.env.CODEKB_BEDROCK_GUARDRAIL_ENABLED !== "false";

// Own stack so the guardrail can sit in the Bedrock region while the service
// lives elsewhere (a guardrail only works for same-region InvokeModel calls).
// The service gets its ARN/version from these outputs via scripts/deploy.sh, so the
// stacks share no cross-region CloudFormation reference. Skipped when disabled.
if (guardrailEnabled) {
  new GuardrailStack(app, `${resourcePrefix}-guardrail`, {
    env: { account: env.account, region: bedrockRegion },
    resourcePrefix,
    description: stackDescription(
      "CodeKB guardrail stack: Bedrock secret/PII guardrail in the Bedrock region",
      "guardrail",
    ),
  });
}

// Service: ECS Fargate running FastAPI (API + analysis worker), serving the
// baked-in SPA. Fronted by CloudFront (below).
const service = new ServiceStack(app, `${resourcePrefix}-service`, {
  env,
  resourcePrefix,
  description: stackDescription(
    "CodeKB service stack: ECS Fargate running the FastAPI API and analysis worker",
    "service",
  ),
  kbBucketName: kbBucketNameOverride ?? storage.kbBucketName,
  vectorBucketName: vectorBucketNameOverride ?? storage.vectorBucketName,
  vectorIndexName: vectorIndexNameOverride ?? storage.vectorIndexName,
  // Shared default model for every workload; override per deploy with
  // CODEKB_BEDROCK_MODEL_ID, like CODEKB_BEDROCK_REGION above.
  bedrockModelId:
    process.env.CODEKB_BEDROCK_MODEL_ID || stageConfig.bedrock.modelId,
  bedrockRegion,
  bedrockMaxTokens: stageConfig.bedrock.maxTokens,
  bedrockReadTimeout: stageConfig.bedrock.readTimeout,
  guardrailEnabled,
  // From the GuardrailStack outputs, re-exported by scripts/deploy.sh before the
  // service deploy (same deploy→read→pass flow as the Cognito/CloudFront values).
  // Empty on the first synth, which never deploys the service; unused when the
  // guardrail is disabled.
  guardrailArn: process.env.CODEKB_BEDROCK_GUARDRAIL_ARN ?? "",
  guardrailVersion: process.env.CODEKB_BEDROCK_GUARDRAIL_VERSION ?? "DRAFT",
  cognitoUserPoolId: auth?.userPoolId,
  cognitoDomainUrl: auth?.cognitoDomainUrl,
  cognitoMcpClientId: auth?.mcpClientId,
  cognitoIdpName: auth?.idpName,
  cognitoAppClientIds,
  requireAuthorization,
  spaBuildArgs,
  // Region of an adopted pool when it lives outside the stack region.
  ...(adoptCognitoRegion && { cognitoRegion: adoptCognitoRegion }),
  // Public base the backend advertises in its OAuth metadata (/.well-known/…),
  // which MCP clients follow to start the authorize flow. Served at the
  // CloudFront root, so the metadata URLs have no stage path.
  ...(callbackDomain && { publicDomain: callbackDomain }),
  allowedGitHosts,
  ...(logLevel !== undefined && { logLevel }),
  ...(logPayloadMax !== undefined &&
    !Number.isNaN(logPayloadMax) && { logPayloadMax }),
  llmEnvironment,
  summarizationConcurrency: stageConfig.summarizationConcurrency,
  containerCount: stageConfig.containerCount,
  otelLogsExporter: stageConfig.otel.logsExporter,
  otelProtocol: stageConfig.otel.protocol,
  otelServiceName: stageConfig.otel.serviceName,
  otelCloudWatchLogStreamPrefix: stageConfig.otel.cloudWatchLogStreamPrefix,
  otelCloudWatchLogStreamRotationMinutes:
    stageConfig.otel.cloudWatchLogStreamRotationMinutes,
});

// WAF: CloudFront WebACL — must live in us-east-1 regardless of the app region.
// Its ARN is imported by the Edge stack via crossRegionReferences.
const waf = new WafStack(app, `${resourcePrefix}-waf`, {
  env: { account: env.account, region: CLOUDFRONT_REGION },
  resourcePrefix,
  crossRegionReferences: true,
  description: stackDescription(
    "CodeKB WAF stack: CloudFront WebACL in us-east-1",
    "waf",
  ),
});

// Certificate for a custom CloudFront domain. CloudFront requires the ACM cert
// in us-east-1, so it lives in a dedicated us-east-1 stack and the Edge stack
// imports its ARN via crossRegionReferences. Only created when a custom domain
// is configured; the default {dist}.cloudfront.net domain needs no cert.
const certificate =
  customDomainName && hostedZoneName
    ? new CertificateStack(app, `${resourcePrefix}-certificate`, {
        env: { account: env.account, region: CLOUDFRONT_REGION },
        resourcePrefix,
        domainName: customDomainName,
        hostedZoneName,
        crossRegionReferences: true,
        description: stackDescription(
          "CodeKB certificate stack: ACM cert for the CloudFront custom domain (us-east-1)",
          "certificate",
        ),
      })
    : undefined;

// Web + API front door: CloudFront → VPC origin → internal ALB → ECS, with the
// container serving both the SPA and the API/MCP/OAuth endpoints. Served at the
// origin root (no stage path), which is what OAuth/RFC 8414 discovery needs so
// MCP sign-in works. The SPA is baked into the container image (VITE_COGNITO_*
// and base "/" inlined at build time — see scripts/deploy.sh),
// not injected by CDK.
new EdgeStack(app, `${resourcePrefix}-edge`, {
  env,
  resourcePrefix,
  loadBalancer: service.loadBalancer,
  wafWebAclArn: waf.webAclArn,
  crossRegionReferences: true,
  description: stackDescription(
    "CodeKB edge stack: CloudFront distribution fronting the ALB and serving the SPA",
    "edge",
  ),
  ...(customDomainName &&
    hostedZoneName && {
      domainName: customDomainName,
      hostedZoneName,
      certificateArn: certificate!.certificateArn,
    }),
});
