#!/usr/bin/env bash
#
# Automate the hosted CodeKB deployment described in docs/deployment.md — the
# placeholder → deploy → real-host redeploy "dance" required because the
# CloudFront distribution domain isn't known until the Edge stack is
# deployed. Driven end to end so you never copy an output by hand.
#
#   1. Build the CDK app, then deploy Storage + Auth (Auth with a placeholder
#      host). Storage is skipped if you point at pre-existing buckets.
#   2. Deploy Service + Waf + Edge (still the placeholder host). The Service
#      image builds the SPA from the Auth outputs (passed as Docker build args)
#      and mints the real {dist}.cloudfront.net host.
#   3. Redeploy Auth + Service with the real host.
#
# ─────────────────────────────────────────────────────────────────────────────
# BEFORE YOU RUN (one-time setup)
#
#   1. Install the tools this script calls: npm, npx, aws (the AWS CLI), and
#      docker. Docker must be running. For finch, export CDK_DOCKER=finch.
#   2. Give the AWS CLI credentials for the account you want to deploy into —
#      for example:  export AWS_PROFILE=<your-profile>   (or run 'aws configure',
#      or use AWS SSO). You do NOT set the account number anywhere; the script
#      reads it from these credentials, prints it back to you, and deploys there.
#      Sanity-check first with:  aws sts get-caller-identity
#
# ENV VARS YOU CAN SET (all optional — defaults shown. The script echoes the
# effective value of every one of these at startup, so you can confirm the
# target before anything is deployed). Our own vars are CODEKB_*; the standard
# AWS_* / CDK_* names are read as-is.
#
#   CODEKB_PROJECT        project name / stack prefix        (default: AwsCodeKb)
#   CODEKB_STAGE          deployment instance qualifier      (default: alpha)
#   CODEKB_REGION         region to deploy into              (REQUIRED, no
#                                       default). Drives BOTH the deploy and
#                                       every stack-output lookup.
#   CODEKB_BEDROCK_REGION Region Bedrock + its guardrail run in (default:
#                                       us-east-1). The guardrail stack is
#                                       deployed here and may differ from
#                                       CODEKB_REGION. Must be a region the
#                                       configured model id is available in.
#   CODEKB_COGNITO_PREFIX Cognito hosted-UI domain prefix     (optional; CDK
#                                       derives codekb-<stage>-<account> if unset)
#
#   Adopt existing storage (skip the Storage stack — set ALL THREE or none):
#   CODEKB_S3_BUCKET, CODEKB_S3_VECTORS_BUCKET, CODEKB_S3_VECTORS_INDEX
#
#   Adopt an existing Cognito pool (set BOTH or neither). The pool, its users,
#   its hosted-UI domain, and its federation are imported and left untouched;
#   this deployment's OWN web + mcp app clients are created inside it, with
#   callback URLs for this host — so there is nothing to register by hand:
#   CODEKB_COGNITO_USER_POOL_ID, CODEKB_COGNITO_DOMAIN_URL
#   (optional: CODEKB_COGNITO_REGION, CODEKB_COGNITO_IDP_NAME).
#
#   External OIDC sign-in for a NEW pool (ignored when adopting). CODEKB_OIDC_ISSUER
#   is REQUIRED when CODEKB_OIDC_CLIENT_ID is set. The OIDC client secret is NOT an
#   env var: CodeKB creates a Secrets Manager secret (the "$PREFIX-auth-secret"
#   stack) and this script pauses on the first deploy so you can populate it.
#   CODEKB_OIDC_CLIENT_ID, CODEKB_OIDC_ISSUER,
#   CODEKB_OIDC_PROVIDER_NAME (default "OidcIdP"),
#   CODEKB_OIDC_ATTRIBUTE_MAPPING (JSON), CODEKB_OIDC_ONLY.
#   Set CODEKB_OIDC_SECRET_READY=true to skip the populate pause (automation).
#
# Usage (from anywhere; the script locates infra/cdk itself):
#     export AWS_PROFILE=<your-profile>
#     CODEKB_REGION=us-east-1 ./scripts/deploy.sh
#     CODEKB_STAGE=dev CODEKB_REGION=us-east-1 ./scripts/deploy.sh
#
# `npm run deploy` from infra/cdk runs this same script.
#
set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
PROJECT="${CODEKB_PROJECT:-AwsCodeKb}"
STAGE="${CODEKB_STAGE:-alpha}"
REGION="${CODEKB_REGION:-}"
if [ -z "$REGION" ]; then
  echo "Error: CODEKB_REGION is not set. Set it to the region to deploy into," >&2
  echo "       e.g. CODEKB_REGION=us-east-1 ./scripts/deploy.sh" >&2
  exit 1
