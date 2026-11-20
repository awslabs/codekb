import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Construct } from "constructs";

export interface CertificateStackProps extends cdk.StackProps {
  /** Lowercased name prefix, e.g. "codekb-alpha" (projectName + stage). */
  resourcePrefix: string;
  /** FQDN the certificate is issued for (e.g. "codekb.example.com"). */
  domainName: string;
  /** Route 53 hosted zone that owns domainName (e.g. "example.com"). */
  hostedZoneName: string;
}

/**
 * ACM certificate for the CloudFront custom domain.
 *
 * CloudFront requires its viewer certificate in us-east-1 regardless of where
 * the distribution or origin stack lives, so this is a dedicated us-east-1 stack
 * and the EdgeStack imports the ARN via crossRegionReferences. Uses the native
 * `acm.Certificate` with DNS validation (the former `acm.DnsValidatedCertificate`
 * is deprecated — it relied on a custom-resource Lambda AWS no longer recommends).
 */
export class CertificateStack extends cdk.Stack {
  public readonly certificateArn: string;

  constructor(scope: Construct, id: string, props: CertificateStackProps) {
    super(scope, id, props);

    const hostedZone = route53.HostedZone.fromLookup(this, "HostedZone", {
      domainName: props.hostedZoneName,
    });

    const certificate = new acm.Certificate(this, "SiteCertificate", {
      domainName: props.domainName,
      validation: acm.CertificateValidation.fromDns(hostedZone),
    });

    this.certificateArn = certificate.certificateArn;

    new cdk.CfnOutput(this, "CertificateArn", { value: this.certificateArn });
  }
}
