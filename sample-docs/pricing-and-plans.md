# Pricing and Plans

**Document owner:** Revenue Operations
**Last reviewed:** 2026-01-30
**Applies to:** All prospective and current customers

## Plan tiers

| Plan | Monthly price | Included API requests / month | Seats included |
|---|---|---|---|
| Starter | $49 | 50,000 | 3 |
| Business | $399 | 1,000,000 | 25 |
| Enterprise | Custom (from $2,000) | 10,000,000 | Unlimited |

Prices are per organization, in US dollars, and exclude applicable taxes.

## API request quotas and overages

The included request allowance resets on the first day of each billing period. Requests are counted per
successful API call; calls that return a 4xx error caused by the client are not counted, but calls that
return 429 rate-limit responses are also not counted.

If an organization exceeds its included allowance, overage is billed automatically at the end of the
billing period:

| Plan | Overage rate |
|---|---|
| Starter | $2.00 per 10,000 additional requests |
| Business | $1.20 per 10,000 additional requests |
| Enterprise | $0.60 per 10,000 additional requests |

Service is **not** suspended when the allowance is exceeded. Customers who prefer a hard stop can enable
a spending cap in billing settings, which returns HTTP 429 once the cap is reached. Overage charges are
never refundable.

Account administrators receive email alerts at 80% and 100% of the included allowance.

## Rate limits

Rate limits are independent of the monthly quota and apply per organization:

- Starter — 10 requests per second
- Business — 50 requests per second
- Enterprise — 200 requests per second, raisable on request

## Annual commitment discount

Customers who commit to a 12-month term paid annually in advance receive a **15% discount** on the
subscription fee. The discount applies to the subscription only, not to overage charges or professional
services. Annual contracts renew automatically unless cancelled 30 days before the renewal date.

## Upgrades and downgrades

Upgrades take effect immediately and are prorated for the remainder of the billing period. Downgrades
take effect at the start of the next billing period; Northwind does not issue mid-period credits for
downgrades. An organization that downgrades below its current seat count must remove seats first.

## Payment methods

Starter and Business plans are billed by credit card. Enterprise customers may pay by invoice with
NET 30 terms, subject to a credit check. Purchase orders are supported on Enterprise only.

## Non-profit and education pricing

Registered non-profits and accredited educational institutions receive 30% off Business plan pricing.
Eligibility is verified once per year and must be requested through the sales team.
