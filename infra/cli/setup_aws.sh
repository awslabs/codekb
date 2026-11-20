#!/usr/bin/env bash
#
# Create the AWS resources CodeKB needs, from the settings in .env: S3 bucket,
# S3 Vectors bucket and index, CloudWatch log group, git-credentials KMS key,
# and the CodeKBAccessPolicy IAM policy (created, not attached).
#
# Run once per AWS account. Safe to re-run: existing resources are skipped, and
# the IAM policy is updated in place with a new default version.
#
# Debug tracing is scoped to the AWS provisioning steps only (see `set -x`
# below). It is deliberately kept OFF while `.env` is sourced: with tracing on,
# `set -v`/`set -x` would echo the file's credential/account-ID assignments to
# stdout/stderr and into any captured logs. The `aws` commands are safe to
# trace because secrets live in the environment, not in command arguments.
set -euo pipefail

# ── Locate .env ───────────────────────────────────────────────────────
# Beside this script in the customer zip; at the repository root in the repo.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE=""
for candidate in "$SCRIPT_DIR/.env" "$SCRIPT_DIR/../../.env"; do
  if [ -f "$candidate" ]; then
    ENV_FILE="$(cd "$(dirname "$candidate")" && pwd)/.env"
    break
  fi
done
if [ -z "$ENV_FILE" ]; then
  echo "Error: no .env found beside this script or at the repository root." >&2
  echo "       Copy .env.example to .env and fill it in before running this." >&2
  exit 1
fi

echo "==> Reading configuration from $ENV_FILE"

# Matched as text before sourcing: "<" is a shell redirection operator.
PLACEHOLDER_LINES="$(
  grep -nE '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=.*<[^>]+>' "$ENV_FILE" || true
)"
if [ -n "$PLACEHOLDER_LINES" ]; then
  echo "Error: $ENV_FILE still has placeholders that need replacing:" >&2
  printf '%s\n' "$PLACEHOLDER_LINES" | sed -E 's|^([0-9]+):|         line \1: |' >&2
  echo "" >&2
  echo "       Replace each <...> with a real value, then run this again." >&2
  exit 1
fi

# Parsed, not sourced: the value after the first "=" is taken literally, so shell
# metacharacters in it cannot execute.
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"   # tolerate CRLF
  case "$line" in ''|'#'*) continue ;; esac
  case "$line" in *=*) ;; *) continue ;; esac
  env_key="${line%%=*}"
  env_key="${env_key%%[[:space:]]*}"
  case "$env_key" in
    [A-Za-z_]*) export "$env_key=${line#*=}" ;;
  esac
done < "$ENV_FILE"

for required in CODEKB_AWS_ACCOUNT_ID CODEKB_S3_BUCKET CODEKB_S3_REGION \
                CODEKB_S3_VECTORS_BUCKET CODEKB_S3_VECTORS_REGION \
                CODEKB_S3_VECTORS_INDEX; do
  if [ -z "${!required:-}" ]; then
    echo "Error: $required is not set in $ENV_FILE." >&2
    exit 1
  fi
done

echo "    S3 bucket:       ${CODEKB_S3_BUCKET} (${CODEKB_S3_REGION})"
echo "    Vectors bucket:  ${CODEKB_S3_VECTORS_BUCKET} (${CODEKB_S3_VECTORS_REGION})"
echo "    Vectors index:   ${CODEKB_S3_VECTORS_INDEX}"

POLICY_FILE="$(mktemp "${TMPDIR:-/tmp}/codekb-policy.XXXXXX")"
BUCKET_POLICY_FILE="$(mktemp "${TMPDIR:-/tmp}/codekb-bucket-policy.XXXXXX")"
GUARDRAIL_FILE="$(mktemp "${TMPDIR:-/tmp}/codekb-guardrail.XXXXXX")"
trap 'rm -f "$POLICY_FILE" "$BUCKET_POLICY_FILE" "$GUARDRAIL_FILE"' EXIT

# Trace the provisioning commands only (no secrets are passed as arguments).
set -x

# ─── 1. Create the regular S3 bucket (knowledge base data) ───
# Note: omit --create-bucket-configuration if REGION is us-east-1
if aws s3api head-bucket --bucket "${CODEKB_S3_BUCKET}" 2>/dev/null; then
  { set +x; } 2>/dev/null
  echo "Step 1 ... S3 bucket ${CODEKB_S3_BUCKET} already exists — skipping create"
  set -x
