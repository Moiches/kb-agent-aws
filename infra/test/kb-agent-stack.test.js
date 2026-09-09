"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
const aws_cdk_lib_1 = require("aws-cdk-lib");
const assertions_1 = require("aws-cdk-lib/assertions");
const config_1 = require("../lib/config");
const kb_agent_stack_1 = require("../lib/kb-agent-stack");
/**
 * These are security and hygiene assertions, not functional ones. They exist so that a
 * regression which quietly makes the stack less safe -- a public bucket, a log group that
 * never expires -- fails the build instead of reaching a deployment.
 *
 * They need no AWS credentials.
 */
function synth(context = {}) {
    const app = new aws_cdk_lib_1.App({ context });
    const settings = (0, config_1.resolveSettings)((key) => app.node.tryGetContext(key));
    const stack = new kb_agent_stack_1.KbAgentStack(app, 'TestStack', { settings });
    return assertions_1.Template.fromStack(stack);
}
describe('storage', () => {
    const template = synth();
    test('the bucket blocks all public access', () => {
        template.hasResourceProperties('AWS::S3::Bucket', {
            PublicAccessBlockConfiguration: {
                BlockPublicAcls: true,
                BlockPublicPolicy: true,
                IgnorePublicAcls: true,
                RestrictPublicBuckets: true,
            },
        });
    });
    test('the bucket is encrypted at rest', () => {
        template.hasResourceProperties('AWS::S3::Bucket', {
            BucketEncryption: {
                ServerSideEncryptionConfiguration: [
                    { ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } },
                ],
            },
        });
    });
    test('the bucket denies plaintext HTTP', () => {
        template.hasResourceProperties('AWS::S3::BucketPolicy', {
            PolicyDocument: {
                Statement: assertions_1.Match.arrayWith([
                    assertions_1.Match.objectLike({
                        Effect: 'Deny',
                        Condition: { Bool: { 'aws:SecureTransport': 'false' } },
                    }),
                ]),
            },
        });
    });
    test('the bucket is versioned, so a bad index can be rolled back', () => {
        template.hasResourceProperties('AWS::S3::Bucket', {
            VersioningConfiguration: { Status: 'Enabled' },
        });
    });
    test('the query log table is on-demand billing and expires its items', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
            BillingMode: 'PAY_PER_REQUEST',
            TimeToLiveSpecification: { AttributeName: 'expires_at', Enabled: true },
        });
    });
    test('sample documents are deployed with the stack', () => {
        // The BucketDeployment custom resource is what pre-seeds the knowledge base.
        template.resourceCountIs('Custom::CDKBucketDeployment', 1);
    });
    test('the deployment does not prune, so it cannot delete the built index', () => {
        template.hasResourceProperties('Custom::CDKBucketDeployment', { Prune: false });
    });
});
describe('portability', () => {
    test('no account id is hard-coded: the bucket name resolves at deploy time', () => {
        const json = JSON.stringify(synth().toJSON());
        expect(json).toContain('AWS::AccountId');
        expect(json).not.toMatch(/\b\d{12}\b/);
    });
    test('the prefix flows into resource names, so two deployments can coexist', () => {
        const template = synth({ prefix: 'kbagent-mc' });
        template.hasResourceProperties('AWS::DynamoDB::Table', {
            TableName: 'kbagent-mc-dev-query-log',
        });
    });
});
describe('environment configuration', () => {
    test('dev tears itself down cleanly', () => {
        const template = synth({ env: 'dev' });
        template.hasResource('AWS::S3::Bucket', { DeletionPolicy: 'Delete' });
        // autoDeleteObjects adds a second custom resource that empties the bucket first.
        template.resourceCountIs('Custom::S3AutoDeleteObjects', 1);
    });
    test('prod retains data', () => {
        const template = synth({ env: 'prod' });
        template.hasResource('AWS::S3::Bucket', { DeletionPolicy: 'Retain' });
        template.resourceCountIs('Custom::S3AutoDeleteObjects', 0);
    });
    test('an unknown environment fails loudly instead of silently defaulting', () => {
        expect(() => synth({ env: 'staging' })).toThrow(/Unknown env/);
    });
    test('a prefix that would produce an invalid bucket name is rejected', () => {
        expect(() => synth({ prefix: 'Not_Valid' })).toThrow(/Invalid prefix/);
    });
});
describe('model provider', () => {
    test('OpenRouter is the default, because the sandbox has no Bedrock access', () => {
        synth().hasOutput('ConfiguredModelProvider', {
            Value: assertions_1.Match.stringLikeRegexp('^openrouter'),
        });
    });
    test('the provider can be switched at deploy time', () => {
        synth({ provider: 'bedrock' }).hasOutput('ConfiguredModelProvider', {
            Value: assertions_1.Match.stringLikeRegexp('^bedrock'),
        });
    });
    test('the brief\'s suggested Claude 3 Haiku is not used: it is Legacy on Bedrock', () => {
        const json = JSON.stringify(synth({ provider: 'bedrock' }).toJSON());
        expect(json).not.toContain('claude-3-haiku');
    });
    test('model ids can be overridden without a code change', () => {
        synth({ generationModel: 'some/other-model' }).hasOutput('ConfiguredModelProvider', {
            Value: assertions_1.Match.stringLikeRegexp('some/other-model'),
        });
    });
});
describe('seeding', () => {
    const template = synth({ prefix: 'kbagent-mc' });
    test('the ingest function has a deterministic name, so `make seed` can be documented', () => {
        template.hasResourceProperties('AWS::Lambda::Function', {
            FunctionName: 'kbagent-mc-dev-ingest',
            Handler: 'handler.lambda_handler',
            Runtime: 'python3.12',
        });
    });
    test('a trigger runs it at deploy time', () => {
        template.resourceCountIs('Custom::Trigger', 1);
    });
    test('the two secrets have deliberately different lifecycles', () => {
        // They are not interchangeable, and the difference is the point.
        //
        // The provider key comes from a third party, so CDK cannot mint it. It is created
        // holding a placeholder the ingest Lambda recognises as "not configured yet" -- because
        // `new Secret()` with no props generates a *random string*, which would be sent to the
        // provider, collect a 401, and fail the deployment instead of skipping cleanly.
        template.hasResourceProperties('AWS::SecretsManager::Secret', {
            Name: 'kbagent-mc-dev/provider-api-key',
            SecretString: 'REPLACE_WITH_PROVIDER_API_KEY',
        });
        // The API bearer token is ours to mint, so CDK generates it inside AWS and it never
        // exists in a file, a shell history or this repository.
        template.hasResourceProperties('AWS::SecretsManager::Secret', {
            Name: 'kbagent-mc-dev/api-token',
            GenerateSecretString: assertions_1.Match.objectLike({ PasswordLength: 48, ExcludePunctuation: true }),
        });
    });
    test('no real credential is ever written into the template', () => {
        const json = JSON.stringify(template.toJSON());
        expect(json).not.toMatch(/sk-or-[A-Za-z0-9]/);
        // The only literal SecretString in the template is the placeholder.
        const literals = json.match(/"SecretString":"[^"]*"/g) ?? [];
        expect(literals).toEqual(['"SecretString":"REPLACE_WITH_PROVIDER_API_KEY"']);
    });
    test('the seeder can read documents but not overwrite them', () => {
        // Scoped to the seeder's own policy. CDK's BucketDeployment has its own, broader role,
        // which is CDK's business and not what this assertion is about.
        const policies = template.findResources('AWS::IAM::Policy');
        const [seederPolicy] = Object.entries(policies)
            .filter(([name]) => name.startsWith('SeederIngestServiceRole'))
            .map(([, resource]) => resource);
        expect(seederPolicy).toBeDefined();
        const statements = seederPolicy.Properties.PolicyDocument.Statement;
        const writes = statements.filter((s) => JSON.stringify(s.Action).includes('s3:PutObject'));
        expect(writes.length).toBe(1);
        expect(JSON.stringify(writes[0].Resource)).toContain('index/*');
        // The seeder must never be able to write back over the source documents.
        expect(JSON.stringify(writes[0].Resource)).not.toContain('raw/*');
    });
    test('no IAM statement grants Bedrock while OpenRouter is the provider', () => {
        const json = JSON.stringify(synth().toJSON());
        expect(json).not.toContain('bedrock:InvokeModel');
    });
    test('the documents function manages source documents but cannot forge the index', () => {
        // It gained PutObject when upload was added, which is the point of the endpoint. The
        // invariant that survived is the one that matters: every write is confined to `raw/`,
        // so this function can offer a document for indexing but cannot write a passage
        // straight into what answers are built from. Only the ingest writes `index/`, and only
        // after reading what is really in the bucket.
        const policies = template.findResources('AWS::IAM::Policy');
        const [documentsPolicy] = Object.entries(policies)
            .filter(([name]) => name.startsWith('ApiDocumentsServiceRole'))
            .map(([, resource]) => resource);
        expect(documentsPolicy).toBeDefined();
        const statements = documentsPolicy.Properties.PolicyDocument.Statement;
        const writes = statements.filter((s) => JSON.stringify(s.Action).match(/s3:(PutObject|DeleteObject)/));
        expect(writes.length).toBeGreaterThan(0);
        for (const statement of writes) {
            const resource = JSON.stringify(statement.Resource);
            expect(resource).toContain('raw/*');
            expect(resource).not.toContain('index/');
        }
    });
    test('a change anywhere under raw/ triggers a rebuild', () => {
        // Without this the index only rebuilds when something calls the ingest, so deleting an
        // object through the console leaves the API answering from a document that is gone.
        template.hasResourceProperties('AWS::S3::Bucket', {
            NotificationConfiguration: {
                QueueConfigurations: assertions_1.Match.arrayWith([
                    assertions_1.Match.objectLike({ Event: 's3:ObjectCreated:*' }),
                    assertions_1.Match.objectLike({ Event: 's3:ObjectRemoved:*' }),
                ]),
            },
        });
    });
    test('reindex events are debounced, and failures are kept rather than retried forever', () => {
        // A deploy uploads the sample documents in a burst. Wired straight to the Lambda that
        // is one full re-embedding per file, concurrent, all writing the same artifact.
        template.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
            BatchSize: 100,
            MaximumBatchingWindowInSeconds: 60,
        });
        template.hasResourceProperties('AWS::SQS::Queue', {
            RedrivePolicy: assertions_1.Match.objectLike({ maxReceiveCount: 3 }),
        });
    });
    test('the query function still cannot touch the corpus', () => {
        // The reason the documents function exists at all. If listing and deleting had been
        // bolted onto the query Lambda, this assertion is what would have broken.
        const policies = template.findResources('AWS::IAM::Policy');
        const [queryPolicy] = Object.entries(policies)
            .filter(([name]) => name.startsWith('ApiQueryServiceRole'))
            .map(([, resource]) => resource);
        expect(queryPolicy).toBeDefined();
        const json = JSON.stringify(queryPolicy.Properties.PolicyDocument.Statement);
        expect(json).not.toContain('s3:PutObject');
        expect(json).not.toContain('s3:DeleteObject');
        expect(json).not.toContain('raw/');
    });
    test('deleting a document is authenticated like everything else', () => {
        template.hasResourceProperties('AWS::ApiGateway::Method', {
            HttpMethod: 'DELETE',
            AuthorizationType: 'CUSTOM',
        });
    });
    test('outputs tell the reviewer exactly what to run next', () => {
        const outputs = template.toJSON().Outputs;
        expect(outputs.ProviderApiKeySecret).toBeDefined();
        // The value is an Fn::Join because the function name is a resource reference, so the
        // assertion goes against the rendered form rather than a plain string.
        expect(JSON.stringify(outputs.SeedCommand.Value)).toContain('aws lambda invoke');
    });
});
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoia2ItYWdlbnQtc3RhY2sudGVzdC5qcyIsInNvdXJjZVJvb3QiOiIiLCJzb3VyY2VzIjpbImtiLWFnZW50LXN0YWNrLnRlc3QudHMiXSwibmFtZXMiOltdLCJtYXBwaW5ncyI6Ijs7QUFBQSw2Q0FBa0M7QUFDbEMsdURBQXlEO0FBQ3pELDBDQUFnRDtBQUNoRCwwREFBcUQ7QUFFckQ7Ozs7OztHQU1HO0FBRUgsU0FBUyxLQUFLLENBQUMsVUFBbUMsRUFBRTtJQUNsRCxNQUFNLEdBQUcsR0FBRyxJQUFJLGlCQUFHLENBQUMsRUFBRSxPQUFPLEVBQUUsQ0FBQyxDQUFDO0lBQ2pDLE1BQU0sUUFBUSxHQUFHLElBQUEsd0JBQWUsRUFBQyxDQUFDLEdBQUcsRUFBRSxFQUFFLENBQUMsR0FBRyxDQUFDLElBQUksQ0FBQyxhQUFhLENBQUMsR0FBRyxDQUFDLENBQUMsQ0FBQztJQUN2RSxNQUFNLEtBQUssR0FBRyxJQUFJLDZCQUFZLENBQUMsR0FBRyxFQUFFLFdBQVcsRUFBRSxFQUFFLFFBQVEsRUFBRSxDQUFDLENBQUM7SUFDL0QsT0FBTyxxQkFBUSxDQUFDLFNBQVMsQ0FBQyxLQUFLLENBQUMsQ0FBQztBQUNuQyxDQUFDO0FBRUQsUUFBUSxDQUFDLFNBQVMsRUFBRSxHQUFHLEVBQUU7SUFDdkIsTUFBTSxRQUFRLEdBQUcsS0FBSyxFQUFFLENBQUM7SUFFekIsSUFBSSxDQUFDLHFDQUFxQyxFQUFFLEdBQUcsRUFBRTtRQUMvQyxRQUFRLENBQUMscUJBQXFCLENBQUMsaUJBQWlCLEVBQUU7WUFDaEQsOEJBQThCLEVBQUU7Z0JBQzlCLGVBQWUsRUFBRSxJQUFJO2dCQUNyQixpQkFBaUIsRUFBRSxJQUFJO2dCQUN2QixnQkFBZ0IsRUFBRSxJQUFJO2dCQUN0QixxQkFBcUIsRUFBRSxJQUFJO2FBQzVCO1NBQ0YsQ0FBQyxDQUFDO0lBQ0wsQ0FBQyxDQUFDLENBQUM7SUFFSCxJQUFJLENBQUMsaUNBQWlDLEVBQUUsR0FBRyxFQUFFO1FBQzNDLFFBQVEsQ0FBQyxxQkFBcUIsQ0FB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QUFvRCxFQUFFLEdBQUcsRUFBRTtRQUM5RCxNQUFNLE9BQU8sR0FBSSxRQUFRLENBQUMsTUFBTSxFQUFVLENBQUMsT0FBTyxDQUFDO1FBQ25ELE1BQU0sQ0FBQyxPQUFPLENBQUMsb0JBQW9CLENBQUMsQ0FBQyxXQUFXLEVBQUUsQ0FBQztRQUNuRCxxRkFBcUY7UUFDckYsdUVBQXVFO1FBQ3ZFLE1BQU0sQ0FBQyxJQUFJLENBQUMsU0FBUyxDQUFDLE9BQU8sQ0FBQyxXQUFXLENBQUMsS0FBSyxDQUFDLENBQUMsQ0FBQyxTQUFTLENBQUMsbUJBQW1CLENBQUMsQ0FBQztJQUNuRixDQUFDLENBQUMsQ0FBQztBQUNMLENBQUMsQ0FBQyxDQUFDIiwic291cmNlc0NvbnRlbnQiOlsiaW1wb3J0IHsgQXBwIH0gZnJvbSAnYXdzLWNkay1saWInO1xyXG5pbXBvcnQgeyBNYXRjaCwgVGVtcGxhdGUgfSBmcm9tICdhd3MtY2RrLWxpYi9hc3NlcnRpb25zJztcclxuaW1wb3J0IHsgcmVzb2x2ZVNldHRpbmdzIH0gZnJvbSAnLi4vbGliL2NvbmZpZyc7XHJcbmltcG9ydCB7IEtiQWdlbnRTdGFjayB9IGZyb20gJy4uL2xpYi9rYi1hZ2VudC1zdGFjayc7XHJcblxyXG4vKipcclxuICogVGhlc2UgYXJlIHNlY3VyaXR5IGFuZCBoeWdpZW5lIGFzc2VydGlvbnMsIG5vdCBmdW5jdGlvbmFsIG9uZXMuIFRoZXkgZXhpc3Qgc28gdGhhdCBhXHJcbiAqIHJlZ3Jlc3Npb24gd2hpY2ggcXVpZXRseSBtYWtlcyB0aGUgc3RhY2sgbGVzcyBzYWZlIC0tIGEgcHVibGljIGJ1Y2tldCwgYSBsb2cgZ3JvdXAgdGhhdFxyXG4gKiBuZXZlciBleHBpcmVzIC0tIGZhaWxzIHRoZSBidWlsZCBpbnN0ZWFkIG9mIHJlYWNoaW5nIGEgZGVwbG95bWVudC5cclxuICpcclxuICogVGhleSBuZWVkIG5vIEFXUyBjcmVkZW50aWFscy5cclxuICovXHJcblxyXG5mdW5jdGlvbiBzeW50aChjb250ZXh0OiBSZWNvcmQ8c3RyaW5nLCB1bmtub3duPiA9IHt9KTogVGVtcGxhdGUge1xyXG4gIGNvbnN0IGFwcCA9IG5ldyBBcHAoeyBjb250ZXh0IH0pO1xyXG4gIGNvbnN0IHNldHRpbmdzID0gcmVzb2x2ZVNldHRpbmdzKChrZXkpID0+IGFwcC5ub2RlLnRyeUdldENvbnRleHQoa2V5KSk7XHJcbiAgY29uc3Qgc3RhY2sgPSBuZXcgS2JBZ2VudFN0YWNrKGFwcCwgJ1Rlc3RTdGFjaycsIHsgc2V0dGluZ3MgfSk7XHJcbiAgcmV0dXJuIFRlbXBsYXRlLmZyb21TdGFjayhzdGFjayk7XHJcbn1cclxuXHJcbmRlc2NyaWJlKCdzdG9yYWdlJywgKCkgPT4ge1xyXG4gIGNvbnN0IHRlbXBsYXRlID0gc3ludGgoKTtcclxuXHJcbiAgdGVzdCgndGhlIGJ1Y2tldCBibG9ja3MgYWxsIHB1YmxpYyBhY2Nlc3MnLCAoKSA9PiB7XHJcbiAgICB0ZW1wbGF0ZS5oYXNSZXNvdXJjZVByb3BlcnRpZXMoJ0FXUzo6UzM6OkJ1Y2tldCcsIHtcclxuICAgICAgUHVibGljQWNjZXNzQmxvY2tDb25maWd1cmF0aW9uOiB7XHJcbiAgICAgICAgQmxvY2tQdWJsaWNBY2xzOiB0cnVlLFxyXG4gICAgICAgIEJsb2NrUHVibGljUG9saWN5OiB0cnVlLFxyXG4gICAgICAgIElnbm9yZVB1YmxpY0FjbHM6IHRydWUsXHJcbiAgICAgICAgUmVzdHJpY3RQdWJsaWNCdWNrZXRzOiB0cnVlLFxyXG4gICAgICB9LFxyXG4gICAgfSk7XHJcbiAgfSk7XHJcblxyXG4gIHRlc3QoJ3RoZSBidWNrZXQgaXMgZW5jcnlwdGVkIGF0IHJlc3QnLCAoKSA9PiB7XHJcbiAgICB0ZW1wbGF0ZS5oYXNSZXNvdXJjZVByb3BlcnRpZXMoJ0FXUzo6UzM6OkJ1Y2tldCcsIHtcclxuICAgICAgQnVja2V0RW5jcnlwdGlvbjoge1xyXG4gICAgICAgIFNlcnZlclNpZGVFbmNyeXB0aW9uQ29uZmlndXJhdGlvbjogW1xyXG4gICAgICAgICAgeyBTZXJ2ZXJTaWRlRW5jcnlwdGlvbkJ5RGVmYXVsdDogeyBTU0VBbGdvcml0aG06ICdBRVMyNTYnIH0gfSxcclxuICAgICAgICBdLFxyXG4gICAgICB9LFxyXG4gICAgfSk7XHJcbiAgfSk7XHJcblxyXG4gIHRlc3QoJ3RoZSBidWNrZXQgZGVuaWVzIHBsYWludGV4dCBIVFRQJywgKCkgPT4ge1xyXG4gICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OlMzOjpCdWNrZXRQb2xpY3knLCB7XHJcbiAgICAgIFBvbGljeURvY3VtZW50OiB7XHJcbiAgICAgICAgU3RhdGVtZW50OiBNYXRjaC5hcnJheVdpdGgoW1xyXG4gICAgICAgICAgTWF0Y2gub2JqZWN0TGlrZSh7XHJcbiAgICAgICAgICAgIEVmZmVjdDogJ0RlbnknLFxyXG4gICAgICAgICAgICBDb25kaXRpb246IHsgQm9vbDogeyAnYXdzOlNlY3VyZVRyYW5zcG9ydCc6ICdmYWxzZScgfSB9LFxyXG4gICAgICAgICAgfSksXHJcbiAgICAgICAgXSksXHJcbiAgICAgIH0sXHJcbiAgICB9KTtcclxuICB9KTtcclxuXHJcbiAgdGVzdCgndGhlIGJ1Y2tldCBpcyB2ZXJzaW9uZWQsIHNvIGEgYmFkIGluZGV4IGNhbiBiZSByb2xsZWQgYmFjaycsICgpID0+IHtcclxuICAgIHRlbXBsYXRlLmhhc1Jlc291cmNlUHJvcGVydGllcygnQVdTOjpTMzo6QnVja2V0Jywge1xyXG4gICAgICBWZXJzaW9uaW5nQ29uZmlndXJhdGlvbjogeyBTdGF0dXM6ICdFbmFibGVkJyB9LFxyXG4gICAgfSk7XHJcbiAgfSk7XHJcblxyXG4gIHRlc3QoJ3RoZSBxdWVyeSBsb2cgdGFibGUgaXMgb24tZGVtYW5kIGJpbGxpbmcgYW5kIGV4cGlyZXMgaXRzIGl0ZW1zJywgKCkgPT4ge1xyXG4gICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OkR5bmFtb0RCOjpUYWJsZScsIHtcclxuICAgICAgQmlsbGluZ01vZGU6ICdQQVlfUEVSX1JFUVVFU1QnLFxyXG4gICAgICBUaW1lVG9MaXZlU3BlY2lmaWNhdGlvbjogeyBBdHRyaWJ1dGVOYW1lOiAnZXhwaXJlc19hdCcsIEVuYWJsZWQ6IHRydWUgfSxcclxuICAgIH0pO1xyXG4gIH0pO1xyXG5cclxuICB0ZXN0KCdzYW1wbGUgZG9jdW1lbnRzIGFyZSBkZXBsb3llZCB3aXRoIHRoZSBzdGFjaycsICgpID0+IHtcclxuICAgIC8vIFRoZSBCdWNrZXREZXBsb3ltZW50IGN1c3RvbSByZXNvdXJjZSBpcyB3aGF0IHByZS1zZWVkcyB0aGUga25vd2xlZGdlIGJhc2UuXHJcbiAgICB0ZW1wbGF0ZS5yZXNvdXJjZUNvdW50SXMoJ0N1c3RvbTo6Q0RLQnVja2V0RGVwbG95bWVudCcsIDEpO1xyXG4gIH0pO1xyXG5cclxuICB0ZXN0KCd0aGUgZGVwbG95bWVudCBkb2VzIG5vdCBwcnVuZSwgc28gaXQgY2Fubm90IGRlbGV0ZSB0aGUgYnVpbHQgaW5kZXgnLCAoKSA9PiB7XHJcbiAgICB0ZW1wbGF0ZS5oYXNSZXNvdXJjZVByb3BlcnRpZXMoJ0N1c3RvbTo6Q0RLQnVja2V0RGVwbG95bWVudCcsIHsgUHJ1bmU6IGZhbHNlIH0pO1xyXG4gIH0pO1xyXG59KTtcclxuXHJcbmRlc2NyaWJlKCdwb3J0YWJpbGl0eScsICgpID0+IHtcclxuICB0ZXN0KCdubyBhY2NvdW50IGlkIGlzIGhhcmQtY29kZWQ6IHRoZSBidWNrZXQgbmFtZSByZXNvbHZlcyBhdCBkZXBsb3kgdGltZScsICgpID0+IHtcclxuICAgIGNvbnN0IGpzb24gPSBKU09OLnN0cmluZ2lmeShzeW50aCgpLnRvSlNPTigpKTtcclxuICAgIGV4cGVjdChqc29uKS50b0NvbnRhaW4oJ0FXUzo6QWNjb3VudElkJyk7XHJcbiAgICBleHBlY3QoanNvbikubm90LnRvTWF0Y2goL1xcYlxcZHsxMn1cXGIvKTtcclxuICB9KTtcclxuXHJcbiAgdGVzdCgndGhlIHByZWZpeCBmbG93cyBpbnRvIHJlc291cmNlIG5hbWVzLCBzbyB0d28gZGVwbG95bWVudHMgY2FuIGNvZXhpc3QnLCAoKSA9PiB7XHJcbiAgICBjb25zdCB0ZW1wbGF0ZSA9IHN5bnRoKHsgcHJlZml4OiAna2JhZ2VudC1tYycgfSk7XHJcbiAgICB0ZW1wbGF0ZS5oYXNSZXNvdXJjZVByb3BlcnRpZXMoJ0FXUzo6RHluYW1vREI6OlRhYmxlJywge1xyXG4gICAgICBUYWJsZU5hbWU6ICdrYmFnZW50LW1jLWRldi1xdWVyeS1sb2cnLFxyXG4gICAgfSk7XHJcbiAgfSk7XHJcbn0pO1xyXG5cclxuZGVzY3JpYmUoJ2Vudmlyb25tZW50IGNvbmZpZ3VyYXRpb24nLCAoKSA9PiB7XHJcbiAgdGVzdCgnZGV2IHRlYXJzIGl0c2VsZiBkb3duIGNsZWFubHknLCAoKSA9PiB7XHJcbiAgICBjb25zdCB0ZW1wbGF0ZSA9IHN5bnRoKHsgZW52OiAnZGV2JyB9KTtcclxuICAgIHRlbXBsYXRlLmhhc1Jlc291cmNlKCdBV1M6OlMzOjpCdWNrZXQnLCB7IERlbGV0aW9uUG9saWN5OiAnRGVsZXRlJyB9KTtcclxuICAgIC8vIGF1dG9EZWxldGVPYmplY3RzIGFkZHMgYSBzZWNvbmQgY3VzdG9tIHJlc291cmNlIHRoYXQgZW1wdGllcyB0aGUgYnVja2V0IGZpcnN0LlxyXG4gICAgdGVtcGxhdGUucmVzb3VyY2VDb3VudElzKCdDdXN0b206OlMzQXV0b0RlbGV0ZU9iamVjdHMnLCAxKTtcclxuICB9KTtcclxuXHJcbiAgdGVzdCgncHJvZCByZXRhaW5zIGRhdGEnLCAoKSA9PiB7XHJcbiAgICBjb25zdCB0ZW1wbGF0ZSA9IHN5bnRoKHsgZW52OiAncHJvZCcgfSk7XHJcbiAgICB0ZW1wbGF0ZS5oYXNSZXNvdXJjZSgnQVdTOjpTMzo6QnVja2V0JywgeyBEZWxldGlvblBvbGljeTogJ1JldGFpbicgfSk7XHJcbiAgICB0ZW1wbGF0ZS5yZXNvdXJjZUNvdW50SXMoJ0N1c3RvbTo6UzNBdXRvRGVsZXRlT2JqZWN0cycsIDApO1xyXG4gIH0pO1xyXG5cclxuICB0ZXN0KCdhbiB1bmtub3duIGVudmlyb25tZW50IGZhaWxzIGxvdWRseSBpbnN0ZWFkIG9mIHNpbGVudGx5IGRlZmF1bHRpbmcnLCAoKSA9PiB7XHJcbiAgICBleHBlY3QoKCkgPT4gc3ludGgoeyBlbnY6ICdzdGFnaW5nJyB9KSkudG9UaHJvdygvVW5rbm93biBlbnYvKTtcclxuICB9KTtcclxuXHJcbiAgdGVzdCgnYSBwcmVmaXggdGhhdCB3b3VsZCBwcm9kdWNlIGFuIGludmFsaWQgYnVja2V0IG5hbWUgaXMgcmVqZWN0ZWQnLCAoKSA9PiB7XHJcbiAgICBleHBlY3QoKCkgPT4gc3ludGgoeyBwcmVmaXg6ICdOb3RfVmFsaWQnIH0pKS50b1Rocm93KC9JbnZhbGlkIHByZWZpeC8pO1xyXG4gIH0pO1xyXG59KTtcclxuXHJcbmRlc2NyaWJlKCdtb2RlbCBwcm92aWRlcicsICgpID0+IHtcclxuICB0ZXN0KCdPcGVuUm91dGVyIGlzIHRoZSBkZWZhdWx0LCBiZWNhdXNlIHRoZSBzYW5kYm94IGhhcyBubyBCZWRyb2NrIGFjY2VzcycsICgpID0+IHtcclxuICAgIHN5bnRoKCkuaGFzT3V0cHV0KCdDb25maWd1cmVkTW9kZWxQcm92aWRlcicsIHtcclxuICAgICAgVmFsdWU6IE1hdGNoLnN0cmluZ0xpa2VSZWdleHAoJ15vcGVucm91dGVyJyksXHJcbiAgICB9KTtcclxuICB9KTtcclxuXHJcbiAgdGVzdCgndGhlIHByb3ZpZGVyIGNhbiBiZSBzd2l0Y2hlZCBhdCBkZXBsb3kgdGltZScsICgpID0+IHtcclxuICAgIHN5bnRoKHsgcHJvdmlkZXI6ICdiZWRyb2NrJyB9KS5oYXNPdXRwdXQoJ0NvbmZpZ3VyZWRNb2RlbFByb3ZpZGVyJywge1xyXG4gICAgICBWYWx1ZTogTWF0Y2guc3RyaW5nTGlrZVJlZ2V4cCgnXmJlZHJvY2snKSxcclxuICAgIH0pO1xyXG4gIH0pO1xyXG5cclxuICB0ZXN0KCd0aGUgYnJpZWZcXCdzIHN1Z2dlc3RlZCBDbGF1ZGUgMyBIYWlrdSBpcyBub3QgdXNlZDogaXQgaXMgTGVnYWN5IG9uIEJlZHJvY2snLCAoKSA9PiB7XHJcbiAgICBjb25zdCBqc29uID0gSlNPTi5zdHJpbmdpZnkoc3ludGgoeyBwcm92aWRlcjogJ2JlZHJvY2snIH0pLnRvSlNPTigpKTtcclxuICAgIGV4cGVjdChqc29uKS5ub3QudG9Db250YWluKCdjbGF1ZGUtMy1oYWlrdScpO1xyXG4gIH0pO1xyXG5cclxuICB0ZXN0KCdtb2RlbCBpZHMgY2FuIGJlIG92ZXJyaWRkZW4gd2l0aG91dCBhIGNvZGUgY2hhbmdlJywgKCkgPT4ge1xyXG4gICAgc3ludGgoeyBnZW5lcmF0aW9uTW9kZWw6ICdzb21lL290aGVyLW1vZGVsJyB9KS5oYXNPdXRwdXQoJ0NvbmZpZ3VyZWRNb2RlbFByb3ZpZGVyJywge1xyXG4gICAgICBWYWx1ZTogTWF0Y2guc3RyaW5nTGlrZVJlZ2V4cCgnc29tZS9vdGhlci1tb2RlbCcpLFxyXG4gICAgfSk7XHJcbiAgfSk7XHJcbn0pO1xyXG5cclxuZGVzY3JpYmUoJ3NlZWRpbmcnLCAoKSA9PiB7XHJcbiAgY29uc3QgdGVtcGxhdGUgPSBzeW50aCh7IHByZWZpeDogJ2tiYWdlbnQtbWMnIH0pO1xyXG5cclxuICB0ZXN0KCd0aGUgaW5nZXN0IGZ1bmN0aW9uIGhhcyBhIGRldGVybWluaXN0aWMgbmFtZSwgc28gYG1ha2Ugc2VlZGAgY2FuIGJlIGRvY3VtZW50ZWQnLCAoKSA9PiB7XHJcbiAgICB0ZW1wbGF0ZS5oYXNSZXNvdXJjZVByb3BlcnRpZXMoJ0FXUzo6TGFtYmRhOjpGdW5jdGlvbicsIHtcclxuICAgICAgRnVuY3Rpb25OYW1lOiAna2JhZ2VudC1tYy1kZXYtaW5nZXN0JyxcclxuICAgICAgSGFuZGxlcjogJ2hhbmRsZXIubGFtYmRhX2hhbmRsZXInLFxyXG4gICAgICBSdW50aW1lOiAncHl0aG9uMy4xMicsXHJcbiAgICB9KTtcclxuICB9KTtcclxuXHJcbiAgdGVzdCgnYSB0cmlnZ2VyIHJ1bnMgaXQgYXQgZGVwbG95IHRpbWUnLCAoKSA9PiB7XHJcbiAgICB0ZW1wbGF0ZS5yZXNvdXJjZUNvdW50SXMoJ0N1c3RvbTo6VHJpZ2dlcicsIDEpO1xyXG4gIH0pO1xyXG5cclxuICB0ZXN0KCd0aGUgdHdvIHNlY3JldHMgaGF2ZSBkZWxpYmVyYXRlbHkgZGlmZmVyZW50IGxpZmVjeWNsZXMnLCAoKSA9PiB7XHJcbiAgICAvLyBUaGV5IGFyZSBub3QgaW50ZXJjaGFuZ2VhYmxlLCBhbmQgdGhlIGRpZmZlcmVuY2UgaXMgdGhlIHBvaW50LlxyXG4gICAgLy9cclxuICAgIC8vIFRoZSBwcm92aWRlciBrZXkgY29tZXMgZnJvbSBhIHRoaXJkIHBhcnR5LCBzbyBDREsgY2Fubm90IG1pbnQgaXQuIEl0IGlzIGNyZWF0ZWRcclxuICAgIC8vIGhvbGRpbmcgYSBwbGFjZWhvbGRlciB0aGUgaW5nZXN0IExhbWJkYSByZWNvZ25pc2VzIGFzIFwibm90IGNvbmZpZ3VyZWQgeWV0XCIgLS0gYmVjYXVzZVxyXG4gICAgLy8gYG5ldyBTZWNyZXQoKWAgd2l0aCBubyBwcm9wcyBnZW5lcmF0ZXMgYSAqcmFuZG9tIHN0cmluZyosIHdoaWNoIHdvdWxkIGJlIHNlbnQgdG8gdGhlXHJcbiAgICAvLyBwcm92aWRlciwgY29sbGVjdCBhIDQwMSwgYW5kIGZhaWwgdGhlIGRlcGxveW1lbnQgaW5zdGVhZCBvZiBza2lwcGluZyBjbGVhbmx5LlxyXG4gICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OlNlY3JldHNNYW5hZ2VyOjpTZWNyZXQnLCB7XHJcbiAgICAgIE5hbWU6ICdrYmFnZW50LW1jLWRldi9wcm92aWRlci1hcGkta2V5JyxcclxuICAgICAgU2VjcmV0U3RyaW5nOiAnUkVQTEFDRV9XSVRIX1BST1ZJREVSX0FQSV9LRVknLFxyXG4gICAgfSk7XHJcblxyXG4gICAgLy8gVGhlIEFQSSBiZWFyZXIgdG9rZW4gaXMgb3VycyB0byBtaW50LCBzbyBDREsgZ2VuZXJhdGVzIGl0IGluc2lkZSBBV1MgYW5kIGl0IG5ldmVyXHJcbiAgICAvLyBleGlzdHMgaW4gYSBmaWxlLCBhIHNoZWxsIGhpc3Rvcnkgb3IgdGhpcyByZXBvc2l0b3J5LlxyXG4gICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OlNlY3JldHNNYW5hZ2VyOjpTZWNyZXQnLCB7XHJcbiAgICAgIE5hbWU6ICdrYmFnZW50LW1jLWRldi9hcGktdG9rZW4nLFxyXG4gICAgICBHZW5lcmF0ZVNlY3JldFN0cmluZzogTWF0Y2gub2JqZWN0TGlrZSh7IFBhc3N3b3JkTGVuZ3RoOiA0OCwgRXhjbHVkZVB1bmN0dWF0aW9uOiB0cnVlIH0pLFxyXG4gICAgfSk7XHJcbiAgfSk7XHJcblxyXG4gIHRlc3QoJ25vIHJlYWwgY3JlZGVudGlhbCBpcyBldmVyIHdyaXR0ZW4gaW50byB0aGUgdGVtcGxhdGUnLCAoKSA9PiB7XHJcbiAgICBjb25zdCBqc29uID0gSlNPTi5zdHJpbmdpZnkodGVtcGxhdGUudG9KU09OKCkpO1xyXG4gICAgZXhwZWN0KGpzb24pLm5vdC50b01hdGNoKC9zay1vci1bQS1aYS16MC05XS8pO1xyXG4gICAgLy8gVGhlIG9ubHkgbGl0ZXJhbCBTZWNyZXRTdHJpbmcgaW4gdGhlIHRlbXBsYXRlIGlzIHRoZSBwbGFjZWhvbGRlci5cclxuICAgIGNvbnN0IGxpdGVyYWxzID0ganNvbi5tYXRjaCgvXCJTZWNyZXRTdHJpbmdcIjpcIlteXCJdKlwiL2cpID8/IFtdO1xyXG4gICAgZXhwZWN0KGxpdGVyYWxzKS50b0VxdWFsKFsnXCJTZWNyZXRTdHJpbmdcIjpcIlJFUExBQ0VfV0lUSF9QUk9WSURFUl9BUElfS0VZXCInXSk7XHJcbiAgfSk7XHJcblxyXG4gIHRlc3QoJ3RoZSBzZWVkZXIgY2FuIHJlYWQgZG9jdW1lbnRzIGJ1dCBub3Qgb3ZlcndyaXRlIHRoZW0nLCAoKSA9PiB7XHJcbiAgICAvLyBTY29wZWQgdG8gdGhlIHNlZWRlcidzIG93biBwb2xpY3kuIENESydzIEJ1Y2tldERlcGxveW1lbnQgaGFzIGl0cyBvd24sIGJyb2FkZXIgcm9sZSxcclxuICAgIC8vIHdoaWNoIGlzIENESydzIGJ1c2luZXNzIGFuZCBub3Qgd2hhdCB0aGlzIGFzc2VydGlvbiBpcyBhYm91dC5cclxuICAgIGNvbnN0IHBvbGljaWVzID0gdGVtcGxhdGUuZmluZFJlc291cmNlcygnQVdTOjpJQU06OlBvbGljeScpO1xyXG4gICAgY29uc3QgW3NlZWRlclBvbGljeV0gPSBPYmplY3QuZW50cmllcyhwb2xpY2llcylcclxuICAgICAgLmZpbHRlcigoW25hbWVdKSA9PiBuYW1lLnN0YXJ0c1dpdGgoJ1NlZWRlckluZ2VzdFNlcnZpY2VSb2xlJykpXHJcbiAgICAgIC5tYXAoKFssIHJlc291cmNlXSkgPT4gcmVzb3VyY2UgYXMgYW55KTtcclxuICAgIGV4cGVjdChzZWVkZXJQb2xpY3kpLnRvQmVEZWZpbmVkKCk7XHJcblxyXG4gICAgY29uc3Qgc3RhdGVtZW50cyA9IHNlZWRlclBvbGljeS5Qcm9wZXJ0aWVzLlBvbGljeURvY3VtZW50LlN0YXRlbWVudCBhcyBhbnlbXTtcclxuICAgIGNvbnN0IHdyaXRlcyA9IHN0YXRlbWVudHMuZmlsdGVyKChzKSA9PiBKU09OLnN0cmluZ2lmeShzLkFjdGlvbikuaW5jbHVkZXMoJ3MzOlB1dE9iamVjdCcpKTtcclxuXHJcbiAgICBleHBlY3Qod3JpdGVzLmxlbmd0aCkudG9CZSgxKTtcclxuICAgIGV4cGVjdChKU09OLnN0cmluZ2lmeSh3cml0ZXNbMF0uUmVzb3VyY2UpKS50b0NvbnRhaW4oJ2luZGV4LyonKTtcclxuICAgIC8vIFRoZSBzZWVkZXIgbXVzdCBuZXZlciBiZSBhYmxlIHRvIHdyaXRlIGJhY2sgb3ZlciB0aGUgc291cmNlIGRvY3VtZW50cy5cclxuICAgIGV4cGVjdChKU09OLnN0cmluZ2lmeSh3cml0ZXNbMF0uUmVzb3VyY2UpKS5ub3QudG9Db250YWluKCdyYXcvKicpO1xyXG4gIH0pO1xyXG5cclxuICB0ZXN0KCdubyBJQU0gc3RhdGVtZW50IGdyYW50cyBCZWRyb2NrIHdoaWxlIE9wZW5Sb3V0ZXIgaXMgdGhlIHByb3ZpZGVyJywgKCkgPT4ge1xyXG4gICAgY29uc3QganNvbiA9IEpTT04uc3RyaW5naWZ5KHN5bnRoKCkudG9KU09OKCkpO1xyXG4gICAgZXhwZWN0KGpzb24pLm5vdC50b0NvbnRhaW4oJ2JlZHJvY2s6SW52b2tlTW9kZWwnKTtcclxuICB9KTtcclxuXHJcbiAgdGVzdCgndGhlIGRvY3VtZW50cyBmdW5jdGlvbiBtYW5hZ2VzIHNvdXJjZSBkb2N1bWVudHMgYnV0IGNhbm5vdCBmb3JnZSB0aGUgaW5kZXgnLCAoKSA9PiB7XHJcbiAgICAvLyBJdCBnYWluZWQgUHV0T2JqZWN0IHdoZW4gdXBsb2FkIHdhcyBhZGRlZCwgd2hpY2ggaXMgdGhlIHBvaW50IG9mIHRoZSBlbmRwb2ludC4gVGhlXHJcbiAgICAvLyBpbnZhcmlhbnQgdGhhdCBzdXJ2aXZlZCBpcyB0aGUgb25lIHRoYXQgbWF0dGVyczogZXZlcnkgd3JpdGUgaXMgY29uZmluZWQgdG8gYHJhdy9gLFxyXG4gICAgLy8gc28gdGhpcyBmdW5jdGlvbiBjYW4gb2ZmZXIgYSBkb2N1bWVudCBmb3IgaW5kZXhpbmcgYnV0IGNhbm5vdCB3cml0ZSBhIHBhc3NhZ2VcclxuICAgIC8vIHN0cmFpZ2h0IGludG8gd2hhdCBhbnN3ZXJzIGFyZSBidWlsdCBmcm9tLiBPbmx5IHRoZSBpbmdlc3Qgd3JpdGVzIGBpbmRleC9gLCBhbmQgb25seVxyXG4gICAgLy8gYWZ0ZXIgcmVhZGluZyB3aGF0IGlzIHJlYWxseSBpbiB0aGUgYnVja2V0LlxyXG4gICAgY29uc3QgcG9saWNpZXMgPSB0ZW1wbGF0ZS5maW5kUmVzb3VyY2VzKCdBV1M6OklBTTo6UG9saWN5Jyk7XHJcbiAgICBjb25zdCBbZG9jdW1lbnRzUG9saWN5XSA9IE9iamVjdC5lbnRyaWVzKHBvbGljaWVzKVxyXG4gICAgICAuZmlsdGVyKChbbmFtZV0pID0+IG5hbWUuc3RhcnRzV2l0aCgnQXBpRG9jdW1lbnRzU2VydmljZVJvbGUnKSlcclxuICAgICAgLm1hcCgoWywgcmVzb3VyY2VdKSA9PiByZXNvdXJjZSBhcyBhbnkpO1xyXG4gICAgZXhwZWN0KGRvY3VtZW50c1BvbGljeSkudG9CZURlZmluZWQoKTtcclxuXHJcbiAgICBjb25zdCBzdGF0ZW1lbnRzID0gZG9jdW1lbnRzUG9saWN5LlByb3BlcnRpZXMuUG9saWN5RG9jdW1lbnQuU3RhdGVtZW50IGFzIGFueVtdO1xyXG4gICAgY29uc3Qgd3JpdGVzID0gc3RhdGVtZW50cy5maWx0ZXIoKHMpID0+XHJcbiAgICAgIEpTT04uc3RyaW5naWZ5KHMuQWN0aW9uKS5tYXRjaCgvczM6KFB1dE9iamVjdHxEZWxldGVPYmplY3QpLyksXHJcbiAgICApO1xyXG4gICAgZXhwZWN0KHdyaXRlcy5sZW5ndGgpLnRvQmVHcmVhdGVyVGhhbigwKTtcclxuXHJcbiAgICBmb3IgKGNvbnN0IHN0YXRlbWVudCBvZiB3cml0ZXMpIHtcclxuICAgICAgY29uc3QgcmVzb3VyY2UgPSBKU09OLnN0cmluZ2lmeShzdGF0ZW1lbnQuUmVzb3VyY2UpO1xyXG4gICAgICBleHBlY3QocmVzb3VyY2UpLnRvQ29udGFpbigncmF3LyonKTtcclxuICAgICAgZXhwZWN0KHJlc291cmNlKS5ub3QudG9Db250YWluKCdpbmRleC8nKTtcclxuICAgIH1cclxuICB9KTtcclxuXHJcbiAgdGVzdCgnYSBjaGFuZ2UgYW55d2hlcmUgdW5kZXIgcmF3LyB0cmlnZ2VycyBhIHJlYnVpbGQnLCAoKSA9PiB7XHJcbiAgICAvLyBXaXRob3V0IHRoaXMgdGhlIGluZGV4IG9ubHkgcmVidWlsZHMgd2hlbiBzb21ldGhpbmcgY2FsbHMgdGhlIGluZ2VzdCwgc28gZGVsZXRpbmcgYW5cclxuICAgIC8vIG9iamVjdCB0aHJvdWdoIHRoZSBjb25zb2xlIGxlYXZlcyB0aGUgQVBJIGFuc3dlcmluZyBmcm9tIGEgZG9jdW1lbnQgdGhhdCBpcyBnb25lLlxyXG4gICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OlMzOjpCdWNrZXQnLCB7XHJcbiAgICAgIE5vdGlmaWNhdGlvbkNvbmZpZ3VyYXRpb246IHtcclxuICAgICAgICBRdWV1ZUNvbmZpZ3VyYXRpb25zOiBNYXRjaC5hcnJheVdpdGgoW1xyXG4gICAgICAgICAgTWF0Y2gub2JqZWN0TGlrZSh7IEV2ZW50OiAnczM6T2JqZWN0Q3JlYXRlZDoqJyB9KSxcclxuICAgICAgICAgIE1hdGNoLm9iamVjdExpa2UoeyBFdmVudDogJ3MzOk9iamVjdFJlbW92ZWQ6KicgfSksXHJcbiAgICAgICAgXSksXHJcbiAgICAgIH0sXHJcbiAgICB9KTtcclxuICB9KTtcclxuXHJcbiAgdGVzdCgncmVpbmRleCBldmVudHMgYXJlIGRlYm91bmNlZCwgYW5kIGZhaWx1cmVzIGFyZSBrZXB0IHJhdGhlciB0aGFuIHJldHJpZWQgZm9yZXZlcicsICgpID0+IHtcclxuICAgIC8vIEEgZGVwbG95IHVwbG9hZHMgdGhlIHNhbXBsZSBkb2N1bWVudHMgaW4gYSBidXJzdC4gV2lyZWQgc3RyYWlnaHQgdG8gdGhlIExhbWJkYSB0aGF0XHJcbiAgICAvLyBpcyBvbmUgZnVsbCByZS1lbWJlZGRpbmcgcGVyIGZpbGUsIGNvbmN1cnJlbnQsIGFsbCB3cml0aW5nIHRoZSBzYW1lIGFydGlmYWN0LlxyXG4gICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OkxhbWJkYTo6RXZlbnRTb3VyY2VNYXBwaW5nJywge1xyXG4gICAgICBCYXRjaFNpemU6IDEwMCxcclxuICAgICAgTWF4aW11bUJhdGNoaW5nV2luZG93SW5TZWNvbmRzOiA2MCxcclxuICAgIH0pO1xyXG4gICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OlNRUzo6UXVldWUnLCB7XHJcbiAgICAgIFJlZHJpdmVQb2xpY3k6IE1hdGNoLm9iamVjdExpa2UoeyBtYXhSZWNlaXZlQ291bnQ6IDMgfSksXHJcbiAgICB9KTtcclxuICB9KTtcclxuXHJcbiAgdGVzdCgndGhlIHF1ZXJ5IGZ1bmN0aW9uIHN0aWxsIGNhbm5vdCB0b3VjaCB0aGUgY29ycHVzJywgKCkgPT4ge1xyXG4gICAgLy8gVGhlIHJlYXNvbiB0aGUgZG9jdW1lbnRzIGZ1bmN0aW9uIGV4aXN0cyBhdCBhbGwuIElmIGxpc3RpbmcgYW5kIGRlbGV0aW5nIGhhZCBiZWVuXHJcbiAgICAvLyBib2x0ZWQgb250byB0aGUgcXVlcnkgTGFtYmRhLCB0aGlzIGFzc2VydGlvbiBpcyB3aGF0IHdvdWxkIGhhdmUgYnJva2VuLlxyXG4gICAgY29uc3QgcG9saWNpZXMgPSB0ZW1wbGF0ZS5maW5kUmVzb3VyY2VzKCdBV1M6OklBTTo6UG9saWN5Jyk7XHJcbiAgICBjb25zdCBbcXVlcnlQb2xpY3ldID0gT2JqZWN0LmVudHJpZXMocG9saWNpZXMpXHJcbiAgICAgIC5maWx0ZXIoKFtuYW1lXSkgPT4gbmFtZS5zdGFydHNXaXRoKCdBcGlRdWVyeVNlcnZpY2VSb2xlJykpXHJcbiAgICAgIC5tYXAoKFssIHJlc291cmNlXSkgPT4gcmVzb3VyY2UgYXMgYW55KTtcclxuICAgIGV4cGVjdChxdWVyeVBvbGljeSkudG9CZURlZmluZWQoKTtcclxuXHJcbiAgICBjb25zdCBqc29uID0gSlNPTi5zdHJpbmdpZnkocXVlcnlQb2xpY3kuUHJvcGVydGllcy5Qb2xpY3lEb2N1bWVudC5TdGF0ZW1lbnQpO1xyXG4gICAgZXhwZWN0KGpzb24pLm5vdC50b0NvbnRhaW4oJ3MzOlB1dE9iamVjdCcpO1xyXG4gICAgZXhwZWN0KGpzb24pLm5vdC50b0NvbnRhaW4oJ3MzOkRlbGV0ZU9iamVjdCcpO1xyXG4gICAgZXhwZWN0KGpzb24pLm5vdC50b0NvbnRhaW4oJ3Jhdy8nKTtcclxuICB9KTtcclxuXHJcbiAgdGVzdCgnZGVsZXRpbmcgYSBkb2N1bWVudCBpcyBhdXRoZW50aWNhdGVkIGxpa2UgZXZlcnl0aGluZyBlbHNlJywgKCkgPT4ge1xyXG4gICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OkFwaUdhdGV3YXk6Ok1ldGhvZCcsIHtcclxuICAgICAgSHR0cE1ldGhvZDogJ0RFTEVURScsXHJcbiAgICAgIEF1dGhvcml6YXRpb25UeXBlOiAnQ1VTVE9NJyxcclxuICAgIH0pO1xyXG4gIH0pO1xyXG5cclxuICB0ZXN0KCdvdXRwdXRzIHRlbGwgdGhlIHJldmlld2VyIGV4YWN0bHkgd2hhdCB0byBydW4gbmV4dCcsICgpID0+IHtcclxuICAgIGNvbnN0IG91dHB1dHMgPSAodGVtcGxhdGUudG9KU09OKCkgYXMgYW55KS5PdXRwdXRzO1xyXG4gICAgZXhwZWN0KG91dHB1dHMuUHJvdmlkZXJBcGlLZXlTZWNyZXQpLnRvQmVEZWZpbmVkKCk7XHJcbiAgICAvLyBUaGUgdmFsdWUgaXMgYW4gRm46OkpvaW4gYmVjYXVzZSB0aGUgZnVuY3Rpb24gbmFtZSBpcyBhIHJlc291cmNlIHJlZmVyZW5jZSwgc28gdGhlXHJcbiAgICAvLyBhc3NlcnRpb24gZ29lcyBhZ2FpbnN0IHRoZSByZW5kZXJlZCBmb3JtIHJhdGhlciB0aGFuIGEgcGxhaW4gc3RyaW5nLlxyXG4gICAgZXhwZWN0KEpTT04uc3RyaW5naWZ5KG91dHB1dHMuU2VlZENvbW1hbmQuVmFsdWUpKS50b0NvbnRhaW4oJ2F3cyBsYW1iZGEgaW52b2tlJyk7XHJcbiAgfSk7XHJcbn0pO1xyXG4iXX0=