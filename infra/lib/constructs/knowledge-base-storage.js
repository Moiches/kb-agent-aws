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
exports.RemovalPolicy = exports.KnowledgeBaseStorage = void 0;
const path = __importStar(require("path"));
const aws_cdk_lib_1 = require("aws-cdk-lib");
Object.defineProperty(exports, "RemovalPolicy", { enumerable: true, get: function () { return aws_cdk_lib_1.RemovalPolicy; } });
const aws_dynamodb_1 = require("aws-cdk-lib/aws-dynamodb");
const aws_s3_1 = require("aws-cdk-lib/aws-s3");
const aws_s3_deployment_1 = require("aws-cdk-lib/aws-s3-deployment");
const constructs_1 = require("constructs");
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
class KnowledgeBaseStorage extends constructs_1.Construct {
    bucket;
    queryLogTable;
    rawPrefix = 'raw/';
    indexKey = 'index/kb-index.json.gz';
    constructor(scope, id, props) {
        super(scope, id);
        const { config, prefix, sampleDocsPath } = props;
        // The account and region tokens resolve at deploy time, which is what keeps the same
        // code deployable to more than one account: the name is globally unique per account
        // without anyone editing it.
        this.bucket = new aws_s3_1.Bucket(this, 'Bucket', {
            bucketName: `${prefix}-${config.envName}-${aws_cdk_lib_1.Aws.ACCOUNT_ID}-${aws_cdk_lib_1.Aws.REGION}`,
            blockPublicAccess: aws_s3_1.BlockPublicAccess.BLOCK_ALL,
            encryption: aws_s3_1.BucketEncryption.S3_MANAGED,
            enforceSSL: true,
            versioned: true,
            objectOwnership: aws_s3_1.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removalPolicy: config.removalPolicy,
            autoDeleteObjects: config.autoDeleteObjects,
        });
        // Ships the version-controlled sample documents with the stack, so a single
        // `cdk deploy` produces a system with a populated knowledge base. Document ingestion
        // is intentionally out of scope; this is what replaces it.
        new aws_s3_deployment_1.BucketDeployment(this, 'SampleDocs', {
            sources: [aws_s3_deployment_1.Source.asset(path.resolve(sampleDocsPath))],
            destinationBucket: this.bucket,
            destinationKeyPrefix: this.rawPrefix,
            // Leave anything else in the bucket alone -- notably the built index, which this
            // deployment must never delete.
            prune: false,
            retainOnDelete: false,
        });
        this.queryLogTable = new aws_dynamodb_1.Table(this, 'QueryLog', {
            tableName: `${prefix}-${config.envName}-query-log`,
            partitionKey: { name: 'session_id', type: aws_dynamodb_1.AttributeType.STRING },
            sortKey: { name: 'ts_request', type: aws_dynamodb_1.AttributeType.STRING },
            billingMode: aws_dynamodb_1.BillingMode.PAY_PER_REQUEST,
            encryption: aws_dynamodb_1.TableEncryption.AWS_MANAGED,
            // Query logs are a debugging aid, not a system of record: let them expire.
            timeToLiveAttribute: 'expires_at',
            pointInTimeRecoverySpecification: {
                pointInTimeRecoveryEnabled: config.pointInTimeRecovery,
            },
            removalPolicy: config.removalPolicy,
        });
    }
    /** ARN pattern for the index object, for scoping the query Lambda's read permission. */
    get indexObjectArn() {
        return this.bucket.arnForObjects('index/*');
    }
    /** ARN pattern for the source documents, for scoping the ingest Lambda's read permission. */
    get rawObjectsArn() {
        return this.bucket.arnForObjects(`${this.rawPrefix}*`);
    }
}
exports.KnowledgeBaseStorage = KnowledgeBaseStorage;
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoia25vd2xlZGdlLWJhc2Utc3RvcmFnZS5qcyIsInNvdXJjZVJvb3QiOiIiLCJzb3VyY2VzIjpbImtub3dsZWRnZS1iYXNlLXN0b3JhZ2UudHMiXSwibmFtZXMiOltdLCJtYXBwaW5ncyI6Ijs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7O0FBQUEsMkNBQTZCO0FBQzdCLDZDQUFpRDtBQTBGeEMsOEZBMUZLLDJCQUFhLE9BMEZMO0FBekZ0QiwyREFBOEY7QUFDOUYsK0NBQTJHO0FBQzNHLHFFQUF5RTtBQUN6RSwyQ0FBdUM7QUFVdkM7Ozs7Ozs7Ozs7R0FVRztBQUNILE1BQWEsb0JBQXFCLFNBQVEsc0JBQVM7SUFDakMsTUFBTSxDQUFVO0lBQ2hCLGFBQWEsQ0FBUTtJQUNyQixTQUFTLEdBQUcsTUFBTSxDQUFDO0lBQ25CLFFBQVEsR0FBRyx3QkFBd0IsQ0FBQztJQUVwRCxZQUFZLEtBQWdCLEVBQUUsRUFBVSxFQUFFLEtBQWdDO1FBQ3hFLEtBQUssQ0FBQyxLQUFLLEVBQUUsRUFBRSxDQUFDLENBQUM7UUFFakIsTUFBTSxFQUFFLE1BQU0sRUFBRSxNQUFNLEVBQUUsY0FBYyxFQUFFLEdBQUcsS0FBSyxDQUFDO1FBRWpELHFGQUFxRjtRQUNyRixvRkFBb0Y7UUFDcEYsNkJBQTZCO1FBQzdCLElBQUksQ0FBQyxNQUFNLEdBQUcsSUFBSSxlQUFNLENBQUMsSUFBSSxFQUFFLFFBQVEsRUFBRTtZQUN2QyxVQUFVLEVBQUUsR0FBRyxNQUFNLElBQUksTUFBTSxDQUFDLE9BQU8sSUFBSSxpQkFBRyxDQUFDLFVBQVUsSUFBSSxpQkFBRyxDQUFDLE1BQU0sRUFBRTtZQUN6RSxpQkFBaUIsRUFBRSwwQkFBaUIsQ0FBQyxTQUFTO1lBQzlDLFVBQVUsRUFBRSx5QkFBZ0IsQ0FBQyxVQUFVO1lBQ3ZDLFVBQVUsRUFBRSxJQUFJO1lBQ2hCLFNBQVMsRUFBRSxJQUFJO1lBQ2YsZUFBZSxFQUFFLHdCQUFlLENBQUMscUJBQXFCO1lBQ3RELGFBQWEsRUFBRSxNQUFNLENBQUMsYUFBYTtZQUNuQyxpQkFBaUIsRUFBRSxNQUFNLENBQUMsaUJBQWlCO1NBQzVDLENBQUMsQ0FBQztRQUVILDRFQUE0RTtRQUM1RSxxRkFBcUY7UUFDckYsMkRBQTJEO1FBQzNELElBQUksb0NBQWdCLENBQUMsSUFBSSxFQUFFLFlBQVksRUFBRTtZQUN2QyxPQUFPLEVBQUUsQ0FBQywwQkFBTSxDQUFDLEtBQUssQ0FBQyxJQUFJLENBQUMsT0FBTyxDQUFDLGNBQWMsQ0FBQyxDQUFDLENBQUM7WUFDckQsaUJBQWlCLEVBQUUsSUFBSSxDQUFDLE1BQU07WUFDOUIsb0JBQW9CLEVBQUUsSUFBSSxDQUFDLFNBQVM7WUFDcEMsaUZBQWlGO1lBQ2pGLGdDQUFnQztZQUNoQyxLQUFLLEVBQUUsS0FBSztZQUNaLGNBQWMsRUFBRSxLQUFLO1NBQ3RCLENBQUMsQ0FBQztRQUVILElBQUksQ0FBQyxhQUFhLEdBQUcsSUFBSSxvQkFBSyxDQUFDLElBQUksRUFBRSxVQUFVLEVBQUU7WUFDL0MsU0FBUyxFQUFFLEdBQUcsTUFBTSxJQUFJLE1BQU0sQ0FBQyxPQUFPLFlBQVk7WUFDbEQsWUFBWSxFQUFFLEVBQUUsSUFBSSxFQUFFLFlBQVksRUFBRSxJQUFJLEVBQUUsNEJBQWEsQ0FBQyxNQUFNLEVBQUU7WUFDaEUsT0FBTyxFQUFFLEVBQUUsSUFBSSxFQUFFLFlBQVksRUFBRSxJQUFJLEVBQUUsNEJBQWEsQ0FBQyxNQUFNLEVBQUU7WUFDM0QsV0FBVyxFQUFFLDBCQUFXLENBQUMsZUFBZTtZQUN4QyxVQUFVLEVBQUUsOEJBQWUsQ0FBQyxXQUFXO1lBQ3ZDLDJFQUEyRTtZQUMzRSxtQkFBbUIsRUFBRSxZQUFZO1lBQ2pDLGdDQUFnQyxFQUFFO2dCQUNoQywwQkFBMEIsRUFBRSxNQUFNLENBQUMsbUJBQW1CO2FBQ3ZEO1lBQ0QsYUFBYSxFQUFFLE1BQU0sQ0FBQyxhQUFhO1NBQ3BDLENBQUMsQ0FBQztJQUNMLENBQUM7SUFFRCx3RkFBd0Y7SUFDeEYsSUFBVyxjQUFjO1FBQ3ZCLE9BQU8sSUFBSSxDQUFDLE1BQU0sQ0FBQyxhQUFhLENBQUMsU0FBUyxDQUFDLENBQUM7SUFDOUMsQ0FBQztJQUVELDZGQUE2RjtJQUM3RixJQUFXLGFBQWE7UUFDdEIsT0FBTyxJQUFJLENBQUMsTUFBTSxDQUFDLGFBQWEsQ0FBQyxHQUFHLElBQUksQ0FBQyxTQUFTLEdBQUcsQ0FBQyxDQUFDO0lBQ3pELENBQUM7Q0FDRjtBQTlERCxvREE4REMiLCJzb3VyY2VzQ29udGVudCI6WyJpbXBvcnQgKiBhcyBwYXRoIGZyb20gJ3BhdGgnO1xuaW1wb3J0IHsgQXdzLCBSZW1vdmFsUG9saWN5IH0gZnJvbSAnYXdzLWNkay1saWInO1xuaW1wb3J0IHsgQXR0cmlidXRlVHlwZSwgQmlsbGluZ01vZGUsIFRhYmxlLCBUYWJsZUVuY3J5cHRpb24gfSBmcm9tICdhd3MtY2RrLWxpYi9hd3MtZHluYW1vZGInO1xuaW1wb3J0IHsgQmxvY2tQdWJsaWNBY2Nlc3MsIEJ1Y2tldCwgQnVja2V0RW5jcnlwdGlvbiwgSUJ1Y2tldCwgT2JqZWN0T3duZXJzaGlwIH0gZnJvbSAnYXdzLWNkay1saWIvYXdzLXMzJztcbmltcG9ydCB7IEJ1Y2tldERlcGxveW1lbnQsIFNvdXJjZSB9IGZyb20gJ2F3cy1jZGstbGliL2F3cy1zMy1kZXBsb3ltZW50JztcbmltcG9ydCB7IENvbnN0cnVjdCB9IGZyb20gJ2NvbnN0cnVjdHMnO1xuaW1wb3J0IHsgRW52aXJvbm1lbnRDb25maWcgfSBmcm9tICcuLi9jb25maWcnO1xuXG5leHBvcnQgaW50ZXJmYWNlIEtub3dsZWRnZUJhc2VTdG9yYWdlUHJvcHMge1xuICByZWFkb25seSBjb25maWc6IEVudmlyb25tZW50Q29uZmlnO1xuICByZWFkb25seSBwcmVmaXg6IHN0cmluZztcbiAgLyoqIERpcmVjdG9yeSBvZiBzb3VyY2UgZG9jdW1lbnRzIHVwbG9hZGVkIHRvIGByYXcvYCBhdCBkZXBsb3kgdGltZS4gKi9cbiAgcmVhZG9ubHkgc2FtcGxlRG9jc1BhdGg6IHN0cmluZztcbn1cblxuLyoqXG4gKiBEdXJhYmxlIHN0YXRlIGZvciB0aGUga25vd2xlZGdlIGJhc2UgYWdlbnQuXG4gKlxuICogVHdvIHJlc291cmNlcywgd2l0aCBkaWZmZXJlbnQgam9iczpcbiAqXG4gKiAgLSAqKlMzKiogaG9sZHMgdGhlIHNvdXJjZSBkb2N1bWVudHMgdW5kZXIgYHJhdy9gIGFuZCB0aGUgYnVpbHQgdmVjdG9yIGluZGV4IHVuZGVyXG4gKiAgICBgaW5kZXgvYC4gVGhlIGluZGV4IGlzIGEgdmVyc2lvbmVkIGFydGlmYWN0IHJhdGhlciB0aGFuIGEgZGF0YWJhc2UgKEFEUi0wMSksIHdoaWNoIGlzXG4gKiAgICB3aHkgcmV0cmlldmFsIGNvc3RzIG5vdGhpbmcgYW5kIHdoeSB0aGUgY29tcHV0ZSBsYXllciBpcyBzdGF0ZWxlc3MuXG4gKiAgLSAqKkR5bmFtb0RCKiogaG9sZHMgdGhlIHF1ZXJ5IGxvZzogb25lIGl0ZW0gcGVyIHJlcXVlc3QsIGtleWVkIGJ5IHNlc3Npb24gc28gYVxuICogICAgY29udmVyc2F0aW9uIGNhbiBiZSByZWNvbnN0cnVjdGVkIHdoaWxlIGRlYnVnZ2luZy5cbiAqL1xuZXhwb3J0IGNsYXNzIEtub3dsZWRnZUJhc2VTdG9yYWdlIGV4dGVuZHMgQ29uc3RydWN0IHtcbiAgcHVibGljIHJlYWRvbmx5IGJ1Y2tldDogSUJ1Y2tldDtcbiAgcHVibGljIHJlYWRvbmx5IHF1ZXJ5TG9nVGFibGU6IFRhYmxlO1xuICBwdWJsaWMgcmVhZG9ubHkgcmF3UHJlZml4ID0gJ3Jhdy8nO1xuICBwdWJsaWMgcmVhZG9ubHkgaW5kZXhLZXkgPSAnaW5kZXgva2ItaW5kZXguanNvbi5neic7XG5cbiAgY29uc3RydWN0b3Ioc2NvcGU6IENvbnN0cnVjdCwgaWQ6IHN0cmluZywgcHJvcHM6IEtub3dsZWRnZUJhc2VTdG9yYWdlUHJvcHMpIHtcbiAgICBzdXBlcihzY29wZSwgaWQpO1xuXG4gICAgY29uc3QgeyBjb25maWcsIHByZWZpeCwgc2FtcGxlRG9jc1BhdGggfSA9IHByb3BzO1xuXG4gICAgLy8gVGhlIGFjY291bnQgYW5kIHJlZ2lvbiB0b2tlbnMgcmVzb2x2ZSBhdCBkZXBsb3kgdGltZSwgd2hpY2ggaXMgd2hhdCBrZWVwcyB0aGUgc2FtZVxuICAgIC8vIGNvZGUgZGVwbG95YWJsZSB0byBtb3JlIHRoYW4gb25lIGFjY291bnQ6IHRoZSBuYW1lIGlzIGdsb2JhbGx5IHVuaXF1ZSBwZXIgYWNjb3VudFxuICAgIC8vIHdpdGhvdXQgYW55b25lIGVkaXRpbmcgaXQuXG4gICAgdGhpcy5idWNrZXQgPSBuZXcgQnVja2V0KHRoaXMsICdCdWNrZXQnLCB7XG4gICAgICBidWNrZXROYW1lOiBgJHtwcmVmaXh9LSR7Y29uZmlnLmVudk5hbWV9LSR7QXdzLkFDQ09VTlRfSUR9LSR7QXdzLlJFR0lPTn1gLFxuICAgICAgYmxvY2tQdWJsaWNBY2Nlc3M6IEJsb2NrUHVibGljQWNjZXNzLkJMT0NLX0FMTCxcbiAgICAgIGVuY3J5cHRpb246IEJ1Y2tldEVuY3J5cHRpb24uUzNfTUFOQUdFRCxcbiAgICAgIGVuZm9yY2VTU0w6IHRydWUsXG4gICAgICB2ZXJzaW9uZWQ6IHRydWUsXG4gICAgICBvYmplY3RPd25lcnNoaXA6IE9iamVjdE93bmVyc2hpcC5CVUNLRVRfT1dORVJfRU5GT1JDRUQsXG4gICAgICByZW1vdmFsUG9saWN5OiBjb25maWcucmVtb3ZhbFBvbGljeSxcbiAgICAgIGF1dG9EZWxldGVPYmplY3RzOiBjb25maWcuYXV0b0RlbGV0ZU9iamVjdHMsXG4gICAgfSk7XG5cbiAgICAvLyBTaGlwcyB0aGUgdmVyc2lvbi1jb250cm9sbGVkIHNhbXBsZSBkb2N1bWVudHMgd2l0aCB0aGUgc3RhY2ssIHNvIGEgc2luZ2xlXG4gICAgLy8gYGNkayBkZXBsb3lgIHByb2R1Y2VzIGEgc3lzdGVtIHdpdGggYSBwb3B1bGF0ZWQga25vd2xlZGdlIGJhc2UuIERvY3VtZW50IGluZ2VzdGlvblxuICAgIC8vIGlzIGludGVudGlvbmFsbHkgb3V0IG9mIHNjb3BlOyB0aGlzIGlzIHdoYXQgcmVwbGFjZXMgaXQuXG4gICAgbmV3IEJ1Y2tldERlcGxveW1lbnQodGhpcywgJ1NhbXBsZURvY3MnLCB7XG4gICAgICBzb3VyY2VzOiBbU291cmNlLmFzc2V0KHBhdGgucmVzb2x2ZShzYW1wbGVEb2NzUGF0aCkpXSxcbiAgICAgIGRlc3RpbmF0aW9uQnVja2V0OiB0aGlzLmJ1Y2tldCxcbiAgICAgIGRlc3RpbmF0aW9uS2V5UHJlZml4OiB0aGlzLnJhd1ByZWZpeCxcbiAgICAgIC8vIExlYXZlIGFueXRoaW5nIGVsc2UgaW4gdGhlIGJ1Y2tldCBhbG9uZSAtLSBub3RhYmx5IHRoZSBidWlsdCBpbmRleCwgd2hpY2ggdGhpc1xuICAgICAgLy8gZGVwbG95bWVudCBtdXN0IG5ldmVyIGRlbGV0ZS5cbiAgICAgIHBydW5lOiBmYWxzZSxcbiAgICAgIHJldGFpbk9uRGVsZXRlOiBmYWxzZSxcbiAgICB9KTtcblxuICAgIHRoaXMucXVlcnlMb2dUYWJsZSA9IG5ldyBUYWJsZSh0aGlzLCAnUXVlcnlMb2cnLCB7XG4gICAgICB0YWJsZU5hbWU6IGAke3ByZWZpeH0tJHtjb25maWcuZW52TmFtZX0tcXVlcnktbG9nYCxcbiAgICAgIHBhcnRpdGlvbktleTogeyBuYW1lOiAnc2Vzc2lvbl9pZCcsIHR5cGU6IEF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgICBzb3J0S2V5OiB7IG5hbWU6ICd0c19yZXF1ZXN0JywgdHlwZTogQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICAgIGJpbGxpbmdNb2RlOiBCaWxsaW5nTW9kZS5QQVlfUEVSX1JFUVVFU1QsXG4gICAgICBlbmNyeXB0aW9uOiBUYWJsZUVuY3J5cHRpb24uQVdTX01BTkFHRUQsXG4gICAgICAvLyBRdWVyeSBsb2dzIGFyZSBhIGRlYnVnZ2luZyBhaWQsIG5vdCBhIHN5c3RlbSBvZiByZWNvcmQ6IGxldCB0aGVtIGV4cGlyZS5cbiAgICAgIHRpbWVUb0xpdmVBdHRyaWJ1dGU6ICdleHBpcmVzX2F0JyxcbiAgICAgIHBvaW50SW5UaW1lUmVjb3ZlcnlTcGVjaWZpY2F0aW9uOiB7XG4gICAgICAgIHBvaW50SW5UaW1lUmVjb3ZlcnlFbmFibGVkOiBjb25maWcucG9pbnRJblRpbWVSZWNvdmVyeSxcbiAgICAgIH0sXG4gICAgICByZW1vdmFsUG9saWN5OiBjb25maWcucmVtb3ZhbFBvbGljeSxcbiAgICB9KTtcbiAgfVxuXG4gIC8qKiBBUk4gcGF0dGVybiBmb3IgdGhlIGluZGV4IG9iamVjdCwgZm9yIHNjb3BpbmcgdGhlIHF1ZXJ5IExhbWJkYSdzIHJlYWQgcGVybWlzc2lvbi4gKi9cbiAgcHVibGljIGdldCBpbmRleE9iamVjdEFybigpOiBzdHJpbmcge1xuICAgIHJldHVybiB0aGlzLmJ1Y2tldC5hcm5Gb3JPYmplY3RzKCdpbmRleC8qJyk7XG4gIH1cblxuICAvKiogQVJOIHBhdHRlcm4gZm9yIHRoZSBzb3VyY2UgZG9jdW1lbnRzLCBmb3Igc2NvcGluZyB0aGUgaW5nZXN0IExhbWJkYSdzIHJlYWQgcGVybWlzc2lvbi4gKi9cbiAgcHVibGljIGdldCByYXdPYmplY3RzQXJuKCk6IHN0cmluZyB7XG4gICAgcmV0dXJuIHRoaXMuYnVja2V0LmFybkZvck9iamVjdHMoYCR7dGhpcy5yYXdQcmVmaXh9KmApO1xuICB9XG59XG5cbi8qKiBSZS1leHBvcnRlZCBzbyBjYWxsZXJzIGRvIG5vdCBuZWVkIHRvIGltcG9ydCBmcm9tIGF3cy1jZGstbGliIGp1c3QgdG8gcmVhZCB0aGUgcG9saWN5LiAqL1xuZXhwb3J0IHsgUmVtb3ZhbFBvbGljeSB9O1xuIl19