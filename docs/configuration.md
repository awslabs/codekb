# Configuration

CodeKB reads its settings from environment variables. Built-in defaults live in
`backend/src/core/default.yaml`, and `.env.example` lists every variable you would
normally set, with a comment on each. This page covers the settings that need a
decision. The [CDK deployment](deployment.md#settings) takes its settings from
your shell instead.

`docker run --env-file` passes values exactly as written, so don't quote them.

- [Required settings](#required-settings)
- [Settings to decide on](#settings-to-decide-on)
- [Models](#models)
- [Thinking, effort and per-workload models](#thinking-effort-and-per-workload-models)
- [Logs](#logs)

## Required settings

Set these before running `setup_aws.sh`. You choose the names; the script creates
the resources.

| Variable | Value |
| --- | --- |
| `CODEKB_AWS_ACCOUNT_ID` | Your account ID. Used by `setup_aws.sh` |
| `CODEKB_S3_BUCKET`, `CODEKB_S3_REGION` | Knowledge-base bucket and its region |
| `CODEKB_S3_VECTORS_BUCKET`, `CODEKB_S3_VECTORS_INDEX`, `CODEKB_S3_VECTORS_REGION` | Vector bucket, index and region |
| `CODEKB_BEDROCK_MODEL_ID`, `CODEKB_BEDROCK_REGION` | Default model and the region to call Bedrock in. See [Models](#models) |
| `CODEKB_ALLOWED_GIT_HOSTS` | Hosts CodeKB may clone from, e.g. `github.com`. Empty disables Git URLs |
| `OTEL_LOGS_EXPORTER` | `none` or `otlp`. See [Logs](#logs) |

Set these from the output of `setup_aws.sh`:

| Variable | Value |
| --- | --- |
| `CODEKB_BEDROCK_GUARDRAIL_ID` | Guardrail ARN. CodeKB won't start without it while the guardrail is on |
| `CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN` | KMS key ARN. Needed to encrypt saved tokens for private repositories |

For sign-in settings, see [Deploying to AWS → Sign-in](deployment.md#sign-in).

## Settings to decide on

| Variable | Default | Change it when |
| --- | --- | --- |
| `CODEKB_REQUIRE_AUTHENTICATION` | `true` | Only `false` for a [local setup](local.md). See [Security](security.md#sign-in) |
| `CODEKB_REQUIRE_AUTHORIZATION` | `true` | `false` lets every signed-in user open every space. See [Security](security.md#space-authorization) |
| `CODEKB_GIT_CREDENTIALS_PROVIDERS` | `github` | Add `gitlab` or `bitbucket`, or set `NONE` for public repositories only. Any other value stops startup |
| `CODEKB_GITHUB_CUSTOM_HOST`, `CODEKB_GITLAB_CUSTOM_HOST`, `CODEKB_BITBUCKET_CUSTOM_HOST` | unset | You run your own Git server, e.g. `https://github.example.com`. Add its host to `CODEKB_ALLOWED_GIT_HOSTS` too |
| `CODEKB_ALLOW_PRIVATE_GIT_ADDRESSES` | `false` | Your Git server's name resolves to a private address |
| `CODEKB_ALLOW_CREDENTIALS_IN_GIT_URL` | `false` | You need private repositories without the KMS key. See [Security](security.md#git-credential-storage) |
| `CODEKB_ZIP_UPLOAD_ENABLED` | `true` | You want Git URLs only |
| `CODEKB_OVERVIEW_GENERATION_ENABLED` | `true` | You want to stop users generating Space and Hyperspace Overviews |
| `CODEKB_EXCLUDE_PATTERNS_EXTRA` | unset | You want to skip more files, e.g. `*.min.js,vendor/`. See [the FAQ](faq.md#how-do-exclusion-patterns-work) |
| `CODEKB_SUMMARIZATION_CONCURRENCY` | `40` in `.env.example`, `1` if unset | Analysis is slow (raise it) or Bedrock throttles (lower it) |
| `CODEKB_PIPELINE_POOL_SIZE` | `32` | Background jobs one process runs at once. Further job requests get HTTP 429. Lower it if memory runs out |
| `CODEKB_MAX_CACHED_SPACES` | `50` | Spaces kept in memory. Lower it when large spaces consume too much memory |
| `CODEKB_SOURCE_CACHE_MAX_SIZE_GB` | `10` | GiB used by extracted source trees under `/tmp/codekb`; active pipeline working trees are additional |
| `CODEKB_SOURCE_CACHE_REFRESH_CHECK_TTL_HOURS` | `4` | How long a replica may trust cached source and knowledge-base data before checking S3 for a refresh by another replica |
| `CODEKB_SUMMARIZATION_FAIL_AFTER_STALE_S` | `43200` | Seconds without progress before the next detailed status check marks an interrupted job failed |
| `CODEKB_BEDROCK_READ_TIMEOUT` | `3600` in `.env.example`, `1800` if unset | Seconds to wait for one Bedrock response |
| `CODEKB_ROOT_PATH` | unset | CodeKB is served under a path prefix. See [Deploying to AWS → Network](deployment.md#network) |
| `CODEKB_LOG_LEVEL` | `WARNING` in `.env.example`, `INFO` if unset | `DEBUG` adds truncated payload dumps; see [Logs](#logs) |
| `CODEKB_THIRD_PARTY_LOG_LEVEL` | `WARNING` | Level for the AWS SDK, HTTP client and Strands loggers. `DEBUG` logs every AWS request and response |

## Models

CodeKB sends every generation request using the Anthropic Messages format. The
supported path is Claude; the default is Claude Opus 4.6:

```bash
CODEKB_BEDROCK_MODEL_ID=us.anthropic.claude-opus-4-6-v1
```

Use an inference-profile prefix (`us.`, `eu.`, `apac.`) available from
`CODEKB_BEDROCK_REGION`. Other Claude models can have different token limits and
accepted thinking settings. CodeKB can parse Qwen-style responses, but the
repository doesn't contain an end-to-end Qwen invocation test, so treat that path
as experimental rather than supported.

`CODEKB_BEDROCK_MAX_TOKENS` (65,000 in `.env.example`) must not exceed the
selected model's output limit. For example, a model capped at 64,000 requires a
lower value. The startup check reports a value the model rejects.

The embedding model, Titan Text Embeddings V2, can't be changed because the
vector index is built for its dimensions.

## Thinking, effort and per-workload models

CodeKB uses the model for four workloads: `SUMMARIZATION` (component
descriptions), `BUSINESS_CONTEXT` (one vocabulary call per analysis), `OVERVIEW`
(Space and Hyperspace Overviews) and `CHAT` (Agent Hopper). Each workload can
override the shared settings:

| Setting | Shared | Per workload |
| --- | --- | --- |
| Model | `CODEKB_BEDROCK_MODEL_ID` | `CODEKB_<WORKLOAD>_MODEL_ID` |
| Max output tokens | `CODEKB_BEDROCK_MAX_TOKENS` | `CODEKB_<WORKLOAD>_MAX_TOKENS` |
| Thinking type | `CODEKB_LLM_THINKING` | `CODEKB_<WORKLOAD>_THINKING` |
| Effort | `CODEKB_LLM_EFFORT` | `CODEKB_<WORKLOAD>_EFFORT` |

- Business context and chat have their own max tokens (2,048 and 16,000), so the
  shared value doesn't change them.
- A workload with its own model doesn't inherit the shared thinking and effort,
  because a value one model accepts can be rejected by another.
- CodeKB sends thinking and effort values to Bedrock unchanged. Which values a
  model accepts is listed under
  [adaptive thinking](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-adaptive-thinking.html)
  in the Bedrock documentation. Thinking tokens are billed as output.
- When the chat's thinking type is `adaptive`, Agent Hopper shows the model's
  summarized reasoning.
- `CODEKB_BEDROCK_REASONING` and `CODEKB_BEDROCK_REASONING_BUDGET_TOKENS` are
  deprecated. Use `CODEKB_CHAT_THINKING`, which wins when both are set.

At startup CodeKB sends one short request per distinct model and settings
combination, and refuses to start if Bedrock rejects one. Missing credentials,
throttling or an outage only log a warning. Set `CODEKB_LLM_STARTUP_CHECK=false`
to skip the requests. The **Settings** page shows each workload's effective
settings.

## Logs

CodeKB writes OpenTelemetry log records. `OTEL_LOGS_EXPORTER` is required and must
be `none` or `otlp`.

- **`none`** (the `.env.example` default): readable lines on the container's
  standard output. Read them with `docker logs -f <container>`.
- **`otlp`** to Amazon CloudWatch: uncomment the `OTEL_EXPORTER_OTLP_*` lines in
  `.env.example` and set the endpoint's region to `CODEKB_S3_REGION`, where
  `setup_aws.sh` created the `/codekb/otel` log group. Requests are signed with
  CodeKB's AWS credentials, and CodeKB won't start if it can't write to the group.
  Read them with `aws logs tail /codekb/otel --follow`.
- **`otlp`** to another backend: set `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` to its URL
  and put its API key in `OTEL_EXPORTER_OTLP_HEADERS`.

In CloudWatch, `x-aws-log-stream` is a prefix. CodeKB creates one stream per
container named `<prefix>/<date>/<container-id>` and starts a new one daily
(`OTEL_CLOUDWATCH_LOG_STREAM_ROTATION_MINUTES`, `0` for never). Retention is set
on the log group.
