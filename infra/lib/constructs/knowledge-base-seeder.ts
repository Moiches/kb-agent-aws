import * as path from 'path';
import { Duration } from 'aws-cdk-lib';
import { PolicyStatement } from 'aws-cdk-lib/aws-iam';
import { Code, Runtime, Tracing } from 'aws-cdk-lib/aws-lambda';
import { LogGroup } from 'aws-cdk-lib/aws-logs';
import { SqsEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import { Bucket, EventType, IBucket } from 'aws-cdk-lib/aws-s3';
import { SqsDestination } from 'aws-cdk-lib/aws-s3-notifications';
import { Queue } from 'aws-cdk-lib/aws-sqs';
import { ISecret } from 'aws-cdk-lib/aws-secretsmanager';
import { TriggerFunction } from 'aws-cdk-lib/triggers';
import { Construct, IConstruct } from 'constructs';
import { EnvironmentConfig, ModelConfig } from '../config';

export interface KnowledgeBaseSeederProps {
  readonly config: EnvironmentConfig;
  readonly models: ModelConfig;
  readonly prefix: string;
  readonly bucket: IBucket;
  readonly rawPrefix: string;
  readonly indexKey: string;
  readonly providerApiKeySecret: ISecret;
  /** Constructs that must exist before seeding runs -- notably the document upload. */
  readonly executeAfter: IConstruct[];
}

/**
 * Builds the vector index from the documents in S3, once, at deploy time.
 *
 * A TriggerFunction rather than a manual post-deploy step, so that `cdk deploy` produces a
 * system with a populated knowledge base instead of an empty one. It stays directly
 * invokable as well, which matters because of the ordering problem below.
 *
 * **The first deployment cannot seed, by construction.** CDK creates the provider API key
 * secret empty and this trigger runs in the same deployment, so there is no key yet. The
 * handler treats that as a normal state and exits successfully rather than failing the
 * trigger and rolling back a stack whose only problem is that nobody has pasted a secret.
 * The documented flow is therefore three commands, not one:
 *
 *   1. cdk deploy                          -- everything exists, seeding is skipped
 *   2. aws secretsmanager put-secret-value  -- the key, injected out of band
 *   3. make seed                            -- build the index
 *
 * Injecting the key out of band is not ceremony. Passing it through CDK context would put
 * it in plaintext in the CloudFormation template, which lands in the bootstrap bucket and
 * in the stack history, readable by anyone with read access to the account.
 */
export class KnowledgeBaseSeeder extends Construct {
  public readonly function: TriggerFunction;
  /** Any write or delete under `raw/` lands here and debounces into one rebuild. */
  public readonly reindexQueue: Queue;

  constructor(scope: Construct, id: string, props: KnowledgeBaseSeederProps) {
    super(scope, id);

    const { config, models, prefix, bucket, providerApiKeySecret } = props;

    this.function = new TriggerFunction(this, 'Ingest', {
      functionName: `${prefix}-${config.envName}-ingest`,
      description: 'Chunks and embeds the sample documents into the vector index artifact',
      runtime: Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: Code.fromAsset(path.join(__dirname, '..', '..', '..', 'services', 'ingest')),
      // Embedding 50-ish chunks takes seconds, but a cold provider or a retry storm can
      // stretch that; the trigger blocks the deployment while it runs, so this is a ceiling
      // rather than an expectation.
      timeout: Duration.minutes(10),
      memorySize: 1024,
      logGroup: new LogGroup(this, 'IngestLogs', {
        logGroupName: `/aws/lambda/${prefix}-${config.envName}-ingest`,
        retention: config.logRetention,
        removalPolicy: config.removalPolicy,
      }),
      tracing: Tracing.ACTIVE,
      environment: {
        KB_BUCKET: bucket.bucketName,
        RAW_PREFIX: props.rawPrefix,
        KB_INDEX_KEY: props.indexKey,
        PROVIDER_API_KEY_SECRET_ARN: providerApiKeySecret.secretArn,
        EMBED_MODEL_ID: models.embeddingModel,
        EMBED_DIMENSIONS: String(models.embeddingDimensions),
      },

      // Re-run when the handler or the documents change, so editing sample-docs/ and
      // redeploying rebuilds the index instead of silently leaving a stale one.
      executeOnHandlerChange: true,
      executeAfter: props.executeAfter,
    });

    // --------------------------------------------------- reindex on any change to raw/
    //
    // Without this, the index only rebuilds when something calls the ingest: the deploy
    // trigger, the seed script, or DELETE /documents. Remove an object from the bucket any
    // other way -- the console, the CLI -- and the index keeps it, so the API goes on
    // answering from a document that no longer exists, with a high grounding score and a
    // citation nobody can open. That happened, which is why this exists.
    //
    // The queue is the whole design. S3 fires one event per object, and the deploy uploads
    // the sample documents in a burst; wired straight to the Lambda that is one full
    // re-embedding per file, concurrent, all racing to write the same artifact. A batching
    // window collapses the burst into a single invocation, and since the ingest rebuilds
    // from whatever `raw/` holds when it starts, the events are only a signal that
    // something changed -- their contents are never read.
    //
    // Cost: SQS bills nothing under a million requests a month, and this is single digits
    // a day.
    const reindexDlq = new Queue(this, 'ReindexDlq', {
      queueName: `${prefix}-${config.envName}-reindex-dlq`,
      retentionPeriod: Duration.days(14),
      enforceSSL: true,
    });

    this.reindexQueue = new Queue(this, 'ReindexQueue', {
      queueName: `${prefix}-${config.envName}-reindex`,
      // Must exceed the function timeout, or SQS redelivers a message the Lambda is still
      // working on and a second rebuild starts on top of the first.
      visibilityTimeout: Duration.minutes(11),
      enforceSSL: true,
      deadLetterQueue: { queue: reindexDlq, maxReceiveCount: 3 },
    });

    this.function.addEventSource(
      new SqsEventSource(this.reindexQueue, {
        batchSize: 100,
        // The debounce. Editing several documents, or a deploy uploading eight of them,
        // becomes one rebuild instead of eight.
        maxBatchingWindow: Duration.seconds(60),
        reportBatchItemFailures: false,
      }),
    );

    for (const event of [EventType.OBJECT_CREATED, EventType.OBJECT_REMOVED]) {
      // Scoped to `raw/`. The ingest writes to `index/`, and notifying on that would have
      // the function retrigger itself forever.
      (bucket as Bucket).addEventNotification(event, new SqsDestination(this.reindexQueue), {
        prefix: props.rawPrefix,
      });
    }

    // Least privilege, and asymmetric on purpose: the seeder reads source documents and
    // writes the index. It cannot overwrite the documents, and it has no access to the
    // generation model -- only to embeddings.
    this.function.addToRolePolicy(
      new PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [bucket.arnForObjects(`${props.rawPrefix}*`)],
      }),
    );
    this.function.addToRolePolicy(
      new PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [bucket.bucketArn],
        conditions: { StringLike: { 's3:prefix': [`${props.rawPrefix}*`] } },
      }),
    );
    this.function.addToRolePolicy(
      new PolicyStatement({
        actions: ['s3:PutObject'],
        resources: [bucket.arnForObjects('index/*')],
      }),
    );

    providerApiKeySecret.grantRead(this.function);
  }

  /** The command a reviewer runs to (re)build the index after setting the secret. */
  public get seedCommand(): string {
    return `aws lambda invoke --function-name ${this.function.functionName} --payload '{}' /tmp/seed.json && cat /tmp/seed.json`;
  }
}
