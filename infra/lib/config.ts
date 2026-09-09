import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import { RetentionDays } from 'aws-cdk-lib/aws-logs';

/**
 * Environment-specific configuration.
 *
 * Everything that differs between a throwaway development deployment and a real one lives
 * here, so the constructs themselves contain no environment conditionals.
 *
 * Note what is deliberately absent: account and region. The stack is environment-agnostic
 * so that the same commit deploys to a personal development account and to the AMCRO
 * sandbox with nothing but a different `--profile`. Baking an account id into the code is
 * the fastest way to make a take-home undeployable on the reviewer's machine.
 */

export type EnvName = 'dev' | 'prod';
export type ProviderName = 'openrouter' | 'bedrock';

export interface EnvironmentConfig {
  readonly envName: EnvName;

  /** Log retention. Short in dev: CloudWatch Logs is billed per GB ingested and stored. */
  readonly logRetention: RetentionDays;

  /** DESTROY in dev so `cdk destroy` leaves nothing behind; RETAIN where data matters. */
  readonly removalPolicy: RemovalPolicy;

  /**
   * Empty and delete the bucket on destroy. The brief asks reviewers to "manually empty any
   * persistent S3 buckets"; this makes that step unnecessary in dev.
   */
  readonly autoDeleteObjects: boolean;

  /**
   * Hard ceiling on concurrent query Lambdas, and therefore on runaway provider spend.
   *
   * Undefined by default, and that is a portability decision rather than an oversight. AWS
   * accounts start with a total concurrency limit of 10 and refuse any reservation that
   * would leave fewer than 10 unreserved -- so on a fresh account *no* reservation is
   * possible and setting one fails the deployment outright. Measured on a real account:
   * ConcurrentExecutions 10, so `reservedConcurrentExecutions: 5` returned
   * "decreases account's UnreservedConcurrentExecution below its minimum value of [10]".
   *
   * Since the reviewer's account limit is unknowable at synth time, the stack has to deploy
   * without it. Enable with `-c reservedConcurrency=5` on an account with headroom.
   *
   * Cost protection does not depend on this: API Gateway stage throttling is the primary
   * ceiling and is account-independent. Reserved concurrency is defence in depth.
   */
  readonly reservedConcurrency?: number;

  readonly throttleRateLimit: number;
  readonly throttleBurstLimit: number;

  /** Log the user's question (truncated). Off outside dev: questions are user content. */
  readonly logQuestions: boolean;

  /** EMF custom metrics cost $0.30/metric/month. Toggleable for that reason. */
  readonly emitMetrics: boolean;

  readonly queryLogTtl: Duration;
  readonly pointInTimeRecovery: boolean;

  /** Budget thresholds, in USD, to alarm on. Empty disables the budget. */
  readonly budgetLimitUsd?: number;

  /**
   * Ceiling on a single uploaded document, enforced by S3 through a presigned-POST
   * condition rather than by the client.
   *
   * The number comes from the budget, not from what S3 can hold. Every uploaded byte turns
   * into chunks and every chunk into a billed embedding, and the corpus has a second
   * ceiling at `MAX_INDEX_CHUNKS` that refuses the rebuild outright. Dev is deliberately
   * tighter than prod: it is the account with $20 on it.
   */
  readonly maxUploadBytes: number;
}

/**
 * Model configuration.
 *
 * The provider is a deployment-time choice (ADR-09). OpenRouter is the default and the only
 * path exercised end-to-end, because the AMCRO sandbox has no Bedrock access and the
 * solution has to be replicable there.
 */
export interface ModelConfig {
  readonly provider: ProviderName;
  readonly generationModel: string;
  readonly embeddingModel: string;
  readonly embeddingDimensions: number;
}

const ENVIRONMENTS: Record<EnvName, EnvironmentConfig> = {
  dev: {
    envName: 'dev',
    logRetention: RetentionDays.ONE_WEEK,
    removalPolicy: RemovalPolicy.DESTROY,
    autoDeleteObjects: true,
    throttleRateLimit: 5,
    throttleBurstLimit: 10,
    logQuestions: true,
    emitMetrics: true,
    queryLogTtl: Duration.days(30),
    pointInTimeRecovery: false,
    budgetLimitUsd: 20,
    maxUploadBytes: 20 * 1024 * 1024,
  },
  prod: {
    envName: 'prod',
    logRetention: RetentionDays.THREE_MONTHS,
    removalPolicy: RemovalPolicy.RETAIN,
    autoDeleteObjects: false,
    reservedConcurrency: 50,
    throttleRateLimit: 100,
    throttleBurstLimit: 200,
    logQuestions: false,
    emitMetrics: true,
    queryLogTtl: Duration.days(90),
    pointInTimeRecovery: true,
    maxUploadBytes: 100 * 1024 * 1024,
  },
};

