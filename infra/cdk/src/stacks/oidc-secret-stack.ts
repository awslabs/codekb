import * as cdk from "aws-cdk-lib";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

export interface OidcSecretStackProps extends cdk.StackProps {
  /** Lowercased name prefix, e.g. "codekb-alpha" (projectName + stage). */
  resourcePrefix: string;
  /**
   * Deterministic Secrets Manager secret name. The AuthStack references the
   * same name to build its `{{resolve:secretsmanager:…}}` dynamic reference, so
   * this is the only coupling between the two stacks — there is no cross-stack
   * CloudFormation link (same by-name decoupling Storage/Service already use).
   */
  secretName: string;
}

/**
 * Owns the Secrets Manager secret that holds the external OIDC client secret,
 * so its lifecycle (name, tags, region, teardown) is managed with the
 * deployment instead of depending on an operator-provisioned ARN.
 *
 * The secret VALUE is deliberately not set here. CDK creates the secret with a
 * throwaway generated value, and the operator populates the real client secret
 * out of band — `scripts/deploy.sh` pauses for this on the first deploy. Because the
 * template declares no `SecretString`/`GenerateSecretString` change on updates,
 * CloudFormation never rewrites the value, so later deploys never clobber the
 * populated secret.
 *
 * This stack is deployed BEFORE the AuthStack's OIDC identity provider, which
 * reads the value at deploy time via a dynamic reference — so the secret must
 * already hold the real client secret by the time that provider is created.
 */
export class OidcSecretStack extends cdk.Stack {
  public readonly secretName: string;

  constructor(scope: Construct, id: string, props: OidcSecretStackProps) {
    super(scope, id, props);

    const secret = new secretsmanager.Secret(this, "OidcClientSecret", {
      secretName: props.secretName,
      description:
        `External OIDC client secret for ${props.resourcePrefix}. ` +
        "Populate with the client secret issued by your OIDC provider.",
      // RETAIN so tearing down the stack never destroys the operator-entered
      // secret (matches the user pool / storage convention). Remove it by hand
      // if you really mean to delete it.
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.secretName = props.secretName;

    new cdk.CfnOutput(this, "OidcClientSecretName", {
      value: secret.secretName,
      description:
        "Secrets Manager secret name holding the external OIDC client secret",
    });
    new cdk.CfnOutput(this, "OidcClientSecretArn", {
      value: secret.secretArn,
      description:
        "Secrets Manager secret ARN holding the external OIDC client secret",
    });
  }
}
