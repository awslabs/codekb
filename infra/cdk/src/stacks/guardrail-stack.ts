import * as cdk from "aws-cdk-lib";
import * as bedrock from "aws-cdk-lib/aws-bedrock";
import { Construct } from "constructs";

export interface GuardrailStackProps extends cdk.StackProps {
  /** Lowercased name prefix, e.g. "codekb-alpha" (projectName + stage). */
  resourcePrefix: string;
}

/**
 * Bedrock secret/PII guardrail, in its own stack so it can be deployed to the
 * Bedrock region independently of the app region — a guardrail only works for
 * InvokeModel calls made in the same region. The ARN + version are exposed as
 * CloudFormation outputs; scripts/deploy.sh reads them and passes them to the
 * ServiceStack, so the two stacks share no cross-region reference.
 *
 * All rules are ANONYMIZE: the service attaches this to every generation call
 * to mask committed credentials (built-in PII + the token regexes below) before
 * anything reaches Bedrock, the stored descriptions, or the vector index. A
 * fail-closed IAM Deny in ServiceStack rejects any generation call without it.
 */
export class GuardrailStack extends cdk.Stack {
  /** Full guardrail ARN — matches the GuardrailIdentifier Deny in ServiceStack. */
  public readonly guardrailArn: string;
  /** Guardrail version (always DRAFT, the resource's working version). */
  public readonly guardrailVersion: string;

  constructor(scope: Construct, id: string, props: GuardrailStackProps) {
    super(scope, id, props);

    // Mask on BOTH input and output. Input is the one that matters here: the
    // committed secret is in the source we send as the prompt, so it must be
    // anonymized before it reaches the model. The legacy `action` alone enables
    // output evaluation only, leaving the input unmasked (the secret would still
    // reach Bedrock), so each rule sets the input/output fields explicitly.
    const MASK = {
      action: "ANONYMIZE",
      inputAction: "ANONYMIZE",
      outputAction: "ANONYMIZE",
      inputEnabled: true,
      outputEnabled: true,
    } as const;

    const secretGuardrail = new bedrock.CfnGuardrail(this, "SecretGuardrail", {
      name: `${props.resourcePrefix}-secret-redaction`,
      description:
        "Masks committed secrets/PII in CodeKB text-generation calls.",
      // Required by the API even though every action is ANONYMIZE, not BLOCK;
      // surfaced only if a BLOCK ever fires.
      blockedInputMessaging: "Request blocked by the CodeKB guardrail.",
      blockedOutputsMessaging: "Response blocked by the CodeKB guardrail.",
      sensitiveInformationPolicyConfig: {
        // Credentials/keys + financial & national-ID PII (rare as false
        // positives in source). Omits NAME/ADDRESS/URL/IP/USERNAME — they
        // pervade code and would mangle the summaries.
        piiEntitiesConfig: [
          { type: "AWS_ACCESS_KEY", ...MASK },
          { type: "AWS_SECRET_KEY", ...MASK },
          { type: "PASSWORD", ...MASK },
          { type: "PIN", ...MASK },
          { type: "CREDIT_DEBIT_CARD_NUMBER", ...MASK },
          { type: "CREDIT_DEBIT_CARD_CVV", ...MASK },
          { type: "US_SOCIAL_SECURITY_NUMBER", ...MASK },
          { type: "US_BANK_ACCOUNT_NUMBER", ...MASK },
          { type: "US_BANK_ROUTING_NUMBER", ...MASK },
          { type: "INTERNATIONAL_BANK_ACCOUNT_NUMBER", ...MASK },
          { type: "SWIFT_CODE", ...MASK },
          { type: "EMAIL", ...MASK },
          { type: "PHONE", ...MASK },
        ],
        // Token shapes the built-ins miss. Single-line patterns only; kept near
        // 10 for the per-guardrail regex quota.
        regexesConfig: [
          {
            name: "github-token",
            pattern: "gh[opusr]_[A-Za-z0-9]{36}",
            ...MASK,
          },
          {
            name: "github-pat",
            pattern: "github_pat_[A-Za-z0-9_]{22,}",
            ...MASK,
          },
          { name: "gitlab-pat", pattern: "glpat-[A-Za-z0-9_-]{20}", ...MASK },
          {
            name: "slack-token",
            pattern: "xox[baprs]-[A-Za-z0-9-]{10,}",
            ...MASK,
          },
          {
            name: "google-api-key",
            pattern: "AIza[0-9A-Za-z_-]{35}",
            ...MASK,
          },
          {
            name: "stripe-key",
            pattern: "(sk|rk)_(live|test)_[0-9a-zA-Z]{24,}",
            ...MASK,
          },
          {
            name: "jwt",
            pattern:
              "eyJ[A-Za-z0-9_-]{10,}\\.eyJ[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,}",
            ...MASK,
          },
          { name: "npm-token", pattern: "npm_[A-Za-z0-9]{36}", ...MASK },
          {
            name: "slack-webhook",
            pattern: "https://hooks\\.slack\\.com/services/[A-Za-z0-9/]+",
            ...MASK,
          },
          {
            name: "url-credentials",
            pattern: "[a-zA-Z][a-zA-Z0-9+.-]*://[^\\s:@/]+:[^\\s:@/]{3,}@",
            ...MASK,
          },
        ],
      },
    });

    // ARN (not the short id) so it matches the GuardrailIdentifier Deny
    // condition in ServiceStack. attrVersion is always DRAFT (the resource's
    // working version), sourced from the resource rather than hardcoded.
    this.guardrailArn = secretGuardrail.attrGuardrailArn;
    this.guardrailVersion = secretGuardrail.attrVersion;

    new cdk.CfnOutput(this, "GuardrailArn", { value: this.guardrailArn });
    new cdk.CfnOutput(this, "GuardrailVersion", {
      value: this.guardrailVersion,
    });
  }
}
