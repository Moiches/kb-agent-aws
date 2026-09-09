import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { resolveSettings } from '../lib/config';
import { KbAgentStack } from '../lib/kb-agent-stack';

/**
 * These are security and hygiene assertions, not functional ones. They exist so that a
 * regression which quietly makes the stack less safe -- a public bucket, a log group that
 * never expires -- fails the build instead of reaching a deployment.
 *
 * They need no AWS credentials.
 */

function synth(context: Record<string, unknown> = {}): Template {
  const app = new App({ context });
  const settings = resolveSettings((key) => app.node.tryGetContext(key));
  const stack = new KbAgentStack(app, 'TestStack', { settings });
  return Template.fromStack(stack);
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
        Statement: Match.arrayWith([
          Match.objectLike({
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
      Value: Match.stringLikeRegexp('^openrouter'),
    });
  });

  test('the provider can be switched at deploy time', () => {
    synth({ provider: 'bedrock' }).hasOutput('ConfiguredModelProvider', {
      Value: Match.stringLikeRegexp('^bedrock'),
    });
  });

  test('the brief\'s suggested Claude 3 Haiku is not used: it is Legacy on Bedrock', () => {
    const json = JSON.stringify(synth({ provider: 'bedrock' }).toJSON());
    expect(json).not.toContain('claude-3-haiku');
  });

  test('model ids can be overridden without a code change', () => {
    synth({ generationModel: 'some/other-model' }).hasOutput('ConfiguredModelProvider', {
      Value: Match.stringLikeRegexp('some/other-model'),
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
      GenerateSecretString: Match.objectLike({ PasswordLength: 48, ExcludePunctuation: true }),
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
      .map(([, resource]) => resource as any);
    expect(seederPolicy).toBeDefined();

    const statements = seederPolicy.Properties.PolicyDocument.Statement as any[];
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
      .map(([, resource]) => resource as any);
    expect(documentsPolicy).toBeDefined();

    const statements = documentsPolicy.Properties.PolicyDocument.Statement as any[];
    const writes = statements.filter((s) =>
      JSON.stringify(s.Action).match(/s3:(PutObject|DeleteObject)/),
    );
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
    template.hasResourceProperties('Custom::S3BucketNotifications', {
      NotificationConfiguration: {
        QueueConfigurations: Match.arrayWith([
          Match.objectLike({ Events: ['s3:ObjectCreated:*'] }),
          Match.objectLike({ Events: ['s3:ObjectRemoved:*'] }),
        ]),
      },
    });

    // Scoped to `raw/`. Notifying on `index/` would make the ingest retrigger itself on
    // its own output, forever, and each loop buys embeddings.
    const notifications = template.findResources('Custom::S3BucketNotifications');
    const configurations = Object.values(notifications).flatMap(
      (r: any) => r.Properties.NotificationConfiguration.QueueConfigurations as any[],
    );
    expect(configurations.length).toBeGreaterThan(0);
    for (const configuration of configurations) {
      expect(configuration.Filter.Key.FilterRules).toEqual([{ Name: 'prefix', Value: 'raw/' }]);
    }
  });

  test('reindex events are debounced, and failures are kept rather than retried forever', () => {
    // A deploy uploads the sample documents in a burst. Wired straight to the Lambda that
    // is one full re-embedding per file, concurrent, all writing the same artifact.
    template.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
      BatchSize: 100,
      MaximumBatchingWindowInSeconds: 60,
    });
    template.hasResourceProperties('AWS::SQS::Queue', {
      RedrivePolicy: Match.objectLike({ maxReceiveCount: 3 }),
    });
  });

  test('the query function still cannot touch the corpus', () => {
    // The reason the documents function exists at all. If listing and deleting had been
    // bolted onto the query Lambda, this assertion is what would have broken.
    const policies = template.findResources('AWS::IAM::Policy');
    const [queryPolicy] = Object.entries(policies)
      .filter(([name]) => name.startsWith('ApiQueryServiceRole'))
      .map(([, resource]) => resource as any);
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
    const outputs = (template.toJSON() as any).Outputs;
    expect(outputs.ProviderApiKeySecret).toBeDefined();
    // The value is an Fn::Join because the function name is a resource reference, so the
    // assertion goes against the rendered form rather than a plain string.
    expect(JSON.stringify(outputs.SeedCommand.Value)).toContain('aws lambda invoke');
  });
});
