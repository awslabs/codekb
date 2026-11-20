import * as cdk from "aws-cdk-lib";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import { Construct } from "constructs";
import { buildWafRules } from "./waf-rules";

export interface WafStackProps extends cdk.StackProps {
  resourcePrefix: string;
}

/**
 * WAF WebACL for CloudFront — must be deployed in us-east-1.
 *
 * CloudFront requires WAF WebACLs to be in us-east-1 regardless of where the
 * distribution or origin stack lives. This stack is deployed separately and its
 * ARN is passed to the EdgeStack via crossRegionReferences.
 *
 * The rule set (per-IP rate limit + AWS managed Common and Known-Bad-Inputs
 * groups) is shared with any regional WebACL via buildWafRules. CloudFront sees
 * the request at the root (no stage segment), so the managed-rule exempt paths
 * (/mcp/ and /oauth2/) carry no prefix — /mcp/ holds JSON-RPC payloads (source
 * code / base64) and /oauth2/ carries the client's loopback redirect URI, both
 * of which the generic signatures would otherwise flag as false positives and
 * block.
 */
export class WafStack extends cdk.Stack {
  public readonly webAclArn: string;

  constructor(scope: Construct, id: string, props: WafStackProps) {
    super(scope, id, props);

    const webAcl = new wafv2.CfnWebACL(this, "CloudFrontWebAcl", {
      scope: "CLOUDFRONT",
      defaultAction: { allow: {} },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: `${props.resourcePrefix}-waf`,
      },
      rules: buildWafRules(props.resourcePrefix, ""),
    });

    this.webAclArn = webAcl.attrArn;

    new cdk.CfnOutput(this, "WebAclArn", { value: this.webAclArn });
  }
}
