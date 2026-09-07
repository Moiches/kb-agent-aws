# Customer Onboarding FAQ

**Document owner:** Customer Success
**Last reviewed:** 2026-02-25
**Applies to:** New customers on all plans

## How long does onboarding take?

A standard Business onboarding takes 3 to 5 business days from contract signature to first production
query. Enterprise onboarding with SSO, custom data pipelines, and a security review typically takes 2 to
3 weeks. The pace is usually set by how quickly the customer's IT team can complete the identity
provider configuration.

## How do I set up SAML single sign-on?

SAML SSO is available on Business and Enterprise plans. Configuration takes four steps:

1. In Northwind, open **Settings → Authentication → SAML** and copy the Assertion Consumer Service URL
   and the Entity ID.
2. In your identity provider, create a new SAML application using those two values. Supported providers
   include Okta, Microsoft Entra ID, Google Workspace, and any provider that supports SAML 2.0.
3. Map the required attributes. Northwind requires `email` and `displayName`; the optional `groups`
   attribute enables automatic role assignment.
4. Paste the identity provider metadata XML back into Northwind and click **Verify**. Verification runs a
   test assertion and reports errors inline.

Once SSO is verified, an administrator can enforce it for the organization, which disables password
login for all members except break-glass accounts. We strongly recommend keeping at least one
break-glass administrator with password login enabled.

SCIM user provisioning is a separate feature available on Enterprise only.

## What are the data import limits?

The bulk import endpoint accepts CSV and newline-delimited JSON. A single import job accepts up to
**500 MB** or **5 million rows**, whichever comes first. Larger datasets must be split into multiple
jobs. Import jobs run asynchronously and typically complete within 20 minutes.

Imports are validated before they are applied. If more than 5% of rows fail validation, the entire job
is rejected and a per-row error report is produced so the customer can correct the source file.

## Can I try the product before buying?

Yes. Every new organization gets a 14-day trial of the Business plan with a 100,000 request allowance
and no credit card required. Trials do not include SSO or the sandbox environment. At the end of the
trial the organization is downgraded to Starter unless a plan is selected.

## Is there a sandbox environment?

Sandbox environments are included on Enterprise plans and available as a paid add-on for Business.
Sandbox data is isolated from production and is reset every 30 days. The uptime commitment in the
Enterprise SLA does not apply to sandbox.

## Who do I contact during onboarding?

Business customers work with the onboarding team at onboarding@northwind-analytics.example. Enterprise
customers are assigned a named technical account manager at contract signature, who remains the primary
contact after onboarding completes.
