import * as path from 'path';
import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import {
  AccessLogFormat,
  AuthorizationType,
  Cors,
  JsonSchemaType,
  LambdaIntegration,
  LogGroupLogDestination,
  MethodLoggingLevel,
  Model,
  RequestValidator,
  ResponseType,
  RestApi,
  TokenAuthorizer,
} from 'aws-cdk-lib/aws-apigateway';
import { Table } from 'aws-cdk-lib/aws-dynamodb';
import { Code, Function as LambdaFunction, IFunction, Runtime, Tracing } from 'aws-cdk-lib/aws-lambda';
import { LogGroup } from 'aws-cdk-lib/aws-logs';
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
export class QueryApi extends Construct {
  public readonly api: RestApi;
  public readonly apiTokenSecret: Secret;
  public readonly queryFunction: LambdaFunction;
  public readonly documentsFunction: LambdaFunction;

  constructor(scope: Construct, id: string, props: QueryApiProps) {
    super(scope, id);

    const { config, models, prefix, bucket, queryLogTable, providerApiKeySecret } = props;
    const { rawPrefix, ingestFunction } = props;
    const namePrefix = `${prefix}-${config.envName}`;

    // Generated at deploy time and never seen by a human until they ask for it. Unlike the
    // provider key, this one is ours to mint, so there is no reason for it to exist in a
    // file, a shell history, or this repository.
    this.apiTokenSecret = new Secret(this, 'ApiToken', {
      secretName: `${namePrefix}/api-token`,
      description: 'Bearer token the local Streamlit client presents to this API',
      generateSecretString: {
        passwordLength: 48,
        // The authorizer's regex only accepts URL-safe characters; punctuation here would
        // produce a token API Gateway rejects before the authorizer ever sees it.
        excludePunctuation: true,
        excludeCharacters: '"@/\\\'',
      },
      removalPolicy: config.removalPolicy,
    });

    // ---------------------------------------------------------------- authorizer

    const authorizerFunction = new LambdaFunction(this, 'Authorizer', {
      functionName: `${namePrefix}-authorizer`,
      description: 'Validates the bearer token against Secrets Manager',
      runtime: Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: Code.fromAsset(path.join(__dirname, '..', '..', '..', 'services', 'authorizer')),
      timeout: Duration.seconds(5),
      memorySize: 128,
      // An explicit log group rather than the deprecated `logRetention`, which provisions a
      // custom resource to set retention after the fact. This is a plain CloudFormation
      // resource that the stack owns and deletes.
      logGroup: new LogGroup(this, 'AuthorizerLogs', {
        logGroupName: `/aws/lambda/${namePrefix}-authorizer`,
        retention: config.logRetention,
        removalPolicy: config.removalPolicy,
      }),
      environment: {
        API_TOKEN_SECRET_ARN: this.apiTokenSecret.secretArn,
        API_TOKEN_TTL_SECONDS: '300',
      },
    });
    this.apiTokenSecret.grantRead(authorizerFunction);

    const authorizer = new TokenAuthorizer(this, 'BearerAuthorizer', {
      authorizerName: `${namePrefix}-bearer`,
      handler: authorizerFunction,
      identitySource: 'method.request.header.Authorization',
      // Rejected here, before any compute runs. A malformed header costs nothing, which
      // removes both a DoS vector and a way to run up a bill.
      validationRegex: '^Bearer [A-Za-z0-9_-]{32,}$',
      resultsCacheTtl: Duration.minutes(5),
    });

    // ---------------------------------------------------------------- query lambda

    this.queryFunction = new LambdaFunction(this, 'Query', {
      functionName: `${namePrefix}-query`,
      description: 'Retrieval, grounded generation, citation verification and confidence',
      runtime: Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: Code.fromAsset(path.join(__dirname, '..', '..', '..', 'services', 'query')),
      // Generous because the provider is off-AWS and slow: ~625 ms to embed and seconds to
      // generate. API Gateway caps the whole request at 29 s regardless.
      timeout: Duration.seconds(28),
      memorySize: 1024,
      // Only when the account can honour it -- see the note in config.ts. A fresh AWS
      // account cannot, and failing the whole deployment over a defence-in-depth control
      // would be the wrong trade. Stage throttling below is the ceiling that always applies.
      reservedConcurrentExecutions: config.reservedConcurrency,
      logGroup: new LogGroup(this, 'QueryLogs', {
        logGroupName: `/aws/lambda/${namePrefix}-query`,
        retention: config.logRetention,
        removalPolicy: config.removalPolicy,
      }),
      tracing: Tracing.ACTIVE,
      environment: {
        KB_BUCKET: bucket.bucketName,
        KB_INDEX_KEY: props.indexKey,
        QUERY_LOG_TABLE: queryLogTable.tableName,
        PROVIDER_API_KEY_SECRET_ARN: providerApiKeySecret.secretArn,
        MODEL_PROVIDER: models.provider,
        MODEL_ID: models.generationModel,
        EMBED_MODEL_ID: models.embeddingModel,
        EMBED_DIMENSIONS: String(models.embeddingDimensions),
        LOG_QUESTIONS: String(config.logQuestions),
        EMIT_METRICS: String(config.emitMetrics),
        QUERY_LOG_TTL_DAYS: String(config.queryLogTtl.toDays()),
      },
    });

    // Read the index, never write it. Writing is the seeder's job, and a query path that
    // cannot corrupt the knowledge base is one less thing to reason about.
    bucket.grantRead(this.queryFunction, 'index/*');
    queryLogTable.grantWriteData(this.queryFunction);
    providerApiKeySecret.grantRead(this.queryFunction);
    // Note what is absent: the query role cannot read the API token secret, and the
    // authorizer cannot read the provider key. Neither can impersonate the other.

    // ---------------------------------------------------------------- the api

    const accessLogGroup = new LogGroup(this, 'AccessLogs', {
      logGroupName: `/aws/apigateway/${namePrefix}`,
      retention: config.logRetention,
      removalPolicy: config.removalPolicy,
    });

    this.api = new RestApi(this, 'Api', {
      restApiName: namePrefix,
      description: 'AWS-native Knowledge Base Agent (RAG)',
      // Required for access logging to work at all on an account that has never had it set.
      cloudWatchRole: props.cloudWatchRole,
      // RETAIN because it is an account-wide singleton: destroying this stack must not
      // silently disable API Gateway logging for anything else in the account.
      cloudWatchRoleRemovalPolicy: RemovalPolicy.RETAIN,
      deployOptions: {
        stageName: config.envName,
        // Same request id that appears in the Lambda log, the X-Ray trace, the DynamoDB
        // item and the JSON the caller receives. One id, five places.
        accessLogDestination: new LogGroupLogDestination(accessLogGroup),
        accessLogFormat: AccessLogFormat.custom(
          JSON.stringify({
            requestId: '$context.requestId',
            ip: '$context.identity.sourceIp',
            method: '$context.httpMethod',
            path: '$context.path',
            status: '$context.status',
            latency: '$context.responseLatency',
            principal: '$context.authorizer.principalId',
            tokenFingerprint: '$context.authorizer.tokenFingerprint',
            authorizerError: '$context.authorizer.error',
          }),
        ),
        loggingLevel: MethodLoggingLevel.ERROR,
        // Off deliberately: data tracing writes request and response bodies into
        // CloudWatch, which for this API means user questions and retrieved documents.
        dataTraceEnabled: false,
        metricsEnabled: true,
        tracingEnabled: true,
        throttlingRateLimit: config.throttleRateLimit,
        throttlingBurstLimit: config.throttleBurstLimit,
      },
      defaultCorsPreflightOptions: {
        allowOrigins: Cors.ALL_ORIGINS,
        allowHeaders: ['Content-Type', 'Authorization'],
        allowMethods: ['GET', 'POST', 'DELETE', 'OPTIONS'],
      },
    });

    // Validated at the gateway, so a malformed body never reaches -- or bills -- compute.
    const queryModel = new Model(this, 'QueryRequestModel', {
      restApi: this.api,
      modelName: 'QueryRequest',
      contentType: 'application/json',
      schema: {
        type: JsonSchemaType.OBJECT,
        required: ['question'],
        properties: {
          question: { type: JsonSchemaType.STRING, minLength: 3, maxLength: 1000 },
          session_id: { type: JsonSchemaType.STRING, maxLength: 64 },
          top_k: { type: JsonSchemaType.INTEGER, minimum: 1, maximum: 10 },
          // "simple" is the reference prototype's "Explain like I'm 10" toggle. Enumerated
          // at the gateway so an invalid value is rejected before it reaches compute.
          style: { type: JsonSchemaType.STRING, enum: ['standard', 'simple'] },
        },
      },
    });

    // ------------------------------------------------------------- documents function
    //
    // Its own function and its own role, rather than more permissions on the one above.
    // Listing and deleting need write access to `raw/`; answering questions must never have
    // it. Keeping them apart is what lets the query role stay read-only on `index/*`, so a
    // bug on the query path cannot reach the corpus no matter what it does.
    this.documentsFunction = new LambdaFunction(this, 'Documents', {
      functionName: `${namePrefix}-documents`,
      description: 'Lists and deletes knowledge base documents, and triggers reindexing',
      runtime: Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: Code.fromAsset(path.join(__dirname, '..', '..', '..', 'services', 'documents')),
      // Generous only against a cold start plus reading a compressed index; the reindex it
      // starts is asynchronous and outlives this invocation entirely.
      timeout: Duration.seconds(30),
      memorySize: 512,
      tracing: Tracing.ACTIVE,
      logGroup: new LogGroup(this, 'DocumentsLogs', {
        logGroupName: `/aws/lambda/${namePrefix}-documents`,
        retention: config.logRetention,
        removalPolicy: config.removalPolicy,
      }),
      environment: {
        KB_BUCKET: bucket.bucketName,
        KB_INDEX_KEY: props.indexKey,
        RAW_PREFIX: rawPrefix,
        INGEST_FUNCTION_NAME: ingestFunction.functionName,
        MAX_UPLOAD_BYTES: String(config.maxUploadBytes),
      },
    });

    // Read the index; list, write and delete under `raw/`. The asymmetry that matters is
    // the one against `index/`: this function manages source documents and cannot touch the
    // artifact answers come from, so nothing it does can forge a searchable passage. Only
    // the ingest writes the index, and only after reading what is actually in the bucket.
    bucket.grantRead(this.documentsFunction, `${props.indexKey}`);
    bucket.grantRead(this.documentsFunction, `${rawPrefix}*`);
    bucket.grantPut(this.documentsFunction, `${rawPrefix}*`);
    bucket.grantDelete(this.documentsFunction, `${rawPrefix}*`);
    ingestFunction.grantInvoke(this.documentsFunction);

    const integration = new LambdaIntegration(this.queryFunction, { proxy: true });
    const documentsIntegration = new LambdaIntegration(this.documentsFunction, { proxy: true });
    const authorized = { authorizer, authorizationType: AuthorizationType.CUSTOM };

    // One validator shared by every method that needs it. Two methods each declaring
    // `requestValidatorOptions` make CDK mint two validators with the same generated id,
    // which collides at synth.
    const bodyValidator = new RequestValidator(this, 'BodyValidator', {
      restApi: this.api,
      requestValidatorName: `${namePrefix}-body`,
      validateRequestBody: true,
      validateRequestParameters: false,
    });

    // /health is authenticated too. The brief says the API must not be publicly callable,
    // and taking that literally has a useful side effect: `curl /health` without a token
    // returning 401 is the cleanest possible demonstration that auth works.
    this.api.root.addResource('health').addMethod('GET', integration, authorized);
    this.api.root.addResource('query').addMethod('POST', integration, {
      ...authorized,
      requestModels: { 'application/json': queryModel },
      requestValidator: bodyValidator,
    });

    // Same authorizer as everything else. An endpoint that deletes documents is the last
    // one that should be reachable without a token.
    const uploadModel = new Model(this, 'UploadRequestModel', {
      restApi: this.api,
      modelName: 'UploadRequest',
      contentType: 'application/json',
      schema: {
        type: JsonSchemaType.OBJECT,
        required: ['filename'],
        properties: {
          // Shape only. Which extensions the ingest can actually read is enforced in the
          // Lambda, next to the list that decides it, rather than duplicated here where the
          // two copies would drift.
          filename: { type: JsonSchemaType.STRING, minLength: 3, maxLength: 200 },
        },
      },
    });

    const documents = this.api.root.addResource('documents');
    documents.addMethod('GET', documentsIntegration, authorized);
    documents.addMethod('POST', documentsIntegration, {
      ...authorized,
      requestModels: { 'application/json': uploadModel },
      requestValidator: bodyValidator,
    });
    documents.addResource('{documentId}').addMethod('DELETE', documentsIntegration, authorized);

    this.addErrorEnvelopes();
  }