elif [ "${CODEKB_S3_REGION}" = "us-east-1" ]; then
  aws s3api create-bucket \
    --bucket "${CODEKB_S3_BUCKET}" \
    --region "${CODEKB_S3_REGION}"
else
  aws s3api create-bucket \
    --bucket "${CODEKB_S3_BUCKET}" \
    --region "${CODEKB_S3_REGION}" \
    --create-bucket-configuration LocationConstraint="${CODEKB_S3_REGION}"
fi

# ─── 1a. Baseline hardening on the knowledge-base bucket ───
# All three are overwrites, so they re-apply cleanly on every run.
aws s3api put-public-access-block \
  --bucket "${CODEKB_S3_BUCKET}" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

aws s3api put-bucket-encryption \
  --bucket "${CODEKB_S3_BUCKET}" \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": { "SSEAlgorithm": "AES256" },
      "BucketKeyEnabled": true
    }]
  }'

cat > "${BUCKET_POLICY_FILE}" << BUCKET_POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyUnencryptedTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::${CODEKB_S3_BUCKET}",
        "arn:aws:s3:::${CODEKB_S3_BUCKET}/*"
      ],
      "Condition": { "Bool": { "aws:SecureTransport": "false" } }
    }
  ]
}
BUCKET_POLICY
aws s3api put-bucket-policy \
  --bucket "${CODEKB_S3_BUCKET}" \
  --policy "file://${BUCKET_POLICY_FILE}"

# Permits the browser PUT used for zip upload. Narrow AllowedOrigins to your own
# origin if you know it.
aws s3api put-bucket-cors \
  --bucket "${CODEKB_S3_BUCKET}" \
  --cors-configuration '{
    "CORSRules": [{
      "AllowedOrigins": ["*"],
      "AllowedMethods": ["PUT", "GET"],
      "AllowedHeaders": ["*"],
      "MaxAgeSeconds": 3600
    }]
  }'

# ─── 2. Create the S3 Vectors bucket ───
if aws s3vectors get-vector-bucket \
     --vector-bucket-name "${CODEKB_S3_VECTORS_BUCKET}" \
     --region "${CODEKB_S3_VECTORS_REGION}" >/dev/null 2>&1; then
  { set +x; } 2>/dev/null
  echo "Step 2 ... vector bucket already exists — skipping create"
  set -x
else
  aws s3vectors create-vector-bucket \
    --vector-bucket-name "${CODEKB_S3_VECTORS_BUCKET}" \
    --encryption-configuration '{"sseType":"AES256"}' \
    --region "${CODEKB_S3_VECTORS_REGION}"
fi

# ─── 3. Create the vector index ───
# The dimension is tied to Titan Text Embeddings V2 and cannot be changed later
# without rebuilding the index.
if aws s3vectors get-index \
     --vector-bucket-name "${CODEKB_S3_VECTORS_BUCKET}" \
     --index-name "${CODEKB_S3_VECTORS_INDEX}" \
     --region "${CODEKB_S3_VECTORS_REGION}" >/dev/null 2>&1; then
  { set +x; } 2>/dev/null
  echo "Step 3 ... vector index already exists — skipping create"
  set -x
else
  aws s3vectors create-index \
    --vector-bucket-name "${CODEKB_S3_VECTORS_BUCKET}" \
    --index-name "${CODEKB_S3_VECTORS_INDEX}" \
    --data-type float32 \
    --dimension 1024 \
    --distance-metric cosine \
    --metadata-configuration '{
      "nonFilterableMetadataKeys": ["source", "timestamp", "content", "description", "call_graph_text"]
    }' \
    --region "${CODEKB_S3_VECTORS_REGION}"
fi

# ─── 4. Create the CloudWatch log group for OpenTelemetry logs ───
# Creates the group in CODEKB_S3_REGION. CodeKB creates the streams inside it at
# runtime, resolving the region from the host in OTEL_EXPORTER_OTLP_LOGS_ENDPOINT,
# so point that endpoint at this same region.
# ResourceAlreadyExistsException is the expected outcome on a re-run.
aws logs create-log-group \
  --log-group-name /codekb/otel \
  --region "${CODEKB_S3_REGION}" 2>/dev/null || true

