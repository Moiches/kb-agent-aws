# Acceptable Use Policy

**Document owner:** Legal and Trust
**Last reviewed:** 2026-01-08
**Applies to:** All users of Northwind Analytics services

## Prohibited uses

Customers may not use Northwind Analytics services to:

- Store or process data the customer has no lawful right to process
- Attempt to gain unauthorized access to any system, account, or dataset, including other tenants
- Reverse engineer, decompile, or attempt to extract the source code of the service
- Resell, sublicense, or provide the service to third parties as a standalone offering without a
  written reseller agreement
- Send unsolicited bulk messages using data exported from the platform
- Circumvent rate limits, quotas, or spending caps through multiple accounts

## Automated access

Automated and programmatic access is expected and encouraged, subject to the published rate limits.
Customers must send a descriptive `User-Agent` header identifying the integration, and must honour
HTTP 429 responses with exponential backoff. Clients that retry aggressively against a 429 response may
be blocked at the edge without notice.

Scraping the web interface instead of using the documented API is not permitted.

## Content standards

Customers are responsible for the content they upload. Northwind does not routinely inspect customer
data, but will act on credible reports of content that is unlawful in the jurisdiction where it is
stored.

## Suspension and termination

Northwind may suspend an account without prior notice where continued operation poses an immediate risk
to the platform, to other customers, or to third parties. For all other violations, Northwind gives
written notice and a **10 business day** window to remediate before suspension.

Suspension for an acceptable use violation does not entitle the customer to a refund or to service
credits for the period of suspension.

## Security research

Northwind welcomes good-faith security research. Researchers who follow the published disclosure policy,
avoid accessing other customers' data, and do not degrade the service will not face legal action.
Testing must be performed against a sandbox environment or the researcher's own organization, never
against another customer's production data. Report findings to security@northwind-analytics.example.

## Changes to this policy

Northwind may update this policy with 30 days' notice to account administrators. Continued use after
the effective date constitutes acceptance.