fi
# Region Bedrock (and its guardrail) runs in. Defaults to us-east-1 because the
# default model id is a US inference profile; the guardrail stack is deployed
# here and its ARN is read from here before the Service deploy. Must match the
# app's default in infra/cdk/src/config/env.ts.
BEDROCK_REGION="${CODEKB_BEDROCK_REGION:-us-east-1}"
COGNITO_PREFIX="${CODEKB_COGNITO_PREFIX:-}"

# Lowercase stack/resource prefix — mirrors the CDK's resourcePrefix exactly
# (projectName lowercased with a leading "aws" stripped, then "-<stage>").
# Stack names are "<PREFIX>-<component>" (e.g. codekb-alpha-storage), all lowercase.
_proj_lc="$(printf '%s' "$PROJECT" | tr '[:upper:]' '[:lower:]')"
PREFIX="${_proj_lc#aws}-$(printf '%s' "$STAGE" | tr '[:upper:]' '[:lower:]')"

KB_BUCKET="${CODEKB_S3_BUCKET:-}"
VEC_BUCKET="${CODEKB_S3_VECTORS_BUCKET:-}"
VEC_INDEX="${CODEKB_S3_VECTORS_INDEX:-}"

ADOPT_USER_POOL_ID="${CODEKB_COGNITO_USER_POOL_ID:-}"
ADOPT_DOMAIN_URL="${CODEKB_COGNITO_DOMAIN_URL:-}"
ADOPT_COGNITO_REGION="${CODEKB_COGNITO_REGION:-}"
ADOPT_IDP_NAME="${CODEKB_COGNITO_IDP_NAME:-}"

OIDC_CLIENT_ID="${CODEKB_OIDC_CLIENT_ID:-}"
OIDC_ISSUER="${CODEKB_OIDC_ISSUER:-}"
OIDC_PROVIDER_NAME="${CODEKB_OIDC_PROVIDER_NAME:-}"
OIDC_ATTRIBUTE_MAPPING="${CODEKB_OIDC_ATTRIBUTE_MAPPING:-}"
OIDC_ONLY="${CODEKB_OIDC_ONLY:-}"

# Secret/PII guardrail on unless explicitly "false" (matches the app + backend).
# Off skips the guardrail stack and its output read below.
GUARDRAIL_ENABLED=true
[ "${CODEKB_BEDROCK_GUARDRAIL_ENABLED:-}" = "false" ] && GUARDRAIL_ENABLED=false

PLACEHOLDER_HOST="placeholder.invalid"

# Host the SPA is actually served from, used for the real-host redeploy below.
# Normally this is the CloudFront distribution domain, read from the Edge stack
# output. An operator can override it by exporting CODEKB_CALLBACK_DOMAIN — set
# it to a custom domain the distribution is served under (e.g. one wired up out
# of band). When unset, the CloudFront domain is used, so default behaviour is
# unchanged. Captured here before the per-phase placeholder overrides below.
CALLBACK_OVERRIDE="${CODEKB_CALLBACK_DOMAIN:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CDK_PKG="$(cd "$SCRIPT_DIR/../infra/cdk" && pwd)"
cd "$CDK_PKG"

# ── Preflight ────────────────────────────────────────────────────────────────
# CDK builds the image with $CDK_DOCKER when set (e.g. finch).
for tool in npm npx aws "${CDK_DOCKER:-docker}"; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Error: '$tool' not found on PATH." >&2; exit 1; }
done

# Resolve the AWS account from the active credentials — you never set it by hand.
# This is also what CDK embeds in derived names, so failing fast here beats a
# confusing deploy error later.
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
if [ -z "$ACCOUNT_ID" ] || [ "$ACCOUNT_ID" = "None" ]; then
  echo "Error: could not resolve an AWS account from your credentials." >&2
  echo "       Set up access first (e.g. 'export AWS_PROFILE=<profile>' or 'aws configure')," >&2
  echo "       then verify with 'aws sts get-caller-identity', and re-run." >&2
  exit 1