  /**
   * Make API Gateway's own errors match the documented contract.
   *
   * Without this, a caller sees `{error, message, request_id}` from the Lambda and a bare
   * `{"message":"Forbidden"}` from the gateway, and has to handle two shapes. This is the
   * capability that REST API has and HTTP API does not, and the reason for ADR-03.
   */
  private addErrorEnvelopes(): void {
    const envelope = (error: string, message: string) => ({
      'application/json': `{"error":"${error}","message":"${message}","request_id":"$context.requestId"}`,
    });

    const responses: Array<[string, ResponseType, string, string, string]> = [
      ['Unauthorized', ResponseType.UNAUTHORIZED, '401', 'unauthorized', 'Missing or malformed authorization token.'],
      ['AccessDenied', ResponseType.ACCESS_DENIED, '403', 'forbidden', 'The provided token is not valid.'],
      ['BadRequestBody', ResponseType.BAD_REQUEST_BODY, '400', 'bad_request', 'Request body failed validation: $context.error.validationErrorString'],
      ['Throttled', ResponseType.THROTTLED, '429', 'rate_limited', 'Too many requests. Retry after a short delay.'],
      ['NotFound', ResponseType.RESOURCE_NOT_FOUND, '404', 'not_found', 'No such route.'],
      ['ServerError', ResponseType.DEFAULT_5XX, '500', 'internal_error', 'An unexpected error occurred.'],
    ];

    for (const [id, type, statusCode, error, message] of responses) {
      this.api.addGatewayResponse(`GatewayResponse${id}`, {
        type,
        statusCode,
        templates: envelope(error, message),
      });
    }
  }

  public get tokenCommand(): string {
    return `aws secretsmanager get-secret-value --secret-id ${this.apiTokenSecret.secretName} --query SecretString --output text`;
  }
}
