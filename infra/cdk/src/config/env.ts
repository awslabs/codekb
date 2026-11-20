/**
 * Environment configuration for the CodeKB CDK stacks.
 *
 * All non-sensitive environment variables that get passed to the
 * ECS task definition live here. Edit this file to change values
 * across deployments without touching stack code.
 *
 * Usage in app.ts:
 *   const config = getEnvConfig('dev');  // uses dev overrides
 *   const config = getEnvConfig();       // uses default
 */

/**
 * Region that CloudFront-scoped resources MUST live in. AWS requires both the
 * CloudFront WAF WebACL and the viewer ACM certificate to be in us-east-1,
 * regardless of the app's region. This is an AWS constraint, not a tunable
 * default — hence a single named constant rather than a scattered literal.
 */
export const CLOUDFRONT_REGION = "us-east-1";

interface BedrockConfig {
  modelId: string;
  region: string;
  maxTokens: string;
  readTimeout: string;
}

interface OtelConfig {
  /** Logs exporter: 'otlp' (CloudWatch/OTLP) or 'none'. The backend accepts
   * no other value and refuses to start on one. */
  logsExporter: "otlp" | "none";
  /** OTLP wire protocol (e.g. 'http/protobuf'). */
  protocol: string;
  /** OTel service.name resource attribute. */
  serviceName: string;
  /**
   * CloudWatch log stream *prefix*. The backend appends the rotation window and
   * a per-container id, so the final stream is `<prefix>/<window>/<instance>`
   * and each task gets its own stream. See codekb/core/observability.py.
   */
  cloudWatchLogStreamPrefix: string;
  /**
   * Minutes per CloudWatch log stream before rolling to a new one.
   * 0 disables time-based rotation (one stream per container, forever).
   */
  cloudWatchLogStreamRotationMinutes: number;
}

interface EnvConfig {
  bedrock: BedrockConfig;
  /** OpenTelemetry log export settings. */
  otel: OtelConfig;
  /** Parallel LLM summarization calls per depth level. */
  summarizationConcurrency: number;
  /** Comma/space-separated Git hosts CodeKB may clone from. */
  allowedGitHosts: string;
  /** Number of ECS Fargate tasks to run (FargateService desiredCount). */
  containerCount: number;
  /** Days to keep noncurrent KB bucket object versions before expiry. */
  kbBucketVersionRetentionDays: number;
}

const envConfig: Record<string, EnvConfig> = {
  default: {
    bedrock: {
      modelId: "us.anthropic.claude-opus-4-6-v1",
      region: "us-east-1",
      maxTokens: "65536",
      readTimeout: "3600",
    },
    otel: {
      logsExporter: "otlp",
      protocol: "http/protobuf",
      serviceName: "codekb",
      cloudWatchLogStreamPrefix: "codekb",
      cloudWatchLogStreamRotationMinutes: 1440,
    },
    summarizationConcurrency: 40,
    allowedGitHosts: "github.com",
    containerCount: 10,
    kbBucketVersionRetentionDays: 90,
  },
  dev: {
    bedrock: {
      modelId: "us.anthropic.claude-opus-4-6-v1",
      region: "us-east-1",
      maxTokens: "65000",
      readTimeout: "3600",
    },
    otel: {
      logsExporter: "otlp",
      protocol: "http/protobuf",
      serviceName: "codekb",
      cloudWatchLogStreamPrefix: "codekb",
      cloudWatchLogStreamRotationMinutes: 1440,
    },
    summarizationConcurrency: 40,
    allowedGitHosts: "github.com",
    containerCount: 1,
    kbBucketVersionRetentionDays: 7,
  },
};

/**
 * Returns the config for the given environment.
 * Falls back to `default` if the environment is not defined.
 */
export function getEnvConfig(environment?: string): EnvConfig {
  return envConfig[environment ?? "default"] ?? envConfig["default"];
}
