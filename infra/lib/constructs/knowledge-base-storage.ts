import * as path from 'path';
import { Aws, RemovalPolicy } from 'aws-cdk-lib';
import { AttributeType, BillingMode, Table, TableEncryption } from 'aws-cdk-lib/aws-dynamodb';
import { BlockPublicAccess, Bucket, BucketEncryption, IBucket, ObjectOwnership } from 'aws-cdk-lib/aws-s3';
import { BucketDeployment, Source } from 'aws-cdk-lib/aws-s3-deployment';
import { Construct } from 'constructs';
import { EnvironmentConfig } from '../config';

export interface KnowledgeBaseStorageProps {
  readonly config: EnvironmentConfig;
  readonly prefix: string;
  /** Directory of source documents uploaded to `raw/` at deploy time. */
  readonly sampleDocsPath: string;
}

/**
 * Durable state for the knowledge base agent.
 *
 * Two resources, with different jobs:
 *
 *  - **S3** holds the source documents under `raw/` and the built vector index under
 *    `index/`. The index is a versioned artifact rather than a database (ADR-01), which is
 *    why retrieval costs nothing and why the compute layer is stateless.
 *  - **DynamoDB** holds the query log: one item per request, keyed by session so a
 *    conversation can be reconstructed while debugging.
 */
export class KnowledgeBaseStorage extends Construct {
  public readonly bucket: IBucket;
  public readonly queryLogTable: Table;
  public readonly rawPrefix = 'raw/';
  public readonly indexKey = 'index/kb-index.json.gz';

  constructor(scope: Construct, id: string, props: KnowledgeBaseStorageProps) {
    super(scope, id);

    const { config, prefix, sampleDocsPath } = props;

    // The account and region tokens resolve at deploy time, which is what keeps the same
    // code deployable to more than one account: the name is globally unique per account
    // without anyone editing it.
    this.bucket = new Bucket(this, 'Bucket', {
      bucketName: `${prefix}-${config.envName}-${Aws.ACCOUNT_ID}-${Aws.REGION}`,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      encryption: BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: true,
      objectOwnership: ObjectOwnership.BUCKET_OWNER_ENFORCED,
      removalPolicy: config.removalPolicy,
      autoDeleteObjects: config.autoDeleteObjects,
    });

    // Ships the version-controlled sample documents with the stack, so a single
    // `cdk deploy` produces a system with a populated knowledge base. Document ingestion
    // is intentionally out of scope; this is what replaces it.
    new BucketDeployment(this, 'SampleDocs', {
      sources: [Source.asset(path.resolve(sampleDocsPath))],
      destinationBucket: this.bucket,
      destinationKeyPrefix: this.rawPrefix,
      // Leave anything else in the bucket alone -- notably the built index, which this
      // deployment must never delete.
      prune: false,
      retainOnDelete: false,
    });

    this.queryLogTable = new Table(this, 'QueryLog', {
      tableName: `${prefix}-${config.envName}-query-log`,
      partitionKey: { name: 'session_id', type: AttributeType.STRING },
      sortKey: { name: 'ts_request', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      encryption: TableEncryption.AWS_MANAGED,
      // Query logs are a debugging aid, not a system of record: let them expire.
      timeToLiveAttribute: 'expires_at',
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: config.pointInTimeRecovery,
      },
      removalPolicy: config.removalPolicy,
    });
  }

  /** ARN pattern for the index object, for scoping the query Lambda's read permission. */
  public get indexObjectArn(): string {
    return this.bucket.arnForObjects('index/*');
  }

  /** ARN pattern for the source documents, for scoping the ingest Lambda's read permission. */
  public get rawObjectsArn(): string {
    return this.bucket.arnForObjects(`${this.rawPrefix}*`);
  }
}

/** Re-exported so callers do not need to import from aws-cdk-lib just to read the policy. */
export { RemovalPolicy };
