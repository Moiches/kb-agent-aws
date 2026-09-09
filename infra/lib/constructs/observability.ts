import { Duration } from 'aws-cdk-lib';
import { RestApi } from 'aws-cdk-lib/aws-apigateway';
import { CfnBudget } from 'aws-cdk-lib/aws-budgets';
import {
  Alarm,
  ComparisonOperator,
  Dashboard,
  GraphWidget,
  LogQueryVisualizationType,
  LogQueryWidget,
  Metric,
  Stats,
  TextWidget,
  TreatMissingData,
} from 'aws-cdk-lib/aws-cloudwatch';
import { SnsAction } from 'aws-cdk-lib/aws-cloudwatch-actions';
import { Function as LambdaFunction } from 'aws-cdk-lib/aws-lambda';
import { Topic } from 'aws-cdk-lib/aws-sns';
import { EmailSubscription } from 'aws-cdk-lib/aws-sns-subscriptions';
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

/** Namespace the query Lambda publishes to via Embedded Metric Format. */
const METRIC_NAMESPACE = 'KbAgent';

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
export class Observability extends Construct {
  public readonly dashboard: Dashboard;

  constructor(scope: Construct, id: string, props: ObservabilityProps) {
    super(scope, id);

    const { config, prefix, api, queryFunction, alertEmail } = props;
    const namePrefix = `${prefix}-${config.envName}`;

    const topic = alertEmail ? new Topic(this, 'Alerts', { topicName: `${namePrefix}-alerts` }) : undefined;
    if (topic && alertEmail) {
      topic.addSubscription(new EmailSubscription(alertEmail));
    }

    const businessMetric = (metricName: string, statistic: string) =>
      new Metric({
        namespace: METRIC_NAMESPACE,
        metricName,
        statistic,
        period: Duration.minutes(5),
        dimensionsMap: { provider: 'openrouter' },
      });

    this.dashboard = new Dashboard(this, 'Dashboard', {
      dashboardName: namePrefix,
      defaultInterval: Duration.hours(3),
    });

    this.dashboard.addWidgets(
      new TextWidget({
        markdown: [
          `# ${namePrefix}`,
          '',
          'One `request_id` links every row below: the API Gateway access log, the Lambda',
          'structured log, the X-Ray trace, the DynamoDB query-log item, and the JSON the',
          'caller received. Start from a request id and the whole path is reconstructible.',
        ].join('\n'),
        width: 24,
        height: 3,
      }),
    );

    this.dashboard.addWidgets(
      new GraphWidget({
        title: 'API traffic and errors',
        left: [api.metricCount({ statistic: Stats.SUM, label: 'Requests' })],
        right: [
          api.metricClientError({ statistic: Stats.SUM, label: '4XX (mostly auth)' }),
          api.metricServerError({ statistic: Stats.SUM, label: '5XX' }),
        ],
        width: 12,
        height: 6,
      }),
      new GraphWidget({
        title: 'End-to-end latency',
        left: [
          api.metricLatency({ statistic: 'p50', label: 'p50' }),
          api.metricLatency({ statistic: 'p90', label: 'p90' }),
          api.metricLatency({ statistic: 'p99', label: 'p99' }),
        ],
        leftYAxis: { label: 'ms', showUnits: false },
        width: 12,
        height: 6,
      }),
    );

    this.dashboard.addWidgets(
      new GraphWidget({
        title: 'Query Lambda',
        left: [
          queryFunction.metricInvocations({ statistic: Stats.SUM, label: 'Invocations' }),
          queryFunction.metricErrors({ statistic: Stats.SUM, label: 'Errors' }),
          queryFunction.metricThrottles({ statistic: Stats.SUM, label: 'Throttles' }),
        ],
        right: [queryFunction.metricDuration({ statistic: 'p90', label: 'Duration p90' })],
        width: 12,
        height: 6,
      }),
      new GraphWidget({
        title: 'Answer quality',
        left: [
          businessMetric('QueryConfidence', Stats.AVERAGE),
          businessMetric('Abstention', Stats.AVERAGE),
        ],
        leftYAxis: { min: 0, max: 1, showUnits: false },
        width: 12,
        height: 6,
      }),
    );

    this.dashboard.addWidgets(
      new GraphWidget({
        title: 'Provider token spend',
        left: [
          businessMetric('ProviderInputTokens', Stats.SUM),
          businessMetric('ProviderOutputTokens', Stats.SUM),
        ],
        width: 12,
        height: 6,
      }),
      new LogQueryWidget({
        title: 'Recent errors and abstentions',
        logGroupNames: [`/aws/lambda/${namePrefix}-query`],
        view: LogQueryVisualizationType.TABLE,
        queryLines: [
          'fields @timestamp, message, correlation_id, error, grounding, confidence',
          'filter level = "ERROR" or abstained = 1',
          'sort @timestamp desc',
          'limit 20',
        ],
        width: 12,
        height: 6,
      }),
    );

    // ------------------------------------------------------------------- alarms

    const alarms = [
      new Alarm(this, 'ApiServerErrors', {
        alarmName: `${namePrefix}-api-5xx`,
        alarmDescription: 'The API is returning server errors. Something is broken, not merely rejected.',
        metric: api.metricServerError({ statistic: Stats.SUM, period: Duration.minutes(5) }),
        threshold: 3,
        evaluationPeriods: 1,
        comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
        // No traffic is not a failure; this system is idle most of the time.
        treatMissingData: TreatMissingData.NOT_BREACHING,
      }),

      new Alarm(this, 'QueryFunctionErrors', {
        alarmName: `${namePrefix}-query-errors`,
        alarmDescription: 'The query Lambda is throwing. Check the structured log for the request id.',
        metric: queryFunction.metricErrors({ statistic: Stats.SUM, period: Duration.minutes(5) }),
        threshold: 3,
        evaluationPeriods: 1,
        comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: TreatMissingData.NOT_BREACHING,
      }),

      new Alarm(this, 'LatencyDegraded', {
        alarmName: `${namePrefix}-latency-p99`,
        // Measured p50 is ~2.7 s, dominated by the off-AWS provider call. 10 s means the
        // provider is degrading or a retry storm is under way, not that we are slow.
        alarmDescription: 'p99 latency above 10s across two periods. Usually the model provider.',
        metric: api.metricLatency({ statistic: 'p99', period: Duration.minutes(5) }),
        threshold: 10_000,
        evaluationPeriods: 2,
        comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: TreatMissingData.NOT_BREACHING,
      }),
    ];

    if (topic) {
      for (const alarm of alarms) {
        alarm.addAlarmAction(new SnsAction(topic));
      }
    }

    // ------------------------------------------------------------------- budget

    // The first two budgets per account are free. Without a subscriber a budget cannot
    // notify anyone, so it is only created when there is somewhere to send the alert.
    if (config.budgetLimitUsd && alertEmail) {
      new CfnBudget(this, 'Budget', {
        budget: {
          budgetName: `${namePrefix}-monthly`,
          budgetType: 'COST',
          timeUnit: 'MONTHLY',
          budgetLimit: { amount: config.budgetLimitUsd, unit: 'USD' },
        },
        // Three thresholds rather than one: the point is to notice a trend early enough to
        // act, not to be told after the limit is already gone.
        notificationsWithSubscribers: [25, 50, 75].map((threshold) => ({
          notification: {
            notificationType: 'ACTUAL',
            comparisonOperator: 'GREATER_THAN',
            threshold,
            thresholdType: 'PERCENTAGE',
          },
          subscribers: [{ subscriptionType: 'EMAIL', address: alertEmail }],
        })),
      });
    }
  }
}
