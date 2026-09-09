import { IBucket } from 'aws-cdk-lib/aws-s3';
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
export declare class KnowledgeBaseSeeder extends Construct {
    readonly function: TriggerFunction;
    /** Any write or delete under `raw/` lands here and debounces into one rebuild. */
    readonly reindexQueue: Queue;
    constructor(scope: Construct, id: string, props: KnowledgeBaseSeederProps);
    /** The command a reviewer runs to (re)build the index after setting the secret. */
    get seedCommand(): string;
}
