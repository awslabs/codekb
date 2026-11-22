# Security

CodeKB runs in your AWS account. This page describes what it protects, what it
leaves to you, and how to remove its data.

- [Sign-in](#sign-in)
- [Space authorization](#space-authorization)
- [Git credential storage](#git-credential-storage)
- [Secrets in analyzed code](#secrets-in-analyzed-code)
- [Network access](#network-access)
- [Decommissioning](#decommissioning)

## Sign-in

`CODEKB_REQUIRE_AUTHENTICATION` is `true` by default. It protects application API
routes and the MCP endpoint with tokens signed by your Amazon Cognito user pool.
If the pool isn't configured, protected routes return 503 rather than becoming
public. `/api/health`, OAuth discovery and callback endpoints, static UI assets,
SPA routes, and `OPTIONS` requests remain public by design. The
[CDK deployment](deployment.md) sets this up; [Requirements for any
deployment](deployment.md#sign-in) lists what it configures.
(`CODEKB_REQUIRE_AUTH` is an older name for the same setting.)

With `CODEKB_REQUIRE_AUTHENTICATION=false`, requests are accepted without
verification. CodeKB takes identity from an unsigned bearer token, an unverified
`X-User-Identity` header, or a shared `local-dev` identity when neither is present.
Anyone who can reach the port can therefore choose another identity and act as
that user. Use this mode only for a [local setup](local.md) published on
`127.0.0.1`.

CodeKB doesn't terminate TLS or provide a general request-rate limiter. The
pipeline pool limits concurrent background jobs, but not searches, chat, UI or
MCP requests. Put CodeKB behind a proxy or load balancer that handles TLS and
request limits; the CDK deployment uses CloudFront and AWS WAF.

## Space authorization

`CODEKB_REQUIRE_AUTHORIZATION` is `true` by default. A signed-in user then sees
only the spaces they are allowed to, and searches, chat and MCP results are
limited the same way:

| Space source | Who can read it |
| --- | --- |
| Public repository | Every signed-in user |
| Private repository | Users whose saved Git token can read the repository. Refresh, delete and overview regeneration need maintainer or admin permission |
| Zip upload | Only the user who uploaded it |

Permissions are checked with the Git provider and cached for 4 hours
(`CODEKB_AUTHZ_REPO_CACHE_TTL_HOURS`), so a revoked grant can keep working that
long. Private repositories need a provider in `CODEKB_GIT_CREDENTIALS_PROVIDERS`
and the KMS key in `CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN`; without them, only public
and zip spaces are reachable.

Set `CODEKB_REQUIRE_AUTHORIZATION=false` to let every signed-in user open every
space. With sign-in off, authorization uses an unverified, caller-controlled
identity and does not provide a security boundary.

## Git credential storage

A saved personal access token is stored in your knowledge-base bucket at
`users/<user-id>/gitcreds.json`. It is encrypted with a fresh AES-256-GCM data key
for every write, and that data key is encrypted by the KMS key in
`CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN`. The user ID is part of the KMS encryption
context, so a token can't be decrypted under another user's identity.

Plan around two consequences:

- **CodeKB's AWS role can decrypt every saved token.** Treat control of the role
  or compromise of the application as access to all saved tokens. Ordinary users
  can manage only their own credentials, and the API never returns token values.
  Every `kms:Decrypt` call appears in CloudTrail.
- **Tokens share the knowledge-base bucket**, separated by key prefix and
  protected by Block Public Access, a TLS-only policy and the KMS encryption. A
  separate bucket isn't configurable.

Recommend read-only tokens scoped to the repositories users need.

`CODEKB_ALLOW_CREDENTIALS_IN_GIT_URL=true` accepts URLs such as
`https://user:token@host/repo.git` for deployments without the KMS key. The token
is then stored in the space's recorded URL, without the protection above.

## Secrets in analyzed code

When enabled, the supplied Amazon Bedrock guardrail attempts to anonymize the PII
categories and credential patterns configured by the CDK or setup script before
text-generation inference. Detection isn't exhaustive. Don't commit secrets or
use the guardrail as a substitute for secret scanning. Source archives in S3
remain unredacted.

The IAM policy denies text-generation calls that don't carry the guardrail, and
CodeKB won't start if the guardrail is enabled but
`CODEKB_BEDROCK_GUARDRAIL_ID` is unset. This enforces use of the guardrail; it
doesn't guarantee that every sensitive value is detected. Generated descriptions,
not raw source, are embedded in the vector index.

`CODEKB_BEDROCK_GUARDRAIL_ENABLED=false` turns the guardrail off in CodeKB and the
setup tools. Not recommended: source sent for generation is then unredacted.

## Network access

CodeKB has no vendor telemetry or phone-home service. It makes outbound HTTPS
calls to:

- AWS: S3, S3 Vectors, Bedrock, KMS, Cognito, and CloudWatch Logs if you export
  logs there. A cross-region inference profile (for example `us.`) can serve a
  request from another region in the same geography.
- The Git hosts in `CODEKB_ALLOWED_GIT_HOSTS`, to clone.
- The Git provider's API (`api.github.com`, `gitlab.com`, `api.bitbucket.org`, or
  your `CODEKB_*_CUSTOM_HOST`), to check repository visibility and permissions and
  to validate saved tokens.
- An OTLP endpoint, if you configure one.

A hostname that resolves to a private or loopback address needs
`CODEKB_ALLOW_PRIVATE_GIT_ADDRESSES=true`. An exact private IP literal explicitly
listed in `CODEKB_ALLOWED_GIT_HOSTS` is also allowed. Link-local, metadata,
multicast, reserved and unspecified addresses are always refused.

The app sets `X-Content-Type-Options` and `X-Frame-Options` on responses, and a
`Content-Security-Policy` on HTML responses (`CODEKB_SECURITY_HEADERS_ENABLED`).
HSTS is off until you set `CODEKB_SECURITY_HSTS`.

## Decommissioning

Deleting the CDK stacks or container doesn't delete retained data. Before any
manual deletion, confirm which resources CodeKB created and which it reused.
Never delete a reused resource just because CodeKB referenced it.

For a complete removal:

1. Stop CodeKB so it can't write new data.
2. Delete every version and delete marker from the versioned knowledge-base
   bucket, then delete the bucket. `aws s3 rm --recursive` removes only the
   current view and is not enough.
3. Delete the S3 Vectors index, then its vector bucket.
4. Empty every version from the retained S3 access-log bucket, then delete it.
5. If CodeKB created them and nothing else uses them, delete the retained Cognito
   user pool and OIDC client-secret.
6. Only after all encrypted Git-token objects and their versions are gone,
   schedule deletion of the Git-credentials KMS key (minimum 7 days).
7. For a CDK deployment, delete the container images each deploy pushed to the
   CDK bootstrap repository, `cdk-<qualifier>-container-assets-<account>-<region>`.

These actions are irreversible. Use the AWS console or your organization's
resource-removal workflow so you can inspect each retained resource, account and
region before confirming it.
