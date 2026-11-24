import * as wafv2 from "aws-cdk-lib/aws-wafv2";

/**
 * The WAF rule set shared by CodeKB's WebACLs: a per-IP rate limit plus the AWS
 * managed Common and Known-Bad-Inputs rule groups.
 *
 * The managed groups are scoped DOWN so they never inspect the MCP or OAuth
 * proxy paths. MCP uses JSON-RPC-over-HTTP whose request bodies carry source
 * code and base64 blobs: the literal tokens the generic signatures hunt for
 * (SQL keywords, `<script>`, `../`, `${...}`) and bodies past the managed
 * SizeRestrictions limit, so the group would block legitimate MCP traffic. The
 * OAuth authorize request carries the MCP client's loopback redirect URI
 * (`http://127.0.0.1:<port>/callback`), which the managed Common rules flag and
 * block, breaking the sign-in flow. The OAuth discovery metadata
 * (`/.well-known/oauth-*`) is exempt too: some MCP clients fetch it without a
 * User-Agent, which the Common rules block. `notStatement(or(these paths))` means
 * "apply the managed rules to everything except these".
 *
 * `exemptPrefix` is prepended to the exempt path. It's empty for the CloudFront
 * WebACL, where requests arrive at the origin root; it exists only for a
 * deployment that fronts the app under a path prefix, where WAF would see that
 * prefix in the path.
 */
export function buildWafRules(
  namePrefix: string,
  exemptPrefix = "",
): wafv2.CfnWebACL.RuleProperty[] {
  const prefix = exemptPrefix.replace(/\/+$/, ""); // '' or '/prod'
  const exemptPaths = [
    `${prefix}/mcp/`,
    `${prefix}/oauth2/`,
    `${prefix}/.well-known/oauth-`,
  ];

  // Managed rule groups skip the exempt paths. Shared by both managed rules.
  const scopeDownToExempt: wafv2.CfnWebACL.StatementProperty = {
    notStatement: {
      statement: {
        orStatement: {
          statements: exemptPaths.map((searchString) => ({
            byteMatchStatement: {
              fieldToMatch: { uriPath: {} },
              positionalConstraint: "STARTS_WITH",
              searchString,
              textTransformations: [{ priority: 0, type: "LOWERCASE" }],
            },
          })),
        },
      },
    },
  };

  return [
    {
      name: "RateLimitPerIP",
      priority: 1,
      action: { block: {} },
      statement: {
        rateBasedStatement: {
          limit: 1000, // 1000 requests per 5-minute window per IP
          aggregateKeyType: "IP",
        },
      },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: `${namePrefix}-rate-limit`,
      },
    },
    {
      name: "AWSManagedCommonRuleSet",
      priority: 2,
      overrideAction: { none: {} },
      statement: {
        managedRuleGroupStatement: {
          vendorName: "AWS",
          name: "AWSManagedRulesCommonRuleSet",
          scopeDownStatement: scopeDownToExempt,
        },
      },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: `${namePrefix}-common-rules`,
      },
    },
    {
      name: "AWSManagedKnownBadInputs",
      priority: 3,
      overrideAction: { none: {} },
      statement: {
        managedRuleGroupStatement: {
          vendorName: "AWS",
          name: "AWSManagedRulesKnownBadInputsRuleSet",
          scopeDownStatement: scopeDownToExempt,
        },
      },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: `${namePrefix}-bad-inputs`,
      },
    },
  ];
}