fi

# Pin the region + account into the variables CDK reads (CDK_DEFAULT_*) and that
# other aws calls honour (AWS_REGION), so `cdk deploy` and the stack-output
# reader below can never target different places than REGION says.
export CDK_DEFAULT_ACCOUNT="$ACCOUNT_ID"
export CDK_DEFAULT_REGION="$REGION"
export AWS_REGION="$REGION"

# Pre-existing storage is all-or-nothing: partial names silently mix with the
# derived defaults and point the service at a half-real bucket set.
SKIP_STORAGE=false
if [ -n "$KB_BUCKET$VEC_BUCKET$VEC_INDEX" ]; then
  if [ -z "$KB_BUCKET" ] || [ -z "$VEC_BUCKET" ] || [ -z "$VEC_INDEX" ]; then
    echo "Error: set ALL of CODEKB_S3_BUCKET, CODEKB_S3_VECTORS_BUCKET, CODEKB_S3_VECTORS_INDEX (or none)." >&2
    exit 1
  fi
  SKIP_STORAGE=true
fi

# Adopting an existing Cognito pool needs the pool id and its hosted-UI domain
# (both or neither). The Auth stack is still deployed either way — it creates
# this deployment's app clients — so there is no SKIP for it.
ADOPT_COGNITO=false
if [ -n "$ADOPT_USER_POOL_ID$ADOPT_DOMAIN_URL" ]; then
  if [ -z "$ADOPT_USER_POOL_ID" ] || [ -z "$ADOPT_DOMAIN_URL" ]; then
    echo "Error: set BOTH CODEKB_COGNITO_USER_POOL_ID and CODEKB_COGNITO_DOMAIN_URL (or neither)." >&2
    exit 1
  fi
  ADOPT_COGNITO=true
fi

STORAGE="$PREFIX-storage"
AUTH_SECRET="$PREFIX-auth-secret"
AUTH="$PREFIX-auth"
GUARDRAIL="$PREFIX-guardrail"
SERVICE="$PREFIX-service"
WAF="$PREFIX-waf"
EDGE="$PREFIX-edge"

# External OIDC federation on a NEWLY created pool: CodeKB creates the client
# secret container (the $AUTH_SECRET stack) and this script pauses for you to
# populate it before the Auth stack's IdP reads it. Ignored when adopting a pool
# (it brings its own IdP). The name mirrors the one derived in main.ts.
OIDC_ENABLED=false
[ "$ADOPT_COGNITO" != true ] && [ -n "$OIDC_CLIENT_ID" ] && OIDC_ENABLED=true
OIDC_SECRET_NAME="$PREFIX-oidc-client-secret"

# The CDK app reads every deployment setting from the environment (see main.ts),
# so export the ones it expects; `cdk deploy` inherits this environment. The
# per-phase CODEKB_CALLBACK_DOMAIN is passed inline on each deploy below because
# it changes between the placeholder and real-host passes. Optional values are
# exported only when set — an empty CODEKB_COGNITO_PREFIX would defeat the app's
# account-scoped default.
export CODEKB_PROJECT="$PROJECT"
export CODEKB_STAGE="$STAGE"
# The app reads this to place the guardrail stack and set CODEKB_BEDROCK_REGION
# on the service; exporting it keeps the shell and the app on the same region.
export CODEKB_BEDROCK_REGION="$BEDROCK_REGION"
# Normalize the guardrail toggle so the app synth and this script agree on it.
export CODEKB_BEDROCK_GUARDRAIL_ENABLED="$GUARDRAIL_ENABLED"
if [ "$ADOPT_COGNITO" = true ]; then
  # Adopt mode: import the existing pool; the app clients are created in it.
  # CODEKB_ENABLE_AUTH stays unset (adopting and creating are mutually exclusive).
  export CODEKB_COGNITO_USER_POOL_ID="$ADOPT_USER_POOL_ID"
  export CODEKB_COGNITO_DOMAIN_URL="$ADOPT_DOMAIN_URL"
  [ -n "$ADOPT_COGNITO_REGION" ] && export CODEKB_COGNITO_REGION="$ADOPT_COGNITO_REGION"
  [ -n "$ADOPT_IDP_NAME" ] && export CODEKB_COGNITO_IDP_NAME="$ADOPT_IDP_NAME"
