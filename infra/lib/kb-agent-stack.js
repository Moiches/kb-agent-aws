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
exports.KbAgentStack = exports.PROVIDER_KEY_PLACEHOLDER = void 0;
const path = __importStar(require("path"));
const aws_cdk_lib_1 = require("aws-cdk-lib");
const aws_secretsmanager_1 = require("aws-cdk-lib/aws-secretsmanager");
const aws_cdk_lib_2 = require("aws-cdk-lib");
const knowledge_base_seeder_1 = require("./constructs/knowledge-base-seeder");
const knowledge_base_storage_1 = require("./constructs/knowledge-base-storage");
const observability_1 = require("./constructs/observability");
const query_api_1 = require("./constructs/query-api");
/**
 * Placeholder written into the provider API key secret at creation time.
 *
 * Shared contract with services/ingest/handler.py, which treats it as "no key configured"
 * and skips seeding cleanly instead of failing the deployment. Not a secret: it is a marker,
 * so having it visible in the template is harmless and intentional.
 */
exports.PROVIDER_KEY_PLACEHOLDER = 'REPLACE_WITH_PROVIDER_API_KEY';
/**
 * The knowledge base agent, as one stack composed of constructs (ADR-05).
 *
 * One stack rather than several: cross-stack references become CloudFormation exports, and
 * CloudFormation refuses to delete a stack whose exports are in use. For a reviewer who
 * deploys and destroys once, that is friction with no benefit. Constructs give the same
 * modularity without it.
 */
