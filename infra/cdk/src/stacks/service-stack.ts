import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";

/**
 * Locate the repository root (the directory holding the Dockerfile and
 * backend/) by walking up from this file. Done by marker rather than a fixed
 * number of "../" segments because the compiled JS runs from dist/ (one level
 * deeper than the TypeScript source), so a hardcoded depth breaks at runtime.
 */
function findRepoRoot(start: string): string {
  let dir = start;
  for (let i = 0; i < 12; i++) {
    if (
      fs.existsSync(path.join(dir, "Dockerfile")) &&
      fs.existsSync(path.join(dir, "backend"))
    ) {
      return dir;
    }
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error(
    `Could not find the repository root (a directory containing both Dockerfile ` +
      `and backend/) above ${start}.`,
  );
}

export interface ServiceStackProps extends cdk.StackProps {
  /** Lowercased name prefix, e.g. "codekb-alpha" (projectName + stage). */
  resourcePrefix: string;
  /** KB bucket name (referenced by name so Service has no CFN link to Storage). */
  kbBucketName: string;
  vectorBucketName: string;
  vectorIndexName: string;
  bedrockModelId: string;
  bedrockRegion: string;
  bedrockMaxTokens: string;
  bedrockReadTimeout: string;
  /**
   * Per-workload LLM settings (CODEKB_LLM_* and CODEKB_<WORKLOAD>_* env vars)
   * to set on the container as-is. Unset names are omitted, so the backend's
   * defaults apply. Default: none.
   */
  llmEnvironment?: Record<string, string>;
  /**
   * Whether the secret/PII guardrail is on. Default true. When false, the task
   * gets CODEKB_BEDROCK_GUARDRAIL_ENABLED=false, no guardrail id/version, and
   * the ApplyGuardrail grant + fail-closed Deny are dropped — so generation runs
   * unredacted. Off is a deliberate, not-recommended choice.
   */
  guardrailEnabled?: boolean;
  /**
   * ARN of the secret/PII guardrail, owned by the GuardrailStack in the Bedrock
   * region. Used both as the GuardrailIdentifier the backend attaches to every
   * generation call and as the resource the fail-closed Deny keys on. Ignored
   * when guardrailEnabled is false.
   */
  guardrailArn: string;
  /** Guardrail version (DRAFT) passed to the backend. Ignored when disabled. */
  guardrailVersion: string;
  /** Override region for S3 KB bucket (cross-region access). Defaults to stack region. */
  s3Region?: string;
  /** Override region for S3 Vectors (cross-region access). Defaults to stack region. */
  s3VectorsRegion?: string;
  /** Cognito user pool ID for JWT verification. */
  cognitoUserPoolId?: string;
  /** Region of the Cognito user pool. Defaults to the stack region (set this when adopting a pool in another region). */
  cognitoRegion?: string;
  /**
   * Docker build args forwarded to the image's SPA build stage (VITE_* values
   * inlined into the frontend bundle). Populated by scripts/deploy.sh from the Auth
   * stack outputs. Omit for an auth-less UI build.
   */
  spaBuildArgs?: Record<string, string>;
  /** Cognito domain URL for OAuth metadata. */
  cognitoDomainUrl?: string;
  /** Cognito MCP client ID for MCP OAuth flow. */
  cognitoMcpClientId?: string;
  /** Comma-separated Cognito app client IDs for audience validation. */
  cognitoAppClientIds?: string;
  /** Optional federated IdP name passed to the Cognito hosted-UI */
  cognitoIdpName?: string;
  /**
   * Space-level authorization (CODEKB_REQUIRE_AUTHORIZATION), ON by default.
   * Pass false to disable it. Only has effect when auth is active — a token's
   * identity is what gets authorized.
   */
  requireAuthorization?: boolean;
  /** Public domain name for constructing OAuth metadata URLs. */
  publicDomain?: string;
  /** Comma/space-separated Git host allowlist (SSRF hardening). */
  allowedGitHosts?: string;
  /**
   * Root log level for the container (DEBUG|INFO|WARNING|ERROR). DEBUG also dumps
   * full REST and AWS SDK payloads and raises the botocore/boto3/httpx/strands
   * loggers. Signatures and tokens are redacted before a record leaves the
   * process. Default: INFO.
   */
  logLevel?: string;
  /** Per-payload character cap when verbose logging is on. 0 = unlimited. Default: 4000. */
  logPayloadMax?: number;
  /**
   * Number of parallel LLM summarization calls per depth level.
   * Connection pools auto-size to match.
   */
  summarizationConcurrency: number;
  /** Number of ECS Fargate tasks to run (FargateService desiredCount). */
  containerCount: number;
  /** OpenTelemetry logs exporter: 'otlp' (CloudWatch/OTLP) or 'none'. */
  otelLogsExporter: "otlp" | "none";
  /** OTLP wire protocol (e.g. 'http/protobuf'). */
  otelProtocol: string;
  /** OTel service.name resource attribute. */
  otelServiceName: string;
  /**
   * CloudWatch log stream *prefix* for exported OTel logs. The backend appends
   * the rotation window and a per-container id and creates each stream itself,
   * so the final name is `<prefix>/<window>/<instance>`.
   */
  otelCloudWatchLogStreamPrefix: string;
  /**
   * Minutes per CloudWatch log stream before rolling over. 0 disables rotation.
   * Sets OTEL_CLOUDWATCH_LOG_STREAM_ROTATION_MINUTES, which the backend reads only on the
   * CloudWatch path — both of these props are inert against a vendor endpoint.
   */
  otelCloudWatchLogStreamRotationMinutes: number;
  /** OTLP logs endpoint. Defaults to the CloudWatch OTLP endpoint for the stack region. */
  otelLogsEndpoint?: string;
  /** OTel deployment.environment resource attribute. Defaults to the resource prefix. */
  otelDeploymentEnvironment?: string;
}

/**
 * Unified ECS Fargate service: FastAPI (REST API + analysis worker).
 *
 * Same code as local `uvicorn` — no Lambda, no Mangum, no API Gateway.
 * CloudFront terminates TLS at the edge and routes /api/* to this ALB.
 */
export class ServiceStack extends cdk.Stack {
  public readonly loadBalancer: elbv2.ApplicationLoadBalancer;
  public readonly serviceUrl: string;

  constructor(scope: Construct, id: string, props: ServiceStackProps) {
    super(scope, id, props);

    // VPC
    const vpc = new ec2.Vpc(this, "Vpc", {
      maxAzs: 2,
      natGateways: 1,
      // Keep S3 off NAT: KB/vector objects and ECR image layers ride the
      // gateway endpoint (free, route-based) instead of egressing to the internet.
      gatewayEndpoints: {
        S3: { service: ec2.GatewayVpcEndpointAwsService.S3 },
      },
    });

    // Hot-path AWS APIs over interface endpoints so they skip NAT. Omitted:
    // STS (task-role creds come from the container provider, not STS) and
    // S3 Vectors (no CDK constant, often cross-region).
    for (const [id, service] of [
      [
        "BedrockRuntimeEndpoint",
        ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
      ],
      ["EcrApiEndpoint", ec2.InterfaceVpcEndpointAwsService.ECR],
      ["EcrDockerEndpoint", ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER],
      [
        "CloudWatchLogsEndpoint",
        ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
      ],
      ["KmsEndpoint", ec2.InterfaceVpcEndpointAwsService.KMS],
    ] as const) {
      vpc.addInterfaceEndpoint(id, { service });
    }

    // Replace CDK's default allow-all egress. 443 is the floor an SG can
    // enforce: git hosts are shifting CDN IPs, so host allowlisting lives in
    // the app (CODEKB_ALLOWED_GIT_HOSTS); the SG only bounds port/protocol.
    const taskSecurityGroup = new ec2.SecurityGroup(this, "TaskSg", {
      vpc,
      description: "CodeKB Fargate task egress: HTTPS + DNS only.",
      allowAllOutbound: false,
    });
    // HTTPS carries everything outbound: AWS APIs (VPC endpoints or NAT) and git.
    taskSecurityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "HTTPS to AWS services (VPC endpoints or NAT) and git hosts",
    );
    // Without DNS egress nothing resolves — not the endpoints, not the git hosts.
    taskSecurityGroup.addEgressRule(
      ec2.Peer.ipv4(vpc.vpcCidrBlock),
      ec2.Port.udp(53),
      "DNS (UDP) to the VPC resolver",
    );
    taskSecurityGroup.addEgressRule(
      ec2.Peer.ipv4(vpc.vpcCidrBlock),
      ec2.Port.tcp(53),
      "DNS (TCP) to the VPC resolver",
    );

    // ECS Cluster
    const cluster = new ecs.Cluster(this, "Cluster", {
      clusterName: props.resourcePrefix,
      vpc,
      // Container Insights with enhanced observability: adds per-container/task
      // granular metrics and drilldown in the CloudWatch console.
      containerInsightsV2: ecs.ContainerInsights.ENHANCED,
    });

    // Task definition
    const taskDef = new ecs.FargateTaskDefinition(this, "TaskDef", {
      family: `${props.resourcePrefix}-service`,
      cpu: 4096,
      memoryLimitMiB: 16384,
      ephemeralStorageGiB: 50,
    });

    // Container image — the canonical repo-root Dockerfile, built with the
    // repository root as context. This is the exact image scripts/build-image.sh
    // builds; the Dockerfile is the single source of truth.
    //
    // The Dockerfile is multi-stage: it builds the SPA in a Node stage and bakes
    // it into the backend image, so the container always serves the UI (no
    // CloudFront or S3 site bucket, and no host-side staging step). The SPA's
    // deploy-time values (Cognito domain / client id / IdP) are passed as Docker
    // build args in `spaBuildArgs`, which scripts/deploy.sh populates from the Auth
    // stack outputs. The repo-root .dockerignore keeps the context minimal.
    //
    // Resolved by marker, so it is correct whether this runs from dist/ (the
    // normal `cdk.json` path) or from source.
    const repoRoot = findRepoRoot(__dirname);

    // Dedicated CloudWatch log group for OpenTelemetry log export. The backend
    // exports OTLP logs to the CloudWatch OTLP endpoint (SigV4 from the task
    // role) into this group. Separate from the container stdout log group.
    //
    // Only the GROUP is created here. Streams are created at runtime by the
    // backend (logs:CreateLogStream, granted below via grantWrite) because the
    // stream name embeds a rotation window and a per-container id that CDK
    // cannot know at synth time. Retention is a group-level property, so it
    // applies to every rotated stream underneath it.
    const otelLogGroup = new logs.LogGroup(this, "OtelLogs", {
      logGroupName: `/ecs/${props.resourcePrefix}/otel`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // KMS key (KEK) that wraps per-user git credentials.
    // RETAINED on stack deletion: every stored token is sealed under this key,
    // so losing it makes existing credentials undecryptable and forces every
    // user to reconnect. Key rotation is safe (KMS keeps prior key material).
    const gitCredsKey = new kms.Key(this, "GitCredentialsKey", {
      alias: `alias/${props.resourcePrefix}-git-creds-kek`,
      description:
        "CodeKB: wraps per-user git credentials (envelope encryption).",
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      // Two statements, each action enumerated: the account administers, the
      // task role uses.
      policy: new iam.PolicyDocument({
        statements: [
          // A KMS key policy must name the account for IAM policies on this key
          // to take effect, so this statement bounds what IAM can delegate:
          // administration of the key, and reading or revoking grants on it.
          new iam.PolicyStatement({
            sid: "KeyAdministration",
            effect: iam.Effect.ALLOW,
            principals: [new iam.AccountRootPrincipal()],
            actions: [
              // Policy repair — keeps this list editable after deploy.
              "kms:GetKeyPolicy",
              "kms:PutKeyPolicy",
              // Metadata, applied by CloudFormation on stack update.
              "kms:DescribeKey",
              "kms:UpdateKeyDescription",
              "kms:TagResource",
              "kms:UntagResource",
              "kms:ListResourceTags",
              // Lifecycle.
              "kms:EnableKey",
              "kms:DisableKey",
              "kms:ScheduleKeyDeletion",
              "kms:CancelKeyDeletion",
              // Rotation — enableKeyRotation below is applied through these.
              "kms:GetKeyRotationStatus",
              "kms:EnableKeyRotation",
              "kms:DisableKeyRotation",
              "kms:RotateKeyOnDemand",
              "kms:ListKeyRotations",
              // Alias operations need permission on the key as well as the alias.
              "kms:CreateAlias",
              "kms:UpdateAlias",
              "kms:DeleteAlias",
              // Grant oversight: see a grant, and remove it.
              "kms:ListGrants",
              "kms:RevokeGrant",
            ],
            // '*' applies to the KMS key this policy is attached to, not to
            // every KMS key in the account.
            resources: ["*"],
          }),
          // GenerateDataKey seals a credential, Decrypt unwraps it — the two
          // calls GitCredentialStore makes. DescribeKey is read-only metadata,
          // used by the infrastructure readiness probe (/api/config) to confirm
          // the key is reachable; without it the probe reports the key missing.
          new iam.PolicyStatement({
            sid: "KeyUsageByTaskRole",
            effect: iam.Effect.ALLOW,
            principals: [taskDef.taskRole],
            actions: ["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"],
            // '*' applies to the KMS key this policy is attached to, not to
            // every KMS key in the account.
            resources: ["*"],
          }),
        ],
      }),
    });

    // Guardrail lives in its own stack in the Bedrock region (only same-region
    // InvokeModel calls can use it). Its ARN/version arrive via props, read from
    // that stack's outputs by scripts/deploy.sh. Used in the task env and Deny below.
    // When disabled, none of that is wired and generation runs unredacted.
    const guardrailEnabled = props.guardrailEnabled ?? true;
    const guardrailArn = props.guardrailArn;
    const guardrailVersion = props.guardrailVersion;

    // The ARN can be empty at synth (the stack is synthesized before
    // scripts/deploy.sh reads it); the IAM below fails closed on that, not open.
    const hasGuardrailArn = guardrailEnabled && guardrailArn.length > 0;

    // Container — runs FastAPI via uvicorn, serving both the API and the SPA
    taskDef.addContainer("api", {
      containerName: "codekb-api",
      image: ecs.ContainerImage.fromAsset(repoRoot, {
        file: "Dockerfile",
        // Context filtering is in the repo-root .dockerignore (shared with
        // scripts/build-image.sh). Build args inline the SPA's deploy-time Cognito
        // values; omitted when auth is off (an auth-less UI build).
        ...(props.spaBuildArgs && { buildArgs: props.spaBuildArgs }),
      }),
      portMappings: [{ containerPort: 8000 }],
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "codekb",
        logGroup: new logs.LogGroup(this, "ServiceLogs", {
          logGroupName: `/ecs/${props.resourcePrefix}/service`,
          retention: logs.RetentionDays.TWO_WEEKS,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
      }),
      environment: {
        CODEKB_STORAGE_BACKEND: "s3_vectors",
        CODEKB_S3_BUCKET: props.kbBucketName,
        CODEKB_S3_REGION: props.s3Region ?? this.region,
        CODEKB_S3_VECTORS_BUCKET: props.vectorBucketName,
        CODEKB_S3_VECTORS_INDEX: props.vectorIndexName,
        CODEKB_S3_VECTORS_REGION: props.s3VectorsRegion ?? this.region,
        CODEKB_LOG_LEVEL: props.logLevel ?? "INFO",
        CODEKB_LOG_PAYLOAD_MAX: String(props.logPayloadMax ?? 4000),
        // Bedrock model configuration
        CODEKB_BEDROCK_MODEL_ID: props.bedrockModelId,
        CODEKB_BEDROCK_REGION: props.bedrockRegion,
        CODEKB_BEDROCK_MAX_TOKENS: props.bedrockMaxTokens,
        CODEKB_BEDROCK_READ_TIMEOUT: props.bedrockReadTimeout,
        // Per-workload model, thinking and effort settings from the deploy shell.
        ...props.llmEnvironment,
        // Guardrail for the backend. When on, pass the id (the ARN, not the
        // short id, so it matches the Deny condition below) and version (DRAFT,
        // the resource's working version). When off, say so explicitly so the
        // backend skips it instead of failing startup on the missing id.
        ...(guardrailEnabled
          ? {
              CODEKB_BEDROCK_GUARDRAIL_ID: guardrailArn,
              CODEKB_BEDROCK_GUARDRAIL_VERSION: guardrailVersion,
            }
          : { CODEKB_BEDROCK_GUARDRAIL_ENABLED: "false" }),
        // Summarization concurrency (parallel LLM calls per depth level).
        CODEKB_SUMMARIZATION_CONCURRENCY: String(
          props.summarizationConcurrency,
        ),
        // Cognito auth
        ...(props.cognitoUserPoolId && {
          CODEKB_COGNITO_USER_POOL_ID: props.cognitoUserPoolId,
          CODEKB_COGNITO_REGION:
            props.cognitoRegion ?? cdk.Stack.of(this).region,
        }),
        ...(props.cognitoDomainUrl && {
          CODEKB_COGNITO_DOMAIN_URL: props.cognitoDomainUrl,
        }),
        // Federated IdP hint — omitted when unset so deployments without a
        // federated IdP get the Cognito IdP picker. The value is supplied by the deployment, not
        // hardcoded here.
        ...(props.cognitoIdpName && {
          CODEKB_COGNITO_IDP_NAME: props.cognitoIdpName,
        }),
        ...(props.cognitoMcpClientId && {
          CODEKB_COGNITO_MCP_CLIENT_ID: props.cognitoMcpClientId,
        }),
        ...(props.cognitoAppClientIds && {
          CODEKB_COGNITO_APP_CLIENT_IDS: props.cognitoAppClientIds,
        }),
        ...(props.publicDomain && {
          CODEKB_PUBLIC_DOMAIN: props.publicDomain,
        }),
        // Git host allowlist (SSRF hardening). Omitted when unset so the
        // deployment falls back to internal-address blocking only.
        ...(props.allowedGitHosts && {
          CODEKB_ALLOWED_GIT_HOSTS: props.allowedGitHosts,
        }),
        // ARN of the KMS key used to encrypt stored git credentials.
        CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN: gitCredsKey.keyArn,
        // Auth is required by default in ECS — fail-closed if Cognito isn't configured.
        // This prevents silent degradation to no-auth from a missing env var.
        CODEKB_REQUIRE_AUTH: "true",
        // Space-level authorization is on by default in the backend. Emit the
        // resolved value explicitly so CODEKB_REQUIRE_AUTHORIZATION=false can
        // actually disable it — an absent var would fall back to the on default.
        CODEKB_REQUIRE_AUTHORIZATION:
          props.requireAuthorization === false ? "false" : "true",
        // OpenTelemetry log export — exports OTLP logs directly to the CloudWatch
        // OTLP endpoint, authenticated with SigV4 from the task role (no secret).
        // Configured entirely through standard OTEL_* variables, so the backend
        // can target any OTLP endpoint without code changes. The log group/stream
        // headers are routing values, not credentials. See codekb/core/observability.py.
        OTEL_LOGS_EXPORTER: props.otelLogsExporter,
        OTEL_EXPORTER_OTLP_PROTOCOL: props.otelProtocol,
        OTEL_EXPORTER_OTLP_LOGS_ENDPOINT:
          props.otelLogsEndpoint ??
          `https://logs.${this.region}.amazonaws.com/v1/logs`,
        // x-aws-log-stream is a stream PREFIX, not the final stream name — the
        // backend appends `<window>/<instance>` and creates the stream.
        OTEL_EXPORTER_OTLP_HEADERS: `x-aws-log-group=${otelLogGroup.logGroupName},x-aws-log-stream=${props.otelCloudWatchLogStreamPrefix}`,
        OTEL_CLOUDWATCH_LOG_STREAM_ROTATION_MINUTES: String(
          props.otelCloudWatchLogStreamRotationMinutes,
        ),
        OTEL_AWS_APPLICATION_SIGNALS_ENABLED: "false",
        OTEL_SERVICE_NAME: props.otelServiceName,
        OTEL_RESOURCE_ATTRIBUTES: `deployment.environment=${props.otelDeploymentEnvironment ?? props.resourcePrefix}`,
      },
      healthCheck: {
        command: [
          "CMD-SHELL",
          "curl -f http://localhost:8000/api/health || exit 1",
        ],
        interval: cdk.Duration.seconds(120),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
      },
    });

    // S3 access — object ops via grantReadWrite. Referenced by name (not the
    // Storage bucket object) so this stack has no CloudFormation dependency on
    // Storage: the bucket may be created by Storage or pre-exist in the account.
    s3.Bucket.fromBucketName(
      this,
      "KbBucket",
      props.kbBucketName,
    ).grantReadWrite(taskDef.taskRole);

    // The task role's encrypt/decrypt access is granted by the key policy above
    // (KeyUsageByTaskRole) — no IAM grant needed for a same-account key.

    // S3 Vectors access — scoped to this deployment's vector bucket and index.
    const s3VectorsRegion = props.s3VectorsRegion ?? this.region;
    const vectorBucketArn = `arn:${this.partition}:s3vectors:${s3VectorsRegion}:${this.account}:bucket/${props.vectorBucketName}`;
    const vectorIndexArn = `${vectorBucketArn}/index/${props.vectorIndexName}`;

    // Bucket-level policy
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["s3vectors:GetVectorBucket"],
        resources: [vectorBucketArn],
      }),
    );

    // Index-level policy
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "s3vectors:GetIndex",
          "s3vectors:QueryVectors",
          "s3vectors:GetVectors",
          "s3vectors:PutVectors",
          "s3vectors:DeleteVectors",
          "s3vectors:ListVectors",
        ],
        resources: [vectorIndexArn],
      }),
    );

    // Bedrock access for summarization, agent, and embeddings
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: [
          // AWS-owned base models: account is always empty, and region stays
          // wildcard so cross-region profiles can reach their sibling regions.
          "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
          "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0",
          // Inference profiles live in this account/region, so pin both. One
          // entry covers system (us./eu./apac.) and application profiles.
          `arn:aws:bedrock:${props.bedrockRegion}:${this.account}:inference-profile/*`,
        ],
      }),
    );

    // Guardrail IAM — only when enabled. Off drops both the ApplyGuardrail grant
    // and the fail-closed Deny, so generation runs unredacted (deliberate, not
    // recommended). Keeping the Deny with the guardrail off would deny every
    // generation call instead.
    if (guardrailEnabled) {
      // Allow the task to apply the guardrail. Skipped without a real ARN —
      // the resource would be invalid and the Deny below blocks everything anyway.
      if (hasGuardrailArn) {
        taskDef.taskRole.addToPrincipalPolicy(
          new iam.PolicyStatement({
            actions: ["bedrock:ApplyGuardrail"],
            resources: [guardrailArn],
          }),
        );
      }

      // Fail closed: deny generation that doesn't carry the guardrail (Titan
      // embeddings are exempt — they embed already-redacted text). Trailing "*"
      // allows the optional ":version". With no ARN, a sentinel is used so the
      // condition matches every real identifier and denies all generation,
      // rather than `${""}*` = "*", which would match everything and fail open.
      const denyGuardrailIdentifier = hasGuardrailArn
        ? `${guardrailArn}*`
        : "codekb-guardrail-arn-not-configured";
      taskDef.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: "BedrockRequireGuardrailOnGeneration",
          effect: iam.Effect.DENY,
          actions: [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
          ],
          resources: [
            "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
            "arn:aws:bedrock:*::foundation-model/qwen.*",
            `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
            "arn:aws:bedrock:*:*:inference-profile/apac.*",
            "arn:aws:bedrock:*:*:inference-profile/eu.*",
            "arn:aws:bedrock:*:*:inference-profile/us.*",
          ],
          conditions: {
            StringNotLike: {
              "bedrock:GuardrailIdentifier": denyGuardrailIdentifier,
            },
          },
        }),
      );
    }

    // CloudWatch Logs — OpenTelemetry log export via the OTLP endpoint (SigV4).
    // grantWrite covers logs:CreateLogStream and logs:PutLogEvents on this group,
    // which is the whole set: the backend creates streams itself, and the AWS
    // Distro for OpenTelemetry (ADOT) exporter reaches the endpoint over signed
    // HTTP without an AWS SDK client.
    otelLogGroup.grantWrite(taskDef.taskRole);

    // ALB — internal, only reachable via CloudFront VPC origin
    this.loadBalancer = new elbv2.ApplicationLoadBalancer(this, "ALB", {
      vpc,
      internetFacing: false,
      loadBalancerName: `${props.resourcePrefix}-alb`,
      idleTimeout: cdk.Duration.seconds(150),
    });

    // Fargate service
    const service = new ecs.FargateService(this, "Service", {
      cluster,
      taskDefinition: taskDef,
      desiredCount: props.containerCount,
      assignPublicIp: false,
      securityGroups: [taskSecurityGroup],
      healthCheckGracePeriod: cdk.Duration.seconds(600),
      // Abort + roll back a failing deploy instead of retrying for ~3 hours.
      circuitBreaker: { rollback: true },
      // Full count stays up during deploys; new tasks start before old drain.
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
    });

    // HTTP listener → Fargate
    const listener = this.loadBalancer.addListener("HttpListener", {
      port: 80,
    });

    listener.addTargets("ApiTarget", {
      port: 8000,
      targets: [service],
      stickinessCookieDuration: cdk.Duration.minutes(10),
      healthCheck: {
        path: "/api/health",
        interval: cdk.Duration.seconds(120),
        healthyThresholdCount: 2,
      },
    });

    this.serviceUrl = `http://${this.loadBalancer.loadBalancerDnsName}`;

    // Outputs
    new cdk.CfnOutput(this, "ServiceUrl", { value: this.serviceUrl });
    new cdk.CfnOutput(this, "ClusterArn", { value: cluster.clusterArn });
    new cdk.CfnOutput(this, "GitCredentialsKeyArn", {
      value: gitCredsKey.keyArn,
    });
  }
}