else
  export CODEKB_ENABLE_AUTH=true
  [ -n "$COGNITO_PREFIX" ] && export CODEKB_COGNITO_PREFIX="$COGNITO_PREFIX"
  [ -n "$OIDC_CLIENT_ID" ] && export CODEKB_OIDC_CLIENT_ID="$OIDC_CLIENT_ID"
  [ -n "$OIDC_ISSUER" ] && export CODEKB_OIDC_ISSUER="$OIDC_ISSUER"
  [ -n "$OIDC_PROVIDER_NAME" ] && export CODEKB_OIDC_PROVIDER_NAME="$OIDC_PROVIDER_NAME"
  [ -n "$OIDC_ATTRIBUTE_MAPPING" ] && export CODEKB_OIDC_ATTRIBUTE_MAPPING="$OIDC_ATTRIBUTE_MAPPING"
  [ -n "$OIDC_ONLY" ] && export CODEKB_OIDC_ONLY="$OIDC_ONLY"
fi
if [ "$SKIP_STORAGE" = true ]; then
  export CODEKB_S3_BUCKET="$KB_BUCKET"
  export CODEKB_S3_VECTORS_BUCKET="$VEC_BUCKET"
  export CODEKB_S3_VECTORS_INDEX="$VEC_INDEX"
fi

DEPLOY=(npx cdk deploy)

# describe-stacks output reader: $1 = stack name, $2 = output key,
# $3 = region (optional, defaults to $REGION — the guardrail lives in $BEDROCK_REGION).
stack_output() {
  local value region="${3:-$REGION}"
  value="$(aws cloudformation describe-stacks \
    --stack-name "$1" --region "$region" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" \
    --output text 2>/dev/null)"
  [ "$value" = "None" ] && value=""
  printf '%s' "$value"
}

require() {  # $1 = value, $2 = human description
  [ -n "$1" ] || { echo "Error: could not read $2 — aborting." >&2; exit 1; }
}

# Echo the effective value of every setting so the operator can confirm the
# deploy target before anything is created.
if [ "$SKIP_STORAGE" = true ]; then
  _storage_line="pre-existing (KB=$KB_BUCKET, VECTOR=$VEC_BUCKET, INDEX=$VEC_INDEX)"
else
  _storage_line="created by the $STORAGE stack"
fi
if [ "$ADOPT_COGNITO" = true ]; then
  _cognito_line="adopted pool $ADOPT_USER_POOL_ID${ADOPT_COGNITO_REGION:+ @ $ADOPT_COGNITO_REGION} (new app clients created in it)"
  _oidc_line="n/a (adopted pool brings its own IdP config)"
else
  _cognito_line="created by the $AUTH stack (prefix ${COGNITO_PREFIX:-<auto: $PREFIX-$ACCOUNT_ID>})"
  if [ -n "$OIDC_CLIENT_ID" ]; then
    _oidc_line="enabled (provider=${OIDC_PROVIDER_NAME:-OidcIdP}, issuer=${OIDC_ISSUER:-<UNSET — required!>}; client secret via $AUTH_SECRET stack)"
  else
    _oidc_line="disabled (Cognito user-pool sign-in)"
  fi
fi
cat <<EOF
======================================================================
 CodeKB hosted deployment — effective configuration
======================================================================
  AWS account       : $ACCOUNT_ID   (from your active AWS credentials)
  Region            : $REGION   (CODEKB_REGION)
  Bedrock region    : $BEDROCK_REGION   (CODEKB_BEDROCK_REGION; guardrail lives here)
  Secret guardrail  : $([ "$GUARDRAIL_ENABLED" = true ] && echo "enabled" || echo "DISABLED — generation runs unredacted")   (CODEKB_BEDROCK_GUARDRAIL_ENABLED)
  Project           : $PROJECT   (CODEKB_PROJECT)
  Stage             : $STAGE   (CODEKB_STAGE)
  Stack name prefix : $PREFIX
  Storage           : $_storage_line
  Cognito           : $_cognito_line
  External OIDC     : $_oidc_line
  Callback host     : ${CALLBACK_OVERRIDE:-<auto: CloudFront domain from Edge output>}
======================================================================
EOF

