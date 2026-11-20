import * as cdk from "aws-cdk-lib";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as targets from "aws-cdk-lib/aws-route53-targets";
import { Construct } from "constructs";

export interface EdgeStackProps extends cdk.StackProps {
  /** Lowercased name prefix, e.g. "codekb-alpha" (projectName + stage). */
  resourcePrefix: string;
  /** The internal ALB (ECS Fargate) that serves the API AND the baked-in SPA. */
  loadBalancer: elbv2.IApplicationLoadBalancer;
  /** Optional custom domain (e.g. 'codekb.example.com'); omit for the cloudfront.net domain. */
  domainName?: string;
  /** Optional Route 53 hosted zone for the custom domain (e.g. 'example.com'). */
  hostedZoneName?: string;
  /**
   * ARN of the ACM certificate for the custom domain (issued in us-east-1 by
   * the CertificateStack, imported here via crossRegionReferences). Required
   * when domainName/hostedZoneName are set.
   */
  certificateArn?: string;
  /** WAF WebACL ARN (must be in us-east-1 for CloudFront scope). */
  wafWebAclArn?: string;
}

/**
 * Public front door: a CloudFront distribution with a single VPC origin to the
 * internal ALB. The container serves everything — the SPA shell and assets AND
 * the API/MCP/OAuth endpoints.
 *
 * SPA routing is owned by the container: its catch-all returns index.html for
 * unknown GETs, so there are NO CloudFront error-response rewrites here (those
 * would turn a real API 404 into the SPA shell).
 */
export class EdgeStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: EdgeStackProps) {
    super(scope, id, props);

    // VPC origin — CloudFront reaches the internal ALB through the VPC without
    // the ALB being internet-facing. The ALB's HTTP listener is open within the
    // private VPC, which is what the origin's managed ENIs use.
    const albOrigin = origins.VpcOrigin.withApplicationLoadBalancer(
      props.loadBalancer,
      {
        protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      },
    );

    // Origin request policy: forward query strings, a safe header allowlist, and
    // cookies — but NOT Host (ALL_VIEWER forwards Host, which confuses the ALB).
    // This allowlist is the identity guard: X-User-Identity is not in it, so
    // CloudFront never forwards it to the origin on ANY behavior. An external
    // client therefore cannot spoof identity via that header — the backend
    // establishes the caller from the verified Cognito JWT only.
    const originRequestPolicy = new cloudfront.OriginRequestPolicy(
      this,
      "ApiOriginRequestPolicy",
      {
        originRequestPolicyName: `${props.resourcePrefix}-api-orp`,
        queryStringBehavior: cloudfront.OriginRequestQueryStringBehavior.all(),
        headerBehavior: cloudfront.OriginRequestHeaderBehavior.allowList(
          "Accept",
          "Accept-Language",
          "Content-Type",
          "Origin",
          "Referer",
        ),
        cookieBehavior: cloudfront.OriginRequestCookieBehavior.all(),
      },
    );

    // Cache policy for dynamic API/MCP paths: effectively no caching, but forwards
    // the Authorization header to the origin. Authorization is a restricted header
    // in CloudFront — it can only be forwarded via the cache policy, not the origin
    // request policy — so the token reaches the container's JWT verification. TTL
    // must be >0 or CloudFront rejects a header allowlist.
    const apiCachePolicy = new cloudfront.CachePolicy(this, "ApiCachePolicy", {
      cachePolicyName: `${props.resourcePrefix}-api-cache`,
      defaultTtl: cdk.Duration.seconds(1),
      minTtl: cdk.Duration.seconds(1),
      maxTtl: cdk.Duration.seconds(1),
      headerBehavior: cloudfront.CacheHeaderBehavior.allowList("Authorization"),
      queryStringBehavior: cloudfront.CacheQueryStringBehavior.all(),
      cookieBehavior: cloudfront.CacheCookieBehavior.none(),
    });

