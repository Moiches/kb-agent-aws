# Security and Data Handling

**Document owner:** Security Engineering
**Last reviewed:** 2026-03-10
**Applies to:** All plans

## Encryption

All customer data is encrypted at rest using AES-256. Encryption keys are managed by the cloud provider's
key management service and rotated annually. Data in transit is encrypted with TLS 1.2 or higher;
TLS 1.0 and 1.1 were disabled in January 2025 and connections using them are rejected.

Database backups are encrypted with the same key hierarchy as the primary store and are stored in the
same region as the source data.

## Data residency

Customer data is stored in the region selected at account creation. Two regions are available:

- **US** — data stored in AWS `us-east-1`, backups replicated to `us-west-2`
- **EU** — data stored in AWS `eu-west-1`, backups replicated to `eu-central-1`

Data does not leave the selected region except for aggregated, non-identifying telemetry used for
capacity planning. Customers cannot change region after account creation; migration requires a new
account and a supported export/import.

## Subprocessors

Northwind Analytics uses the following categories of subprocessor: cloud infrastructure, transactional
email delivery, error monitoring, and payment processing. The current list of named subprocessors is
published at the trust page and customers on Enterprise plans are notified 30 days before any addition.

## Data retention

Active customer data is retained for the life of the account. After termination:

- Production data is retained for **90 days**, then permanently deleted
- Backups containing customer data expire on a rolling **35-day** schedule
- Audit logs are retained for **13 months** regardless of account status

Customers may request earlier deletion in writing; deletion is completed within 30 days of the request.

## Access control

Northwind staff access to production is granted through short-lived, role-based credentials with
mandatory multi-factor authentication. Access is logged and reviewed quarterly. No engineer holds
standing production database credentials; access requires an approved, time-boxed elevation request.

## Incident and breach notification

Confirmed security incidents affecting customer data are reported to affected customers within
**72 hours** of confirmation. The notification includes what was affected, what has been done, and what
the customer should do. A written post-incident review is provided within 14 days.

## Certifications

Northwind Analytics maintains SOC 2 Type II certification, renewed annually. The current report is
available to customers under NDA on request. Penetration tests are performed by an external firm twice
per year, and a summary letter is available on request.