# ─── 5. Create the KMS key (KEK) that encrypts stored git credentials ───
# One key per deployment, in the same region as the KB bucket (the backend
# resolves the KMS client region from CODEKB_S3_REGION).
#
# The key keeps the default KMS policy (account root); access is granted by IAM
# via the CodeKBAccessPolicy "GitCredentialsKmsKey" statement below.
CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN=$(
  aws kms describe-key --key-id alias/codekb-git-creds-kek \
    --region "${CODEKB_S3_REGION}" \
    --query 'KeyMetadata.Arn' --output text 2>/dev/null || true
)
if [ -z "${CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN}" ]; then
  CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN=$(aws kms create-key \
    --description "CodeKB: wraps per-user git credentials (envelope encryption)" \
    --region "${CODEKB_S3_REGION}" \
    --query 'KeyMetadata.Arn' --output text)
  # Alias immediately: it is how a later run finds this key again.
  aws kms create-alias \
    --alias-name alias/codekb-git-creds-kek \
    --target-key-id "${CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN}" \
    --region "${CODEKB_S3_REGION}"
fi

# Outside the branch, so a re-run enables rotation on a key that missed it.
aws kms enable-key-rotation \
  --key-id "${CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN}" \
  --region "${CODEKB_S3_REGION}"

# ─── 6. Create the Bedrock Guardrail (secret/PII redaction) ───
# Masks committed secrets/PII (built-in PII + custom regexes, all ANONYMIZE) on
# every text-generation call, so nothing sensitive reaches Bedrock, the stored
# descriptions, or the shared index. The IAM policy below requires it, so calls
# fail closed without it. Re-run safe: an existing guardrail is updated in place.
#
# Honors CODEKB_BEDROCK_GUARDRAIL_ENABLED (default true) — the same variable the
# container reads. When false, skip creating the guardrail and omit the
# Apply/Deny statements from the policy, so generation runs unredacted
# (deliberate, not recommended).
GUARDRAIL_ENABLED=true
[ "${CODEKB_BEDROCK_GUARDRAIL_ENABLED:-}" = "false" ] && GUARDRAIL_ENABLED=false
GUARDRAIL_ARN=""
GUARDRAIL_POLICY_STMTS=""

if [ "$GUARDRAIL_ENABLED" = true ]; then
GUARDRAIL_REGION="${CODEKB_BEDROCK_REGION:-us-east-1}"
GUARDRAIL_NAME="codekb-secret-redaction"

# Quoted heredoc: patterns are written verbatim (no shell expansion). "\\." is
# the JSON escape for a literal dot in the regex.
cat > "${GUARDRAIL_FILE}" << 'GUARDRAIL_SIP'
{
  "piiEntitiesConfig": [
    { "type": "AWS_ACCESS_KEY", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true },
    { "type": "AWS_SECRET_KEY", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true },
    { "type": "PASSWORD", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true },
    { "type": "PIN", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true },
    { "type": "CREDIT_DEBIT_CARD_NUMBER", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true },
    { "type": "CREDIT_DEBIT_CARD_CVV", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true },
    { "type": "US_SOCIAL_SECURITY_NUMBER", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true },
    { "type": "US_BANK_ACCOUNT_NUMBER", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true },
    { "type": "US_BANK_ROUTING_NUMBER", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true },
    { "type": "INTERNATIONAL_BANK_ACCOUNT_NUMBER", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true },
    { "type": "SWIFT_CODE", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true },
    { "type": "EMAIL", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true },
    { "type": "PHONE", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true }
  ],
  "regexesConfig": [
    { "name": "github-token", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true, "pattern": "gh[opusr]_[A-Za-z0-9]{36}" },
    { "name": "github-pat", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true, "pattern": "github_pat_[A-Za-z0-9_]{22,}" },
    { "name": "gitlab-pat", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true, "pattern": "glpat-[A-Za-z0-9_-]{20}" },
    { "name": "slack-token", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true, "pattern": "xox[baprs]-[A-Za-z0-9-]{10,}" },
    { "name": "google-api-key", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true, "pattern": "AIza[0-9A-Za-z_-]{35}" },
    { "name": "stripe-key", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true, "pattern": "(sk|rk)_(live|test)_[0-9a-zA-Z]{24,}" },
    { "name": "jwt", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true, "pattern": "eyJ[A-Za-z0-9_-]{10,}\\.eyJ[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,}" },
    { "name": "npm-token", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true, "pattern": "npm_[A-Za-z0-9]{36}" },
    { "name": "slack-webhook", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true, "pattern": "https://hooks\\.slack\\.com/services/[A-Za-z0-9/]+" },
    { "name": "url-credentials", "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": true, "outputEnabled": true, "pattern": "[a-zA-Z][a-zA-Z0-9+.-]*://[^\\s:@/]+:[^\\s:@/]{3,}@" }
  ]
}
GUARDRAIL_SIP