/**
 * Defaults verified against Bedrock on 2026-09-07 rather than assumed:
 *
 *  - `anthropic.claude-3-haiku-20240307-v1:0`, which the project brief suggests, is now
 *    marked Legacy by the provider and returns ResourceNotFoundException for accounts that
 *    were not already using it. Claude Haiku 4.5 is its active successor and keeps the
 *    brief's cost-conscious intent.
 *  - Titan Text Embeddings V2 honours `dimensions: 512` and returns unit-normalized vectors
 *    (measured L2 norm 1.0), which is what lets the retriever treat cosine similarity as a
 *    plain dot product.
 *
 * The OpenRouter slugs were probed on the same day (scripts/check_openrouter.py) rather
 * than assumed: both resolve, `dimensions: 512` is honoured, and the returned vectors are
 * unit-normalized to float32 precision. They stay overridable by context so trying another
 * model never requires a code change.
 */
const MODELS: Record<ProviderName, ModelConfig> = {
  openrouter: {
    provider: 'openrouter',
    generationModel: 'anthropic/claude-haiku-4.5',
    embeddingModel: 'openai/text-embedding-3-small',
    embeddingDimensions: 512,
  },
  bedrock: {
    provider: 'bedrock',
    generationModel: 'anthropic.claude-haiku-4-5-20251001-v1:0',
    embeddingModel: 'amazon.titan-embed-text-v2:0',
    embeddingDimensions: 512,
  },
};

export interface StackSettings {
  readonly env: EnvironmentConfig;
  readonly models: ModelConfig;
  readonly prefix: string;
  /** When set, the stack refuses to deploy into any other account. Opt-in. */
  readonly expectedAccount?: string;
  /**
   * Whether to create the account-level CloudWatch Logs role API Gateway needs.
   *
   * `AWS::ApiGateway::Account` is a singleton per account and region. A fresh account has
   * none, and without it the stage fails to create at all: "CloudWatch Logs role ARN must
   * be set in account settings to enable logging". So it defaults to true, because a
   * deployment that works on an untouched account is the point.
   *
   * Pass `-c cloudWatchRole=false` in a shared account where someone else already
   * configured it, to avoid two stacks fighting over one global setting.
   */
  readonly cloudWatchRole: boolean;
  /**
   * Where alarms and budget alerts are sent. Optional, and no default is invented: a
   * hard-coded address would send another account's alerts to the wrong person, and a
   * budget with no subscriber cannot notify anyone, so it is simply not created.
   */
  readonly alertEmail?: string;
}

/** Read settings from CDK context: `cdk deploy -c env=dev -c prefix=kbagent-mc`. */
export function resolveSettings(tryGetContext: (key: string) => unknown): StackSettings {
  const envName = (tryGetContext('env') as EnvName) ?? 'dev';
  if (!(envName in ENVIRONMENTS)) {
    throw new Error(`Unknown env "${envName}". Expected one of: ${Object.keys(ENVIRONMENTS).join(', ')}`);
  }

  const provider = (tryGetContext('provider') as ProviderName) ?? 'openrouter';
  if (!(provider in MODELS)) {
    throw new Error(`Unknown provider "${provider}". Expected one of: ${Object.keys(MODELS).join(', ')}`);
  }

  const prefix = (tryGetContext('prefix') as string) ?? 'kbagent';
  if (!/^[a-z0-9-]{3,24}$/.test(prefix)) {
    throw new Error(
      `Invalid prefix "${prefix}". Use 3-24 lowercase letters, digits or hyphens; it becomes part of an S3 bucket name.`,
    );
  }

  const overrides: Partial<ModelConfig> = {
    generationModel: tryGetContext('generationModel') as string,
    embeddingModel: tryGetContext('embeddingModel') as string,
  };

  // Opt-in, because whether the target account can honour it is not knowable here.
  const reservedConcurrency = tryGetContext('reservedConcurrency');

  return {
    env: {
      ...ENVIRONMENTS[envName],
      ...(reservedConcurrency !== undefined
        ? { reservedConcurrency: Number(reservedConcurrency) }
        : {}),
    },
    models: {
      ...MODELS[provider],
      ...Object.fromEntries(Object.entries(overrides).filter(([, v]) => v !== undefined)),
    },
    prefix,
    expectedAccount: tryGetContext('expectedAccount') as string | undefined,
    cloudWatchRole: tryGetContext('cloudWatchRole') !== 'false',
    alertEmail: tryGetContext('alertEmail') as string | undefined,
  };
}