    // Custom domain (optional): ACM cert (us-east-1 for CloudFront) + alias record.
    let certificate: acm.ICertificate | undefined;
    let domainNames: string[] | undefined;
    let hostedZone: route53.IHostedZone | undefined;

    if (props.domainName && props.hostedZoneName) {
      // CloudFront requires a viewer certificate whenever custom domainNames are
      // set. Fail fast on a partial prop set rather than letting CloudFront
      // synth fail with an opaque error. (main.ts always supplies the ARN with
      // the domain, so this only guards direct/misconfigured use.)
      if (!props.certificateArn) {
        throw new Error(
          "EdgeStack: certificateArn is required when domainName/hostedZoneName are set — " +
            "CloudFront needs a us-east-1 ACM certificate for a custom domain.",
        );
      }
      hostedZone = route53.HostedZone.fromLookup(this, "HostedZone", {
        domainName: props.hostedZoneName,
      });
      // The cert is issued in a dedicated us-east-1 stack (CloudFront requires
      // us-east-1) and imported here by ARN via crossRegionReferences. Avoids
      // the deprecated acm.DnsValidatedCertificate.
      certificate = acm.Certificate.fromCertificateArn(
        this,
        "SiteCertificate",
        props.certificateArn,
      );
      domainNames = [props.domainName];
    }

    // Dynamic backend behavior (API + MCP): all methods, no caching, Authorization
    // forwarded. Identity spoofing is already blocked by the origin request
    // policy's header allowlist (X-User-Identity is not forwarded on any
    // behavior), so no edge function is needed here.
    const dynamicBehavior = (): cloudfront.BehaviorOptions => ({
      origin: albOrigin,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachePolicy: apiCachePolicy,
      originRequestPolicy,
    });

    // Static SPA asset behavior: GET/HEAD, edge-cached (Vite emits content-hashed,
    // immutable filenames, so caching is safe).
    const staticBehavior = (): cloudfront.BehaviorOptions => ({
      origin: albOrigin,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
      cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      originRequestPolicy,
    });

    const distribution = new cloudfront.Distribution(this, "Distribution", {
      certificate,
      domainNames,
      ...(props.wafWebAclArn && { webAclId: props.wafWebAclArn }),
      // Default: the SPA shell and client-side routes, served by the container.
      // No caching so a new deploy's index.html shows up immediately; the SPA's
      // hashed assets are cached via the /assets/* behavior below.
      defaultBehavior: {
        origin: albOrigin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy,
      },
      additionalBehaviors: {
        "/assets/*": staticBehavior(),
        "/diagrams/*": staticBehavior(),
        "/api/*": dynamicBehavior(),
        "/mcp/*": dynamicBehavior(),
        "/oauth2/*": {
          origin: albOrigin,
          viewerProtocolPolicy:
            cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy,
        },
        "/.well-known/*": {
          origin: albOrigin,
          viewerProtocolPolicy:
            cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy,
        },
      },
    });

    // Route 53 alias records → CloudFront (only with a custom domain).
    if (hostedZone && props.domainName) {
      new route53.ARecord(this, "AliasRecord", {
        zone: hostedZone,
        recordName: props.domainName,
        target: route53.RecordTarget.fromAlias(
          new targets.CloudFrontTarget(distribution),
        ),
      });
      new route53.AaaaRecord(this, "AliasRecordIPv6", {
        zone: hostedZone,
        recordName: props.domainName,
        target: route53.RecordTarget.fromAlias(
          new targets.CloudFrontTarget(distribution),
        ),
      });
    }

    new cdk.CfnOutput(this, "DistributionUrl", {
      value: props.domainName
        ? `https://${props.domainName}`
        : `https://${distribution.distributionDomainName}`,
    });
    new cdk.CfnOutput(this, "DistributionDomainName", {
      value: distribution.distributionDomainName,
    });
  }
}