GUARDRAIL_ARN=$(
  aws bedrock list-guardrails \
    --region "${GUARDRAIL_REGION}" \
    --query "guardrails[?name=='${GUARDRAIL_NAME}'].arn | [0]" \
    --output text 2>/dev/null || true
)
if [ -z "${GUARDRAIL_ARN}" ] || [ "${GUARDRAIL_ARN}" = "None" ]; then
  GUARDRAIL_ARN=$(aws bedrock create-guardrail \
    --name "${GUARDRAIL_NAME}" \
    --description "Masks committed secrets/PII in CodeKB text-generation calls." \
    --blocked-input-messaging "Request blocked by the CodeKB guardrail." \
    --blocked-outputs-messaging "Response blocked by the CodeKB guardrail." \
    --sensitive-information-policy-config "file://${GUARDRAIL_FILE}" \
    --region "${GUARDRAIL_REGION}" \
    --query 'guardrailArn' --output text)
else
  # Push any pattern/entity changes from the heredoc above to the existing
  # guardrail, so a re-run after a release keeps it current (not stale).
  aws bedrock update-guardrail \
    --guardrail-identifier "${GUARDRAIL_ARN}" \
    --name "${GUARDRAIL_NAME}" \
    --description "Masks committed secrets/PII in CodeKB text-generation calls." \
    --blocked-input-messaging "Request blocked by the CodeKB guardrail." \
    --blocked-outputs-messaging "Response blocked by the CodeKB guardrail." \
    --sensitive-information-policy-config "file://${GUARDRAIL_FILE}" \
    --region "${GUARDRAIL_REGION}" >/dev/null
fi

# Guardrail-referencing policy statements, spliced into the policy below only
# when the guardrail exists. The leading comma joins them to the statement
# before the splice point.
GUARDRAIL_POLICY_STMTS=$(cat <<GUARDRAIL_POLICY
,
    {
      "Sid": "BedrockApplyGuardrail",
      "Effect": "Allow",
      "Action": "bedrock:ApplyGuardrail",
      "Resource": "${GUARDRAIL_ARN}"
    },
    {
      "Sid": "BedrockRequireGuardrailOnGeneration",
      "Effect": "Deny",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:*::foundation-model/qwen.*",
        "arn:aws:bedrock:*:${CODEKB_AWS_ACCOUNT_ID}:inference-profile/*",
        "arn:aws:bedrock:*:*:inference-profile/apac.*",
        "arn:aws:bedrock:*:*:inference-profile/eu.*",
        "arn:aws:bedrock:*:*:inference-profile/us.*"
      ],
      "Condition": {
        "StringNotLike": {
          "bedrock:GuardrailIdentifier": "${GUARDRAIL_ARN}*"
        }
      }
    }
GUARDRAIL_POLICY
)
fi

# ─── 7. Create IAM policy for CodeKB ───
cat > "${POLICY_FILE}" << POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3KnowledgeBase",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket",
        "s3:DeleteObject"
      ],
      "Resource": [
        "arn:aws:s3:::${CODEKB_S3_BUCKET}",
        "arn:aws:s3:::${CODEKB_S3_BUCKET}/*"
      ]
    },
    {
      "Sid": "S3VectorsBucket",
      "Effect": "Allow",
      "Action": [
        "s3vectors:CreateVectorBucket",
        "s3vectors:GetVectorBucket",
        "s3vectors:CreateIndex",
        "s3vectors:GetIndex"
      ],
      "Resource": "arn:aws:s3vectors:${CODEKB_S3_VECTORS_REGION}:${CODEKB_AWS_ACCOUNT_ID}:bucket/${CODEKB_S3_VECTORS_BUCKET}"
    },
    {
      "Sid": "S3VectorsData",
      "Effect": "Allow",
      "Action": [
        "s3vectors:PutVectors",
        "s3vectors:GetVectors",
        "s3vectors:QueryVectors",
        "s3vectors:DeleteVectors",
        "s3vectors:ListVectors"
      ],
      "Resource": "arn:aws:s3vectors:${CODEKB_S3_VECTORS_REGION}:${CODEKB_AWS_ACCOUNT_ID}:bucket/${CODEKB_S3_VECTORS_BUCKET}/index/${CODEKB_S3_VECTORS_INDEX}"
    },
    {
      "Sid": "BedrockModels",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.*",
        "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2*",
        "arn:aws:bedrock:*:${CODEKB_AWS_ACCOUNT_ID}:inference-profile/*.anthropic.*"
      ]
    },
    {
      "Sid": "CloudWatchLogsOtel",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:${CODEKB_S3_REGION}:${CODEKB_AWS_ACCOUNT_ID}:log-group:/codekb/otel:*"
    },
    {
      "Sid": "GitCredentialsKmsKey",
      "Effect": "Allow",
      "Action": [
        "kms:GenerateDataKey",
        "kms:Decrypt"
      ],
      "Resource": "${CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN}"
    }${GUARDRAIL_POLICY_STMTS}
  ]
}
POLICY

