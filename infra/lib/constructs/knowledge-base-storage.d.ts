import { RemovalPolicy } from 'aws-cdk-lib';
import { Table } from 'aws-cdk-lib/aws-dynamodb';
import { IBucket } from 'aws-cdk-lib/aws-s3';
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
export declare class KnowledgeBaseStorage extends Construct {
    readonly bucket: IBucket;
    readonly queryLogTable: Table;
    readonly rawPrefix = "raw/";
    readonly indexKey = "index/kb-index.json.gz";
    constructor(scope: Construct, id: string, props: KnowledgeBaseStorageProps);
    /** ARN pattern for the index object, for scoping the query Lambda's read permission. */
    get indexObjectArn(): string;
    /** ARN pattern for the source documents, for scoping the ingest Lambda's read permission. */
    get rawObjectsArn(): string;
}
/** Re-exported so callers do not need to import from aws-cdk-lib just to read the policy. */
export { RemovalPolicy };
