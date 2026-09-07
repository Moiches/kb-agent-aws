# Enterprise Service Level Agreement

**Document owner:** Customer Success
**Last reviewed:** 2026-02-02
**Applies to:** Enterprise plan customers only

## Uptime commitment

Northwind Analytics commits to a monthly uptime of **99.9%** for the production API and web application.
Uptime is measured per calendar month, excluding scheduled maintenance announced at least 72 hours in
advance and excluding failures caused by the customer's own integrations.

## Severity levels and response times

| Severity | Definition | Initial response | Target resolution |
|---|---|---|---|
| S1 — Critical | Production is fully unavailable, or data loss is occurring | 15 minutes | 4 hours |
| S2 — High | Major feature unusable; no workaround available | 1 hour | 1 business day |
| S3 — Medium | Feature degraded; workaround exists | 4 business hours | 5 business days |
| S4 — Low | Question, cosmetic defect, or feature request | 1 business day | Next release cycle |

S1 and S2 tickets are handled 24x7. S3 and S4 tickets are handled during business hours in the
customer's assigned support region.

## Service credits

If monthly uptime falls below the commitment, the customer may claim a service credit against the next
invoice:

| Monthly uptime | Service credit |
|---|---|
| Below 99.9% but at or above 99.0% | 10% of monthly fee |
| Below 99.0% but at or above 95.0% | 25% of monthly fee |
| Below 95.0% | 50% of monthly fee |

Service credits are the sole and exclusive remedy for missed uptime commitments. **A service credit is
not a refund**: credits are applied against future invoices and are never paid out in cash. Refunds are
governed by the separate Refund and Cancellation Policy.

To claim a credit, the customer must submit a request within 30 days of the end of the affected month,
including the dates and times of the outage. Credits are not applied automatically.

## Support channels

Enterprise customers receive a dedicated Slack Connect channel, a named technical account manager, and
access to the 24x7 emergency paging line for S1 incidents. The paging line is reserved for S1 only;
misuse may result in the channel being revoked.

## Exclusions

The uptime commitment does not apply to sandbox environments, beta features explicitly labelled as
preview, or degradation caused by the customer exceeding published rate limits.
