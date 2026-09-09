"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.KnowledgeBaseSeeder = void 0;
const path = __importStar(require("path"));
const aws_cdk_lib_1 = require("aws-cdk-lib");
const aws_iam_1 = require("aws-cdk-lib/aws-iam");
const aws_lambda_1 = require("aws-cdk-lib/aws-lambda");
const aws_logs_1 = require("aws-cdk-lib/aws-logs");
const aws_lambda_event_sources_1 = require("aws-cdk-lib/aws-lambda-event-sources");
const aws_s3_1 = require("aws-cdk-lib/aws-s3");
const aws_s3_notifications_1 = require("aws-cdk-lib/aws-s3-notifications");
const aws_sqs_1 = require("aws-cdk-lib/aws-sqs");
const triggers_1 = require("aws-cdk-lib/triggers");
const constructs_1 = require("constructs");
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
class KnowledgeBaseSeeder extends constructs_1.Construct {
    function;
    /** Any write or delete under `raw/` lands here and debounces into one rebuild. */
    reindexQueue;
    constructor(scope, id, props) {
        super(scope, id);
        const { config, models, prefix, bucket, providerApiKeySecret } = props;
        this.function = new triggers_1.TriggerFunction(this, 'Ingest', {
            functionName: `${prefix}-${config.envName}-ingest`,
            description: 'Chunks and embeds the sample documents into the vector index artifact',
            runtime: aws_lambda_1.Runtime.PYTHON_3_12,
            handler: 'handler.lambda_handler',
            code: aws_lambda_1.Code.fromAsset(path.join(__dirname, '..', '..', '..', 'services', 'ingest')),
            // Embedding 50-ish chunks takes seconds, but a cold provider or a retry storm can
            // stretch that; the trigger blocks the deployment while it runs, so this is a ceiling
            // rather than an expectation.
            timeout: aws_cdk_lib_1.Duration.minutes(10),
            memorySize: 1024,
            logGroup: new aws_logs_1.LogGroup(this, 'IngestLogs', {
                logGroupName: `/aws/lambda/${prefix}-${config.envName}-ingest`,
                retention: config.logRetention,
                removalPolicy: config.removalPolicy,
            }),
            tracing: aws_lambda_1.Tracing.ACTIVE,
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
        const reindexDlq = new aws_sqs_1.Queue(this, 'ReindexDlq', {
            queueName: `${prefix}-${config.envName}-reindex-dlq`,
            retentionPeriod: aws_cdk_lib_1.Duration.days(14),
            enforceSSL: true,
        });
        this.reindexQueue = new aws_sqs_1.Queue(this, 'ReindexQueue', {
            queueName: `${prefix}-${config.envName}-reindex`,
            // Must exceed the function timeout, or SQS redelivers a message the Lambda is still
            // working on and a second rebuild starts on top of the first.
            visibilityTimeout: aws_cdk_lib_1.Duration.minutes(11),
            enforceSSL: true,
            deadLetterQueue: { queue: reindexDlq, maxReceiveCount: 3 },
        });
        this.function.addEventSource(new aws_lambda_event_sources_1.SqsEventSource(this.reindexQueue, {
            batchSize: 100,
            // The debounce. Editing several documents, or a deploy uploading eight of them,
            // becomes one rebuild instead of eight.
            maxBatchingWindow: aws_cdk_lib_1.Duration.seconds(60),
            reportBatchItemFailures: false,
        }));
        for (const event of [aws_s3_1.EventType.OBJECT_CREATED, aws_s3_1.EventType.OBJECT_REMOVED]) {
            // Scoped to `raw/`. The ingest writes to `index/`, and notifying on that would have
            // the function retrigger itself forever.
            bucket.addEventNotification(event, new aws_s3_notifications_1.SqsDestination(this.reindexQueue), {
                prefix: props.rawPrefix,
            });
        }
        // Least privilege, and asymmetric on purpose: the seeder reads source documents and
        // writes the index. It cannot overwrite the documents, and it has no access to the
        // generation model -- only to embeddings.
        this.function.addToRolePolicy(new aws_iam_1.PolicyStatement({
            actions: ['s3:GetObject'],
            resources: [bucket.arnForObjects(`${props.rawPrefix}*`)],
        }));
        this.function.addToRolePolicy(new aws_iam_1.PolicyStatement({
            actions: ['s3:ListBucket'],
            resources: [bucket.bucketArn],
            conditions: { StringLike: { 's3:prefix': [`${props.rawPrefix}*`] } },
        }));
        this.function.addToRolePolicy(new aws_iam_1.PolicyStatement({
            actions: ['s3:PutObject'],
            resources: [bucket.arnForObjects('index/*')],
        }));
        providerApiKeySecret.grantRead(this.function);
    }
    /** The command a reviewer runs to (re)build the index after setting the secret. */
    get seedCommand() {
        return `aws lambda invoke --function-name ${this.function.functionName} --payload '{}' /tmp/seed.json && cat /tmp/seed.json`;
    }
}
exports.KnowledgeBaseSeeder = KnowledgeBaseSeeder;
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoia25vd2xlZGdlLWJhc2Utc2VlZGVyLmpzIiwic291cmNlUm9vdCI6IiIsInNvdXJjZXMiOlsia25vd2xlZGdlLWJhc2Utc2VlZGVyLnRzIl0sIm5hbWVzIjpbXSwibWFwcGluZ3MiOiI7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7OztBQUFBLDJDQUE2QjtBQUM3Qiw2Q0FBdUM7QUFDdkMsaURBQXNEO0FBQ3RELHVEQUFnRTtBQUNoRSxtREFBZ0Q7QUFDaEQsbUZBQXNFO0FBQ3RFLCtDQUFnRTtBQUNoRSwyRUFBa0U7QUFDbEUsaURBQTRDO0FBRTVDLG1EQUF1RDtBQUN2RCwyQ0FBbUQ7QUFlbkQ7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7O0dBb0JHO0FBQ0gsTUFBYSxtQkFBb0IsU0FBUSxzQkFBUztJQUNoQyxRQUFRLENBQWtCO0lBQzFDLGtGQUFrRjtJQUNsRSxZQUFZLENBQVE7SUFFcEMsWUFBWSxLQUFnQixFQUFFLEVBQVUsRUFBRSxLQUErQjtRQUN2RSxLQUFLLENBQUMsS0FBSyxFQUFFLEVBQUUsQ0FBQyxDQUFDO1FBRWpCLE1BQU0sRUFBRSxNQUFNLEVBQUUsTUFBTSxFQUFFLE1BQU0sRUFBRSxNQUFNLEVBQUUsb0JBQW9CLEVBQUUsR0FBRyxLQUFLLENBQUM7UUFFdkUsSUFBSSxDQUFDLFFBQVEsR0FBRyxJQUFJLDBCQUFlLENBQUMsSUFBSSxFQUFFLFFBQVEsRUFBRTtZQUNsRCxZQUFZLEVBQUUsR0FBRyxNQUFNLElBQUksTUFBTSxDQUFDLE9BQU8sU0FBUztZQUNsRCxXQUFXLEVBQUUsdUVBQXVFO1lBQ3BGLE9BQU8sRUFBRSxvQkFBTyxDQUFDLFdBQVc7WUFDNUIsT0FBTyxFQUFFLHdCQUF3QjtZQUNqQyxJQUFJLEVBQUUsaUJBQUksQ0FBQyxTQUFTLENBQUMsSUFBSSxDQUFDLElBQUksQ0FBQyxTQUFTLEVBQUUsSUFBSSxFQUFFLElBQUksRUFBRSxJQUFJLEVBQUUsVUFBVSxFQUFFLFFBQVEsQ0FBQyxDQUFDO1lBQ2xGLGtGQUFrRjtZQUNs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RiwwQ0FBMEM7UUFDMUMsSUFBSSxDQUFDLFFBQVEsQ0FBQyxlQUFlLENBQzNCLElBQUkseUJBQWUsQ0FBQztZQUNsQixPQUFPLEVBQUUsQ0FBQyxjQUFjLENBQUM7WUFDekIsU0FBUyxFQUFFLENBQUMsTUFBTSxDQUFDLGFBQWEsQ0FBQyxHQUFHLEtBQUssQ0FBQyxTQUFTLEdBQUcsQ0FBQyxDQUFDO1NBQ3pELENBQUMsQ0FDSCxDQUFDO1FBQ0YsSUFBSSxDQUFDLFFBQVEsQ0FBQyxlQUFlLENBQzNCLElBQUkseUJBQWUsQ0FBQztZQUNsQixPQUFPLEVBQUUsQ0FBQyxlQUFlLENBQUM7WUFDMUIsU0FBUyxFQUFFLENBQUMsTUFBTSxDQUFDLFNBQVMsQ0FBQztZQUM3QixVQUFVLEVBQUUsRUFBRSxVQUFVLEVBQUUsRUFBRSxXQUFXLEVBQUUsQ0FBQyxHQUFHLEtBQUssQ0FBQyxTQUFTLEdBQUcsQ0FBQyxFQUFFLEVBQUU7U0FDckUsQ0FBQyxDQUNILENBQUM7UUFDRixJQUFJLENBQUMsUUFBUSxDQUFDLGVBQWUsQ0FDM0IsSUFBSSx5QkFBZSxDQUFDO1lBQ2xCLE9BQU8sRUFBRSxDQUFDLGNBQWMsQ0FBQztZQUN6QixTQUFTLEVBQUUsQ0FBQyxNQUFNLENBQUMsYUFBYSxDQUFDLFNBQVMsQ0FBQyxDQUFDO1NBQzdDLENBQUMsQ0FDSCxDQUFDO1FBRUYsb0JBQW9CLENBQUMsU0FBUyxDQUFDLElBQUksQ0FBQyxRQUFRLENBQUMsQ0FBQztJQUNoRCxDQUFDO0lBRUQsbUZBQW1GO0lBQ25GLElBQVcsV0FBVztRQUNwQixPQUFPLHFDQUFxQyxJQUFJLENBQUMsUUFBUSxDQUFDLFlBQVksc0RBQXNELENBQUM7SUFDL0gsQ0FBQztDQUNGO0FBMUhELGtEQTBIQyIsInNvdXJjZXNDb250ZW50IjpbImltcG9ydCAqIGFzIHBhdGggZnJvbSAncGF0aCc7XHJcbmltcG9ydCB7IER1cmF0aW9uIH0gZnJvbSAnYXdzLWNkay1saWInO1xyXG5pbXBvcnQgeyBQb2xpY3lTdGF0ZW1lbnQgfSBmcm9tICdhd3MtY2RrLWxpYi9hd3MtaWFtJztcclxuaW1wb3J0IHsgQ29kZSwgUnVudGltZSwgVHJhY2luZyB9IGZyb20gJ2F3cy1jZGstbGliL2F3cy1sYW1iZGEnO1xyXG5pbXBvcnQgeyBMb2dHcm91cCB9IGZyb20gJ2F3cy1jZGstbGliL2F3cy1sb2dzJztcclxuaW1wb3J0IHsgU3FzRXZlbnRTb3VyY2UgfSBmcm9tICdhd3MtY2RrLWxpYi9hd3MtbGFtYmRhLWV2ZW50LXNvdXJjZXMnO1xyXG5pbXBvcnQgeyBCdWNrZXQsIEV2ZW50VHlwZSwgSUJ1Y2tldCB9IGZyb20gJ2F3cy1jZGstbGliL2F3cy1zMyc7XHJcbmltcG9ydCB7IFNxc0Rlc3RpbmF0aW9uIH0gZnJvbSAnYXdzLWNkay1saWIvYXdzLXMzLW5vdGlmaWNhdGlvbnMnO1xyXG5pbXBvcnQgeyBRdWV1ZSB9IGZyb20gJ2F3cy1jZGstbGliL2F3cy1zcXMnO1xyXG5pbXBvcnQgeyBJU2VjcmV0IH0gZnJvbSAnYXdzLWNkay1saWIvYXdzLXNlY3JldHNtYW5hZ2VyJztcclxuaW1wb3J0IHsgVHJpZ2dlckZ1bmN0aW9uIH0gZnJvbSAnYXdzLWNkay1saWIvdHJpZ2dlcnMnO1xyXG5pbXBvcnQgeyBDb25zdHJ1Y3QsIElDb25zdHJ1Y3QgfSBmcm9tICdjb25zdHJ1Y3RzJztcclxuaW1wb3J0IHsgRW52aXJvbm1lbnRDb25maWcsIE1vZGVsQ29uZmlnIH0gZnJvbSAnLi4vY29uZmlnJztcclxuXHJcbmV4cG9ydCBpbnRlcmZhY2UgS25vd2xlZGdlQmFzZVNlZWRlclByb3BzIHtcclxuICByZWFkb25seSBjb25maWc6IEVudmlyb25tZW50Q29uZmlnO1xyXG4gIHJlYWRvbmx5IG1vZGVsczogTW9kZWxDb25maWc7XHJcbiAgcmVhZG9ubHkgcHJlZml4OiBzdHJpbmc7XHJcbiAgcmVhZG9ubHkgYnVja2V0OiBJQnVja2V0O1xyXG4gIHJlYWRvbmx5IHJhd1ByZWZpeDogc3RyaW5nO1xyXG4gIHJlYWRvbmx5IGluZGV4S2V5OiBzdHJpbmc7XHJcbiAgcmVhZG9ubHkgcHJvdmlkZXJBcGlLZXlTZWNyZXQ6IElTZWNyZXQ7XHJcbiAgLyoqIENvbnN0cnVjdHMgdGhhdCBtdXN0IGV4aXN0IGJlZm9yZSBzZWVkaW5nIHJ1bnMgLS0gbm90YWJseSB0aGUgZG9jdW1lbnQgdXBsb2FkLiAqL1xyXG4gIHJlYWRvbmx5IGV4ZWN1dGVBZnRlcjogSUNvbnN0cnVjdFtdO1xyXG59XHJcblxyXG4vKipcclxuICogQnVpbGRzIHRoZSB2ZWN0b3IgaW5kZXggZnJvbSB0aGUgZG9jdW1lbnRzIGluIFMzLCBvbmNlLCBhdCBkZXBsb3kgdGltZS5cclxuICpcclxuICogQSBUcmlnZ2VyRnVuY3Rpb24gcmF0aGVyIHRoYW4gYSBtYW51YWwgcG9zdC1kZXBsb3kgc3RlcCwgc28gdGhhdCBgY2RrIGRlcGxveWAgcHJvZHVjZXMgYVxyXG4gKiBzeXN0ZW0gd2l0aCBhIHBvcHVsYXRlZCBrbm93bGVkZ2UgYmFzZSBpbnN0ZWFkIG9mIGFuIGVtcHR5IG9uZS4gSXQgc3RheXMgZGlyZWN0bHlcclxuICogaW52b2thYmxlIGFzIHdlbGwsIHdoaWNoIG1hdHRlcnMgYmVjYXVzZSBvZiB0aGUgb3JkZXJpbmcgcHJvYmxlbSBiZWxvdy5cclxuICpcclxuICogKipUaGUgZmlyc3QgZGVwbG95bWVudCBjYW5ub3Qgc2VlZCwgYnkgY29uc3RydWN0aW9uLioqIENESyBjcmVhdGVzIHRoZSBwcm92aWRlciBBUEkga2V5XHJcbiAqIHNlY3JldCBlbXB0eSBhbmQgdGhpcyB0cmlnZ2VyIHJ1bnMgaW4gdGhlIHNhbWUgZGVwbG95bWVudCwgc28gdGhlcmUgaXMgbm8ga2V5IHlldC4gVGhlXHJcbiAqIGhhbmRsZXIgdHJlYXRzIHRoYXQgYXMgYSBub3JtYWwgc3RhdGUgYW5kIGV4aXRzIHN1Y2Nlc3NmdWxseSByYXRoZXIgdGhhbiBmYWlsaW5nIHRoZVxyXG4gKiB0cmlnZ2VyIGFuZCByb2xsaW5nIGJhY2sgYSBzdGFjayB3aG9zZSBvbmx5IHByb2JsZW0gaXMgdGhhdCBub2JvZHkgaGFzIHBhc3RlZCBhIHNlY3JldC5cclxuICogVGhlIGRvY3VtZW50ZWQgZmxvdyBpcyB0aGVyZWZvcmUgdGhyZWUgY29tbWFuZHMsIG5vdCBvbmU6XHJcbiAqXHJcbiAqICAgMS4gY2RrIGRlcGxveSAgICAgICAgICAgICAgICAgICAgICAgICAgLS0gZXZlcnl0aGluZyBleGlzdHMsIHNlZWRpbmcgaXMgc2tpcHBlZFxyXG4gKiAgIDIuIGF3cyBzZWNyZXRzbWFuYWdlciBwdXQtc2VjcmV0LXZhbHVlICAtLSB0aGUga2V5LCBpbmplY3RlZCBvdXQgb2YgYmFuZFxyXG4gKiAgIDMuIG1ha2Ugc2VlZCAgICAgICAgICAgICAgICAgICAgICAgICAgICAtLSBidWlsZCB0aGUgaW5kZXhcclxuICpcclxuICogSW5qZWN0aW5nIHRoZSBrZXkgb3V0IG9mIGJhbmQgaXMgbm90IGNlcmVtb255LiBQYXNzaW5nIGl0IHRocm91Z2ggQ0RLIGNvbnRleHQgd291bGQgcHV0XHJcbiAqIGl0IGluIHBsYWludGV4dCBpbiB0aGUgQ2xvdWRGb3JtYXRpb24gdGVtcGxhdGUsIHdoaWNoIGxhbmRzIGluIHRoZSBib290c3RyYXAgYnVja2V0IGFuZFxyXG4gKiBpbiB0aGUgc3RhY2sgaGlzdG9yeSwgcmVhZGFibGUgYnkgYW55b25lIHdpdGggcmVhZCBhY2Nlc3MgdG8gdGhlIGFjY291bnQuXHJcbiAqL1xyXG5leHBvcnQgY2xhc3MgS25vd2xlZGdlQmFzZVNlZWRlciBleHRlbmRzIENvbnN0cnVjdCB7XHJcbiAgcHVibGljIHJlYWRvbmx5IGZ1bmN0aW9uOiBUcmlnZ2VyRnVuY3Rpb247XHJcbiAgLyoqIEFueSB3cml0ZSBvciBkZWxldGUgdW5kZXIgYHJhdy9gIGxhbmRzIGhlcmUgYW5kIGRlYm91bmNlcyBpbnRvIG9uZSByZWJ1aWxkLiAqL1xyXG4gIHB1YmxpYyByZWFkb25seSByZWluZGV4UXVldWU6IFF1ZXVlO1xyXG5cclxuICBjb25zdHJ1Y3RvcihzY29wZTogQ29uc3RydWN0LCBpZDogc3RyaW5nLCBwcm9wczogS25vd2xlZGdlQmFzZVNlZWRlclByb3BzKSB7XHJcbiAgICBzdXBlcihzY29wZSwgaWQpO1xyXG5cclxuICAgIGNvbnN0IHsgY29uZmlnLCBtb2RlbHMsIHByZWZpeCwgYnVja2V0LCBwcm92aWRlckFwaUtleVNlY3JldCB9ID0gcHJvcHM7XHJcblxyXG4gICAgdGhpcy5mdW5jdGlvbiA9IG5ldyBUcmlnZ2VyRnVuY3Rpb24odGhpcywgJ0luZ2VzdCcsIHtcclxuICAgICAgZnVuY3Rpb25OYW1lOiBgJHtwcmVmaXh9LSR7Y29uZmlnLmVudk5hbWV9LWluZ2VzdGAsXHJcbiAgICAgIGRlc2NyaXB0aW9uOiAnQ2h1bmtzIGFuZCBlbWJlZHMgdGhlIHNhbXBsZSBkb2N1bWVudHMgaW50byB0aGUgdmVjdG9yIGluZGV4IGFydGlmYWN0JyxcclxuICAgICAgcnVudGltZTogUnVudGltZS5QWVRIT05fM18xMixcclxuICAgICAgaGFuZGxlcjogJ2hhbmRsZXIubGFtYmRhX2hhbmRsZXInLFxyXG4gICAgICBjb2RlOiBDb2RlLmZyb21Bc3NldChwYXRoLmpvaW4oX19kaXJuYW1lLCAnLi4nLCAnLi4nLCAnLi4nLCAnc2VydmljZXMnLCAnaW5nZXN0JykpLFxyXG4gICAgICAvLyBFbWJlZGRpbmcgNTAtaXNoIGNodW5rcyB0YWtlcyBzZWNvbmRzLCBidXQgYSBjb2xkIHByb3ZpZGVyIG9yIGEgcmV0cnkgc3Rvcm0gY2FuXHJcbiAgICAgIC8vIHN0cmV0Y2ggdGhhdDsgdGhlIHRyaWdnZXIgYmxvY2tzIHRoZSBkZXBsb3ltZW50IHdoaWxlIGl0IHJ1bnMsIHNvIHRoaXMgaXMgYSBjZWlsaW5nXHJcbiAgICAgIC8vIHJhdGhlciB0aGFuIGFuIGV4cGVjdGF0aW9uLlxyXG4gICAgICB0aW1lb3V0OiBEdXJhdGlvbi5taW51dGVzKDEwKSxcclxuICAgICAgbWVtb3J5U2l6ZTogMTAyNCxcclxuICAgICAgbG9nR3JvdXA6IG5ldyBMb2dHcm91cCh0aGlzLCAnSW5nZXN0TG9ncycsIHtcclxuICAgICAgICBsb2dHcm91cE5hbWU6IGAvYXdzL2xhbWJkYS8ke3ByZWZpeH0tJHtjb25maWcuZW52TmFtZX0taW5nZXN0YCxcclxuICAgICAgICByZXRlbnRpb246IGNvbmZpZy5sb2dSZXRlbnRpb24sXHJcbiAgICAgICAgcmVtb3ZhbFBvbGljeTogY29uZmlnLnJlbW92YWxQb2xpY3ksXHJcbiAgICAgIH0pLFxyXG4gICAgICB0cmFjaW5nOiBUcmFjaW5nLkFDVElWRSxcclxuICAgICAgZW52aXJvbm1lbnQ6IHtcclxuICAgICAgICBLQl9CVUNLRVQ6IGJ1Y2tldC5idWNrZXROYW1lLFxyXG4gICAgICAgIFJBV19QUkVGSVg6IHByb3BzLnJhd1ByZWZpeCxcclxuICAgICAgICBLQl9JTkRFWF9LRVk6IHByb3BzLmluZGV4S2V5LFxyXG4gICAgICAgIFBST1ZJREVSX0FQSV9LRVlfU0VDUkVUX0FSTjogcHJvdmlkZXJBcGlLZXlTZWNyZXQuc2VjcmV0QXJuLFxyXG4gICAgICAgIEVNQkVEX01PREVMX0lEOiBtb2RlbHMuZW1iZWRkaW5nTW9kZWwsXHJcbiAgICAgICAgRU1CRURfRElNRU5TSU9OUzogU3RyaW5nKG1vZGVscy5lbWJlZGRpbmdEaW1lbnNpb25zKSxcclxuICAgICAgfSxcclxuXHJcbiAgICAgIC8vIFJlLXJ1biB3aGVuIHRoZSBoYW5kbGVyIG9yIHRoZSBkb2N1bWVudHMgY2hhbmdlLCBzbyBlZGl0aW5nIHNhbXBsZS1kb2NzLyBhbmRcclxuICAgICAgLy8gcmVkZXBsb3lpbmcgcmVidWlsZHMgdGhlIGluZGV4IGluc3RlYWQgb2Ygc2lsZW50bHkgbGVhdmluZyBhIHN0YWxlIG9uZS5cclxuICAgICAgZXhlY3V0ZU9uSGFuZGxlckNoYW5nZTogdHJ1ZSxcclxuICAgICAgZXhlY3V0ZUFmdGVyOiBwcm9wcy5leGVjdXRlQWZ0ZXIsXHJcbiAgICB9KTtcclxuXHJcbiAgICAvLyAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0gcmVpbmRleCBvbiBhbnkgY2hhbmdlIHRvIHJhdy9cclxuICAgIC8vXHJcbiAgICAvLyBXaXRob3V0IHRoaXMsIHRoZSBpbmRleCBvbmx5IHJlYnVpbGRzIHdoZW4gc29tZXRoaW5nIGNhbGxzIHRoZSBpbmdlc3Q6IHRoZSBkZXBsb3lcclxuICAgIC8vIHRyaWdnZXIsIHRoZSBzZWVkIHNjcmlwdCwgb3IgREVMRVRFIC9kb2N1bWVudHMuIFJlbW92ZSBhbiBvYmplY3QgZnJvbSB0aGUgYnVja2V0IGFueVxyXG4gICAgLy8gb3RoZXIgd2F5IC0tIHRoZSBjb25zb2xlLCB0aGUgQ0xJIC0tIGFuZCB0aGUgaW5kZXgga2VlcHMgaXQsIHNvIHRoZSBBUEkgZ29lcyBvblxyXG4gICAgLy8gYW5zd2VyaW5nIGZyb20gYSBkb2N1bWVudCB0aGF0IG5vIGxvbmdlciBleGlzdHMsIHdpdGggYSBoaWdoIGdyb3VuZGluZyBzY29yZSBhbmQgYVxyXG4gICAgLy8gY2l0YXRpb24gbm9ib2R5IGNhbiBvcGVuLiBUaGF0IGhhcHBlbmVkLCB3aGljaCBpcyB3aHkgdGhpcyBleGlzdHMuXHJcbiAgICAvL1xyXG4gICAgLy8gVGhlIHF1ZXVlIGlzIHRoZSB3aG9sZSBkZXNpZ24uIFMzIGZpcmVzIG9uZSBldmVudCBwZXIgb2JqZWN0LCBhbmQgdGhlIGRlcGxveSB1cGxvYWRzXHJcbiAgICAvLyB0aGUgc2FtcGxlIGRvY3VtZW50cyBpbiBhIGJ1cnN0OyB3aXJlZCBzdHJhaWdodCB0byB0aGUgTGFtYmRhIHRoYXQgaXMgb25lIGZ1bGxcclxuICAgIC8vIHJlLWVtYmVkZGluZyBwZXIgZmlsZSwgY29uY3VycmVudCwgYWxsIHJhY2luZyB0byB3cml0ZSB0aGUgc2FtZSBhcnRpZmFjdC4gQSBiYXRjaGluZ1xyXG4gICAgLy8gd2luZG93IGNvbGxhcHNlcyB0aGUgYnVyc3QgaW50byBhIHNpbmdsZSBpbnZvY2F0aW9uLCBhbmQgc2luY2UgdGhlIGluZ2VzdCByZWJ1aWxkc1xyXG4gICAgLy8gZnJvbSB3aGF0ZXZlciBgcmF3L2AgaG9sZHMgd2hlbiBpdCBzdGFydHMsIHRoZSBldmVudHMgYXJlIG9ubHkgYSBzaWduYWwgdGhhdFxyXG4gICAgLy8gc29tZXRoaW5nIGNoYW5nZWQgLS0gdGhlaXIgY29udGVudHMgYXJlIG5ldmVyIHJlYWQuXHJcbiAgICAvL1xyXG4gICAgLy8gQ29zdDogU1FTIGJpbGxzIG5vdGhpbmcgdW5kZXIgYSBtaWxsaW9uIHJlcXVlc3RzIGEgbW9udGgsIGFuZCB0aGlzIGlzIHNpbmdsZSBkaWdpdHNcclxuICAgIC8vIGEgZGF5LlxyXG4gICAgY29uc3QgcmVpbmRleERscSA9IG5ldyBRdWV1ZSh0aGlzLCAnUmVpbmRleERscScsIHtcclxuICAgICAgcXVldWVOYW1lOiBgJHtwcmVmaXh9LSR7Y29uZmlnLmVudk5hbWV9LXJlaW5kZXgtZGxxYCxcclxuICAgICAgcmV0ZW50aW9uUGVyaW9kOiBEdXJhdGlvbi5kYXlzKDE0KSxcclxuICAgICAgZW5mb3JjZVNTTDogdHJ1ZSxcclxuICAgIH0pO1xyXG5cclxuICAgIHRoaXMucmVpbmRleFF1ZXVlID0gbmV3IFF1ZXVlKHRoaXMsICdSZWluZGV4UXVldWUnLCB7XHJcbiAgICAgIHF1ZXVlTmFtZTogYCR7cHJlZml4fS0ke2NvbmZpZy5lbnZOYW1lfS1yZWluZGV4YCxcclxuICAgICAgLy8gTXVzdCBleGNlZWQgdGhlIGZ1bmN0aW9uIHRpbWVvdXQsIG9yIFNRUyByZWRlbGl2ZXJzIGEgbWVzc2FnZSB0aGUgTGFtYmRhIGlzIHN0aWxsXHJcbiAgICAgIC8vIHdvcmtpbmcgb24gYW5kIGEgc2Vjb25kIHJlYnVpbGQgc3RhcnRzIG9uIHRvcCBvZiB0aGUgZmlyc3QuXHJcbiAgICAgIHZpc2liaWxpdHlUaW1lb3V0OiBEdXJhdGlvbi5taW51dGVzKDExKSxcclxuICAgICAgZW5mb3JjZVNTTDogdHJ1ZSxcclxuICAgICAgZGVhZExldHRlclF1ZXVlOiB7IHF1ZXVlOiByZWluZGV4RGxxLCBtYXhSZWNlaXZlQ291bnQ6IDMgfSxcclxuICAgIH0pO1xyXG5cclxuICAgIHRoaXMuZnVuY3Rpb24uYWRkRXZlbnRTb3VyY2UoXHJcbiAgICAgIG5ldyBTcXNFdmVudFNvdXJjZSh0aGlzLnJlaW5kZXhRdWV1ZSwge1xyXG4gICAgICAgIGJhdGNoU2l6ZTogMTAwLFxyXG4gICAgICAgIC8vIFRoZSBkZWJvdW5jZS4gRWRpdGluZyBzZXZlcmFsIGRvY3VtZW50cywgb3IgYSBkZXBsb3kgdXBsb2FkaW5nIGVpZ2h0IG9mIHRoZW0sXHJcbiAgICAgICAgLy8gYmVjb21lcyBvbmUgcmVidWlsZCBpbnN0ZWFkIG9mIGVpZ2h0LlxyXG4gICAgICAgIG1heEJhdGNoaW5nV2luZG93OiBEdXJhdGlvbi5zZWNvbmRzKDYwKSxcclxuICAgICAgICByZXBvcnRCYXRjaEl0ZW1GYWlsdXJlczogZmFsc2UsXHJcbiAgICAgIH0pLFxyXG4gICAgKTtcclxuXHJcbiAgICBmb3IgKGNvbnN0IGV2ZW50IG9mIFtFdmVudFR5cGUuT0JKRUNUX0NSRUFURUQsIEV2ZW50VHlwZS5PQkpFQ1RfUkVNT1ZFRF0pIHtcclxuICAgICAgLy8gU2NvcGVkIHRvIGByYXcvYC4gVGhlIGluZ2VzdCB3cml0ZXMgdG8gYGluZGV4L2AsIGFuZCBub3RpZnlpbmcgb24gdGhhdCB3b3VsZCBoYXZlXHJcbiAgICAgIC8vIHRoZSBmdW5jdGlvbiByZXRyaWdnZXIgaXRzZWxmIGZvcmV2ZXIuXHJcbiAgICAgIChidWNrZXQgYXMgQnVja2V0KS5hZGRFdmVudE5vdGlmaWNhdGlvbihldmVudCwgbmV3IFNxc0Rlc3RpbmF0aW9uKHRoaXMucmVpbmRleFF1ZXVlKSwge1xyXG4gICAgICAgIHByZWZpeDogcHJvcHMucmF3UHJlZml4LFxyXG4gICAgICB9KTtcclxuICAgIH1cclxuXHJcbiAgICAvLyBMZWFzdCBwcml2aWxlZ2UsIGFuZCBhc3ltbWV0cmljIG9uIHB1cnBvc2U6IHRoZSBzZWVkZXIgcmVhZHMgc291cmNlIGRvY3VtZW50cyBhbmRcclxuICAgIC8vIHdyaXRlcyB0aGUgaW5kZXguIEl0IGNhbm5vdCBvdmVyd3JpdGUgdGhlIGRvY3VtZW50cywgYW5kIGl0IGhhcyBubyBhY2Nlc3MgdG8gdGhlXHJcbiAgICAvLyBnZW5lcmF0aW9uIG1vZGVsIC0tIG9ubHkgdG8gZW1iZWRkaW5ncy5cclxuICAgIHRoaXMuZnVuY3Rpb24uYWRkVG9Sb2xlUG9saWN5KFxyXG4gICAgICBuZXcgUG9saWN5U3RhdGVtZW50KHtcclxuICAgICAgICBhY3Rpb25zOiBbJ3MzOkdldE9iamVjdCddLFxyXG4gICAgICAgIHJlc291cmNlczogW2J1Y2tldC5hcm5Gb3JPYmplY3RzKGAke3Byb3BzLnJhd1ByZWZpeH0qYCldLFxyXG4gICAgICB9KSxcclxuICAgICk7XHJcbiAgICB0aGlzLmZ1bmN0aW9uLmFkZFRvUm9sZVBvbGljeShcclxuICAgICAgbmV3IFBvbGljeVN0YXRlbWVudCh7XHJcbiAgICAgICAgYWN0aW9uczogWydzMzpMaXN0QnVja2V0J10sXHJcbiAgICAgICAgcmVzb3VyY2VzOiBbYnVja2V0LmJ1Y2tldEFybl0sXHJcbiAgICAgICAgY29uZGl0aW9uczogeyBTdHJpbmdMaWtlOiB7ICdzMzpwcmVmaXgnOiBbYCR7cHJvcHMucmF3UHJlZml4fSpgXSB9IH0sXHJcbiAgICAgIH0pLFxyXG4gICAgKTtcclxuICAgIHRoaXMuZnVuY3Rpb24uYWRkVG9Sb2xlUG9saWN5KFxyXG4gICAgICBuZXcgUG9saWN5U3RhdGVtZW50KHtcclxuICAgICAgICBhY3Rpb25zOiBbJ3MzOlB1dE9iamVjdCddLFxyXG4gICAgICAgIHJlc291cmNlczogW2J1Y2tldC5hcm5Gb3JPYmplY3RzKCdpbmRleC8qJyldLFxyXG4gICAgICB9KSxcclxuICAgICk7XHJcblxyXG4gICAgcHJvdmlkZXJBcGlLZXlTZWNyZXQuZ3JhbnRSZWFkKHRoaXMuZnVuY3Rpb24pO1xyXG4gIH1cclxuXHJcbiAgLyoqIFRoZSBjb21tYW5kIGEgcmV2aWV3ZXIgcnVucyB0byAocmUpYnVpbGQgdGhlIGluZGV4IGFmdGVyIHNldHRpbmcgdGhlIHNlY3JldC4gKi9cclxuICBwdWJsaWMgZ2V0IHNlZWRDb21tYW5kKCk6IHN0cmluZyB7XHJcbiAgICByZXR1cm4gYGF3cyBsYW1iZGEgaW52b2tlIC0tZnVuY3Rpb24tbmFtZSAke3RoaXMuZnVuY3Rpb24uZnVuY3Rpb25OYW1lfSAtLXBheWxvYWQgJ3t9JyAvdG1wL3NlZWQuanNvbiAmJiBjYXQgL3RtcC9zZWVkLmpzb25gO1xyXG4gIH1cclxufVxyXG4iXX0=