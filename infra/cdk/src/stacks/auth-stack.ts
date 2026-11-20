import * as cdk from "aws-cdk-lib";
import * as cognito from "aws-cdk-lib/aws-cognito";
import { Construct } from "constructs";

export interface AuthStackProps extends cdk.StackProps {
  /** Lowercased name prefix, e.g. "codekb-alpha" (projectName + stage). */
  resourcePrefix: string;
  /**
   * Domain the hosted UI redirects back to after login, without a scheme —
   * either a CloudFront distribution domain (e.g. 'd111111abcdef8.cloudfront.net')
   * or a custom domain. The frontend derives its redirect_uri from
   * window.location.origin, so this must match the host the SPA is served from
   * or Cognito rejects the authorize call with redirect_mismatch.
   */
  callbackDomain: string;
  /**
   * Register http://localhost:5173 as a valid callback/logout URL on the app
   * clients, for running the SPA via `vite` locally against this pool. Defaults
   * to false; set it only for a dev deployment, never production.
   */
  allowLocalhostRedirects?: boolean;
  /**
   * Path prefix the SPA is served under, without a trailing slash (e.g.
   * "/app"). Defaults to "" because CloudFront serves the SPA at the origin
   * root; set it only if the container is fronted under a path prefix. The
   * callback and logout URLs registered here must include this prefix, since
   * Cognito matches them exactly against what the SPA sends (see BASE_PATH in
   * the frontend auth module).
   */
  callbackBasePath?: string;

  // ── Adopt an existing pool ──────────────────────────────────────────────
  /**
   * Id of an existing Cognito user pool to adopt. When set, the pool, its
   * users, its hosted-UI domain, and its federation are reused as-is: this
   * stack imports the pool and only creates its OWN web + mcp app clients in
   * it (with callback URLs for THIS deployment's host). It does not create or
   * modify the pool, domain, or identity providers. Omit to create a new pool.
   */
  existingUserPoolId?: string;
  /**
   * Hosted-UI domain URL of the adopted pool, e.g.
   * "https://<prefix>.auth.<region>.amazoncognito.com". Required when
   * existingUserPoolId is set (CDK cannot read it back from an imported pool).
   */
  existingDomainUrl?: string;
  /**
   * Federated IdP name already configured on the adopted pool, if any. Added to
   * the new clients' supported identity providers and surfaced as the hosted-UI
   * hint. Omit for a pool whose own users sign in.
   */
  existingIdpName?: string;

  // ── Create a new pool (ignored when adopting) ───────────────────────────
  /**
   * Hosted-UI domain prefix for a newly created pool. Cognito requires this to
   * be unique across every account in the region. Required when creating a pool.
   */
  domainPrefix?: string;
  /**
   * OIDC client id for an external OIDC identity provider. Omit to deploy a user
   * pool with no federation, where sign-in uses the pool's own users.
   */
  oidcClientId?: string;
  /**
   * Name of the Secrets Manager secret holding the OIDC client secret
   */
  oidcClientSecretName?: string;
  /**
   * Restrict sign-in to the OIDC provider, refusing the user pool's own
   * users. Requires oidcClientId. Defaults to false, which lets users
   * created with AdminCreateUser sign in through the hosted UI.
   */
  oidcOnly?: boolean;
  /**
   * OIDC issuer URL of the identity provider (e.g.
   * "https://accounts.example.com"). Required when oidcClientId is set;
   * Cognito performs discovery at {issuer}/.well-known/openid-configuration.
   */
  oidcIssuer?: string;
  /**
   * Display name for the OIDC IdP in Cognito. This is what the hosted UI's
   * `identity_provider` hint must reference, so it must equal the frontend's
   * VITE_COGNITO_IDP_NAME. Defaults to "OidcIdP".
   */
  oidcProviderName?: string;
  /**
   * Cognito-attribute → IdP-claim mapping. Defaults to the standard OIDC claim
   * names (email, given_name, family_name). Providers that emit non-standard
   * claim names (e.g. all-uppercase) override this.
   */
  oidcAttributeMapping?: Record<string, string>;
}

/**
 * Cognito for CodeKB. Two modes:
 *   • Create (default): a new user pool, hosted-UI domain, optional OIDC
 *     federation, and the web + mcp app clients.
 *   • Adopt (existingUserPoolId set): import an existing pool and create only
 *     this deployment's web + mcp app clients in it. The pool, its users, its
 *     domain, and its IdPs are left untouched.
 *
 * Either way this stack OWNS the two app clients, so their callback URLs are
 * set for this deployment's host and updated by the real-host redeploy.
 */