# ── Build the CDK app (cdk.json runs 'node dist/src/main', so tsc must run) ───
# Every run, so an upgrade picks up a changed lockfile.
echo "==> [build] npm ci"
npm ci
echo "==> [build] npm run build"
npm run build

# ── Phase 0: external OIDC client secret (create, then populate) ─────────────
# When federating a NEW pool to an external OIDC IdP, create the Secrets Manager
# secret that holds the client secret, then pause so you can populate it. The
# Auth stack's identity provider (Phase 1) resolves this value at deploy time,
# so it must hold the real client secret before Phase 1 runs.
if [ "$OIDC_ENABLED" = true ]; then
  # First deploy creates the secret and pauses; a re-run (secret already exists)
  # assumes it is populated and continues without pausing.
  SECRET_EXISTED=true
  aws secretsmanager describe-secret \
    --secret-id "$OIDC_SECRET_NAME" --region "$REGION" >/dev/null 2>&1 \
    || SECRET_EXISTED=false

  echo "==> [0/3] Deploy $AUTH_SECRET (OIDC client secret container)"
  CODEKB_CALLBACK_DOMAIN="$PLACEHOLDER_HOST" "${DEPLOY[@]}" "$AUTH_SECRET"

  if [ "$SECRET_EXISTED" = false ] && [ "${CODEKB_OIDC_SECRET_READY:-}" != "true" ]; then
    cat <<EOF

----------------------------------------------------------------------
 ACTION REQUIRED: populate the OIDC client secret
----------------------------------------------------------------------
CodeKB created an empty Secrets Manager secret:

  $OIDC_SECRET_NAME   (region $REGION)

Set it to the client secret issued by your OIDC provider, e.g.:

  aws secretsmanager put-secret-value \\
    --secret-id $OIDC_SECRET_NAME --region $REGION \\
    --secret-string '<your-oidc-client-secret>'

The next step creates the Cognito identity provider, which reads this
value at deploy time — populate it now, then continue.
----------------------------------------------------------------------
EOF
    if [ -e /dev/tty ]; then
      printf 'Press ENTER once the secret is populated (Ctrl-C to abort)... '
      read -r _ </dev/tty
    else
      echo "Error: no TTY to pause for secret population. Populate $OIDC_SECRET_NAME," >&2
      echo "       then re-run with CODEKB_OIDC_SECRET_READY=true." >&2
      exit 1
    fi
  fi
fi

# ── Phase 1: Storage + Auth (placeholder host) ───────────────────────────────
# Auth is always deployed: it creates this deployment's app clients, in a new
# pool or in the adopted one. Storage is skipped when you adopt buckets.
PHASE1=()
[ "$SKIP_STORAGE" = true ] || PHASE1+=("$STORAGE")
PHASE1+=("$AUTH")
echo "==> [1/3] Deploy ${PHASE1[*]} (placeholder host)"
CODEKB_CALLBACK_DOMAIN="$PLACEHOLDER_HOST" "${DEPLOY[@]}" "${PHASE1[@]}"

echo "==> Reading Auth outputs"
COGNITO_DOMAIN="$(stack_output "$AUTH" CognitoDomainUrl)"
COGNITO_CLIENT_ID="$(stack_output "$AUTH" UserPoolClientId)"
USER_POOL_ID="$(stack_output "$AUTH" UserPoolId)"
# IdpName is only output when a federated IdP is configured; empty otherwise, so
# the SPA shows the Cognito picker instead of requesting a nonexistent IdP.
SPA_IDP_NAME="$(stack_output "$AUTH" IdpName)"
require "$COGNITO_DOMAIN" "Auth output CognitoDomainUrl"
require "$COGNITO_CLIENT_ID" "Auth output UserPoolClientId"
require "$USER_POOL_ID" "Auth output UserPoolId"

# The SPA is built inside the image (multi-stage Dockerfile), not staged here.
# Export the Cognito values the Service deploy inlines into the SPA via Docker
# build args (main.ts reads these CODEKB_SPA_* and forwards them to fromAsset).
# The web client id is only known now that Auth is deployed — this is why the
# frontend is built at the Service phase and not earlier.
export CODEKB_SPA_COGNITO_DOMAIN="$COGNITO_DOMAIN"
export CODEKB_SPA_WEB_CLIENT_ID="$COGNITO_CLIENT_ID"
export CODEKB_SPA_IDP_NAME="$SPA_IDP_NAME"

