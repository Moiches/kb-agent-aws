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
exports.QueryApi = void 0;
const path = __importStar(require("path"));
const aws_cdk_lib_1 = require("aws-cdk-lib");
const aws_apigateway_1 = require("aws-cdk-lib/aws-apigateway");
const aws_lambda_1 = require("aws-cdk-lib/aws-lambda");
const aws_logs_1 = require("aws-cdk-lib/aws-logs");
const aws_secretsmanager_1 = require("aws-cdk-lib/aws-secretsmanager");
const constructs_1 = require("constructs");
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
class QueryApi extends constructs_1.Construct {
    api;
    apiTokenSecret;
    queryFunction;
    documentsFunction;
    constructor(scope, id, props) {
        super(scope, id);
        const { config, models, prefix, bucket, queryLogTable, providerApiKeySecret } = props;
        const { rawPrefix, ingestFunction } = props;
        const namePrefix = `${prefix}-${config.envName}`;
        // Generated at deploy time and never seen by a human until they ask for it. Unlike the
        // provider key, this one is ours to mint, so there is no reason for it to exist in a
        // file, a shell history, or this repository.
        this.apiTokenSecret = new aws_secretsmanager_1.Secret(this, 'ApiToken', {
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
        const authorizerFunction = new aws_lambda_1.Function(this, 'Authorizer', {
            functionName: `${namePrefix}-authorizer`,
            description: 'Validates the bearer token against Secrets Manager',
            runtime: aws_lambda_1.Runtime.PYTHON_3_12,
            handler: 'handler.lambda_handler',
            code: aws_lambda_1.Code.fromAsset(path.join(__dirname, '..', '..', '..', 'services', 'authorizer')),
            timeout: aws_cdk_lib_1.Duration.seconds(5),
            memorySize: 128,
            // An explicit log group rather than the deprecated `logRetention`, which provisions a
            // custom resource to set retention after the fact. This is a plain CloudFormation
            // resource that the stack owns and deletes.
            logGroup: new aws_logs_1.LogGroup(this, 'AuthorizerLogs', {
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
        const authorizer = new aws_apigateway_1.TokenAuthorizer(this, 'BearerAuthorizer', {
            authorizerName: `${namePrefix}-bearer`,
            handler: authorizerFunction,
            identitySource: 'method.request.header.Authorization',
            // Rejected here, before any compute runs. A malformed header costs nothing, which
            // removes both a DoS vector and a way to run up a bill.
            validationRegex: '^Bearer [A-Za-z0-9_-]{32,}$',
            resultsCacheTtl: aws_cdk_lib_1.Duration.minutes(5),
        });
        // ---------------------------------------------------------------- query lambda
        this.queryFunction = new aws_lambda_1.Function(this, 'Query', {
            functionName: `${namePrefix}-query`,
            description: 'Retrieval, grounded generation, citation verification and confidence',
            runtime: aws_lambda_1.Runtime.PYTHON_3_12,
            handler: 'handler.lambda_handler',
            code: aws_lambda_1.Code.fromAsset(path.join(__dirname, '..', '..', '..', 'services', 'query')),
            // Generous because the provider is off-AWS and slow: ~625 ms to embed and seconds to
            // generate. API Gateway caps the whole request at 29 s regardless.
            timeout: aws_cdk_lib_1.Duration.seconds(28),
            memorySize: 1024,
            // Only when the account can honour it -- see the note in config.ts. A fresh AWS
            // account cannot, and failing the whole deployment over a defence-in-depth control
            // would be the wrong trade. Stage throttling below is the ceiling that always applies.
            reservedConcurrentExecutions: config.reservedConcurrency,
            logGroup: new aws_logs_1.LogGroup(this, 'QueryLogs', {
                logGroupName: `/aws/lambda/${namePrefix}-query`,
                retention: config.logRetention,
                removalPolicy: config.removalPolicy,
            }),
            tracing: aws_lambda_1.Tracing.ACTIVE,
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
        const accessLogGroup = new aws_logs_1.LogGroup(this, 'AccessLogs', {
            logGroupName: `/aws/apigateway/${namePrefix}`,
            retention: config.logRetention,
            removalPolicy: config.removalPolicy,
        });
        this.api = new aws_apigateway_1.RestApi(this, 'Api', {
            restApiName: namePrefix,
            description: 'AWS-native Knowledge Base Agent (RAG)',
            // Required for access logging to work at all on an account that has never had it set.
            cloudWatchRole: props.cloudWatchRole,
            // RETAIN because it is an account-wide singleton: destroying this stack must not
            // silently disable API Gateway logging for anything else in the account.
            cloudWatchRoleRemovalPolicy: aws_cdk_lib_1.RemovalPolicy.RETAIN,
            deployOptions: {
                stageName: config.envName,
                // Same request id that appears in the Lambda log, the X-Ray trace, the DynamoDB
                // item and the JSON the caller receives. One id, five places.
                accessLogDestination: new aws_apigateway_1.LogGroupLogDestination(accessLogGroup),
                accessLogFormat: aws_apigateway_1.AccessLogFormat.custom(JSON.stringify({
                    requestId: '$context.requestId',
                    ip: '$context.identity.sourceIp',
                    method: '$context.httpMethod',
                    path: '$context.path',
                    status: '$context.status',
                    latency: '$context.responseLatency',
                    principal: '$context.authorizer.principalId',
                    tokenFingerprint: '$context.authorizer.tokenFingerprint',
                    authorizerError: '$context.authorizer.error',
                })),
                loggingLevel: aws_apigateway_1.MethodLoggingLevel.ERROR,
                // Off deliberately: data tracing writes request and response bodies into
                // CloudWatch, which for this API means user questions and retrieved documents.
                dataTraceEnabled: false,
                metricsEnabled: true,
                tracingEnabled: true,
                throttlingRateLimit: config.throttleRateLimit,
                throttlingBurstLimit: config.throttleBurstLimit,
            },
            defaultCorsPreflightOptions: {
                allowOrigins: aws_apigateway_1.Cors.ALL_ORIGINS,
                allowHeaders: ['Content-Type', 'Authorization'],
                allowMethods: ['GET', 'POST', 'DELETE', 'OPTIONS'],
            },
        });
        // Validated at the gateway, so a malformed body never reaches -- or bills -- compute.
        const queryModel = new aws_apigateway_1.Model(this, 'QueryRequestModel', {
            restApi: this.api,
            modelName: 'QueryRequest',
            contentType: 'application/json',
            schema: {
                type: aws_apigateway_1.JsonSchemaType.OBJECT,
                required: ['question'],
                properties: {
                    question: { type: aws_apigateway_1.JsonSchemaType.STRING, minLength: 3, maxLength: 1000 },
                    session_id: { type: aws_apigateway_1.JsonSchemaType.STRING, maxLength: 64 },
                    top_k: { type: aws_apigateway_1.JsonSchemaType.INTEGER, minimum: 1, maximum: 10 },
                    // "simple" is the reference prototype's "Explain like I'm 10" toggle. Enumerated
                    // at the gateway so an invalid value is rejected before it reaches compute.
                    style: { type: aws_apigateway_1.JsonSchemaType.STRING, enum: ['standard', 'simple'] },
                },
            },
        });
        // ------------------------------------------------------------- documents function
        //
        // Its own function and its own role, rather than more permissions on the one above.
        // Listing and deleting need write access to `raw/`; answering questions must never have
        // it. Keeping them apart is what lets the query role stay read-only on `index/*`, so a
        // bug on the query path cannot reach the corpus no matter what it does.
        this.documentsFunction = new aws_lambda_1.Function(this, 'Documents', {
            functionName: `${namePrefix}-documents`,
            description: 'Lists and deletes knowledge base documents, and triggers reindexing',
            runtime: aws_lambda_1.Runtime.PYTHON_3_12,
            handler: 'handler.lambda_handler',
            code: aws_lambda_1.Code.fromAsset(path.join(__dirname, '..', '..', '..', 'services', 'documents')),
            // Generous only against a cold start plus reading a compressed index; the reindex it
            // starts is asynchronous and outlives this invocation entirely.
            timeout: aws_cdk_lib_1.Duration.seconds(30),
            memorySize: 512,
            tracing: aws_lambda_1.Tracing.ACTIVE,
            logGroup: new aws_logs_1.LogGroup(this, 'DocumentsLogs', {
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
        const integration = new aws_apigateway_1.LambdaIntegration(this.queryFunction, { proxy: true });
        const documentsIntegration = new aws_apigateway_1.LambdaIntegration(this.documentsFunction, { proxy: true });
        const authorized = { authorizer, authorizationType: aws_apigateway_1.AuthorizationType.CUSTOM };
        // One validator shared by every method that needs it. Two methods each declaring
        // `requestValidatorOptions` make CDK mint two validators with the same generated id,
        // which collides at synth.
        const bodyValidator = new aws_apigateway_1.RequestValidator(this, 'BodyValidator', {
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
        const uploadModel = new aws_apigateway_1.Model(this, 'UploadRequestModel', {
            restApi: this.api,
            modelName: 'UploadRequest',
            contentType: 'application/json',
            schema: {
                type: aws_apigateway_1.JsonSchemaType.OBJECT,
                required: ['filename'],
                properties: {
                    // Shape only. Which extensions the ingest can actually read is enforced in the
                    // Lambda, next to the list that decides it, rather than duplicated here where the
                    // two copies would drift.
                    filename: { type: aws_apigateway_1.JsonSchemaType.STRING, minLength: 3, maxLength: 200 },
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
    addErrorEnvelopes() {
        const envelope = (error, message) => ({
            'application/json': `{"error":"${error}","message":"${message}","request_id":"$context.requestId"}`,
        });
        const responses = [
            ['Unauthorized', aws_apigateway_1.ResponseType.UNAUTHORIZED, '401', 'unauthorized', 'Missing or malformed authorization token.'],
            ['AccessDenied', aws_apigateway_1.ResponseType.ACCESS_DENIED, '403', 'forbidden', 'The provided token is not valid.'],
            ['BadRequestBody', aws_apigateway_1.ResponseType.BAD_REQUEST_BODY, '400', 'bad_request', 'Request body failed validation: $context.error.validationErrorString'],
            ['Throttled', aws_apigateway_1.ResponseType.THROTTLED, '429', 'rate_limited', 'Too many requests. Retry after a short delay.'],
            ['NotFound', aws_apigateway_1.ResponseType.RESOURCE_NOT_FOUND, '404', 'not_found', 'No such route.'],
            ['ServerError', aws_apigateway_1.ResponseType.DEFAULT_5XX, '500', 'internal_error', 'An unexpected error occurred.'],
        ];
        for (const [id, type, statusCode, error, message] of responses) {
            this.api.addGatewayResponse(`GatewayResponse${id}`, {
                type,
                statusCode,
                templates: envelope(error, message),
            });
        }
    }
    get tokenCommand() {
        return `aws secretsmanager get-secret-value --secret-id ${this.apiTokenSecret.secretName} --query SecretString --output text`;
    }
}
exports.QueryApi = QueryApi;
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoicXVlcnktYXBpLmpzIiwic291cmNlUm9vdCI6IiIsInNvdXJjZXMiOlsicXVlcnktYXBpLnRzIl0sIm5hbWVzIjpbXSwibWFwcGluZ3MiOiI7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7OztBQUFBLDJDQUE2QjtBQUM3Qiw2Q0FBc0Q7QUFDdEQsK0RBYW9DO0FBRXBDLHVEQUF1RztBQUN2RyxtREFBZ0Q7QUFFaEQsdUVBQWlFO0FBQ2pFLDJDQUF1QztBQWlCdkM7Ozs7Ozs7Ozs7R0FVRztBQUNILE1BQWEsUUFBUyxTQUFRLHNCQUFTO0lBQ3JCLEdBQUcsQ0FBVTtJQUNiLGNBQWMsQ0FBUztJQUN2QixhQUFhLENBQWlCO0lBQzlCLGlCQUFpQixDQUFpQjtJQUVsRCxZQUFZLEtBQWdCLEVBQUUsRUFBVSxFQUFFLEtBQW9CO1FBQzVELEtBQUssQ0FBQyxLQUFLLEVBQUUsRUFBRSxDQUFDLENBQUM7UUFFakIsTUFBTSxFQUFFLE1BQU0sRUFBRSxNQUFNLEVBQUUsTUFBTSxFQUFFLE1BQU0sRUFBRSxhQUFhLEVBQUUsb0JBQW9CLEVBQUUsR0FBRyxLQUFLLENBQUM7UUFDdEYsTUFBTSxFQUFFLFNBQVMsRUFBRSxjQUFjLEVBQUUsR0FBRyxLQUFLLENBQUM7UUFDNUMsTUFBTSxVQUFVLEdBQUcsR0FBRyxNQUFNLElBQUksTUFBTSxDQUFDLE9BQU8sRUFBRSxDQUFDO1FBRWpELHVGQUF1RjtRQUN2RixxRkFBcUY7UUFDckYsNkNBQTZDO1FBQzdDLElBQUksQ0FBQyxjQUFjLEdBQUcsSUFBSSwyQkFBTSxDQUFDLElBQUksRUFBRSxVQUFVLEVBQUU7WUFDakQsVUFBVSxFQUFFLEdBQUcsVUFBVSxZQUFZO1lBQ3JDLFdBQVcsRUFBRSw4REFBOEQ7WUFDM0Usb0JBQW9CLEVBQUU7Z0JBQ3BCLGNBQWMsRUFBRSxFQUFFO2dCQUNsQixrRkFBa0Y7Z0JBQ2xGLDBFQUEwRTtnQkFDMUUsa0JBQWtCLEVBQUUsSUFBSTtnQkFDeEIsaUJBQWlCLEVBQUUsU0FBUzthQUM3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LDRCQWtUQyIsInNvdXJjZXNDb250ZW50IjpbImltcG9ydCAqIGFzIHBhdGggZnJvbSAncGF0aCc7XHJcbmltcG9ydCB7IER1cmF0aW9uLCBSZW1vdmFsUG9saWN5IH0gZnJvbSAnYXdzLWNkay1saWInO1xyXG5pbXBvcnQge1xyXG4gIEFjY2Vzc0xvZ0Zvcm1hdCxcclxuICBBdXRob3JpemF0aW9uVHlwZSxcclxuICBDb3JzLFxyXG4gIEpzb25TY2hlbWFUeXBlLFxyXG4gIExhbWJkYUludGVncmF0aW9uLFxyXG4gIExvZ0dyb3VwTG9nRGVzdGluYXRpb24sXHJcbiAgTWV0aG9kTG9nZ2luZ0xldmVsLFxyXG4gIE1vZGVsLFxyXG4gIFJlcXVlc3RWYWxpZGF0b3IsXHJcbiAgUmVzcG9uc2VUeXBlLFxyXG4gIFJlc3RBcGksXHJcbiAgVG9rZW5BdXRob3JpemVyLFxyXG59IGZyb20gJ2F3cy1jZGstbGliL2F3cy1hcGlnYXRld2F5JztcclxuaW1wb3J0IHsgVGFibGUgfSBmcm9tICdhd3MtY2RrLWxpYi9hd3MtZHluYW1vZGInO1xyXG5pbXBvcnQgeyBDb2RlLCBGdW5jdGlvbiBhcyBMYW1iZGFGdW5jdGlvbiwgSUZ1bmN0aW9uLCBSdW50aW1lLCBUcmFjaW5nIH0gZnJvbSAnYXdzLWNkay1saWIvYXdzLWxhbWJkYSc7XHJcbmltcG9ydCB7IExvZ0dyb3VwIH0gZnJvbSAnYXdzLWNkay1saWIvYXdzLWxvZ3MnO1xyXG5pbXBvcnQgeyBJQnVja2V0IH0gZnJvbSAnYXdzLWNkay1saWIvYXdzLXMzJztcclxuaW1wb3J0IHsgSVNlY3JldCwgU2VjcmV0IH0gZnJvbSAnYXdzLWNkay1saWIvYXdzLXNlY3JldHNtYW5hZ2VyJztcclxuaW1wb3J0IHsgQ29uc3RydWN0IH0gZnJvbSAnY29uc3RydWN0cyc7XHJcbmltcG9ydCB7IEVudmlyb25tZW50Q29uZmlnLCBNb2RlbENvbmZpZyB9IGZyb20gJy4uL2NvbmZpZyc7XHJcblxyXG5leHBvcnQgaW50ZXJmYWNlIFF1ZXJ5QXBpUHJvcHMge1xyXG4gIHJlYWRvbmx5IGNvbmZpZzogRW52aXJvbm1lbnRDb25maWc7XHJcbiAgcmVhZG9ubHkgY2xvdWRXYXRjaFJvbGU6IGJvb2xlYW47XHJcbiAgcmVhZG9ubHkgbW9kZWxzOiBNb2RlbENvbmZpZztcclxuICByZWFkb25seSBwcmVmaXg6IHN0cmluZztcclxuICByZWFkb25seSBidWNrZXQ6IElCdWNrZXQ7XHJcbiAgcmVhZG9ubHkgaW5kZXhLZXk6IHN0cmluZztcclxuICByZWFkb25seSByYXdQcmVmaXg6IHN0cmluZztcclxuICByZWFkb25seSBxdWVyeUxvZ1RhYmxlOiBUYWJsZTtcclxuICByZWFkb25seSBwcm92aWRlckFwaUtleVNlY3JldDogSVNlY3JldDtcclxuICAvKiogSW52b2tlZCBhc3luY2hyb25vdXNseSB0byByZWJ1aWxkIHRoZSBpbmRleCBhZnRlciBhIGRvY3VtZW50IGlzIGRlbGV0ZWQuICovXHJcbiAgcmVhZG9ubHkgaW5nZXN0RnVuY3Rpb246IElGdW5jdGlvbjtcclxufVxyXG5cclxuLyoqXHJcbiAqIFRoZSBhdXRoZW50aWNhdGVkIEFQSTogZ2F0ZXdheSwgYXV0aG9yaXplciwgYW5kIHRoZSBMYW1iZGEgdGhhdCBhbnN3ZXJzIHF1ZXN0aW9ucy5cclxuICpcclxuICogKipXaHkgUkVTVCBBUEkgcmF0aGVyIHRoYW4gdGhlIGNoZWFwZXIgSFRUUCBBUEkgKEFEUi0wMykuKiogVGhlIGJyaWVmIHNwZWNpZmllcyBhbiBlcnJvclxyXG4gKiBlbnZlbG9wZSAtLSBge2Vycm9yLCBtZXNzYWdlLCByZXF1ZXN0X2lkfWAgLS0gYW5kIEhUVFAgQVBJIHJldHVybnMgYSBiYXJlXHJcbiAqIGB7XCJtZXNzYWdlXCI6XCJGb3JiaWRkZW5cIn1gIGZvciBhIGRlbmllZCBhdXRob3JpemVyIHdpdGggbm8gd2F5IHRvIGNoYW5nZSBpdC4gR2F0ZXdheVxyXG4gKiBSZXNwb25zZXMgZXhpc3Qgb25seSBvbiBSRVNULiBUaGUgYWx0ZXJuYXRpdmUgd291bGQgYmUgbW92aW5nIGF1dGhlbnRpY2F0aW9uIGludG8gdGhlXHJcbiAqIGJ1c2luZXNzIExhbWJkYSwgd2hpY2ggYm90aCBtdWRkbGVzIHJlc3BvbnNpYmlsaXRpZXMgYW5kIG1ha2VzIGV2ZXJ5IHVuYXV0aGVudGljYXRlZFxyXG4gKiByZXF1ZXN0IGNvc3QgYW4gaW52b2NhdGlvbi4gVGhlIHByaWNlIGRpZmZlcmVuY2UgaXMgJDMuNTAgdmVyc3VzICQxLjAwIHBlciBtaWxsaW9uXHJcbiAqIHJlcXVlc3RzOiBhYm91dCBzaXggdGVudGhzIG9mIGEgY2VudCBhdCB0aGlzIHZvbHVtZS5cclxuICovXHJcbmV4cG9ydCBjbGFzcyBRdWVyeUFwaSBleHRlbmRzIENvbnN0cnVjdCB7XHJcbiAgcHVibGljIHJlYWRvbmx5IGFwaTogUmVzdEFwaTtcclxuICBwdWJsaWMgcmVhZG9ubHkgYXBpVG9rZW5TZWNyZXQ6IFNlY3JldDtcclxuICBwdWJsaWMgcmVhZG9ubHkgcXVlcnlGdW5jdGlvbjogTGFtYmRhRnVuY3Rpb247XHJcbiAgcHVibGljIHJlYWRvbmx5IGRvY3VtZW50c0Z1bmN0aW9uOiBMYW1iZGFGdW5jdGlvbjtcclxuXHJcbiAgY29uc3RydWN0b3Ioc2NvcGU6IENvbnN0cnVjdCwgaWQ6IHN0cmluZywgcHJvcHM6IFF1ZXJ5QXBpUHJvcHMpIHtcclxuICAgIHN1cGVyKHNjb3BlLCBpZCk7XHJcblxyXG4gICAgY29uc3QgeyBjb25maWcsIG1vZGVscywgcHJlZml4LCBidWNrZXQsIHF1ZXJ5TG9nVGFibGUsIHByb3ZpZGVyQXBpS2V5U2VjcmV0IH0gPSBwcm9wcztcclxuICAgIGNvbnN0IHsgcmF3UHJlZml4LCBpbmdlc3RGdW5jdGlvbiB9ID0gcHJvcHM7XHJcbiAgICBjb25zdCBuYW1lUHJlZml4ID0gYCR7cHJlZml4fS0ke2NvbmZpZy5lbnZOYW1lfWA7XHJcblxyXG4gICAgLy8gR2VuZXJhdGVkIGF0IGRlcGxveSB0aW1lIGFuZCBuZXZlciBzZWVuIGJ5IGEgaHVtYW4gdW50aWwgdGhleSBhc2sgZm9yIGl0LiBVbmxpa2UgdGhlXHJcbiAgICAvLyBwcm92aWRlciBrZXksIHRoaXMgb25lIGlzIG91cnMgdG8gbWludCwgc28gdGhlcmUgaXMgbm8gcmVhc29uIGZvciBpdCB0byBleGlzdCBpbiBhXHJcbiAgICAvLyBmaWxlLCBhIHNoZWxsIGhpc3RvcnksIG9yIHRoaXMgcmVwb3NpdG9yeS5cclxuICAgIHRoaXMuYXBpVG9rZW5TZWNyZXQgPSBuZXcgU2VjcmV0KHRoaXMsICdBcGlUb2tlbicsIHtcclxuICAgICAgc2VjcmV0TmFtZTogYCR7bmFtZVByZWZpeH0vYXBpLXRva2VuYCxcclxuICAgICAgZGVzY3JpcHRpb246ICdCZWFyZXIgdG9rZW4gdGhlIGxvY2FsIFN0cmVhbWxpdCBjbGllbnQgcHJlc2VudHMgdG8gdGhpcyBBUEknLFxyXG4gICAgICBnZW5lcmF0ZVNlY3JldFN0cmluZzoge1xyXG4gICAgICAgIHBhc3N3b3JkTGVuZ3RoOiA0OCxcclxuICAgICAgICAvLyBUaGUgYXV0aG9yaXplcidzIHJlZ2V4IG9ubHkgYWNjZXB0cyBVUkwtc2FmZSBjaGFyYWN0ZXJzOyBwdW5jdHVhdGlvbiBoZXJlIHdvdWxkXHJcbiAgICAgICAgLy8gcHJvZHVjZSBhIHRva2VuIEFQSSBHYXRld2F5IHJlamVjdHMgYmVmb3JlIHRoZSBhdXRob3JpemVyIGV2ZXIgc2VlcyBpdC5cclxuICAgICAgICBleGNsdWRlUHVuY3R1YXRpb246IHRydWUsXHJcbiAgICAgICAgZXhjbHVkZUNoYXJhY3RlcnM6ICdcIkAvXFxcXFxcJycsXHJcbiAgICAgIH0sXHJcbiAgICAgIHJlbW92YWxQb2xpY3k6IGNvbmZpZy5yZW1vdmFsUG9saWN5LFxyXG4gICAgfSk7XHJcblxyXG4gICAgLy8gLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLSBhdXRob3JpemVyXHJcblxyXG4gICAgY29uc3QgYXV0aG9yaXplckZ1bmN0aW9uID0gbmV3IExhbWJkYUZ1bmN0aW9uKHRoaXMsICdBdXRob3JpemVyJywge1xyXG4gICAgICBmdW5jdGlvbk5hbWU6IGAke25hbWVQcmVmaXh9LWF1dGhvcml6ZXJgLFxyXG4gICAgICBkZXNjcmlwdGlvbjogJ1ZhbGlkYXRlcyB0aGUgYmVhcmVyIHRva2VuIGFnYWluc3QgU2VjcmV0cyBNYW5hZ2VyJyxcclxuICAgICAgcnVudGltZTogUnVudGltZS5QWVRIT05fM18xMixcclxuICAgICAgaGFuZGxlcjogJ2hhbmRsZXIubGFtYmRhX2hhbmRsZXInLFxyXG4gICAgICBjb2RlOiBDb2RlLmZyb21Bc3NldChwYXRoLmpvaW4oX19kaXJuYW1lLCAnLi4nLCAnLi4nLCAnLi4nLCAnc2VydmljZXMnLCAnYXV0aG9yaXplcicpKSxcclxuICAgICAgdGltZW91dDogRHVyYXRpb24uc2Vjb25kcyg1KSxcclxuICAgICAgbWVtb3J5U2l6ZTogMTI4LFxyXG4gICAgICAvLyBBbiBleHBsaWNpdCBsb2cgZ3JvdXAgcmF0aGVyIHRoYW4gdGhlIGRlcHJlY2F0ZWQgYGxvZ1JldGVudGlvbmAsIHdoaWNoIHByb3Zpc2lvbnMgYVxyXG4gICAgICAvLyBjdXN0b20gcmVzb3VyY2UgdG8gc2V0IHJldGVudGlvbiBhZnRlciB0aGUgZmFjdC4gVGhpcyBpcyBhIHBsYWluIENsb3VkRm9ybWF0aW9uXHJcbiAgICAgIC8vIHJlc291cmNlIHRoYXQgdGhlIHN0YWNrIG93bnMgYW5kIGRlbGV0ZXMuXHJcbiAgICAgIGxvZ0dyb3VwOiBuZXcgTG9nR3JvdXAodGhpcywgJ0F1dGhvcml6ZXJMb2dzJywge1xyXG4gICAgICAgIGxvZ0dyb3VwTmFtZTogYC9hd3MvbGFtYmRhLyR7bmFtZVByZWZpeH0tYXV0aG9yaXplcmAsXHJcbiAgICAgICAgcmV0ZW50aW9uOiBjb25maWcubG9nUmV0ZW50aW9uLFxyXG4gICAgICAgIHJlbW92YWxQb2xpY3k6IGNvbmZpZy5yZW1vdmFsUG9saWN5LFxyXG4gICAgICB9KSxcclxuICAgICAgZW52aXJvbm1lbnQ6IHtcclxuICAgICAgICBBUElfVE9LRU5fU0VDUkVUX0FSTjogdGhpcy5hcGlUb2tlblNlY3JldC5zZWNyZXRBcm4sXHJcbiAgICAgICAgQVBJX1RPS0VOX1RUTF9TRUNPTkRTOiAnMzAwJyxcclxuICAgICAgfSxcclxuICAgIH0pO1xyXG4gICAgdGhpcy5hcGlUb2tlblNlY3JldC5ncmFudFJlYWQoYXV0aG9yaXplckZ1bmN0aW9uKTtcclxuXHJcbiAgICBjb25zdCBhdXRob3JpemVyID0gbmV3IFRva2VuQXV0aG9yaXplcih0aGlzLCAnQmVhcmVyQXV0aG9yaXplcicsIHtcclxuICAgICAgYXV0aG9yaXplck5hbWU6IGAke25hbWVQcmVmaXh9LWJlYXJlcmAsXHJcbiAgICAgIGhhbmRsZXI6IGF1dGhvcml6ZXJGdW5jdGlvbixcclxuICAgICAgaWRlbnRpdHlTb3VyY2U6ICdtZXRob2QucmVxdWVzdC5oZWFkZXIuQXV0aG9yaXphdGlvbicsXHJcbiAgICAgIC8vIFJlamVjdGVkIGhlcmUsIGJlZm9yZSBhbnkgY29tcHV0ZSBydW5zLiBBIG1hbGZvcm1lZCBoZWFkZXIgY29zdHMgbm90aGluZywgd2hpY2hcclxuICAgICAgLy8gcmVtb3ZlcyBib3RoIGEgRG9TIHZlY3RvciBhbmQgYSB3YXkgdG8gcnVuIHVwIGEgYmlsbC5cclxuICAgICAgdmFsaWRhdGlvblJlZ2V4OiAnXkJlYXJlciBbQS1aYS16MC05Xy1dezMyLH0kJyxcclxuICAgICAgcmVzdWx0c0NhY2hlVHRsOiBEdXJhdGlvbi5taW51dGVzKDUpLFxyXG4gICAgfSk7XHJcblxyXG4gICAgLy8gLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLSBxdWVyeSBsYW1iZGFcclxuXHJcbiAgICB0aGlzLnF1ZXJ5RnVuY3Rpb24gPSBuZXcgTGFtYmRhRnVuY3Rpb24odGhpcywgJ1F1ZXJ5Jywge1xyXG4gICAgICBmdW5jdGlvbk5hbWU6IGAke25hbWVQcmVmaXh9LXF1ZXJ5YCxcclxuICAgICAgZGVzY3JpcHRpb246ICdSZXRyaWV2YWwsIGdyb3VuZGVkIGdlbmVyYXRpb24sIGNpdGF0aW9uIHZlcmlmaWNhdGlvbiBhbmQgY29uZmlkZW5jZScsXHJcbiAgICAgIHJ1bnRpbWU6IFJ1bnRpbWUuUFlUSE9OXzNfMTIsXHJcbiAgICAgIGhhbmRsZXI6ICdoYW5kbGVyLmxhbWJkYV9oYW5kbGVyJyxcclxuICAgICAgY29kZTogQ29kZS5mcm9tQXNzZXQocGF0aC5qb2luKF9fZGlybmFtZSwgJy4uJywgJy4uJywgJy4uJywgJ3NlcnZpY2VzJywgJ3F1ZXJ5JykpLFxyXG4gICAgICAvLyBHZW5lcm91cyBiZWNhdXNlIHRoZSBwcm92aWRlciBpcyBvZmYtQVdTIGFuZCBzbG93OiB+NjI1IG1zIHRvIGVtYmVkIGFuZCBzZWNvbmRzIHRvXHJcbiAgICAgIC8vIGdlbmVyYXRlLiBBUEkgR2F0ZXdheSBjYXBzIHRoZSB3aG9sZSByZXF1ZXN0IGF0IDI5IHMgcmVnYXJkbGVzcy5cclxuICAgICAgdGltZW91dDogRHVyYXRpb24uc2Vjb25kcygyOCksXHJcbiAgICAgIG1lbW9yeVNpemU6IDEwMjQsXHJcbiAgICAgIC8vIE9ubHkgd2hlbiB0aGUgYWNjb3VudCBjYW4gaG9ub3VyIGl0IC0tIHNlZSB0aGUgbm90ZSBpbiBjb25maWcudHMuIEEgZnJlc2ggQVdTXHJcbiAgICAgIC8vIGFjY291bnQgY2Fubm90LCBhbmQgZmFpbGluZyB0aGUgd2hvbGUgZGVwbG95bWVudCBvdmVyIGEgZGVmZW5jZS1pbi1kZXB0aCBjb250cm9sXHJcbiAgICAgIC8vIHdvdWxkIGJlIHRoZSB3cm9uZyB0cmFkZS4gU3RhZ2UgdGhyb3R0bGluZyBiZWxvdyBpcyB0aGUgY2VpbGluZyB0aGF0IGFsd2F5cyBhcHBsaWVzLlxyXG4gICAgICByZXNlcnZlZENvbmN1cnJlbnRFeGVjdXRpb25zOiBjb25maWcucmVzZXJ2ZWRDb25jdXJyZW5jeSxcclxuICAgICAgbG9nR3JvdXA6IG5ldyBMb2dHcm91cCh0aGlzLCAnUXVlcnlMb2dzJywge1xyXG4gICAgICAgIGxvZ0dyb3VwTmFtZTogYC9hd3MvbGFtYmRhLyR7bmFtZVByZWZpeH0tcXVlcnlgLFxyXG4gICAgICAgIHJldGVudGlvbjogY29uZmlnLmxvZ1JldGVudGlvbixcclxuICAgICAgICByZW1vdmFsUG9saWN5OiBjb25maWcucmVtb3ZhbFBvbGljeSxcclxuICAgICAgfSksXHJcbiAgICAgIHRyYWNpbmc6IFRyYWNpbmcuQUNUSVZFLFxyXG4gICAgICBlbnZpcm9ubWVudDoge1xyXG4gICAgICAgIEtCX0JVQ0tFVDogYnVja2V0LmJ1Y2tldE5hbWUsXHJcbiAgICAgICAgS0JfSU5ERVhfS0VZOiBwcm9wcy5pbmRleEtleSxcclxuICAgICAgICBRVUVSWV9MT0dfVEFCTEU6IHF1ZXJ5TG9nVGFibGUudGFibGVOYW1lLFxyXG4gICAgICAgIFBST1ZJREVSX0FQSV9LRVlfU0VDUkVUX0FSTjogcHJvdmlkZXJBcGlLZXlTZWNyZXQuc2VjcmV0QXJuLFxyXG4gICAgICAgIE1PREVMX1BST1ZJREVSOiBtb2RlbHMucHJvdmlkZXIsXHJcbiAgICAgICAgTU9ERUxfSUQ6IG1vZGVscy5nZW5lcmF0aW9uTW9kZWwsXHJcbiAgICAgICAgRU1CRURfTU9ERUxfSUQ6IG1vZGVscy5lbWJlZGRpbmdNb2RlbCxcclxuICAgICAgICBFTUJFRF9ESU1FTlNJT05TOiBTdHJpbmcobW9kZWxzLmVtYmVkZGluZ0RpbWVuc2lvbnMpLFxyXG4gICAgICAgIExPR19RVUVTVElPTlM6IFN0cmluZyhjb25maWcubG9nUXVlc3Rpb25zKSxcclxuICAgICAgICBFTUlUX01FVFJJQ1M6IFN0cmluZyhjb25maWcuZW1pdE1ldHJpY3MpLFxyXG4gICAgICAgIFFVRVJZX0xPR19UVExfREFZUzogU3RyaW5nKGNvbmZpZy5xdWVyeUxvZ1R0bC50b0RheXMoKSksXHJcbiAgICAgIH0sXHJcbiAgICB9KTtcclxuXHJcbiAgICAvLyBSZWFkIHRoZSBpbmRleCwgbmV2ZXIgd3JpdGUgaXQuIFdyaXRpbmcgaXMgdGhlIHNlZWRlcidzIGpvYiwgYW5kIGEgcXVlcnkgcGF0aCB0aGF0XHJcbiAgICAvLyBjYW5ub3QgY29ycnVwdCB0aGUga25vd2xlZGdlIGJhc2UgaXMgb25lIGxlc3MgdGhpbmcgdG8gcmVhc29uIGFib3V0LlxyXG4gICAgYnVja2V0LmdyYW50UmVhZCh0aGlzLnF1ZXJ5RnVuY3Rpb24sICdpbmRleC8qJyk7XHJcbiAgICBxdWVyeUxvZ1RhYmxlLmdyYW50V3JpdGVEYXRhKHRoaXMucXVlcnlGdW5jdGlvbik7XHJcbiAgICBwcm92aWRlckFwaUtleVNlY3JldC5ncmFudFJlYWQodGhpcy5xdWVyeUZ1bmN0aW9uKTtcclxuICAgIC8vIE5vdGUgd2hhdCBpcyBhYnNlbnQ6IHRoZSBxdWVyeSByb2xlIGNhbm5vdCByZWFkIHRoZSBBUEkgdG9rZW4gc2VjcmV0LCBhbmQgdGhlXHJcbiAgICAvLyBhdXRob3JpemVyIGNhbm5vdCByZWFkIHRoZSBwcm92aWRlciBrZXkuIE5laXRoZXIgY2FuIGltcGVyc29uYXRlIHRoZSBvdGhlci5cclxuXHJcbiAgICAvLyAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tIHRoZSBhcGlcclxuXHJcbiAgICBjb25zdCBhY2Nlc3NMb2dHcm91cCA9IG5ldyBMb2dHcm91cCh0aGlzLCAnQWNjZXNzTG9ncycsIHtcclxuICAgICAgbG9nR3JvdXBOYW1lOiBgL2F3cy9hcGlnYXRld2F5LyR7bmFtZVByZWZpeH1gLFxyXG4gICAgICByZXRlbnRpb246IGNvbmZpZy5sb2dSZXRlbnRpb24sXHJcbiAgICAgIHJlbW92YWxQb2xpY3k6IGNvbmZpZy5yZW1vdmFsUG9saWN5LFxyXG4gICAgfSk7XHJcblxyXG4gICAgdGhpcy5hcGkgPSBuZXcgUmVzdEFwaSh0aGlzLCAnQXBpJywge1xyXG4gICAgICByZXN0QXBpTmFtZTogbmFtZVByZWZpeCxcclxuICAgICAgZGVzY3JpcHRpb246ICdBV1MtbmF0aXZlIEtub3dsZWRnZSBCYXNlIEFnZW50IChSQUcpJyxcclxuICAgICAgLy8gUmVxdWlyZWQgZm9yIGFjY2VzcyBsb2dnaW5nIHRvIHdvcmsgYXQgYWxsIG9uIGFuIGFjY291bnQgdGhhdCBoYXMgbmV2ZXIgaGFkIGl0IHNldC5cclxuICAgICAgY2xvdWRXYXRjaFJvbGU6IHByb3BzLmNsb3VkV2F0Y2hSb2xlLFxyXG4gICAgICAvLyBSRVRBSU4gYmVjYXVzZSBpdCBpcyBhbiBhY2NvdW50LXdpZGUgc2luZ2xldG9uOiBkZXN0cm95aW5nIHRoaXMgc3RhY2sgbXVzdCBub3RcclxuICAgICAgLy8gc2lsZW50bHkgZGlzYWJsZSBBUEkgR2F0ZXdheSBsb2dnaW5nIGZvciBhbnl0aGluZyBlbHNlIGluIHRoZSBhY2NvdW50LlxyXG4gICAgICBjbG91ZFdhdGNoUm9sZVJlbW92YWxQb2xpY3k6IFJlbW92YWxQb2xpY3kuUkVUQUlOLFxyXG4gICAgICBkZXBsb3lPcHRpb25zOiB7XHJcbiAgICAgICAgc3RhZ2VOYW1lOiBjb25maWcuZW52TmFtZSxcclxuICAgICAgICAvLyBTYW1lIHJlcXVlc3QgaWQgdGhhdCBhcHBlYXJzIGluIHRoZSBMYW1iZGEgbG9nLCB0aGUgWC1SYXkgdHJhY2UsIHRoZSBEeW5hbW9EQlxyXG4gICAgICAgIC8vIGl0ZW0gYW5kIHRoZSBKU09OIHRoZSBjYWxsZXIgcmVjZWl2ZXMuIE9uZSBpZCwgZml2ZSBwbGFjZXMuXHJcbiAgICAgICAgYWNjZXNzTG9nRGVzdGluYXRpb246IG5ldyBMb2dHcm91cExvZ0Rlc3RpbmF0aW9uKGFjY2Vzc0xvZ0dyb3VwKSxcclxuICAgICAgICBhY2Nlc3NMb2dGb3JtYXQ6IEFjY2Vzc0xvZ0Zvcm1hdC5jdXN0b20oXHJcbiAgICAgICAgICBKU09OLnN0cmluZ2lmeSh7XHJcbiAgICAgICAgICAgIHJlcXVlc3RJZDogJyRjb250ZXh0LnJlcXVlc3RJZCcsXHJcbiAgICAgICAgICAgIGlwOiAnJGNvbnRleHQuaWRlbnRpdHkuc291cmNlSXAnLFxyXG4gICAgICAgICAgICBtZXRob2Q6ICckY29udGV4dC5odHRwTWV0aG9kJyxcclxuICAgICAgICAgICAgcGF0aDogJyRjb250ZXh0LnBhdGgnLFxyXG4gICAgICAgICAgICBzdGF0dXM6ICckY29udGV4dC5zdGF0dXMnLFxyXG4gICAgICAgICAgICBsYXRlbmN5OiAnJGNvbnRleHQucmVzcG9uc2VMYXRlbmN5JyxcclxuICAgICAgICAgICAgcHJpbmNpcGFsOiAnJGNvbnRleHQuYXV0aG9yaXplci5wcmluY2lwYWxJZCcsXHJcbiAgICAgICAgICAgIHRva2VuRmluZ2VycHJpbnQ6ICckY29udGV4dC5hdXRob3JpemVyLnRva2VuRmluZ2VycHJpbnQnLFxyXG4gICAgICAgICAgICBhdXRob3JpemVyRXJyb3I6ICckY29udGV4dC5hdXRob3JpemVyLmVycm9yJyxcclxuICAgICAgICAgIH0pLFxyXG4gICAgICAgICksXHJcbiAgICAgICAgbG9nZ2luZ0xldmVsOiBNZXRob2RMb2dnaW5nTGV2ZWwuRVJST1IsXHJcbiAgICAgICAgLy8gT2ZmIGRlbGliZXJhdGVseTogZGF0YSB0cmFjaW5nIHdyaXRlcyByZXF1ZXN0IGFuZCByZXNwb25zZSBib2RpZXMgaW50b1xyXG4gICAgICAgIC8vIENsb3VkV2F0Y2gsIHdoaWNoIGZvciB0aGlzIEFQSSBtZWFucyB1c2VyIHF1ZXN0aW9ucyBhbmQgcmV0cmlldmVkIGRvY3VtZW50cy5cclxuICAgICAgICBkYXRhVHJhY2VFbmFibGVkOiBmYWxzZSxcclxuICAgICAgICBtZXRyaWNzRW5hYmxlZDogdHJ1ZSxcclxuICAgICAgICB0cmFjaW5nRW5hYmxlZDogdHJ1ZSxcclxuICAgICAgICB0aHJvdHRsaW5nUmF0ZUxpbWl0OiBjb25maWcudGhyb3R0bGVSYXRlTGltaXQsXHJcbiAgICAgICAgdGhyb3R0bGluZ0J1cnN0TGltaXQ6IGNvbmZpZy50aHJvdHRsZUJ1cnN0TGltaXQsXHJcbiAgICAgIH0sXHJcbiAgICAgIGRlZmF1bHRDb3JzUHJlZmxpZ2h0T3B0aW9uczoge1xyXG4gICAgICAgIGFsbG93T3JpZ2luczogQ29ycy5BTExfT1JJR0lOUyxcclxuICAgICAgICBhbGxvd0hlYWRlcnM6IFsnQ29udGVudC1UeXBlJywgJ0F1dGhvcml6YXRpb24nXSxcclxuICAgICAgICBhbGxvd01ldGhvZHM6IFsnR0VUJywgJ1BPU1QnLCAnREVMRVRFJywgJ09QVElPTlMnXSxcclxuICAgICAgfSxcclxuICAgIH0pO1xyXG5cclxuICAgIC8vIFZhbGlkYXRlZCBhdCB0aGUgZ2F0ZXdheSwgc28gYSBtYWxmb3JtZWQgYm9keSBuZXZlciByZWFjaGVzIC0tIG9yIGJpbGxzIC0tIGNvbXB1dGUuXHJcbiAgICBjb25zdCBxdWVyeU1vZGVsID0gbmV3IE1vZGVsKHRoaXMsICdRdWVyeVJlcXVlc3RNb2RlbCcsIHtcclxuICAgICAgcmVzdEFwaTogdGhpcy5hcGksXHJcbiAgICAgIG1vZGVsTmFtZTogJ1F1ZXJ5UmVxdWVzdCcsXHJcbiAgICAgIGNvbnRlbnRUeXBlOiAnYXBwbGljYXRpb24vanNvbicsXHJcbiAgICAgIHNjaGVtYToge1xyXG4gICAgICAgIHR5cGU6IEpzb25TY2hlbWFUeXBlLk9CSkVDVCxcclxuICAgICAgICByZXF1aXJlZDogWydxdWVzdGlvbiddLFxyXG4gICAgICAgIHByb3BlcnRpZXM6IHtcclxuICAgICAgICAgIHF1ZXN0aW9uOiB7IHR5cGU6IEpzb25TY2hlbWFUeXBlLlNUUklORywgbWluTGVuZ3RoOiAzLCBtYXhMZW5ndGg6IDEwMDAgfSxcclxuICAgICAgICAgIHNlc3Npb25faWQ6IHsgdHlwZTogSnNvblNjaGVtYVR5cGUuU1RSSU5HLCBtYXhMZW5ndGg6IDY0IH0sXHJcbiAgICAgICAgICB0b3BfazogeyB0eXBlOiBKc29uU2NoZW1hVHlwZS5JTlRFR0VSLCBtaW5pbXVtOiAxLCBtYXhpbXVtOiAxMCB9LFxyXG4gICAgICAgICAgLy8gXCJzaW1wbGVcIiBpcyB0aGUgcmVmZXJlbmNlIHByb3RvdHlwZSdzIFwiRXhwbGFpbiBsaWtlIEknbSAxMFwiIHRvZ2dsZS4gRW51bWVyYXRlZFxyXG4gICAgICAgICAgLy8gYXQgdGhlIGdhdGV3YXkgc28gYW4gaW52YWxpZCB2YWx1ZSBpcyByZWplY3RlZCBiZWZvcmUgaXQgcmVhY2hlcyBjb21wdXRlLlxyXG4gICAgICAgICAgc3R5bGU6IHsgdHlwZTogSnNvblNjaGVtYVR5cGUuU1RSSU5HLCBlbnVtOiBbJ3N0YW5kYXJkJywgJ3NpbXBsZSddIH0sXHJcbiAgICAgICAgfSxcclxuICAgICAgfSxcclxuICAgIH0pO1xyXG5cclxuICAgIC8vIC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0gZG9jdW1lbnRzIGZ1bmN0aW9uXHJcbiAgICAvL1xyXG4gICAgLy8gSXRzIG93biBmdW5jdGlvbiBhbmQgaXRzIG93biByb2xlLCByYXRoZXIgdGhhbiBtb3JlIHBlcm1pc3Npb25zIG9uIHRoZSBvbmUgYWJvdmUuXHJcbiAgICAvLyBMaXN0aW5nIGFuZCBkZWxldGluZyBuZWVkIHdyaXRlIGFjY2VzcyB0byBgcmF3L2A7IGFuc3dlcmluZyBxdWVzdGlvbnMgbXVzdCBuZXZlciBoYXZlXHJcbiAgICAvLyBpdC4gS2VlcGluZyB0aGVtIGFwYXJ0IGlzIHdoYXQgbGV0cyB0aGUgcXVlcnkgcm9sZSBzdGF5IHJlYWQtb25seSBvbiBgaW5kZXgvKmAsIHNvIGFcclxuICAgIC8vIGJ1ZyBvbiB0aGUgcXVlcnkgcGF0aCBjYW5ub3QgcmVhY2ggdGhlIGNvcnB1cyBubyBtYXR0ZXIgd2hhdCBpdCBkb2VzLlxyXG4gICAgdGhpcy5kb2N1bWVudHNGdW5jdGlvbiA9IG5ldyBMYW1iZGFGdW5jdGlvbih0aGlzLCAnRG9jdW1lbnRzJywge1xyXG4gICAgICBmdW5jdGlvbk5hbWU6IGAke25hbWVQcmVmaXh9LWRvY3VtZW50c2AsXHJcbiAgICAgIGRlc2NyaXB0aW9uOiAnTGlzdHMgYW5kIGRlbGV0ZXMga25vd2xlZGdlIGJhc2UgZG9jdW1lbnRzLCBhbmQgdHJpZ2dlcnMgcmVpbmRleGluZycsXHJcbiAgICAgIHJ1bnRpbWU6IFJ1bnRpbWUuUFlUSE9OXzNfMTIsXHJcbiAgICAgIGhhbmRsZXI6ICdoYW5kbGVyLmxhbWJkYV9oYW5kbGVyJyxcclxuICAgICAgY29kZTogQ29kZS5mcm9tQXNzZXQocGF0aC5qb2luKF9fZGlybmFtZSwgJy4uJywgJy4uJywgJy4uJywgJ3NlcnZpY2VzJywgJ2RvY3VtZW50cycpKSxcclxuICAgICAgLy8gR2VuZXJvdXMgb25seSBhZ2FpbnN0IGEgY29sZCBzdGFydCBwbHVzIHJlYWRpbmcgYSBjb21wcmVzc2VkIGluZGV4OyB0aGUgcmVpbmRleCBpdFxyXG4gICAgICAvLyBzdGFydHMgaXMgYXN5bmNocm9ub3VzIGFuZCBvdXRsaXZlcyB0aGlzIGludm9jYXRpb24gZW50aXJlbHkuXHJcbiAgICAgIHRpbWVvdXQ6IER1cmF0aW9uLnNlY29uZHMoMzApLFxyXG4gICAgICBtZW1vcnlTaXplOiA1MTIsXHJcbiAgICAgIHRyYWNpbmc6IFRyYWNpbmcuQUNUSVZFLFxyXG4gICAgICBsb2dHcm91cDogbmV3IExvZ0dyb3VwKHRoaXMsICdEb2N1bWVudHNMb2dzJywge1xyXG4gICAgICAgIGxvZ0dyb3VwTmFtZTogYC9hd3MvbGFtYmRhLyR7bmFtZVByZWZpeH0tZG9jdW1lbnRzYCxcclxuICAgICAgICByZXRlbnRpb246IGNvbmZpZy5sb2dSZXRlbnRpb24sXHJcbiAgICAgICAgcmVtb3ZhbFBvbGljeTogY29uZmlnLnJlbW92YWxQb2xpY3ksXHJcbiAgICAgIH0pLFxyXG4gICAgICBlbnZpcm9ubWVudDoge1xyXG4gICAgICAgIEtCX0JVQ0tFVDogYnVja2V0LmJ1Y2tldE5hbWUsXHJcbiAgICAgICAgS0JfSU5ERVhfS0VZOiBwcm9wcy5pbmRleEtleSxcclxuICAgICAgICBSQVdfUFJFRklYOiByYXdQcmVmaXgsXHJcbiAgICAgICAgSU5HRVNUX0ZVTkNUSU9OX05BTUU6IGluZ2VzdEZ1bmN0aW9uLmZ1bmN0aW9uTmFtZSxcclxuICAgICAgICBNQVhfVVBMT0FEX0JZVEVTOiBTdHJpbmcoY29uZmlnLm1heFVwbG9hZEJ5dGVzKSxcclxuICAgICAgfSxcclxuICAgIH0pO1xyXG5cclxuICAgIC8vIFJlYWQgdGhlIGluZGV4OyBsaXN0LCB3cml0ZSBhbmQgZGVsZXRlIHVuZGVyIGByYXcvYC4gVGhlIGFzeW1tZXRyeSB0aGF0IG1hdHRlcnMgaXNcclxuICAgIC8vIHRoZSBvbmUgYWdhaW5zdCBgaW5kZXgvYDogdGhpcyBmdW5jdGlvbiBtYW5hZ2VzIHNvdXJjZSBkb2N1bWVudHMgYW5kIGNhbm5vdCB0b3VjaCB0aGVcclxuICAgIC8vIGFydGlmYWN0IGFuc3dlcnMgY29tZSBmcm9tLCBzbyBub3RoaW5nIGl0IGRvZXMgY2FuIGZvcmdlIGEgc2VhcmNoYWJsZSBwYXNzYWdlLiBPbmx5XHJcbiAgICAvLyB0aGUgaW5nZXN0IHdyaXRlcyB0aGUgaW5kZXgsIGFuZCBvbmx5IGFmdGVyIHJlYWRpbmcgd2hhdCBpcyBhY3R1YWxseSBpbiB0aGUgYnVja2V0LlxyXG4gICAgYnVja2V0LmdyYW50UmVhZCh0aGlzLmRvY3VtZW50c0Z1bmN0aW9uLCBgJHtwcm9wcy5pbmRleEtleX1gKTtcclxuICAgIGJ1Y2tldC5ncmFudFJlYWQodGhpcy5kb2N1bWVudHNGdW5jdGlvbiwgYCR7cmF3UHJlZml4fSpgKTtcclxuICAgIGJ1Y2tldC5ncmFudFB1dCh0aGlzLmRvY3VtZW50c0Z1bmN0aW9uLCBgJHtyYXdQcmVmaXh9KmApO1xyXG4gICAgYnVja2V0LmdyYW50RGVsZXRlKHRoaXMuZG9jdW1lbnRzRnVuY3Rpb24sIGAke3Jhd1ByZWZpeH0qYCk7XHJcbiAgICBpbmdlc3RGdW5jdGlvbi5ncmFudEludm9rZSh0aGlzLmRvY3VtZW50c0Z1bmN0aW9uKTtcclxuXHJcbiAgICBjb25zdCBpbnRlZ3JhdGlvbiA9IG5ldyBMYW1iZGFJbnRlZ3JhdGlvbih0aGlzLnF1ZXJ5RnVuY3Rpb24sIHsgcHJveHk6IHRydWUgfSk7XHJcbiAgICBjb25zdCBkb2N1bWVudHNJbnRlZ3JhdGlvbiA9IG5ldyBMYW1iZGFJbnRlZ3JhdGlvbih0aGlzLmRvY3VtZW50c0Z1bmN0aW9uLCB7IHByb3h5OiB0cnVlIH0pO1xyXG4gICAgY29uc3QgYXV0aG9yaXplZCA9IHsgYXV0aG9yaXplciwgYXV0aG9yaXphdGlvblR5cGU6IEF1dGhvcml6YXRpb25UeXBlLkNVU1RPTSB9O1xyXG5cclxuICAgIC8vIE9uZSB2YWxpZGF0b3Igc2hhcmVkIGJ5IGV2ZXJ5IG1ldGhvZCB0aGF0IG5lZWRzIGl0LiBUd28gbWV0aG9kcyBlYWNoIGRlY2xhcmluZ1xyXG4gICAgLy8gYHJlcXVlc3RWYWxpZGF0b3JPcHRpb25zYCBtYWtlIENESyBtaW50IHR3byB2YWxpZGF0b3JzIHdpdGggdGhlIHNhbWUgZ2VuZXJhdGVkIGlkLFxyXG4gICAgLy8gd2hpY2ggY29sbGlkZXMgYXQgc3ludGguXHJcbiAgICBjb25zdCBib2R5VmFsaWRhdG9yID0gbmV3IFJlcXVlc3RWYWxpZGF0b3IodGhpcywgJ0JvZHlWYWxpZGF0b3InLCB7XHJcbiAgICAgIHJlc3RBcGk6IHRoaXMuYXBpLFxyXG4gICAgICByZXF1ZXN0VmFsaWRhdG9yTmFtZTogYCR7bmFtZVByZWZpeH0tYm9keWAsXHJcbiAgICAgIHZhbGlkYXRlUmVxdWVzdEJvZHk6IHRydWUsXHJcbiAgICAgIHZhbGlkYXRlUmVxdWVzdFBhcmFtZXRlcnM6IGZhbHNlLFxyXG4gICAgfSk7XHJcblxyXG4gICAgLy8gL2hlYWx0aCBpcyBhdXRoZW50aWNhdGVkIHRvby4gVGhlIGJyaWVmIHNheXMgdGhlIEFQSSBtdXN0IG5vdCBiZSBwdWJsaWNseSBjYWxsYWJsZSxcclxuICAgIC8vIGFuZCB0YWtpbmcgdGhhdCBsaXRlcmFsbHkgaGFzIGEgdXNlZnVsIHNpZGUgZWZmZWN0OiBgY3VybCAvaGVhbHRoYCB3aXRob3V0IGEgdG9rZW5cclxuICAgIC8vIHJldHVybmluZyA0MDEgaXMgdGhlIGNsZWFuZXN0IHBvc3NpYmxlIGRlbW9uc3RyYXRpb24gdGhhdCBhdXRoIHdvcmtzLlxyXG4gICAgdGhpcy5hcGkucm9vdC5hZGRSZXNvdXJjZSgnaGVhbHRoJykuYWRkTWV0aG9kKCdHRVQnLCBpbnRlZ3JhdGlvbiwgYXV0aG9yaXplZCk7XHJcbiAgICB0aGlzLmFwaS5yb290LmFkZFJlc291cmNlKCdxdWVyeScpLmFkZE1ldGhvZCgnUE9TVCcsIGludGVncmF0aW9uLCB7XHJcbiAgICAgIC4uLmF1dGhvcml6ZWQsXHJcbiAgICAgIHJlcXVlc3RNb2RlbHM6IHsgJ2FwcGxpY2F0aW9uL2pzb24nOiBxdWVyeU1vZGVsIH0sXHJcbiAgICAgIHJlcXVlc3RWYWxpZGF0b3I6IGJvZHlWYWxpZGF0b3IsXHJcbiAgICB9KTtcclxuXHJcbiAgICAvLyBTYW1lIGF1dGhvcml6ZXIgYXMgZXZlcnl0aGluZyBlbHNlLiBBbiBlbmRwb2ludCB0aGF0IGRlbGV0ZXMgZG9jdW1lbnRzIGlzIHRoZSBsYXN0XHJcbiAgICAvLyBvbmUgdGhhdCBzaG91bGQgYmUgcmVhY2hhYmxlIHdpdGhvdXQgYSB0b2tlbi5cclxuICAgIGNvbnN0IHVwbG9hZE1vZGVsID0gbmV3IE1vZGVsKHRoaXMsICdVcGxvYWRSZXF1ZXN0TW9kZWwnLCB7XHJcbiAgICAgIHJlc3RBcGk6IHRoaXMuYXBpLFxyXG4gICAgICBtb2RlbE5hbWU6ICdVcGxvYWRSZXF1ZXN0JyxcclxuICAgICAgY29udGVudFR5cGU6ICdhcHBsaWNhdGlvbi9qc29uJyxcclxuICAgICAgc2NoZW1hOiB7XHJcbiAgICAgICAgdHlwZTogSnNvblNjaGVtYVR5cGUuT0JKRUNULFxyXG4gICAgICAgIHJlcXVpcmVkOiBbJ2ZpbGVuYW1lJ10sXHJcbiAgICAgICAgcHJvcGVydGllczoge1xyXG4gICAgICAgICAgLy8gU2hhcGUgb25seS4gV2hpY2ggZXh0ZW5zaW9ucyB0aGUgaW5nZXN0IGNhbiBhY3R1YWxseSByZWFkIGlzIGVuZm9yY2VkIGluIHRoZVxyXG4gICAgICAgICAgLy8gTGFtYmRhLCBuZXh0IHRvIHRoZSBsaXN0IHRoYXQgZGVjaWRlcyBpdCwgcmF0aGVyIHRoYW4gZHVwbGljYXRlZCBoZXJlIHdoZXJlIHRoZVxyXG4gICAgICAgICAgLy8gdHdvIGNvcGllcyB3b3VsZCBkcmlmdC5cclxuICAgICAgICAgIGZpbGVuYW1lOiB7IHR5cGU6IEpzb25TY2hlbWFUeXBlLlNUUklORywgbWluTGVuZ3RoOiAzLCBtYXhMZW5ndGg6IDIwMCB9LFxyXG4gICAgICAgIH0sXHJcbiAgICAgIH0sXHJcbiAgICB9KTtcclxuXHJcbiAgICBjb25zdCBkb2N1bWVudHMgPSB0aGlzLmFwaS5yb290LmFkZFJlc291cmNlKCdkb2N1bWVudHMnKTtcclxuICAgIGRvY3VtZW50cy5hZGRNZXRob2QoJ0dFVCcsIGRvY3VtZW50c0ludGVncmF0aW9uLCBhdXRob3JpemVkKTtcclxuICAgIGRvY3VtZW50cy5hZGRNZXRob2QoJ1BPU1QnLCBkb2N1bWVudHNJbnRlZ3JhdGlvbiwge1xyXG4gICAgICAuLi5hdXRob3JpemVkLFxyXG4gICAgICByZXF1ZXN0TW9kZWxzOiB7ICdhcHBsaWNhdGlvbi9qc29uJzogdXBsb2FkTW9kZWwgfSxcclxuICAgICAgcmVxdWVzdFZhbGlkYXRvcjogYm9keVZhbGlkYXRvcixcclxuICAgIH0pO1xyXG4gICAgZG9jdW1lbnRzLmFkZFJlc291cmNlKCd7ZG9jdW1lbnRJZH0nKS5hZGRNZXRob2QoJ0RFTEVURScsIGRvY3VtZW50c0ludGVncmF0aW9uLCBhdXRob3JpemVkKTtcclxuXHJcbiAgICB0aGlzLmFkZEVycm9yRW52ZWxvcGVzKCk7XHJcbiAgfVxyXG5cclxuICAvKipcclxuICAgKiBNYWtlIEFQSSBHYXRld2F5J3Mgb3duIGVycm9ycyBtYXRjaCB0aGUgZG9jdW1lbnRlZCBjb250cmFjdC5cclxuICAgKlxyXG4gICAqIFdpdGhvdXQgdGhpcywgYSBjYWxsZXIgc2VlcyBge2Vycm9yLCBtZXNzYWdlLCByZXF1ZXN0X2lkfWAgZnJvbSB0aGUgTGFtYmRhIGFuZCBhIGJhcmVcclxuICAgKiBge1wibWVzc2FnZVwiOlwiRm9yYmlkZGVuXCJ9YCBmcm9tIHRoZSBnYXRld2F5LCBhbmQgaGFzIHRvIGhhbmRsZSB0d28gc2hhcGVzLiBUaGlzIGlzIHRoZVxyXG4gICAqIGNhcGFiaWxpdHkgdGhhdCBSRVNUIEFQSSBoYXMgYW5kIEhUVFAgQVBJIGRvZXMgbm90LCBhbmQgdGhlIHJlYXNvbiBmb3IgQURSLTAzLlxyXG4gICAqL1xyXG4gIHByaXZhdGUgYWRkRXJyb3JFbnZlbG9wZXMoKTogdm9pZCB7XHJcbiAgICBjb25zdCBlbnZlbG9wZSA9IChlcnJvcjogc3RyaW5nLCBtZXNzYWdlOiBzdHJpbmcpID0+ICh7XHJcbiAgICAgICdhcHBsaWNhdGlvbi9qc29uJzogYHtcImVycm9yXCI6XCIke2Vycm9yfVwiLFwibWVzc2FnZVwiOlwiJHttZXNzYWdlfVwiLFwicmVxdWVzdF9pZFwiOlwiJGNvbnRleHQucmVxdWVzdElkXCJ9YCxcclxuICAgIH0pO1xyXG5cclxuICAgIGNvbnN0IHJlc3BvbnNlczogQXJyYXk8W3N0cmluZywgUmVzcG9uc2VUeXBlLCBzdHJpbmcsIHN0cmluZywgc3RyaW5nXT4gPSBbXHJcbiAgICAgIFsnVW5hdXRob3JpemVkJywgUmVzcG9uc2VUeXBlLlVOQVVUSE9SSVpFRCwgJzQwMScsICd1bmF1dGhvcml6ZWQnLCAnTWlzc2luZyBvciBtYWxmb3JtZWQgYXV0aG9yaXphdGlvbiB0b2tlbi4nXSxcclxuICAgICAgWydBY2Nlc3NEZW5pZWQnLCBSZXNwb25zZVR5cGUuQUNDRVNTX0RFTklFRCwgJzQwMycsICdmb3JiaWRkZW4nLCAnVGhlIHByb3ZpZGVkIHRva2VuIGlzIG5vdCB2YWxpZC4nXSxcclxuICAgICAgWydCYWRSZXF1ZXN0Qm9keScsIFJlc3BvbnNlVHlwZS5CQURfUkVRVUVTVF9CT0RZLCAnNDAwJywgJ2JhZF9yZXF1ZXN0JywgJ1JlcXVlc3QgYm9keSBmYWlsZWQgdmFsaWRhdGlvbjogJGNvbnRleHQuZXJyb3IudmFsaWRhdGlvbkVycm9yU3RyaW5nJ10sXHJcbiAgICAgIFsnVGhyb3R0bGVkJywgUmVzcG9uc2VUeXBlLlRIUk9UVExFRCwgJzQyOScsICdyYXRlX2xpbWl0ZWQnLCAnVG9vIG1hbnkgcmVxdWVzdHMuIFJldHJ5IGFmdGVyIGEgc2hvcnQgZGVsYXkuJ10sXHJcbiAgICAgIFsnTm90Rm91bmQnLCBSZXNwb25zZVR5cGUuUkVTT1VSQ0VfTk9UX0ZPVU5ELCAnNDA0JywgJ25vdF9mb3VuZCcsICdObyBzdWNoIHJvdXRlLiddLFxyXG4gICAgICBbJ1NlcnZlckVycm9yJywgUmVzcG9uc2VUeXBlLkRFRkFVTFRfNVhYLCAnNTAwJywgJ2ludGVybmFsX2Vycm9yJywgJ0FuIHVuZXhwZWN0ZWQgZXJyb3Igb2NjdXJyZWQuJ10sXHJcbiAgICBdO1xyXG5cclxuICAgIGZvciAoY29uc3QgW2lkLCB0eXBlLCBzdGF0dXNDb2RlLCBlcnJvciwgbWVzc2FnZV0gb2YgcmVzcG9uc2VzKSB7XHJcbiAgICAgIHRoaXMuYXBpLmFkZEdhdGV3YXlSZXNwb25zZShgR2F0ZXdheVJlc3BvbnNlJHtpZH1gLCB7XHJcbiAgICAgICAgdHlwZSxcclxuICAgICAgICBzdGF0dXNDb2RlLFxyXG4gICAgICAgIHRlbXBsYXRlczogZW52ZWxvcGUoZXJyb3IsIG1lc3NhZ2UpLFxyXG4gICAgICB9KTtcclxuICAgIH1cclxuICB9XHJcblxyXG4gIHB1YmxpYyBnZXQgdG9rZW5Db21tYW5kKCk6IHN0cmluZyB7XHJcbiAgICByZXR1cm4gYGF3cyBzZWNyZXRzbWFuYWdlciBnZXQtc2VjcmV0LXZhbHVlIC0tc2VjcmV0LWlkICR7dGhpcy5hcGlUb2tlblNlY3JldC5zZWNyZXROYW1lfSAtLXF1ZXJ5IFNlY3JldFN0cmluZyAtLW91dHB1dCB0ZXh0YDtcclxuICB9XHJcbn1cclxuIl19