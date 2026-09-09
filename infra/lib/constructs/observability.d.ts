import { RestApi } from 'aws-cdk-lib/aws-apigateway';
import { Dashboard } from 'aws-cdk-lib/aws-cloudwatch';
import { Function as LambdaFunction } from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import { EnvironmentConfig } from '../config';
export interface ObservabilityProps {
    readonly config: EnvironmentConfig;
    readonly prefix: string;
    readonly api: RestApi;
    readonly queryFunction: LambdaFunction;
    /** Where alarms and budget alerts go. Without it, alarms still fire but notify nobody. */
    readonly alertEmail?: string;
}
/**
 * Dashboard, alarms and a cost guardrail.
 *
 * The operational half already exists elsewhere: structured JSON logs, X-Ray tracing and
 * API Gateway access logs are configured where the resources they describe are created.
 * What this adds is the part that answers questions without reading logs -- is it healthy,
 * is it accurate, and is it about to cost more than it should.
 *
 * The accuracy widgets are the interesting ones. Confidence and abstention rate are not
 * infrastructure metrics; they are the ones that would reveal the knowledge base going
 * stale, or a provider silently degrading, long before anything starts erroring.
 */
export declare class Observability extends Construct {
    readonly dashboard: Dashboard;
    constructor(scope: Construct, id: string, props: ObservabilityProps);
}
