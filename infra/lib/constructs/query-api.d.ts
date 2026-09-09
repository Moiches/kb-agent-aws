import { RestApi } from 'aws-cdk-lib/aws-apigateway';
import { Table } from 'aws-cdk-lib/aws-dynamodb';
import { Function as LambdaFunction, IFunction } from 'aws-cdk-lib/aws-lambda';
import { IBucket } from 'aws-cdk-lib/aws-s3';
import { ISecret, Secret } from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import { EnvironmentConfig, ModelConfig } from '../config';
export interface QueryApiProps {
    readonly config: EnvironmentConfig;
    readonly cloudWatchRole: boolean;
    readonly models: ModelConfig;
    readonly prefix: string;
    readonly bucket: IBucket;
    readonly indexKey: string;
    readonly rawPrefix: string;
    readonly queryLogTable: Table;
    readonly providerApiKeySecret: ISecret;
    /** Invoked asynchronously to rebuild the index after a document is deleted. */
    readonly ingestFunction: IFunction;
}
/**
 * The authenticated API: gateway, authorizer, and the Lambda that answers questions.
 *
 * **Why REST API rather than the cheaper HTTP API (ADR-03).** The brief specifies an error
 * envelope -- `{error, message, request_id}` -- and HTTP API returns a bare
 * `{"message":"Forbidden"}` for a denied authorizer with no way to change it. Gateway
 * Responses exist only on REST. The alternative would be moving authentication into the
 * business Lambda, which both muddles responsibilities and makes every unauthenticated
 * request cost an invocation. The price difference is $3.50 versus $1.00 per million
 * requests: about six tenths of a cent at this volume.
 */
export declare class QueryApi extends Construct {
    readonly api: RestApi;
    readonly apiTokenSecret: Secret;
    readonly queryFunction: LambdaFunction;
    readonly documentsFunction: LambdaFunction;
    constructor(scope: Construct, id: string, props: QueryApiProps);
    /**
     * Make API Gateway's own errors match the documented contract.
     *
     * Without this, a caller sees `{error, message, request_id}` from the Lambda and a bare
     * `{"message":"Forbidden"}` from the gateway, and has to handle two shapes. This is the
     * capability that REST API has and HTTP API does not, and the reason for ADR-03.
     */
    private addErrorEnvelopes;
    get tokenCommand(): string;
}
