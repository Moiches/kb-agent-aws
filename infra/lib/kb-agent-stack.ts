import * as path from 'path';
import { Annotations, Aws, CfnOutput, Stack, StackProps, Tags, Token } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { Secret } from 'aws-cdk-lib/aws-secretsmanager';
import { SecretValue } from 'aws-cdk-lib';
import { StackSettings } from './config';
import { KnowledgeBaseSeeder } from './constructs/knowledge-base-seeder';
import { KnowledgeBaseStorage } from './constructs/knowledge-base-storage';
import { Observability } from './constructs/observability';
import { QueryApi } from './constructs/query-api';

/**
 * Placeholder written into the provider API key secret at creation time.
 *
 * Shared contract with services/ingest/handler.py, which treats it as "no key configured"
 * and skips seeding cleanly instead of failing the deployment. Not a secret: it is a marker,
 * so having it visible in the template is harmless and intentional.
 */
export const PROVIDER_KEY_PLACEHOLDER = 'REPLACE_WITH_PROVIDER_API_KEY';

export interface KbAgentStackProps extends StackProps {
  readonly settings: StackSettings;
}

/**
 * The knowledge base agent, as one stack composed of constructs (ADR-05).
 *
 * One stack rather than several: cross-stack references become CloudFormation exports, and
 * CloudFormation refuses to delete a stack whose exports are in use. For a reviewer who
 * deploys and destroys once, that is friction with no benefit. Constructs give the same
 * modularity without it.
 */
export class KbAgentStack extends Stack {
  constructor(scope: Construct, id: string, props: KbAgentStackProps) {
    super(scope, id, props);

    const { settings } = props;
    const { env, prefix } = settings;

    this.guardAccount(settings);

    Tags.of(this).add('project', 'kb-agent');
    Tags.of(this).add('environment', env.envName);

    const storage = new KnowledgeBaseStorage(this, 'Storage', {
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
    const providerApiKeySecret = new Secret(this, 'ProviderApiKey', {
      secretName: `${prefix}-${env.envName}/provider-api-key`,
      description:
        'API key for the model provider (OpenRouter by default -- see ADR-09). ' +
        'Populate with: aws secretsmanager put-secret-value --secret-id <name> --secret-string sk-or-...',
      secretStringValue: SecretValue.unsafePlainText(PROVIDER_KEY_PLACEHOLDER),
      removalPolicy: env.removalPolicy,
    });

    const seeder = new KnowledgeBaseSeeder(this, 'Seeder', {
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

    const api = new QueryApi(this, 'Api', {
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

    const observability = new Observability(this, 'Observability', {
      config: env,
      prefix,
      api: api.api,
      queryFunction: api.queryFunction,
      alertEmail: settings.alertEmail,
    });

    new CfnOutput(this, 'DashboardUrl', {
      value: `https://${Aws.REGION}.console.aws.amazon.com/cloudwatch/home?region=${Aws.REGION}#dashboards:name=${observability.dashboard.dashboardName}`,
      description: 'CloudWatch dashboard: traffic, latency, answer quality and token spend',
    });

    new CfnOutput(this, 'ApiBaseUrl', {
      value: api.api.url.replace(/\/$/, ''),
      description: 'Base URL for the Streamlit client. Every route requires a bearer token.',
    });

    new CfnOutput(this, 'ApiTokenSecret', {
      value: api.apiTokenSecret.secretName,
      description: 'Secrets Manager name of the API bearer token, for scripts/configure_client.py',
    });

    new CfnOutput(this, 'GetTokenCommand', {
      value: api.tokenCommand,
      description: 'Prints the API bearer token. Put it in client/.streamlit/secrets.toml.',
    });

    new CfnOutput(this, 'ProviderApiKeySecret', {
      value: providerApiKeySecret.secretName,
      description: 'Set this secret before seeding: aws secretsmanager put-secret-value --secret-id <this> --secret-string sk-or-...',
    });

    new CfnOutput(this, 'SeedCommand', {
      value: seeder.seedCommand,
      description: 'Builds the vector index. Run after populating the provider API key secret.',
    });

    new CfnOutput(this, 'KnowledgeBaseBucket', {
      value: storage.bucket.bucketName,
      description: 'S3 bucket holding source documents (raw/) and the vector index (index/)',
    });

    new CfnOutput(this, 'QueryLogTable', {
      value: storage.queryLogTable.tableName,
      description: 'DynamoDB table holding one item per query, for debugging and evaluation',
    });

    // Configuration echo, not verified state. Nothing in this stack has contacted the
    // provider: it reports what the query Lambda will be told to use once it exists. The
    // authoritative answer at runtime comes from GET /health, which reports the provider
    // and model that actually served a request.
    new CfnOutput(this, 'ConfiguredModelProvider', {
      value: `${settings.models.provider} | generation=${settings.models.generationModel} | embeddings=${settings.models.embeddingModel} (configured, not yet verified)`,
      description:
        'Model provider this stack is configured for (ADR-09). This is configuration only -- ' +
        'no credential has been checked and no model has been called. GET /health reports what is actually in use.',
    });

    new CfnOutput(this, 'DeployedToAccount', {
      value: `${Aws.ACCOUNT_ID} / ${Aws.REGION}`,
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
  private guardAccount(settings: StackSettings): void {
    const { expectedAccount } = settings;
    if (!expectedAccount) {
      return;
    }
    if (Token.isUnresolved(this.account)) {
      Annotations.of(this).addWarningV2(
        'kb-agent:account-guard-skipped',
        'expectedAccount was set but the stack is environment-agnostic, so the account is ' +
          'only known at deploy time. The guard cannot run during synth.',
      );
      return;
    }
    if (this.account !== expectedAccount) {
      throw new Error(
        `Account guard: this stack is configured for account ${expectedAccount} but the ` +
          `active credentials resolve to ${this.account}. Check your --profile.`,
      );
    }
  }
}