export class AuthStack extends cdk.Stack {
  public readonly userPool: cognito.IUserPool;
  public readonly userPoolClient: cognito.UserPoolClient;
  public readonly cognitoDomainUrl: string;
  public readonly userPoolId: string;
  public readonly userPoolClientId: string;
  public readonly mcpClientId: string;
  /**
   * Federated IdP name, or undefined when this deployment has no federation.
   * Consumers pass it through to the hosted-UI `identity_provider` hint; when
   * undefined the hosted UI shows its own sign-in form instead of jumping
   * straight to an external IdP.
   */
  public readonly idpName: string | undefined;

  constructor(scope: Construct, id: string, props: AuthStackProps) {
    super(scope, id, props);

    const adopt = !!props.existingUserPoolId;

    // OIDC federation applies only when creating a pool; an adopted pool brings
    // its own identity providers.
    const oidcEnabled = !adopt && !!props.oidcClientId;
    if (oidcEnabled && !props.oidcClientSecretName) {
      throw new Error(
        "AuthStack: oidcClientSecretName is required when oidcClientId is set " +
          "(it names the secret created by the OidcSecretStack).",
      );
    }
    if (!adopt && props.oidcOnly && !oidcEnabled) {
      throw new Error(
        "AuthStack: oidcOnly requires oidcClientId — otherwise no identity " +
          "provider would be able to sign in.",
      );
    }

    // ── The pool: import an existing one, or create a new one ───────────────
    if (adopt) {
      if (!props.existingDomainUrl) {
        throw new Error(
          "AuthStack: existingDomainUrl is required when adopting a pool " +
            "(set CODEKB_COGNITO_DOMAIN_URL=https://<prefix>.auth.<region>.amazoncognito.com).",
        );
      }
      this.userPool = cognito.UserPool.fromUserPoolId(
        this,
        "ImportedUserPool",
        props.existingUserPoolId!,
      );
    } else {
      if (!props.domainPrefix) {
        throw new Error(
          "AuthStack: domainPrefix is required when creating a pool.",
        );
      }
      // User pool — self-signup is always off. Users arrive either through the
      // federated IdP or through AdminCreateUser.
      // NOTE: CDK leaves UserPool on RemovalPolicy.RETAIN, so deleting this stack
      // leaves the pool behind as an orphan that no stack manages.
      this.userPool = new cognito.UserPool(this, "UserPool", {
        userPoolName: `${props.resourcePrefix}-userpool`,
        selfSignUpEnabled: false,
        signInAliases: { email: true },
        standardAttributes: {
          email: { required: true },
        },
      });
    }

    // Federated identity provider (create mode only; generic OIDC). Everything
    // specific to a given provider comes from the deployment via props.
    const idpProviderName = props.oidcProviderName ?? "OidcIdP";
    let identityProvider: cognito.CfnUserPoolIdentityProvider | undefined;

    if (oidcEnabled) {
      if (!props.oidcIssuer) {
        throw new Error(
          "AuthStack: oidcIssuer is required when oidcClientId is set " +
            "(set CODEKB_OIDC_ISSUER=<oidc-issuer-url>).",
        );
      }

      identityProvider = new cognito.CfnUserPoolIdentityProvider(
        this,
        "OidcIdentityProvider",
        {
          providerName: idpProviderName,
          userPoolId: this.userPool.userPoolId,
          providerType: "OIDC",
          attributeMapping: props.oidcAttributeMapping ?? {
            email: "email",
            family_name: "family_name",
            given_name: "given_name",
          },
          providerDetails: {
            attributes_request_method: "GET",
            authorize_scopes: "email openid profile",
            client_id: props.oidcClientId!,
            // Resolve the client secret from Secrets Manager at deploy time.
            // The secret's plain-string value is the OIDC client secret.
            // SecretValue.secretsManager(...) renders a
            // `{{resolve:secretsmanager:…}}` dynamic reference; unsafeUnwrap()
            // yields that reference string (NOT the secret value) so it can sit
            // in this string-typed L1 property. The real secret stays in
            // Secrets Manager and is substituted by CloudFormation during the
            // deployment — it is never written into the synthesized template.
            client_secret: cdk.SecretValue.secretsManager(
              props.oidcClientSecretName!,
            ).unsafeUnwrap(),
            oidc_issuer: props.oidcIssuer,
          },
        },
      );
    }

    // Resolve the federated IdP name this deployment advertises: the one we just
    // created (create mode) or the one already on the adopted pool.
    this.idpName = adopt
      ? props.existingIdpName
      : oidcEnabled
        ? idpProviderName
        : undefined;

    // Which providers the app clients accept. COGNITO is what allows a user
    // created with AdminCreateUser to sign in; without it the hosted UI has
    // nothing to offer but the federated IdP.
    const supportedIdentityProviders: cognito.UserPoolClientIdentityProvider[] =
      [];
    if (this.idpName) {
      supportedIdentityProviders.push(
        cognito.UserPoolClientIdentityProvider.custom(this.idpName),
      );
    }
    // Adopt mode always allows pool-native users; create mode honours oidcOnly.
    if (adopt || !props.oidcOnly) {
      supportedIdentityProviders.push(
        cognito.UserPoolClientIdentityProvider.COGNITO,
      );
    }

    // Callback + logout URLs. basePath is the stage prefix ("/prod") when the
    // SPA is served under a REST API stage, or "" when root-served. These must
    // match the SPA's computed redirect/logout URLs exactly, or Cognito rejects
    // them with redirect_mismatch.
    const basePath = (props.callbackBasePath ?? "").replace(/\/+$/, "");
    const callbackUrls = [
      `https://${props.callbackDomain}${basePath}`,
      `https://${props.callbackDomain}${basePath}/auth/callback`,
    ];
    const logoutUrls = [`https://${props.callbackDomain}${basePath}`];

    // Allow localhost redirects only on explicit opt-in — a dev convenience for
    // running the SPA via `vite` on :5173 against this pool. Gated on a dedicated
    // flag, NOT the stage name, so a production-intended deploy that forgets to
    // pass a stage can never silently register http://localhost:5173 as a valid
    // OAuth redirect on the app clients.
    if (props.allowLocalhostRedirects) {
      callbackUrls.push(
        "http://localhost:5173",
        "http://localhost:5173/auth/callback",
      );
      logoutUrls.push("http://localhost:5173");
    }

    // User Pool Client — authorization code grant only
    this.userPoolClient = this.userPool.addClient("WebAppClient", {
      userPoolClientName: `${props.resourcePrefix}-webapp`,
      oAuth: {
        callbackUrls,
        logoutUrls,
        flows: { authorizationCodeGrant: true, implicitCodeGrant: false },
        scopes: [
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.PROFILE,
        ],
      },
      accessTokenValidity: cdk.Duration.hours(1),
      idTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(30),
      supportedIdentityProviders,
      enableTokenRevocation: true,
      preventUserExistenceErrors: true,
    });

    // Ensure IdP is created before the client references it
    if (identityProvider) {
      this.userPoolClient.node.addDependency(identityProvider);
    }

    // MCP app client — used by MCP clients (Kiro, Claude Code, etc.)
    // Uses callback relay pattern: the authorize proxy rewrites redirect_uri
    // to our own /oauth2/callback endpoint, so Cognito only ever sees this
    // single callback URL. The proxy then relays the auth code to the MCP
    // client's original localhost redirect_uri.
    const mcpCallbackUrls = [
      `https://${props.callbackDomain}${basePath}/oauth2/callback`,
    ];

    const mcpClient = this.userPool.addClient("McpClient", {
      userPoolClientName: `${props.resourcePrefix}-mcp`,
      oAuth: {
        callbackUrls: mcpCallbackUrls,
        logoutUrls: [`https://${props.callbackDomain}${basePath}`],
        flows: { authorizationCodeGrant: true, implicitCodeGrant: false },
        scopes: [
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.PROFILE,
        ],
      },
      accessTokenValidity: cdk.Duration.hours(1),
      idTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(30),
      supportedIdentityProviders,
      enableTokenRevocation: true,
      preventUserExistenceErrors: true,
    });
    if (identityProvider) {
      mcpClient.node.addDependency(identityProvider);
    }
    this.mcpClientId = mcpClient.userPoolClientId;

    // Hosted UI domain: created for a new pool; taken from input when adopting.
    if (adopt) {
      this.cognitoDomainUrl = props.existingDomainUrl!;
    } else {
      const domain = new cognito.UserPoolDomain(this, "CognitoDomain", {
        userPool: this.userPool,
        cognitoDomain: { domainPrefix: props.domainPrefix! },
      });
      // Force hosted UI v1 (classic) — v2 (managed login) requires additional branding config
      const cfnDomain = domain.node.defaultChild as cdk.CfnResource;
      cfnDomain.addPropertyOverride("ManagedLoginVersion", 1);
      this.cognitoDomainUrl = `https://${props.domainPrefix}.auth.${this.region}.amazoncognito.com`;
    }

    this.userPoolId = this.userPool.userPoolId;
    this.userPoolClientId = this.userPoolClient.userPoolClientId;

    // Outputs
    new cdk.CfnOutput(this, "UserPoolId", { value: this.userPoolId });
    new cdk.CfnOutput(this, "UserPoolClientId", {
      value: this.userPoolClientId,
    });
    new cdk.CfnOutput(this, "CognitoDomainUrl", {
      value: this.cognitoDomainUrl,
    });
    new cdk.CfnOutput(this, "McpClientId", { value: this.mcpClientId });
    // CloudFormation rejects an empty output value, so only emit when set. The
    // deploy script reads this (empty when absent) for the SPA's IdP hint.
    if (this.idpName) {
      new cdk.CfnOutput(this, "IdpName", { value: this.idpName });
    }
  }
}
