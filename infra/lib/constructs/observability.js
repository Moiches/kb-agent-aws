"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.Observability = void 0;
const aws_cdk_lib_1 = require("aws-cdk-lib");
const aws_budgets_1 = require("aws-cdk-lib/aws-budgets");
const aws_cloudwatch_1 = require("aws-cdk-lib/aws-cloudwatch");
const aws_cloudwatch_actions_1 = require("aws-cdk-lib/aws-cloudwatch-actions");
const aws_sns_1 = require("aws-cdk-lib/aws-sns");
const aws_sns_subscriptions_1 = require("aws-cdk-lib/aws-sns-subscriptions");
const constructs_1 = require("constructs");
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
class Observability extends constructs_1.Construct {
    dashboard;
    constructor(scope, id, props) {
        super(scope, id);
        const { config, prefix, api, queryFunction, alertEmail } = props;
        const namePrefix = `${prefix}-${config.envName}`;
        const topic = alertEmail ? new aws_sns_1.Topic(this, 'Alerts', { topicName: `${namePrefix}-alerts` }) : undefined;
        if (topic && alertEmail) {
            topic.addSubscription(new aws_sns_subscriptions_1.EmailSubscription(alertEmail));
        }
        const businessMetric = (metricName, statistic) => new aws_cloudwatch_1.Metric({
            namespace: METRIC_NAMESPACE,
            metricName,
            statistic,
            period: aws_cdk_lib_1.Duration.minutes(5),
            dimensionsMap: { provider: 'openrouter' },
        });
        this.dashboard = new aws_cloudwatch_1.Dashboard(this, 'Dashboard', {
            dashboardName: namePrefix,
            defaultInterval: aws_cdk_lib_1.Duration.hours(3),
        });
        this.dashboard.addWidgets(new aws_cloudwatch_1.TextWidget({
            markdown: [
                `# ${namePrefix}`,
                '',
                'One `request_id` links every row below: the API Gateway access log, the Lambda',
                'structured log, the X-Ray trace, the DynamoDB query-log item, and the JSON the',
                'caller received. Start from a request id and the whole path is reconstructible.',
            ].join('\n'),
            width: 24,
            height: 3,
        }));
        this.dashboard.addWidgets(new aws_cloudwatch_1.GraphWidget({
            title: 'API traffic and errors',
            left: [api.metricCount({ statistic: aws_cloudwatch_1.Stats.SUM, label: 'Requests' })],
            right: [
                api.metricClientError({ statistic: aws_cloudwatch_1.Stats.SUM, label: '4XX (mostly auth)' }),
                api.metricServerError({ statistic: aws_cloudwatch_1.Stats.SUM, label: '5XX' }),
            ],
            width: 12,
            height: 6,
        }), new aws_cloudwatch_1.GraphWidget({
            title: 'End-to-end latency',
            left: [
                api.metricLatency({ statistic: 'p50', label: 'p50' }),
                api.metricLatency({ statistic: 'p90', label: 'p90' }),
                api.metricLatency({ statistic: 'p99', label: 'p99' }),
            ],
            leftYAxis: { label: 'ms', showUnits: false },
            width: 12,
            height: 6,
        }));
        this.dashboard.addWidgets(new aws_cloudwatch_1.GraphWidget({
            title: 'Query Lambda',
            left: [
                queryFunction.metricInvocations({ statistic: aws_cloudwatch_1.Stats.SUM, label: 'Invocations' }),
                queryFunction.metricErrors({ statistic: aws_cloudwatch_1.Stats.SUM, label: 'Errors' }),
                queryFunction.metricThrottles({ statistic: aws_cloudwatch_1.Stats.SUM, label: 'Throttles' }),
            ],
            right: [queryFunction.metricDuration({ statistic: 'p90', label: 'Duration p90' })],
            width: 12,
            height: 6,
        }), new aws_cloudwatch_1.GraphWidget({
            title: 'Answer quality',
            left: [
                businessMetric('QueryConfidence', aws_cloudwatch_1.Stats.AVERAGE),
                businessMetric('Abstention', aws_cloudwatch_1.Stats.AVERAGE),
            ],
            leftYAxis: { min: 0, max: 1, showUnits: false },
            width: 12,
            height: 6,
        }));
        this.dashboard.addWidgets(new aws_cloudwatch_1.GraphWidget({
            title: 'Provider token spend',
            left: [
                businessMetric('ProviderInputTokens', aws_cloudwatch_1.Stats.SUM),
                businessMetric('ProviderOutputTokens', aws_cloudwatch_1.Stats.SUM),
            ],
            width: 12,
            height: 6,
        }), new aws_cloudwatch_1.LogQueryWidget({
            title: 'Recent errors and abstentions',
            logGroupNames: [`/aws/lambda/${namePrefix}-query`],
            view: aws_cloudwatch_1.LogQueryVisualizationType.TABLE,
            queryLines: [
                'fields @timestamp, message, correlation_id, error, grounding, confidence',
                'filter level = "ERROR" or abstained = 1',
                'sort @timestamp desc',
                'limit 20',
            ],
            width: 12,
            height: 6,
        }));
        // ------------------------------------------------------------------- alarms
        const alarms = [
            new aws_cloudwatch_1.Alarm(this, 'ApiServerErrors', {
                alarmName: `${namePrefix}-api-5xx`,
                alarmDescription: 'The API is returning server errors. Something is broken, not merely rejected.',
                metric: api.metricServerError({ statistic: aws_cloudwatch_1.Stats.SUM, period: aws_cdk_lib_1.Duration.minutes(5) }),
                threshold: 3,
                evaluationPeriods: 1,
                comparisonOperator: aws_cloudwatch_1.ComparisonOperator.GREATER_THAN_THRESHOLD,
                // No traffic is not a failure; this system is idle most of the time.
                treatMissingData: aws_cloudwatch_1.TreatMissingData.NOT_BREACHING,
            }),
            new aws_cloudwatch_1.Alarm(this, 'QueryFunctionErrors', {
                alarmName: `${namePrefix}-query-errors`,
                alarmDescription: 'The query Lambda is throwing. Check the structured log for the request id.',
                metric: queryFunction.metricErrors({ statistic: aws_cloudwatch_1.Stats.SUM, period: aws_cdk_lib_1.Duration.minutes(5) }),
                threshold: 3,
                evaluationPeriods: 1,
                comparisonOperator: aws_cloudwatch_1.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treatMissingData: aws_cloudwatch_1.TreatMissingData.NOT_BREACHING,
            }),
            new aws_cloudwatch_1.Alarm(this, 'LatencyDegraded', {
                alarmName: `${namePrefix}-latency-p99`,
                // Measured p50 is ~2.7 s, dominated by the off-AWS provider call. 10 s means the
                // provider is degrading or a retry storm is under way, not that we are slow.
                alarmDescription: 'p99 latency above 10s across two periods. Usually the model provider.',
                metric: api.metricLatency({ statistic: 'p99', period: aws_cdk_lib_1.Duration.minutes(5) }),
                threshold: 10_000,
                evaluationPeriods: 2,
                comparisonOperator: aws_cloudwatch_1.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treatMissingData: aws_cloudwatch_1.TreatMissingData.NOT_BREACHING,
            }),
        ];
        if (topic) {
            for (const alarm of alarms) {
                alarm.addAlarmAction(new aws_cloudwatch_actions_1.SnsAction(topic));
            }
        }
        // ------------------------------------------------------------------- budget
        // The first two budgets per account are free. Without a subscriber a budget cannot
        // notify anyone, so it is only created when there is somewhere to send the alert.
        if (config.budgetLimitUsd && alertEmail) {
            new aws_budgets_1.CfnBudget(this, 'Budget', {
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
exports.Observability = Observability;
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoib2JzZXJ2YWJpbGl0eS5qcyIsInNvdXJjZVJvb3QiOiIiLCJzb3VyY2VzIjpbIm9ic2VydmFiaWxpdHkudHMiXSwibmFtZXMiOltdLCJtYXBwaW5ncyI6Ijs7O0FBQUEsNkNBQXVDO0FBRXZDLHlEQUFvRDtBQUNwRCwrREFXb0M7QUFDcEMsK0VBQStEO0FBRS9ELGlEQUE0QztBQUM1Qyw2RUFBc0U7QUFDdEUsMkNBQXVDO0FBWXZDLDBFQUEwRTtBQUMxRSxNQUFNLGdCQUFnQixHQUFHLFNBQVMsQ0FBQztBQUVuQzs7Ozs7Ozs7Ozs7R0FXRztBQUNILE1BQWEsYUFBYyxTQUFRLHNCQUFTO0lBQzFCLFNBQVMsQ0FBWTtJQUVyQyxZQUFZLEtBQWdCLEVBQUUsRUFBVSxFQUFFLEtBQXlCO1FBQ2pFLEtBQUssQ0FBQyxLQUFLLEVBQUUsRUFBRSxDQUFDLENBQUM7UUFFakIsTUFBTSxFQUFFLE1BQU0sRUFBRSxNQUFNLEVBQUUsR0FBRyxFQUFFLGFBQWEsRUFBRSxVQUFVLEVBQUUsR0FBRyxLQUFLLENBQUM7UUFDakUsTUFBTSxVQUFVLEdBQUcsR0FBRyxNQUFNLElBQUksTUFBTSxDQUFDLE9BQU8sRUFBRSxDQUFDO1FBRWpELE1BQU0sS0FBSyxHQUFHLFVBQVUsQ0FBQyxDQUFDLENBQUMsSUFBSSxlQUFLLENBQUMsSUFBSSxFQUFFLFFBQVEsRUFBRSxFQUFFLFNBQVMsRUFBRSxHQUFHLFVBQVUsU0FBUyxFQUFFLENBQUMsQ0FBQyxDQUFDLENBQUMsU0FBUyxDQUFDO1FBQ3hHLElBQUksS0FBSyxJQUFJLFVBQVUsRUFBRSxDQUFDO1lBQ3hCLEtBQUssQ0FBQyxlQUFlLENBQUMsSUFBSSx5Q0FBaUIsQ0FBQyxVQUFVLENBQUMsQ0FBQyxDQUFDO1FBQzNELENBQUM7UUFFRCxNQUFNLGNBQWMsR0FBRyxDQUFDLFVBQWtCLEVBQUUsU0FBaUIsRUFBRSxFQUFFLENBQy9ELElBQUksdUJBQU0sQ0FBQztZQUNULFNBQVMsRUFBRSxnQkFBZ0I7WUFDM0IsVUFBVTtZQUNWLFNBQVM7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LE9BQU8sRUFBRSxVQUFVLEVBQUUsQ0FBQztpQkFDbEUsQ0FBQyxDQUFDO2FBQ0osQ0FBQyxDQUFDO1FBQ0wsQ0FBQztJQUNILENBQUM7Q0FDRjtBQXhMRCxzQ0F3TEMiLCJzb3VyY2VzQ29udGVudCI6WyJpbXBvcnQgeyBEdXJhdGlvbiB9IGZyb20gJ2F3cy1jZGstbGliJztcbmltcG9ydCB7IFJlc3RBcGkgfSBmcm9tICdhd3MtY2RrLWxpYi9hd3MtYXBpZ2F0ZXdheSc7XG5pbXBvcnQgeyBDZm5CdWRnZXQgfSBmcm9tICdhd3MtY2RrLWxpYi9hd3MtYnVkZ2V0cyc7XG5pbXBvcnQge1xuICBBbGFybSxcbiAgQ29tcGFyaXNvbk9wZXJhdG9yLFxuICBEYXNoYm9hcmQsXG4gIEdyYXBoV2lkZ2V0LFxuICBMb2dRdWVyeVZpc3VhbGl6YXRpb25UeXBlLFxuICBMb2dRdWVyeVdpZGdldCxcbiAgTWV0cmljLFxuICBTdGF0cyxcbiAgVGV4dFdpZGdldCxcbiAgVHJlYXRNaXNzaW5nRGF0YSxcbn0gZnJvbSAnYXdzLWNkay1saWIvYXdzLWNsb3Vkd2F0Y2gnO1xuaW1wb3J0IHsgU25zQWN0aW9uIH0gZnJvbSAnYXdzLWNkay1saWIvYXdzLWNsb3Vkd2F0Y2gtYWN0aW9ucyc7XG5pbXBvcnQgeyBGdW5jdGlvbiBhcyBMYW1iZGFGdW5jdGlvbiB9IGZyb20gJ2F3cy1jZGstbGliL2F3cy1sYW1iZGEnO1xuaW1wb3J0IHsgVG9waWMgfSBmcm9tICdhd3MtY2RrLWxpYi9hd3Mtc25zJztcbmltcG9ydCB7IEVtYWlsU3Vic2NyaXB0aW9uIH0gZnJvbSAnYXdzLWNkay1saWIvYXdzLXNucy1zdWJzY3JpcHRpb25zJztcbmltcG9ydCB7IENvbnN0cnVjdCB9IGZyb20gJ2NvbnN0cnVjdHMnO1xuaW1wb3J0IHsgRW52aXJvbm1lbnRDb25maWcgfSBmcm9tICcuLi9jb25maWcnO1xuXG5leHBvcnQgaW50ZXJmYWNlIE9ic2VydmFiaWxpdHlQcm9wcyB7XG4gIHJlYWRvbmx5IGNvbmZpZzogRW52aXJvbm1lbnRDb25maWc7XG4gIHJlYWRvbmx5IHByZWZpeDogc3RyaW5nO1xuICByZWFkb25seSBhcGk6IFJlc3RBcGk7XG4gIHJlYWRvbmx5IHF1ZXJ5RnVuY3Rpb246IExhbWJkYUZ1bmN0aW9uO1xuICAvKiogV2hlcmUgYWxhcm1zIGFuZCBidWRnZXQgYWxlcnRzIGdvLiBXaXRob3V0IGl0LCBhbGFybXMgc3RpbGwgZmlyZSBidXQgbm90aWZ5IG5vYm9keS4gKi9cbiAgcmVhZG9ubHkgYWxlcnRFbWFpbD86IHN0cmluZztcbn1cblxuLyoqIE5hbWVzcGFjZSB0aGUgcXVlcnkgTGFtYmRhIHB1Ymxpc2hlcyB0byB2aWEgRW1iZWRkZWQgTWV0cmljIEZvcm1hdC4gKi9cbmNvbnN0IE1FVFJJQ19OQU1FU1BBQ0UgPSAnS2JBZ2VudCc7XG5cbi8qKlxuICogRGFzaGJvYXJkLCBhbGFybXMgYW5kIGEgY29zdCBndWFyZHJhaWwuXG4gKlxuICogVGhlIG9wZXJhdGlvbmFsIGhhbGYgYWxyZWFkeSBleGlzdHMgZWxzZXdoZXJlOiBzdHJ1Y3R1cmVkIEpTT04gbG9ncywgWC1SYXkgdHJhY2luZyBhbmRcbiAqIEFQSSBHYXRld2F5IGFjY2VzcyBsb2dzIGFyZSBjb25maWd1cmVkIHdoZXJlIHRoZSByZXNvdXJjZXMgdGhleSBkZXNjcmliZSBhcmUgY3JlYXRlZC5cbiAqIFdoYXQgdGhpcyBhZGRzIGlzIHRoZSBwYXJ0IHRoYXQgYW5zd2VycyBxdWVzdGlvbnMgd2l0aG91dCByZWFkaW5nIGxvZ3MgLS0gaXMgaXQgaGVhbHRoeSxcbiAqIGlzIGl0IGFjY3VyYXRlLCBhbmQgaXMgaXQgYWJvdXQgdG8gY29zdCBtb3JlIHRoYW4gaXQgc2hvdWxkLlxuICpcbiAqIFRoZSBhY2N1cmFjeSB3aWRnZXRzIGFyZSB0aGUgaW50ZXJlc3Rpbmcgb25lcy4gQ29uZmlkZW5jZSBhbmQgYWJzdGVudGlvbiByYXRlIGFyZSBub3RcbiAqIGluZnJhc3RydWN0dXJlIG1ldHJpY3M7IHRoZXkgYXJlIHRoZSBvbmVzIHRoYXQgd291bGQgcmV2ZWFsIHRoZSBrbm93bGVkZ2UgYmFzZSBnb2luZ1xuICogc3RhbGUsIG9yIGEgcHJvdmlkZXIgc2lsZW50bHkgZGVncmFkaW5nLCBsb25nIGJlZm9yZSBhbnl0aGluZyBzdGFydHMgZXJyb3JpbmcuXG4gKi9cbmV4cG9ydCBjbGFzcyBPYnNlcnZhYmlsaXR5IGV4dGVuZHMgQ29uc3RydWN0IHtcbiAgcHVibGljIHJlYWRvbmx5IGRhc2hib2FyZDogRGFzaGJvYXJkO1xuXG4gIGNvbnN0cnVjdG9yKHNjb3BlOiBDb25zdHJ1Y3QsIGlkOiBzdHJpbmcsIHByb3BzOiBPYnNlcnZhYmlsaXR5UHJvcHMpIHtcbiAgICBzdXBlcihzY29wZSwgaWQpO1xuXG4gICAgY29uc3QgeyBjb25maWcsIHByZWZpeCwgYXBpLCBxdWVyeUZ1bmN0aW9uLCBhbGVydEVtYWlsIH0gPSBwcm9wcztcbiAgICBjb25zdCBuYW1lUHJlZml4ID0gYCR7cHJlZml4fS0ke2NvbmZpZy5lbnZOYW1lfWA7XG5cbiAgICBjb25zdCB0b3BpYyA9IGFsZXJ0RW1haWwgPyBuZXcgVG9waWModGhpcywgJ0FsZXJ0cycsIHsgdG9waWNOYW1lOiBgJHtuYW1lUHJlZml4fS1hbGVydHNgIH0pIDogdW5kZWZpbmVkO1xuICAgIGlmICh0b3BpYyAmJiBhbGVydEVtYWlsKSB7XG4gICAgICB0b3BpYy5hZGRTdWJzY3JpcHRpb24obmV3IEVtYWlsU3Vic2NyaXB0aW9uKGFsZXJ0RW1haWwpKTtcbiAgICB9XG5cbiAgICBjb25zdCBidXNpbmVzc01ldHJpYyA9IChtZXRyaWNOYW1lOiBzdHJpbmcsIHN0YXRpc3RpYzogc3RyaW5nKSA9PlxuICAgICAgbmV3IE1ldHJpYyh7XG4gICAgICAgIG5hbWVzcGFjZTogTUVUUklDX05BTUVTUEFDRSxcbiAgICAgICAgbWV0cmljTmFtZSxcbiAgICAgICAgc3RhdGlzdGljLFxuICAgICAgICBwZXJpb2Q6IER1cmF0aW9uLm1pbnV0ZXMoNSksXG4gICAgICAgIGRpbWVuc2lvbnNNYXA6IHsgcHJvdmlkZXI6ICdvcGVucm91dGVyJyB9LFxuICAgICAgfSk7XG5cbiAgICB0aGlzLmRhc2hib2FyZCA9IG5ldyBEYXNoYm9hcmQodGhpcywgJ0Rhc2hib2FyZCcsIHtcbiAgICAgIGRhc2hib2FyZE5hbWU6IG5hbWVQcmVmaXgsXG4gICAgICBkZWZhdWx0SW50ZXJ2YWw6IER1cmF0aW9uLmhvdXJzKDMpLFxuICAgIH0pO1xuXG4gICAgdGhpcy5kYXNoYm9hcmQuYWRkV2lkZ2V0cyhcbiAgICAgIG5ldyBUZXh0V2lkZ2V0KHtcbiAgICAgICAgbWFya2Rvd246IFtcbiAgICAgICAgICBgIyAke25hbWVQcmVmaXh9YCxcbiAgICAgICAgICAnJyxcbiAgICAgICAgICAnT25lIGByZXF1ZXN0X2lkYCBsaW5rcyBldmVyeSByb3cgYmVsb3c6IHRoZSBBUEkgR2F0ZXdheSBhY2Nlc3MgbG9nLCB0aGUgTGFtYmRhJyxcbiAgICAgICAgICAnc3RydWN0dXJlZCBsb2csIHRoZSBYLVJheSB0cmFjZSwgdGhlIER5bmFtb0RCIHF1ZXJ5LWxvZyBpdGVtLCBhbmQgdGhlIEpTT04gdGhlJyxcbiAgICAgICAgICAnY2FsbGVyIHJlY2VpdmVkLiBTdGFydCBmcm9tIGEgcmVxdWVzdCBpZCBhbmQgdGhlIHdob2xlIHBhdGggaXMgcmVjb25zdHJ1Y3RpYmxlLicsXG4gICAgICAgIF0uam9pbignXFxuJyksXG4gICAgICAgIHdpZHRoOiAyNCxcbiAgICAgICAgaGVpZ2h0OiAzLFxuICAgICAgfSksXG4gICAgKTtcblxuICAgIHRoaXMuZGFzaGJvYXJkLmFkZFdpZGdldHMoXG4gICAgICBuZXcgR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ0FQSSB0cmFmZmljIGFuZCBlcnJvcnMnLFxuICAgICAgICBsZWZ0OiBbYXBpLm1ldHJpY0NvdW50KHsgc3RhdGlzdGljOiBTdGF0cy5TVU0sIGxhYmVsOiAnUmVxdWVzdHMnIH0pXSxcbiAgICAgICAgcmlnaHQ6IFtcbiAgICAgICAgICBhcGkubWV0cmljQ2xpZW50RXJyb3IoeyBzdGF0aXN0aWM6IFN0YXRzLlNVTSwgbGFiZWw6ICc0WFggKG1vc3RseSBhdXRoKScgfSksXG4gICAgICAgICAgYXBpLm1ldHJpY1NlcnZlckVycm9yKHsgc3RhdGlzdGljOiBTdGF0cy5TVU0sIGxhYmVsOiAnNVhYJyB9KSxcbiAgICAgICAgXSxcbiAgICAgICAgd2lkdGg6IDEyLFxuICAgICAgICBoZWlnaHQ6IDYsXG4gICAgICB9KSxcbiAgICAgIG5ldyBHcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnRW5kLXRvLWVuZCBsYXRlbmN5JyxcbiAgICAgICAgbGVmdDogW1xuICAgICAgICAgIGFwaS5tZXRyaWNMYXRlbmN5KHsgc3RhdGlzdGljOiAncDUwJywgbGFiZWw6ICdwNTAnIH0pLFxuICAgICAgICAgIGFwaS5tZXRyaWNMYXRlbmN5KHsgc3RhdGlzdGljOiAncDkwJywgbGFiZWw6ICdwOTAnIH0pLFxuICAgICAgICAgIGFwaS5tZXRyaWNMYXRlbmN5KHsgc3RhdGlzdGljOiAncDk5JywgbGFiZWw6ICdwOTknIH0pLFxuICAgICAgICBdLFxuICAgICAgICBsZWZ0WUF4aXM6IHsgbGFiZWw6ICdtcycsIHNob3dVbml0czogZmFsc2UgfSxcbiAgICAgICAgd2lkdGg6IDEyLFxuICAgICAgICBoZWlnaHQ6IDYsXG4gICAgICB9KSxcbiAgICApO1xuXG4gICAgdGhpcy5kYXNoYm9hcmQuYWRkV2lkZ2V0cyhcbiAgICAgIG5ldyBHcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnUXVlcnkgTGFtYmRhJyxcbiAgICAgICAgbGVmdDogW1xuICAgICAgICAgIHF1ZXJ5RnVuY3Rpb24ubWV0cmljSW52b2NhdGlvbnMoeyBzdGF0aXN0aWM6IFN0YXRzLlNVTSwgbGFiZWw6ICdJbnZvY2F0aW9ucycgfSksXG4gICAgICAgICAgcXVlcnlGdW5jdGlvbi5tZXRyaWNFcnJvcnMoeyBzdGF0aXN0aWM6IFN0YXRzLlNVTSwgbGFiZWw6ICdFcnJvcnMnIH0pLFxuICAgICAgICAgIHF1ZXJ5RnVuY3Rpb24ubWV0cmljVGhyb3R0bGVzKHsgc3RhdGlzdGljOiBTdGF0cy5TVU0sIGxhYmVsOiAnVGhyb3R0bGVzJyB9KSxcbiAgICAgICAgXSxcbiAgICAgICAgcmlnaHQ6IFtxdWVyeUZ1bmN0aW9uLm1ldHJpY0R1cmF0aW9uKHsgc3RhdGlzdGljOiAncDkwJywgbGFiZWw6ICdEdXJhdGlvbiBwOTAnIH0pXSxcbiAgICAgICAgd2lkdGg6IDEyLFxuICAgICAgICBoZWlnaHQ6IDYsXG4gICAgICB9KSxcbiAgICAgIG5ldyBHcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnQW5zd2VyIHF1YWxpdHknLFxuICAgICAgICBsZWZ0OiBbXG4gICAgICAgICAgYnVzaW5lc3NNZXRyaWMoJ1F1ZXJ5Q29uZmlkZW5jZScsIFN0YXRzLkFWRVJBR0UpLFxuICAgICAgICAgIGJ1c2luZXNzTWV0cmljKCdBYnN0ZW50aW9uJywgU3RhdHMuQVZFUkFHRSksXG4gICAgICAgIF0sXG4gICAgICAgIGxlZnRZQXhpczogeyBtaW46IDAsIG1heDogMSwgc2hvd1VuaXRzOiBmYWxzZSB9LFxuICAgICAgICB3aWR0aDogMTIsXG4gICAgICAgIGhlaWdodDogNixcbiAgICAgIH0pLFxuICAgICk7XG5cbiAgICB0aGlzLmRhc2hib2FyZC5hZGRXaWRnZXRzKFxuICAgICAgbmV3IEdyYXBoV2lkZ2V0KHtcbiAgICAgICAgdGl0bGU6ICdQcm92aWRlciB0b2tlbiBzcGVuZCcsXG4gICAgICAgIGxlZnQ6IFtcbiAgICAgICAgICBidXNpbmVzc01ldHJpYygnUHJvdmlkZXJJbnB1dFRva2VucycsIFN0YXRzLlNVTSksXG4gICAgICAgICAgYnVzaW5lc3NNZXRyaWMoJ1Byb3ZpZGVyT3V0cHV0VG9rZW5zJywgU3RhdHMuU1VNKSxcbiAgICAgICAgXSxcbiAgICAgICAgd2lkdGg6IDEyLFxuICAgICAgICBoZWlnaHQ6IDYsXG4gICAgICB9KSxcbiAgICAgIG5ldyBMb2dRdWVyeVdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnUmVjZW50IGVycm9ycyBhbmQgYWJzdGVudGlvbnMnLFxuICAgICAgICBsb2dHcm91cE5hbWVzOiBbYC9hd3MvbGFtYmRhLyR7bmFtZVByZWZpeH0tcXVlcnlgXSxcbiAgICAgICAgdmlldzogTG9nUXVlcnlWaXN1YWxpemF0aW9uVHlwZS5UQUJMRSxcbiAgICAgICAgcXVlcnlMaW5lczogW1xuICAgICAgICAgICdmaWVsZHMgQHRpbWVzdGFtcCwgbWVzc2FnZSwgY29ycmVsYXRpb25faWQsIGVycm9yLCBncm91bmRpbmcsIGNvbmZpZGVuY2UnLFxuICAgICAgICAgICdmaWx0ZXIgbGV2ZWwgPSBcIkVSUk9SXCIgb3IgYWJzdGFpbmVkID0gMScsXG4gICAgICAgICAgJ3NvcnQgQHRpbWVzdGFtcCBkZXNjJyxcbiAgICAgICAgICAnbGltaXQgMjAnLFxuICAgICAgICBdLFxuICAgICAgICB3aWR0aDogMTIsXG4gICAgICAgIGhlaWdodDogNixcbiAgICAgIH0pLFxuICAgICk7XG5cbiAgICAvLyAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tIGFsYXJtc1xuXG4gICAgY29uc3QgYWxhcm1zID0gW1xuICAgICAgbmV3IEFsYXJtKHRoaXMsICdBcGlTZXJ2ZXJFcnJvcnMnLCB7XG4gICAgICAgIGFsYXJtTmFtZTogYCR7bmFtZVByZWZpeH0tYXBpLTV4eGAsXG4gICAgICAgIGFsYXJtRGVzY3JpcHRpb246ICdUaGUgQVBJIGlzIHJldHVybmluZyBzZXJ2ZXIgZXJyb3JzLiBTb21ldGhpbmcgaXMgYnJva2VuLCBub3QgbWVyZWx5IHJlamVjdGVkLicsXG4gICAgICAgIG1ldHJpYzogYXBpLm1ldHJpY1NlcnZlckVycm9yKHsgc3RhdGlzdGljOiBTdGF0cy5TVU0sIHBlcmlvZDogRHVyYXRpb24ubWludXRlcyg1KSB9KSxcbiAgICAgICAgdGhyZXNob2xkOiAzLFxuICAgICAgICBldmFsdWF0aW9uUGVyaW9kczogMSxcbiAgICAgICAgY29tcGFyaXNvbk9wZXJhdG9yOiBDb21wYXJpc29uT3BlcmF0b3IuR1JFQVRFUl9USEFOX1RIUkVTSE9MRCxcbiAgICAgICAgLy8gTm8gdHJhZmZpYyBpcyBub3QgYSBmYWlsdXJlOyB0aGlzIHN5c3RlbSBpcyBpZGxlIG1vc3Qgb2YgdGhlIHRpbWUuXG4gICAgICAgIHRyZWF0TWlzc2luZ0RhdGE6IFRyZWF0TWlzc2luZ0RhdGEuTk9UX0JSRUFDSElORyxcbiAgICAgIH0pLFxuXG4gICAgICBuZXcgQWxhcm0odGhpcywgJ1F1ZXJ5RnVuY3Rpb25FcnJvcnMnLCB7XG4gICAgICAgIGFsYXJtTmFtZTogYCR7bmFtZVByZWZpeH0tcXVlcnktZXJyb3JzYCxcbiAgICAgICAgYWxhcm1EZXNjcmlwdGlvbjogJ1RoZSBxdWVyeSBMYW1iZGEgaXMgdGhyb3dpbmcuIENoZWNrIHRoZSBzdHJ1Y3R1cmVkIGxvZyBmb3IgdGhlIHJlcXVlc3QgaWQuJyxcbiAgICAgICAgbWV0cmljOiBxdWVyeUZ1bmN0aW9uLm1ldHJpY0Vycm9ycyh7IHN0YXRpc3RpYzogU3RhdHMuU1VNLCBwZXJpb2Q6IER1cmF0aW9uLm1pbnV0ZXMoNSkgfSksXG4gICAgICAgIHRocmVzaG9sZDogMyxcbiAgICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDEsXG4gICAgICAgIGNvbXBhcmlzb25PcGVyYXRvcjogQ29tcGFyaXNvbk9wZXJhdG9yLkdSRUFURVJfVEhBTl9USFJFU0hPTEQsXG4gICAgICAgIHRyZWF0TWlzc2luZ0RhdGE6IFRyZWF0TWlzc2luZ0RhdGEuTk9UX0JSRUFDSElORyxcbiAgICAgIH0pLFxuXG4gICAgICBuZXcgQWxhcm0odGhpcywgJ0xhdGVuY3lEZWdyYWRlZCcsIHtcbiAgICAgICAgYWxhcm1OYW1lOiBgJHtuYW1lUHJlZml4fS1sYXRlbmN5LXA5OWAsXG4gICAgICAgIC8vIE1lYXN1cmVkIHA1MCBpcyB+Mi43IHMsIGRvbWluYXRlZCBieSB0aGUgb2ZmLUFXUyBwcm92aWRlciBjYWxsLiAxMCBzIG1lYW5zIHRoZVxuICAgICAgICAvLyBwcm92aWRlciBpcyBkZWdyYWRpbmcgb3IgYSByZXRyeSBzdG9ybSBpcyB1bmRlciB3YXksIG5vdCB0aGF0IHdlIGFyZSBzbG93LlxuICAgICAgICBhbGFybURlc2NyaXB0aW9uOiAncDk5IGxhdGVuY3kgYWJvdmUgMTBzIGFjcm9zcyB0d28gcGVyaW9kcy4gVXN1YWxseSB0aGUgbW9kZWwgcHJvdmlkZXIuJyxcbiAgICAgICAgbWV0cmljOiBhcGkubWV0cmljTGF0ZW5jeSh7IHN0YXRpc3RpYzogJ3A5OScsIHBlcmlvZDogRHVyYXRpb24ubWludXRlcyg1KSB9KSxcbiAgICAgICAgdGhyZXNob2xkOiAxMF8wMDAsXG4gICAgICAgIGV2YWx1YXRpb25QZXJpb2RzOiAyLFxuICAgICAgICBjb21wYXJpc29uT3BlcmF0b3I6IENvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fVEhSRVNIT0xELFxuICAgICAgICB0cmVhdE1pc3NpbmdEYXRhOiBUcmVhdE1pc3NpbmdEYXRhLk5PVF9CUkVBQ0hJTkcsXG4gICAgICB9KSxcbiAgICBdO1xuXG4gICAgaWYgKHRvcGljKSB7XG4gICAgICBmb3IgKGNvbnN0IGFsYXJtIG9mIGFsYXJtcykge1xuICAgICAgICBhbGFybS5hZGRBbGFybUFjdGlvbihuZXcgU25zQWN0aW9uKHRvcGljKSk7XG4gICAgICB9XG4gICAgfVxuXG4gICAgLy8gLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLSBidWRnZXRcblxuICAgIC8vIFRoZSBmaXJzdCB0d28gYnVkZ2V0cyBwZXIgYWNjb3VudCBhcmUgZnJlZS4gV2l0aG91dCBhIHN1YnNjcmliZXIgYSBidWRnZXQgY2Fubm90XG4gICAgLy8gbm90aWZ5IGFueW9uZSwgc28gaXQgaXMgb25seSBjcmVhdGVkIHdoZW4gdGhlcmUgaXMgc29tZXdoZXJlIHRvIHNlbmQgdGhlIGFsZXJ0LlxuICAgIGlmIChjb25maWcuYnVkZ2V0TGltaXRVc2QgJiYgYWxlcnRFbWFpbCkge1xuICAgICAgbmV3IENmbkJ1ZGdldCh0aGlzLCAnQnVkZ2V0Jywge1xuICAgICAgICBidWRnZXQ6IHtcbiAgICAgICAgICBidWRnZXROYW1lOiBgJHtuYW1lUHJlZml4fS1tb250aGx5YCxcbiAgICAgICAgICBidWRnZXRUeXBlOiAnQ09TVCcsXG4gICAgICAgICAgdGltZVVuaXQ6ICdNT05USExZJyxcbiAgICAgICAgICBidWRnZXRMaW1pdDogeyBhbW91bnQ6IGNvbmZpZy5idWRnZXRMaW1pdFVzZCwgdW5pdDogJ1VTRCcgfSxcbiAgICAgICAgfSxcbiAgICAgICAgLy8gVGhyZWUgdGhyZXNob2xkcyByYXRoZXIgdGhhbiBvbmU6IHRoZSBwb2ludCBpcyB0byBub3RpY2UgYSB0cmVuZCBlYXJseSBlbm91Z2ggdG9cbiAgICAgICAgLy8gYWN0LCBub3QgdG8gYmUgdG9sZCBhZnRlciB0aGUgbGltaXQgaXMgYWxyZWFkeSBnb25lLlxuICAgICAgICBub3RpZmljYXRpb25zV2l0aFN1YnNjcmliZXJzOiBbMjUsIDUwLCA3NV0ubWFwKCh0aHJlc2hvbGQpID0+ICh7XG4gICAgICAgICAgbm90aWZpY2F0aW9uOiB7XG4gICAgICAgICAgICBub3RpZmljYXRpb25UeXBlOiAnQUNUVUFMJyxcbiAgICAgICAgICAgIGNvbXBhcmlzb25PcGVyYXRvcjogJ0dSRUFURVJfVEhBTicsXG4gICAgICAgICAgICB0aHJlc2hvbGQsXG4gICAgICAgICAgICB0aHJlc2hvbGRUeXBlOiAnUEVSQ0VOVEFHRScsXG4gICAgICAgICAgfSxcbiAgICAgICAgICBzdWJzY3JpYmVyczogW3sgc3Vic2NyaXB0aW9uVHlwZTogJ0VNQUlMJywgYWRkcmVzczogYWxlcnRFbWFpbCB9XSxcbiAgICAgICAgfSkpLFxuICAgICAgfSk7XG4gICAgfVxuICB9XG59XG4iXX0=