# Troubleshooting

**`docker build` fails**
- Build from the repository root; the build context is the whole repository.
- The build pulls images from `public.ecr.aws` and packages from PyPI and npm.
  Check that the host, or its proxy, can reach them.
- On an `arm64` host, build with `--build-arg BASE_PLATFORM=linux/arm64`, or use
  `./scripts/build-image.sh`, which picks the host's architecture.

**CodeKB stops at startup**
- Read the last lines of the log; startup errors name the setting to fix.
- `OTEL_LOGS_EXPORTER` must be exactly `none` or `otlp`.
- With the guardrail on, `CODEKB_BEDROCK_GUARDRAIL_ID` must be set.
- The startup check rejected a model setting, for example a
  `CODEKB_BEDROCK_MAX_TOKENS` above the model's limit. See
  [Configuration → Models](configuration.md#models).

**The UI loads but every request fails with 503**
- Sign-in is on but not configured. Set `CODEKB_COGNITO_USER_POOL_ID` and
  `CODEKB_COGNITO_APP_CLIENT_IDS` ([Deploying to AWS → Sign-in](deployment.md#sign-in)),
  or, for a [local setup](local.md) only, set
  `CODEKB_REQUIRE_AUTHENTICATION=false`.

**Bedrock errors (`AccessDeniedException`, `ValidationException`)**
- Check that `CodeKBAccessPolicy` is attached to the identity CodeKB runs as.
- Check that the model is enabled in `CODEKB_BEDROCK_REGION` and that its prefix
  (`us.`, `eu.`, `apac.`) matches that region.
- The Qwen response path is experimental and needs the model ARN added to the
  policy.

**Analysis is slow, or `ThrottlingException` appears in the logs**
- Summarization makes many Bedrock calls at once. If Bedrock throttles, lower
  `CODEKB_SUMMARIZATION_CONCURRENCY` (for example from 40 to 10) or request a
  higher quota in Service Quotas. If it doesn't, raising the value speeds things up.
- If `CODEKB_SUMMARIZATION_CONCURRENCY` is unset it is 1, which serializes the
  calls. Runtime still varies with component count, model latency and quota.

**A space remains in progress after the server stopped**
- It can't be deleted while marked `ANALYZING`, `SUMMARIZING` or `REFRESHING`.
  On the first detailed status check after 12 hours without progress
  (`CODEKB_SUMMARIZATION_FAIL_AFTER_STALE_S`), CodeKB marks it failed. Then retry
  a failed refresh with **Refresh space**.
- Retry a failed analysis of a Git repository with
  `POST /api/spaces/{space_id}/retry`. It keeps the space ID and commit, and does
  not regenerate the descriptions the earlier run stored. The same call works on
  an analysis that has made no progress for 30 minutes
  (`CODEKB_SUMMARIZATION_WARN_AFTER_STALE_S`), without waiting for the 12 hours.
  A failed analysis of a ZIP upload is deleted and created again.

**The container can't find AWS credentials, or they expire**
- Check from the host first: `aws sts get-caller-identity`.
- For SSO, run `aws sso login` again. `./scripts/run-container.sh` mounts `~/.aws`
  read-write so tokens refresh; exported keys don't refresh, so restart the
  container when they expire.
- On EC2 under Docker, set the instance metadata hop limit to 2.

**`setup_aws.sh` can't attach the policy to an SSO role**
- Roles created by IAM Identity Center (`AWSReservedSSO_*`) can't be changed in
  IAM. Ask an administrator to add `CodeKBAccessPolicy` to your permission set, or
  run CodeKB as a different role or user.

**A private repository can't be added**
- Save a token on the **Git credentials** page first, and check that the provider
  is in `CODEKB_GIT_CREDENTIALS_PROVIDERS` and `CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN`
  is set.
- 400 means the host is unsupported or the token was rejected; 503 means the
  provider didn't answer, so retry.

**A Dart class is missing**
- Expected if it uses a declaring constructor. See [Languages → Dart](languages.md#dart).

**An MCP client can't connect**
- The URL ends in `/mcp/mcp`. See [MCP](mcp.md).
- Against a deployment with sign-in on, the client must support MCP authorization.
- Restart the client after changing its configuration.

**Port 8000 is in use**
- `./scripts/run.sh` (ports 8000 and 5173) and `./scripts/run-container.sh`
  (port 8000) stop if their port is taken. Inspect old containers first with
  `docker ps -a --filter name=codekb`, then remove only the confirmed stale one.
  Substitute `finch` if that is your runtime.
- With `docker run`, publish another host port: `-p 127.0.0.1:9000:8000`.