class KbAgentStack extends aws_cdk_lib_1.Stack {
    constructor(scope, id, props) {
        super(scope, id, props);
        const { settings } = props;
        const { env, prefix } = settings;
        this.guardAccount(settings);
        aws_cdk_lib_1.Tags.of(this).add('project', 'kb-agent');
        aws_cdk_lib_1.Tags.of(this).add('environment', env.envName);
        const storage = new knowledge_base_storage_1.KnowledgeBaseStorage(this, 'Storage', {
            config: env,
            prefix,
            sampleDocsPath: path.join(__dirname, '..', '..', 'sample-docs'),
        });
        // Seeded with a recognisable placeholder rather than a value, and deliberately NOT with
        // CDK's default: `new Secret()` with no props generates a *random string*, which the
        // ingest Lambda would happily send to the provider, collect a 401 for, and fail the
        // deployment over. A sentinel it can recognise is what makes "not configured yet" a
        // state the system can report instead of a rollback.
        //
        // The real value is injected out of band after the first deploy. Routing it through CDK
        // context would write the key in plaintext into the CloudFormation template, which lands
        // in the bootstrap bucket and the stack history for anyone with read access.
        const providerApiKeySecret = new aws_secretsmanager_1.Secret(this, 'ProviderApiKey', {
            secretName: `${prefix}-${env.envName}/provider-api-key`,
            description: 'API key for the model provider (OpenRouter by default -- see ADR-09). ' +
                'Populate with: aws secretsmanager put-secret-value --secret-id <name> --secret-string sk-or-...',
            secretStringValue: aws_cdk_lib_2.SecretValue.unsafePlainText(exports.PROVIDER_KEY_PLACEHOLDER),
            removalPolicy: env.removalPolicy,
        });
        const seeder = new knowledge_base_seeder_1.KnowledgeBaseSeeder(this, 'Seeder', {
            config: env,
            models: settings.models,
            prefix,
            bucket: storage.bucket,
            rawPrefix: storage.rawPrefix,
            indexKey: storage.indexKey,
            providerApiKeySecret,
            // The documents have to be in the bucket before there is anything to index.
            executeAfter: [storage],
        });
        const api = new query_api_1.QueryApi(this, 'Api', {
            config: env,
            cloudWatchRole: settings.cloudWatchRole,
            models: settings.models,
            prefix,
            bucket: storage.bucket,
            indexKey: storage.indexKey,
            rawPrefix: storage.rawPrefix,
            queryLogTable: storage.queryLogTable,
            providerApiKeySecret,
            ingestFunction: seeder.function,
        });
        const observability = new observability_1.Observability(this, 'Observability', {
            config: env,
            prefix,
            api: api.api,
            queryFunction: api.queryFunction,
            alertEmail: settings.alertEmail,
        });
        new aws_cdk_lib_1.CfnOutput(this, 'DashboardUrl', {
            value: `https://${aws_cdk_lib_1.Aws.REGION}.console.aws.amazon.com/cloudwatch/home?region=${aws_cdk_lib_1.Aws.REGION}#dashboards:name=${observability.dashboard.dashboardName}`,
            description: 'CloudWatch dashboard: traffic, latency, answer quality and token spend',
        });
        new aws_cdk_lib_1.CfnOutput(this, 'ApiBaseUrl', {
            value: api.api.url.replace(/\/$/, ''),
            description: 'Base URL for the Streamlit client. Every route requires a bearer token.',
        });
        new aws_cdk_lib_1.CfnOutput(this, 'ApiTokenSecret', {
            value: api.apiTokenSecret.secretName,
            description: 'Secrets Manager name of the API bearer token, for scripts/configure_client.py',
        });
        new aws_cdk_lib_1.CfnOutput(this, 'GetTokenCommand', {
            value: api.tokenCommand,
            description: 'Prints the API bearer token. Put it in client/.streamlit/secrets.toml.',
        });
        new aws_cdk_lib_1.CfnOutput(this, 'ProviderApiKeySecret', {
            value: providerApiKeySecret.secretName,
            description: 'Set this secret before seeding: aws secretsmanager put-secret-value --secret-id <this> --secret-string sk-or-...',
        });
        new aws_cdk_lib_1.CfnOutput(this, 'SeedCommand', {
            value: seeder.seedCommand,
            description: 'Builds the vector index. Run after populating the provider API key secret.',
        });
        new aws_cdk_lib_1.CfnOutput(this, 'KnowledgeBaseBucket', {
            value: storage.bucket.bucketName,
            description: 'S3 bucket holding source documents (raw/) and the vector index (index/)',
        });
        new aws_cdk_lib_1.CfnOutput(this, 'QueryLogTable', {
            value: storage.queryLogTable.tableName,
            description: 'DynamoDB table holding one item per query, for debugging and evaluation',
        });
        // Configuration echo, not verified state. Nothing in this stack has contacted the
        // provider: it reports what the query Lambda will be told to use once it exists. The
        // authoritative answer at runtime comes from GET /health, which reports the provider
        // and model that actually served a request.
        new aws_cdk_lib_1.CfnOutput(this, 'ConfiguredModelProvider', {
            value: `${settings.models.provider} | generation=${settings.models.generationModel} | embeddings=${settings.models.embeddingModel} (configured, not yet verified)`,
            description: 'Model provider this stack is configured for (ADR-09). This is configuration only -- ' +
                'no credential has been checked and no model has been called. GET /health reports what is actually in use.',
        });
        new aws_cdk_lib_1.CfnOutput(this, 'DeployedToAccount', {
            value: `${aws_cdk_lib_1.Aws.ACCOUNT_ID} / ${aws_cdk_lib_1.Aws.REGION}`,
            description: 'Account and region this stack landed in -- check before demoing',
        });
    }
    /**
     * Optional account guard.
     *
     * Deliberately opt-in. A hard-coded account would defeat the point of an
     * environment-agnostic stack: the same commit has to deploy to a personal development
     * account and to the AMCRO sandbox. Passing `-c expectedAccount=<id>` turns on the check
     * for people who keep several profiles and would rather fail than deploy to the wrong one.
     */
    guardAccount(settings) {
        const { expectedAccount } = settings;
        if (!expectedAccount) {
            return;
        }
        if (aws_cdk_lib_1.Token.isUnresolved(this.account)) {
            aws_cdk_lib_1.Annotations.of(this).addWarningV2('kb-agent:account-guard-skipped', 'expectedAccount was set but the stack is environment-agnostic, so the account is ' +
                'only known at deploy time. The guard cannot run during synth.');
            return;
        }
        if (this.account !== expectedAccount) {
            throw new Error(`Account guard: this stack is configured for account ${expectedAccount} but the ` +
                `active credentials resolve to ${this.account}. Check your --profile.`);
        }
    }
}
exports.KbAgentStack = KbAgentStack;
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoia2ItYWdlbnQtc3RhY2suanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyJrYi1hZ2VudC1zdGFjay50cyJdLCJuYW1lcyI6W10sIm1hcHBpbmdzIjoiOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7QUFBQSwyQ0FBNkI7QUFDN0IsNkNBQTBGO0FBRTFGLHVFQUF3RDtBQUN4RCw2Q0FBMEM7QUFFMUMsOEVBQXlFO0FBQ3pFLGdGQUEyRTtBQUMzRSw4REFBMkQ7QUFDM0Qsc0RBQWtEO0FBRWxEOzs7Ozs7R0FNRztBQUNVLFFBQUEsd0JBQXdCLEdBQUcsK0JBQStCLENBQUM7QUFNeEU7Ozs7Ozs7R0FPRztBQUNILE1BQWEsWUFBYSxTQUFRLG1CQUFLO0lBQ3JDLFlBQVksS0FBZ0IsRUFBRSxFQUFVLEVBQUUsS0FBd0I7UUFDaEUsS0FBSyxDQUFDLEtBQUssRUFBRSxFQUFFLEVBQUUsS0FBSyxDQUFDLENBQUM7UUFFeEIsTUFBTSxFQUFFLFFBQVEsRUFBRSxHQUFHLEtBQUssQ0FBQztRQUMzQixNQUFNLEVBQUUsR0FBRyxFQUFFLE1BQU0sRUFBRSxHQUFHLFFBQVEsQ0FBQztRQUVqQyxJQUFJLENBQUMsWUFBWSxDQUFDLFFBQVEsQ0FBQyxDQUFDO1FBRTVCLGtCQUFJLENBQUMsRUFBRSxDQUFDLElBQUksQ0FBQyxDQUFDLEdBQUcsQ0FBQyxTQUFTLEVBQUUsVUFBVSxDQUFDLENBQUM7UUFDekMsa0JBQUksQ0FBQyxFQUFFLENBQUMsSUFBSSxDQUFDLENBQUMsR0FBRyxDQUFDLGFBQWEsRUFBRSxHQUFHLENBQUMsT0FBTyxDQUFDLENBQUM7UUFFOUMsTUFBTSxPQUFPLEdBQUcsSUFBSSw2Q0FBb0IsQ0FBQyxJQUFJLEVBQUUsU0FBUyxFQUFFO1lBQ3hELE1BQU0sRUFBRSxHQUFHO1lBQ1gsTUFBTTtZQUNOLGNBQWMsRUFBRSxJQUFJLENBQUMsSUFBSSxDQUFDLFNBQVMsRUFBRSxJQUFJLEVBQUUsSUFBSSxFQUFFLGFBQWEsQ0FBQztTQUNoRSxD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QUUsR0FBRyxRQUFRLENBQUM7UUFDckMsSUFBSSxDQUFDLGVBQWUsRUFBRSxDQUFDO1lBQ3JCLE9BQU87UUFDVCxDQUFDO1FBQ0QsSUFBSSxtQkFBSyxDQUFDLFlBQVksQ0FBQyxJQUFJLENBQUMsT0FBTyxDQUFDLEVBQUUsQ0FBQztZQUNyQyx5QkFBVyxDQUFDLEVBQUUsQ0FBQyxJQUFJLENBQUMsQ0FBQyxZQUFZLENBQy9CLGdDQUFnQyxFQUNoQyxtRkFBbUY7Z0JBQ2pGLCtEQUErRCxDQUNsRSxDQUFDO1lBQ0YsT0FBTztRQUNULENBQUM7UUFDRCxJQUFJLElBQUksQ0FBQyxPQUFPLEtBQUssZUFBZSxFQUFFLENBQUM7WUFDckMsTUFBTSxJQUFJLEtBQUssQ0FDYix1REFBdUQsZUFBZSxXQUFXO2dCQUMvRSxpQ0FBaUMsSUFBSSxDQUFDLE9BQU8seUJBQXlCLENBQ3pFLENBQUM7UUFDSixDQUFDO0lBQ0gsQ0FBQztDQUNGO0FBMUpELG9DQTBKQyIsInNvdXJjZXNDb250ZW50IjpbImltcG9ydCAqIGFzIHBhdGggZnJvbSAncGF0aCc7XHJcbmltcG9ydCB7IEFubm90YXRpb25zLCBBd3MsIENmbk91dHB1dCwgU3RhY2ssIFN0YWNrUHJvcHMsIFRhZ3MsIFRva2VuIH0gZnJvbSAnYXdzLWNkay1saWInO1xyXG5pbXBvcnQgeyBDb25zdHJ1Y3QgfSBmcm9tICdjb25zdHJ1Y3RzJztcclxuaW1wb3J0IHsgU2VjcmV0IH0gZnJvbSAnYXdzLWNkay1saWIvYXdzLXNlY3JldHNtYW5hZ2VyJztcclxuaW1wb3J0IHsgU2VjcmV0VmFsdWUgfSBmcm9tICdhd3MtY2RrLWxpYic7XHJcbmltcG9ydCB7IFN0YWNrU2V0dGluZ3MgfSBmcm9tICcuL2NvbmZpZyc7XHJcbmltcG9ydCB7IEtub3dsZWRnZUJhc2VTZWVkZXIgfSBmcm9tICcuL2NvbnN0cnVjdHMva25vd2xlZGdlLWJhc2Utc2VlZGVyJztcclxuaW1wb3J0IHsgS25vd2xlZGdlQmFzZVN0b3JhZ2UgfSBmcm9tICcuL2NvbnN0cnVjdHMva25vd2xlZGdlLWJhc2Utc3RvcmFnZSc7XHJcbmltcG9ydCB7IE9ic2VydmFiaWxpdHkgfSBmcm9tICcuL2NvbnN0cnVjdHMvb2JzZXJ2YWJpbGl0eSc7XHJcbmltcG9ydCB7IFF1ZXJ5QXBpIH0gZnJvbSAnLi9jb25zdHJ1Y3RzL3F1ZXJ5LWFwaSc7XHJcblxyXG4vKipcclxuICogUGxhY2Vob2xkZXIgd3JpdHRlbiBpbnRvIHRoZSBwcm92aWRlciBBUEkga2V5IHNlY3JldCBhdCBjcmVhdGlvbiB0aW1lLlxyXG4gKlxyXG4gKiBTaGFyZWQgY29udHJhY3Qgd2l0aCBzZXJ2aWNlcy9pbmdlc3QvaGFuZGxlci5weSwgd2hpY2ggdHJlYXRzIGl0IGFzIFwibm8ga2V5IGNvbmZpZ3VyZWRcIlxyXG4gKiBhbmQgc2tpcHMgc2VlZGluZyBjbGVhbmx5IGluc3RlYWQgb2YgZmFpbGluZyB0aGUgZGVwbG95bWVudC4gTm90IGEgc2VjcmV0OiBpdCBpcyBhIG1hcmtlcixcclxuICogc28gaGF2aW5nIGl0IHZpc2libGUgaW4gdGhlIHRlbXBsYXRlIGlzIGhhcm1sZXNzIGFuZCBpbnRlbnRpb25hbC5cclxuICovXHJcbmV4cG9ydCBjb25zdCBQUk9WSURFUl9LRVlfUExBQ0VIT0xERVIgPSAnUkVQTEFDRV9XSVRIX1BST1ZJREVSX0FQSV9LRVknO1xyXG5cclxuZXhwb3J0IGludGVyZmFjZSBLYkFnZW50U3RhY2tQcm9wcyBleHRlbmRzIFN0YWNrUHJvcHMge1xyXG4gIHJlYWRvbmx5IHNldHRpbmdzOiBTdGFja1NldHRpbmdzO1xyXG59XHJcblxyXG4vKipcclxuICogVGhlIGtub3dsZWRnZSBiYXNlIGFnZW50LCBhcyBvbmUgc3RhY2sgY29tcG9zZWQgb2YgY29uc3RydWN0cyAoQURSLTA1KS5cclxuICpcclxuICogT25lIHN0YWNrIHJhdGhlciB0aGFuIHNldmVyYWw6IGNyb3NzLXN0YWNrIHJlZmVyZW5jZXMgYmVjb21lIENsb3VkRm9ybWF0aW9uIGV4cG9ydHMsIGFuZFxyXG4gKiBDbG91ZEZvcm1hdGlvbiByZWZ1c2VzIHRvIGRlbGV0ZSBhIHN0YWNrIHdob3NlIGV4cG9ydHMgYXJlIGluIHVzZS4gRm9yIGEgcmV2aWV3ZXIgd2hvXHJcbiAqIGRlcGxveXMgYW5kIGRlc3Ryb3lzIG9uY2UsIHRoYXQgaXMgZnJpY3Rpb24gd2l0aCBubyBiZW5lZml0LiBDb25zdHJ1Y3RzIGdpdmUgdGhlIHNhbWVcclxuICogbW9kdWxhcml0eSB3aXRob3V0IGl0LlxyXG4gKi9cclxuZXhwb3J0IGNsYXNzIEtiQWdlbnRTdGFjayBleHRlbmRzIFN0YWNrIHtcclxuICBjb25zdHJ1Y3RvcihzY29wZTogQ29uc3RydWN0LCBpZDogc3RyaW5nLCBwcm9wczogS2JBZ2VudFN0YWNrUHJvcHMpIHtcclxuICAgIHN1cGVyKHNjb3BlLCBpZCwgcHJvcHMpO1xyXG5cclxuICAgIGNvbnN0IHsgc2V0dGluZ3MgfSA9IHByb3BzO1xyXG4gICAgY29uc3QgeyBlbnYsIHByZWZpeCB9ID0gc2V0dGluZ3M7XHJcblxyXG4gICAgdGhpcy5ndWFyZEFjY291bnQoc2V0dGluZ3MpO1xyXG5cclxuICAgIFRhZ3Mub2YodGhpcykuYWRkKCdwcm9qZWN0JywgJ2tiLWFnZW50Jyk7XHJcbiAgICBUYWdzLm9mKHRoaXMpLmFkZCgnZW52aXJvbm1lbnQnLCBlbnYuZW52TmFtZSk7XHJcblxyXG4gICAgY29uc3Qgc3RvcmFnZSA9IG5ldyBLbm93bGVkZ2VCYXNlU3RvcmFnZSh0aGlzLCAnU3RvcmFnZScsIHtcclxuICAgICAgY29uZmlnOiBlbnYsXHJcbiAgICAgIHByZWZpeCxcclxuICAgICAgc2FtcGxlRG9jc1BhdGg6IHBhdGguam9pbihfX2Rpcm5hbWUsICcuLicsICcuLicsICdzYW1wbGUtZG9jcycpLFxyXG4gICAgfSk7XHJcblxyXG4gICAgLy8gU2VlZGVkIHdpdGggYSByZWNvZ25pc2FibGUgcGxhY2Vob2xkZXIgcmF0aGVyIHRoYW4gYSB2YWx1ZSwgYW5kIGRlbGliZXJhdGVseSBOT1Qgd2l0aFxyXG4gICAgLy8gQ0RLJ3MgZGVmYXVsdDogYG5ldyBTZWNyZXQoKWAgd2l0aCBubyBwcm9wcyBnZW5lcmF0ZXMgYSAqcmFuZG9tIHN0cmluZyosIHdoaWNoIHRoZVxyXG4gICAgLy8gaW5nZXN0IExhbWJkYSB3b3VsZCBoYXBwaWx5IHNlbmQgdG8gdGhlIHByb3ZpZGVyLCBjb2xsZWN0IGEgNDAxIGZvciwgYW5kIGZhaWwgdGhlXHJcbiAgICAvLyBkZXBsb3ltZW50IG92ZXIuIEEgc2VudGluZWwgaXQgY2FuIHJlY29nbmlzZSBpcyB3aGF0IG1ha2VzIFwibm90IGNvbmZpZ3VyZWQgeWV0XCIgYVxyXG4gICAgLy8gc3RhdGUgdGhlIHN5c3RlbSBjYW4gcmVwb3J0IGluc3RlYWQgb2YgYSByb2xsYmFjay5cclxuICAgIC8vXHJcbiAgICAvLyBUaGUgcmVhbCB2YWx1ZSBpcyBpbmplY3RlZCBvdXQgb2YgYmFuZCBhZnRlciB0aGUgZmlyc3QgZGVwbG95LiBSb3V0aW5nIGl0IHRocm91Z2ggQ0RLXHJcbiAgICAvLyBjb250ZXh0IHdvdWxkIHdyaXRlIHRoZSBrZXkgaW4gcGxhaW50ZXh0IGludG8gdGhlIENsb3VkRm9ybWF0aW9uIHRlbXBsYXRlLCB3aGljaCBsYW5kc1xyXG4gICAgLy8gaW4gdGhlIGJvb3RzdHJhcCBidWNrZXQgYW5kIHRoZSBzdGFjayBoaXN0b3J5IGZvciBhbnlvbmUgd2l0aCByZWFkIGFjY2Vzcy5cclxuICAgIGNvbnN0IHByb3ZpZGVyQXBpS2V5U2VjcmV0ID0gbmV3IFNlY3JldCh0aGlzLCAnUHJvdmlkZXJBcGlLZXknLCB7XHJcbiAgICAgIHNlY3JldE5hbWU6IGAke3ByZWZpeH0tJHtlbnYuZW52TmFtZX0vcHJvdmlkZXItYXBpLWtleWAsXHJcbiAgICAgIGRlc2NyaXB0aW9uOlxyXG4gICAgICAgICdBUEkga2V5IGZvciB0aGUgbW9kZWwgcHJvdmlkZXIgKE9wZW5Sb3V0ZXIgYnkgZGVmYXVsdCAtLSBzZWUgQURSLTA5KS4gJyArXHJcbiAgICAgICAgJ1BvcHVsYXRlIHdpdGg6IGF3cyBzZWNyZXRzbWFuYWdlciBwdXQtc2VjcmV0LXZhbHVlIC0tc2VjcmV0LWlkIDxuYW1lPiAtLXNlY3JldC1zdHJpbmcgc2stb3ItLi4uJyxcclxuICAgICAgc2VjcmV0U3RyaW5nVmFsdWU6IFNlY3JldFZhbHVlLnVuc2FmZVBsYWluVGV4dChQUk9WSURFUl9LRVlfUExBQ0VIT0xERVIpLFxyXG4gICAgICByZW1vdmFsUG9saWN5OiBlbnYucmVtb3ZhbFBvbGljeSxcclxuICAgIH0pO1xyXG5cclxuICAgIGNvbnN0IHNlZWRlciA9IG5ldyBLbm93bGVkZ2VCYXNlU2VlZGVyKHRoaXMsICdTZWVkZXInLCB7XHJcbiAgICAgIGNvbmZpZzogZW52LFxyXG4gICAgICBtb2RlbHM6IHNldHRpbmdzLm1vZGVscyxcclxuICAgICAgcHJlZml4LFxyXG4gICAgICBidWNrZXQ6IHN0b3JhZ2UuYnVja2V0LFxyXG4gICAgICByYXdQcmVmaXg6IHN0b3JhZ2UucmF3UHJlZml4LFxyXG4gICAgICBpbmRleEtleTogc3RvcmFnZS5pbmRleEtleSxcclxuICAgICAgcHJvdmlkZXJBcGlLZXlTZWNyZXQsXHJcbiAgICAgIC8vIFRoZSBkb2N1bWVudHMgaGF2ZSB0byBiZSBpbiB0aGUgYnVja2V0IGJlZm9yZSB0aGVyZSBpcyBhbnl0aGluZyB0byBpbmRleC5cclxuICAgICAgZXhlY3V0ZUFmdGVyOiBbc3RvcmFnZV0sXHJcbiAgICB9KTtcclxuXHJcbiAgICBjb25zdCBhcGkgPSBuZXcgUXVlcnlBcGkodGhpcywgJ0FwaScsIHtcclxuICAgICAgY29uZmlnOiBlbnYsXHJcbiAgICAgIGNsb3VkV2F0Y2hSb2xlOiBzZXR0aW5ncy5jbG91ZFdhdGNoUm9sZSxcclxuICAgICAgbW9kZWxzOiBzZXR0aW5ncy5tb2RlbHMsXHJcbiAgICAgIHByZWZpeCxcclxuICAgICAgYnVja2V0OiBzdG9yYWdlLmJ1Y2tldCxcclxuICAgICAgaW5kZXhLZXk6IHN0b3JhZ2UuaW5kZXhLZXksXHJcbiAgICAgIHJhd1ByZWZpeDogc3RvcmFnZS5yYXdQcmVmaXgsXHJcbiAgICAgIHF1ZXJ5TG9nVGFibGU6IHN0b3JhZ2UucXVlcnlMb2dUYWJsZSxcclxuICAgICAgcHJvdmlkZXJBcGlLZXlTZWNyZXQsXHJcbiAgICAgIGluZ2VzdEZ1bmN0aW9uOiBzZWVkZXIuZnVuY3Rpb24sXHJcbiAgICB9KTtcclxuXHJcbiAgICBjb25zdCBvYnNlcnZhYmlsaXR5ID0gbmV3IE9ic2VydmFiaWxpdHkodGhpcywgJ09ic2VydmFiaWxpdHknLCB7XHJcbiAgICAgIGNvbmZpZzogZW52LFxyXG4gICAgICBwcmVmaXgsXHJcbiAgICAgIGFwaTogYXBpLmFwaSxcclxuICAgICAgcXVlcnlGdW5jdGlvbjogYXBpLnF1ZXJ5RnVuY3Rpb24sXHJcbiAgICAgIGFsZXJ0RW1haWw6IHNldHRpbmdzLmFsZXJ0RW1haWwsXHJcbiAgICB9KTtcclxuXHJcbiAgICBuZXcgQ2ZuT3V0cHV0KHRoaXMsICdEYXNoYm9hcmRVcmwnLCB7XHJcbiAgICAgIHZhbHVlOiBgaHR0cHM6Ly8ke0F3cy5SRUdJT059LmNvbnNvbGUuYXdzLmFtYXpvbi5jb20vY2xvdWR3YXRjaC9ob21lP3JlZ2lvbj0ke0F3cy5SRUdJT059I2Rhc2hib2FyZHM6bmFtZT0ke29ic2VydmFiaWxpdHkuZGFzaGJvYXJkLmRhc2hib2FyZE5hbWV9YCxcclxuICAgICAgZGVzY3JpcHRpb246ICdDbG91ZFdhdGNoIGRhc2hib2FyZDogdHJhZmZpYywgbGF0ZW5jeSwgYW5zd2VyIHF1YWxpdHkgYW5kIHRva2VuIHNwZW5kJyxcclxuICAgIH0pO1xyXG5cclxuICAgIG5ldyBDZm5PdXRwdXQodGhpcywgJ0FwaUJhc2VVcmwnLCB7XHJcbiAgICAgIHZhbHVlOiBhcGkuYXBpLnVybC5yZXBsYWNlKC9cXC8kLywgJycpLFxyXG4gICAgICBkZXNjcmlwdGlvbjogJ0Jhc2UgVVJMIGZvciB0aGUgU3RyZWFtbGl0IGNsaWVudC4gRXZlcnkgcm91dGUgcmVxdWlyZXMgYSBiZWFyZXIgdG9rZW4uJyxcclxuICAgIH0pO1xyXG5cclxuICAgIG5ldyBDZm5PdXRwdXQodGhpcywgJ0FwaVRva2VuU2VjcmV0Jywge1xyXG4gICAgICB2YWx1ZTogYXBpLmFwaVRva2VuU2VjcmV0LnNlY3JldE5hbWUsXHJcbiAgICAgIGRlc2NyaXB0aW9uOiAnU2VjcmV0cyBNYW5hZ2VyIG5hbWUgb2YgdGhlIEFQSSBiZWFyZXIgdG9rZW4sIGZvciBzY3JpcHRzL2NvbmZpZ3VyZV9jbGllbnQucHknLFxyXG4gICAgfSk7XHJcblxyXG4gICAgbmV3IENmbk91dHB1dCh0aGlzLCAnR2V0VG9rZW5Db21tYW5kJywge1xyXG4gICAgICB2YWx1ZTogYXBpLnRva2VuQ29tbWFuZCxcclxuICAgICAgZGVzY3JpcHRpb246ICdQcmludHMgdGhlIEFQSSBiZWFyZXIgdG9rZW4uIFB1dCBpdCBpbiBjbGllbnQvLnN0cmVhbWxpdC9zZWNyZXRzLnRvbWwuJyxcclxuICAgIH0pO1xyXG5cclxuICAgIG5ldyBDZm5PdXRwdXQodGhpcywgJ1Byb3ZpZGVyQXBpS2V5U2VjcmV0Jywge1xyXG4gICAgICB2YWx1ZTogcHJvdmlkZXJBcGlLZXlTZWNyZXQuc2VjcmV0TmFtZSxcclxuICAgICAgZGVzY3JpcHRpb246ICdTZXQgdGhpcyBzZWNyZXQgYmVmb3JlIHNlZWRpbmc6IGF3cyBzZWNyZXRzbWFuYWdlciBwdXQtc2VjcmV0LXZhbHVlIC0tc2VjcmV0LWlkIDx0aGlzPiAtLXNlY3JldC1zdHJpbmcgc2stb3ItLi4uJyxcclxuICAgIH0pO1xyXG5cclxuICAgIG5ldyBDZm5PdXRwdXQodGhpcywgJ1NlZWRDb21tYW5kJywge1xyXG4gICAgICB2YWx1ZTogc2VlZGVyLnNlZWRDb21tYW5kLFxyXG4gICAgICBkZXNjcmlwdGlvbjogJ0J1aWxkcyB0aGUgdmVjdG9yIGluZGV4LiBSdW4gYWZ0ZXIgcG9wdWxhdGluZyB0aGUgcHJvdmlkZXIgQVBJIGtleSBzZWNyZXQuJyxcclxuICAgIH0pO1xyXG5cclxuICAgIG5ldyBDZm5PdXRwdXQodGhpcywgJ0tub3dsZWRnZUJhc2VCdWNrZXQnLCB7XHJcbiAgICAgIHZhbHVlOiBzdG9yYWdlLmJ1Y2tldC5idWNrZXROYW1lLFxyXG4gICAgICBkZXNjcmlwdGlvbjogJ1MzIGJ1Y2tldCBob2xkaW5nIHNvdXJjZSBkb2N1bWVudHMgKHJhdy8pIGFuZCB0aGUgdmVjdG9yIGluZGV4IChpbmRleC8pJyxcclxuICAgIH0pO1xyXG5cclxuICAgIG5ldyBDZm5PdXRwdXQodGhpcywgJ1F1ZXJ5TG9nVGFibGUnLCB7XHJcbiAgICAgIHZhbHVlOiBzdG9yYWdlLnF1ZXJ5TG9nVGFibGUudGFibGVOYW1lLFxyXG4gICAgICBkZXNjcmlwdGlvbjogJ0R5bmFtb0RCIHRhYmxlIGhvbGRpbmcgb25lIGl0ZW0gcGVyIHF1ZXJ5LCBmb3IgZGVidWdnaW5nIGFuZCBldmFsdWF0aW9uJyxcclxuICAgIH0pO1xyXG5cclxuICAgIC8vIENvbmZpZ3VyYXRpb24gZWNobywgbm90IHZlcmlmaWVkIHN0YXRlLiBOb3RoaW5nIGluIHRoaXMgc3RhY2sgaGFzIGNvbnRhY3RlZCB0aGVcclxuICAgIC8vIHByb3ZpZGVyOiBpdCByZXBvcnRzIHdoYXQgdGhlIHF1ZXJ5IExhbWJkYSB3aWxsIGJlIHRvbGQgdG8gdXNlIG9uY2UgaXQgZXhpc3RzLiBUaGVcclxuICAgIC8vIGF1dGhvcml0YXRpdmUgYW5zd2VyIGF0IHJ1bnRpbWUgY29tZXMgZnJvbSBHRVQgL2hlYWx0aCwgd2hpY2ggcmVwb3J0cyB0aGUgcHJvdmlkZXJcclxuICAgIC8vIGFuZCBtb2RlbCB0aGF0IGFjdHVhbGx5IHNlcnZlZCBhIHJlcXVlc3QuXHJcbiAgICBuZXcgQ2ZuT3V0cHV0KHRoaXMsICdDb25maWd1cmVkTW9kZWxQcm92aWRlcicsIHtcclxuICAgICAgdmFsdWU6IGAke3NldHRpbmdzLm1vZGVscy5wcm92aWRlcn0gfCBnZW5lcmF0aW9uPSR7c2V0dGluZ3MubW9kZWxzLmdlbmVyYXRpb25Nb2RlbH0gfCBlbWJlZGRpbmdzPSR7c2V0dGluZ3MubW9kZWxzLmVtYmVkZGluZ01vZGVsfSAoY29uZmlndXJlZCwgbm90IHlldCB2ZXJpZmllZClgLFxyXG4gICAgICBkZXNjcmlwdGlvbjpcclxuICAgICAgICAnTW9kZWwgcHJvdmlkZXIgdGhpcyBzdGFjayBpcyBjb25maWd1cmVkIGZvciAoQURSLTA5KS4gVGhpcyBpcyBjb25maWd1cmF0aW9uIG9ubHkgLS0gJyArXHJcbiAgICAgICAgJ25vIGNyZWRlbnRpYWwgaGFzIGJlZW4gY2hlY2tlZCBhbmQgbm8gbW9kZWwgaGFzIGJlZW4gY2FsbGVkLiBHRVQgL2hlYWx0aCByZXBvcnRzIHdoYXQgaXMgYWN0dWFsbHkgaW4gdXNlLicsXHJcbiAgICB9KTtcclxuXHJcbiAgICBuZXcgQ2ZuT3V0cHV0KHRoaXMsICdEZXBsb3llZFRvQWNjb3VudCcsIHtcclxuICAgICAgdmFsdWU6IGAke0F3cy5BQ0NPVU5UX0lEfSAvICR7QXdzLlJFR0lPTn1gLFxyXG4gICAgICBkZXNjcmlwdGlvbjogJ0FjY291bnQgYW5kIHJlZ2lvbiB0aGlzIHN0YWNrIGxhbmRlZCBpbiAtLSBjaGVjayBiZWZvcmUgZGVtb2luZycsXHJcbiAgICB9KTtcclxuICB9XHJcblxyXG4gIC8qKlxyXG4gICAqIE9wdGlvbmFsIGFjY291bnQgZ3VhcmQuXHJcbiAgICpcclxuICAgKiBEZWxpYmVyYXRlbHkgb3B0LWluLiBBIGhhcmQtY29kZWQgYWNjb3VudCB3b3VsZCBkZWZlYXQgdGhlIHBvaW50IG9mIGFuXHJcbiAgICogZW52aXJvbm1lbnQtYWdub3N0aWMgc3RhY2s6IHRoZSBzYW1lIGNvbW1pdCBoYXMgdG8gZGVwbG95IHRvIGEgcGVyc29uYWwgZGV2ZWxvcG1lbnRcclxuICAgKiBhY2NvdW50IGFuZCB0byB0aGUgQU1DUk8gc2FuZGJveC4gUGFzc2luZyBgLWMgZXhwZWN0ZWRBY2NvdW50PTxpZD5gIHR1cm5zIG9uIHRoZSBjaGVja1xyXG4gICAqIGZvciBwZW9wbGUgd2hvIGtlZXAgc2V2ZXJhbCBwcm9maWxlcyBhbmQgd291bGQgcmF0aGVyIGZhaWwgdGhhbiBkZXBsb3kgdG8gdGhlIHdyb25nIG9uZS5cclxuICAgKi9cclxuICBwcml2YXRlIGd1YXJkQWNjb3VudChzZXR0aW5nczogU3RhY2tTZXR0aW5ncyk6IHZvaWQge1xyXG4gICAgY29uc3QgeyBleHBlY3RlZEFjY291bnQgfSA9IHNldHRpbmdzO1xyXG4gICAgaWYgKCFleHBlY3RlZEFjY291bnQpIHtcclxuICAgICAgcmV0dXJuO1xyXG4gICAgfVxyXG4gICAgaWYgKFRva2VuLmlzVW5yZXNvbHZlZCh0aGlzLmFjY291bnQpKSB7XHJcbiAgICAgIEFubm90YXRpb25zLm9mKHRoaXMpLmFkZFdhcm5pbmdWMihcclxuICAgICAgICAna2ItYWdlbnQ6YWNjb3VudC1ndWFyZC1za2lwcGVkJyxcclxuICAgICAgICAnZXhwZWN0ZWRBY2NvdW50IHdhcyBzZXQgYnV0IHRoZSBzdGFjayBpcyBlbnZpcm9ubWVudC1hZ25vc3RpYywgc28gdGhlIGFjY291bnQgaXMgJyArXHJcbiAgICAgICAgICAnb25seSBrbm93biBhdCBkZXBsb3kgdGltZS4gVGhlIGd1YXJkIGNhbm5vdCBydW4gZHVyaW5nIHN5bnRoLicsXHJcbiAgICAgICk7XHJcbiAgICAgIHJldHVybjtcclxuICAgIH1cclxuICAgIGlmICh0aGlzLmFjY291bnQgIT09IGV4cGVjdGVkQWNjb3VudCkge1xyXG4gICAgICB0aHJvdyBuZXcgRXJyb3IoXHJcbiAgICAgICAgYEFjY291bnQgZ3VhcmQ6IHRoaXMgc3RhY2sgaXMgY29uZmlndXJlZCBmb3IgYWNjb3VudCAke2V4cGVjdGVkQWNjb3VudH0gYnV0IHRoZSBgICtcclxuICAgICAgICAgIGBhY3RpdmUgY3JlZGVudGlhbHMgcmVzb2x2ZSB0byAke3RoaXMuYWNjb3VudH0uIENoZWNrIHlvdXIgLS1wcm9maWxlLmAsXHJcbiAgICAgICk7XHJcbiAgICB9XHJcbiAgfVxyXG59XHJcbiJdfQ==