CODEKB_POLICY_ARN="arn:aws:iam::${CODEKB_AWS_ACCOUNT_ID}:policy/CodeKBAccessPolicy"
if aws iam get-policy --policy-arn "${CODEKB_POLICY_ARN}" >/dev/null 2>&1; then
  # A new default version applies any permission changes to an existing policy.
  # IAM caps a managed policy at five versions, so prune the oldest non-default
  # one when at the limit.
  POLICY_VERSION_COUNT=$(aws iam list-policy-versions \
    --policy-arn "${CODEKB_POLICY_ARN}" \
    --query 'length(Versions)' --output text)
  if [ "${POLICY_VERSION_COUNT}" -ge 5 ]; then
    OLDEST_POLICY_VERSION=$(aws iam list-policy-versions \
      --policy-arn "${CODEKB_POLICY_ARN}" \
      --query 'sort_by(Versions[?IsDefaultVersion==`false`], &CreateDate)[0].VersionId' \
      --output text)
    aws iam delete-policy-version \
      --policy-arn "${CODEKB_POLICY_ARN}" \
      --version-id "${OLDEST_POLICY_VERSION}"
  fi
  aws iam create-policy-version \
    --policy-arn "${CODEKB_POLICY_ARN}" \
    --policy-document "file://${POLICY_FILE}" \
    --set-as-default
  CODEKB_POLICY_ACTION="updated to a new default version"
else
  aws iam create-policy \
    --policy-name CodeKBAccessPolicy \
    --policy-document "file://${POLICY_FILE}"
  CODEKB_POLICY_ACTION="created but NOT attached"
fi

# ─── Done ───
set +x   # stop tracing before printing the help output
echo ""
echo "═══════════════════════════════════════════════════════════════════════"
echo "  CodeKBAccessPolicy ${CODEKB_POLICY_ACTION}."
echo "  Run ONE of the following to attach it to your IAM principal:"
echo ""
echo "  # For an IAM user:"
echo "  aws iam attach-user-policy --user-name <YOUR_USER> \\"
echo "    --policy-arn ${CODEKB_POLICY_ARN}"
echo ""
echo "  # For an IAM role:"
echo "  aws iam attach-role-policy --role-name <YOUR_ROLE> \\"
echo "    --policy-arn ${CODEKB_POLICY_ARN}"
echo "  (Attaching twice is harmless, so re-run this if you are unsure.)"
echo "═══════════════════════════════════════════════════════════════════════"
echo ""
echo "  Git credentials KMS key (add to .env):"
echo "    CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN=${CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN}"
echo "  Do NOT delete or re-create this key once users have connected — stored"
echo "  tokens are sealed under it and would become undecryptable."
echo "═══════════════════════════════════════════════════════════════════════"
echo ""
if [ "$GUARDRAIL_ENABLED" = true ]; then
  echo "  Secret/PII guardrail — REQUIRED (add to .env; generation calls fail"
  echo "  closed without it, since the IAM policy above mandates it):"
  echo "    CODEKB_BEDROCK_GUARDRAIL_ID=${GUARDRAIL_ARN}"
  echo "    CODEKB_BEDROCK_GUARDRAIL_VERSION=DRAFT"
  echo "  Keep CODEKB_BEDROCK_REGION set to ${GUARDRAIL_REGION} (where it lives)."
else
  echo "  Secret/PII guardrail — DISABLED (CODEKB_BEDROCK_GUARDRAIL_ENABLED=false)."
  echo "  No guardrail was created and the IAM policy omits the guardrail deny,"
  echo "  so generation runs UNREDACTED — committed secrets/PII reach Bedrock and"
  echo "  the vector index in the clear. Keep CODEKB_BEDROCK_GUARDRAIL_ENABLED=false"
  echo "  in .env to match."
fi
echo "═══════════════════════════════════════════════════════════════════════"