# ── Phase 2: Service + Waf + Edge (placeholder host) → mint the real host ─────
# Guardrail lives in the Bedrock region, so deploy it alone first and read its
# ARN/version (from $BEDROCK_REGION) into the Service env — same deploy→read→pass
# as the Auth outputs above, keeping the stacks free of a cross-region CFN link.
# Skipped entirely when the guardrail is disabled.
if [ "$GUARDRAIL_ENABLED" = true ]; then
  echo "==> [2/3] Deploy $GUARDRAIL (Bedrock region: $BEDROCK_REGION)"
  CODEKB_CALLBACK_DOMAIN="$PLACEHOLDER_HOST" "${DEPLOY[@]}" "$GUARDRAIL"

  echo "==> Reading Guardrail outputs"
  GUARDRAIL_ARN="$(stack_output "$GUARDRAIL" GuardrailArn "$BEDROCK_REGION")"
  GUARDRAIL_VERSION="$(stack_output "$GUARDRAIL" GuardrailVersion "$BEDROCK_REGION")"
  require "$GUARDRAIL_ARN" "Guardrail output GuardrailArn"
  export CODEKB_BEDROCK_GUARDRAIL_ARN="$GUARDRAIL_ARN"
  export CODEKB_BEDROCK_GUARDRAIL_VERSION="$GUARDRAIL_VERSION"
else
  echo "==> [2/3] Guardrail disabled (CODEKB_BEDROCK_GUARDRAIL_ENABLED=false) — skipping"
fi

# The Service image build runs the frontend build stage with the args above, so
# the SPA is always baked in; no host-side staging step exists anymore.
echo "==> [2/3] Deploy $SERVICE + $WAF + $EDGE (placeholder host)"
CODEKB_CALLBACK_DOMAIN="$PLACEHOLDER_HOST" "${DEPLOY[@]}" "$SERVICE" "$WAF" "$EDGE"

echo "==> Reading CloudFront domain"
CLOUDFRONT_HOST="$(stack_output "$EDGE" DistributionDomainName)"
require "$CLOUDFRONT_HOST" "Edge output DistributionDomainName"
echo "    CloudFront host: $CLOUDFRONT_HOST"
# Prefer an operator-provided callback host (a custom domain) over the raw
# CloudFront domain, so the app clients' callback URLs and the service's OAuth
# metadata are wired to the host users actually reach. Falls back to the
# CloudFront domain when CODEKB_CALLBACK_DOMAIN is unset (the default).
if [ -n "$CALLBACK_OVERRIDE" ]; then
  CALLBACK="$CALLBACK_OVERRIDE"
  echo "    Using operator callback host (CODEKB_CALLBACK_DOMAIN): $CALLBACK"
else
  CALLBACK="$CLOUDFRONT_HOST"
fi

# ── Phase 3: redeploy Auth + Service with the real host ──────────────────────
# Auth is redeployed so the app clients it owns get the real callback URLs
# (true whether the pool was created or adopted); Service picks up the real
# public domain for its OAuth metadata. The SPA build args are unchanged, so the
# image asset hash is the same and the image is not rebuilt.
echo "==> [3/3] Redeploy $AUTH + $SERVICE with real host"
CODEKB_CALLBACK_DOMAIN="$CALLBACK" "${DEPLOY[@]}" "$AUTH" "$SERVICE"

echo ""
echo "==> Done. App: https://$CALLBACK/"
if [ "$ADOPT_COGNITO" = true ]; then
  cat <<EOF

Adopted pool $USER_POOL_ID: its existing users can sign in. This deployment's
own web/mcp app clients were created in the pool with callback URLs for
https://$CALLBACK/, so there is nothing to register by hand.
EOF
else
  cat <<EOF

Create a user and sign in:
  aws cognito-idp admin-create-user \\
    --user-pool-id $USER_POOL_ID --username <email> \\
    --user-attributes Name=email,Value=<email> Name=email_verified,Value=true \\
    --region $REGION
  aws cognito-idp admin-set-user-password \\
    --user-pool-id $USER_POOL_ID --username <email> \\
    --password '<StrongPassw0rd!>' --permanent --region $REGION
EOF
fi
