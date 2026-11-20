import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3vectors from "aws-cdk-lib/aws-s3vectors";
import { Construct } from "constructs";

export interface StorageStackProps extends cdk.StackProps {
  /** Lowercased name prefix, e.g. "codekb-alpha" (projectName + stage). */
  resourcePrefix: string;
  /** Days to keep noncurrent KB bucket object versions before S3 expires them. */
  kbBucketVersionRetentionDays: number;
}

/**
 * S3 buckets for knowledge base JSON files and S3 Vectors for embeddings.
 */
export class StorageStack extends cdk.Stack {
  public readonly kbBucket: s3.IBucket;
  public readonly kbBucketName: string;
  public readonly vectorBucket: s3vectors.CfnVectorBucket;
  public readonly vectorIndex: s3vectors.CfnIndex;
  public readonly vectorBucketName: string;
  public readonly vectorIndexName: string;

  constructor(scope: Construct, id: string, props: StorageStackProps) {
    super(scope, id, props);

    // Knowledge base JSON + source-tarball storage. This stack assumes a fresh
    // account and creates the bucket; RETAIN keeps it (and its data) if the
    // stack is torn down. A deployment that manages this bucket elsewhere skips
    // this stack entirely and points the service at the bucket by name via
    // CODEKB_S3_BUCKET (the service references it by name, not by reference, so
    // there is no CloudFormation link). See docs/deployment.md.
    // Server access logs for the KB bucket land in a dedicated logging bucket.
    // S3 does not let a bucket log to itself, so this bucket has no access
    // logging of its own (expected). RETAIN keeps the logs if the stack is
    // torn down, matching the KB bucket.
    const accessLogsBucket = new s3.Bucket(this, "AccessLogsBucket", {
      bucketName: `${props.resourcePrefix}-s3-access-logs-${this.account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.kbBucketName = `${props.resourcePrefix}-knowledgebase-${this.account}`;
    this.kbBucket = new s3.Bucket(this, "KnowledgeBaseBucket", {
      bucketName: this.kbBucketName,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      serverAccessLogsBucket: accessLogsBucket,
      serverAccessLogsPrefix: "knowledgebase/",
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      // Recover overwritten/deleted KB objects; expire old versions to cap cost.
      versioned: true,
      lifecycleRules: [
        {
          noncurrentVersionExpiration: cdk.Duration.days(
            props.kbBucketVersionRetentionDays,
          ),
        },
      ],
      // The "Upload zip" flow PUTs the source archive straight to a presigned
      // S3 URL from the browser — a cross-origin request that S3 rejects
      // without a CORS rule (the upload fails with "Failed to fetch"). The
      // presign pins Content-Type: application/zip, which is not a
      // CORS-safelisted value, so the browser sends a preflight that needs PUT
      // and the content-type header allowed. Mirrors customer-assets/setup_aws.sh
      // (the local/customer flow). AllowedOrigins is '*' because Storage is
      // deliberately decoupled from the Edge stack, so the CloudFront domain is
      // not known here; scoping it would couple the two stacks. Access is still
      // gated by the short-lived presigned URL, not the CORS origin.
      cors: [
        {
          allowedMethods: [s3.HttpMethods.PUT, s3.HttpMethods.GET],
          allowedOrigins: ["*"],
          allowedHeaders: ["*"],
          maxAge: 3600,
        },
      ],
    });

    // S3 Vectors bucket for embeddings, encrypted with SSE-S3 (AES256).
    this.vectorBucketName = `${props.resourcePrefix}-vectors-${this.account}`;
    this.vectorBucket = new s3vectors.CfnVectorBucket(this, "VectorBucket", {
      vectorBucketName: this.vectorBucketName,
      encryptionConfiguration: { sseType: "AES256" },
    });
    this.vectorBucket.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    // Vector index (1024-dim for Amazon Titan Text Embeddings V2, cosine similarity)
    this.vectorIndexName = `${props.resourcePrefix}-index`;
    this.vectorIndex = new s3vectors.CfnIndex(this, "VectorIndex", {
      vectorBucketName: this.vectorBucketName,
      indexName: this.vectorIndexName,
      dataType: "float32",
      dimension: 1024,
      distanceMetric: "cosine",
      metadataConfiguration: {
        nonFilterableMetadataKeys: [
          "source",
          "timestamp",
          "content",
          "description",
          "call_graph_text",
        ],
      },
    });
    this.vectorIndex.addResourceDependency(this.vectorBucket);
    this.vectorIndex.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    // Outputs
    new cdk.CfnOutput(this, "KBBucketName", {
      value: this.kbBucketName,
    });
    new cdk.CfnOutput(this, "VectorBucketName", {
      value: this.vectorBucketName,
    });
    new cdk.CfnOutput(this, "VectorIndexName", {
      value: this.vectorIndexName,
    });
  }
}
